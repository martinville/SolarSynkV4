from .settings import CONTROLS, readable_settings


def entity_rows(serial, metrics, observed_settings, commands):
    """Describe only the entities this add-on can actually discover."""
    rows = []
    for key, item in sorted(metrics.items(), key=lambda pair: pair[1].get('name', pair[0]).lower()):
        rows.append({'name': item.get('name', key), 'system': f'solarsynkv4_{serial}_{key}', 'unit': item.get('unit') or '—', 'value': item.get('value'), 'status': ''})
    for key, definition in CONTROLS.items():
        if key not in observed_settings or observed_settings[key] is None:
            continue
        pending = commands.get((serial, key))
        value = pending['value'] if pending else observed_settings[key]
        if definition[1] == 'switch':
            value = 'On' if value is True or str(value).lower() in ('true', '1', 'on') else 'Off'
        rows.append({'name': definition[0], 'system': f'solarsynkv4_{serial}_{key}', 'unit': definition[5] or '—', 'value': value, 'status': 'Pending' if pending else ''})
    for key, value in readable_settings(observed_settings).items():
        if key not in CONTROLS:
            rows.append({'name': 'Cloud setting ' + key, 'system': f'solarsynkv4_{serial}_setting_{key}', 'unit': '—', 'value': value, 'status': 'Read only'})
    return rows


def uptime_label(seconds):
    minutes = max(0, int(seconds)) // 60
    days, remainder = divmod(minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    return f'{minutes}m'
