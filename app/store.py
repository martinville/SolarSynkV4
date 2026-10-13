import copy
import json
import os
import tempfile
import time
import threading
from pathlib import Path

DEFAULT = {"version": 1, "cloud": {"server": "api.sunsynk.net", "username": "", "password": ""}, "mqtt": {"mode": "auto", "host": "", "port": 1883, "username": "", "password": "", "tls": False, "prefix": "solarsynkv4"}, "selected": [], "devices": [], "names": {}, "poll_seconds": 300, "admin": {"enabled": False, "username": "admin", "hash": ""}, "history": [], "activity": [], "owned": {}}

class Store:
    def __init__(self, directory='/data'):
        self._lock = threading.RLock()
        self.path = Path(directory) / 'config.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.data = copy.deepcopy(DEFAULT)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if not isinstance(loaded, dict) or loaded.get('version') != 1:
                    raise ValueError('Unsupported configuration version')
                for key, default in DEFAULT.items():
                    if key not in loaded:
                        continue
                    value = loaded[key]
                    if isinstance(default, dict):
                        if not isinstance(value, dict):
                            raise ValueError('Invalid saved configuration section')
                        self.data[key] = {**default, **value}
                    elif not isinstance(value, type(default)):
                        raise ValueError('Invalid saved configuration section')
                    else:
                        self.data[key] = value
                if not all(isinstance(item, str) for item in self.data['selected']):
                    raise ValueError('Invalid selected inverter list')
                if not all(isinstance(item, dict) and isinstance(item.get('sn'), str) for item in self.data['devices']):
                    raise ValueError('Invalid saved inverter list')
                if not 60 <= self.data['poll_seconds'] <= 1800:
                    self.data['poll_seconds'] = DEFAULT['poll_seconds']
                if not self.data.get('devices') and self.data.get('selected'):
                    self.data['devices'] = [{'sn': serial, 'alias': self.data.get('names', {}).get(serial) or serial, 'plant': 'Previously selected', 'model': '', 'status': None, 'updated': None, 'role': None} for serial in self.data['selected']]
            except (ValueError, OSError):
                backup = self.path.with_name('config.corrupt.' + str(int(time.time())) + '.json')
                os.replace(self.path, backup)
                self.error = 'Saved configuration was damaged. The original was kept as ' + backup.name
                self.data = copy.deepcopy(DEFAULT)
    def save(self):
        with self._lock:
            fd, name = tempfile.mkstemp(dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    os.chmod(name, 0o600)
                    json.dump(self.data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def record_activity(self, category, message, serial='', level='info'):
        """Store a short, redacted application event for the Activity screen."""
        if category not in ('cloud', 'mqtt', 'inverters', 'settings') or level not in ('info', 'warning', 'error'):
            raise ValueError('Invalid activity type')
        with self._lock:
            self.data.setdefault('activity', []).append({'at': time.time(), 'category': category, 'message': str(message)[:240], 'serial': str(serial)[:80], 'level': level})
            self.data['activity'] = self.data['activity'][-200:]
            self.save()

    def public(self):
        out = copy.deepcopy(self.data)
        out['cloud']['password'] = ''
        out['mqtt']['password'] = ''
        out['admin']['hash'] = ''
        return out
