import html
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from collections import defaultdict
import requests
from argon2 import PasswordHasher
from flask import Flask, abort, g, make_response, redirect, request
from .cloud import Cloud, CloudError
from .discovery import discover, merge_devices
from .dashboard import entity_rows, uptime_label
from .mqtt import Broker, test_connection
from .naming import DEVICE_NAME_HINT, device_name, validate_device_name
from .settings import CONTROLS, SettingsManager, validate
from .store import Store
from .supervisor import mqtt_service_status
from .telemetry import poll, timestamp_age

app = Flask(__name__)
store = Store(os.environ.get('SOLARSYNKV4_DATA', '/data'))
cloud = Cloud(store.data['cloud'])
sessions = {}
attempts = defaultdict(list)
state = {'devices': list(store.data.get('devices', [])), 'discovery_message': '', 'errors': [], 'metrics': {}, 'api': 'Not connected', 'mqtt_service_error': None, 'last_publish': None, 'manual_publish': None}
lock = threading.RLock()
poll_lock = threading.Lock()
poll_changed = threading.Event()
started_at = time.time()

def activity_event(category, message, serial='', level='info'):
    try:
        store.record_activity(category, message, serial=serial, level=level)
        if category == 'settings':
            logging.getLogger('gunicorn.error').info('%s [%s]', message, serial)
    except OSError:
        pass

def supervisor_mqtt():
    service, error = mqtt_service_status(os.environ.get('SUPERVISOR_TOKEN'), requests)
    state['mqtt_service_error'] = error
    return service

def refresh_mqtt_service():
    cfg = store.data['mqtt']
    if cfg.get('mode', 'auto') != 'auto':
        return False
    service = supervisor_mqtt()
    if not service:
        return False
    if any(cfg.get(key) != value for key, value in service.items()):
        cfg.update(service)
        store.save()
        running = globals().get('broker')
        if running:
            running.stop()
            running.config = cfg
            running.start()
    return True

if store.data['mqtt'].get('mode', 'auto') == 'auto':
    refresh_mqtt_service()
broker = Broker(store.data['mqtt'], store, lambda serial, key, value: settings.request(serial, key, value, immediate=True), activity_event)
settings = SettingsManager(cloud, store, broker.control_state, on_event=activity_event)
broker.start()

def poll_selected():
    succeeded = 0
    errors = []
    for serial in list(store.data['selected']):
        try:
            metrics, timestamp, section_errors, detail = poll(cloud, serial)
            info = next((d for d in state['devices'] if d['sn'] == serial), {'sn': serial})
            if isinstance(detail, dict):
                info = {**info, 'alias': detail.get('alias') or info.get('alias'), 'model': detail.get('model') or info.get('model')}
            with lock:
                previous = state['metrics'].get(serial, {})
                changed_at = previous.get('changed_at', time.time()) if previous.get('timestamp') == timestamp and timestamp else time.time()
                age = timestamp_age(timestamp)
                fresh = age is not None and age < 900 and time.time() - changed_at < 900
                state['metrics'][serial] = {'values': metrics, 'timestamp': timestamp, 'at': time.time(), 'changed_at': changed_at, 'fresh': fresh, 'age': age, 'errors': section_errors}
                state['api'] = 'Connected'
            broker.telemetry(serial, info, metrics, timestamp, fresh)
            activity_event('cloud', f'Fetched {len(metrics)} telemetry value(s); inverter data is {"fresh" if fresh else "stale or untimed"}.', serial=serial, level='info' if fresh else 'warning')
            if section_errors:
                errors.append(f'{serial}: some telemetry groups could not be read')
            try:
                observed = cloud.settings(serial)
                if not isinstance(observed, dict) or str(observed.get('sn')) != serial:
                    raise CloudError('The cloud returned settings for a different inverter.')
                broker.settings(serial, observed)
                settings.observe(serial, observed)
            except CloudError as exc:
                errors.append(f'{serial}: settings could not be read: {exc}')
            succeeded += 1
        except Exception as exc:
            broker.publish(f'{broker.prefix}/{serial}/availability', 'offline', retain=True)
            errors.append(f'{serial}: {exc}')
            store.record_activity('cloud', 'Could not fetch inverter data. Check cloud connection and inverter access.', serial=serial, level='error')
            with lock:
                if serial in state['metrics']:
                    state['metrics'][serial]['fresh'] = False
                state['api'] = str(exc)
    with lock:
        state['last_publish'] = time.time()
        state['errors'] = (state['errors'] + errors)[-20:]
    return succeeded, errors

def background():
    while True:
        if store.data['mqtt'].get('mode', 'auto') == 'auto' and not broker.connected:
            refresh_mqtt_service()
        if store.data['cloud']['username'] and store.data['selected']:
            with poll_lock:
                poll_selected()
        poll_changed.wait(max(60, min(1800, int(store.data['poll_seconds']))))
        poll_changed.clear()

def manual_publish():
    try:
        if broker.connected:
            broker.republish(force=True)
        succeeded, errors = poll_selected()
        if not broker.connected:
            errors.append('MQTT disconnected during the refresh; reconnect and try again.')
        with lock:
            state['manual_publish'] = {'status': 'finished' if broker.connected else 'failed', 'succeeded': succeeded, 'errors': errors, 'at': time.time()}
        activity_event('mqtt', f'Manual refresh finished for {succeeded} inverter(s).', level='info' if broker.connected else 'warning')
    except Exception:
        with lock:
            state['manual_publish'] = {'status': 'failed', 'succeeded': 0, 'errors': ['The refresh stopped unexpectedly. Try again.'], 'at': time.time()}
    finally:
        poll_lock.release()

def refresh_after_change(reason):
    with poll_lock:
        succeeded, errors = poll_selected()
    activity_event('cloud', f'{reason}: refreshed {succeeded} selected inverter(s).', level='warning' if errors else 'info')

threading.Thread(target=background, daemon=True).start()

def esc(value):
    return html.escape(str(value), quote=True)
def path(name):
    return './' + name

def current_session():
    token = request.cookies.get('ssv4_session')
    item = sessions.get(token)
    if not item or item['expires'] < time.time():
        return None
    return item

@app.before_request
def security():
    if os.environ.get('SUPERVISOR_TOKEN') and request.remote_addr not in ('172.30.32.2', '127.0.0.1'):
        abort(403)
    g.csrf = request.cookies.get('ssv4_csrf') or secrets.token_urlsafe(24)
    if request.endpoint in ('login', 'login_post'):
        if request.method == 'POST' and request.form.get('csrf') != g.csrf:
            abort(403)
        return
    if store.data['admin']['enabled'] and not current_session():
        return redirect(path('login'))
    if request.method == 'POST':
        session = current_session()
        expected = session['csrf'] if session else request.cookies.get('ssv4_csrf')
        if not expected or not secrets.compare_digest(request.form.get('csrf', ''), expected):
            abort(403)

def csrf():
    session = current_session()
    if session:
        return session['csrf']
    return g.csrf
def form(action, body, label):
    return f'<form method="post" action="{path(action)}"><input type="hidden" name="csrf" value="{esc(csrf())}">{body}<button class="btn btn-primary">{esc(label)}</button></form>'
def field(name, label, value='', typ='text', required=False, hint=''):
    return f'<label>{esc(label)}<input class="form-control" name="{esc(name)}" type="{typ}" value="{esc(value)}" {"required" if required else ""}></label><small>{esc(hint)}</small>'
STYLE = '''
:root{color-scheme:dark;--bg:#0a1014;--panel:#162127;--panel2:#1c2a31;--line:#34454d;--text:#edf4f3;--muted:#aebfc1;--accent:#49d7b4;font:16px system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 85% -15%,#173a38 0,transparent 42%),var(--bg);line-height:1.5}a{color:#80e6cf}a:hover{color:#b3f8e8}h1,h2,h3,h4,p{margin-top:0}h1{font-size:1.25rem;letter-spacing:-.02em}h2{font-size:clamp(1.7rem,3vw,2.25rem);letter-spacing:-.035em;margin-bottom:.35rem}h3{font-size:1.15rem;margin-bottom:.35rem}p{color:var(--muted)}header{background:#10191dda;border-bottom:1px solid var(--line)}.header-inner,main{max-width:1120px;margin:auto;padding:0 1.5rem}.header-inner{padding-top:1rem}.brand{display:flex;align-items:center;gap:.7rem;font-weight:750;font-size:1.2rem;letter-spacing:-.03em}.brand-mark{display:grid;place-items:center;width:2rem;height:2rem;border-radius:10px;color:#06231b;background:linear-gradient(145deg,#96efbc,#3fc7ad);font-size:1.2rem}nav{display:flex;align-items:center;gap:.2rem;flex-wrap:wrap;margin-top:.9rem}nav a{display:inline-block;padding:.75rem .9rem;text-decoration:none;color:var(--muted);border-bottom:2px solid transparent;font-weight:600}nav a:hover,nav a.active{color:var(--text);border-bottom-color:var(--accent)}nav form{margin-left:auto}nav form button{margin:0;background:transparent;color:var(--muted);border:1px solid var(--line);padding:.45rem .8rem;min-height:38px}main{padding-top:2rem;padding-bottom:4rem}.page-heading{margin-bottom:1.5rem}.page-heading p{max-width:65ch;margin-bottom:0}.card,.settings-panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;box-shadow:0 14px 35px #0002}.card{padding:1.5rem;margin:1rem 0}.grid,.fields-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1rem 1.3rem}.settings-panel{overflow:hidden}.settings-intro{padding:1.5rem 1.75rem;background:linear-gradient(100deg,#1a3030,#182329);border-bottom:1px solid var(--line)}.settings-intro h3{margin:0}.settings-intro p{margin:.35rem 0 0}.settings-section{display:grid;grid-template-columns:minmax(180px,240px) minmax(0,1fr);gap:2rem;padding:1.7rem 1.75rem;border-bottom:1px solid var(--line)}.section-heading{display:flex;align-items:center;gap:.65rem}.section-number{display:grid;place-items:center;flex:none;width:1.7rem;height:1.7rem;border-radius:8px;background:#25453f;color:#9ef0d7;font-size:.75rem;font-weight:800}.section-copy p{font-size:.91rem;margin:.55rem 0 0}.section-fields{min-width:0}.section-fields>p:first-child{margin-top:0}.form-actions{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1.2rem 1.75rem;background:#18262a}.form-actions p{margin:0;font-size:.9rem}.form-actions button{margin:0;min-width:165px}label{display:block;font-weight:650;font-size:.91rem;margin:0 0 .3rem}input,select{display:block;width:100%;background:#0e181c;color:var(--text);border:1px solid #53676d;border-radius:10px;padding:.75rem .85rem;margin-top:.4rem;font:inherit;min-height:44px}input:hover,select:hover{border-color:#78a39f}input[type=checkbox]{display:inline;width:auto;min-width:22px;min-height:22px;margin-right:.5rem}small,.muted{color:var(--muted);display:block;font-size:.84rem;margin:.2rem 0 .9rem}button{background:#247e70;border:1px solid #38a58f;color:white;padding:.75rem 1.1rem;border-radius:10px;cursor:pointer;font:inherit;font-weight:700;min-height:44px;margin-top:.6rem}button:hover{background:#309987}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #a2f5df;outline-offset:3px}details{border:1px solid var(--line);border-radius:12px;padding:.9rem 1rem;margin-top:1rem;background:#111c20}summary{cursor:pointer;font-weight:700;color:#c8e9e2}details[open] summary{margin-bottom:1rem}.notice,.error{padding:1rem 1.1rem;border-radius:10px;border:1px solid}.notice{background:#18352f;color:#d4f6e8;border-color:#347f68}.error{background:#41292e;color:#ffe2e3;border-color:#95515b}.pending-warning{background:#5a2630;color:#fff0f0;border:2px solid #e68490;border-radius:10px;padding:.85rem 1rem;margin:.7rem 0 1rem;font-weight:700;box-shadow:0 0 0 3px #e6849022}.next-step{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}.next-step p{margin-bottom:0}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.65rem;border-bottom:1px solid var(--line)}.button-link{display:inline-flex;align-items:center;justify-content:center;background:#247e70;border:1px solid #38a58f;color:white;text-decoration:none;padding:.75rem 1.1rem;border-radius:10px;font-weight:700;min-height:44px}.button-link:hover{background:#309987;color:white}.modal-overlay{display:none;position:fixed;inset:0;z-index:10;align-items:center;justify-content:center;background:#03090bcc;padding:1rem}.modal-overlay:target,.modal-overlay.is-open{display:flex}.modal-card{width:min(100%,620px);max-height:calc(100vh - 2rem);overflow:auto;background:#18262c;border:1px solid #527069;border-radius:18px;box-shadow:0 25px 80px #0009;padding:1.5rem}.modal-header,.modal-actions{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}.modal-header p{margin:0}.modal-close{font-size:1.8rem;line-height:1;text-decoration:none;color:var(--muted);min-width:44px;min-height:44px;text-align:center}.mode-choice{display:grid;grid-template-columns:1fr 1fr;gap:.65rem;margin:1.3rem 0}.mode-radio{position:absolute;opacity:0;width:1px;height:1px}.mode-option{display:flex;flex-direction:column;gap:.3rem;margin:0;padding:1rem;border:1px solid var(--line);border-radius:12px;cursor:pointer;background:#101c20;min-height:95px}.mode-option span{font-size:.84rem;font-weight:400;color:var(--muted)}.mode-radio:checked+.mode-option{border-color:var(--accent);background:#1a3631}.mode-radio:focus-visible+.mode-option{outline:3px solid #a2f5df;outline-offset:3px}.manual-fields{display:none;grid-column:1/-1;border:1px solid var(--line);border-radius:12px;padding:1rem}.mode-radio#mqtt-manual:checked~.manual-fields{display:block}.mqtt-advanced{margin:1rem 0}.modal-actions{align-items:center;margin-top:1.3rem}.modal-actions button{margin:0}@media(max-width:600px){.mode-choice{grid-template-columns:1fr}.manual-fields{grid-column:1}.modal-card{padding:1.1rem}.modal-actions{align-items:stretch;flex-direction:column-reverse}.modal-actions button{width:100%}}
.container{width:100%;max-width:1120px;margin-inline:auto;padding-inline:1.5rem}.row{display:flex;flex-wrap:wrap;margin-inline:-.75rem}.row>*{padding-inline:.75rem}.col{flex:1 0 0}.col-12{flex:0 0 100%}.btn,.btn-primary{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:.75rem 1.1rem;border-radius:10px;background:#247e70;border:1px solid #38a58f;color:#fff;text-decoration:none;font-weight:700}.form-control,.form-select{display:block;width:100%;background:#0e181c;color:var(--text);border:1px solid #53676d;border-radius:10px;padding:.75rem .85rem}.table-responsive{width:100%;overflow-x:auto}.table{width:100%;border-collapse:collapse}.table-dark{color:var(--text);background:transparent}.table-hover tbody tr:hover{background:#203138}.badge{display:inline-block;border-radius:999px;padding:.25rem .6rem;background:#25453f;color:#b5f4df;font-weight:700;font-size:.78rem}.stats-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1rem;margin-bottom:1.25rem}.stat-card{background:linear-gradient(160deg,#1b2c31,#142126);border:1px solid var(--line);border-radius:16px;padding:1.15rem 1.25rem;box-shadow:0 12px 28px #0002}.stat-label{display:block;color:var(--muted);font-size:.83rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em}.stat-card strong{display:block;font-size:2rem;line-height:1.25;margin:.55rem 0 .2rem;letter-spacing:-.04em}.stat-card small{margin:0}.dashboard-status{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}.dashboard-status p{margin-bottom:.3rem}.dashboard-status form button{margin:0}.dashboard-status>.muted{flex-basis:100%;margin:0}.inverter-card{padding:0;margin:1rem 0;background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}.inverter-card summary{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1.1rem 1.35rem;list-style:none;cursor:pointer}.inverter-card summary::-webkit-details-marker{display:none}.inverter-card[open] summary{border-bottom:1px solid var(--line);margin:0}.inverter-title{display:flex;align-items:center;gap:.9rem}.inverter-title strong{display:block;color:var(--text);font-size:1.05rem}.inverter-title small{margin:0}.chevron{display:grid;place-items:center;width:2rem;height:2rem;border-radius:9px;background:#263d3e;color:#9ef0d7;font-size:1.8rem;line-height:1;transition:transform .2s}.inverter-card[open] .chevron{transform:rotate(90deg)}.summary-time{font-size:.84rem;color:var(--muted);text-align:right}.inverter-body{padding:1.3rem}.inverter-body p{margin-bottom:1rem}.inverter-body code{font-size:.82rem;color:#b5e8d9;overflow-wrap:anywhere}.inverter-body td:last-child{font-weight:700;white-space:nowrap}.inverter-body th,.inverter-body td{padding:.35rem .55rem;font-size:.82rem;line-height:1.2}.inverter-body tbody tr:nth-child(even){background:#18272c}.inverter-body th{color:#c4d9d8;font-size:.85rem}.inverter-body small{margin:1rem 0 0}@media(max-width:800px){.stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.container{padding-inline:1rem}.stats-grid{gap:.65rem}.stat-card{padding:1rem}.stat-card strong{font-size:1.6rem}.dashboard-status{align-items:stretch}.dashboard-status form,.dashboard-status button{width:100%}.inverter-card summary{align-items:flex-start;flex-direction:column}.summary-time{text-align:left;padding-left:2.9rem}.inverter-body{padding:1rem}.inverter-body table{min-width:620px}}
.header-inner.navbar{display:flex;align-items:center;gap:1.5rem;min-height:68px;padding-top:.65rem;padding-bottom:.65rem}.navbar-brand.brand{color:var(--text);text-decoration:none;margin:0;white-space:nowrap}.navbar-brand.brand:hover{color:var(--text)}nav.navbar-nav{display:flex;flex-direction:row;align-items:center;gap:.15rem;margin:0 0 0 auto;flex-wrap:wrap}nav.navbar-nav .nav-link{border:0;border-radius:9px;padding:.6rem .8rem;white-space:nowrap;color:var(--muted);font-weight:650}nav.navbar-nav .nav-link:hover,nav.navbar-nav .nav-link.active{color:#d9fff1;background:#1b3935}nav.navbar-nav form{margin:0 0 0 .3rem}nav.navbar-nav form button{margin:0}.nav-dropdown{position:relative;border:0;background:transparent;margin:0;padding:0;overflow:visible}.nav-dropdown summary{margin:0!important;list-style:none;cursor:pointer}.nav-dropdown summary::-webkit-details-marker{display:none}.nav-dropdown .dropdown-menu{position:absolute;top:calc(100% + .4rem);left:0;min-width:205px;padding:.4rem;background:#1d2b30;border:1px solid #48605e;border-radius:12px;box-shadow:0 16px 35px #0008;z-index:20}.nav-dropdown:not([open]) .dropdown-menu{display:none}.nav-dropdown .dropdown-item{display:block;color:var(--text);border-radius:8px;padding:.65rem .75rem;text-decoration:none}.nav-dropdown .dropdown-item:hover,.nav-dropdown .dropdown-item:focus{background:#29473f;color:#d9fff1}.inverter-modal{width:min(100%,820px)}.refresh-row{display:flex;align-items:center;justify-content:space-between;gap:1rem;margin:1.2rem 0;padding:1rem;border:1px solid var(--line);border-radius:12px;background:#102025}.refresh-row span{font-weight:700}.refresh-row button{margin:0}.inverter-list{max-height:48vh;overflow:auto}.inverter-list form>button{position:sticky;bottom:0;width:100%;margin:1rem 0 0}.inverter-choice{padding:1rem;border:1px solid var(--line);border-radius:12px;margin:.65rem 0;background:#111e22}.inverter-choice>label{display:flex;align-items:flex-start;gap:.65rem;margin-bottom:.8rem}.inverter-choice>label span{min-width:0}.inverter-choice>label strong{display:block}.inverter-choice>label small{margin:0}.inverter-choice input[type=checkbox]{flex:none;margin:.15rem 0 0}.inverter-choice label:has(input:not([type=checkbox])){font-size:.82rem}@media(max-width:820px){.header-inner.navbar{align-items:flex-start;flex-direction:column;gap:.5rem}nav.navbar-nav{margin:0;width:100%}.nav-dropdown .dropdown-menu{left:auto;right:0}}@media(max-width:600px){nav.navbar-nav{gap:0}nav.navbar-nav .nav-link{padding:.55rem .6rem;font-size:.87rem}.refresh-row{align-items:stretch;flex-direction:column}.refresh-row form,.refresh-row button{width:100%}.nav-dropdown .dropdown-menu{left:0;right:auto}}
.setup-modal{width:min(100%,900px)}.setup-modal .settings-panel{box-shadow:none;margin-top:1rem}.setup-modal .settings-section{padding:1.1rem 1.25rem;gap:1.25rem}.setup-modal .settings-intro{padding:1rem 1.25rem}.setup-modal .form-actions{padding:1rem 1.25rem}.setup-modal .section-copy p{font-size:.84rem}.inverter-body .table-responsive{border:1px solid var(--line);border-radius:10px}.inverter-body table{margin-bottom:0}.inverter-body thead{background:#203239}
@media(max-width:700px){.header-inner,main{padding-left:1rem;padding-right:1rem}.settings-section{grid-template-columns:1fr;gap:1rem;padding:1.35rem}.settings-intro,.form-actions{padding:1.25rem}.form-actions{align-items:stretch;flex-direction:column}.form-actions button{width:100%}nav a{padding:.7rem .55rem;font-size:.9rem}nav form{margin-left:0}.card{padding:1.2rem}table{display:block;overflow-x:auto;font-size:.85rem}}
'''

def inverter_modal():
    rows = ''
    entered = getattr(g, 'inverter_form', None)
    selected = entered.getlist('serial') if entered is not None else store.data['selected']
    for item in state['devices']:
        serial = item['sn']
        name = device_name(store.data['names'].get(serial), item.get('alias'), serial)
        value = entered.get('name_' + serial, name) if entered is not None else name
        detail = ' · '.join(str(value) for value in (item.get('plant'), item.get('model'), serial) if value)
        name_field = f'<label>Name in Home Assistant<input class="form-control" name="{esc("name_" + serial)}" type="text" value="{esc(value)}" maxlength="80"></label><small>{esc(DEVICE_NAME_HINT)}</small>'
        rows += f'<div class="inverter-choice"><label><input type="checkbox" name="serial" value="{esc(serial)}" {"checked" if serial in selected else ""}><span><strong>{esc(name)}</strong><small>{esc(detail)}</small></span></label>{name_field}</div>'
    if not rows:
        rows = '<p>No saved inverters yet. Connect your cloud account, then refresh the list.</p>'
    status = '<p class="notice" role="status">' + esc(state['discovery_message']) + '</p>' if state.get('discovery_message') else ''
    if getattr(g, 'inverter_error', ''):
        status += '<p class="error" role="alert">' + esc(g.inverter_error) + '</p>'
    if request.args.get('saved_inverters'):
        status += '<p class="notice" role="status">Inverter selection saved.</p>'
    opened = ' is-open' if request.args.get('inverters') == 'open' or getattr(g, 'inverter_error', '') else ''
    body = f'<div id="select-inverters" class="modal-overlay{opened}" role="dialog" aria-modal="true" aria-labelledby="inverter-dialog-title"><div class="modal-card inverter-modal"><div class="modal-header"><div><h3 id="inverter-dialog-title">Select Inverters</h3><p>Your saved list remains here after restart. Refresh only when your cloud account gains a new inverter.</p></div><a class="modal-close" href="./" aria-label="Close inverter selection">×</a></div>'
    body += status + '<div class="refresh-row"><span>' + esc(len(state['devices'])) + ' inverter(s) in your saved list</span>' + form('discover', '', 'Refresh inverter list') + '</div>'
    body += '<p class="muted">Select only the inverters you want in Home Assistant. Deselecting one stops its updates and removes its V4 discovery entities; history remains in Home Assistant.</p>'
    body += '<div class="inverter-list">' + form('devices', rows, 'Save selected inverters') + '</div></div></div>'
    return body

def page(title, content, message=''):
    current = {'Overview': 'overview', 'Setup': 'setup', 'Cloud Setup': 'setup', 'MQTT': 'setup', 'Inverter Settings': 'settings', 'Activity': 'activity', 'Help': 'help'}.get(title)
    links = [('Overview', './', 'overview'), ('Inverter Settings', './settings', 'settings'), ('Activity', './activity', 'activity'), ('Help', './help', 'help')]
    left = ''.join(f'<a class="nav-link {"active" if current == key else ""}" href="{url}" {"aria-current=page" if current == key else ""}>{label}</a>' for label, url, key in links[:2])
    right = ''.join(f'<a class="nav-link {"active" if current == key else ""}" href="{url}" {"aria-current=page" if current == key else ""}>{label}</a>' for label, url, key in links[2:])
    setup_menu = '<details class="nav-dropdown"><summary class="nav-link dropdown-toggle ' + ('active' if current == 'setup' else '') + '">Setup</summary><div class="dropdown-menu show"><a class="dropdown-item" href="#cloud-setup">Cloud Setup</a><a class="dropdown-item" href="#select-inverters">Select Inverters</a><a class="dropdown-item" href="./?mqtt=open">Setup MQTT</a></div></details>'
    nav = '' if title == 'Log in' else '<nav class="navbar-nav ms-auto" aria-label="Main navigation">' + left + setup_menu + right + (form('logout', '', 'Log out') if store.data['admin']['enabled'] else '') + '</nav>'
    subtitles = {'Cloud Setup': 'Manage your cloud account, extra sign-in and polling preferences.', 'Overview': 'Your selected inverters and their latest available readings.', 'Inverter Settings': 'Adjust supported inverter controls and follow their confirmation status.', 'MQTT': 'Connect SolarSynkV4 to Home Assistant devices and controls.'}
    modal = '' if title == 'Log in' else cloud_modal() + inverter_modal() + mqtt_modal(getattr(g, 'mqtt_error', ''), getattr(g, 'mqtt_form', None))
    body = f'''<!doctype html><html lang="en" data-bs-theme="dark"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · SolarSynkV4</title><link rel="stylesheet" href="./static/bootstrap.min.css"><style>{STYLE}</style></head><body><header><div class="header-inner container navbar navbar-expand-lg"><a class="navbar-brand brand" href="./"><span class="brand-mark" aria-hidden="true">☀</span>SolarSynkV4</a>{nav}</div></header><main class="container"><div class="page-heading"><h2>{esc(title)}</h2>{'<p>'+esc(subtitles[title])+'</p>' if title in subtitles else ''}</div>{'<p class="notice" role="status">'+esc(message)+'</p>' if message else ''}{content}</main>{modal}</body></html>'''
    response = make_response(body)
    if not current_session() and not request.cookies.get('ssv4_csrf'):
        response.set_cookie('ssv4_csrf', g.csrf, httponly=True, secure=bool(os.environ.get('SUPERVISOR_TOKEN')), samesite='Lax', path='/')
    response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'self'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/')
def index():
    selected = list(store.data['selected'])
    with lock:
        snapshots = {serial: dict(state['metrics'].get(serial, {})) for serial in selected}
        api_status = state['api']
        last_refresh = state['last_publish']
        manual = dict(state['manual_publish'] or {})
    discovered = sum(1 for record in store.data.get('owned', {}) if record.split(':', 1)[0] in selected)
    fresh_count = sum(1 for sample in snapshots.values() if sample.get('fresh') and time.time() - sample.get('at', 0) < 900)
    stats = (
        ('Inverters', len(selected), 'Selected for monitoring'),
        ('Entities', discovered, 'V4 discovery records'),
        ('Available', fresh_count, 'Fresh inverter data'),
        ('Uptime', uptime_label(time.time() - started_at), 'Since add-on start'),
    )
    body = '<div class="stats-grid">' + ''.join(f'<section class="stat-card"><span class="stat-label">{esc(label)}</span><strong>{esc(value)}</strong><small>{esc(description)}</small></section>' for label, value, description in stats) + '</div>'
    body += '<section class="card dashboard-status"><div><h3>Connections</h3><p><strong>Cloud:</strong> ' + esc(api_status) + ' · <strong>MQTT:</strong> ' + ('Connected' if broker.connected else 'Disconnected') + '</p><p>Last cloud refresh attempt: ' + (esc(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_refresh))) if last_refresh else 'Not yet') + '</p></div>'
    if selected:
        body += form('publish_all', '', 'Send all data to MQTT')
    body += '<p class="muted">MQTT Connected means V4 reached the broker. If the device is missing in Home Assistant, open <a href="./activity">Activity</a> to check discovery, then confirm the Home Assistant MQTT integration has discovery enabled.</p></section>'
    if manual:
        if manual.get('status') == 'running':
            body += '<p class="notice" role="status">Refreshing selected inverters and sending available data to MQTT. Reload this page to see the result.</p>'
        elif manual.get('status') == 'finished':
            body += f'<p class="notice" role="status">Last manual refresh: {esc(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(manual["at"])))}, {esc(manual.get("succeeded", 0))} inverter(s) updated.' + (' Some data could not be read; check the affected inverter cards and try again.' if manual.get('errors') else '') + '</p>'
        else:
            body += '<p class="error" role="alert">The refresh did not finish. Try again.</p>'
    if not selected:
        body += '<section class="card"><h3>No inverters selected yet</h3><p>Connect your account in <a href="./setup">Setup</a>, then use <a href="#select-inverters">Select Inverters</a> to choose the devices you want to monitor.</p></section>'
    for serial in selected:
        sample = snapshots[serial]
        metrics = sample.get('values', {})
        observed = broker.devices.get(serial, {}).get('settings', {})
        rows = entity_rows(serial, metrics, observed, settings.commands)
        name = store.data['names'].get(serial) or broker.devices.get(serial, {}).get('alias') or serial
        availability = 'Available' if sample.get('fresh') and time.time() - sample.get('at', 0) < 900 else 'Stale or unavailable'
        timestamp = sample.get('timestamp') or 'Not received'
        table_rows = ''.join(f'<tr><td>{esc(item["name"])}</td><td><code>{esc(item["system"])}</code></td><td>{esc(item["unit"])}</td><td>{esc(item["value"])}{(" · " + esc(item["status"])) if item["status"] else ""}</td></tr>' for item in rows)
        if not table_rows:
            table_rows = '<tr><td colspan="4">No readings yet. Try a refresh after connecting the cloud and MQTT.</td></tr>'
        body += f'<details class="inverter-card"><summary><span class="inverter-title"><span class="chevron" aria-hidden="true">›</span><span><strong>{esc(name)}</strong><small>{esc(serial)} · {esc(availability)}</small></span></span><span class="summary-time">Telemetry: {esc(timestamp)}</span></summary><div class="inverter-body"><p>Values come from the last cloud response. A pending setting shows the requested value until read-back confirms or times out.</p><div class="table-responsive"><table class="table table-dark table-hover"><thead><tr><th scope="col">Entity name</th><th scope="col">Entity system naming</th><th scope="col">UOM</th><th scope="col">Value</th></tr></thead><tbody>{table_rows}</tbody></table></div><small>System naming shows the stable MQTT unique ID. Home Assistant may assign a different visible entity ID.</small></div></details>'
    return page('Overview', body)

@app.post('/publish_all')
def publish_all():
    if not store.data['selected']:
        return page('Overview', '<p><a href="#select-inverters">Select Inverters</a></p>', 'Choose at least one inverter first.'), 400
    if not store.data['cloud']['username']:
        return page('Overview', '<p><a href="./setup">Connect cloud account</a></p>', 'Connect your cloud account first.'), 400
    if not broker.connected:
        return page('Overview', '<p><a href="./mqtt">Open MQTT</a></p>', 'Connect MQTT before sending data.'), 400
    if not poll_lock.acquire(blocking=False):
        return page('Overview', '<p><a href="./">Return to Overview</a></p>', 'A refresh is already running.'), 409
    with lock:
        state['manual_publish'] = {'status': 'running', 'at': time.time()}
    threading.Thread(target=manual_publish, daemon=True).start()
    return redirect('./')

def cloud_modal():
    c, admin = store.data['cloud'], store.data['admin']
    entered = getattr(g, 'cloud_form', {})
    server = entered.get('cloud_server', c['server'])
    cloud_select = '<label>Cloud service<select name="cloud_server"><option value="api.sunsynk.net"' + (' selected' if server == 'api.sunsynk.net' else '') + '>Sunsynk</option><option value="pv.inteless.com"' + (' selected' if server == 'pv.inteless.com' else '') + '>Inteless</option></select></label>'
    enabled = entered.get('admin_enabled', 'yes' if admin['enabled'] else 'no') == 'yes'
    admin_select = '<label>Extra login<select name="admin_enabled"><option value="no"' + (' selected' if not enabled else '') + '>Off — use Home Assistant sign-in</option><option value="yes"' + (' selected' if enabled else '') + '>On — require an additional password</option></select></label>'
    admin_fields = admin_select + field('admin_username', 'Admin username', entered.get('admin_username', admin['username']), required=True) + field('admin_password', 'New admin password', typ='password', hint='Leave blank to keep the saved password.') + field('admin_current', 'Current admin password', typ='password', hint='Required when changing or disabling an existing extra login.')
    cloud_fields = cloud_select + field('cloud_username', 'Cloud account email', entered.get('cloud_username', c['username'])) + field('cloud_password', 'Cloud password', typ='password', hint='Leave blank to keep the saved password.')
    preferences = field('poll', 'Polling interval in seconds', entered.get('poll', store.data['poll_seconds']), typ='number', required=True, hint='60–1800; five minutes is recommended.')
    sections = '<div class="settings-panel"><div class="settings-intro"><h3>Configuration</h3><p>Save cloud account, sign-in and polling changes together.</p></div>'
    for number, heading, description, controls in (
        ('01', 'Cloud account', 'Use the account you use for Sunsynk Connect or Inteless.', cloud_fields),
        ('02', 'Extra sign-in', 'Optional protection. Home Assistant sign-in already controls sidebar access.', admin_fields),
        ('03', 'Polling', 'Choose how often to check the cloud. Faster checks do not force fresher inverter readings.', preferences),
    ):
        sections += f'<section class="settings-section"><div class="section-copy"><div class="section-heading"><span class="section-number">{number}</span><h3>{heading}</h3></div><p>{description}</p></div><div class="section-fields">{controls}</div></section>'
    sections += '<div class="form-actions"><p>All three sections are saved together.</p><button type="submit">Save settings</button></div></div>'
    error = getattr(g, 'cloud_error', '')
    opened = ' is-open' if error or request.args.get('cloud') == 'open' else ''
    body = f'<div id="cloud-setup" class="modal-overlay{opened}" role="dialog" aria-modal="true" aria-labelledby="cloud-dialog-title"><div class="modal-card setup-modal"><div class="modal-header"><div><h3 id="cloud-dialog-title">Cloud Setup</h3><p>Account and application preferences</p></div><a class="modal-close" href="./" aria-label="Close Cloud Setup">×</a></div>'
    if request.args.get('saved_cloud'):
        body += '<p class="notice" role="status">Settings saved.</p>'
    if error:
        body += '<p class="error" role="alert">' + esc(error) + '</p>'
    body += f'<form method="post" action="{path("setup_save")}"><input type="hidden" name="csrf" value="{esc(csrf())}">{sections}</form></div></div>'
    return body

@app.get('/setup')
def setup():
    return redirect('./?cloud=open')

@app.post('/setup_save')
def setup_save():
    old_cloud, old_admin = store.data['cloud'], store.data['admin']
    cloud_candidate = dict(old_cloud)
    admin_candidate = dict(old_admin)
    cloud_candidate['server'] = request.form.get('cloud_server', '')
    cloud_candidate['username'] = request.form.get('cloud_username', '').strip()
    if request.form.get('cloud_password'):
        cloud_candidate['password'] = request.form['cloud_password']
    admin_candidate['enabled'] = request.form.get('admin_enabled') == 'yes'
    admin_candidate['username'] = request.form.get('admin_username', '').strip()
    new_admin_password = request.form.get('admin_password') or ''
    try:
        if cloud_candidate['server'] not in ('api.sunsynk.net', 'pv.inteless.com'):
            raise ValueError('Choose Sunsynk or Inteless.')
        if bool(cloud_candidate['username']) != bool(cloud_candidate['password']):
            raise ValueError('Enter both cloud account email and password.')
        poll_value = int(request.form.get('poll', ''))
        if not 60 <= poll_value <= 1800:
            raise ValueError('Polling interval must be between 60 and 1800 seconds.')
        if not admin_candidate['username']:
            raise ValueError('Enter an admin username.')
        admin_intent = admin_candidate['enabled'] != old_admin['enabled'] or admin_candidate['username'] != old_admin['username'] or bool(new_admin_password)
        if old_admin['enabled'] and admin_intent:
            try:
                if not ph.verify(old_admin['hash'], request.form.get('admin_current', '')):
                    raise ValueError()
            except Exception:
                raise ValueError('Enter the current admin password to change login settings.')
        if new_admin_password and len(new_admin_password) < 12:
            raise ValueError('Use at least 12 characters for the admin password.')
        if admin_candidate['enabled'] and not (new_admin_password or admin_candidate['hash']):
            raise ValueError('Choose an admin password before turning on extra login.')
    except ValueError as exc:
        g.cloud_error = str(exc)
        g.cloud_form = request.form
        return index(), 400
    probe = None
    if cloud_candidate['username'] and cloud_candidate != old_cloud:
        try:
            probe = Cloud(cloud_candidate)
            probe.login()
        except CloudError as exc:
            g.cloud_error = str(exc)
            g.cloud_form = request.form
            return index(), 400
    if new_admin_password:
        admin_candidate['hash'] = ph.hash(new_admin_password)
    cloud_changed = cloud_candidate != old_cloud
    admin_changed = admin_candidate != old_admin
    store.data['cloud'] = cloud_candidate
    store.data['admin'] = admin_candidate
    poll_changed_value = poll_value != store.data['poll_seconds']
    store.data['poll_seconds'] = poll_value
    store.save()
    if poll_changed_value:
        poll_changed.set()
    cloud.config = store.data['cloud']
    if probe:
        cloud.token, cloud.expires = probe.token, probe.expires
    elif cloud_changed:
        cloud.token, cloud.expires = None, 0
    if cloud_changed and store.data['selected'] and cloud_candidate['username']:
        threading.Thread(target=refresh_after_change, args=('Cloud account updated',), daemon=True).start()
    if admin_changed:
        sessions.clear()
        if admin_candidate['enabled']:
            return redirect('./login')
    return redirect('./?cloud=open&saved_cloud=1')

def mqtt_modal(error='', entered=None):
    m = store.data['mqtt']
    entered = entered or {}
    mode = entered.get('mqtt_mode', m.get('mode', 'auto'))
    checked = request.args.get('mqtt') == 'open' or getattr(g, 'mqtt_form', None) is not None
    service = supervisor_mqtt() if checked else None
    available = bool(service) if checked else bool(m.get('host') and m.get('mode') == 'auto')
    if m.get('mode') == 'manual' and broker.connected:
        status = 'Connected using manual broker settings.'
    elif m.get('mode') == 'auto' and broker.connected:
        status = 'Connected using automatic broker settings.'
    elif mode == 'auto' and available:
        status = 'Automatic broker details are available. Select Test connection, then Save MQTT settings.'
    elif mode == 'auto':
        status = 'Home Assistant did not share automatic broker details. ' + (state.get('mqtt_service_error') or 'Select Test connection to check again.')
    else:
        status = 'Manual broker settings are saved.' if m.get('host') else 'Enter manual broker details or choose Automatic.'
    checked_auto = 'checked' if mode == 'auto' else ''
    checked_manual = 'checked' if mode == 'manual' else ''
    tls_no = 'selected' if not m['tls'] else ''
    tls_yes = 'selected' if m['tls'] else ''
    manual = field('mqtt_host', 'Broker address', entered.get('mqtt_host', m['host'] if mode == 'manual' else ''), hint='The address supplied by your MQTT broker.') + field('mqtt_port', 'Broker port', entered.get('mqtt_port', m['port'] if mode == 'manual' else 1883), typ='number') + field('mqtt_username', 'Broker username', entered.get('mqtt_username', m['username'] if mode == 'manual' else '')) + field('mqtt_password', 'Broker password', typ='password', hint='Leave blank to keep a saved manual password.') + f'<label>Secure MQTT connection<select name="mqtt_tls"><option value="no" {tls_no}>No</option><option value="yes" {tls_yes}>Yes</option></select></label>'
    modal_class = 'modal-overlay is-open' if error or getattr(g, 'mqtt_result', '') or request.args.get('mqtt') == 'open' else 'modal-overlay'
    body = f'<div id="mqtt-setup" class="{modal_class}" role="dialog" aria-modal="true" aria-labelledby="mqtt-dialog-title"><div class="modal-card"><div class="modal-header"><div><h3 id="mqtt-dialog-title">Set up MQTT</h3><p>Choose how SolarSynkV4 connects to your broker.</p></div><a class="modal-close" href="./" aria-label="Close MQTT setup">×</a></div>'
    body += '<p class="muted" role="status">' + esc(status) + '</p>'
    if request.args.get('saved_mqtt'):
        body += '<p class="notice" role="status">MQTT settings saved. The connection may take a moment.</p>'
    if getattr(g, 'mqtt_result', ''):
        body += '<p class="notice" role="status">' + esc(g.mqtt_result) + '</p>'
    if error:
        body += '<p class="error" role="alert">' + esc(error) + '</p>'
    body += f'<form method="post" action="{path("mqtt_save")}"><input type="hidden" name="csrf" value="{esc(csrf())}">'
    body += f'<div class="mode-choice"><input id="mqtt-auto" class="mode-radio" type="radio" name="mqtt_mode" value="auto" {checked_auto}><label for="mqtt-auto" class="mode-option"><strong>Automatic</strong><span>Use broker details shared by Home Assistant Supervisor. Available when a broker app provides the MQTT service.</span></label><input id="mqtt-manual" class="mode-radio" type="radio" name="mqtt_mode" value="manual" {checked_manual}><label for="mqtt-manual" class="mode-option"><strong>Manual</strong><span>Enter the details for an external or custom broker.</span></label><div class="manual-fields">{manual}</div></div>'
    body += '<details class="mqtt-advanced"><summary>Advanced</summary>' + field('prefix', 'MQTT topic prefix', entered.get('prefix', m['prefix']), required=True, hint='Usually leave this unchanged. Changing it may create new discovery topics.') + '</details>'
    body += '<div class="modal-actions"><a href="./">Cancel</a><button type="submit" name="action" value="test" class="btn btn-outline-light">Test connection</button><button type="submit" name="action" value="save">Save MQTT settings</button></div></form></div></div>'
    return body

@app.get('/mqtt')
def mqtt_page():
    return redirect('./?mqtt=open')

@app.post('/mqtt_save')
def mqtt_save():
    previous = store.data['mqtt']
    candidate = dict(previous)
    mode = request.form.get('mqtt_mode', '')
    try:
        if mode not in ('auto', 'manual'):
            raise ValueError('Choose Automatic or Manual.')
        candidate['mode'] = mode
        prefix = request.form.get('prefix', '').strip()
        if not prefix or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for char in prefix):
            raise ValueError('MQTT topic prefix may use letters, numbers, underscores and hyphens.')
        candidate['prefix'] = prefix
        if mode == 'auto':
            service = supervisor_mqtt()
            if not service:
                raise ValueError((state.get('mqtt_service_error') or 'Automatic broker details are unavailable.') + ' Keep Manual selected if your broker does not provide a Supervisor MQTT service.')
            candidate.update(service)
        else:
            candidate['host'] = request.form.get('mqtt_host', '').strip()
            candidate['port'] = int(request.form.get('mqtt_port', ''))
            candidate['username'] = request.form.get('mqtt_username', '').strip()
            candidate['tls'] = request.form.get('mqtt_tls') == 'yes'
            candidate['password'] = request.form.get('mqtt_password') or (previous.get('password', '') if previous.get('mode') == 'manual' else '')
            if not candidate['host'] or not 1 <= candidate['port'] <= 65535:
                raise ValueError('Enter a broker address and a port between 1 and 65535.')
    except (ValueError, TypeError) as exc:
        g.mqtt_error = str(exc) if mode == 'auto' else 'Check the broker address, port and topic prefix, then try again.'
        g.mqtt_form = request.form
        return index(), 400
    if request.form.get('action') == 'test':
        ok, result = test_connection(candidate)
        g.mqtt_form = request.form
        if ok:
            g.mqtt_result = result + ' Settings were not saved.'
        else:
            g.mqtt_error = result
        return index(), 200 if ok else 400
    if candidate != previous:
        broker.stop()
        store.data['mqtt'] = candidate
        store.save()
        broker.config = store.data['mqtt']
        broker.prefix = candidate['prefix']
        broker.start()
    return redirect('./?mqtt=open&saved_mqtt=1')

@app.post('/discover')
def find_devices():
    try:
        found, errors = discover(cloud)
        store.data['devices'], added = merge_devices(store.data.get('devices', []), found)
        store.save()
        state['devices'] = list(store.data['devices'])
        state['errors'] = errors
        state['discovery_message'] = f'List refreshed. {added} new inverter(s) added; {len(state["devices"])} saved.' + (' Some plants could not be read. Your saved list was kept.' if errors else '')
        activity_event('inverters', f'Refreshed the inverter list: {len(state["devices"])} saved, {added} new.', level='warning' if errors else 'info')
    except CloudError as exc:
        state['discovery_message'] = 'Could not refresh the list. Your saved inverters and selections were kept. ' + str(exc)
        activity_event('inverters', 'Could not refresh the inverter list. Saved selections were kept.', level='error')
    return redirect('./?inverters=open')

@app.get('/devices')
def devices():
    return redirect('./?inverters=open')

@app.post('/devices')
def save_devices():
    selected = list(dict.fromkeys(request.form.getlist('serial')))
    known = {device['sn'] for device in store.data.get('devices', [])}
    if not set(selected) <= known:
        abort(400)
    names = {}
    for serial in selected:
        try:
            names[serial] = validate_device_name(request.form.get('name_' + serial, ''))
        except ValueError as exc:
            g.inverter_error = f'Inverter {serial}: {exc}'
            g.inverter_form = request.form
            return index(), 400
    old = set(store.data['selected'])
    store.data['selected'] = selected
    store.data['names'].update(names)
    store.save()
    activity_event('inverters', f'Saved {len(selected)} selected inverter(s).')
    broker.republish()
    for serial in old - set(selected):
        broker.remove(serial)
    state['discovery_message'] = ''
    if selected and store.data['cloud']['username'] and set(selected) != old:
        threading.Thread(target=refresh_after_change, args=('Inverter selection updated',), daemon=True).start()
    return redirect('./?inverters=open&saved_inverters=1')

@app.get('/settings')
def settings_page():
    body = '<p>Change any available values below, then use the one Save changes button at the bottom. A pending value waits for cloud read-back and does not prove physical application.</p>'
    fields = ''
    for serial in store.data['selected']:
        available = broker.devices.get(serial, {}).get('settings', {})
        fields += f'<section class="card"><h3>{esc(store.data["names"].get(serial) or serial)}</h3>'
        if not available:
            fields += '<p>Settings have not loaded or this account cannot read them.</p>'
        for group, heading in (('battery', 'Battery'), ('mode', 'System mode and schedule'), ('aux', 'Aux Load'), ('advanced', 'Advanced')):
            controls = [(key, definition) for key, definition in CONTROLS.items() if definition[2] == group and key in available]
            if not controls:
                continue
            fields += f'<h4>{heading}</h4><div class="grid">'
            for key, (name, kind, _, low, high, unit) in controls:
                pending = settings.commands.get((serial, key))
                value = pending['value'] if pending else available[key]
                input_name = 'setting__' + serial + '__' + key
                if kind == 'switch':
                    is_on = str(value).lower() in ('true', '1', 'on')
                    control = f'<select name="{esc(input_name)}"><option value="ON" {"selected" if is_on else ""}>On</option><option value="OFF" {"selected" if not is_on else ""}>Off</option></select>'
                elif kind == 'time':
                    control = f'<input type="time" name="{esc(input_name)}" value="{esc(str(value)[:5])}">'
                else:
                    step = '1' if kind == 'integer' else 'any'
                    control = f'<input type="number" step="{step}" min="{esc(low)}" max="{esc(high)}" name="{esc(input_name)}" value="{esc(value)}">'
                fields += f'<div><label>{esc(name)}{control}</label><small>{esc(unit or "")}</small>'
                if pending:
                    fields += '<p class="alert alert-danger pending-warning" role="status">' + ('Waiting for cloud read-back.' if pending['status'] == 'sent' else 'Sending setting to Sunsynk...') + '</p>'
                fields += '</div>'
            fields += '</div>'
        fields += '</section>'
    if not store.data['selected']:
        fields = '<section class="card"><p>No inverters selected. <a href="./devices">Choose inverters</a> first.</p></section>'
    else:
        fields = form('change', fields, 'Save changes')
    if request.args.get('saved'):
        body += '<p class="notice">Changes requested. Check Activity for the result.</p>'
    return page('Inverter Settings', body + fields)
@app.post('/change')
def change():
    changes = []
    try:
        for serial in store.data['selected']:
            available = broker.devices.get(serial, {}).get('settings', {})
            for key in CONTROLS:
                name = 'setting__' + serial + '__' + key
                if name not in request.form or key not in available:
                    continue
                requested = validate(key, request.form[name])
                pending = settings.commands.get((serial, key))
                shown = pending['value'] if pending else available[key]
                try:
                    current = validate(key, shown)
                except (ValueError, TypeError):
                    current = shown
                if requested != current:
                    changes.append((serial, key, requested))
    except (ValueError, TypeError) as exc:
        return page('Inverter Settings not saved', '<p><a href="./settings">Back to Inverter Settings</a></p>', str(exc)), 400
    for serial, key, value in changes:
        settings.request(serial, key, value)
    return redirect('./settings?saved=' + str(len(changes)))
@app.get('/activity')
def activity():
    events = list(store.data.get('activity', []))
    for (serial, key), command in list(settings.commands.items()):
        events.append({'at': command.get('started', time.time()), 'category': 'settings', 'serial': serial, 'message': key + ': waiting for cloud confirmation', 'level': 'warning'})
    for item in store.data.get('history', []):
        events.append({'at': item.get('at', 0), 'category': 'settings', 'serial': item.get('serial', ''), 'message': str(item.get('setting', 'Setting')) + ': ' + str(item.get('status', '')), 'level': 'error' if item.get('status') in ('rejected', 'unconfirmed') else 'info'})
    events.sort(key=lambda item: item.get('at', 0), reverse=True)
    rows = ''
    for item in events[:200]:
        at = datetime.fromtimestamp(item.get('at', 0), timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        rows += f'<tr><td>{esc(at)}</td><td>{esc(item.get("category", ""))}</td><td>{esc(item.get("serial") or "—")}</td><td>{esc(item.get("message", ""))}</td><td>{esc(item.get("level", "info").title())}</td></tr>'
    if not rows:
        rows = '<tr><td colspan="5">No activity yet. Connect MQTT or fetch an inverter to see connection and publishing events here.</td></tr>'
    intro = '<div class="card"><h3>Connection and publishing history</h3><p>Cloud fetches, MQTT discovery and telemetry sends, and setting changes appear here. “Queued” means the MQTT client accepted the message; check the MQTT integration for Home Assistant registration.</p><div class="table-responsive"><table class="table table-dark table-hover"><thead><tr><th>Time</th><th>Source</th><th>Inverter</th><th>Event</th><th>Level</th></tr></thead><tbody>'
    return page('Activity', intro + rows + '</tbody></table></div></div>')

@app.get('/help')
def help_page():
    return page('Help', '<div class="card"><h3>Getting started</h3><p>Open Setup → Setup MQTT to connect the broker, then Setup → Cloud Setup to sign in. Open Setup → Select Inverters to refresh the saved list and choose the devices you want. MQTT is how this app sends devices to Home Assistant.</p><h3>Changing settings</h3><p>Open Inverter Settings or use a discovered Home Assistant control. A pending value waits for cloud read-back. Developer Tools “Set state” does not send a command.</p><h3>Connection problems</h3><p>Check Setup → Cloud Setup for cloud credentials and Setup → Setup MQTT for broker status. Cloud telemetry can be older than the last API response.</p></div>')
ph = PasswordHasher()
@app.get('/login')
def login():
    if not store.data['admin']['enabled']:
        return redirect('./')
    body = form('login', field('username','Username',required=True)+field('password','Password',typ='password',required=True)+'<label><input type="checkbox" name="remember" value="yes"> Remember me</label>', 'Log in')
    return page('Log in', body)
@app.post('/login')
def login_post():
    ip = request.remote_addr or 'unknown'; now = time.time()
    attempts[ip] = [x for x in attempts[ip] if now-x < 300]
    if len(attempts[ip]) >= 5:
        return page('Log in', '<p>Wait five minutes before trying again.</p>'), 429
    admin = store.data['admin']
    try:
        valid = request.form.get('username') == admin['username'] and ph.verify(admin['hash'], request.form.get('password',''))
    except Exception:
        valid = False
    if not valid:
        attempts[ip].append(now)
        return page('Log in', '<p>Username or password is incorrect. <a href="./login">Try again</a>.</p>'), 401
    token = secrets.token_urlsafe(32); remember = request.form.get('remember') == 'yes'; age = 30*86400 if remember else 12*3600
    sessions[token] = {'expires': now+age, 'csrf': secrets.token_urlsafe(24)}
    response = redirect('./'); response.set_cookie('ssv4_session', token, max_age=age, httponly=True, secure=bool(os.environ.get('SUPERVISOR_TOKEN')), samesite='Lax', path='/')
    return response
@app.post('/logout')
def logout():
    sessions.pop(request.cookies.get('ssv4_session'), None)
    response = redirect('./login'); response.delete_cookie('ssv4_session', path='/')
    return response
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8099, threaded=True)
