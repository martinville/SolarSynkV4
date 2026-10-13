def mqtt_service_status(token, http):
    """Read MQTT details from Supervisor with a safe, user-facing failure reason."""
    if not token:
        return None, 'Supervisor access token is unavailable to this add-on.'
    try:
        response = http.get('http://supervisor/services/mqtt', headers={'Authorization': 'Bearer ' + token}, timeout=5)
        response.raise_for_status()
        envelope = response.json()
        if not isinstance(envelope, dict):
            return None, 'Supervisor returned an invalid MQTT service response.'
        if envelope.get('result') == 'error':
            return None, 'Supervisor reports no MQTT service available to this add-on.'
        data = envelope.get('data', envelope)
        if not isinstance(data, dict) or not data.get('host'):
            return None, 'Supervisor did not provide an MQTT broker address.'
        port = int(data.get('port') or 1883)
        if not 1 <= port <= 65535:
            return None, 'Supervisor provided an invalid MQTT broker port.'
        return {'host': str(data['host']), 'port': port, 'username': str(data.get('username') or ''), 'password': str(data.get('password') or ''), 'tls': str(data.get('ssl', False)).lower() in ('true', '1')}, None
    except Exception as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in (401, 403):
            return None, 'Supervisor denied access to MQTT service details.'
        if status == 404:
            return None, 'Supervisor has no MQTT service registered for this add-on.'
        return None, 'Could not read MQTT service details from Supervisor right now.'


def mqtt_service(token, http):
    """Compatibility helper returning only connection details."""
    return mqtt_service_status(token, http)[0]
