import copy
import json
from datetime import datetime, timezone
import pathlib
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
fake_cloud_module = types.ModuleType('app.cloud')
fake_cloud_module.CloudError = type('CloudError', (Exception,), {})
sys.modules['app.cloud'] = fake_cloud_module
fake_paho = types.ModuleType('paho')
fake_paho_mqtt = types.ModuleType('paho.mqtt')
fake_paho_client = types.ModuleType('paho.mqtt.client')
sys.modules.setdefault('paho', fake_paho)
sys.modules.setdefault('paho.mqtt', fake_paho_mqtt)
sys.modules.setdefault('paho.mqtt.client', fake_paho_client)
from app.mqtt import Broker
from app.store import Store
from app.supervisor import mqtt_service, mqtt_service_status
from app.discovery import discover, merge_devices
from app.dashboard import entity_rows, uptime_label
from app.settings import SettingsManager, BATTERY, MODE, AUX, ADVANCED, CONTROLS, validate
from app.telemetry import normalise, timestamp_age

class FakeCloud:
    def __init__(self):
        self.values = {key: 1 for key in set(BATTERY + MODE + AUX + ADVANCED)}
        self.values['sn'] = 'A1'
        self.values['parallel'] = '0'
        self.values.update({f'sellTime{i}En': '0' for i in range(1, 7)})
        self.values['flag2'] = '0'
        self.values['gridPeakShaving'] = '0'
        self.values['mondayOn'] = 'true'
        self.values['tuesdayOn'] = 'false'
        self.values.update({f'{prefix}{i}': '0' for prefix in ('volt', 'current') for i in range(1, 13)})
        self.values['batteryShutdownCap'] = 20
        self.writes = []
        self.reads = 0
        self.reject = False
        self.apply_after = 3
    def inverter(self, serial):
        return {'sn': serial, 'plant': {}}
    def settings(self, serial):
        self.reads += 1
        if self.writes and self.reads >= self.apply_after:
            self.values.update(self.writes[-1])
        return copy.deepcopy(self.values)
    def write_settings(self, serial, payload):
        if self.reject:
            raise fake_cloud_module.CloudError('Rejected by cloud')
        self.writes.append(copy.deepcopy(payload))

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.data['selected'] = ['A1']
        self.cloud = FakeCloud()
        self.events = []
        self.manager = SettingsManager(self.cloud, self.store, lambda *args: self.events.append(args), .01, .01, .15, on_event=self.store.record_activity)
    def tearDown(self):
        self.wait()
        time.sleep(.03)
        self.temp.cleanup()
    def wait(self):
        for _ in range(100):
            if not self.manager.commands and not self.manager.active:
                return
            time.sleep(.01)
        self.fail('command did not finish')
    def test_delayed_value_stays_requested(self):
        self.manager.observe('A1', self.cloud.settings('A1'))
        self.cloud.apply_after = 7
        self.manager.request('A1','batteryShutdownCap',40)
        time.sleep(.04)
        self.manager.observe('A1', {'batteryShutdownCap':20})
        self.assertEqual([e for e in self.events if e[1] == 'batteryShutdownCap'][-1][2], 40)
        self.wait()
        self.assertEqual(self.events[-1][3]['status'], 'confirmed')
    def test_superseded_response_cannot_confirm_new_value(self):
        self.manager.request('A1','batteryShutdownCap',40)
        time.sleep(.025)
        self.manager.request('A1','batteryShutdownCap',50)
        self.wait()
        setting_events = [event for event in self.events if event[1] == 'batteryShutdownCap' and event[3] is not None]
        self.assertEqual(setting_events[-1][2], 50)
        self.assertEqual(setting_events[-1][3]['status'], 'confirmed')
    def test_coalesced_fields_and_unrelated_preserved(self):
        self.manager.request('A1','batteryShutdownCap',40)
        self.manager.request('A1','batteryRestartCap',50)
        self.wait()
        self.assertEqual(len(self.cloud.writes), 1)
        self.assertEqual(self.cloud.writes[0]['batteryShutdownCap'], 40)
        self.assertEqual(self.cloud.writes[0]['batteryRestartCap'], 50)
        self.assertEqual(self.cloud.writes[0]['batteryMaxCurrentCharge'], 1)
    def test_rejected_write_restores_observed_once(self):
        self.manager.observe('A1', self.cloud.settings('A1'))
        self.cloud.reject = True
        self.manager.request('A1','batteryShutdownCap',40)
        self.wait()
        self.assertEqual(self.events[-1][2],20)
        self.assertEqual(self.events[-1][3]['status'],'rejected')
    def test_timeout_is_unconfirmed(self):
        self.cloud.apply_after = 1000
        self.manager.request('A1','batteryShutdownCap',40)
        self.wait()
        self.assertEqual(self.events[-1][3]['status'],'unconfirmed')
    def test_api_success_is_logged_before_read_back(self):
        self.cloud.apply_after = 1000
        self.manager.request('A1', 'batteryShutdownCap', 40, immediate=True)
        for _ in range(50):
            if self.cloud.writes:
                break
            time.sleep(.01)
        self.assertTrue(self.cloud.writes)
        activity = [item for item in self.store.data['activity'] if item['category'] == 'settings']
        self.assertEqual(len(activity), 1)
        self.assertIn('Sunsynk API write succeeded', activity[0]['message'])
        self.assertEqual(activity[0]['serial'], 'A1')
        self.assertIn('batteryShutdownCap', activity[0]['message'])
        self.wait()
        self.assertEqual(self.store.data['history'][-1]['status'], 'unconfirmed')

    def test_parallel_page_write_keeps_pending_shared_value(self):
        self.cloud.apply_after = 1000
        self.manager.request('A1', 'batteryLowCap', 35, immediate=True)
        for _ in range(30):
            if self.cloud.writes:
                break
            time.sleep(.01)
        self.assertEqual(len(self.cloud.writes), 1)
        self.manager.request('A1', 'peakAndVallery', 'ON', immediate=True)
        for _ in range(20):
            if len(self.cloud.writes) >= 2:
                break
            time.sleep(.01)
        self.assertEqual(self.cloud.writes[1]['batteryLowCap'], 35)
        self.wait()

    def test_wrong_readback_identity_never_confirms_write(self):
        original = self.cloud.settings
        def settings_for_other_inverter_after_write(serial):
            data = original(serial)
            if self.cloud.writes:
                data['sn'] = 'OTHER'
            return data
        self.cloud.settings = settings_for_other_inverter_after_write
        self.manager.request('A1', 'batteryShutdownCap', 40, immediate=True)
        self.wait()
        self.assertEqual(len(self.cloud.writes), 1)
        self.assertEqual(self.store.data['history'][-1]['status'], 'unconfirmed')

    def test_wrong_inverter_identity_prevents_write(self):
        self.cloud.inverter = lambda serial: {'sn': 'OTHER'}
        self.manager.request('A1', 'batteryShutdownCap', 40, immediate=True)
        self.wait()
        self.assertFalse(self.cloud.writes)
        self.assertEqual(self.store.data['history'][-1]['status'], 'rejected')

    def test_rejected_api_has_no_success_event(self):
        self.cloud.reject = True
        self.manager.request('A1', 'batteryShutdownCap', 40, immediate=True)
        self.wait()
        self.assertFalse([item for item in self.store.data['activity'] if 'write succeeded' in item['message']])

    def test_immediate_request_bypasses_batch_delay(self):
        self.manager = SettingsManager(self.cloud, self.store, coalesce_seconds=.5, confirm_interval=.01, confirm_window=.15)
        self.manager.request('A1', 'batteryShutdownCap', 40, immediate=True)
        for _ in range(15):
            if self.cloud.writes:
                break
            time.sleep(.01)
        self.assertTrue(self.cloud.writes)
        self.wait()

    def test_second_entity_change_posts_while_first_waits_for_read_back(self):
        self.cloud.apply_after = 1000
        self.manager.request('A1', 'batteryShutdownCap', 40, immediate=True)
        for _ in range(30):
            if self.cloud.writes:
                break
            time.sleep(.01)
        self.assertEqual(len(self.cloud.writes), 1)
        self.manager.request('A1', 'batteryRestartCap', 50, immediate=True)
        for _ in range(10):
            if len(self.cloud.writes) >= 2:
                break
            time.sleep(.01)
        self.assertEqual(len(self.cloud.writes), 2)
        self.assertEqual(self.cloud.writes[1]['batteryShutdownCap'], 40)
        self.assertEqual(self.cloud.writes[1]['batteryRestartCap'], 50)
        self.wait()

    def test_missing_group_prevents_write(self):
        del self.cloud.values['batteryMaxCurrentCharge']
        self.manager.request('A1','batteryShutdownCap',40)
        self.wait()
        self.assertFalse(self.cloud.writes)
    def test_system_mode_payload_includes_portal_fields(self):
        self.manager.request('A1', 'solarSell', 0)
        self.wait()
        payload = self.cloud.writes[0]
        for key in ('sellTime1En', 'sellTime6En', 'gridPeakShaving', 'generatorStartCap', 'batteryLowCap'):
            self.assertIn(key, payload)

    def test_aux_write_preserves_group_and_switch_wire_type(self):
        self.cloud.values['genConnectGrid'] = '0'
        self.cloud.values['loadMode'] = '0'
        self.manager.request('A1', 'genConnectGrid', 'on')
        self.wait()
        payload = self.cloud.writes[0]
        self.assertEqual(payload['genConnectGrid'], '1')
        self.assertEqual(payload['loadMode'], '0')
        self.assertEqual(set(payload), set(AUX))

    def test_advanced_write_uses_captured_page_shape(self):
        self.manager.request('A1', 'solarMaxSellPower', 4000)
        self.wait()
        payload = self.cloud.writes[0]
        self.assertEqual(payload['solarMaxSellPower'], 4000)
        self.assertEqual(payload['gridPeakShaving'], '0')
        self.assertEqual(payload['codeMode'], 'undefined')
        self.assertEqual(payload['earthBond'], 'undefined')
        self.assertEqual(set(payload), set(ADVANCED) | {
            'codeMode', 'earthBond', 'exMeterCT', 'nightPowerSave', 'risoEn',
            'smartMeter2Enable', 'solar3WindInputEnable', 'tenOverVoltEn', 'tenOverVoltVal'})

    def test_system_mode_weekdays_use_portal_boolean_type(self):
        self.manager.request('A1', 'mondayOn', 'OFF')
        self.wait()
        payload = self.cloud.writes[0]
        self.assertIs(payload['mondayOn'], False)
        self.assertIs(payload['tuesdayOn'], False)

    def test_use_timer_switch_writes_cloud_and_has_mqtt_command(self):
        self.cloud.values['peakAndVallery'] = '1'
        self.manager.request('A1', 'peakAndVallery', 'OFF')
        self.wait()
        self.assertEqual(self.cloud.writes[0]['peakAndVallery'], '0')
        sent = []
        broker = Broker(self.store.data['mqtt'], self.store, self.manager.request)
        broker.connected = True
        broker.publish = lambda topic, value, retain=False: sent.append((topic, value, retain))
        broker.settings('A1', {'peakAndVallery': '1'})
        config = next(json.loads(value) for topic, value, _ in sent if topic == 'homeassistant/switch/solarsynkv4_A1/peakAndVallery/config')
        self.assertEqual(config['command_topic'], 'solarsynkv4/A1/set/peakAndVallery')

    def test_promoted_cloud_sensor_is_removed(self):
        old_topic = 'homeassistant/sensor/solarsynkv4_A1/setting_peakAndVallery/config'
        self.store.data['owned']['A1:sensor:setting_peakAndVallery'] = old_topic
        sent = []
        broker = Broker(self.store.data['mqtt'], self.store, self.manager.request)
        broker.connected = True
        broker.publish = lambda topic, value, retain=False: sent.append((topic, value, retain))
        broker.settings('A1', {'peakAndVallery': '1'})
        self.assertIn((old_topic, '', True), sent)
        self.assertNotIn('A1:sensor:setting_peakAndVallery', self.store.data['owned'])

    def test_grid_controls_are_not_exposed(self):
        self.assertNotIn('gridPeakShaving', CONTROLS)
        self.assertNotIn('gridPeakPower', CONTROLS)
        for key in ('parallel', 'phase', 'equipMode', 'meterSelect', 'externalCtRatio', 'gridSideINVMeter2'):
            self.assertNotIn(key, CONTROLS)
        with self.assertRaises(ValueError):
            validate('gridPeakPower', '8000')

    def test_aux_mode_requires_whole_number(self):
        with self.assertRaises(ValueError):
            validate('loadMode', '1.5')

    def test_supervisor_mqtt_uses_internal_token(self):
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {'data': {'host': 'core-mosquitto', 'port': '1883', 'username': 'addon', 'password': 'private', 'ssl': False}}
        class HTTP:
            def get(self, url, headers, timeout):
                self.url, self.headers, self.timeout = url, headers, timeout
                return Response()
        http = HTTP()
        found = mqtt_service('secret-token', http)
        self.assertEqual(found['host'], 'core-mosquitto')
        self.assertEqual(found['password'], 'private')
        self.assertEqual(http.url, 'http://supervisor/services/mqtt')
        self.assertEqual(http.headers['Authorization'], 'Bearer secret-token')
        self.assertIsNone(mqtt_service('', http))
    def test_supervisor_mqtt_failure_is_not_called_missing_broker(self):
        service, reason = mqtt_service_status('', object())
        self.assertIsNone(service)
        self.assertIn('token', reason)
        class Response:
            status_code = 404
        class HTTPError(Exception):
            def __init__(self):
                self.response = Response()
        class HTTP:
            def get(self, url, headers, timeout):
                raise HTTPError()
        service, reason = mqtt_service_status('token', HTTP())
        self.assertIsNone(service)
        self.assertIn('no MQTT service registered', reason)

    def test_supervisor_mqtt_reports_service_error_without_losing_manual_config(self):
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {'result': 'error', 'message': 'Service not available'}
        class HTTP:
            def get(self, *args, **kwargs):
                return Response()
        service, reason = mqtt_service_status('token', HTTP())
        self.assertIsNone(service)
        self.assertIn('no MQTT service', reason)
        self.assertEqual(self.store.data['mqtt']['mode'], 'auto')

    def test_manual_mqtt_republish_covers_telemetry_and_settings(self):
        sent = []
        self.store.data['mqtt']['prefix'] = 'solarsynkv4'
        broker = Broker(self.store.data['mqtt'], self.store, lambda *args: None)
        broker.connected = True
        broker.publish = lambda topic, value, retain=False: sent.append((topic, value, retain))
        metrics = {'battery_soc': {'name': 'Battery charge', 'value': 68, 'unit': '%', 'kind': 'battery'}}
        broker.telemetry('A1', {'alias': 'Main'}, metrics, '2026-10-13T10:00:00Z', True)
        broker.settings('A1', {'batteryShutdownCap': 20})
        broker.control_state('A1', 'batteryShutdownCap', 20, None)
        sent.clear()
        broker.republish(force=True)
        self.assertTrue(any(topic.endswith('/battery_soc/config') and retained for topic, _, retained in sent))
        self.assertTrue(any(topic.endswith('/batteryShutdownCap/config') and retained for topic, _, retained in sent))
        broker.telemetry('A1', {'alias': 'Main'}, metrics, '2026-10-13T10:00:00Z', True)
        broker.control_state('A1', 'batteryShutdownCap', 20, None)
        self.assertTrue(any(topic.endswith('/battery_soc/state') for topic, _, _ in sent))
        self.assertTrue(any(topic.endswith('/batteryShutdownCap/state') for topic, _, _ in sent))

    def test_new_settings_discover_and_accept_mqtt_commands(self):
        sent = []
        broker = Broker(self.store.data['mqtt'], self.store, self.manager.request)
        broker.connected = True
        broker.client = object()
        broker.publish = lambda topic, value, retain=False: sent.append((topic, value, retain))
        broker.settings('A1', {'loadMode': '0', 'genConnectGrid': '0', 'solarMaxSellPower': '2560', 'sellTime1': '03:00'})
        configs = {topic: json.loads(payload) for topic, payload, _ in sent if topic.endswith("/config")}
        self.assertEqual(configs['homeassistant/number/solarsynkv4_A1/loadMode/config']['command_topic'], 'solarsynkv4/A1/set/loadMode')
        self.assertEqual(configs['homeassistant/number/solarsynkv4_A1/loadMode/config']['step'], 1)
        self.assertIn('homeassistant/switch/solarsynkv4_A1/genConnectGrid/config', configs)
        self.assertIn('homeassistant/number/solarsynkv4_A1/solarMaxSellPower/config', configs)
        self.assertIn('homeassistant/text/solarsynkv4_A1/sellTime1/config', configs)
        broker._message(broker.client, None, types.SimpleNamespace(topic='solarsynkv4/A1/set/loadMode', payload=b'2', retain=False))
        self.wait()
        self.assertEqual(self.cloud.writes[0]['loadMode'], 2)

    def test_cached_cloud_data_discovers_and_replays_immediately_after_mqtt_connect(self):
        published = []
        events = []
        class Client:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append((topic, payload, qos, retain))
            def subscribe(self, topic):
                pass
        client = Client()
        broker = Broker(self.store.data['mqtt'], self.store, lambda *args: None, lambda *args, **kwargs: events.append((args, kwargs)))
        broker.client = client
        stamp = datetime.now(timezone.utc).isoformat()
        broker.telemetry('A1', {'alias': 'Main', 'model': 'Hybrid'}, {'input_pac': {'name': 'PV power', 'value': 1400, 'unit': 'W', 'kind': 'power'}}, stamp, True)
        broker.settings('A1', {'batteryShutdownCap': 20, 'batteryRestartCap': 30})
        self.assertFalse(published)
        broker._connect(client, None, None, types.SimpleNamespace(is_failure=False), None)
        discovery = [(topic, json.loads(payload), qos, retain) for topic, payload, qos, retain in published if topic.endswith('/input_pac/config')]
        self.assertEqual(len(discovery), 1)
        topic, config, qos, retain = discovery[0]
        self.assertEqual(topic, 'homeassistant/sensor/solarsynkv4_A1/input_pac/config')
        self.assertEqual(config['unique_id'], 'solarsynkv4_A1_input_pac')
        self.assertEqual(config['device']['identifiers'], ['solarsynkv4_A1'])
        self.assertEqual(config['device']['name'], 'SolarsynkV4')
        self.assertEqual(config['state_topic'], 'solarsynkv4/A1/input_pac/state')
        self.assertTrue(retain)
        self.assertEqual(qos, 1)
        self.assertIn(('solarsynkv4/A1/input_pac/state', '1400', 1, False), published)
        self.assertIn(('solarsynkv4/A1/availability', 'online', 1, True), published)
        self.assertTrue(any(t.endswith('/batteryShutdownCap/config') for t, _, _, _ in published))
        self.assertTrue(any('discovery' in args[1] for args, _ in events))

    def test_selected_inverter_is_discovered_before_first_cloud_sample(self):
        published = []
        class Client:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append((topic, payload, retain))
            def subscribe(self, topic):
                pass
        broker = Broker(self.store.data['mqtt'], self.store, lambda *args: None)
        broker.client = Client()
        broker._connect(broker.client, None, None, types.SimpleNamespace(is_failure=False), None)
        configs = [json.loads(payload) for topic, payload, retained in published if topic.endswith('/telemetry_status/config') and retained]
        self.assertEqual(configs[0]['device']['identifiers'], ['solarsynkv4_A1'])
        self.assertEqual(configs[0]['availability'], [{'topic': 'solarsynkv4/availability'}])
        self.assertIn(('solarsynkv4/A1/telemetry_status/state', 'Waiting for cloud data', False), published)

    def test_connected_broker_sends_new_cloud_sample_immediately(self):
        published = []
        class Client:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append((topic, payload, qos, retain))
            def subscribe(self, topic):
                pass
        broker = Broker(self.store.data['mqtt'], self.store, lambda *args: None)
        broker.client = Client()
        broker._connect(broker.client, None, None, types.SimpleNamespace(is_failure=False), None)
        published.clear()
        stamp = datetime.now(timezone.utc).isoformat()
        broker.telemetry('A1', {'alias': 'Main'}, {'input_pac': {'name': 'PV power', 'value': 800, 'unit': 'W', 'kind': 'power'}}, stamp, True)
        self.assertTrue(any(topic.endswith('/input_pac/config') and retained for topic, _, _, retained in published))
        self.assertIn(('solarsynkv4/A1/input_pac/state', '800', 1, False), published)
        self.assertIn(('solarsynkv4/A1/availability', 'online', 1, True), published)

    def test_stale_cached_telemetry_is_not_replayed_as_fresh(self):
        published = []
        class Client:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append((topic, payload))
            def subscribe(self, topic):
                pass
        broker = Broker(self.store.data['mqtt'], self.store, lambda *args: None)
        broker.client = Client()
        broker.telemetry('A1', {'alias': 'Main'}, {'input_pac': {'name': 'PV power', 'value': 1400, 'unit': 'W', 'kind': 'power'}}, '2020-01-01T00:00:00Z', True)
        broker._connect(broker.client, None, None, types.SimpleNamespace(is_failure=False), None)
        self.assertIn(('solarsynkv4/A1/availability', 'offline'), published)
        self.assertIn(('solarsynkv4/A1/telemetry_status/state', 'Stale'), published)

    def test_activity_events_survive_restart(self):
        self.store.record_activity('mqtt', 'Queued 3 discovery configurations.')
        restored = Store(self.temp.name)
        self.assertEqual(restored.data['activity'][-1]['category'], 'mqtt')
        self.assertIn('Queued 3', restored.data['activity'][-1]['message'])

    def test_mqtt_startup_failure_does_not_stop_app(self):
        import app.mqtt as mqtt_module
        class Client:
            def __init__(self, *args, **kwargs):
                pass
            def will_set(self, *args, **kwargs):
                pass
            def reconnect_delay_set(self, *args, **kwargs):
                pass
            def connect_async(self, *args, **kwargs):
                raise OSError('DNS failed')
        old_client = getattr(mqtt_module.mqtt, 'Client', None)
        old_version = getattr(mqtt_module.mqtt, 'CallbackAPIVersion', None)
        old_protocol = getattr(mqtt_module.mqtt, 'MQTTv5', None)
        events = []
        try:
            mqtt_module.mqtt.Client = Client
            mqtt_module.mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
            mqtt_module.mqtt.MQTTv5 = 5
            broker = Broker({**self.store.data['mqtt'], 'host': 'bad-host'}, self.store, lambda *args: None,
                            lambda *args, **kwargs: events.append((args, kwargs)))
            broker.start()
            self.assertIsNone(broker.client)
            self.assertFalse(broker.connected)
            self.assertIn('Could not start', events[-1][0][1])
        finally:
            for name, value in [('Client', old_client), ('CallbackAPIVersion', old_version), ('MQTTv5', old_protocol)]:
                if value is None:
                    delattr(mqtt_module.mqtt, name)
                else:
                    setattr(mqtt_module.mqtt, name, value)

    def test_mqtt_connection_test_checks_handshake_without_publishing(self):
        import app.mqtt as mqtt_module
        class Reason:
            is_failure = False
        class Client:
            def __init__(self, *args, **kwargs):
                self.published = False
            def username_pw_set(self, *args):
                pass
            def connect_async(self, host, port, keepalive):
                self.host, self.port = host, port
            def loop_start(self):
                self.on_connect(self, None, None, Reason(), None)
            def disconnect(self):
                pass
            def loop_stop(self):
                pass
            def publish(self, *args):
                self.published = True
        old_client = getattr(mqtt_module.mqtt, 'Client', None)
        old_version = getattr(mqtt_module.mqtt, 'CallbackAPIVersion', None)
        old_protocol = getattr(mqtt_module.mqtt, 'MQTTv5', None)
        try:
            mqtt_module.mqtt.Client = Client
            mqtt_module.mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
            mqtt_module.mqtt.MQTTv5 = 5
            ok, message = mqtt_module.test_connection({'host': 'broker', 'port': 1883}, timeout=.1)
            self.assertTrue(ok)
            self.assertIn('succeeded', message)
        finally:
            for name, value in [('Client', old_client), ('CallbackAPIVersion', old_version), ('MQTTv5', old_protocol)]:
                if value is None:
                    delattr(mqtt_module.mqtt, name)
                else:
                    setattr(mqtt_module.mqtt, name, value)

    def test_discovery_refresh_keeps_cached_and_selected_inverters(self):
        self.store.data['selected'] = ['A1']
        self.store.data['devices'] = [{'sn': 'A1', 'alias': 'Original', 'plant': 'Home'}]
        merged, added = merge_devices(self.store.data['devices'], [{'sn': 'B2', 'alias': 'New', 'plant': 'Home'}])
        self.store.data['devices'] = merged
        self.store.save()
        restored = Store(self.temp.name)
        self.assertEqual(restored.data['selected'], ['A1'])
        self.assertEqual({item['sn'] for item in restored.data['devices']}, {'A1', 'B2'})
        self.assertEqual(added, 1)
        again, added = merge_devices(restored.data['devices'], [])
        self.assertEqual(len(again), 2)
        self.assertEqual(added, 0)

    def test_old_selected_inverter_is_shown_after_upgrade(self):
        self.store.data['selected'] = ['A1']
        self.store.data.pop('devices', None)
        self.store.save()
        restored = Store(self.temp.name)
        self.assertEqual(restored.data['devices'][0]['sn'], 'A1')

    def test_dashboard_rows_show_actual_discoverable_fields(self):
        metrics = {'battery_soc': {'name': 'Battery charge', 'value': 68, 'unit': '%', 'kind': 'battery'}}
        settings = {'batteryShutdownCap': 20, 'unknownCloudField': 42, 'apiToken': 'private'}
        commands = {('A1', 'batteryShutdownCap'): {'value': 40, 'status': 'pending'}}
        rows = entity_rows('A1', metrics, settings, commands)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['system'], 'solarsynkv4_A1_battery_soc')
        self.assertEqual(rows[1]['value'], 40)
        self.assertEqual(rows[1]['status'], 'Pending')
        self.assertIn('unknownCloudField', str(rows))
        self.assertNotIn('apiToken', str(rows))
        self.assertEqual(uptime_label(3660), '1h 1m')

    def test_old_config_defaults_to_automatic_mqtt(self):
        self.store.data['mqtt'].pop('mode')
        self.store.save()
        self.assertEqual(Store(self.temp.name).data['mqtt']['mode'], 'auto')
    def test_store_secrets_not_in_public_copy(self):
        self.store.data['cloud']['password']='secret'
        self.store.save()
        self.assertEqual(Store(self.temp.name).data['cloud']['password'], 'secret')
        self.assertEqual(self.store.public()['cloud']['password'], '')
    def test_invalid_configuration_shape_is_preserved(self):
        config = pathlib.Path(self.temp.name) / 'config.json'
        config.write_text('{"version": 1, "cloud": null}')
        restored = Store(self.temp.name)
        self.assertEqual(restored.data['cloud']['server'], 'api.sunsynk.net')
        self.assertTrue(list(pathlib.Path(self.temp.name).glob('config.corrupt.*.json')))

    def test_partial_saved_configuration_keeps_defaults(self):
        config = pathlib.Path(self.temp.name) / 'config.json'
        config.write_text('{"version": 1, "mqtt": {"host": "broker"}, "poll_seconds": 60}')
        restored = Store(self.temp.name)
        self.assertEqual(restored.data['mqtt']['prefix'], 'solarsynkv4')
        self.assertEqual(restored.data['mqtt']['host'], 'broker')
        self.assertEqual(restored.data['poll_seconds'], 60)

    def test_corrupt_configuration_is_preserved(self):
        path = pathlib.Path(self.temp.name) / 'config.json'
        path.write_text('{broken')
        replacement = Store(self.temp.name)
        self.assertTrue(hasattr(replacement, 'error'))
        self.assertTrue(list(pathlib.Path(self.temp.name).glob('config.corrupt.*.json')))
    def test_missing_telemetry_is_not_zero(self):
        self.assertEqual(normalise('battery', {}), {})
        self.assertEqual(normalise('input', {'pac': '1234'})['input_pac']['unit'], 'W')
        self.assertEqual(normalise('grid', {'vip': [{'volt': 230, 'power': 100}]})['grid_phase_1_volt']['value'], 230)
        self.assertEqual(normalise('input', {'pvIV': [{'ppv': 700, 'vpv': 300}]})['pvIV_1_vpv']['unit'], 'V')
    def test_all_available_mppts_and_phases_are_normalised(self):
        mppts = [{'ppv': 100 * i, 'vpv': 200 + i, 'ipv': i} for i in range(1, 5)]
        metrics = normalise('input', {'pvIV': mppts})
        self.assertEqual(metrics['pvIV_4_ppv']['value'], 400)
        self.assertEqual(metrics['pvIV_4_vpv']['value'], 204)
        self.assertEqual(metrics['pvIV_4_ipv']['value'], 4)
        self.assertEqual(len([key for key in metrics if key.startswith('pvIV_')]), 12)
        self.assertEqual(len(normalise('input', {'pvIV': mppts[:2]})), 6)
        phases = normalise('grid', {'vip': [{'volt': 230 + i} for i in range(4)]})
        self.assertEqual(phases['grid_phase_4_volt']['value'], 233)
        self.assertEqual(normalise('grid', {'limiterPowerArr': [10, None, 30]})['grid_limiter_power_3']['value'], 30)

    def test_timestamp_age_and_restart_does_not_replay(self):
        self.assertIsNone(timestamp_age(None))
        self.assertLess(timestamp_age(time.time()), 2)
        self.manager.request('A1','batteryShutdownCap',40)
        self.wait()
        writes = len(self.cloud.writes)
        replacement = SettingsManager(self.cloud, self.store)
        self.assertEqual(replacement.commands, {})
        self.assertEqual(len(self.cloud.writes), writes)
    def test_external_change_without_pending(self):
        self.manager.observe('A1', {'batteryShutdownCap': 35})
        self.assertEqual(self.events[-1][2], 35)
    def test_discovery_hides_cloud_binding_stack(self):
        class Broken:
            def request(self, method, path):
                if '/plants?' in path:
                    return {'infos': [{'id': 1, 'name': 'Home'}]}
                raise fake_cloud_module.CloudError('org.springframework.validation.BindException invSearchDTO')
        devices, errors = discover(Broken())
        self.assertEqual(devices, [])
        self.assertIn('cloud rejected', errors[0])
        self.assertNotIn('org.springframework', errors[0])
    def test_discovery_reads_second_inverter_page(self):
        class Pages:
            def request(self, method, path):
                if '/plants?' in path:
                    return {'infos': [{'id': 1, 'name': 'Home'}]}
                if 'page=1' in path:
                    return {'infos': [{'sn': str(i)} for i in range(10)], 'total': 0}
                return {'infos': [{'sn': '10'}], 'total': 0}
        devices, errors = discover(Pages())
        self.assertEqual(len(devices), 11)
        self.assertEqual(errors, [])
    def test_paginated_discovery(self):
        class Pages:
            def assert_inverter_query(self, path):
                assert 'status=-1' in path and 'type=-2' in path, path
            def request(self, method, path):
                if '/plants?' in path:
                    return {'infos':[{'id':1,'name':'Home'}]}
                self.assert_inverter_query(path)
                return {'infos':[{'sn':'A1','alias':'Main'}]}
        devices, errors = discover(Pages())
        self.assertEqual(devices[0]['sn'],'A1')
        self.assertEqual(errors,[])

if __name__ == '__main__':
    unittest.main()
