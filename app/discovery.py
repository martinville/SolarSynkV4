from urllib.parse import urlencode
from .cloud import CloudError

def _records(data):
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        raise CloudError('The cloud returned an unexpected device list.')
    rows = data.get('infos', data.get('records', data.get('list')))
    if not isinstance(rows, list):
        raise CloudError('The cloud returned an unexpected device list.')
    return rows, data.get('total')

def _pages(cloud, path, extra=None):
    page = 1
    while page <= 100:
        params = {'page': page, 'limit': 10}
        params.update(extra or {})
        rows, total = _records(cloud.request('GET', path + '?' + urlencode(params)))
        yield from rows
        if not rows or len(rows) < 10:
            return
        try:
            if total is not None and int(total) > 0 and page * 10 >= int(total):
                return
        except (TypeError, ValueError):
            pass
        page += 1
    raise CloudError('The account has more than 100 pages. Discovery stopped safely.')

def discover(cloud):
    plants = list(_pages(cloud, '/api/v1/plants'))
    found = {}
    errors = []
    for plant in plants:
        plant_id = plant.get('id')
        if plant_id is None:
            continue
        try:
            devices = _pages(cloud, f'/api/v1/plant/{plant_id}/inverters', {'status': -1, 'type': -2})
            for item in devices:
                serial = str(item.get('sn') or '').strip()
                if not serial:
                    continue
                found[serial] = {'sn': serial, 'alias': item.get('alias') or serial, 'model': item.get('model') or '', 'plant': plant.get('name') or str(plant_id), 'status': item.get('status'), 'updated': item.get('updateAt'), 'role': item.get('parallelRole')}
        except CloudError as exc:
            detail = str(exc)
            if 'org.springframework' in detail or 'invSearchDTO' in detail:
                detail = 'The cloud rejected the inverter search. Please retry after updating V4, or check account permissions.'
            errors.append(f"{plant.get('name') or plant_id}: {detail}")
    return list(found.values()), errors


def merge_devices(cached, found):
    """Keep remembered serials while updating details for devices found now."""
    by_serial = {item['sn']: dict(item) for item in cached if item.get('sn')}
    added = 0
    for item in found:
        serial = item.get('sn')
        if not serial:
            continue
        if serial not in by_serial:
            added += 1
        by_serial[serial] = dict(item)
    return list(by_serial.values()), added
