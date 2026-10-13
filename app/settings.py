import re
import threading
import time
import uuid
from .cloud import CloudError

BATTERY = '''sn safetyType battMode absorptionVolt batteryCap batteryEfficiency batteryEmptyV batteryImpedance batteryLowCap batteryLowVolt batteryMaxCurrentCharge batteryMaxCurrentDischarge batteryOn batteryRestartCap batteryRestartVolt batteryShutdownCap batteryShutdownVolt bmsErrStop disableFloatCharge equChargeCycle equChargeTime equVoltCharge floatVolt genChargeOn genSignal generatorBatteryCurrent generatorForcedStart generatorStartCap generatorStartVolt gridSignal lithiumMode lowNoiseMode lowPowerMode sdBatteryCurrent sdChargeOn sdStartCap sdStartVolt signalIslandModeEnable tempco'''.split()
MODE = '''sn safetyType battMode solarSell pvMaxLimit energyMode peakAndVallery sysWorkMode sellTime1 sellTime2 sellTime3 sellTime4 sellTime5 sellTime6 sellTime1Pac sellTime2Pac sellTime3Pac sellTime4Pac sellTime5Pac sellTime6Pac cap1 cap2 cap3 cap4 cap5 cap6 sellTime1Volt sellTime2Volt sellTime3Volt sellTime4Volt sellTime5Volt sellTime6Volt zeroExportPower solarMaxSellPower mondayOn tuesdayOn wednesdayOn thursdayOn fridayOn saturdayOn sundayOn time1on time2on time3on time4on time5on time6on genTime1on genTime2on genTime3on genTime4on genTime5on genTime6on'''.split()
AUX = '''sn safetyType battMode genMinSolar loadMode genOffCap genOnCap genOffVolt genOnVolt gridAlwaysOn offGridImmediatelyOff acCoupleFreqUpper genConnectGrid genPeakShaving genPeakPower acCoupleOnLoadSideEnable acCoupleOnGridSideEnable'''.split()
ADVANCED = '''sn genPeakShaving gridPeakShaving gridPeakPower parallel modbusSn sensorsCheck remoteLock gridAlwaysOn micExportGridOff bmsErrStop exMeterCtSwitch meterA meterB meterC atsEnable atsSwitch meterSelect gridSideINVMeter2 smartCTEnable equipMode solarMaxSellPower drmEnable backupDelay solar1WindInputEnable solar2WindInputEn solar2WindInputEnable solar3WindInputEn phase signalIslandModeEnable isletProtect mpptMulti pv1SelfCheck pv2SelfCheck pv3SelfCheck acOutputPowerLimit pwm genToLoad genConnectGrid genPeakPower arcFactFrz externalCtRatio'''.split()
for prefix in ('volt', 'current'):
    ADVANCED.extend(f'{prefix}{i}' for i in range(1, 13))
ADVANCED_UNDEFINED = '''codeMode earthBond exMeterCT nightPowerSave risoEn smartMeter2Enable solar3WindInputEnable tenOverVoltEn tenOverVoltVal'''.split()
GROUPS = {'battery': BATTERY, 'mode': MODE, 'aux': AUX, 'advanced': ADVANCED}
MODE_EXTRAS = '''sellTime1En sellTime2En sellTime3En sellTime4En sellTime5En sellTime6En gridPeakShaving generatorStartCap batteryLowCap'''.split()
MODE_DAYS = {f'{day}On' for day in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')}
# Only fields whose type and conservative range are documented in source integrations are exposed.
CONTROLS = {
 'batteryMaxCurrentCharge': ('Battery charge current', 'number', 'battery', 0, 200, 'A'),
 'batteryMaxCurrentDischarge': ('Battery discharge current', 'number', 'battery', 0, 200, 'A'),
 'batteryShutdownCap': ('Battery shutdown charge', 'number', 'battery', 0, 100, '%'),
 'batteryRestartCap': ('Battery restart charge', 'number', 'battery', 0, 100, '%'),
 'batteryLowCap': ('Battery low charge', 'number', 'battery', 0, 100, '%'),
 'sdChargeOn': ('Grid charging', 'switch', 'battery', None, None, None),
 'solarSell': ('Solar export', 'switch', 'mode', None, None, None),
 'zeroExportPower': ('Zero export power', 'number', 'mode', 0, 500, 'W'),
 'genMinSolar': ('Generator minimum solar power', 'number', 'aux', 0, 30000, 'W'),
 'loadMode': ('Aux port mode (0 Gen, 1 Aux, 2 Micro inverter)', 'integer', 'aux', 0, 2, None),
 'genOffCap': ('Aux off battery charge', 'number', 'aux', 0, 100, '%'),
 'genOnCap': ('Aux on battery charge', 'number', 'aux', 0, 100, '%'),
 'genOffVolt': ('Aux off battery voltage', 'number', 'aux', 40, 65, 'V'),
 'genOnVolt': ('Aux on battery voltage', 'number', 'aux', 40, 65, 'V'),
 'gridAlwaysOn': ('Grid always on', 'switch', 'aux', None, None, None),
 'offGridImmediatelyOff': ('Off grid immediately off', 'switch', 'aux', None, None, None),
 'acCoupleFreqUpper': ('AC couple upper frequency', 'number', 'aux', 45, 65, 'Hz'),
 'genConnectGrid': ('Generator connected to grid input', 'switch', 'aux', None, None, None),
 'genPeakShaving': ('Generator peak shaving', 'switch', 'aux', None, None, None),
 'genPeakPower': ('Generator peak shaving power', 'number', 'aux', 500, 30000, 'W'),
 'acCoupleOnLoadSideEnable': ('AC couple on load side', 'switch', 'aux', None, None, None),
 'acCoupleOnGridSideEnable': ('AC couple on grid side', 'switch', 'aux', None, None, None),
 'solarMaxSellPower': ('Maximum solar power', 'number', 'advanced', 1000, 12000, 'W'),
}
CONTROLS.update({
 'battMode': ('Battery type (-1 Lithium, 0 AGM V, 1 AGM %, 2 No battery)', 'integer', 'battery', -1, 2, None),
 'batteryCap': ('Battery capacity', 'integer', 'battery', 0, 9999, 'Ah'),
 'batteryEfficiency': ('Battery efficiency', 'integer', 'battery', 0, 100, '%'),
 'batteryEmptyV': ('Battery empty voltage', 'number', 'battery', 41, 63, 'V'),
 'batteryLowVolt': ('Battery low voltage', 'number', 'battery', 40, 65, 'V'),
 'batteryRestartVolt': ('Battery restart voltage', 'number', 'battery', 40, 65, 'V'),
 'batteryShutdownVolt': ('Battery shutdown voltage', 'number', 'battery', 40, 65, 'V'),
 'batteryOn': ('Battery enabled', 'switch', 'battery', None, None, None),
 'bmsErrStop': ('Stop on BMS error', 'switch', 'battery', None, None, None),
 'lithiumMode': ('Lithium protocol', 'integer', 'battery', 0, 20, None),
 'sdStartCap': ('Grid charge start', 'integer', 'battery', 0, 100, '%'),
 'sdBatteryCurrent': ('Grid charge current', 'integer', 'battery', 0, 275, 'A'),
 'genChargeOn': ('Generator charging', 'switch', 'battery', None, None, None),
 'gridSignal': ('Grid signal', 'switch', 'battery', None, None, None),
 'genSignal': ('Generator signal', 'switch', 'battery', None, None, None),
 'generatorForcedStart': ('Force generator start', 'switch', 'battery', None, None, None),
 'generatorStartCap': ('Generator start charge', 'integer', 'battery', 0, 100, '%'),
 'generatorStartVolt': ('Generator start voltage', 'number', 'battery', 40, 65, 'V'),
 'generatorBatteryCurrent': ('Generator charge current', 'integer', 'battery', 0, 275, 'A'),
 'signalIslandModeEnable': ('Signal island mode', 'switch', 'battery', None, None, None),
 'lowPowerMode': ('Low power mode', 'switch', 'battery', None, None, None),
 'disableFloatCharge': ('Disable float charge', 'switch', 'battery', None, None, None),
 'lowNoiseMode': ('Low noise mode', 'switch', 'battery', None, None, None),
 'absorptionVolt': ('Absorption voltage', 'number', 'battery', 40, 65, 'V'),
 'floatVolt': ('Float voltage', 'number', 'battery', 40, 65, 'V'),
 'equVoltCharge': ('Equalization voltage', 'number', 'battery', 40, 65, 'V'),
 'equChargeCycle': ('Equalization cycle', 'integer', 'battery', 0, 365, 'days'),
 'equChargeTime': ('Equalization time', 'integer', 'battery', 0, 24, 'hours'),
 'pvMaxLimit': ('Maximum sell power', 'integer', 'mode', 0, 9600, 'W'),
 'energyMode': ('Energy priority (0 Battery, 1 Load)', 'integer', 'mode', 0, 1, None),
 'peakAndVallery': ('Use timer', 'switch', 'mode', None, None, None),
 'sysWorkMode': ('Work mode (0 Sell first, 1 Load only, 2 Home)', 'integer', 'mode', 0, 2, None),
})
for i in range(1, 7):
    CONTROLS[f'cap{i}'] = (f'Slot {i} charge target', 'number', 'mode', 0, 100, '%')
    CONTROLS[f'sellTime{i}'] = (f'Slot {i} start', 'time', 'mode', None, None, None)
    CONTROLS[f'sellTime{i}Pac'] = (f'Slot {i} power', 'integer', 'mode', 0, 9600, 'W')
    CONTROLS[f'sellTime{i}Volt'] = (f'Slot {i} voltage', 'number', 'mode', 40, 65, 'V')
    CONTROLS[f'sellTime{i}En'] = (f'Slot {i} sell enabled', 'switch', 'mode', None, None, None)
    CONTROLS[f'time{i}on'] = (f'Slot {i} grid charge', 'switch', 'mode', None, None, None)
    CONTROLS[f'genTime{i}on'] = (f'Slot {i} generator charge', 'switch', 'mode', None, None, None)

for day in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'):
    CONTROLS[f'{day}On'] = (f'{day.title()} timer', 'switch', 'mode', None, None, None)

_SECRET_FIELD = re.compile(r'password|passwd|secret|token|private|credential|auth', re.I)
_SAFE_FIELD = re.compile(r'[A-Za-z][A-Za-z0-9_]*\Z')


def readable_settings(data):
    """Return scalar cloud settings safe to display as read-only diagnostics."""
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        if key == 'sn' or not _SAFE_FIELD.fullmatch(key) or _SECRET_FIELD.search(key):
            continue
        if value is None or not isinstance(value, (str, int, float, bool)):
            continue
        rendered = str(value) if not isinstance(value, bool) else ('On' if value else 'Off')
        if len(rendered) > 255:
            continue
        result[key] = rendered
    return result

def validate(key, raw):
    if key not in CONTROLS:
        raise ValueError('This setting is not available.')
    _, kind, _, low, high, _ = CONTROLS[key]
    if kind in ('number', 'integer'):
        value = float(raw)
        if kind == 'integer' and not value.is_integer():
            raise ValueError('Enter a whole number.')
        if not low <= value <= high:
            raise ValueError(f'Enter a value from {low} to {high}.')
        return int(value) if value.is_integer() else value
    if kind == 'switch':
        if str(raw).lower() not in ('on', 'off', 'true', 'false', '1', '0'):
            raise ValueError('Choose On or Off.')
        return str(raw).lower() in ('on', 'true', '1')
    if kind == 'time':
        if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?', str(raw)):
            raise ValueError('Enter a time such as 06:30.')
        return str(raw)[:5]
    raise ValueError('Unsupported setting type.')

def same(a, b):
    if isinstance(a, bool):
        return str(b).lower() in ('true', '1', 'on') if a else str(b).lower() in ('false', '0', 'off')
    try:
        return abs(float(a) - float(b)) <= 0.01
    except (TypeError, ValueError):
        return str(a) == str(b)

class SettingsManager:
    def __init__(self, cloud, store, on_change=lambda *args: None, coalesce_seconds=0.6, confirm_interval=5, confirm_window=180, on_event=None):
        self.cloud, self.store, self.on_change = cloud, store, on_change
        self.on_event = on_event or (lambda *args, **kwargs: None)
        self.coalesce_seconds, self.confirm_interval, self.confirm_window = coalesce_seconds, confirm_interval, confirm_window
        self.lock = threading.RLock()
        self.commands = {}
        self.observed = {}
        self.active = set()
        self.serial_locks = {}
        self.wake = {}
    def _event(self, message, serial, level='info'):
        try:
            self.on_event('settings', message, serial=serial, level=level)
        except Exception:
            pass

    def observe(self, serial, data):
        if not isinstance(data, dict) or ('sn' in data and str(data['sn']) != serial):
            return
        with self.lock:
            for key in CONTROLS:
                if key in data:
                    self.observed[(serial, key)] = data[key]
                    if (serial, key) not in self.commands:
                        self.on_change(serial, key, data[key], None)
    def request(self, serial, key, raw, immediate=False):
        value = validate(key, raw)
        if serial not in self.store.data['selected']:
            raise ValueError('Select this inverter first.')
        with self.lock:
            command = {'id': uuid.uuid4().hex, 'value': value, 'status': 'pending', 'started': time.time(), 'observed': self.observed.get((serial, key))}
            self.commands[(serial, key)] = command
            self.on_change(serial, key, value, command)
            group = CONTROLS[key][2]
            self.serial_locks.setdefault(serial, threading.Lock())
            token = (serial, group)
            wake = self.wake.setdefault(token, threading.Event())
            if immediate:
                wake.set()
            if token not in self.active:
                self.active.add(token)
                threading.Thread(target=self._worker, args=token, daemon=True).start()
            return command
    def _finish(self, serial, key, ident, status, error=None):
        with self.lock:
            current = self.commands.get((serial, key))
            if not current or current['id'] != ident:
                return
            current['status'] = status
            current['error'] = error
            current['finished'] = time.time()
            value = current['value'] if status == 'confirmed' else self.observed.get((serial, key))
            self.on_change(serial, key, value, current)
            self.store.data['history'] = (self.store.data['history'] + [{'serial': serial, 'setting': key, 'status': status, 'at': current['finished'], 'error': error}])[-100:]
            self.store.save()
            del self.commands[(serial, key)]
    def _worker(self, serial, group):
        try:
            while True:
                self.wake[(serial, group)].wait(self.coalesce_seconds)
                with self.lock:
                    self.wake[(serial, group)].clear()
                    batch = {key: dict(cmd) for (sn, key), cmd in self.commands.items() if sn == serial and CONTROLS[key][2] == group and cmd['status'] == 'pending'}
                if not batch:
                    return
                try:
                    with self.serial_locks[serial]:
                        info = self.cloud.inverter(serial)
                        if not isinstance(info, dict) or (info.get('sn') is not None and str(info['sn']) != serial):
                            raise CloudError('Inverter identity cannot be verified.')
                        current = self.cloud.settings(serial)
                        if not isinstance(current, dict) or str(current.get('sn')) != serial:
                            raise CloudError('The cloud returned settings for a different inverter.')
                        self.observe(serial, current)
                        if isinstance(current, dict) and str(current.get('parallel', '0')) not in ('0', 'False', 'false', 'None'):
                            raise CloudError('Parallel system detected. Setting changes are disabled until master/slave routing is verified.')
                        required = GROUPS[group]
                        if not isinstance(current, dict) or any(k not in current or current[k] is None for k in required):
                            raise CloudError('A complete settings group is unavailable. Nothing was sent.')
                        if str(current['sn']) != serial:
                            raise CloudError('The cloud returned settings for a different inverter.')
                        payload = {key: current[key] for key in required}
                        if group == 'battery':
                            if 'flag2' in current:
                                payload['flag2'] = current['flag2']
                            payload['sdStartVol'] = current.get('sdStartVol', 'undefined')
                        elif group == 'mode':
                            payload.update({key: current[key] for key in MODE_EXTRAS if key in current})
                            payload['lowVoltCrossEn'] = current.get('lowVoltCrossEn', 'undefined')
                            for day in MODE_DAYS:
                                payload[day] = str(current[day]).lower() in ('true', '1', 'on')
                        elif group == 'advanced':
                            payload.update({key: current.get(key, 'undefined') for key in ADVANCED_UNDEFINED})
                        # A later page write must carry earlier requested values while
                        # the cloud still reports its old settings.
                        with self.lock:
                            desired = {key: dict(cmd) for (sn, key), cmd in self.commands.items()
                                       if sn == serial and key in payload and cmd['status'] in ('pending', 'sent')}
                        for key, cmd in desired.items():
                            value = cmd['value']
                            if key in MODE_DAYS:
                                value = bool(value)
                            elif CONTROLS[key][1] == 'switch' and isinstance(current[key], str):
                                value = ('1' if value else '0') if current[key] in ('0', '1') else ('true' if value else 'false')
                            payload[key] = value
                        self.cloud.write_settings(serial, payload)
                    with self.lock:
                        for key, cmd in batch.items():
                            latest = self.commands.get((serial, key))
                            if latest and latest['id'] == cmd['id']:
                                latest['status'] = 'sent'
                                self.on_change(serial, key, latest['value'], latest)
                    message = 'Sunsynk API write succeeded for ' + ', '.join(sorted(batch)) + '; awaiting inverter read-back.'
                    self._event(message, serial)
                    threading.Thread(target=self._confirm, args=(serial, batch), daemon=True).start()
                except Exception as exc:
                    self._event('Setting change failed for ' + ', '.join(sorted(batch)) + ': ' + str(exc), serial, level='error')
                    for key, cmd in batch.items():
                        self._finish(serial, key, cmd['id'], 'rejected', str(exc))
        finally:
            with self.lock:
                self.active.discard((serial, group))
                if any(sn == serial and CONTROLS[key][2] == group and cmd['status'] == 'pending' for (sn, key), cmd in self.commands.items()):
                    self.active.add((serial, group))
                    threading.Thread(target=self._worker, args=(serial, group), daemon=True).start()

    def _confirm(self, serial, batch):
        deadline = time.monotonic() + self.confirm_window
        confirmed = set()
        while time.monotonic() < deadline:
            with self.lock:
                live = {key for key, cmd in batch.items()
                        if (latest := self.commands.get((serial, key))) and latest['id'] == cmd['id']}
            if not live or live <= confirmed:
                break
            time.sleep(self.confirm_interval)
            try:
                observed = self.cloud.settings(serial)
            except Exception:
                continue
            if not isinstance(observed, dict) or str(observed.get('sn')) != serial:
                continue
            self.observe(serial, observed)
            for key, cmd in batch.items():
                with self.lock:
                    latest = self.commands.get((serial, key))
                if latest and latest['id'] == cmd['id'] and same(cmd['value'], observed.get(key)):
                    confirmed.add(key)
            if live <= confirmed:
                break
        for key, cmd in batch.items():
            self._finish(serial, key, cmd['id'], 'confirmed' if key in confirmed else 'unconfirmed', None if key in confirmed else 'The cloud did not show this value within three minutes.')
