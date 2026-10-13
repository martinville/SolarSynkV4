import base64
import hashlib
import re
import time
import threading
from urllib.parse import quote
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

SERVERS = ('api.sunsynk.net', 'pv.inteless.com')
class CloudError(Exception):
    pass

class Cloud:
    def __init__(self, config, session=None):
        self.config = config
        self.http = session or requests.Session()
        self.token = None
        self.expires = 0
        self.last_success = None
        self.lock = threading.RLock()
    @property
    def base(self):
        server = self.config['server']
        if server not in SERVERS:
            raise CloudError('Choose Sunsynk or Inteless.')
        return 'https://' + server
    def login(self):
        source = 'elinter' if self.config['server'] == 'pv.inteless.com' else 'sunsynk'
        nonce = int(time.time() * 1000)
        sign = hashlib.md5(f'nonce={nonce}&source={source}POWER_VIEW'.encode()).hexdigest()
        try:
            response = self.http.get(self.base + '/anonymous/publicKey', params={'source': source, 'nonce': nonce, 'sign': sign}, timeout=15)
            response.raise_for_status()
            key_text = response.json()['data']
            pem = '-----BEGIN PUBLIC KEY-----\n' + key_text + '\n-----END PUBLIC KEY-----\n'
            key = serialization.load_pem_public_key(pem.encode())
            encrypted = base64.b64encode(key.encrypt(self.config['password'].encode(), padding.PKCS1v15())).decode()
            nonce = int(time.time() * 1000)
            # V3 signs this request with the literal sunsynk source even for Inteless.
            sign = hashlib.md5(f'nonce={nonce}&source=sunsynk{key_text[:10]}'.encode()).hexdigest()
            response = self.http.post(self.base + '/oauth/token/new', json={'client_id': 'csp-web', 'grant_type': 'password', 'password': encrypted, 'source': source, 'username': self.config['username'], 'nonce': nonce, 'sign': sign}, timeout=15)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get('data'), dict) or body.get('msg') != 'Success' or not body['data'].get('access_token'):
                raise CloudError('Cloud sign-in failed. Check your account, password and cloud service. MFA accounts may not be supported.')
            self.token = body['data']['access_token']
            self.expires = time.monotonic() + max(60, int(body['data'].get('expires_in', 3600)) - 120)
            self.last_success = time.time()
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            raise CloudError('Could not sign in to the cloud service.') from exc
    def request(self, method, path, **kwargs):
        with self.lock:
            return self._request(method, path, **kwargs)
    def _request(self, method, path, **kwargs):
        if not self.token or time.monotonic() >= self.expires:
            self.login()
        for attempt in range(2):
            try:
                response = self.http.request(method, self.base + path, headers={'Authorization': 'Bearer ' + self.token}, timeout=15, **kwargs)
                if response.status_code == 401 and attempt == 0:
                    self.token = None
                    self.login()
                    continue
                if response.status_code == 429:
                    raise CloudError('Cloud rate limit reached. Wait before trying again.')
                if response.status_code == 403:
                    raise CloudError('This account does not have permission for this inverter or setting.')
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise CloudError('Cloud returned an invalid response.')
                message = str(body.get('msg') or '')
                success = re.fullmatch(r'(?:success|send\s+command\s+success(?:\s*:\s*\{\s*\})?)', message.strip(), re.I)
                if not success or str(body.get('code', 0)) != '0':
                    raise CloudError(str(body.get('msg') or 'Cloud request failed.')[:200])
                self.last_success = time.time()
                return body.get('data')
            except requests.RequestException as exc:
                raise CloudError('The cloud service did not respond. Try again later.') from exc
        raise CloudError('Cloud authentication expired.')
    def inverter(self, serial):
        return self.request('GET', '/api/v1/inverter/' + quote(serial, safe=''))
    def settings(self, serial):
        return self.request('GET', '/api/v1/common/setting/' + quote(serial, safe='') + '/read')
    def write_settings(self, serial, payload):
        return self.request('POST', '/api/v1/common/setting/' + quote(serial, safe='') + '/set', json=payload)
