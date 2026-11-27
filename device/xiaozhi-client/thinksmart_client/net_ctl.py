"""Local Wi-Fi (NetworkManager nmcli) and AirPlay (thinksmart-airplay user unit) controls.

Runs Blocking CLI calls in a worker thread off the asyncio event loop. Mirrors thinksmart-kiosk.py's
nmcli escaping and saved-profile rollback so Settings -> Network works identically whether called
through WebKit's message handler or over 127.0.0.1 HTTP.
"""
import os
import subprocess
import threading
import time

HANDSHAKE_GIVE_UP = 5.0
STAGES = {
    'scanning': 'scan',
    'authenticating': 'join',
    'associating': 'join',
    'associated': 'join',
    '4way_handshake': 'auth',
    'group_handshake': 'auth',
    'completed': 'ip',
}
_SUPPLICANT_PATH = ''


def _nmcli(*args, timeout=10):
    return subprocess.run(['nmcli', *args], capture_output=True, text=True, timeout=timeout)


def nmcli_fields(line: str) -> list[str]:
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    if '\\' not in line:
        return line.split(':')
    fields, field = [], []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '\\' and i + 1 < len(line) and line[i + 1] in ('\\', ':'):
            i += 1
            field.append(line[i])
        elif ch == ':':
            fields.append(''.join(field))
            field.clear()
        else:
            field.append(ch)
        i += 1
    fields.append(''.join(field))
    return fields


def _unescape_nmcli_single(val: str) -> str:
    out = []
    i = 0
    while i < len(val):
        if val[i] == '\\' and i + 1 < len(val):
            out.append(val[i + 1])
            i += 2
        else:
            out.append(val[i])
            i += 1
    return ''.join(out)


def _connection_uuids(active: bool = False) -> dict[str, str]:
    out = _nmcli('-t', '-f', 'UUID,TYPE', 'connection', 'show', *(['--active'] if active else [])).stdout
    return {f[0]: f[1] for f in map(nmcli_fields, out.splitlines()) if len(f) >= 2}


def _nmcli_multi_ssid(wifi_uuids: list[str]) -> list[str]:
    if not wifi_uuids:
        return []
    raw = _nmcli('-g', 'connection.uuid,802-11-wireless.ssid', 'connection', 'show', 'uuid', *wifi_uuids).stdout
    uuid_set = set(wifi_uuids)
    by_uuid = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        u = lines[i]
        if u in uuid_set and u not in by_uuid:
            nxt = lines[i + 1] if i + 1 < len(lines) else ''
            if nxt in uuid_set:
                by_uuid[u] = ''
                i += 1
            else:
                by_uuid[u] = nxt
                i += 2
        else:
            i += 1
    return [by_uuid.get(u, '') for u in wifi_uuids]


def _active_wifi_ssid() -> str:
    act = [u for u, kind in _connection_uuids(active=True).items() if kind == '802-11-wireless']
    if not act:
        return ''
    raw = _nmcli_multi_ssid(act[:1])
    return _unescape_nmcli_single(raw[0]) if raw and raw[0] else ''


def _wifi_profiles(uuids=None) -> dict[str, str]:
    wifi_uuids = [
        u for u, kind in (uuids if uuids is not None else _connection_uuids()).items()
        if kind == '802-11-wireless'
    ]
    if not wifi_uuids:
        return {}
    out = _nmcli_multi_ssid(wifi_uuids)
    return {_unescape_nmcli_single(s): u for u, s in zip(wifi_uuids, out) if s}


def wifi_status_sync() -> dict:
    try:
        out = _nmcli('-t', '-f', 'ACTIVE,SSID,SIGNAL', 'dev', 'wifi', 'list', '--rescan', 'no', timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {'ok': False}
    for f in map(nmcli_fields, out.splitlines()):
        if len(f) >= 3 and f[0] == 'yes':
            ssid = f[1] or _active_wifi_ssid()
            return {'ok': True, 'ssid': ssid, 'signal': int(f[2]) if f[2].isdigit() else 0}
    ssid = _active_wifi_ssid()
    return {'ok': True, 'ssid': ssid, 'signal': 0}


def wifi_scan_sync(rescan: bool = True) -> dict:
    try:
        out = _nmcli(
            '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list',
            '--rescan', 'yes' if rescan else 'no',
            timeout=25 if rescan else 6,
        ).stdout
        saved = _wifi_profiles()
    except (OSError, subprocess.SubprocessError):
        return {'ok': False}
    best = {}
    for line in out.splitlines():
        f = nmcli_fields(line)
        if len(f) < 4:
            continue
        ssid = f[1] or (_active_wifi_ssid() if f[0] == '*' else '')
        if not ssid:
            continue
        n = {
            'ssid': ssid,
            'signal': int(f[2]) if f[2].isdigit() else 0,
            'secure': f[3] not in ('', '--'),
            'active': f[0] == '*',
            'saved': ssid in saved,
        }
        old = best.get(ssid)
        if old is None or n['active'] or (not old['active'] and n['signal'] > old['signal']):
            best[ssid] = n
    nets = sorted(best.values(), key=lambda x: (not x['active'], not x['saved'], -x['signal']))
    return {'ok': True, 'networks': nets}


def _supplicant_path(refresh: bool = False) -> str:
    global _SUPPLICANT_PATH
    if _SUPPLICANT_PATH and not refresh:
        return _SUPPLICANT_PATH
    try:
        out = subprocess.run(
            ['busctl', 'call', 'fi.w1.wpa_supplicant1', '/fi/w1/wpa_supplicant1',
             'fi.w1.wpa_supplicant1', 'GetInterface', 's', 'wlan0'],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    _SUPPLICANT_PATH = out.split('"')[1] if out.count('"') >= 2 else ''
    return _SUPPLICANT_PATH


def _supplicant_state(path: str) -> str:
    try:
        out = subprocess.run(
            ['busctl', 'get-property', 'fi.w1.wpa_supplicant1', path,
             'fi.w1.wpa_supplicant1.Interface', 'State'],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.split('"')[1] if out.count('"') >= 2 else ''


def _run_nmcli_watched(cmd: list[str], progress=None) -> tuple[bool, str]:
    path = _supplicant_path()
    p = subprocess.Popen(['nmcli', *cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 50
    stage = handshake_since = None
    left_old = False
    while p.poll() is None:
        state = _supplicant_state(path) if path else ''
        if not state and path:
            path = _supplicant_path(refresh=True)
            state = _supplicant_state(path) if path else ''
        left_old = left_old or (state not in ('', 'completed'))
        new_stage = STAGES.get(state) if left_old else None
        if new_stage and new_stage != stage:
            stage = new_stage
            if progress:
                progress(stage)
        if state in ('4way_handshake', 'group_handshake'):
            handshake_since = handshake_since or time.monotonic()
            if time.monotonic() - handshake_since > HANDSHAKE_GIVE_UP:
                break
        else:
            handshake_since = None
        if time.monotonic() > deadline:
            break
        time.sleep(0.25)
    if p.poll() is None:
        wrong = handshake_since is not None
        p.terminate()
        try:
            p.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
        try:
            _nmcli('device', 'disconnect', 'wlan0')
        except (OSError, subprocess.SubprocessError):
            pass
        return False, 'Secrets were required' if wrong else 'Timeout'
    out, err = p.communicate()
    return p.returncode == 0, (err or out).strip()


def _reconnect_later(uuid: str):
    def work():
        try:
            _nmcli('connection', 'up', 'uuid', uuid, timeout=45)
        except (OSError, subprocess.SubprocessError):
            pass
    threading.Thread(target=work, daemon=True).start()


def wifi_connect_sync(msg: dict, progress=None) -> dict:
    ssid = str(msg.get('ssid') or '')
    password = str(msg.get('password') or '')
    hidden = bool(msg.get('hidden'))
    if not 0 < len(ssid.encode('utf-8')) <= 32 or (password and not 8 <= len(password) <= 63):
        return {'ok': False, 'reason': 'invalid'}
    try:
        before = _connection_uuids()
        previous = next((u for u, t in _connection_uuids(active=True).items() if t == '802-11-wireless'), None)
        saved = _wifi_profiles(before).get(ssid)
        old_psk = None
        old_km = None
        was_open = False
        if saved and (password or hidden):
            km = _nmcli('-g', '802-11-wireless-security.key-mgmt', 'connection', 'show', 'uuid', saved)
            if km.returncode == 0 and not km.stdout.strip():
                was_open = True
            elif km.returncode == 0:
                old_km = km.stdout.strip()
                r = _nmcli('-s', '-g', '802-11-wireless-security.psk', 'connection', 'show', 'uuid', saved)
                raw_psk = r.stdout.rstrip('\r\n') if r.returncode == 0 else ''
                old_psk = _unescape_nmcli_single(raw_psk) if raw_psk else None
    except (OSError, subprocess.SubprocessError):
        return {'ok': False, 'reason': 'failed'}
    if saved and not password and not hidden:
        cmd = ['--wait', '40', 'connection', 'up', 'uuid', saved]
    else:
        cmd = ['--wait', '40', 'dev', 'wifi', 'connect', ssid]
        if password:
            cmd += ['password', password]
        if hidden:
            cmd += ['hidden', 'yes']
    ok, err = _run_nmcli_watched(cmd, progress)
    if ok:
        return {'ok': True}
    reason = (
        'password' if 'Secrets were required' in err or 'no-secrets' in err or 'password' in err.lower() else
        'notfound' if 'No network with SSID' in err else
        'timeout' if 'Timeout' in err or 'timed out' in err else 'failed'
    )
    try:
        new_uuids = list(set(_connection_uuids()) - set(before))
        if new_uuids:
            _nmcli('connection', 'delete', 'uuid', *new_uuids)
        if old_km and old_psk:
            _nmcli('connection', 'modify', 'uuid', saved, 'wifi-sec.key-mgmt', old_km, 'wifi-sec.psk', old_psk)
        elif old_psk:
            _nmcli('connection', 'modify', 'uuid', saved, 'wifi-sec.psk', old_psk)
        elif was_open:
            _nmcli('connection', 'modify', 'uuid', saved, 'remove', '802-11-wireless-security')
    except (OSError, subprocess.SubprocessError):
        pass
    if previous and (previous != saved or password or hidden):
        _reconnect_later(previous)
    return {'ok': False, 'reason': reason, 'detail': err.splitlines()[-1][:120] if err else ''}


def wifi_forget_sync(msg: dict) -> dict:
    ssid = str(msg.get('ssid') or '')
    if not ssid:
        return {'ok': False}
    try:
        wifi_uuids = [u for u, kind in _connection_uuids().items() if kind == '802-11-wireless']
        if not wifi_uuids:
            return {'ok': True}
        out = _nmcli_multi_ssid(wifi_uuids)
        del_uuids = [u for u, s in zip(wifi_uuids, out) if s and _unescape_nmcli_single(s) == ssid]
        if del_uuids and _nmcli('connection', 'delete', 'uuid', *del_uuids).returncode != 0:
            return {'ok': False}
    except (OSError, subprocess.SubprocessError):
        return {'ok': False}
    return {'ok': True}


# ---- AirPlay (ubuntu user systemd unit: thinksmart-airplay.service) ----

AIRPLAY_UNIT = 'thinksmart-airplay.service'


def _user_bus_env() -> tuple[str, dict]:
    uid = os.getuid()
    rt = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{uid}'
    bus = os.environ.get('DBUS_SESSION_BUS_ADDRESS') or f'unix:path={rt}/bus'
    env = {**os.environ, 'XDG_RUNTIME_DIR': rt, 'DBUS_SESSION_BUS_ADDRESS': bus}
    return rt, env


def airplay_status_sync() -> dict:
    rt, env = _user_bus_env()
    available = os.path.isfile('/usr/local/bin/uxplay') or os.path.isfile('/usr/local/bin/thinksmart-airplay.py')
    active = False
    unit_enabled = False
    try:
        r_show = subprocess.run(
            ['systemctl', '--user', 'show', '-p', 'ActiveState', '-p', 'UnitFileState', '--value', AIRPLAY_UNIT],
            capture_output=True, text=True, timeout=3, env=env,
        )
        lines = [ln.strip() for ln in (r_show.stdout or '').splitlines() if ln.strip()]
        if r_show.returncode == 0 and len(lines) >= 2:
            active = lines[0] == 'active'
            unit_enabled = active or (lines[1] not in ('disabled', 'masked', 'not-found', ''))
        else:
            r_act = subprocess.run(
                ['systemctl', '--user', 'is-active', AIRPLAY_UNIT],
                capture_output=True, text=True, timeout=3, env=env,
            )
            active = r_act.returncode == 0
            r_en = subprocess.run(
                ['systemctl', '--user', 'is-enabled', AIRPLAY_UNIT],
                capture_output=True, text=True, timeout=3, env=env,
            )
            en_txt = (r_en.stdout or '').strip()
            unit_enabled = active or (bool(en_txt) and en_txt not in ('disabled', 'masked', 'not-found'))
    except (OSError, subprocess.SubprocessError):
        pass
    mirroring = os.path.exists(os.path.join(rt, 'uxplay-mirroring-active'))
    streaming = os.path.exists(os.path.join(rt, 'uxplay-audio-active'))
    return {
        'ok': True,
        'available': available,
        'enabled': active,
        'unit_enabled': unit_enabled,
        'mirroring': active and mirroring,
        'streaming': active and streaming,
        'name': 'ThinkSmart View',
    }


def set_airplay_sync(enabled: bool) -> dict:
    rt, env = _user_bus_env()
    want = bool(enabled)
    try:
        if want:
            subprocess.run(['systemctl', '--user', 'unmask', AIRPLAY_UNIT],
                           capture_output=True, text=True, timeout=4, env=env)
            r = subprocess.run(['systemctl', '--user', 'enable', '--now', AIRPLAY_UNIT],
                               capture_output=True, text=True, timeout=10, env=env)
        else:
            r = subprocess.run(['systemctl', '--user', 'disable', '--now', AIRPLAY_UNIT],
                               capture_output=True, text=True, timeout=10, env=env)
            for flag in ('uxplay-mirroring-active', 'uxplay-audio-active'):
                try:
                    os.unlink(os.path.join(rt, flag))
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
    except (OSError, subprocess.SubprocessError) as e:
        st = airplay_status_sync()
        st.update(ok=False, error=str(e)[:120])
        return st
    st = airplay_status_sync()
    if r.returncode != 0:
        err = (r.stderr or r.stdout or 'AirPlay 服务操作失败').strip()
        st.update(ok=False, error=err.splitlines()[-1][:120] if err else 'AirPlay 服务操作失败')
    return st
