import json
import secrets
import re
import threading
import time
import paho.mqtt.client as mqtt
from .settings import CONTROLS, readable_settings
from .telemetry import timestamp_age


def test_connection(config, timeout=6):
    """Complete an MQTT CONNECT handshake without publishing or saving settings."""
    if not config.get('host'):
        return False, 'No broker address is available.'
    finished = threading.Event()
    result = {'ok': False, 'message': 'The broker did not respond in time.'}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='solarsynkv4-test-' + secrets.token_hex(4), protocol=mqtt.MQTTv5)
    if config.get('username'):
        client.username_pw_set(config['username'], config.get('password') or None)
    if config.get('tls'):
        client.tls_set()
    def connected(client, userdata, flags, reason_code, properties):
        result['ok'] = not reason_code.is_failure
        result['message'] = 'MQTT connection succeeded.' if result['ok'] else 'The broker rejected the connection or credentials.'
        finished.set()
    client.on_connect = connected
    def connect_failed(client, userdata):
        result['message'] = 'Could not reach the broker. Check its address, port and TLS setting.'
        finished.set()
    client.on_connect_fail = connect_failed
    try:
        client.connect_async(str(config['host']), int(config['port']), keepalive=10)
        client.loop_start()
        finished.wait(timeout)
        return result['ok'], result['message']
    except (OSError, ValueError):
        return False, 'Could not reach the broker. Check its address, port and TLS setting.'
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except (OSError, RuntimeError):
            pass

class Broker:
    def __init__(self, config, store, command, on_event=None):
        self.config, self.store, self.command = config, store, command
        self.on_event = on_event or (lambda *args, **kwargs: None)
        self.client = None
        self.connected = False
        self.owned = set(store.data.get('owned', {}).values())
        self.devices = {item['sn']: dict(item) for item in store.data.get('devices', []) if item.get('sn') in store.data['selected']}
        self.control_states = {}
        self._configs = {}
        self.lock = threading.RLock()
        self._last_connect_failure = 0
        self.prefix = config.get('prefix') or 'solarsynkv4'
    def _event(self, category, message, serial='', level='info'):
        try:
            self.on_event(category, message, serial=serial, level=level)
        except OSError:
            pass
    def start(self):
        if not self.config.get('host'):
            self._event('mqtt', 'No MQTT broker address is configured.', level='warning')
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='solarsynkv4', protocol=mqtt.MQTTv5)
        if self.config.get('username'):
            client.username_pw_set(self.config['username'], self.config.get('password') or None)
        if self.config.get('tls'):
            client.tls_set()
        client.will_set(self.prefix + '/availability', 'offline', retain=True)
        client.on_connect = self._connect
        client.on_connect_fail = self._connect_failed
        client.on_disconnect = self._disconnect
        client.on_message = self._message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.client = client
        try:
            client.connect_async(self.config['host'], int(self.config.get('port') or 1883), keepalive=60)
            client.loop_start()
        except (OSError, ValueError, RuntimeError):
            self.client = None
            self.connected = False
            self._event('mqtt', 'Could not start the MQTT connection. Check broker address, port and TLS settings.', level='error')
    def stop(self):
        if self.client:
            if self.connected:
                self.client.publish(self.prefix + '/availability', 'offline', qos=1, retain=True)
            self.client.disconnect()
            self.client.loop_stop()
        self.connected = False
        self.client = None
    def _connect_failed(self, client, userdata):
        if client is not self.client:
            return
        now = time.time()
        if now - self._last_connect_failure >= 60:
            self._last_connect_failure = now
            self._event('mqtt', 'Could not reach the MQTT broker. Check its address, port and TLS setting.', level='warning')
    def _connect(self, client, userdata, flags, reason_code, properties):
        if client is not self.client:
            return
        self.connected = not reason_code.is_failure
        if not self.connected:
            self._event('mqtt', 'The broker rejected the MQTT connection. Check credentials and permissions.', level='error')
            return
        client.subscribe('homeassistant/status')
        client.subscribe(self.prefix + '/+/set/+')
        client.publish(self.prefix + '/availability', 'online', qos=1, retain=True)
        for serial in self.store.data['selected']:
            client.publish(f'{self.prefix}/{serial}/availability', 'offline', qos=1, retain=True)
        for record, topic in list(self.store.data.get('owned', {}).items()):
            if record.split(':', 1)[0] not in self.store.data['selected']:
                client.publish(topic, '', qos=1, retain=True)
                del self.store.data['owned'][record]
        self.store.save()
        self.republish(force=True)
        self._replay_cached()
        self._event('mqtt', 'Connected to the MQTT broker.')
    def _disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if client is not self.client:
            return
        was_connected = self.connected
        self.connected = False
        if was_connected:
            self._event('mqtt', 'Disconnected from the MQTT broker.', level='warning')
    def _message(self, client, userdata, msg):
        if client is not self.client:
            return
        if msg.topic == 'homeassistant/status':
            if msg.payload == b'online':
                self.republish(force=True)
                self._replay_cached()
            return
        if msg.retain:
            return
        parts = msg.topic.split('/')
        if len(parts) != 4 or parts[0] != self.prefix or parts[2] != 'set':
            return
        serial, key = parts[1], parts[3]
        if serial not in self.store.data['selected'] or key not in CONTROLS:
            return
        try:
            self.command(serial, key, msg.payload.decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            self._event('settings', f'Rejected MQTT command for {key}: {exc}', serial=serial, level='warning')
    def publish(self, topic, value, retain=False):
        if self.connected and self.client:
            return self.client.publish(topic, value if isinstance(value, str) else json.dumps(value), qos=1, retain=retain)
        return None
    def _device(self, serial):
        info = self.devices.get(serial, {})
        return {'identifiers': ['solarsynkv4_' + serial], 'name': 'SolarsynkV4', 'manufacturer': 'Sunsynk', 'model': info.get('model') or 'Inverter', 'serial_number': serial}
    def entity(self, serial, key, name, kind='sensor', unit=None, device_class=None, state_class=None, control=False, category=None, app_only=False):
        if serial not in self.store.data['selected'] or not re.fullmatch(r'[A-Za-z0-9_-]+', serial):
            return
        topic = f'homeassistant/{kind}/solarsynkv4_{serial}/{key}/config'
        base = f'{self.prefix}/{serial}/{key}'
        availability = [{'topic': self.prefix + '/availability'}]
        if not app_only:
            availability.append({'topic': self.prefix + '/' + serial + '/availability'})
        payload = {'name': name, 'unique_id': f'solarsynkv4_{serial}_{key}', 'device': self._device(serial), 'state_topic': base + '/state', 'availability': availability, 'availability_mode': 'all'}
        if category:
            payload['entity_category'] = category
        if unit:
            payload['unit_of_measurement'] = unit
        if device_class:
            payload['device_class'] = device_class
        if state_class:
            payload['state_class'] = state_class
        if control:
            payload['command_topic'] = f'{self.prefix}/{serial}/set/{key}'
            payload['json_attributes_topic'] = base + '/attributes'
            definition = CONTROLS[key]
            if kind == 'number':
                payload.update({'min': definition[3], 'max': definition[4], 'step': 1 if definition[1] == 'integer' else 0.01, 'mode': 'box'})
            if definition[1] == 'time':
                payload.update({'min': 5, 'max': 5, 'pattern': r'^([01]\d|2[0-3]):[0-5]\d$'})
            if kind == 'switch':
                payload.update({'payload_on': 'ON', 'payload_off': 'OFF', 'state_on': 'ON', 'state_off': 'OFF'})
        encoded = json.dumps(payload, sort_keys=True)
        if self._configs.get(topic) == encoded:
            return
        self._configs[topic] = encoded
        self.owned.add(topic)
        self.store.data.setdefault('owned', {})[serial + ':' + kind + ':' + key] = topic
        self.store.save()
        self.publish(topic, encoded, retain=True)
        return True
    def republish(self, force=False):
        # Remove retained diagnostic sensors promoted to writable controls.
        retired = False
        owned = self.store.data.setdefault('owned', {})
        for serial in self.store.data['selected']:
            for key in CONTROLS:
                record = f'{serial}:sensor:setting_{key}'
                topic = owned.pop(record, None)
                if topic:
                    self.publish(topic, '', retain=True)
                    self.owned.discard(topic)
                    self._configs.pop(topic, None)
                    retired = True
        if retired:
            self.store.save()
        if force:
            self._configs.clear()
        count = 0
        for serial in self.store.data['selected']:
            info = self.devices.setdefault(serial, {})
            count += bool(self.entity(serial, 'telemetry_status', 'Telemetry status', category='diagnostic', app_only=True))
            for key, value in info.get('metrics', {}).items():
                count += bool(self.entity(serial, key, value['name'], kind=value.get('entity_type', 'sensor'), unit=value.get('unit'), device_class=value.get('kind'), state_class='total_increasing' if value.get('kind') == 'energy' and ('etotal' in key.lower() or key.endswith('totalUsed')) else 'measurement' if isinstance(value.get('value'), (int, float)) and value.get('kind') != 'energy' else None))
            for key, (name, kind, group, low, high, unit) in CONTROLS.items():
                if info.get('settings', {}).get(key) is not None:
                    entity_kind = 'number' if kind == 'integer' else 'text' if kind == 'time' else kind
                    count += bool(self.entity(serial, key, name, kind=entity_kind, unit=unit, control=True))
            for key in readable_settings(info.get('settings', {})):
                if key not in CONTROLS:
                    count += bool(self.entity(serial, 'setting_' + key, 'Cloud setting ' + key, category='diagnostic'))
        if count and self.connected:
            self._event('mqtt', f'Queued {count} Home Assistant discovery configuration(s).')
        return count
    def _fresh_cached(self, info):
        age = timestamp_age(info.get('timestamp'))
        return bool(info.get('fresh') and info.get('received_at') and time.time() - info['received_at'] < 900 and age is not None and age < 900)
    def _publish_cached(self, serial):
        info = self.devices.get(serial, {})
        fresh = self._fresh_cached(info)
        self.publish(f'{self.prefix}/{serial}/availability', 'online' if fresh else 'offline', retain=True)
        status = 'Fresh' if fresh else 'Stale' if info.get('timestamp') else 'Waiting for cloud data'
        self.publish(f'{self.prefix}/{serial}/telemetry_status/state', status)
        for key, item in info.get('metrics', {}).items():
            self.publish(f'{self.prefix}/{serial}/{key}/state', str(item['value']))
        for key, value in readable_settings(info.get('settings', {})).items():
            if key not in CONTROLS:
                self.publish(f'{self.prefix}/{serial}/setting_{key}/state', value)
        for (sn, key), (value, command) in list(self.control_states.items()):
            if sn == serial:
                self.control_state(serial, key, value, command)
    def _replay_cached(self):
        for serial in self.store.data['selected']:
            self._publish_cached(serial)
    def telemetry(self, serial, info, metrics, timestamp, fresh):
        self.devices.setdefault(serial, {}).update(info)
        self.devices[serial].update({'metrics': metrics, 'timestamp': timestamp, 'fresh': fresh, 'received_at': time.time()})
        self.republish()
        if self.connected:
            self._publish_cached(serial)
            self._event('mqtt', f'Queued {len(metrics)} telemetry value(s) for Home Assistant.', serial=serial)
        else:
            self._event('mqtt', 'Cloud data fetched; MQTT is disconnected. Values will be sent after reconnection.', serial=serial, level='warning')
    def settings(self, serial, data):
        self.devices.setdefault(serial, {})['settings'] = data
        self.republish()
        if self.connected:
            for key, value in readable_settings(data).items():
                if key not in CONTROLS:
                    self.publish(f'{self.prefix}/{serial}/setting_{key}/state', value)
    def control_state(self, serial, key, value, command):
        if value is None:
            return
        self.control_states[(serial, key)] = (value, command)
        if CONTROLS[key][1] == 'switch':
            value = 'ON' if value is True or str(value).lower() in ('true', '1', 'on') else 'OFF'
        if CONTROLS[key][1] == 'time':
            value = str(value)[:5]
        self.publish(f'{self.prefix}/{serial}/{key}/state', str(value))
        self.publish(f'{self.prefix}/{serial}/{key}/attributes', {'status': command['status'] if command else 'observed', 'requested': command['value'] if command else None, 'observed': command['observed'] if command else value, 'command_id': command['id'] if command else None, 'error': command.get('error') if command else None})
    def remove(self, serial):
        for record, topic in list(self.store.data.get('owned', {}).items()):
            if record.startswith(serial + ':'):
                self.publish(topic, '', retain=True)
                self.owned.discard(topic)
                self._configs.pop(topic, None)
                del self.store.data['owned'][record]
        self.store.save()
        self.publish(f'{self.prefix}/{serial}/availability', 'offline', retain=True)
        self.devices.pop(serial, None)
        for sn, key in list(self.control_states):
            if sn == serial:
                del self.control_states[(sn, key)]
