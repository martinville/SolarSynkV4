from urllib.parse import quote
from datetime import date, datetime, timezone
import re
import time

FIELDS = {
    'info': {
        'pac': ('Inverter power', 'W', 'power'), 'etoday': ('Inverter energy today', 'kWh', 'energy'),
        'etotal': ('Inverter energy total', 'kWh', 'energy'), 'emonth': ('Inverter energy this month', 'kWh', 'energy'),
        'eyear': ('Inverter energy this year', 'kWh', 'energy'), 'ratePower': ('Rated inverter power', 'W', 'power'),
    },
    'input': {
        'pac': ('PV power', 'W', 'power'), 'etoday': ('PV energy today', 'kWh', 'energy'),
        'etotal': ('PV energy total', 'kWh', 'energy'), 'grid_tip_power': ('PV grid tip power', 'W', 'power'),
    },
    'battery': {
        'soc': ('Battery charge', '%', 'battery'), 'power': ('Battery power', 'W', 'power'),
        'voltage': ('Battery voltage', 'V', 'voltage'), 'current': ('Battery current', 'A', 'current'),
        'temp': ('Battery temperature', '°C', 'temperature'),
    },
    'grid': {
        'pac': ('Grid power', 'W', 'power'), 'qac': ('Grid reactive power', 'var', None),
        'fac': ('Grid frequency', 'Hz', 'frequency'), 'pf': ('Grid power factor', None, None),
        'etotalFrom': ('Grid import energy total', 'kWh', 'energy'), 'etotalTo': ('Grid export energy total', 'kWh', 'energy'),
        'etodayFrom': ('Grid import energy today', 'kWh', 'energy'), 'etodayTo': ('Grid export energy today', 'kWh', 'energy'),
    },
    'load': {
        'totalPower': ('Load power', 'W', 'power'), 'upsPowerTotal': ('UPS load power', 'W', 'power'),
        'totalUsed': ('Load energy total', 'kWh', 'energy'), 'dailyUsed': ('Load energy today', 'kWh', 'energy'),
        'loadFac': ('Load frequency', 'Hz', 'frequency'),
    },
    'output': {
        'pInv': ('Inverter output power', 'W', 'power'), 'pac': ('Inverter AC output power', 'W', 'power'),
        'fac': ('Inverter output frequency', 'Hz', 'frequency'),
    },
    'flow': {
        'pvPower': ('Flow PV power', 'W', 'power'), 'battPower': ('Flow battery power', 'W', 'power'),
        'battPower2': ('Flow battery 2 power', 'W', 'power'), 'gridOrMeterPower': ('Flow grid or meter power', 'W', 'power'),
        'loadOrEpsPower': ('Flow load or EPS power', 'W', 'power'), 'genPower': ('Flow generator power', 'W', 'power'),
        'minPower': ('Flow micro inverter power', 'W', 'power'), 'smartLoadPower': ('Flow smart load power', 'W', 'power'),
        'upsLoadPower': ('Flow UPS load power', 'W', 'power'), 'homeLoadPower': ('Flow home load power', 'W', 'power'),
        'chargePilePower': ('Flow charge pile power', 'W', 'power'), 'soc': ('Flow battery charge', '%', 'battery'),
    },
}


def _words(key):
    return re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', key).replace('_', ' ').strip().capitalize()


def _add(section, keys, unit=None, kind=None):
    for key in keys.split():
        FIELDS[section][key] = (section.title() + ' ' + _words(key), unit, kind)


_add('info', 'sn alias gsn status runStatus type opened brand model commType protocolIdentifier equipType updateAt')
_add('battery', 'time type status bms1Version1 bms1Version2 bms2Version1 bms2Version2 batteryStatus2 batt1Factory batt2Factory')
_add('battery', 'etodayChg etodayDischg emonthChg emonthDischg eyearChg eyearDischg etotalChg etotalDischg', 'kWh', 'energy')
_add('battery', 'capacity correctCap', 'Ah')
_add('battery', 'bmsSoc soc2 batterySoc1 batterySoc2', '%', 'battery')
_add('battery', 'bmsVolt voltage2 batteryVolt1 batteryVolt2 chargeVolt dischargeVolt chargeVolt2 dischargeVolt2', 'V', 'voltage')
_add('battery', 'bmsCurrent current2 batteryCurrent1 batteryCurrent2 chargeCurrentLimit dischargeCurrentLimit maxChargeCurrentLimit maxDischargeCurrentLimit chargeCurrentLimit2 dischargeCurrentLimit2 maxChargeCurrentLimit2 maxDischargeCurrentLimit2', 'A', 'current')
_add('battery', 'bmsTemp temp2 batteryTemp1 batteryTemp2', '°C', 'temperature')
_add('battery', 'batteryPower1 batteryPower2', 'W', 'power')
_add('battery', 'numberOfBatteries')
_add('grid', 'status acRealyStatus')
_add('grid', 'limiterTotalPower', 'W', 'power')
_add('load', 'smartLoadStatus')
_add('load', 'upsPowerL1 upsPowerL2 upsPowerL3', 'W', 'power')
_add('flow', 'pvTo toLoad toSmartLoad toUpsLoad toHomeLoad toGrid toBat batTo gridTo genTo minTo toChargePile time custCode protocolIdentifier meterCode')
_add('flow', 'existsGen existsMin existsGrid genOn microOn existsMeter bmsCommFaultFlag existsThreeLoad existsSmartLoad existsChargePile existThinkPower', None, 'binary')

PATHS = {
    'input': '/api/v1/inverter/{sn}/realtime/input',
    'battery': '/api/v1/inverter/battery/{sn}/realtime?sn={sn}&lan=en',
    'grid': '/api/v1/inverter/grid/{sn}/realtime?sn={sn}',
    'load': '/api/v1/inverter/load/{sn}/realtime?sn={sn}',
    'output': '/api/v1/inverter/{sn}/realtime/output',
    'flow': '/api/v1/inverter/{sn}/flow',
    'temperature': '/api/v1/inverter/{sn}/output/day?lan=en&date={date}&column=dc_temp,igbt_temp',
}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise(section, data):
    if not isinstance(data, dict):
        return {}
    result = {}
    if section == 'temperature':
        infos = data.get('infos')
        if isinstance(infos, list):
            for index, label in enumerate(('DC temperature', 'IGBT temperature')):
                if index >= len(infos) or not isinstance(infos[index], dict):
                    continue
                records = infos[index].get('records')
                if isinstance(records, list):
                    values = [_number(item.get('value')) for item in records if isinstance(item, dict)]
                    values = [value for value in values if value is not None]
                    if values:
                        result['temperature_' + ('dc' if index == 0 else 'igbt')] = {'name': label, 'value': values[-1], 'unit': '°C', 'kind': 'temperature'}
        return result
    for key, (name, unit, kind) in FIELDS[section].items():
        value = data.get(key)
        if value is None:
            continue
        if kind == 'binary':
            if str(value).lower() not in ('true', 'false', '1', '0'):
                continue
            value = 'ON' if str(value).lower() in ('true', '1') else 'OFF'
            result[section + '_' + key] = {'name': name, 'value': value, 'unit': None, 'kind': None, 'entity_type': 'binary_sensor'}
            continue
        numeric = _number(value)
        if numeric is not None:
            value = numeric
        elif isinstance(value, (str, bool)):
            value = str(value).strip()
            if not value or len(value) > 255:
                continue
            kind = None
            unit = None
        else:
            continue
        result[section + '_' + key] = {'name': name, 'value': value, 'unit': unit, 'kind': kind}
    if section == 'input':
        for prefix in ('pvIV', 'mpptIV'):
            devices = data.get(prefix) or []
            if not isinstance(devices, list):
                continue
            for index, item in enumerate(devices, 1):
                if not isinstance(item, dict):
                    continue
                for field, label, unit, kind in (('ppv', 'power', 'W', 'power'), ('vpv', 'voltage', 'V', 'voltage'), ('ipv', 'current', 'A', 'current')):
                    value = _number(item.get(field))
                    if value is not None:
                        result[f'{prefix}_{index}_{field}'] = {'name': f'PV MPPT {index} {label}', 'value': value, 'unit': unit, 'kind': kind}
    if section == 'grid':
        limiter = data.get('limiterPowerArr')
        if isinstance(limiter, list):
            for index, item in enumerate(limiter, 1):
                value = _number(item)
                if value is not None:
                    result[f'grid_limiter_power_{index}'] = {'name': f'Grid limiter power {index}', 'value': value, 'unit': None, 'kind': None}
    if section in ('grid', 'load', 'output'):
        phases = data.get('vip') or []
        if isinstance(phases, list):
            for index, phase in enumerate(phases, 1):
                if not isinstance(phase, dict):
                    continue
                for field, unit, kind in (('volt', 'V', 'voltage'), ('current', 'A', 'current'), ('power', 'W', 'power')):
                    value = _number(phase.get(field))
                    if value is not None:
                        result[f'{section}_phase_{index}_{field}'] = {'name': f'{section.title()} phase {index} {field}', 'value': value, 'unit': unit, 'kind': kind}
    return result


def poll(cloud, serial):
    encoded = quote(serial, safe='')
    sections = {'info': cloud.inverter(serial)}
    errors = []
    for name, path in PATHS.items():
        try:
            sections[name] = cloud.request('GET', path.format(sn=encoded, date=date.today().isoformat()))
        except Exception as exc:
            errors.append(f'{name}: {exc}')
    result = {}
    for name, data in sections.items():
        result.update(normalise(name, data))
    timestamp = sections['info'].get('updateAt') if isinstance(sections['info'], dict) else None
    return result, timestamp, errors, sections['info']


def timestamp_age(timestamp):
    if timestamp is None:
        return None
    try:
        if isinstance(timestamp, (int, float)) or str(timestamp).isdigit():
            number = float(timestamp)
            seconds = number / 1000 if number > 10**11 else number
            return max(0, time.time() - seconds)
        parsed = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return max(0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
