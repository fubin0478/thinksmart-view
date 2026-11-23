#!/opt/thinksmart-addons/venv/bin/python
"""联想 View 管理后台 — thinksmart-admin（2026-11-20，Mac 上的 Claude，）。

一个页面管全部：可视对讲、留言板、倒数日、学习陪伴、拍照讲题这几页本来就由 thinksmart-intercom 提供，
后台页面把它们嵌进来。本服务只补上它们原来没有的两样：
  * 本地歌曲：列出本机（/home/ubuntu/Music 和小智的音乐文件夹）和 U 盘里的歌；上传、下载、试听、改名、
    移动、删除（7 天内可在「最近删除」里恢复）；在家里大屏上播放。
  * Clash（mihomo）：模式、节点选择和测速、连接、规则集；完整面板 MetaCubeXD 也从这里打开。
    mihomo 的密钥只在本服务里，由本服务代为加到请求上，浏览器拿不到。

只监听 127.0.0.1:8097，入口是 thinksmart-intercom 的 /admin/ 转发（和对讲页同一个网址）。访问策略：
和远程可视一样只有两种进法——连上指定的 VPS 节点（https://203.0.113.10:18443/admin/，VPS 只对节点开放），
或者家里局域网（http://<设备局域网 IP>:18091/admin/）；intercom 只放行隧道和私网来源，本服务再按 X-TS-Peer 核一遍。
密码可选：默认不设（和远程可视一样）；用 /usr/local/sbin/thinksmart-set-admin-password 设了以后才要登录，
只存 scrypt 哈希（输入不回显，不进日志）；输入空行就取消密码。
"""
import argparse
import asyncio
import base64
import contextlib
import errno
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:
    if '/usr/lib/python3/dist-packages' not in sys.path:
        sys.path.append('/usr/lib/python3/dist-packages')
    try:
        import yaml as _yaml
    except ImportError:
        _yaml = None

import aiohttp
from aiohttp import web, WSMsgType
from yarl import URL

logging.basicConfig(level=logging.INFO, format='%(levelname)s admin: %(message)s')
log = logging.getLogger('admin')

_URL_SECRET_RE = re.compile(r'((?:https?|wss?)://)(?:[^/\s@]+@)?([^\s/\'"<>)\]]+)(?:/[^\s\'"<>)\]]*)?', re.I)
_AUTH_TOKEN_RE = re.compile(r'(Bearer\s+)[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9.*_-]{6,}', re.I)


def _redact_err(text: object, *secrets: str) -> str:
    s = str(text or '')
    for sec in secrets:
        if sec and len(sec) >= 6 and sec not in ('none', 'local'):
            s = s.replace(sec, '***')
    s = _URL_SECRET_RE.sub(r'\1\2/...', s)
    s = _AUTH_TOKEN_RE.sub(lambda m: (m.group(1) + '***') if m.group(1) else 'sk-***', s)
    return s


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


PORT = int(os.environ.get('ADMIN_PORT', '8097'))
BIND = os.environ.get('ADMIN_BIND', '127.0.0.1')
STATE_DIR = _env_path('ADMIN_STATE', '/var/lib/thinksmart-admin')
UI_DIR = _env_path('ADMIN_UI_DIR', '/usr/local/share/thinksmart-admin')
MUSIC_LOCAL = _env_path('ADMIN_MUSIC_LOCAL', '/home/ubuntu/Music')
MUSIC_HUB = _env_path('ADMIN_MUSIC_HUB', '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/music')
MUSIC_USB = _env_path('ADMIN_MUSIC_USB', '/mnt/usb')
USB_ASSUME_MOUNTED = os.environ.get('ADMIN_USB_ASSUME_MOUNTED') == '1'     # tests only
MIHOMO_API = os.environ.get('ADMIN_MIHOMO_API', 'http://127.0.0.1:9090').rstrip('/')
MIHOMO_CFG = _env_path('ADMIN_MIHOMO_CFG', '/etc/mihomo/config.yaml')
# /etc/mihomo 只有 root 能进，systemd 把里面的 ui 只读挂到这里给本服务（见 thinksmart-admin.service）
MIHOMO_UI = _env_path('ADMIN_MIHOMO_UI', '/run/thinksmart-admin/clash-ui')
QQ_API = os.environ.get('ADMIN_QQ_API', 'http://127.0.0.1:8091').rstrip('/')
HUB_DIR = _env_path('ADMIN_HUB_DIR', '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
HUB_PY = _env_path('ADMIN_HUB_PY', '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python')
HUB_API = os.environ.get('ADMIN_HUB_API', 'http://127.0.0.1:16688').rstrip('/')
HOME_STATE = _env_path('ADMIN_HOME_STATE', '/var/lib/thinksmart-home')
HOME_API = os.environ.get('ADMIN_HOME_API', 'http://127.0.0.1:8095').rstrip('/')
STUDY_STATE = _env_path('ADMIN_STUDY_STATE', '/var/lib/thinksmart-study')
STUDY_API = os.environ.get('ADMIN_STUDY_API', 'http://127.0.0.1:8093').rstrip('/')
TUTOR_STATE = _env_path('ADMIN_TUTOR_STATE', '/var/lib/thinksmart-tutor')
TUTOR_API = os.environ.get('ADMIN_TUTOR_API', 'http://127.0.0.1:8096').rstrip('/')
MONITOR_STATE = _env_path('ADMIN_MONITOR_STATE', '/var/lib/thinksmart-monitor')
MONITOR_SHARE = _env_path('ADMIN_MONITOR_SHARE', '/usr/local/share/thinksmart-monitor')
MONITOR_API = os.environ.get('ADMIN_MONITOR_API', 'http://127.0.0.1:8092').rstrip('/')
CLIENT_API = os.environ.get('ADMIN_CLIENT_API', 'http://127.0.0.1:8090').rstrip('/')
INTERCOM_API = os.environ.get('ADMIN_INTERCOM_API', 'http://127.0.0.1:18091').rstrip('/')
CLIENT_CFG = _env_path('ADMIN_CLIENT_CFG', '/home/ubuntu/.config/thinksmart-client/config.json')
CLIENT_SOUNDS_DIR = _env_path('ADMIN_CLIENT_SOUNDS_DIR', '/opt/thinksmart/client/current/web/sounds')
QQ_CFG_DIR = _env_path('ADMIN_QQ_CFG_DIR', '/home/ubuntu/.config/thinksmart-qqmusic')
UPDATE_STATE = _env_path('ADMIN_UPDATE_STATE', '/var/lib/thinksmart-update')
METASO_TEST_URL = os.environ.get('ADMIN_METASO_URL', 'https://metaso.cn/api/v1/search')
AMAP_TEST_URL = os.environ.get('ADMIN_AMAP_URL', 'https://restapi.amap.com/v3/geocode/geo')
SILICONFLOW_TTS_URL = os.environ.get('ADMIN_SILICONFLOW_TTS_URL', 'https://api.siliconflow.cn/v1/audio/speech')
NEWSNOW_API_URL = os.environ.get('ADMIN_NEWSNOW_URL', 'https://newsnow.busiyi.world/api/s?id=')
OPEN_METEO_GEO_URL = os.environ.get('ADMIN_OPEN_METEO_GEO_URL', 'https://geocoding-api.open-meteo.com/v1/search')
OPEN_METEO_FORECAST_URL = os.environ.get('ADMIN_OPEN_METEO_FORECAST_URL', 'https://api.open-meteo.com/v1/forecast')
CHINANEWS_DEFAULT_RSS = os.environ.get('ADMIN_CHINANEWS_RSS_URL', 'https://www.chinanews.com.cn/rss/scroll-news.xml')
QWEATHER_PROTO = 'https'
RESERVE_SYSTEM = int(os.environ.get('ADMIN_RESERVE_BYTES', str(1024 ** 3)))   # 系统盘至少留 1 GB
RESERVE_USB = 32 * 1024 ** 2
MAX_UPLOAD = 1024 ** 3
TRASH_DIR = '.thinksmart-trash'          # 放在各自的歌曲文件夹里（同一个挂载点才能直接改名移过去）
INCOMING_DIR = '.incoming'               # 正在上传、导入的临时文件放这里：歌曲文件夹本身只在歌真正进来或删掉时才变（大屏据此刷新歌单）
TRASH_DAYS = 7
# 和 thinksmart-qqmusic 的 AUDIO_EXTS 一致：传上来的歌大屏都能播
AUDIO_EXTS = ('.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.wma')
MIME = {'.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
        '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.wma': 'audio/x-ms-wma'}
ROOT_LABEL = {'local': '本机', 'hub': '本机', 'usb': 'U盘'}
COOKIE = 'ts_admin'
COOKIE_WAN = 'ts_wan'
WAN_PUBLIC_URL = os.environ.get('ADMIN_WAN_URL', 'https://panel.example.com/admin/')
LONG_SESSION = 90 * 86400
SHORT_SESSION = 12 * 3600
MIN_PASSWORD = 4
NO_STORE = {'Cache-Control': 'no-store'}

PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob: data:; connect-src 'self' ws: wss:; frame-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# 完整面板（MetaCubeXD）：分组图标是外部图片；别的外部请求一律不放行
CLASH_UI_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self' ws: wss:; "
                "frame-ancestors 'self'; base-uri 'self'; form-action 'self'; worker-src 'self' blob:")


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.msg = msg
        self.status = status


def jok(**kw):
    return web.json_response({'ok': True, **kw}, headers=NO_STORE)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode('ascii').rstrip('=')


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def fmt_size(n: float) -> str:
    n = float(max(0, n))
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


def write_private_json(path: Path, data) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError(f'Refusing symlink path: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if path.is_symlink():
            raise OSError(f'Refusing symlink target: {path}')
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ============================== 登录 ==============================
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 14, 8, 1


def clean_username(raw) -> str:
    u = str(raw or '').strip()
    if not u or len(u) > 64 or any(ord(c) < 32 or c.isspace() or c in ':/\\' for c in u):
        raise ApiError('管理账号应为 1～64 位可见字符（不能含空格、冒号或斜杠）')
    return u


def hash_password(pw: str, username: str = 'admin') -> dict:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode('utf-8'), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        maxmem=64 * 1024 * 1024, dklen=32)
    return {'v': 1, 'algo': 'scrypt', 'n': SCRYPT_N, 'r': SCRYPT_R, 'p': SCRYPT_P,
            'username': clean_username(username),
            'salt': _b64e(salt), 'hash': _b64e(dk), 'set_at': int(time.time())}


def verify_password(pw: str, rec) -> bool:
    try:
        if not isinstance(rec, dict) or rec.get('algo') != 'scrypt':
            return False
        n, r, p = int(rec['n']), int(rec['r']), int(rec['p'])
        if not (1 << 10 <= n <= 1 << 17 and 1 <= r <= 16 and 1 <= p <= 4):
            return False
        want = _b64d(rec['hash'])
        got = hashlib.scrypt(pw.encode('utf-8'), salt=_b64d(rec['salt']), n=n, r=r, p=p,
                             maxmem=64 * 1024 * 1024, dklen=len(want))
        return hmac.compare_digest(got, want)
    except (KeyError, ValueError, TypeError):
        return False


def _file_sig(p: Path):
    try:
        st = p.stat()
        return (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class Auth:
    """auth.json = 账号与密码哈希；sessions.json = 已登录设备（只存 token 的 sha256）。两份都可能被设置密码的命令改掉，用时按 mtime 重读。"""

    def __init__(self, state_dir: Path):
        self.auth_path = state_dir / 'auth.json'
        self.sess_path = state_dir / 'sessions.json'
        self._lock = threading.RLock()
        self._sess = {}
        self._sig = 'unset'
        self._rec = (None, None)

    def record(self):
        """密码是可选的：没设时和远程可视一样只靠网络限制（VPS 节点或局域网）；设了就要登录。"""
        with self._lock:
            sig = _file_sig(self.auth_path)
            if sig != self._rec[0]:
                rec = read_json(self.auth_path, None) if sig else None
                self._rec = (sig, rec if isinstance(rec, dict) and rec.get('hash') else None)
            return self._rec[1]

    def username(self) -> str:
        rec = self.record()
        u = str((rec or {}).get('username') or '').strip()
        return u or 'admin'

    def _load(self):
        with self._lock:
            sig = _file_sig(self.sess_path)
            if sig != self._sig:
                data = read_json(self.sess_path, {})
                self._sess = data if isinstance(data, dict) else {}
                self._sig = sig

    def _save(self):
        with self._lock:
            write_private_json(self.sess_path, self._sess)
            self._sig = _file_sig(self.sess_path)

    def create(self, remember: bool, ua: str):
        with self._lock:
            self._load()
            now = int(time.time())
            token = secrets.token_urlsafe(32)
            ttl = LONG_SESSION if remember else SHORT_SESSION
            self._sess[hashlib.sha256(token.encode()).hexdigest()] = {
                'created': now, 'seen': now, 'ttl': ttl, 'ua': (ua or '')[:160]}
            if len(self._sess) > 30:
                for k, _v in sorted(self._sess.items(), key=lambda kv: kv[1].get('seen', 0))[:len(self._sess) - 30]:
                    self._sess.pop(k, None)
            self._save()
            return token, ttl

    def check(self, token):
        if not token or not isinstance(token, str) or len(token) > 200:
            return None
        with self._lock:
            self._load()
            key = hashlib.sha256(token.encode()).hexdigest()
            s = self._sess.get(key)
            if not isinstance(s, dict):
                return None
            now = int(time.time())
            try:
                seen, ttl = int(s.get('seen', 0)), int(s.get('ttl', SHORT_SESSION))
            except (TypeError, ValueError):
                return None
            if now - seen > ttl:
                self._sess.pop(key, None)
                with contextlib.suppress(OSError):
                    self._save()
                return None
            if now - seen > 600:
                s['seen'] = now
                with contextlib.suppress(OSError):
                    self._save()
            return dict(s)

    def revoke(self, token):
        with self._lock:
            self._load()
            if token and self._sess.pop(hashlib.sha256(token.encode()).hexdigest(), None) is not None:
                self._save()

    def revoke_all(self, keep_token=None):
        with self._lock:
            self._load()
            keep = hashlib.sha256(keep_token.encode()).hexdigest() if keep_token else None
            self._sess = {k: v for k, v in self._sess.items() if k == keep}
            self._save()

    def clear_all(self):
        with self._lock:
            for p in (self.sess_path, self.auth_path):
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
            self._sess = {}
            self._sig = 'unset'
            self._rec = (None, None)

    def enable_password(self, rec: dict, ua: str):
        with self._lock:
            write_private_json(self.sess_path, {})
            self._sess = {}
            self._sig = _file_sig(self.sess_path)
            write_private_json(self.auth_path, rec)
            self._rec = (_file_sig(self.auth_path), rec)
            return self.create(True, ua)

    def set_password_record(self, rec: dict, keep_token=None):
        with self._lock:
            self.revoke_all(keep_token=keep_token)
            write_private_json(self.auth_path, rec)
            self._rec = (_file_sig(self.auth_path), rec)

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._sess)


class Limiter:
    """输错密码的限速：同一来源 10 分钟内错 5 次先停一停；所有来源合计错 30 次也停。"""
    WINDOW = 600
    PER_KEY = 5
    GLOBAL = 30

    def __init__(self):
        self.keys = {}
        self.glob = []

    def wait(self, key: str) -> int:
        now = time.time()
        self.glob = [t for t in self.glob if now - t < self.WINDOW]
        lst = [t for t in self.keys.get(key, []) if now - t < self.WINDOW]
        if lst:
            self.keys[key] = lst
        else:
            self.keys.pop(key, None)
        w = 0.0
        if len(lst) >= self.PER_KEY:
            w = max(w, lst[-self.PER_KEY] + self.WINDOW - now)
        if len(self.glob) >= self.GLOBAL:
            w = max(w, self.glob[-self.GLOBAL] + self.WINDOW - now)
        return int(w) + 1 if w > 0 else 0

    def fail(self, key: str):
        now = time.time()
        self.keys.setdefault(key, []).append(now)
        self.glob.append(now)
        if len(self.keys) > 500:
            self.keys = dict(list(self.keys.items())[-200:])

    def ok(self, key: str):
        self.keys.pop(key, None)

    def clear_prefix(self, prefix: str):
        for k in [x for x in self.keys if x.startswith(prefix)]:
            self.keys.pop(k, None)


class WanGate:
    """wan_access.json = 公网外网访问开关（默认关闭，仅在后台按钮开启后放行）。"""

    def __init__(self, state_dir: Path):
        self.path = state_dir / 'wan_access.json'
        self._lock = threading.RLock()
        self._sig = 'unset'
        self._enabled = False

    def enabled(self) -> bool:
        with self._lock:
            sig = _file_sig(self.path)
            if sig != self._sig:
                data = read_json(self.path, {}) if sig else {}
                self._enabled = bool(data.get('enabled')) if isinstance(data, dict) else False
                self._sig = sig
            return self._enabled

    def set_enabled(self, on: bool) -> dict:
        with self._lock:
            rec = {'enabled': bool(on), 'updated_at': int(time.time())}
            write_private_json(self.path, rec)
            self._sig = _file_sig(self.path)
            self._enabled = bool(on)
            return rec


def client_key(request) -> str:
    """限速按访问者算：隧道来的用 nginx 给的 IP（intercom 只对隧道来的保留这两个头），局域网直连的用连接地址。"""
    h = request.headers
    ip = ((h.get('X-Real-IP') or '').strip() or (h.get('X-Forwarded-For') or '').split(',')[0].strip()
          or (h.get('X-TS-Peer') or '').strip())
    prefix = 'wan:' if is_wan_request(request) else ''
    return f'{prefix}{ip or "direct"}'[:64]


# 访问通道策略：默认仅允许局域网或本机可信反向隧道；公网访问需在后台手动开启（2026-11-23 加外网开关）
_LAN_NETS = ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fe80::/10', 'fc00::/7')


def _load_vpn_tunnel_nets():
    """可信免密 SSH 反向隧道出口 IP/CIDR（默认仅本机回环；可通过 ADMIN_VPN_NETS 环境变量补充逗号分隔的 CIDR）。"""
    nets = ['127.0.0.0/8', '::1/128']
    raw = os.environ.get('ADMIN_VPN_NETS', '').strip()
    if raw:
        for tok in raw.split(','):
            tok = tok.strip()
            if tok:
                with contextlib.suppress(ValueError):
                    ipaddress.ip_network(tok, strict=False)
                    nets.append(tok)
    return tuple(nets)


_VPN_TUNNEL_NETS = _load_vpn_tunnel_nets()


def _parse_ip(raw: str):
    try:
        ip = ipaddress.ip_address(str(raw or '').split('%', 1)[0].strip())
    except ValueError:
        return None
    return ip.ipv4_mapped if getattr(ip, 'ipv4_mapped', None) is not None else ip


def _ip_in_nets(raw: str, nets) -> bool:
    ip = _parse_ip(raw)
    return bool(ip is not None and any(ip in ipaddress.ip_network(n) for n in nets))


def _is_loopback(raw: str) -> bool:
    ip = _parse_ip(raw)
    return bool(ip is not None and ip.is_loopback)


def peer_ok(peer: str) -> bool:
    return _ip_in_nets(peer, _LAN_NETS)


def is_wan_request(request) -> bool:
    h = request.headers
    peer = h.get('X-TS-Peer')
    if peer is not None and not peer_ok(peer):
        return True
    return h.get('X-TS-WAN') == '1' and (peer is None or _is_loopback(peer))


def is_vpn_tunnel(request) -> bool:
    if is_wan_request(request):
        return False
    h = request.headers
    peer = h.get('X-TS-Peer')
    if peer is None or not _is_loopback(peer):
        return False
    host, proto = entry_host_proto(request)
    if proto != 'https':
        return False
    try:
        port = urllib.parse.urlsplit(f'//{host}').port
    except ValueError:
        return False
    if port != 18443:
        return False
    client_ip = ((h.get('X-Real-IP') or '').strip() or (h.get('X-Forwarded-For') or '').split(',')[0].strip())
    return bool(client_ip and _ip_in_nets(client_ip, _VPN_TUNNEL_NETS))


def entry_host_proto(request):
    """浏览器眼里的网址（经 nginx → intercom 转发过来时，Host 已经变成了本机地址）。"""
    h = request.headers
    proto = (h.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
    if proto not in ('http', 'https'):
        proto = 'http'
    host = (h.get('X-TS-Orig-Host') or h.get('Host') or '').strip()
    xfh = (h.get('X-Forwarded-Host') or '').split(',')[0].strip()
    if xfh and _hostname(host) in ('127.0.0.1', 'localhost', '::1', ''):
        host = xfh
    return host, proto


def _hostname(host: str) -> str:
    if not host:
        return ''
    try:
        return (urllib.parse.urlsplit(f'//{host}').hostname or '').lower().rstrip('.')
    except ValueError:
        return ''


async def _communicate_bounded(proc, input_bytes: bytes | None = None, timeout: float = 10.0) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(proc.communicate(input_bytes), timeout)
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        raise


def same_origin_ok(request) -> bool:
    sfs = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
    if sfs and sfs not in ('same-origin', 'none'):
        return False
    origin = request.headers.get('Origin')
    if origin is None:
        return request.headers.get('X-TS-Admin') == '1' or sfs == 'same-origin'
    try:
        sp = urllib.parse.urlsplit(origin)
        o_host = (sp.hostname or '').lower().rstrip('.')
    except ValueError:
        return False
    if sp.scheme not in ('http', 'https') or not o_host:
        return False
    host, proto = entry_host_proto(request)
    if sp.scheme != proto and not (sp.scheme == 'http' and o_host == '127.0.0.1' and sp.port is None):
        return False
    try:
        e_port = urllib.parse.urlsplit(f'//{host}').port
    except ValueError:
        e_port = None
    if sp.port is not None and e_port is not None and sp.port != e_port:
        return False
    return bool(o_host) and o_host == _hostname(host)


# ============================== 本地歌曲 ==============================
def usb_mounted() -> bool:
    if USB_ASSUME_MOUNTED:
        return MUSIC_USB.is_dir()
    try:
        return MUSIC_USB.stat().st_dev != MUSIC_USB.parent.stat().st_dev
    except OSError:
        return False


def root_base(key: str):
    if key == 'local':
        return MUSIC_LOCAL if MUSIC_LOCAL.is_dir() else None
    if key == 'hub':
        return MUSIC_HUB if MUSIC_HUB.is_dir() else None
    if key == 'usb':
        return MUSIC_USB if usb_mounted() else None
    return None


def root_writable(base) -> bool:
    if base is None:
        return False
    try:
        sv = os.statvfs(base)
        return not (sv.f_flag & os.ST_RDONLY) and os.access(base, os.W_OK)
    except OSError:
        return False


def root_space(key: str, base) -> dict:
    try:
        du = shutil.disk_usage(base)
    except OSError:
        return {'total': 0, 'used': 0, 'free': 0, 'usable': 0}
    reserve = RESERVE_USB if key == 'usb' else RESERVE_SYSTEM
    return {'total': du.total, 'used': du.used, 'free': du.free, 'usable': max(0, du.free - reserve)}


def _skip_name(name: str) -> bool:
    return (name.startswith('.') or name.startswith('$') or name == 'System Volume Information'
            or name.endswith('.part'))


def scan_root(key: str, base, limit: int = 4000):
    """不跟符号链接（本机音乐里的 usb → /mnt/usb 单独算 U 盘）；跳过隐藏文件夹（回收站、正在上传的临时文件）。"""
    files, folders = [], []
    if base is None:
        return files, folders
    stack = [(str(base), '', 0)]
    seen = 0
    while stack:
        d, rel, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > 30000:
                return files, sorted(folders)
            if _skip_name(e.name):
                continue
            try:
                if e.is_symlink():
                    continue
                rp = f'{rel}/{e.name}' if rel else e.name
                if e.is_dir(follow_symlinks=False):
                    if depth < 5:
                        folders.append(rp)
                        stack.append((e.path, rp, depth + 1))
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                if ext not in AUDIO_EXTS or not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
                files.append({'root': key, 'path': rp, 'name': e.name, 'folder': rel,
                              'size': st.st_size, 'mtime': int(st.st_mtime), 'ext': ext[1:]})
                if len(files) >= limit:
                    return files, sorted(folders)
            except OSError:
                continue
    return files, sorted(folders)


def _check_rel(rel, allow_empty=False) -> list:
    if rel is None or not isinstance(rel, str):
        raise ApiError('路径不对')
    rel = rel.strip('/')
    if not rel:
        if allow_empty:
            return []
        raise ApiError('路径不对')
    if len(rel) > 1024 or '\x00' in rel or '\\' in rel:
        raise ApiError('路径不对')
    parts = rel.split('/')
    if any(p in ('', '.', '..') or _skip_name(p) for p in parts):
        raise ApiError('路径不对')
    return parts


def _walk_no_links(base: Path, parts: list) -> Path:
    cur = base
    for part in parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise ApiError('找不到这首歌（可能刚被改过），请刷新', 404)
        except OSError:
            raise ApiError('读不到这个文件', 409)
        if stat.S_ISLNK(st.st_mode):
            raise ApiError('路径不对')
    return cur


def need_base(key: str) -> Path:
    if key not in ('local', 'hub', 'usb'):
        raise ApiError('位置不对')
    base = root_base(key)
    if base is None:
        raise ApiError('U 盘没有插好' if key == 'usb' else '找不到歌曲文件夹', 409)
    return base


def resolve_file(key: str, rel: str) -> Path:
    base = need_base(key)
    parts = _check_rel(rel)
    p = _walk_no_links(base, parts)
    if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
        raise ApiError('找不到这首歌（可能刚被改过），请刷新', 404)
    return p


def resolve_dir(key: str, rel: str) -> Path:
    base = need_base(key)
    parts = _check_rel(rel or '', allow_empty=True)
    p = _walk_no_links(base, parts) if parts else base
    if not p.is_dir():
        raise ApiError('找不到这个文件夹，请刷新', 404)
    return p


def rel_of(key: str, p: Path) -> str:
    return p.relative_to(need_base(key)).as_posix()


_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|‪-‮⁦-⁩]')
_WIN_RESERVED = re.compile(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])$', re.I)


def _clean_part(raw: str, what: str) -> str:
    name = unicodedata.normalize('NFC', raw if isinstance(raw, str) else '').strip()
    name = _BAD_CHARS.sub('_', name).strip(' .')
    if not name:
        raise ApiError(f'{what}不能为空')
    return name


def _trim_bytes(s: str, limit: int) -> str:
    while len(s.encode('utf-8')) > limit:
        s = s[:-1]
    return s


def clean_song_name(raw: str, keep_ext: str = None) -> str:
    name = _clean_part(raw, '文件名')
    stem, ext = os.path.splitext(name)
    if keep_ext and ext.lower() != keep_ext.lower():
        if ext.lower() in AUDIO_EXTS:
            raise ApiError('不能改扩展名（文件格式）')
        stem, ext = name, keep_ext
    if ext.lower() not in AUDIO_EXTS:
        raise ApiError('只能放 MP3、FLAC、WAV、M4A、AAC、OGG、OPUS、WMA 音乐文件', 415)
    stem = _trim_bytes(stem.strip(' .'), 180)
    if not stem:
        raise ApiError('文件名不能为空')
    if _WIN_RESERVED.match(stem):
        stem = '_' + stem
    return stem + ext


def clean_folder_name(raw: str) -> str:
    name = _trim_bytes(_clean_part(raw, '文件夹名'), 120).strip(' .')
    if not name or name.startswith('$') or _WIN_RESERVED.match(name):
        raise ApiError('换个文件夹名')
    return name


def unique_path(dirpath: Path, name: str) -> Path:
    p = dirpath / name
    if not os.path.lexists(p):
        return p
    stem, ext = os.path.splitext(name)
    for i in range(2, 1000):
        cand = dirpath / f'{stem}（{i}）{ext}'
        if not os.path.lexists(cand):
            return cand
    raise ApiError('同名的文件太多了')


def _has_mpeg_sync(buf: bytes) -> bool:
    trimmed = buf.lstrip(b'\x00 \t\r\n')
    if len(trimmed) < 2:
        return False
    b0, b1 = trimmed[0], trimmed[1]
    return b0 == 0xFF and (b1 & 0xE0) == 0xE0 and (b1 & 0x18) != 0x08 and (b1 & 0x06) != 0x00


def sniff_ok(ext: str, head: bytes) -> bool:
    if ext == '.mp3':
        return (head[:3] == b'ID3' or (head[:4] == b'RIFF' and head[8:12] == b'WAVE') or _has_mpeg_sync(head))
    if ext == '.flac':
        return head[:4] == b'fLaC' or head[:3] == b'ID3'
    if ext == '.wav':
        return head[:4] in (b'RIFF', b'RF64') and head[8:12] == b'WAVE'
    if ext == '.m4a':
        return head[4:8] == b'ftyp' or head[:3] == b'ID3'
    if ext == '.aac':
        return head[:3] == b'ID3' or head[4:8] == b'ftyp' or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0)
    if ext in ('.ogg', '.opus'):
        return head[:4] == b'OggS'
    if ext == '.wma':
        return head[:16] == bytes.fromhex('3026b2758e66cf11a6d900aa0062ce6c')
    return False


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def _safe_meta_dir(base: Path, sub: str, create: bool = False):
    try:
        if os.path.islink(base):
            return None
        d = base / sub
        if os.path.islink(d):
            return None
        if create and not d.exists():
            d.mkdir(mode=0o755, exist_ok=True)
        st = os.lstat(d)
        return d if stat.S_ISDIR(st.st_mode) else None
    except OSError:
        return None


def _incoming(base: Path, name: str) -> Path:
    d = _safe_meta_dir(base, INCOMING_DIR, create=True)
    if d is None:
        raise ApiError('路径不对', 400)
    return d / name


def _touch_local_sig(key: str, base: Path) -> None:
    if key in ('local', 'hub') and base is not None:
        with contextlib.suppress(OSError):
            os.utime(base, None)


def _sync_and_drop_cache(fd: int) -> None:
    os.fsync(fd)
    with contextlib.suppress(AttributeError, OSError):
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)     # 写完就放掉页缓存，免得挤占服务的内存上限


def content_disposition(name: str) -> str:
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', '_', name) or 'song'
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


TRASH_RE = re.compile(r'(\d{9,11})-([0-9a-f]{8})\.deleted')


class Trash:
    """删除 = 改名挪进同一文件夹下的 .thinksmart-trash/<id>.deleted（扩展名不是音乐，大屏和小麦都不会再扫到）。"""

    def __init__(self, state_dir: Path):
        self.path = state_dir / 'trash.json'

    def load(self) -> dict:
        d = read_json(self.path, {})
        return d if isinstance(d, dict) else {}

    def save(self, d: dict):
        write_private_json(self.path, d)

    @staticmethod
    def file_of(base: Path, tid: str) -> Path:
        return base / TRASH_DIR / f'{tid}.deleted'

    def put_many(self, entries):
        """entries = [(root, base, path)]；索引只读写一次（一次删几百首也快）。返回 (删掉的, 失败的)。"""
        d = self.load()
        done, failed = [], []
        now = int(time.time())
        for key, base, p in entries:
            try:
                tdir = _safe_meta_dir(base, TRASH_DIR, create=True)
                if tdir is None:
                    raise OSError('symlink trash dir')
                tid = f'{now}-{secrets.token_hex(4)}'
                size = os.lstat(p).st_size
                rel = p.relative_to(base).as_posix()
                os.rename(p, tdir / f'{tid}.deleted')
                d[tid] = {'root': key, 'path': rel, 'name': p.name, 'size': size, 'at': now}
                done.append({'id': tid, **d[tid]})
            except OSError:
                failed.append({'path': p.name, 'error': '删不掉（U 盘可能是只读的或刚拔掉）'})
        if done:
            self.save(d)
            for k_touched in {x['root'] for x in done}:
                _touch_local_sig(k_touched, root_base(k_touched))
        return done, failed

    def put(self, key: str, base: Path, p: Path) -> dict:
        done, failed = self.put_many([(key, base, p)])
        if not done:
            raise ApiError(failed[0]['error'], 409)
        return done[0]

    def listing(self) -> list:
        d = self.load()
        out = []
        now = time.time()
        for tid, ent in d.items():
            if not isinstance(ent, dict) or not TRASH_RE.fullmatch(f'{tid}.deleted'):
                continue
            base = root_base(ent.get('root', ''))
            tdir = _safe_meta_dir(base, TRASH_DIR) if base is not None else None
            if tdir is None:
                continue
            src = tdir / f'{tid}.deleted'
            if src.is_symlink() or not src.is_file():
                continue
            left = TRASH_DAYS * 86400 - (now - int(ent.get('at', 0)))
            out.append({'id': tid, 'root': ent.get('root'), 'path': ent.get('path', ''), 'name': ent.get('name', ''),
                        'size': ent.get('size', 0), 'at': ent.get('at', 0), 'days_left': max(0, int(left // 86400) + 1)})
        out.sort(key=lambda x: -x['at'])
        return out

    def _restore_one(self, d: dict, tid: str) -> dict:
        ent = d.get(tid)
        if not isinstance(ent, dict):
            raise ApiError('这首歌已经不在最近删除里了', 404)
        key = ent.get('root', '')
        base = root_base(key)
        if base is None:
            raise ApiError('U 盘没插，暂时恢复不了', 409)
        tdir = _safe_meta_dir(base, TRASH_DIR)
        if tdir is None:
            raise ApiError('回收站路径异常，不能恢复', 400)
        src = tdir / f'{tid}.deleted'
        if src.is_symlink() or not src.is_file():
            d.pop(tid, None)
            raise ApiError('这首歌的文件已经找不到了', 404)
        parent = base
        with contextlib.suppress(ApiError):
            folder = ent.get('path', '').rpartition('/')[0]
            if folder:
                parent = resolve_dir(key, folder)
        name = ent.get('name') or 'song.mp3'
        final = unique_path(parent, name)
        os.rename(src, final)
        d.pop(tid, None)
        return {'root': key, 'path': final.relative_to(base).as_posix(), 'name': final.name}

    def restore_many(self, ids):
        d = self.load()
        done, failed = [], []
        for tid in ids:
            try:
                done.append(self._restore_one(d, tid))
            except (ApiError, OSError) as e:
                failed.append({'id': tid, 'error': e.msg if isinstance(e, ApiError) else '恢复不了'})
        self.save(d)
        for k_touched in {x['root'] for x in done}:
            _touch_local_sig(k_touched, root_base(k_touched))
        return done, failed

    def restore(self, tid: str) -> dict:
        done, failed = self.restore_many([tid])
        if not done:
            raise ApiError(failed[0]['error'], 404)
        return done[0]

    def purge(self, ids=None, everything=False, older_than=TRASH_DAYS * 86400) -> int:
        d = self.load()
        now = time.time()
        removed = 0
        changed = False
        for key in ('local', 'hub', 'usb'):
            base = root_base(key)
            if base is None:
                continue
            tdir = _safe_meta_dir(base, TRASH_DIR)
            if tdir is None:
                continue
            try:
                names = os.listdir(tdir)
            except OSError:
                continue
            for n in names:
                m = TRASH_RE.fullmatch(n)
                if not m:
                    continue
                tid = f'{m.group(1)}-{m.group(2)}'
                ent = d.get(tid)
                if ent is not None and ent.get('root') != key:
                    continue
                if everything or (ids and tid in ids) or now - int(m.group(1)) > older_than:
                    unlinked = False
                    try:
                        os.unlink(tdir / n)
                        unlinked = True
                        removed += 1
                        changed = True
                    except FileNotFoundError:
                        unlinked = True
                    except OSError:
                        pass
                    if unlinked and tid in d:
                        d.pop(tid, None)
                        changed = True
            with contextlib.suppress(OSError):
                os.rmdir(tdir)          # 空了就把回收站文件夹也收掉
        for tid in list(d):
            ent = d[tid]
            if not isinstance(ent, dict) or now - int(ent.get('at', 0)) > older_than:
                d.pop(tid, None)
                changed = True
        if changed:
            self.save(d)
        return removed


def cleanup_partials(max_age: int = 3600) -> None:
    """服务重启时清掉没传完的临时文件。"""
    now = time.time()
    for key in ('local', 'hub', 'usb'):
        base = root_base(key)
        if base is None or not root_writable(base):
            continue
        for d, _dirs, files in os.walk(base):
            if TRASH_DIR in Path(d).parts:
                continue
            for n in files:
                if n.startswith(('.upload-', '.import-')) and n.endswith('.part'):
                    p = Path(d) / n
                    with contextlib.suppress(OSError):
                        st = p.lstat()
                        if not stat.S_ISLNK(st.st_mode) and now - st.st_mtime > max_age:
                            p.unlink()


class _Stop(Exception):
    pass


def _natural_key(s: str):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', s.lower())]


def _same_file_there(p: Path, size: int) -> bool:
    try:
        st = os.lstat(p)
        return stat.S_ISREG(st.st_mode) and st.st_size == size
    except OSError:
        return False


def _already_in_dir(dirpath: Path, name: str, size: int) -> bool:
    p = dirpath / name
    if _same_file_there(p, size):
        return True
    if not os.path.lexists(p):
        return False
    stem, ext = os.path.splitext(name)
    for i in range(2, 60):
        cand = dirpath / f'{stem}（{i}）{ext}'
        if not os.path.lexists(cand):
            break
        if _same_file_there(cand, size):
            return True
    return False


class ImportJob:
    """U 盘 → 本机音乐文件夹（固定一个文件夹）。在后台线程里一首一首复制：先写 .import-*.part，写完改名，
    中途拔 U 盘或点停止都不会留下半截文件；本机已有同名同大小的直接跳过，同名不同大小的存成「（2）」。"""

    def __init__(self, items, fs_lock, inflight_box=None, need_bytes=None, reserved_keys=None):
        self.id = secrets.token_hex(6)
        self.items = items                  # [(源文件, 名字, 大小)]
        self.fs_lock = fs_lock
        self.inflight_box = inflight_box
        self.total = len(items)
        self.total_bytes = sum(s for _p, _n, s in items)
        self._reserved_left = set(reserved_keys) if reserved_keys is not None else None
        self.inflight_remaining = self.total_bytes if need_bytes is None else max(0, int(need_bytes))
        self.done = 0
        self.bytes = 0
        self.imported, self.skipped, self.failed = [], [], []
        self.current = ''
        self.state = 'running'
        self.cancel = False
        self.started = time.time()
        self.finished = None

    def public(self) -> dict:
        return {'id': self.id, 'state': self.state, 'total': self.total, 'done': self.done,
                'total_bytes': self.total_bytes, 'bytes': min(self.bytes, self.total_bytes), 'current': self.current,
                'imported': len(self.imported), 'skipped': len(self.skipped), 'failed': self.failed[-20:],
                'failed_count': len(self.failed), 'started': int(self.started),
                'finished': int(self.finished) if self.finished else None}

    def run(self):
        dest = MUSIC_LOCAL
        try:
            for src, name, size in self.items:
                if self.cancel:
                    break
                self.current = name
                tmp = None
                start_bytes = self.bytes
                if self._reserved_left is not None:
                    _rk = (name.lower(), size)
                    _was_reserved = _rk in self._reserved_left
                    self._reserved_left.discard(_rk)
                    rem_item = min(size, self.inflight_remaining) if _was_reserved else 0
                else:
                    _was_reserved = False
                    rem_item = min(size, self.inflight_remaining)
                try:
                    other_inf = (self.inflight_box.get('local', 0) if isinstance(self.inflight_box, dict) else 0)
                    if _already_in_dir(dest, name, size):
                        if self._reserved_left is None:
                            rem_item = 0
                        self.skipped.append(name)
                    elif size > max(0, root_space('local', dest)['usable'] - other_inf):
                        self.failed.append({'name': name, 'error': '本机空间不够了'})
                    else:
                        tmp = _incoming(dest, f'.import-{secrets.token_hex(6)}.part')
                        with open(src, 'rb') as fi, open(tmp, 'xb') as fo:
                            while True:
                                if self.cancel:
                                    raise _Stop()
                                buf = fi.read(1 << 20)
                                if not buf:
                                    break
                                fo.write(buf)
                                self.bytes += len(buf)
                                step = min(rem_item, len(buf))
                                rem_item -= step
                                self.inflight_remaining = max(0, self.inflight_remaining - step)
                            fo.flush()
                            os.fsync(fo.fileno())
                            with contextlib.suppress(AttributeError, OSError):
                                os.posix_fadvise(fo.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                        with self.fs_lock:
                            final = unique_path(dest, name)
                            os.rename(tmp, final)
                        tmp = None
                        self.imported.append(final.name)
                except _Stop:
                    self.bytes = start_bytes
                    break
                except (OSError, ApiError) as e:
                    self.bytes = start_bytes
                    self.failed.append({'name': name, 'error': e.msg if isinstance(e, ApiError) else '复制失败（U 盘可能拔掉了）'})
                finally:
                    if tmp is not None:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp)
                    if rem_item > 0:
                        self.inflight_remaining = max(0, self.inflight_remaining - rem_item)
                if not self.cancel:
                    self.done += 1
                    self.bytes = start_bytes + size
            self.state = 'cancelled' if self.cancel else 'done'
        except Exception:
            log.exception('import job failed')
            self.state = 'error'
        finally:
            self.inflight_remaining = 0
            self.current = ''
            self.finished = time.time()
            if self.imported:
                with contextlib.suppress(Exception):
                    _touch_local_sig('local', dest)
            log.info('import %s: %d imported, %d skipped, %d failed', self.state, len(self.imported),
                     len(self.skipped), len(self.failed))


# ============================== Clash（mihomo）==============================
def mihomo_secret() -> str:
    cands = []
    cd = os.environ.get('CREDENTIALS_DIRECTORY')
    if cd:
        cands.append(Path(cd) / 'mihomo.yaml')
    cands.append(MIHOMO_CFG)
    for p in cands:
        try:
            with open(p, encoding='utf-8') as f:
                for line in f:
                    m = re.match(r'^secret:[ \t]*(.*?)[ \t]*$', line.rstrip('\r\n'))
                    if not m:
                        continue
                    v = m.group(1)
                    if v[:1] in ('"', "'"):
                        q = v[0]
                        end = v.find(q, 1)
                        v = v[1:end] if end > 0 else v[1:]
                    else:
                        v = re.split(r'\s+#', v, maxsplit=1)[0].strip()
                    return v
            return ''
        except OSError:
            continue
    return ''


_SEG = r'[^/]+'
CLASH_ALLOW = {
    'GET': re.compile(rf'^(|version|configs|proxies|proxies/{_SEG}|proxies/{_SEG}/delay|group|group/{_SEG}|group/{_SEG}/delay|'
                      rf'providers/proxies|providers/proxies/{_SEG}|providers/proxies/{_SEG}/healthcheck|'
                      rf'providers/proxies/{_SEG}/{_SEG}/healthcheck|providers/rules|rules|connections|traffic|memory|logs|dns/query)$'),
    'PUT': re.compile(rf'^(proxies/{_SEG}|providers/proxies/{_SEG}|providers/rules/{_SEG}|configs)$'),
    'PATCH': re.compile(r'^(configs|rules/disable)$'),
    'DELETE': re.compile(rf'^(connections|connections/{_SEG}|proxies/{_SEG})$'),
    'POST': re.compile(r'^(cache/fakeip/flush|cache/dns/flush|upgrade/geo)$'),
}
CLASH_WS = re.compile(r'^(traffic|memory|logs|connections)$')
CLASH_PATCH_KEYS = {'mode', 'log-level', 'ipv6', 'tcp-concurrent', 'unified-delay', 'sniffing', 'find-process-mode'}
CLASH_MODES = {'rule', 'global', 'direct', 'Rule', 'Global', 'Direct'}


def clash_tail(request) -> str:
    raw = request.raw_path.split('?', 1)[0]
    if raw == '/admin/api/clash' and request.method == 'GET':
        return ''
    prefix = '/admin/api/clash/'
    if not raw.startswith(prefix):
        raise ApiError('不支持的请求', 404)
    tail = raw[len(prefix):]
    if tail == '' and request.method == 'GET':
        return ''                       # mihomo 根地址（{"hello":"mihomo"}），完整面板连接时先探这个
    segs = tail.split('/')
    if not tail or any(not s or urllib.parse.unquote(s) in ('.', '..') for s in segs):
        raise ApiError('不支持的请求', 404)
    if any(c in tail for c in ('\\', '\x00', ' ')):
        raise ApiError('不支持的请求', 404)
    return tail


def clash_url(tail: str, query, ws: bool = False):
    """原样拼回 mihomo 的地址（yarl 不再改动编码）；浏览器带来的 token 参数丢掉，由本服务加密钥。"""
    base = MIHOMO_API.replace('http://', 'ws://', 1) if ws else MIHOMO_API
    qs = urllib.parse.urlencode([(k, v) for k, v in query.items() if k != 'token'])
    return URL(f'{base}/{tail}' + (f'?{qs}' if qs else ''), encoded=True)


def clash_body_ok(method: str, tail: str, body: bytes) -> bool:
    if method == 'PATCH' and tail == 'rules/disable':
        try:
            d = json.loads(body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(d, dict) and bool(d) and len(d) <= 500 and all(
            isinstance(k, str) and k.isdigit() and isinstance(v, bool) for k, v in d.items())
    if method == 'PATCH':
        try:
            d = json.loads(body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(d, dict) or not d or not set(d) <= CLASH_PATCH_KEYS:
            return False
        if 'mode' in d and d['mode'] not in CLASH_MODES:
            return False
        if 'log-level' in d and d['log-level'] not in ('debug', 'info', 'warning', 'error', 'silent'):
            return False
        if 'find-process-mode' in d and d['find-process-mode'] not in ('always', 'strict', 'off'):
            return False
        for bk in ('ipv6', 'tcp-concurrent', 'unified-delay', 'sniffing'):
            if bk in d and not isinstance(d[bk], bool):
                return False
        return True
    if method == 'PUT' and tail == 'configs':
        # 只允许「按原文件重新加载」，不允许从别的路径或内容加载配置
        try:
            d = json.loads(body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(d, dict) and not d.get('path') and not d.get('payload')
    return True


# ============================== 接口与模型配置辅助 ==============================
# 2026-11 最新各厂商主力模型与本机网关实测可用模型预设（支持一键点选，也可点「拉取接口可用模型」实时读取）
LLM_VENDORS = (
    {'id': 'deepseek', 'label': 'DeepSeek', 'entry': 'DeepSeekLLM', 'base_url': 'https://api.deepseek.com',
     'models': ['deepseek-v4.1-flash', 'deepseek-flash', 'deepseek-v4-flash', 'deepseek-chat', 'deepseek-reasoner']},
    {'id': 'openai', 'label': 'OpenAI / 兼容网关', 'entry': 'OpenAILLM', 'base_url': 'https://api.openai.com/v1',
     'models': ['gpt-6.2-astra', 'gpt-6.2-sol', 'gpt-6.1-terra', 'gpt-6.1-sol', 'gpt-6-luna']},
    {'id': 'claude', 'label': 'Claude', 'entry': 'ClaudeAPI', 'base_url': 'https://api.anthropic.com/v1/',
     'models': ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5-5', 'claude-fable-5-2']},
    {'id': 'gemini', 'label': 'Gemini', 'entry': 'GeminiAPI', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/',
     'models': ['gemini-4-hydrogen', 'gemini-4-neon', 'gemini-4-carbon', 'gemini-3.8-flash', 'gemini-3.5-flash-lite']},
    {'id': 'qwen', 'label': '通义千问', 'entry': 'AliLLM', 'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     'models': ['qwen4-max', 'qwen3.5-plus', 'qwen-flash', 'qwen-plus']},
    {'id': 'doubao', 'label': '字节豆包', 'entry': 'DoubaoLLM', 'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
     'models': ['doubao-seed-2.1-pro', 'doubao-seed-2.1-lite', 'doubao-seed-2-0-lite-260215']},
    {'id': 'glm', 'label': '智谱 GLM', 'entry': 'ChatGLMLLM', 'base_url': 'https://open.bigmodel.cn/api/paas/v4/',
     'models': ['glm-5', 'glm-5-flash', 'glm-4.5-flash', 'glm-4-flash']},
    {'id': 'kimi', 'label': 'Kimi', 'entry': 'KimiLLM', 'base_url': 'https://api.moonshot.cn/v1',
     'models': ['kimi-k3.1-pro', 'kimi-k3', 'kimi-latest']},
    {'id': 'grok', 'label': 'xAI Grok', 'entry': 'GrokLLM', 'base_url': 'https://api.x.ai/v1',
     'models': ['grok-4.20', 'grok-4-fast', 'grok-4', 'grok-3-mini']},
    {'id': 'custom', 'label': '自定义 OpenAI 兼容', 'entry': 'CustomOpenAILLM', 'base_url': '',
     'models': [], 'hint': '支持任意 OpenAI 兼容接口（如中转站、本机 Ollama http://192.168.x.x:11434/v1 等），可点「拉取接口可用模型」自动读取'},
)
VENDOR_BY_ID = {v['id']: v for v in LLM_VENDORS}
VENDOR_BY_ENTRY = {v['entry']: v for v in LLM_VENDORS}


def _finite_float(val) -> float:
    if isinstance(val, bool):
        raise ValueError('bool is not float')
    f = float(val)
    if not math.isfinite(f):
        raise ValueError('non-finite float')
    return f


def _int_or(val, default: int, lo: int, hi: int) -> int:
    if val is None or isinstance(val, bool) or str(val).strip() == '':
        return default
    try:
        return max(lo, min(hi, int(_finite_float(val))))
    except (TypeError, ValueError, OverflowError):
        return default

TUTOR_MODEL_PRESETS = (
    'gpt-6.1-terra', 'gpt-6.2-sol', 'gpt-6.1-sol', 'gpt-6.2-astra',
    'deepseek-v4.1-flash', 'gpt-4o', 'qwen-vl-max', 'glm-4v-plus',
)
CLAUDE_CLI_HINT = os.environ.get('ADMIN_CLAUDE_CLI_BIN', '')
CLAUDE_CLI_MODELS = ('haiku', 'sonnet', 'opus', 'fable')
CLAUDE_CLI_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
CLAUDE_CLI_LEVELS = {
    'read': ('default', ['Read', 'Grep', 'Glob']),
    'edit': ('acceptEdits', ['Read', 'Grep', 'Glob', 'Write', 'Edit']),
    'auto': ('auto', ['Read', 'Grep', 'Glob', 'Write', 'Edit', 'Bash', 'TodoWrite']),
}
CLAUDE_WEB_TOOLS = ('WebSearch', 'WebFetch')
WAKE_PRESETS = (
    {'raw': 'x iǎo m ài x iǎo m ài @小麦小麦', 'label': '小麦小麦（推荐）'},
    {'raw': 'h ā l óu x iǎo m ài @哈喽小麦', 'label': '哈喽小麦'},
    {'raw': 'x iǎo m ài t óng x ué @小麦同学', 'label': '小麦同学'},
)
WAKE_PRESET_MAP = {w['raw']: w['label'] for w in WAKE_PRESETS}
INTENT_PLUGINS = (
    {'id': 'get_weather', 'label': '查天气（Open-Meteo / 和风）'},
    {'id': 'web_search', 'label': '四合一联网搜索'},
    {'id': 'play_music', 'label': '语音点歌与音乐控制'},
    {'id': 'get_news_from_newsnow', 'label': '热点财经新闻（NewsNow）'},
    {'id': 'get_news_from_chinanews', 'label': '中新网要闻播报'},
    {'id': 'change_role', 'label': '语音切换助手角色'},
)
INTENT_PLUGIN_IDS = {p['id'] for p in INTENT_PLUGINS}
HASS_PLUGIN_FUNCS = ('hass_get_state', 'hass_set_state', 'hass_play_music')
EXT_PLUGIN_IDS = set(HASS_PLUGIN_FUNCS) | {'search_from_ragflow'}
SCREEN_OFF_MINUTES_SET = (1, 3, 5, 10, 30)
ALARM_TONES = (
    {'id': 'coffee', 'label': '咖啡时光'},
    {'id': 'morning', 'label': '晨曦'},
    {'id': 'bright', 'label': '元气早晨'},
    {'id': 'hop', 'label': '阳光小跳跃'},
    {'id': 'marimba', 'label': '清晨木琴'},
)
ALARM_TONE_IDS = {t['id'] for t in ALARM_TONES}
ALARM_TONE_FILES = {
    'coffee': 'alarm-coffee.ogg',
    'morning': 'alarm-morning-light.ogg',
    'bright': 'alarm-bright-start.ogg',
    'hop': 'alarm-sunny-hop.ogg',
}
SHERPA_LOCAL_MODELS = (
    {'dir': 'models/sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03', 'type': 'transducer', 'label': 'Zipformer 中英带标点（2026-06 推荐）'},
    {'dir': 'models/SenseVoiceSmall', 'type': 'sense_voice', 'label': 'SenseVoiceSmall 多语种（中英日韩粤）'},
)
SHERPA_MODEL_BY_DIR = {m['dir']: m for m in SHERPA_LOCAL_MODELS}

EDGE_VOICES = (
    {'value': 'zh-CN-XiaoxiaoNeural', 'name': '晓晓 · 温暖女声（普通话）'},
    {'value': 'zh-CN-XiaoyiNeural', 'name': '晓伊 · 活泼女声（普通话）'},
    {'value': 'zh-CN-YunxiNeural', 'name': '云希 · 阳光男声（普通话）'},
    {'value': 'zh-CN-YunxiaNeural', 'name': '云夏 · 童声男声（普通话）'},
    {'value': 'zh-CN-YunjianNeural', 'name': '云健 · 激情解说（普通话）'},
    {'value': 'zh-CN-YunyangNeural', 'name': '云扬 · 稳重播报（普通话）'},
    {'value': 'zh-CN-liaoning-XiaobeiNeural', 'name': '晓北 · 东北话女声'},
    {'value': 'zh-CN-shaanxi-XiaoniNeural', 'name': '晓妮 · 陕西话女声'},
    {'value': 'zh-HK-HiuGaaiNeural', 'name': '曉佳 · 粤语女声'},
    {'value': 'zh-HK-HiuMaanNeural', 'name': '曉曼 · 粤语女声'},
    {'value': 'zh-HK-WanLungNeural', 'name': '雲龍 · 粤语男声'},
    {'value': 'zh-TW-HsiaoChenNeural', 'name': '曉臻 · 台湾女声'},
    {'value': 'zh-TW-HsiaoYuNeural', 'name': '曉雨 · 台湾女声'},
    {'value': 'zh-TW-YunJheNeural', 'name': '雲哲 · 台湾男声'},
)
EDGE_VOICE_SET = {v['value'] for v in EDGE_VOICES}

DOUBAO_ASR_RESOURCES = (
    {'value': 'volc.seedasr.sauc.duration', 'label': '豆包 2.0 小时版（推荐）'},
    {'value': 'volc.seedasr.sauc.concurrent', 'label': '豆包 2.0 并发版'},
    {'value': 'volc.bigasr.sauc.duration', 'label': '豆包 1.0 小时版'},
    {'value': 'volc.bigasr.sauc.concurrent', 'label': '豆包 1.0 并发版'},
)

NEWSNOW_CHANNEL_MAP = {
    '财联社': 'cls-telegraph', '华尔街见闻': 'wallstreetcn-quick', '格隆汇': 'gelonghui',
    '金十数据': 'jin10', 'MKTNews': 'mktnews-flash', '法布财经': 'fastbull-express',
    '雪球': 'xueqiu-hotstock', '澎湃新闻': 'thepaper', '联合早报': 'zaobao',
    'IT之家': 'ithome', '36氪': '36kr-quick', '百度热搜': 'baidu',
    '微博': 'weibo', '知乎': 'zhihu', '今日头条': 'toutiao',
    '凤凰网': 'ifeng', '参考消息': 'cankaoxiaoxi', '靠谱新闻': 'kaopu',
    '卫星通讯社': 'sputniknewscn', '哔哩哔哩': 'bilibili-hot-search', '抖音': 'douyin',
    '快手': 'kuaishou', '虎扑': 'hupu', '百度贴吧': 'tieba',
    '酷安': 'coolapk', '少数派': 'sspai', '稀土掘金': 'juejin',
    'V2EX': 'v2ex-share', 'Solidot': 'solidot', 'Hacker News': 'hackernews',
    'Product Hunt': 'producthunt', 'Github': 'github-trending-today',
    '牛客': 'nowcoder', '远景论坛': 'pcbeta-windows11', '虫部落': 'chongbuluo-latest',
}
NEWSNOW_FEATURED = (
    '财联社', '华尔街见闻', '格隆汇', '金十数据', 'MKTNews', '雪球', '法布财经',
    '澎湃新闻', '联合早报', 'IT之家', '36氪', '百度热搜', '微博', '知乎', '今日头条', '凤凰网',
)

CALL_NAME_PRESETS = (
    ('DeepSeekLLM', ['deepseek', '深度求索']),
    ('OpenAILLM', ['gpt', '小g']),
    ('GeminiAPI', ['gemini', '双子星']),
    ('ClaudeAPI', ['claude', 'cloud', '克劳德']),
    ('AliLLM', ['千问', '通义']),
    ('DoubaoLLM', ['豆包']),
    ('ChatGLMLLM', ['智谱', 'glm']),
    ('KimiLLM', ['kimi']),
    ('GrokLLM', ['grok', '格罗克']),
    ('ClaudeCLI', ['编程助手', '代码助手']),
)
VALID_CALL_ENTRIES = {v['entry'] for v in LLM_VENDORS} | {'ClaudeCLI'}
_CALL_ENTRY_LOOKUP = {e.lower(): e for e in VALID_CALL_ENTRIES}
for _v in LLM_VENDORS:
    _CALL_ENTRY_LOOKUP[_v['id'].lower()] = _v['entry']
    _CALL_ENTRY_LOOKUP[_v['label'].lower()] = _v['entry']
_CALL_ENTRY_LOOKUP['claude_cli'] = 'ClaudeCLI'
_CALL_ALIAS_BAD = re.compile(r'[\s,，.。!！?？、~～;；:："\'“”‘’]+')

_PROTECTED_LOOPBACK_PORTS = {
    22, 8090, 8091, 8092, 8093, 8094, 8095, 8096, 8097, 8098,
    9090, 16688, 16689, 18022, 18090, 18091, 18092,
}
_QW_HOST_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{2,80}\.[a-z]{2,16}$')
_OVERSEAS_PROXY_SUFFIXES = ('googleapis.com', 'openai.com', 'anthropic.com', 'x.ai', 'openrouter.ai', 'busiyi.world')
_clash_probe_cache = [0.0, False]


def _clash_local_proxy(url: str = '') -> str | None:
    if url:
        try:
            h = (urllib.parse.urlsplit(str(url)).hostname or '').strip().lower().rstrip('.')
            if h == 'localhost' or h.endswith(('.localhost', '.local', '.lan')):
                return None
            if h:
                try:
                    ip = ipaddress.ip_address(h.split('%', 1)[0])
                    if getattr(ip, 'ipv4_mapped', None) is not None:
                        ip = ip.ipv4_mapped
                    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
                        return None
                except ValueError:
                    pass
        except ValueError:
            return None
    now = time.monotonic()
    if now - _clash_probe_cache[0] > 5.0:
        ok = False
        try:
            with socket.create_connection(('127.0.0.1', 7890), timeout=0.15):
                ok = True
        except OSError:
            ok = False
        _clash_probe_cache[0], _clash_probe_cache[1] = now, ok
    return 'http://127.0.0.1:7890' if _clash_probe_cache[1] else None


def _clash_proxy_for_url(url: str) -> str | None:
    try:
        host = (urllib.parse.urlsplit(str(url or '')).hostname or '').strip().lower().rstrip('.')
    except ValueError:
        return None
    if not host or not any(host == s or host.endswith('.' + s) for s in _OVERSEAS_PROXY_SUFFIXES):
        return None
    return _clash_local_proxy(url)


def _read_sys_stats() -> dict:
    out = {
        'disk_total': 0, 'disk_used': 0, 'disk_free': 0, 'disk_pct': 0,
        'mem_total': 0, 'mem_avail': 0, 'mem_pct': 0,
        'cpu_temp_c': None, 'uptime_sec': 0,
    }
    with contextlib.suppress(OSError):
        du = shutil.disk_usage('/')
        if du.total > 0:
            out['disk_total'] = int(du.total)
            out['disk_used'] = int(du.used)
            out['disk_free'] = int(du.free)
            out['disk_pct'] = max(0, min(100, round(du.used * 100 / du.total)))
    with contextlib.suppress(OSError, ValueError):
        mt, ma = 0, 0
        with open('/proc/meminfo', encoding='utf-8') as fm:
            for ln in fm:
                if ln.startswith('MemTotal:'):
                    mt = int(ln.split()[1]) * 1024
                elif ln.startswith('MemAvailable:'):
                    ma = int(ln.split()[1]) * 1024
                if mt and ma:
                    break
        if mt > 0:
            out['mem_total'] = mt
            out['mem_avail'] = max(0, min(mt, ma))
            out['mem_pct'] = max(0, min(100, round((mt - out['mem_avail']) * 100 / mt)))
    with contextlib.suppress(OSError, ValueError):
        temps = []
        tbase = Path('/sys/class/thermal')
        if tbase.is_dir():
            for tz in tbase.glob('thermal_zone*/temp'):
                v = int(tz.read_text(encoding='utf-8').strip())
                if 10000 <= v <= 115000:
                    temps.append(v / 1000.0)
        if temps:
            out['cpu_temp_c'] = round(max(temps), 1)
    with contextlib.suppress(OSError, ValueError):
        up_raw = Path('/proc/uptime').read_text(encoding='utf-8').split()[0]
        out['uptime_sec'] = max(0, int(float(up_raw)))
    return out


_WMO_WEATHER_ZH = {
    0: '晴', 1: '大部晴朗', 2: '多云', 3: '阴',
    45: '雾', 48: '雾凇', 51: '毛毛雨', 53: '细雨', 55: '密毛毛雨',
    61: '小雨', 63: '中雨', 65: '大雨', 66: '冻雨', 67: '强冻雨',
    71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
    80: '阵雨', 81: '中阵雨', 82: '强阵雨', 85: '阵雪', 86: '强阵雪',
    95: '雷阵雨', 96: '雷雨伴冰雹', 99: '强雷暴冰雹',
}


def is_placeholder_secret(val) -> bool:
    if val is None:
        return True
    s = str(val).strip()
    if not s:
        return True
    low = s.lower()
    return ('你' in s) or ('your_' in low) or ('your-' in low) or (low in ('none', 'null', 'xxx', 'changeme'))


def clean_text_val(val) -> str:
    return '' if is_placeholder_secret(val) else str(val).strip()


def mask_secret(val) -> tuple[bool, str]:
    if is_placeholder_secret(val):
        return False, ''
    s = str(val).strip()
    if len(s) <= 4:
        return True, '••••'
    if len(s) <= 8:
        return True, f'{s[:2]}••••{s[-2:]}'
    return True, f'{s[:4]}••••{s[-4:]}'


def check_no_ws_secret(val: str, label: str = 'API Key') -> str:
    s = str(val or '').strip()
    if any(ch.isspace() or ord(ch) < 32 for ch in s):
        raise ApiError(f'{label} 里不能有空格或换行')
    if len(s) > 512:
        raise ApiError(f'{label} 太长了')
    return s


def _check_ip_target(ip, eff_port: int, label: str = '接口地址'):
    if getattr(ip, 'ipv4_mapped', None) is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        raise ApiError(f'{label}不允许指向保留地址')
    if ip.is_loopback and eff_port in _PROTECTED_LOOPBACK_PORTS:
        raise ApiError(f'{label}不能指向设备内部管理端口（:{eff_port}）')
    return ip


def check_safe_url(raw: str, label: str = '接口地址', allow_empty: bool = False, schemes=('http', 'https')) -> str:
    s = str(raw or '').strip()
    if not s:
        if allow_empty:
            return ''
        raise ApiError(f'请填写{label}')
    if len(s) > 320 or any(ch.isspace() or ord(ch) < 32 for ch in s):
        raise ApiError(f'{label}格式不对')
    try:
        sp = urllib.parse.urlsplit(s)
    except ValueError:
        raise ApiError(f'{label}格式不对')
    if sp.scheme.lower() not in schemes or not sp.hostname:
        raise ApiError(f'{label}需要以 {" / ".join(x + "://" for x in schemes)} 开头')
    if sp.username or sp.password:
        raise ApiError(f'{label}里不能包含账号密码')
    host = sp.hostname.lower().rstrip('.')
    try:
        port = sp.port
    except ValueError:
        raise ApiError(f'{label}端口格式不对')
    eff_port = port or ({'http': 80, 'https': 443, 'ws': 80, 'wss': 443}.get(sp.scheme.lower(), 80))
    is_loop = (host == 'localhost' or host.endswith('.localhost'))
    try:
        ip = _check_ip_target(ipaddress.ip_address(host), eff_port, label)
        if ip.is_loopback:
            is_loop = True
    except ValueError:
        if re.fullmatch(r'(?:0x[0-9a-f]+|\d+)', host.rsplit('.', 1)[-1]):
            raise ApiError(f'{label}不支持非标准数字 IP 地址')
    if is_loop and eff_port in _PROTECTED_LOOPBACK_PORTS:
        raise ApiError(f'{label}不能指向设备内部管理端口（:{eff_port}）')
    return s


async def _verify_outbound_url(url: str, label: str = '接口地址') -> dict:
    s = str(url or '').strip()
    try:
        sp = urllib.parse.urlsplit(s)
        port = sp.port
    except ValueError:
        raise ApiError(f'{label}格式不对')
    host = (sp.hostname or '').lower().rstrip('.')
    if not host:
        raise ApiError(f'{label}格式不对')
    eff_port = port or ({'http': 80, 'https': 443, 'ws': 80, 'wss': 443}.get(sp.scheme.lower(), 80))
    try:
        ip = _check_ip_target(ipaddress.ip_address(host.split('%', 1)[0]), eff_port, label)
        return {'local': bool(ip.is_loopback or ip.is_private or ip.is_link_local)}
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, host, eff_port, 0, socket.SOCK_STREAM),
            timeout=4.0,
        )
    except (socket.gaierror, asyncio.TimeoutError, OSError):
        return {'local': host == 'localhost' or host.endswith(('.localhost', '.local', '.lan'))}
    any_local = host == 'localhost' or host.endswith(('.localhost', '.local', '.lan'))
    for info in infos:
        sa = info[4]
        if not sa:
            continue
        addr = sa[0].split('%', 1)[0]
        try:
            ip = _check_ip_target(ipaddress.ip_address(addr), eff_port, label)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            any_local = True
    return {'local': any_local}


def normalize_qweather_host(raw: str) -> str:
    s = str(raw or '').strip().lower()
    if not s:
        return ''
    for pfx in ('https://', 'http://'):
        if s.startswith(pfx):
            s = s[len(pfx):]
    s = s.split('/', 1)[0].strip()
    # Allow host:port ONLY in offline unit tests when QWEATHER_PROTO == 'http'
    if QWEATHER_PROTO == 'http' and re.fullmatch(r'127\.0\.0\.1:\d{4,5}', s):
        p = int(s.split(':', 1)[1])
        if p not in _PROTECTED_LOOPBACK_PORTS:
            return s
    if not _QW_HOST_RE.fullmatch(s):
        raise ApiError('和风天气 API Host 格式不对（例如 abc123xyz.re.qweatherapi.com，不带 https://）')
    try:
        ipaddress.ip_address(s)
        raise ApiError('和风天气 API Host 请填写域名，不要填纯 IP')
    except ValueError:
        pass
    return s


def normalize_mac_asr_url(raw: str) -> str:
    s = check_safe_url(raw, '语音识别接口地址', allow_empty=False)
    base = s.rstrip('/')
    if base.endswith('/audio/transcriptions'):
        return base
    if base.endswith('/v1'):
        return base + '/audio/transcriptions'
    return base + '/v1/audio/transcriptions'


def strip_chat_completions_suffix(raw: str) -> str:
    s = str(raw or '').strip()
    if s.rstrip('/').endswith('/chat/completions'):
        return s.rstrip('/')[:-len('/chat/completions')]
    return s


def anthropic_messages_url(base_url: str) -> str:
    u = str(base_url or '').strip().rstrip('/')
    if u.endswith('/messages'):
        return u
    if u.endswith('/v1'):
        return u + '/messages'
    return u + '/v1/messages'


async def read_resp_bounded(r, limit: int = 262144) -> bytes:
    """读取完整 HTTP 响应直到 EOF（避免 aiohttp StreamReader.read(n) 在首个分块后提前截断流式音频）。"""
    buf = bytearray()
    async for chunk in r.content.iter_chunked(65536):
        buf.extend(chunk)
        if len(buf) > limit:
            raise ApiError('上游返回数据超出大小限制', 502)
    return bytes(buf)


def fmt_net_err(e: Exception, label: str = '目标接口') -> str:
    if isinstance(e, asyncio.TimeoutError):
        return f'{label}响应超时，请检查服务是否繁忙或网络是否畅通'
    if isinstance(e, aiohttp.ClientConnectorSSLError):
        return f'{label}安全证书校验失败（SSL 错误）'
    if isinstance(e, aiohttp.ClientConnectorError):
        return f'连不上{label}，请检查地址、端口或网关是否启动'
    return f'{label}请求失败（{type(e).__name__}）'


def _deep_merge_dict(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


_RUAMEL_MUTATE_SCRIPT = r"""
import json, os, secrets, sys
from pathlib import Path
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

def _to_ruamel(v):
    if isinstance(v, dict):
        cm = CommentedMap()
        for kk, vv in v.items():
            cm[kk] = _to_ruamel(vv)
        return cm
    if isinstance(v, list):
        cs = CommentedSeq()
        for item in v:
            cs.append(_to_ruamel(item))
        return cs
    return v

custom_path = Path(sys.argv[1])
if custom_path.is_symlink() or custom_path.parent.is_symlink():
    raise OSError(f'Refusing symlink path: {custom_path}')
ops = json.loads(sys.stdin.read())
yml = YAML()
yml.preserve_quotes = True
yml.width = 4096

if custom_path.is_file():
    with open(custom_path, 'r', encoding='utf-8') as f:
        data = yml.load(f)
else:
    data = None
if not isinstance(data, dict):
    data = CommentedMap()

for item in ops:
    op = item.get('op')
    path = item.get('path') or []
    if not path:
        continue
    if op == 'set':
        cur = data
        for seg in path[:-1]:
            nxt = cur.get(seg)
            if not isinstance(nxt, dict):
                nxt = CommentedMap()
                cur[seg] = nxt
            cur = nxt
        cur[path[-1]] = _to_ruamel(item.get('value'))
    elif op == 'del':
        cur = data
        for seg in path[:-1]:
            cur = cur.get(seg) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict):
            cur.pop(path[-1], None)

custom_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
tmp = custom_path.with_name(f'.config.{secrets.token_hex(4)}.tmp')
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        os.fchmod(f.fileno(), 0o600)
        yml.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    if custom_path.is_symlink():
        raise OSError(f'Refusing symlink target: {custom_path}')
    os.replace(tmp, custom_path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
"""


def _load_yaml_file(p: Path) -> dict:
    if not p.is_file():
        return {}
    raw = p.read_text(encoding='utf-8')
    if not raw.strip():
        return {}
    if raw.lstrip().startswith('{'):
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    if _yaml is not None:
        d = _yaml.safe_load(raw)
        return d if isinstance(d, dict) else {}
    if HUB_PY.is_file():
        try:
            proc = subprocess.run(
                [str(HUB_PY), '-B', '-c',
                 'import json, sys; from ruamel.yaml import YAML; y=YAML(typ="safe"); '
                 'd=y.load(open(sys.argv[1], encoding="utf-8")) or {}; print(json.dumps(d, ensure_ascii=False))',
                 str(p)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
            if proc.returncode == 0:
                d = json.loads(proc.stdout.decode('utf-8'))
                return d if isinstance(d, dict) else {}
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return {}
    return {}


def load_hub_configs() -> tuple[dict, dict]:
    base_cfg = _load_yaml_file(HUB_DIR / 'config.yaml')
    custom_cfg = _load_yaml_file(HUB_DIR / 'data' / '.config.yaml')
    return _deep_merge_dict(base_cfg, custom_cfg), custom_cfg


def mutate_hub_yaml(ops: list[dict], lock: threading.RLock) -> None:
    if not ops:
        return
    custom_path = HUB_DIR / 'data' / '.config.yaml'
    with lock:
        if custom_path.is_symlink() or custom_path.parent.is_symlink():
            raise ApiError('拒绝写入符号链接配置文件', 409)
        custom_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if custom_path.is_symlink():
            raise ApiError('拒绝写入符号链接配置文件', 409)
        if HUB_PY.is_file():
            try:
                proc = subprocess.run(
                    [str(HUB_PY), '-B', '-c', _RUAMEL_MUTATE_SCRIPT, str(custom_path)],
                    input=json.dumps(ops, ensure_ascii=False).encode('utf-8'),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
            except (subprocess.TimeoutExpired, OSError) as e:
                log.error('ruamel mutate failed: %s', _redact_err(e))
                raise ApiError('保存小智配置文件超时或失败', 500)
            if proc.returncode != 0:
                err_s = _redact_err(proc.stderr.decode('utf-8', errors='replace').strip())[:160]
                log.error('ruamel mutate failed: %s', err_s)
                raise ApiError('保存小智配置文件失败', 500)
            return
        # Fallback for environments without HUB_PY (e.g. standalone tests)
        data = _load_yaml_file(custom_path)
        for item in ops:
            op = item.get('op')
            path = item.get('path') or []
            if not path:
                continue
            if op == 'set':
                cur = data
                for seg in path[:-1]:
                    nxt = cur.get(seg)
                    if not isinstance(nxt, dict):
                        nxt = {}
                        cur[seg] = nxt
                    cur = nxt
                cur[path[-1]] = item.get('value')
            elif op == 'del':
                cur = data
                for seg in path[:-1]:
                    cur = cur.get(seg) if isinstance(cur, dict) else None
                    if cur is None:
                        break
                if isinstance(cur, dict):
                    cur.pop(path[-1], None)
        tmp = custom_path.with_name(f'.config.{secrets.token_hex(4)}.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                os.fchmod(f.fileno(), 0o600)
                if _yaml is not None:
                    _yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=4096)
                else:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if custom_path.is_symlink():
                raise ApiError('拒绝写入符号链接配置文件', 409)
            os.replace(tmp, custom_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def write_private_text(path: Path, text: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ApiError('拒绝写入符号链接', 409)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ApiError('拒绝写入符号链接', 409)
    tmp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.is_symlink():
            raise ApiError('拒绝写入符号链接', 409)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _wav_wrap_pcm16(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def write_client_cfg_patch(patch: dict) -> None:
    """原子更新 ~/.config/thinksmart-client/config.json（保留原有字段与 0644 权限）。"""
    if not patch:
        return
    if CLIENT_CFG.is_symlink() or CLIENT_CFG.parent.is_symlink():
        raise ApiError('拒绝写入符号链接配置文件', 409)
    CLIENT_CFG.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if CLIENT_CFG.is_symlink():
        raise ApiError('拒绝写入符号链接配置文件', 409)
    data = read_json(CLIENT_CFG, {})
    if not isinstance(data, dict):
        data = {}
    data.update(patch)
    tmp = CLIENT_CFG.with_name(f'.config.{secrets.token_hex(4)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o644)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        if CLIENT_CFG.is_symlink():
            raise ApiError('拒绝写入符号链接配置文件', 409)
        os.replace(tmp, CLIENT_CFG)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _split_alias_tokens(raw_val) -> list[str]:
    if isinstance(raw_val, list):
        items = [str(x) for x in raw_val]
    elif isinstance(raw_val, str):
        items = re.split(r'[,，;；|]+', raw_val)
    else:
        return []
    out = []
    for it in items:
        s = re.sub(r'\s+', ' ', it).strip()
        if s and len(s) <= 40 and s not in out:
            out.append(s)
    return out[:30]


def _normalize_corrections_dict(raw_map) -> dict[str, list[str]]:
    """读取小智 asr_corrections（标准结构为 {正确写法: [错词1, 错词2]}，同时兼容旧的字符串值）。"""
    if not isinstance(raw_map, dict):
        return {}
    out = {}
    taken = set()
    for k, v in list(raw_map.items())[:200]:
        target = re.sub(r'\s+', ' ', str(k or '')).strip()
        if not target or len(target) > 40:
            continue
        aliases = []
        for a in _split_alias_tokens(v):
            al = a.lower()
            if al != target.lower() and al not in taken:
                taken.add(al)
                aliases.append(a)
        if aliases:
            out[target] = aliases
    return out


def _format_corrections_text(corr_map: dict[str, list[str]]) -> str:
    return '\n'.join(f'{target} = {", ".join(aliases)}' for target, aliases in corr_map.items() if aliases)


def _parse_corrections_input(raw_c) -> dict[str, list[str]]:
    """解析同音字纠错词典：支持「正确写法 = 错词1, 错词2」与「错词1, 错词2 -> 正确写法」两种直观写法。"""
    if isinstance(raw_c, dict):
        return _normalize_corrections_dict(raw_c)
    if not isinstance(raw_c, str):
        return {}
    merged_pairs = []
    for ln in raw_c.splitlines()[:200]:
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        arrow_hit = None
        for sep in ('->', '→'):
            if sep in ln:
                left, right = ln.split(sep, 1)
                arrow_hit = (right, left)   # 错词 -> 正确写法
                break
        if arrow_hit is not None:
            merged_pairs.append(arrow_hit)
            continue
        for sep in ('=', ':', '：'):
            if sep in ln:
                left, right = ln.split(sep, 1)
                merged_pairs.append((left, right))   # 正确写法 = 错词1, 错词2
                break
    out = {}
    taken = set()
    for raw_t, raw_a in merged_pairs:
        target = re.sub(r'\s+', ' ', str(raw_t or '')).strip()
        if not target or len(target) > 40:
            continue
        bucket = out.setdefault(target, [])
        for a in _split_alias_tokens(raw_a):
            al = a.lower()
            if al != target.lower() and al not in taken:
                taken.add(al)
                bucket.append(a)
        if not bucket:
            out.pop(target, None)
    return out


def _parse_hotwords_input(raw_h) -> list[str]:
    if isinstance(raw_h, list):
        tokens = [str(x) for x in raw_h]
    elif isinstance(raw_h, str):
        tokens = re.split(r'[\r\n,，;；]+', raw_h)
    else:
        return []
    out, seen = [], set()
    for t in tokens:
        w = re.sub(r'\s+', ' ', t).strip()
        if not w or len(w) > 40:
            continue
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            out.append(w)
            if len(out) >= 200:
                break
    return out


def _claude_cli_level_of(entry: dict) -> str:
    mode = str(entry.get('permission_mode') or 'default')
    tools = [str(t) for t in (entry.get('allowed_tools') or [])]
    core = sorted(t for t in tools if t not in CLAUDE_WEB_TOOLS)
    for lv, (lv_mode, lv_tools) in CLAUDE_CLI_LEVELS.items():
        if mode == lv_mode and core == sorted(lv_tools):
            return lv
    return 'read'


def _find_claude_cli(custom_path: str = '') -> str:
    if CLAUDE_CLI_HINT == '__NONE__':
        return ''
    cands = []
    if CLAUDE_CLI_HINT:
        cands.append(CLAUDE_CLI_HINT)
    if custom_path:
        cands.append(os.path.expanduser(custom_path))
    w = shutil.which('claude')
    if w:
        cands.append(w)
    cands.extend([
        '/home/ubuntu/.local/bin/claude',
        '/home/ubuntu/.npm-global/bin/claude',
        '/usr/local/bin/claude',
        '/usr/bin/claude',
    ])
    for c in cands:
        try:
            p = Path(c)
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
        except OSError:
            continue
    return ''


def _seal_study_envelope(secret_bytes: bytes, obj: dict) -> dict:
    """与 thinksmart-study.py 完全一致的 SHAKE256 + HMAC-SHA256 端到端密封信封，用于实测密钥匹配。"""
    enc = hmac.new(secret_bytes, b'study-vision enc req', hashlib.sha256).digest()
    mac = hmac.new(secret_bytes, b'study-vision mac req', hashlib.sha256).digest()
    nonce, ts = os.urandom(16), int(time.time())
    plain = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    if plain:
        ks = hashlib.shake_256(enc + nonce).digest(len(plain))
        ct = (int.from_bytes(plain, 'big') ^ int.from_bytes(ks, 'big')).to_bytes(len(plain), 'big')
    else:
        ct = b''
    tag = hmac.new(mac, b'v1|%d|' % ts + nonce + ct, hashlib.sha256).digest()
    return {
        'v': 1,
        'ts': ts,
        'nonce': base64.b64encode(nonce).decode('ascii'),
        'ct': base64.b64encode(ct).decode('ascii'),
        'tag': base64.b64encode(tag).decode('ascii'),
    }


def _clear_study_voice_cache() -> None:
    """当学习陪伴提醒 TTS 地址或音色变更时，清理旧的已缓存语音片段，使新音色立即生效。"""
    vdir = STUDY_STATE / 'voice'
    if not vdir.is_dir() or vdir.is_symlink():
        return
    with contextlib.suppress(OSError):
        for p in vdir.glob('*.pcm'):
            if p.is_file() and not p.is_symlink():
                with contextlib.suppress(OSError):
                    p.unlink()


def _resolve_call_entry(raw_k: str) -> str:
    s = str(raw_k or '').strip()
    if not s:
        return ''
    hit = _CALL_ENTRY_LOOKUP.get(s.lower())
    if hit:
        return hit
    if re.fullmatch(r'GrokBot_[\w\u4e00-\u9fff]{1,48}', s):
        return s
    return ''


def _normalize_call_learning(ln: dict) -> dict:
    if not isinstance(ln, dict):
        return {'active': False, 'target': None, 'left': 0, 'need': 3, 'got': [], 'samples': [], 'remaining': 0, 'seconds_left': 0}
    raw_samples = ln.get('samples') if isinstance(ln.get('samples'), list) else (ln.get('got') if isinstance(ln.get('got'), list) else [])
    samples = []
    for s in raw_samples[:10]:
        if isinstance(s, dict):
            samples.append({'text': str(s.get('text') or '')[:80], 'alias': str(s.get('alias') or '')[:40]})
    need = int(ln.get('count') or ln.get('need') or 3)
    rem = int(ln['seconds_left']) if isinstance(ln.get('seconds_left'), (int, float)) else int(ln.get('remaining') or 0)
    left = int(ln['left']) if isinstance(ln.get('left'), (int, float)) else max(0, need - len(samples))
    return {
        'active': bool(ln.get('active')),
        'target': ln.get('target'),
        'left': max(0, left),
        'need': max(1, need),
        'got': samples,
        'samples': samples,
        'remaining': max(0, rem),
        'seconds_left': max(0, rem),
    }


def _synth_marimba_wav() -> bytes:
    """合成与机身 alarms.py 清晨木琴完全一致的 8 音符木琴旋律（24kHz PCM16 WAV，仅浏览器试听，机身扬声器零出声）。"""
    sr = 24000
    n_tot = int(sr * 1.6)
    acc = [0.0] * n_tot
    notes = (1046.5, 1318.5, 1568.0, 2093.0, 1568.0, 1318.5, 1046.5, 1568.0)
    n_bar = int(sr * 0.6)
    two_pi = 2.0 * math.pi
    for idx, freq in enumerate(notes):
        st = int(sr * 0.13 * idx)
        for i in range(min(n_bar, n_tot - st)):
            t = i / sr
            att = min(1.0, t / 0.0015)
            v = att * (
                math.sin(two_pi * freq * t) * math.exp(-6.0 * t)
                + 0.35 * math.sin(two_pi * 4.0 * freq * t) * math.exp(-26.0 * t)
                + 0.10 * math.sin(two_pi * 9.9 * freq * t) * math.exp(-40.0 * t)
            )
            acc[st + i] += v
    pk = max((abs(x) for x in acc), default=1.0) or 1.0
    scale = 0.6 * 32767.0 / pk
    pcm = struct.pack(f'<{n_tot}h', *(max(-32767, min(32767, int(x * scale))) for x in acc))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


def _normalize_call_models(raw_m) -> dict[str, list[str]]:
    if not isinstance(raw_m, dict):
        return {}
    out, taken = {}, set()
    for raw_k, raw_v in list(raw_m.items())[:25]:
        ent = _resolve_call_entry(raw_k)
        if not ent:
            continue
        vals = raw_v if isinstance(raw_v, list) else [str(raw_v or '')]
        bucket = out.setdefault(ent, [])
        for it in vals:
            for tok in re.split(r'[,，;；|]+', str(it or '')):
                a = _CALL_ALIAS_BAD.sub('', tok.strip()).lower()
                if a and len(a) <= 24 and a not in taken:
                    taken.add(a)
                    bucket.append(a)
        if not bucket:
            out.pop(ent, None)
    return out


def _format_call_models_text(m_map: dict[str, list[str]]) -> str:
    return '\n'.join(f'{ent} = {", ".join(als)}' for ent, als in m_map.items() if als)


def _parse_call_models_input(raw_c) -> dict[str, list[str]]:
    if isinstance(raw_c, dict):
        return _normalize_call_models(raw_c)
    if not isinstance(raw_c, str):
        return {}
    raw_dict = {}
    for ln in raw_c.splitlines()[:30]:
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        for sep in ('=', ':', '：', '->', '→'):
            if sep in ln:
                left, right = ln.split(sep, 1)
                ent = _resolve_call_entry(left.strip())
                if not ent:
                    raise ApiError(f'未知的模型标识「{left.strip()[:30]}」，请使用如 DeepSeekLLM / OpenAILLM / GeminiAPI / ClaudeAPI / GrokBot_*')
                raw_dict.setdefault(ent, []).extend(re.split(r'[,，;；|]+', right))
                break
    return _normalize_call_models(raw_dict)


def _parse_news_sources(raw_s) -> str:
    parts = [x.strip() for x in re.split(r'[;；,，、\r\n]+', str(raw_s or '')) if x.strip()]
    if not parts:
        return '澎湃新闻;百度热搜;财联社'
    out = []
    for p in parts[:20]:
        if p not in NEWSNOW_CHANNEL_MAP:
            raise ApiError(f'不支持的新闻源「{p[:24]}」，请从下方快捷频道胶囊点选')
        if p not in out:
            out.append(p)
    return ';'.join(out)


def _sqlite_read_memory(db_path: Path, pref_dev: str = '') -> dict:
    if not db_path.is_file() or db_path.is_symlink():
        return {'device': pref_dev or 'default', 'summary': '', 'version': 0, 'updated': None, 'source': ''}
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2.0)
        try:
            rows = con.execute('SELECT device, summary, version, source, updated FROM device_memory ORDER BY updated DESC').fetchall()
        finally:
            con.close()
        chosen = None
        if pref_dev:
            chosen = next((r for r in rows if r[0] == pref_dev), None)
        if chosen is None:
            chosen = next((r for r in rows if 'selftest' not in str(r[0])), None) or (rows[0] if rows else None)
        if chosen:
            return {'device': str(chosen[0]), 'summary': str(chosen[1] or ''), 'version': int(chosen[2] or 0),
                    'source': str(chosen[3] or ''), 'updated': chosen[4]}
    except Exception:
        pass
    return {'device': pref_dev or 'default', 'summary': '', 'version': 0, 'updated': None, 'source': ''}


def _sqlite_write_memory(db_path: Path, device: str, summary: str, expect_version: int | None) -> tuple[bool, dict]:
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if db_path.is_symlink():
        raise ApiError('拒绝写入符号链接数据库', 409)
    db_path.touch(mode=0o600, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=3.0, isolation_level=None)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute(
            'CREATE TABLE IF NOT EXISTS device_memory ('
            'device TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT "", '
            'version INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT "", updated REAL NOT NULL)'
        )
        con.execute('BEGIN IMMEDIATE')
        try:
            row = con.execute('SELECT summary, version, source, updated FROM device_memory WHERE device=?', (device,)).fetchone()
            cur_ver = int(row[1]) if row else 0
            if expect_version is not None and cur_ver != expect_version:
                con.execute('ROLLBACK')
                return False, {'summary': row[0] if row else '', 'version': cur_ver, 'source': row[2] if row else '', 'updated': row[3] if row else None}
            new_ver = cur_ver + 1
            now_ts = time.time()
            src = 'manual' if summary.strip() else 'cleared'
            con.execute(
                'INSERT INTO device_memory(device, summary, version, source, updated) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(device) DO UPDATE SET summary=excluded.summary, version=excluded.version, source=excluded.source, updated=excluded.updated',
                (device, summary, new_ver, src, now_ts),
            )
            con.execute('COMMIT')
            return True, {'summary': summary, 'version': new_ver, 'source': src, 'updated': now_ts}
        except BaseException:
            with contextlib.suppress(Exception):
                con.execute('ROLLBACK')
            raise
    finally:
        con.close()


# ============================== 应用 ==============================
class Admin:
    def __init__(self):
        self.auth = Auth(STATE_DIR)
        self.wan_gate = WanGate(STATE_DIR)
        self.limiter = Limiter()
        self.trash = Trash(STATE_DIR)
        self.secret = mihomo_secret()
        self.sess = None
        self.auth_sem = asyncio.Semaphore(2)
        self.upload_sem = asyncio.Semaphore(3)
        self.inflight_bytes = {'local': 0, 'usb': 0}
        self.fs_lock = threading.Lock()     # 挑名字 + 改名：上传、导入、删除、恢复共用（只在线程里拿，不跨 await）
        self.cfg_lock = threading.RLock()   # 串行读写 .config.yaml 与各服务 JSON 配置
        self.job = None
        self.job_thread = None
        self.ws_count = 0
        self.open_ws = set()
        self.open_clash_reqs = set()
        self.page_cache = {}
        self.last_active_mono = time.monotonic()

    def wan_enabled(self) -> bool:
        return self.wan_gate.enabled()

    def set_wan_enabled(self, on: bool) -> dict:
        rec = self.wan_gate.set_enabled(on)
        if on:
            self.limiter.clear_prefix('wan:')
        return rec

    # ---------- 页面 ----------
    def _read_ui(self, name: str):
        p = UI_DIR / name
        st = p.stat()
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        hit = self.page_cache.get(name)
        if hit and hit[0] == sig:
            return hit[1], hit[2]
        data = p.read_bytes()
        etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
        self.page_cache[name] = (sig, data, etag)
        return data, etag

    async def page(self, request):
        try:
            data, etag = self._read_ui('index.html')
        except OSError:
            return web.Response(status=503, text='管理后台页面文件缺失', content_type='text/plain')
        hdrs = {'Cache-Control': 'no-cache', 'ETag': etag, 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': PAGE_CSP,
                'Permissions-Policy': 'camera=(self), microphone=(self), fullscreen=(self)'}
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers=hdrs)
        return web.Response(body=data, content_type='text/html', charset='utf-8', headers=hdrs)

    async def asset(self, request):
        name = request.match_info['name']
        types = {'favicon.svg': 'image/svg+xml', 'icon.png': 'image/png'}
        if name not in types:
            raise web.HTTPNotFound()
        try:
            data, etag = self._read_ui(name)
        except OSError:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type=types[name],
                            headers={'Cache-Control': 'max-age=86400', 'ETag': etag, 'X-Content-Type-Options': 'nosniff'})

    async def redirect_slash(self, _request):
        raise web.HTTPPermanentRedirect('/admin/')

    # ---------- 登录 ----------
    def _cookie(self, resp, request, token, ttl, remember):
        _host, proto = entry_host_proto(request)
        sec = (proto == 'https')
        resp.set_cookie(COOKIE, token, path='/admin', httponly=True, samesite='Strict',
                        secure=sec, max_age=ttl if remember else None)
        if sec or is_wan_request(request):
            resp.set_cookie(COOKIE_WAN, token, path='/', httponly=True, samesite='Strict',
                            secure=sec, max_age=ttl if remember else None)

    def _clear_cookies(self, resp, request):
        _host, proto = entry_host_proto(request)
        sec = (proto == 'https')
        resp.set_cookie(COOKIE, '', path='/admin', httponly=True, samesite='Strict', secure=sec, max_age=0)
        if sec or COOKIE_WAN in request.cookies or is_wan_request(request):
            resp.set_cookie(COOKIE_WAN, '', path='/', httponly=True, samesite='Strict', secure=sec, max_age=0)

    async def session(self, request):
        rec = self.auth.record()
        on = rec is not None
        vpn = is_vpn_tunnel(request)
        wan_req = is_wan_request(request)
        s = self.auth.check(request.cookies.get(COOKIE)) if on else None
        _host, proto = entry_host_proto(request)
        entry = 'vps' if vpn else ('wan' if wan_req else ('vps' if proto == 'https' else 'lan'))
        logged_in = True if vpn else ((not on and not wan_req) or bool(s))
        return jok(
            logged_in=logged_in,
            password_on=on,
            username=self.auth.username(),
            wan_enabled=self.wan_enabled(),
            wan_url=WAN_PUBLIC_URL,
            need_setup=False,
            entry=entry,
        )

    async def login(self, request):
        data = await json_body(request, 4096)
        uname = data.get('username')
        pw = data.get('password')
        remember = data.get('remember', True) is not False
        key = client_key(request)
        rec = self.auth.record()
        if rec is None:
            raise ApiError('没有设置密码，不用登录', 409)
        if not isinstance(uname, str) or not uname.strip() or not isinstance(pw, str) or not pw or len(pw) > 256:
            raise ApiError('请输入账号和密码')
        exp_user = str(rec.get('username') or 'admin').strip() or 'admin'
        async with self.auth_sem:
            wait = self.limiter.wait(key)
            if wait:
                raise ApiError(f'输错次数太多，请 {max(1, (wait + 59) // 60)} 分钟后再试', 429)
            pw_ok = await asyncio.get_running_loop().run_in_executor(None, verify_password, pw, rec)
            user_ok = hmac.compare_digest(uname.strip().encode('utf-8'), exp_user.encode('utf-8'))
            if not (pw_ok and user_ok):
                self.limiter.fail(key)
                log.info('login failed (%s)', 'proxied' if key != 'direct' else 'direct')
                await asyncio.sleep(0.6)
                raise ApiError('账号或密码不对', 403)
            self.limiter.ok(key)
        token, ttl = self.auth.create(remember, request.headers.get('User-Agent', ''))
        resp = jok(username=exp_user, wan_enabled=self.wan_enabled())
        self._cookie(resp, request, token, ttl, remember)
        log.info('login ok')
        return resp

    async def logout(self, request):
        data = await json_body(request, 1024)
        tok = request.cookies.get(COOKIE)
        if data.get('all'):
            self.auth.revoke_all()
        else:
            self.auth.revoke(tok)
        resp = jok()
        self._clear_cookies(resp, request)
        return resp

    async def change_password(self, request):
        data = await json_body(request, 4096)
        old, new = data.get('old'), data.get('new')
        remove = bool(data.get('remove'))
        key = client_key(request)
        wait = self.limiter.wait(key)
        if wait:
            raise ApiError(f'输错次数太多，请 {max(1, (wait + 59) // 60)} 分钟后再试', 429)
        rec = self.auth.record()
        if remove:
            if self.wan_enabled():
                raise ApiError('请先关闭外网访问，再取消登录密码', 400)
            if rec is None:
                return jok(password_on=False)
            async with self.auth_sem:
                wait2 = self.limiter.wait(key)
                if wait2:
                    raise ApiError(f'输错次数太多，请 {max(1, (wait2 + 59) // 60)} 分钟后再试', 429)
                ok = isinstance(old, str) and await asyncio.get_running_loop().run_in_executor(None, verify_password, old, rec)
                if not ok:
                    self.limiter.fail(key)
                    raise ApiError('原密码不对', 403)
                self.limiter.ok(key)
            self.auth.clear_all()
            resp = jok(password_on=False)
            self._clear_cookies(resp, request)
            log.info('password removed from the page')
            return resp
        if rec is None:
            if not isinstance(new, str) or len(new) < MIN_PASSWORD or len(new) > 128:
                raise ApiError(f'新密码至少 {MIN_PASSWORD} 位')
            new_user = clean_username(data['username']) if (isinstance(data.get('username'), str) and data['username'].strip()) else 'admin'
            async with self.auth_sem:
                if self.auth.record() is not None:
                    raise ApiError('密码刚刚已经设置过了，请刷新页面', 409)
                new_rec = await asyncio.get_running_loop().run_in_executor(None, hash_password, new, new_user)
            token, ttl = self.auth.enable_password(new_rec, request.headers.get('User-Agent', ''))
            resp = jok(password_on=True, username=new_user)
            self._cookie(resp, request, token, ttl, True)
            log.info('password enabled from the page')
            return resp
        cur_user = self.auth.username()
        new_user = clean_username(data['username']) if (isinstance(data.get('username'), str) and data['username'].strip()) else cur_user
        want_pw = isinstance(new, str) and new != ''
        if want_pw and (len(new) < MIN_PASSWORD or len(new) > 128):
            raise ApiError(f'新密码至少 {MIN_PASSWORD} 位')
        if not want_pw and new_user == cur_user:
            raise ApiError(f'新密码至少 {MIN_PASSWORD} 位')
        async with self.auth_sem:
            wait2 = self.limiter.wait(key)
            if wait2:
                raise ApiError(f'输错次数太多，请 {max(1, (wait2 + 59) // 60)} 分钟后再试', 429)
            ok = isinstance(old, str) and await asyncio.get_running_loop().run_in_executor(None, verify_password, old, rec)
            if not ok:
                self.limiter.fail(key)
                raise ApiError('原密码不对', 403)
            self.limiter.ok(key)
            if want_pw:
                new_rec = await asyncio.get_running_loop().run_in_executor(None, hash_password, new, new_user)
            else:
                new_rec = dict(rec)
                new_rec['username'] = new_user
                new_rec['set_at'] = int(time.time())
        self.auth.set_password_record(new_rec, keep_token=request.cookies.get(COOKIE))    # 其他设备要重新登录
        log.info('password updated from the page')
        return jok(password_on=True, username=new_user)

    async def set_wan_access(self, request):
        data = await json_body(request, 1024)
        enabled = bool(data.get('enabled'))
        if enabled and self.auth.record() is None:
            raise ApiError('开启外网访问前请先设置后台登录账号与密码', 400)
        self.set_wan_enabled(enabled)
        log.info('wan_access changed: enabled=%s', enabled)
        return jok(wan_enabled=enabled, wan_url=WAN_PUBLIC_URL)

    # ---------- 歌曲 ----------
    async def songs(self, request):
        loop = asyncio.get_running_loop()
        roots = {}
        files, folders = [], {}
        for key in ('local', 'hub', 'usb'):
            base = root_base(key)
            if base is None:
                continue
            fs, fd = await loop.run_in_executor(None, scan_root, key, base)
            files.extend(fs)
            folders[key] = fd
        local_base = root_base('local') or root_base('hub')
        sys_space = await loop.run_in_executor(None, root_space, 'local', local_base) if local_base else None
        roots['local'] = {'ok': local_base is not None, 'writable': root_writable(root_base('local')),
                          'space': sys_space, 'reserve': RESERVE_SYSTEM}
        roots['hub'] = {'ok': root_base('hub') is not None, 'writable': root_writable(root_base('hub'))}
        ub = root_base('usb')
        roots['usb'] = {'ok': ub is not None, 'writable': root_writable(ub),
                        'space': (await loop.run_in_executor(None, root_space, 'usb', ub)) if ub else None}
        trash = await loop.run_in_executor(None, self.trash.listing)
        return jok(files=files, folders=folders, roots=roots, trash=trash, max_upload=MAX_UPLOAD,
                   exts=[e[1:] for e in AUDIO_EXTS], local_dir=str(MUSIC_LOCAL),
                   job=self.job.public() if self.job else None)

    async def upload(self, request):
        q = request.query
        key = q.get('root', 'local')
        if key not in ('local', 'usb'):
            raise ApiError('只能传到本机或 U 盘')
        base = need_base(key)
        if not root_writable(base):
            raise ApiError('U 盘是只读的，传不进去' if key == 'usb' else '本机音乐文件夹不能写入', 409)
        tdir = resolve_dir(key, q.get('dir', ''))
        name = clean_song_name(q.get('name', ''))
        ext = os.path.splitext(name)[1].lower()
        try:
            size = int(q.get('size') or request.content_length or 0)
        except ValueError:
            size = 0
        if size <= 0:
            raise ApiError('文件是空的')
        if size > MAX_UPLOAD:
            raise ApiError('单个文件最大 1 GB', 413)
        where = 'U 盘' if key == 'usb' else '本机'
        job_inf = lambda: (self.job.inflight_remaining if (key == 'local' and self.job and self.job.state == 'running') else 0)
        space = root_space(key, base)
        avail_pre = max(0, space['usable'] - self.inflight_bytes.get(key, 0) - job_inf())
        if size > avail_pre:
            raise ApiError(f'{where}空间不够：还能放 {fmt_size(avail_pre)}，这首 {fmt_size(size)}', 507)
        loop = asyncio.get_running_loop()
        async with self.upload_sem:
            space_now = await loop.run_in_executor(None, root_space, key, base)
            avail_now = max(0, space_now['usable'] - self.inflight_bytes.get(key, 0) - job_inf())
            if size > avail_now:
                raise ApiError(f'{where}空间不够：还能放 {fmt_size(avail_now)}，这首 {fmt_size(size)}', 507)
            reserved = size
            self.inflight_bytes[key] = self.inflight_bytes.get(key, 0) + reserved
            tmp = None
            fd_lock = threading.Lock()
            fd_box = [-1]
            aborted = [False]
            got, head, checked, done = 0, b'', False, False
            final = None
            sniff_need = min(size, 4096)

            def _open_part(tmp_path: Path) -> None:
                with fd_lock:
                    if aborted[0]:
                        raise OSError(errno.ECANCELED, 'upload cancelled')
                    fd_box[0] = os.open(
                        tmp_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                        0o644,
                    )

            def _write_part(data: bytes) -> None:
                with fd_lock:
                    if aborted[0] or fd_box[0] < 0:
                        raise OSError(errno.ECANCELED, 'upload cancelled')
                    _write_all(fd_box[0], data)

            def _finish_part() -> None:
                with fd_lock:
                    if aborted[0] or fd_box[0] < 0:
                        raise OSError(errno.ECANCELED, 'upload cancelled')
                    _sync_and_drop_cache(fd_box[0])
                    os.close(fd_box[0])
                    fd_box[0] = -1

            def _cleanup_upload_part(unlink_tmp: bool, tmp_path: Path | None) -> None:
                with fd_lock:
                    aborted[0] = True
                    if fd_box[0] >= 0:
                        with contextlib.suppress(OSError):
                            os.close(fd_box[0])
                        fd_box[0] = -1
                    if unlink_tmp and tmp_path is not None:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp_path)

            try:
                tmp = await loop.run_in_executor(None, _incoming, base, f'.upload-{secrets.token_hex(6)}.part')
                await loop.run_in_executor(None, _open_part, tmp)
                async for chunk in request.content.iter_chunked(256 * 1024):
                    if not chunk:
                        continue
                    got += len(chunk)
                    if got > size:
                        raise ApiError('文件比说好的大，已停止')
                    if not checked:
                        head += chunk[:sniff_need - len(head)]
                        if len(head) >= sniff_need:
                            checked = True
                            if not sniff_ok(ext, head):
                                raise ApiError('这不像音乐文件（内容和扩展名对不上）', 415)
                    await loop.run_in_executor(None, _write_part, chunk)
                    step = min(reserved, len(chunk))
                    if step:
                        reserved -= step
                        self.inflight_bytes[key] = max(0, self.inflight_bytes.get(key, 0) - step)
                if got != size:
                    raise ApiError('没有传完，请重试')
                if not checked and not sniff_ok(ext, head):
                    raise ApiError('这不像音乐文件（内容和扩展名对不上）', 415)
                await loop.run_in_executor(None, _finish_part)
                final = await loop.run_in_executor(None, self._place, tmp, tdir, name)
                done = True
            except (aiohttp.ClientPayloadError, ConnectionError):
                raise ApiError('没有传完，请重试', 400)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    raise ApiError(f'{where}空间不够了，已停止写入', 507)
                raise ApiError(f'{where}写入失败，请检查存储设备是否正常', 409)
            finally:
                if fd_box[0] >= 0 or not done:
                    await asyncio.shield(loop.run_in_executor(None, _cleanup_upload_part, not done, tmp))
                if reserved > 0:
                    self.inflight_bytes[key] = max(0, self.inflight_bytes.get(key, 0) - reserved)
        _touch_local_sig(key, base)
        log.info('upload ok: %s (%s, %s)', final.name, key, fmt_size(got))
        return jok(root=key, path=rel_of(key, final), name=final.name, renamed=final.name != name, size=got)

    async def song_file(self, request):
        q = request.query
        p = resolve_file(q.get('root', ''), q.get('path', ''))
        hdrs = {'Cache-Control': 'private, no-cache', 'X-Content-Type-Options': 'nosniff',
                'Content-Type': MIME.get(p.suffix.lower(), 'application/octet-stream')}
        if q.get('dl') == '1':
            hdrs['Content-Disposition'] = content_disposition(p.name)
        return web.FileResponse(p, headers=hdrs)

    def _place(self, tmp: Path, tdir: Path, name: str) -> Path:
        with self.fs_lock:
            final = unique_path(tdir, name)
            os.rename(tmp, final)
        return final

    def _locked(self, fn, *args):
        with self.fs_lock:
            return fn(*args)

    def _need_writable(self, key):
        base = need_base(key)
        if not root_writable(base):
            raise ApiError('U 盘是只读的，改不了' if key == 'usb' else '这个文件夹不能写入', 409)
        return base

    async def delete(self, request):
        """删一首（root + path）或一批（items: [{root, path}]）：都挪进「最近删除」，7 天内能恢复。"""
        d = await json_body(request, 1024 * 1024)
        raw = d.get('items')
        if raw is None:
            raw = [{'root': d.get('root', ''), 'path': d.get('path')}]
        if not isinstance(raw, list) or not raw or len(raw) > 5000:
            raise ApiError('没有要删的歌')
        entries, failed, seen_del = [], [], set()
        for it in raw:
            if not isinstance(it, dict):
                continue
            key = it.get('root', '')
            try:
                base = self._need_writable(key)
                pf = resolve_file(key, it.get('path'))
                dk = (key, str(pf))
                if dk in seen_del:
                    continue
                seen_del.add(dk)
                entries.append((key, base, pf))
            except ApiError as e:
                failed.append({'path': str(it.get('path', ''))[:200], 'error': e.msg})
        done, more = await asyncio.get_running_loop().run_in_executor(None, self._locked, self.trash.put_many, entries)
        failed += more
        if not done:
            raise ApiError(failed[0]['error'] if failed else '没有要删的歌', 409)
        log.info('deleted to trash: %d song(s)%s', len(done), f', {len(failed)} failed' if failed else '')
        return jok(ids=[x['id'] for x in done], id=done[0]['id'], name=done[0]['name'], deleted=len(done), failed=failed)

    async def restore(self, request):
        d = await json_body(request, 512 * 1024)
        ids = d.get('ids')
        if ids is None:
            ids = [d.get('id')]
        if (not isinstance(ids, list) or not ids or len(ids) > 5000
                or not all(isinstance(x, str) and TRASH_RE.fullmatch(f'{x}.deleted') for x in ids)):
            raise ApiError('找不到这首歌')
        done, failed = await asyncio.get_running_loop().run_in_executor(None, self._locked, self.trash.restore_many, ids)
        if not done:
            raise ApiError(failed[0]['error'] if failed else '找不到这首歌', 404)
        return jok(restored=len(done), failed=failed, items=done, **done[0])

    async def purge(self, request):
        d = await json_body(request)
        ids = d.get('ids')
        everything = d.get('all') is True
        if not everything:
            if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and TRASH_RE.fullmatch(f'{x}.deleted') for x in ids):
                raise ApiError('没有要清掉的歌')
        n = await asyncio.get_running_loop().run_in_executor(None, lambda: self._locked(self.trash.purge, set(ids or []), everything))
        return jok(removed=n)

    async def rename(self, request):
        d = await json_body(request)
        key = d.get('root', '')
        self._need_writable(key)
        p = resolve_file(key, d.get('path'))
        new = clean_song_name(d.get('name', ''), keep_ext=p.suffix)
        if new == p.name:
            return jok(path=rel_of(key, p), name=p.name)
        target = p.parent / new

        def _do():
            with self.fs_lock:
                if os.path.lexists(target):
                    same = False
                    with contextlib.suppress(OSError):
                        same = os.path.samefile(target, p)          # 只改大小写（U 盘不分大小写）
                    if not same:
                        raise ApiError('这个文件夹里已经有同名的歌了', 409)
                os.rename(p, target)
        try:
            await asyncio.get_running_loop().run_in_executor(None, _do)
        except OSError:
            raise ApiError('改名失败，请检查存储设备是否只读', 409)
        _touch_local_sig(key, root_base(key))
        return jok(path=rel_of(key, target), name=target.name)

    async def move(self, request):
        d = await json_body(request)
        key = d.get('root', '')
        self._need_writable(key)
        p = resolve_file(key, d.get('path'))
        tdir = resolve_dir(key, d.get('dir', ''))
        if tdir == p.parent:
            return jok(path=rel_of(key, p), name=p.name)
        try:
            final = await asyncio.get_running_loop().run_in_executor(None, self._place, p, tdir, p.name)
        except OSError:
            raise ApiError('移动失败，请检查存储设备是否正常', 409)
        _touch_local_sig(key, root_base(key))
        return jok(path=rel_of(key, final), name=final.name, renamed=final.name != p.name)

    async def mkdir(self, request):
        d = await json_body(request)
        key = d.get('root', '')
        if key not in ('local', 'usb'):
            raise ApiError('只能在本机或 U 盘里建文件夹')
        self._need_writable(key)
        parent = resolve_dir(key, d.get('parent', ''))
        if len(parent.relative_to(need_base(key)).parts) >= 3:
            raise ApiError('文件夹层数太多了')
        name = clean_folder_name(d.get('name', ''))
        p = parent / name
        if os.path.lexists(p):
            raise ApiError('已经有这个文件夹了', 409)
        try:
            os.mkdir(p, 0o755)
        except OSError:
            raise ApiError('新建文件夹失败，请检查存储空间或权限', 409)
        return jok(dir=rel_of(key, p))

    async def rmdir(self, request):
        d = await json_body(request)
        key = d.get('root', '')
        self._need_writable(key)
        parts = _check_rel(d.get('dir', ''))
        p = _walk_no_links(need_base(key), parts)
        if not p.is_dir():
            raise ApiError('找不到这个文件夹', 404)
        try:
            os.rmdir(p)
        except OSError:
            raise ApiError('文件夹里还有东西，先把歌删掉或移走', 409)
        return jok()

    @staticmethod
    def _track(key: str, p: Path, base: Path) -> dict:
        folder = p.parent
        return {'title': p.stem, 'artist': 'U盘' if key == 'usb' else '本地音乐',
                'album': folder.name if folder != base else ('U盘' if key == 'usb' else '本地音乐'),
                'source': 'U盘无损/本地' if key == 'usb' else '本地音乐', 'quality': p.suffix[1:].upper(),
                'url': str(p), 'is_local': True, 'duration': 0, 'cover': ''}

    async def play(self, request):
        d = await json_body(request, 512 * 1024)
        items = d.get('items')
        if isinstance(items, list) and items:
            tracks = []
            for it in items[:200]:
                if isinstance(it, dict):
                    with contextlib.suppress(ApiError):
                        k = it.get('root', '')
                        tracks.append(self._track(k, resolve_file(k, it.get('path')), need_base(k)))
            if not tracks:
                raise ApiError('选中的歌都找不到了，请刷新', 404)
            res = await self.qq('POST', '/api/music/play_tracks', {'tracks': tracks, 'index': 0, 'open_ui': False})
            if not res.get('ok', True) and res.get('error'):
                raise ApiError(str(res.get('error'))[:80], 409)
            return jok(title=tracks[0]['title'], count=len(tracks))
        key = d.get('root', '')
        p = resolve_file(key, d.get('path'))
        base = need_base(key)
        folder = p.parent

        def _queue():
            names = []
            with os.scandir(folder) as it:
                for e in it:
                    if _skip_name(e.name) or e.is_symlink():
                        continue
                    if os.path.splitext(e.name)[1].lower() in AUDIO_EXTS and e.is_file(follow_symlinks=False):
                        names.append(e.name)
            return sorted(names, key=_natural_key)

        names = await asyncio.get_running_loop().run_in_executor(None, _queue)
        if p.name not in names:
            names.append(p.name)
        idx = names.index(p.name)
        album = folder.name if folder != base else ('U盘' if key == 'usb' else '本地音乐')
        tracks = [{'title': os.path.splitext(n)[0], 'artist': 'U盘' if key == 'usb' else '本地音乐', 'album': album,
                   'source': 'U盘无损/本地' if key == 'usb' else '本地音乐', 'quality': os.path.splitext(n)[1][1:].upper(),
                   'url': str(folder / n), 'is_local': True, 'duration': 0, 'cover': ''} for n in names]
        if len(tracks) > 200:
            start = max(0, min(idx - 100, len(tracks) - 200))
            tracks, idx = tracks[start:start + 200], idx - start
        res = await self.qq('POST', '/api/music/play_tracks', {'tracks': tracks, 'index': idx, 'open_ui': False})
        if not res.get('ok', True) and res.get('error'):
            raise ApiError(str(res.get('error'))[:80], 409)
        return jok(title=os.path.splitext(p.name)[0])

    async def qq(self, method, path, body=None):
        try:
            async with self.sess.request(method, QQ_API + path, json=body, timeout=aiohttp.ClientTimeout(total=12)) as r:
                raw = await read_resp_bounded(r, 1024 * 1024)
                try:
                    j = json.loads(raw.decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    j = {}
                if r.status >= 400:
                    raise ApiError((j.get('error') if isinstance(j, dict) else '') or '大屏音乐服务没有响应', 502)
                return j if isinstance(j, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ApiError('大屏音乐服务暂时连不上', 502)

    async def music_status(self, _request):
        j = await self.qq('GET', '/api/music/status?lite=1')
        t = j.get('track') if isinstance(j.get('track'), dict) else {}
        amb = j.get('ambient') if isinstance(j.get('ambient'), dict) else {}
        amb_lbl = amb.get('name') or amb.get('primary_name') or amb.get('label') or ''
        if (not j.get('playing')) and amb_lbl:
            return jok(playing=True, paused=False, title=amb_lbl,
                       artist='助眠声景', local=False, live=True,
                       elapsed=0, duration=0, volume=j.get('volume'))
        return jok(playing=bool(j.get('playing')), paused=bool(j.get('paused')), title=t.get('title', ''),
                   artist=t.get('artist', ''), local=bool(t.get('file') or t.get('is_local')), live=bool(t.get('is_live')),
                   elapsed=j.get('elapsed') or 0, duration=j.get('duration') or 0, volume=j.get('volume'))

    async def music_control(self, request):
        d = await json_body(request, 1024)
        action = d.get('action')
        if action not in ('pause', 'resume', 'next', 'prev', 'stop'):
            raise ApiError('不支持的操作')
        await self.qq('POST', '/api/music/control', {'action': action, 'source': 'admin'})
        return jok()

    # ---------- U 盘导入本机 ----------
    async def import_start(self, request):
        d = await json_body(request, 1024 * 1024)
        if self.job and self.job.state == 'running':
            raise ApiError('正在导入上一批，等它导完再来', 409)
        ubase = need_base('usb')
        if not root_writable(MUSIC_LOCAL):
            raise ApiError('本机音乐文件夹不能写入', 409)
        loop = asyncio.get_running_loop()
        if d.get('all') is True:
            files, _ = await loop.run_in_executor(None, scan_root, 'usb', ubase, 20000)
            rels = [f['path'] for f in files]
        else:
            rels = d.get('paths')
            if not isinstance(rels, list) or not rels or len(rels) > 20000:
                raise ApiError('没有要导入的歌')

        def _collect():
            items = []
            seen_need = set()
            reserved_keys = []
            need = 0
            for rel in rels:
                if not isinstance(rel, str):
                    continue
                with contextlib.suppress(ApiError, OSError):
                    p = resolve_file('usb', rel)
                    sz = p.stat().st_size
                    if sz <= 0:
                        continue
                    nm = clean_song_name(p.name)
                    items.append((p, nm, sz))
            items.sort(key=lambda x: _natural_key(str(x[0])))
            for _p, nm, sz in items:
                key = (nm.lower(), sz)
                if key not in seen_need and not _already_in_dir(MUSIC_LOCAL, nm, sz):
                    seen_need.add(key)
                    reserved_keys.append(key)
                    need += sz
            return items, need, reserved_keys, root_space('local', MUSIC_LOCAL)['usable']

        items, need, reserved_keys, usable = await loop.run_in_executor(None, _collect)
        if self.job and self.job.state == 'running':
            raise ApiError('正在导入上一批，等它导完再来', 409)
        usable_eff = max(0, usable - self.inflight_bytes.get('local', 0))
        if not items:
            raise ApiError('选中的歌在 U 盘上找不到了，请刷新', 404)
        if need > usable_eff:
            raise ApiError(f'本机空间不够：还能放 {fmt_size(usable_eff)}，这些歌一共 {fmt_size(need)}，请少选一些', 507)
        job = ImportJob(items, self.fs_lock, self.inflight_bytes, need_bytes=need, reserved_keys=reserved_keys)
        self.job = job
        th = threading.Thread(target=job.run, name='usb-import', daemon=True)
        self.job_thread = th
        th.start()
        log.info('import started: %d song(s), %s', job.total, fmt_size(job.total_bytes))
        return jok(job=job.public())

    async def import_status(self, _request):
        return jok(job=self.job.public() if self.job else None)

    async def import_cancel(self, request):
        await json_body(request, 1024)
        if self.job and self.job.state == 'running':
            self.job.cancel = True
        return jok(job=self.job.public() if self.job else None)

    # ---------- Clash ----------
    def _clash_headers(self):
        return {'Authorization': f'Bearer {self.secret}'} if self.secret else {}

    async def clash(self, request):
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self.clash_ws(request)
        tail = clash_tail(request)
        method = request.method
        rx = CLASH_ALLOW.get(method)
        # 按原样（节点名保持百分号编码）对照白名单：名字里的 / 是 %2F，不会被当成路径分隔
        if rx is None or not rx.fullmatch(tail):
            raise ApiError('管理后台不允许这个操作', 403)
        body = b''
        if method in ('PUT', 'PATCH', 'POST', 'DELETE'):
            body = await read_limited(request, 65536)
            if not clash_body_ok(method, tail, body):
                raise ApiError('管理后台不允许改这一项', 403)
        hdrs = self._clash_headers()
        if body:
            hdrs['Content-Type'] = 'application/json'
        is_stream = tail in ('traffic', 'memory', 'logs')
        if is_stream:
            if self.ws_count >= 8:
                raise ApiError('连接太多了', 429)
            self.ws_count += 1
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=4, sock_read=None if is_stream else 40)
        try:
            r = await self.sess.request(method, clash_url(tail, request.query), data=body or None, headers=hdrs,
                                        timeout=timeout, allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if is_stream:
                self.ws_count = max(0, self.ws_count - 1)
            raise ApiError('Clash 没有在运行', 502)
        except BaseException:
            if is_stream:
                self.ws_count = max(0, self.ws_count - 1)
            raise
        resp = None
        eof_ok = False
        watch_disc = None
        self.open_clash_reqs.add(r)
        try:
            if r.status == 401:
                self.secret = mihomo_secret()
                raise ApiError('Clash 的密钥对不上，请重启管理后台服务', 502)
            resp = web.StreamResponse(status=r.status, headers={
                'Content-Type': r.headers.get('Content-Type', 'application/json'), 'Cache-Control': 'no-store',
                'X-Content-Type-Options': 'nosniff'})
            await resp.prepare(request)
            if is_stream:
                async def _watch_client_close():
                    while not r.closed:
                        await asyncio.sleep(5.0)
                        tr = request.transport
                        if tr is None or tr.is_closing():
                            r.close()
                            break
                watch_disc = asyncio.create_task(_watch_client_close())
            async for chunk in r.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            eof_ok = True
            return resp
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError):
            if resp is not None and resp.prepared:
                if request.transport is not None:
                    request.transport.close()
                return resp
            return web.Response(status=502)
        finally:
            if watch_disc is not None and not watch_disc.done():
                watch_disc.cancel()
                with contextlib.suppress(BaseException):
                    await watch_disc
            if is_stream:
                self.ws_count = max(0, self.ws_count - 1)
            self.open_clash_reqs.discard(r)
            if eof_ok:
                r.release()
            else:
                r.close()

    async def clash_ws(self, request):
        tail = clash_tail(request)
        if not CLASH_WS.fullmatch(tail):
            raise ApiError('不支持的请求', 404)
        if self.ws_count >= 8:
            raise ApiError('连接太多了', 429)
        self.ws_count += 1
        up = None
        try:
            try:
                up = await self.sess.ws_connect(clash_url(tail, request.query, ws=True), headers=self._clash_headers(),
                                                heartbeat=30, max_msg_size=4 * 1024 * 1024)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise ApiError('Clash 没有在运行', 502)
            ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
            await ws.prepare(request)
        except BaseException:
            self.ws_count -= 1
            if up is not None:
                with contextlib.suppress(Exception):
                    await up.close()
            raise
        pair = (ws, up)
        self.open_ws.add(pair)
        try:
            await bridge_ws(ws, up)
        finally:
            self.open_ws.discard(pair)
            self.ws_count -= 1
            with contextlib.suppress(Exception):
                await up.close()
            with contextlib.suppress(Exception):
                await ws.close()
        return ws

    async def clash_ui(self, request):
        rel = request.match_info.get('path', '')
        if rel in ('', 'index.html'):
            rel = 'index.html'
        if rel == 'config.js':
            js = ("(function(){\n"
                  "  var u = location.origin + location.pathname.replace(/\\/clash-ui(\\/.*)?$/, '/api/clash');\n"
                  "  window.__METACUBEXD_CONFIG__ = { defaultBackendURL: u, githubToken: '' };\n"
                  "  try {\n"
                  "    var raw = localStorage.getItem('endpointList');\n"
                  "    if (raw) {\n"
                  "      var lst = JSON.parse(raw);\n"
                  "      var hit = Array.isArray(lst) ? lst.find(function(x){ return x && x.url === u; }) : null;\n"
                  "      if (!hit) {\n"
                  "        localStorage.removeItem('endpointList');\n"
                  "        localStorage.removeItem('selectedEndpoint');\n"
                  "      } else if (hit.id && localStorage.getItem('selectedEndpoint') !== JSON.stringify(hit.id)) {\n"
                  "        localStorage.setItem('selectedEndpoint', JSON.stringify(hit.id));\n"
                  "      }\n"
                  "    }\n"
                  "  } catch (e) {}\n"
                  "})();\n")
            return web.Response(text=js, content_type='application/javascript', headers=NO_STORE)
        if rel == 'sw.js' or rel.startswith('workbox-'):
            raise web.HTTPNotFound()            # 不装 service worker，免得缓存住旧页面
        parts = rel.split('/')
        if any(p in ('', '.', '..') for p in parts) or '\\' in rel or '\x00' in rel:
            raise web.HTTPNotFound()
        p = MIHOMO_UI.joinpath(*parts)
        try:
            rp = p.resolve()
            ok = rp.is_relative_to(MIHOMO_UI.resolve()) and rp.is_file()
        except OSError:
            ok = False
        if not ok:
            if '.' in parts[-1]:
                raise web.HTTPNotFound()
            rp = MIHOMO_UI / '200.html' if (MIHOMO_UI / '200.html').is_file() else MIHOMO_UI / 'index.html'
            if not rp.is_file():
                raise web.HTTPNotFound()
        hdrs = {'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'}
        if rp.suffix == '.html':
            hdrs.update({'Content-Security-Policy': CLASH_UI_CSP, 'Cache-Control': 'no-cache', 'X-Frame-Options': 'SAMEORIGIN'})
        elif '_nuxt' in rp.parts:
            hdrs['Cache-Control'] = 'private, max-age=604800, immutable'
        else:
            hdrs['Cache-Control'] = 'private, max-age=3600'
        return web.FileResponse(rp, headers=hdrs)

    # ---------- 接口与模型（统一配置中心） ----------
    def _hub_reload_url(self, merged: dict) -> str:
        if HUB_API != 'http://127.0.0.1:16688':
            return f'{HUB_API}/kiosk/internal/reload'
        hp = (merged.get('server') or {}).get('http_port') or 16688
        try:
            hp_i = int(hp)
        except (TypeError, ValueError):
            hp_i = 16688
        return f'http://127.0.0.1:{hp_i}/kiosk/internal/reload'

    async def _notify_hub_reload(self, sections: list[str], reset_device_llm: bool = False) -> str:
        merged, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
        url = self._hub_reload_url(merged)
        try:
            async with self.sess.post(
                url,
                json={'sections': sections, 'reset_device_llm': reset_device_llm},
                headers={'X-TS-Internal-Reload': '1'},
                timeout=aiohttp.ClientTimeout(total=12, sock_connect=2),
                allow_redirects=False,
            ) as r:
                raw = await read_resp_bounded(r, 65536)
                j = json.loads(raw.decode('utf-8')) if raw else {}
                if r.status == 200 and isinstance(j, dict) and j.get('ok'):
                    res = j.get('results') or {}
                    failed = [f"{k}: {v.get('error')}" for k, v in res.items() if isinstance(v, dict) and v.get('failed')]
                    if failed:
                        return '配置已保存，但热加载提示：' + '；'.join(failed)
                    return '已保存并热生效'
        except Exception:
            pass
        return '已保存（小智网关下次连接或启动时生效）'

    def _hub_diag_url(self, merged: dict) -> str:
        return self._hub_reload_url(merged).replace('/kiosk/internal/reload', '/kiosk/internal/diag')

    @staticmethod
    def _clear_hub_tts_cache_local():
        hub_data = HUB_DIR / 'data'
        if not HUB_DIR.is_symlink() and not hub_data.is_symlink():
            with contextlib.suppress(OSError):
                ww_y = hub_data / '.wakeup_words.yaml'
                if ww_y.exists() and not ww_y.is_symlink() and ww_y.is_file():
                    ww_y.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                cdir = hub_data / 'call_reply_audio'
                if cdir.is_dir() and not cdir.is_symlink():
                    for fp in cdir.glob('*.opus'):
                        if not fp.is_symlink():
                            fp.unlink(missing_ok=True)
        hub_cfg = HUB_DIR / 'config'
        adir = hub_cfg / 'assets' / 'wakeup_words'
        if not HUB_DIR.is_symlink() and not hub_cfg.is_symlink() and not (hub_cfg / 'assets').is_symlink():
            with contextlib.suppress(OSError):
                if adir.is_dir() and not adir.is_symlink():
                    for fp in adir.glob('*.wav'):
                        if not fp.is_symlink():
                            fp.unlink(missing_ok=True)

    async def _clear_hub_tts_cache(self):
        await asyncio.get_running_loop().run_in_executor(None, self._clear_hub_tts_cache_local)
        with contextlib.suppress(Exception):
            merged, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
            async with self.sess.post(
                self._hub_diag_url(merged),
                json={'action': 'clear_tts_cache'},
                headers={'X-TS-Internal-Reload': '1'},
                timeout=aiohttp.ClientTimeout(total=3, sock_connect=1.5),
            ) as r:
                await r.read()

    @staticmethod
    def _resolve_sherpa_dir(shp_a: dict) -> tuple[str, Path]:
        cands = []
        if shp_a.get('model_dir'):
            cands.append(str(shp_a.get('model_dir')))
        cands.extend([
            'models/sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03',
            'models/SenseVoiceSmall',
            'models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17',
        ])
        for rel in cands:
            p = Path(rel) if os.path.isabs(rel) else (HUB_DIR / rel)
            if p.is_dir():
                return rel, p
        first = cands[0]
        return first, (Path(first) if os.path.isabs(first) else (HUB_DIR / first))

    @staticmethod
    def _read_vllm_out(merged: dict, sel: dict) -> dict:
        vllms = merged.get('VLLM') if isinstance(merged.get('VLLM'), dict) else {}
        sel_v = str(sel.get('VLLM') or 'ChatGLMVLLM')
        ent = vllms.get(sel_v) if isinstance(vllms.get(sel_v), dict) else {}
        uf = 'url' if ('url' in ent and 'base_url' not in ent) else 'base_url'
        v_url = clean_text_val(ent.get(uf))
        v_mod = clean_text_val(ent.get('model_name'))
        raw_vk = ent.get('api_key')
        vk_set, vk_hint = (False, '') if str(raw_vk or '').strip() == 'local' else mask_secret(raw_vk)
        if sel_v == 'ChatGLMVLLM' and not vk_set and 'bigmodel.cn' in v_url:
            v_url, v_mod = '', ''
        cloud_need_key = any(d in v_url.lower() for d in ('bigmodel.cn', 'aliyuncs.com', 'volces.com', 'openai.com', 'anthropic.com', 'googleapis.com'))
        return {
            'entry': sel_v,
            'base_url': v_url,
            'model': v_mod,
            'key_set': vk_set,
            'key_hint': vk_hint,
            'configured': bool(v_url and v_mod and (vk_set or not cloud_need_key)),
        }

    def _read_settings_sync(self) -> dict:
        with self.cfg_lock:
            merged, _custom = load_hub_configs()
            sel = merged.get('selected_module') if isinstance(merged.get('selected_module'), dict) else {}
            llms = merged.get('LLM') if isinstance(merged.get('LLM'), dict) else {}
            ttss = merged.get('TTS') if isinstance(merged.get('TTS'), dict) else {}
            asrs = merged.get('ASR') if isinstance(merged.get('ASR'), dict) else {}
            plugins = merged.get('plugins') if isinstance(merged.get('plugins'), dict) else {}
            ccfg = read_json(CLIENT_CFG, {})
            if not isinstance(ccfg, dict):
                ccfg = {}

            # 1) LLM + ClaudeCLI + Chat History + Call Names + Tutor
            sel_llm = str(sel.get('LLM') or 'DeepSeekLLM')
            cur_v = VENDOR_BY_ENTRY.get(sel_llm)
            vendor_list = []
            for v in LLM_VENDORS:
                ent = llms.get(v['entry']) if isinstance(llms.get(v['entry']), dict) else {}
                uf = 'url' if ('url' in ent and 'base_url' not in ent) else 'base_url'
                b_url = clean_text_val(ent.get(uf)) or v['base_url']
                mod = clean_text_val(ent.get('model_name')) or (v['models'][0] if v['models'] else '')
                cands = list(v['models'])
                if mod and mod not in cands:
                    cands.insert(0, mod)
                k_set, k_hint = mask_secret(ent.get('api_key'))
                v_temp = ''
                if ent.get('temperature') not in (None, ''):
                    with contextlib.suppress(TypeError, ValueError):
                        v_temp = round(max(0.0, min(2.0, float(ent.get('temperature')))), 2)
                v_max_tok = ''
                if ent.get('max_tokens') not in (None, ''):
                    with contextlib.suppress(TypeError, ValueError):
                        v_max_tok = max(64, min(32768, int(ent.get('max_tokens'))))
                vendor_list.append({
                    'id': v['id'],
                    'label': v['label'],
                    'entry': v['entry'],
                    'base_url': b_url,
                    'model': mod,
                    'models': cands,
                    'hint': v.get('hint', ''),
                    'temperature': v_temp,
                    'max_tokens': v_max_tok,
                    'key_set': k_set,
                    'key_hint': k_hint,
                    'configured': bool(b_url and mod and (k_set or v['id'] == 'custom')),
                })

            ccli = llms.get('ClaudeCLI') if isinstance(llms.get('ClaudeCLI'), dict) else {}
            ccli_tools = [str(t) for t in (ccli.get('allowed_tools') or [])]
            ccli_cfg_path = clean_text_val(ccli.get('cli_path'))
            ccli_bin = _find_claude_cli(ccli_cfg_path)
            ccli_bg = _int_or(ccli.get('background_after_seconds'), 30, 10, 300)
            ccli_bg_max = _int_or(ccli.get('background_max_seconds'), 7200, 0, 14400)
            ccli_tt = _int_or(ccli.get('turn_timeout_seconds'), 600, 60, 3600)
            ccli_stall = _int_or(ccli.get('stall_timeout_seconds'), 30, 0, 600)
            ccli_idle = _int_or(ccli.get('idle_timeout_seconds'), 300, 0, 3600)
            ccli_res = _int_or(ccli.get('resume_window_minutes'), 120, 0, 1440)
            ccli_fdelay = _int_or(ccli.get('filler_delay_seconds'), 2, 0, 15)
            ccli_p_int = _int_or(ccli.get('progress_interval_seconds'), 30, 0, 300)
            raw_rkw = ccli.get('resume_keywords') if isinstance(ccli.get('resume_keywords'), list) else ['接着上次聊']
            ccli_rkw = [str(x).strip()[:24] for x in raw_rkw[:15] if str(x).strip()] or ['接着上次聊']
            claude_cli_out = {
                'installed': bool(ccli_bin),
                'cli_path': ccli_bin,
                'cli_path_cfg': ccli_cfg_path,
                'model': clean_text_val(ccli.get('model')) or 'haiku',
                'effort': clean_text_val(ccli.get('effort')) or 'low',
                'level': _claude_cli_level_of(ccli),
                'web': all(t in ccli_tools for t in CLAUDE_WEB_TOOLS),
                'workdir': clean_text_val(ccli.get('workdir')) or '~/xiaozhi-workspace',
                'first_reply_filler': str(ccli.get('first_reply_filler') if ccli.get('first_reply_filler') is not None else '好的，我看一下'),
                'filler_delay_seconds': ccli_fdelay,
                'background_after_seconds': ccli_bg,
                'background_max_seconds': ccli_bg_max,
                'turn_timeout_seconds': ccli_tt,
                'stall_timeout_seconds': ccli_stall,
                'idle_timeout_seconds': ccli_idle,
                'resume_window_minutes': ccli_res,
                'resume_keywords': ccli_rkw,
                'resume_keywords_text': '，'.join(ccli_rkw),
                'progress_interval_seconds': ccli_p_int,
                'progress_reply': str(ccli.get('progress_reply') if ccli.get('progress_reply') is not None else '还在处理，请稍等'),
            }

            ch_cfg = merged.get('chat_history') if isinstance(merged.get('chat_history'), dict) else {}
            ch_max = _int_or(ch_cfg.get('max_messages'), 30, 2, 100)
            ch_max_tok = _int_or(ch_cfg.get('max_tokens'), 8000, 1000, 64000)
            ch_disp_lim = _int_or(ch_cfg.get('display_limit'), 100, 20, 500)
            ch_smin = _int_or(ch_cfg.get('summary_min'), 6, 4, 40)
            ch_schars = _int_or(ch_cfg.get('summary_chars'), 500, 200, 3000)
            ch_sllm = str(ch_cfg.get('summary_llm') or '').strip()
            ch_sllm = _resolve_call_entry(ch_cfg.get('summary_llm'))
            chat_history_out = {
                'enabled': bool(ch_cfg.get('enabled', True)),
                'max_messages': ch_max,
                'max_tokens': ch_max_tok,
                'display_limit': ch_disp_lim,
                'summary': bool(ch_cfg.get('summary', True)),
                'summary_min': ch_smin,
                'summary_chars': ch_schars,
                'summary_llm': ch_sllm,
            }

            cn_cfg = merged.get('call_names') if isinstance(merged.get('call_names'), dict) else {}
            cn_models = _normalize_call_models(cn_cfg.get('models'))
            call_names_out = {
                'enabled': bool(cn_cfg.get('enabled', True)),
                'reply': str(cn_cfg.get('reply') or '我在，请说')[:40],
                'models': cn_models,
                'models_text': _format_call_models_text(cn_models),
                'preset_text': _format_call_models_text(dict(CALL_NAME_PRESETS)),
                'learning': _normalize_call_learning(None),
            }
            grok_ents = []
            for gk_name, gk_ent in llms.items():
                if isinstance(gk_ent, dict) and gk_ent.get('type') == 'grok_bot':
                    grok_ents.append({
                        'entry': str(gk_name),
                        'bot_id': str(gk_ent.get('bot_id') or '').strip(),
                        'bot_name': str(gk_ent.get('bot_name') or gk_name).strip(),
                        'aliases': cn_models.get(str(gk_name), []),
                    })
            pref_dev_id = str(ccfg.get('device_id') or '').strip()
            dev_override = None
            ag_sess = read_json(HUB_DIR / 'data' / 'agent_sessions.json', {})
            lm_map = ag_sess.get('last_model') if isinstance(ag_sess, dict) and isinstance(ag_sess.get('last_model'), dict) else {}
            if lm_map:
                lm_rec = lm_map.get(pref_dev_id) if pref_dev_id and pref_dev_id in lm_map else None
                if not isinstance(lm_rec, dict):
                    best_recs = [v for v in lm_map.values() if isinstance(v, dict) and v.get('model')]
                    if best_recs:
                        lm_rec = max(best_recs, key=lambda r: float(r.get('last_active') or 0))
                ov_ent = str((lm_rec or {}).get('model') or '').strip() if isinstance(lm_rec, dict) else ''
                if ov_ent and ov_ent != sel_llm:
                    ov_v = next((vx for vx in vendor_list if vx['entry'] == ov_ent), None)
                    ov_gb = next((gx for gx in grok_ents if gx['entry'] == ov_ent), None)
                    if ov_v:
                        dev_override = {'entry': ov_ent, 'model': ov_ent, 'label': f"{ov_v['label']} · {ov_v['model']}"}
                    elif ov_ent == 'ClaudeCLI':
                        dev_override = {'entry': 'ClaudeCLI', 'model': 'ClaudeCLI', 'label': f"本机 Claude CLI · {claude_cli_out['model']}"}
                    elif ov_gb:
                        dev_override = {'entry': ov_ent, 'model': ov_ent, 'label': f"Grok Bot · {ov_gb['bot_name']}"}

            tutor_prefs = read_json(TUTOR_STATE / 'prefs.json', {})
            if not isinstance(tutor_prefs, dict):
                tutor_prefs = {}
            tm = tutor_prefs.get('tutor_models') if isinstance(tutor_prefs.get('tutor_models'), dict) else {}
            ds_ent = llms.get('DeepSeekLLM') if isinstance(llms.get('DeepSeekLLM'), dict) else {}
            sel_ent = llms.get(sel_llm) if (sel_llm and sel_llm != 'ClaudeCLI' and isinstance(llms.get(sel_llm), dict)) else {}
            xz_cur_mod = clean_text_val(sel_ent.get('model_name'))
            ds_mod = clean_text_val(ds_ent.get('model_name')) or xz_cur_mod or 'deepseek-v4.1-flash'
            try:
                t_rot = int(tutor_prefs.get('cam_rotate') or 0)
            except (TypeError, ValueError):
                t_rot = 0
            if t_rot not in (0, 90, 180, 270):
                t_rot = 0
            try:
                t_zoom = round(float(tutor_prefs.get('cam_zoom') or 1.0), 1)
            except (TypeError, ValueError):
                t_zoom = 1.0
            if t_zoom not in (1.0, 1.5, 2.0):
                t_zoom = 1.0
            tut_hist = read_json(TUTOR_STATE / 'history.json', [])
            tut_mist = read_json(TUTOR_STATE / 'mistake_book.json', [])
            tut_hist_cnt = sum(1 for x in tut_hist if isinstance(x, dict) and x.get('id')) if isinstance(tut_hist, list) else 0
            tut_mist_cnt = sum(1 for x in tut_mist if isinstance(x, dict) and x.get('id')) if isinstance(tut_mist, list) else 0
            llm_out = {
                'selected_entry': sel_llm,
                'device_override': dev_override,
                'mode': 'cli' if sel_llm == 'ClaudeCLI' else 'api',
                'vendor': 'claude_cli' if sel_llm == 'ClaudeCLI' else (cur_v['id'] if cur_v else 'deepseek'),
                'vendors': vendor_list,
                'claude_cli': claude_cli_out,
                'chat_history': chat_history_out,
                'long_memory': _sqlite_read_memory(HUB_DIR / 'data' / 'chat.db', pref_dev_id),
                'call_names': call_names_out,
                'grokbot': {
                    'account': {'logged_in': False, 'needs_login': False, 'expires_at': 0},
                    'login': {'pending': False, 'url': ''},
                    'entries': grok_ents,
                    'bots': None,
                    'bots_error': '',
                },
                'prompt': str(merged.get('prompt') or ''),
                'tutor': {
                    'fast_model': str(tm.get('fast') or xz_cur_mod or 'gpt-6.1-terra'),
                    'deep_model': str(tm.get('deep') or xz_cur_mod or 'gpt-6.1-sol'),
                    'backup_model': str(tm.get('backup') or ds_mod),
                    'mobile_entry_url': str(tutor_prefs.get('mobile_entry_url') or ''),
                    'cam_rotate': t_rot,
                    'cam_zoom': t_zoom,
                    'cam_bright': bool(tutor_prefs.get('cam_bright', False)),
                    'history_count': tut_hist_cnt,
                    'mistake_count': tut_mist_cnt,
                    'presets': list(TUTOR_MODEL_PRESETS),
                },
            }

            # 2) Voice (TTS, ASR, VAD, Hotwords, Corrections & Device Wakeup)
            sel_tts = str(sel.get('TTS') or 'EdgeTTS')
            idx_t = ttss.get('IndexStreamTTS') if isinstance(ttss.get('IndexStreamTTS'), dict) else {}
            edg_t = ttss.get('EdgeTTS') if isinstance(ttss.get('EdgeTTS'), dict) else {}
            hs_t = ttss.get('HuoshanDoubleStreamTTSV2') if isinstance(ttss.get('HuoshanDoubleStreamTTSV2'), dict) else {}
            cs_t = ttss.get('CosyVoiceSiliconflow') if isinstance(ttss.get('CosyVoiceSiliconflow'), dict) else {}
            oa_t = ttss.get('OpenAITTS') if isinstance(ttss.get('OpenAITTS'), dict) else {}
            hs_set, hs_hint = mask_secret(hs_t.get('access_token'))
            cs_set, cs_hint = mask_secret(cs_t.get('access_token'))
            oa_set, oa_hint = mask_secret(oa_t.get('api_key'))
            try:
                oa_spd = round(max(0.5, min(2.0, float(oa_t.get('speed') if oa_t.get('speed') is not None else 1.0))), 2)
            except (TypeError, ValueError):
                oa_spd = 1.0
            oa_fmt = clean_text_val(oa_t.get('format')).lower()
            if oa_fmt not in ('pcm', 'wav', 'mp3'):
                oa_fmt = 'mp3'
            try:
                prebuf = max(0, min(60, int(merged.get('tts_audio_prebuffer_count') if merged.get('tts_audio_prebuffer_count') is not None else 30)))
            except (TypeError, ValueError):
                prebuf = 30
            send_delay_ms = _int_or(merged.get('tts_audio_send_delay'), 0, 0, 120)

            sel_asr = str(sel.get('ASR') or 'SherpaXASR')
            mac_a = asrs.get('MacASR') if isinstance(asrs.get('MacASR'), dict) else {}
            shp_a = asrs.get('SherpaXASR') if isinstance(asrs.get('SherpaXASR'), dict) else {}
            db_a = asrs.get('DoubaoStreamASRV2') if isinstance(asrs.get('DoubaoStreamASRV2'), dict) else {}
            mac_k_set, mac_k_hint = mask_secret(mac_a.get('api_key'))
            db_t_set, db_t_hint = mask_secret(db_a.get('access_token'))
            db_k_set, db_k_hint = mask_secret(db_a.get('api_key'))
            db_appid = clean_text_val(db_a.get('appid'))
            shp_dir_rel, shp_dir_p = self._resolve_sherpa_dir(shp_a)

            corr_clean = _normalize_corrections_dict(merged.get('asr_corrections'))
            hotwords = _parse_hotwords_input(merged.get('asr_hotwords'))
            svad = ((merged.get('VAD') or {}).get('SileroVAD')) if isinstance(merged.get('VAD'), dict) else {}
            if not isinstance(svad, dict):
                svad = {}
            try:
                vad_ms = max(200, min(5000, int(svad.get('min_silence_duration_ms') or 800)))
            except (TypeError, ValueError):
                vad_ms = 800
            try:
                vad_thr = round(max(0.10, min(0.90, float(svad.get('threshold') if svad.get('threshold') is not None else 0.50))), 2)
            except (TypeError, ValueError):
                vad_thr = 0.50
            try:
                vad_thr_low = round(max(0.05, min(0.80, float(svad.get('threshold_low') if svad.get('threshold_low') is not None else 0.30))), 2)
            except (TypeError, ValueError):
                vad_thr_low = 0.30
            try:
                tts_tout = max(5, min(60, int(merged.get('tts_timeout') or 15)))
            except (TypeError, ValueError):
                tts_tout = 15

            raw_ww = ccfg.get('wake_words') if isinstance(ccfg.get('wake_words'), list) else [w['raw'] for w in WAKE_PRESETS]
            wake_words = [str(w).strip() for w in raw_ww if str(w).strip()] or [w['raw'] for w in WAKE_PRESETS]
            try:
                kws_thr = round(max(0.10, min(0.60, float(ccfg.get('kws_threshold', 0.25)))), 2)
            except (TypeError, ValueError):
                kws_thr = 0.25
            try:
                kws_score = round(max(1.0, min(4.0, float(ccfg.get('kws_score', 2.0)))), 1)
            except (TypeError, ValueError):
                kws_score = 2.0
            try:
                kws_gain = max(0, min(24, int(ccfg.get('kws_gain_db', 10))))
            except (TypeError, ValueError):
                kws_gain = 10
            try:
                fup_sec = max(0, min(30, int(ccfg.get('follow_up_seconds', 10))))
            except (TypeError, ValueError):
                fup_sec = 10
            wk_verify = 'hub' if str(ccfg.get('wake_verify') or '').strip() == 'hub' else ''

            try:
                mac_tout = max(2, min(30, int(float(mac_a.get('timeout') or 8))))
            except (TypeError, ValueError):
                mac_tout = 8
            try:
                mac_ctout = round(max(0.5, min(10.0, float(mac_a.get('connect_timeout') if mac_a.get('connect_timeout') is not None else 1.5))), 1)
            except (TypeError, ValueError):
                mac_ctout = 1.5
            try:
                mac_ret = max(0, min(3, int(mac_a.get('retries') if mac_a.get('retries') is not None and not isinstance(mac_a.get('retries'), bool) else 1)))
            except (TypeError, ValueError):
                mac_ret = 1
            mac_uproxy = bool(mac_a.get('use_proxy', False))
            try:
                nv_sec = max(30, min(600, int(merged.get('close_connection_no_voice_time') or 120)))
            except (TypeError, ValueError):
                nv_sec = 120
            ep_cfg = merged.get('end_prompt') if isinstance(merged.get('end_prompt'), dict) else {}
            ep_rep = str(ep_cfg.get('reply') if ep_cfg.get('reply') is not None else '好一会儿没听到你说话，我先休息啦，有事再叫我。')[:120]
            ep_prm = str(ep_cfg.get('prompt') if ep_cfg.get('prompt') is not None else '请你以“时间过得真快”为开头，用富有感情、依依不舍的话来结束这场对话吧！').strip()[:200]
            raw_exit = merged.get('exit_commands') if isinstance(merged.get('exit_commands'), list) else ['退出', '关闭']
            exit_cmds = [str(x).strip()[:20] for x in raw_exit[:20] if str(x).strip()] or ['退出', '关闭']
            en_greet = bool(merged.get('enable_greeting', True))
            wk_cache = bool(merged.get('enable_wakeup_words_response_cache', True))
            stop_tts_notify = bool(merged.get('enable_stop_tts_notify', False))
            sys_err_rep = str(merged.get('system_error_response') if merged.get('system_error_response') is not None else '现在有点忙，我们稍后再试吧。')[:120]
            ww_labels = [re.sub(r'[^\w\u4e00-\u9fff]+', '', w.split('@', 1)[1]) for w in wake_words if '@' in w]
            ww_labels = [x for x in ww_labels if x]
            hub_ww_raw = merged.get('wakeup_words') if isinstance(merged.get('wakeup_words'), list) else []
            hub_ww_set = {re.sub(r'[^\w\u4e00-\u9fff]+', '', str(x)) for x in hub_ww_raw if isinstance(x, str)}
            hub_ww_synced = bool(ww_labels and all(lb in hub_ww_set for lb in ww_labels))

            edg_rate = _int_or(edg_t.get('rate'), 0, -50, 50)
            edg_pitch = _int_or(edg_t.get('pitch'), 0, -50, 50)
            edg_vol = _int_or(edg_t.get('volume'), 50, -50, 100)
            hs_ap = hs_t.get('audio_params') if isinstance(hs_t.get('audio_params'), dict) else {}
            hs_rate = _int_or(hs_ap.get('speech_rate'), 0, -50, 100)
            hs_loud = _int_or(hs_ap.get('loudness_rate'), 0, -50, 100)
            shp_seg_ms = _int_or(shp_a.get('segment_pause_ms'), 0, 0, 3000)
            try:
                shp_hw_score = round(max(1.0, min(5.0, float(shp_a.get('hotwords_score') if shp_a.get('hotwords_score') is not None else 1.5))), 1)
            except (TypeError, ValueError):
                shp_hw_score = 1.5
            shp_mtype = str(shp_a.get('model_type') or ('transducer' if 'transducer' in shp_dir_rel.lower() or 'zipformer' in shp_dir_rel.lower() else 'sense_voice'))
            shp_models_out = [
                {**m, 'installed': (HUB_DIR / m['dir']).is_dir()}
                for m in SHERPA_LOCAL_MODELS
            ]
            kws_paths = _int_or(ccfg.get('kws_paths'), 8, 4, 16)

            voice_out = {
                'tts': {
                    'selected': sel_tts,
                    'prebuffer_count': prebuf,
                    'send_delay_ms': send_delay_ms,
                    'timeout': tts_tout,
                    'edge_voices': list(EDGE_VOICES),
                    'index_stream': {
                        'api_url': clean_text_val(idx_t.get('api_url')),
                        'voice': clean_text_val(idx_t.get('voice')) or 'xiaohe',
                        'audio_format': clean_text_val(idx_t.get('audio_format')) or 'pcm',
                    },
                    'edge': {
                        'voice': clean_text_val(edg_t.get('voice')) or 'zh-CN-XiaoxiaoNeural',
                        'rate': edg_rate,
                        'pitch': edg_pitch,
                        'volume': edg_vol,
                    },
                    'huoshan': {
                        'ws_url': clean_text_val(hs_t.get('ws_url')) or 'wss://openspeech.bytedance.com/api/v3/tts/bidirection',
                        'appid': clean_text_val(hs_t.get('appid')),
                        'speaker': clean_text_val(hs_t.get('speaker')) or 'zh_female_wanwanxiaohe_moon_bigtts',
                        'resource_id': clean_text_val(hs_t.get('resource_id')) or 'seed-tts-2.0',
                        'speech_rate': hs_rate,
                        'loudness_rate': hs_loud,
                        'token_set': hs_set,
                        'token_hint': hs_hint,
                    },
                    'siliconflow': {
                        'model': clean_text_val(cs_t.get('model')) or 'FunAudioLLM/CosyVoice2-0.5B',
                        'voice': clean_text_val(cs_t.get('voice')) or 'FunAudioLLM/CosyVoice2-0.5B:alex',
                        'token_set': cs_set,
                        'token_hint': cs_hint,
                    },
                    'openai_tts': {
                        'api_url': clean_text_val(oa_t.get('api_url')) or 'https://api.openai.com/v1/audio/speech',
                        'model': clean_text_val(oa_t.get('model')) or 'tts-1',
                        'voice': clean_text_val(oa_t.get('voice')) or 'alloy',
                        'format': oa_fmt,
                        'speed': oa_spd,
                        'key_set': oa_set,
                        'key_hint': oa_hint,
                        'models': ['Qwen3-TTS-12Hz-1.7B-Base-bf16', 'tts-1', 'tts-1-hd', 'gpt-4o-mini-tts'],
                        'voices': ['xiaohe', 'alloy', 'nova', 'shimmer', 'echo', 'onyx'],
                    },
                },
                'asr': {
                    'selected': sel_asr,
                    'sherpa_ready': shp_dir_p.is_dir(),
                    'doubao_resources': list(DOUBAO_ASR_RESOURCES),
                    'mac': {
                        'base_url': clean_text_val(mac_a.get('base_url')),
                        'model_name': clean_text_val(mac_a.get('model_name')) or 'Qwen3-ASR-1.7B',
                        'language': clean_text_val(mac_a.get('language')) or 'zh',
                        'prompt': str(mac_a.get('prompt') or ''),
                        'timeout': mac_tout,
                        'connect_timeout': mac_ctout,
                        'retries': mac_ret,
                        'use_proxy': mac_uproxy,
                        'key_set': mac_k_set,
                        'key_hint': mac_k_hint,
                    },
                    'sherpa': {
                        'model_dir': shp_dir_rel,
                        'model_type': shp_mtype,
                        'num_threads': int(shp_a.get('num_threads') or 2),
                        'segment_pause_ms': shp_seg_ms,
                        'hotwords_score': shp_hw_score,
                        'models': shp_models_out,
                    },
                    'doubao': {
                        'auth_mode': 'api_key' if (db_k_set and not db_appid) else 'appid',
                        'appid': db_appid,
                        'resource_id': clean_text_val(db_a.get('resource_id')) or 'volc.seedasr.sauc.duration',
                        'enable_multilingual': bool(db_a.get('enable_multilingual', False)),
                        'token_set': db_t_set,
                        'token_hint': db_t_hint,
                        'key_set': db_k_set,
                        'key_hint': db_k_hint,
                    },
                    'hotwords': hotwords,
                    'corrections': corr_clean,
                    'corrections_text': _format_corrections_text(corr_clean),
                    'recent': [],
                },
                'vad': {
                    'silence_ms': vad_ms,
                    'threshold': vad_thr,
                    'threshold_low': vad_thr_low,
                },
                'session': {
                    'no_voice_close_sec': nv_sec,
                    'end_prompt_enabled': bool(ep_cfg.get('enable', True)),
                    'end_prompt_reply': ep_rep,
                    'end_prompt_prompt': ep_prm,
                    'exit_commands': exit_cmds,
                    'exit_commands_text': '，'.join(exit_cmds),
                    'enable_greeting': en_greet,
                    'wakeup_cache': wk_cache,
                    'stop_tts_notify': stop_tts_notify,
                    'websocket_ping': bool(merged.get('enable_websocket_ping', False)),
                    'delete_audio': bool(merged.get('delete_audio', True)),
                    'system_error_response': sys_err_rep,
                },
                'wakeup': {
                    'presets': list(WAKE_PRESETS),
                    'wake_words': wake_words,
                    'kws_threshold': kws_thr,
                    'kws_score': kws_score,
                    'kws_paths': kws_paths,
                    'kws_gain_db': kws_gain,
                    'follow_up_seconds': fup_sec,
                    'wake_verify': wk_verify,
                    'hub_wakeup_synced': hub_ww_synced,
                },
            }

            # 3) Weather, Web Search, News & Intent Plugins
            gw = plugins.get('get_weather') if isinstance(plugins.get('get_weather'), dict) else {}
            ws = plugins.get('web_search') if isinstance(plugins.get('web_search'), dict) else {}
            nn = plugins.get('get_news_from_newsnow') if isinstance(plugins.get('get_news_from_newsnow'), dict) else {}
            home_cfg = read_json(HOME_STATE / 'config.json', {})
            if not isinstance(home_cfg, dict):
                home_cfg = {}
            qw_host = clean_text_val(gw.get('api_host')).removeprefix('https://').removeprefix('http://').strip('/')
            qw_set, qw_hint = mask_secret(gw.get('api_key'))
            home_city = str(home_cfg.get('city') or clean_text_val(gw.get('default_location')) or clean_text_val(gw.get('location')) or '北京').strip()
            ex_cities = home_cfg.get('extra_cities')
            if isinstance(ex_cities, list):
                extra_cities = [str(x).strip() for x in ex_cities[:8] if isinstance(x, str) and 0 < len(x.strip()) <= 20]
            else:
                extra_cities = ['上海', '广州', '深圳']

            intent_fc = ((merged.get('Intent') or {}).get('function_call') or {})
            fc_funcs = [str(x).strip() for x in (intent_fc.get('functions') or []) if str(x).strip()] if isinstance(intent_fc.get('functions'), list) else []
            eff_funcs = fc_funcs if ('functions' in intent_fc and isinstance(intent_fc.get('functions'), list)) else [
                'get_weather', 'get_news_from_newsnow', 'get_news_from_chinanews', 'play_music', 'web_search'
            ]
            ws_k_set, ws_k_hint = mask_secret(ws.get('api_key'))
            mt_set, mt_hint = mask_secret(ws.get('metaso_key'))
            am_set, am_hint = mask_secret(ws.get('amap_key'))
            ha = plugins.get('home_assistant') if isinstance(plugins.get('home_assistant'), dict) else {}
            ha_set, ha_hint = mask_secret(ha.get('api_key'))
            ha_devs = [str(x).strip() for x in (ha.get('devices') or []) if str(x).strip() and '玩具灯' not in str(x) and 'switch.cuco_cn' not in str(x)] if isinstance(ha.get('devices'), list) else []
            ha_url = clean_text_val(ha.get('base_url'))
            if ha_url == 'http://homeassistant.local:8123' and not ha_set:
                ha_url = ''
            rf = plugins.get('search_from_ragflow') if isinstance(plugins.get('search_from_ragflow'), dict) else {}
            rf_set, rf_hint = mask_secret(rf.get('api_key'))
            rf_url = clean_text_val(rf.get('base_url'))
            if rf_url == 'http://192.168.0.8' and not rf_set:
                rf_url = ''
            rf_ds = [str(x).strip() for x in (rf.get('dataset_ids') or []) if str(x).strip() and str(x).strip() != '123456789'] if isinstance(rf.get('dataset_ids'), list) else []
            rf_desc = str(rf.get('description') or '')
            if '当用户问xxx时' in rf_desc:
                rf_desc = ''
            try:
                tool_tout = max(10, min(120, int(merged.get('tool_call_timeout') or 30)))
            except (TypeError, ValueError):
                tool_tout = 30
            raw_mcp = str(merged.get('mcp_endpoint') or '').strip()
            mcp_ep = '' if (not raw_mcp or is_placeholder_secret(raw_mcp) or '你的' in raw_mcp) else raw_mcp

            cn_rss = plugins.get('get_news_from_chinanews') if isinstance(plugins.get('get_news_from_chinanews'), dict) else {}
            apis_out = {
                'tool_call_timeout': tool_tout,
                'mcp_endpoint': mcp_ep,
                'plugins': {
                    'available': list(INTENT_PLUGINS),
                    'enabled': eff_funcs,
                },
                'qweather': {
                    'api_host': qw_host,
                    'key_set': qw_set,
                    'key_hint': qw_hint,
                    'city': home_city,
                    'extra_cities': extra_cities,
                    'configured': bool(qw_host and qw_set),
                },
                'web_search': {
                    'enabled': 'web_search' in eff_funcs,
                    'base_url': clean_text_val(ws.get('base_url')) or 'https://api.deepseek.com/anthropic/v1',
                    'model': clean_text_val(ws.get('model')) or 'deepseek-v4-flash',
                    'models': ['deepseek-v4-flash', 'deepseek-flash', 'claude-haiku-5-5', 'claude-sonnet-5-5'],
                    'max_uses': max(1, min(10, int(ws.get('max_uses') or 5))),
                    'key_set': ws_k_set,
                    'key_hint': ws_k_hint,
                    'metaso_set': mt_set,
                    'metaso_hint': mt_hint,
                    'amap_set': am_set,
                    'amap_hint': am_hint,
                },
                'news': {
                    'news_sources': str(nn.get('news_sources') or '澎湃新闻;百度热搜;财联社'),
                    'url': str(nn.get('url') or ''),
                    'featured_channels': list(NEWSNOW_FEATURED),
                    'pool_size': max(6, min(40, int(nn.get('pool_size') or 20))),
                    'batch_size': max(1, min(8, int(nn.get('batch_size') or 3))),
                    'seen_days': max(1, min(30, int(nn.get('seen_days') or 3))),
                    'detail_chars': max(300, min(8000, int(nn.get('detail_chars') or 2000))),
                    'focus_keywords': ', '.join(str(x).strip() for x in nn['focus_keywords'] if str(x).strip()) if isinstance(nn.get('focus_keywords'), list) else str(nn.get('focus_keywords') or ''),
                },
                'chinanews': {
                    'default_rss_url': clean_text_val(cn_rss.get('default_rss_url')),
                    'society_rss_url': clean_text_val(cn_rss.get('society_rss_url')),
                    'world_rss_url': clean_text_val(cn_rss.get('world_rss_url')),
                    'finance_rss_url': clean_text_val(cn_rss.get('finance_rss_url')),
                },
                'home_assistant': {
                    'enabled': any(f in eff_funcs for f in HASS_PLUGIN_FUNCS),
                    'base_url': ha_url,
                    'key_set': ha_set,
                    'key_hint': ha_hint,
                    'devices_text': '\n'.join(ha_devs),
                },
                'ragflow': {
                    'enabled': 'search_from_ragflow' in eff_funcs,
                    'base_url': rf_url,
                    'key_set': rf_set,
                    'key_hint': rf_hint,
                    'dataset_ids_text': ', '.join(rf_ds),
                    'description': rf_desc,
                },
                'vllm': self._read_vllm_out(merged, sel),
            }

            # 4) Study Vision & Voice
            study_cfg = read_json(STUDY_STATE / 'config.json', {})
            if not isinstance(study_cfg, dict):
                study_cfg = {}
            sec_raw = ''
            with contextlib.suppress(OSError, UnicodeDecodeError):
                sec_p = STUDY_STATE / 'secret'
                if sec_p.is_file() and not sec_p.is_symlink():
                    sec_raw = sec_p.read_text(encoding='utf-8').strip()
            sec_set, sec_hint = mask_secret(sec_raw)
            study_out = {
                'enabled': bool(study_cfg.get('enabled', False)),
                'vision_url': str(study_cfg.get('vision_url') or ''),
                'tts_url': str(study_cfg.get('tts_url') or ''),
                'voice': str(study_cfg.get('voice') or 'xiaohe'),
                'auto_off_hours': max(0, min(12, int(study_cfg.get('auto_off_hours', 3)))),
                'interval': max(30, min(600, int(study_cfg.get('interval', 60)))),
                'cooldown': max(60, min(3600, int(study_cfg.get('cooldown', 300)))),
                'min_gap': max(30, min(1800, int(study_cfg.get('min_gap', 90)))),
                'burst_frames': max(1, min(4, int(study_cfg.get('burst_frames', 3)))),
                'burst_gap': max(1, min(8, int(study_cfg.get('burst_gap', 4)))),
                'secret_set': sec_set,
                'secret_hint': sec_hint,
                'auto_off_at': None,
                'last': None,
                'today': {'checks': 0, 'present': 0, 'reminders_count': 0, 'focus_sessions': 0, 'focus_minutes': 0},
                'focus': {'active': False, 'phase': '', 'remaining': 0, 'minutes': 25, 'break_minutes': 5},
            }

            # 5) Extra (Nezha, Password) + Device fallback defaults
            mon_cfg = read_json(MONITOR_STATE / 'config.json', {})
            if not isinstance(mon_cfg, dict):
                mon_cfg = {}
            srv_list = read_json(MONITOR_SHARE / 'servers.json', [])
            srv_cnt = len(srv_list) if isinstance(srv_list, list) else 0
            extra_out = {
                'nezha_host': str(mon_cfg.get('nezha_host') or 'https://nezha.example.com'),
                'nezha_proxy': str(mon_cfg['nezha_proxy']) if 'nezha_proxy' in mon_cfg and isinstance(mon_cfg.get('nezha_proxy'), str) else 'http://127.0.0.1:7890',
                'nezha_servers': srv_cnt,
                'password_on': self.auth.record() is not None,
                'username': self.auth.username(),
                'wan_enabled': self.wan_enabled(),
                'wan_url': WAN_PUBLIC_URL,
                'sessions': self.auth.count(),
            }

            sc_off_raw = ccfg.get('screen_off') if isinstance(ccfg.get('screen_off'), dict) else {}
            sc_mins = int(sc_off_raw.get('minutes') or 5) if str(sc_off_raw.get('minutes') or '').isdigit() else 5
            if sc_mins not in SCREEN_OFF_MINUTES_SET:
                sc_mins = 5
            slp_raw = ccfg.get('sleep') if isinstance(ccfg.get('sleep'), dict) else {}
            atone = str(ccfg.get('alarm_tone') or 'coffee')
            if atone not in ALARM_TONE_IDS:
                atone = 'coffee'
            upd_raw = read_json(UPDATE_STATE / 'status.json', {})
            if not isinstance(upd_raw, dict):
                upd_raw = {}
            sys_upd = upd_raw.get('system') if isinstance(upd_raw.get('system'), dict) else {}
            upd_job = upd_raw.get('job') if isinstance(upd_raw.get('job'), dict) else {}
            update_out = {
                'enabled': bool(upd_raw) or (UPDATE_STATE / 'requests').is_dir(),
                'client_current': str(upd_raw.get('current') or ''),
                'client_available': str(upd_raw.get('available') or '') if upd_raw.get('available') else '',
                'system_current': str(sys_upd.get('current') or ''),
                'system_available': str(sys_upd.get('available') or '') if sys_upd.get('available') else '',
                'checked': int(upd_raw.get('checked') or 0) if isinstance(upd_raw.get('checked'), (int, float)) else 0,
                'check_error': str(upd_raw.get('check_error') or ''),
                'job_state': str(upd_job.get('state') or ''),
                'job_message': str(upd_job.get('message') or ''),
            }
            device_out = {
                'online': False,
                'auto_brightness': True,
                'brightness': max(0, min(100, int(ccfg.get('brightness', 100)))),
                'lux': None,
                'sensor_ok': False,
                'screen_on': None,
                'dimmed': False,
                'volume': 60,
                'alarm_volume': max(0, min(100, int(ccfg.get('alarm_volume', 70)))),
                'alarm_tone': atone,
                'alarm_tones': list(ALARM_TONES),
                'screen_off': {
                    'enabled': bool(sc_off_raw.get('enabled', True)),
                    'minutes': sc_mins,
                },
                'sleep': {
                    'enabled': bool(slp_raw.get('enabled', False)),
                    'start': str(slp_raw.get('start') or '22:00'),
                    'end': str(slp_raw.get('end') or '07:00'),
                },
                'bluetooth': bool(ccfg.get('bluetooth', True)),
                'bluetooth_detail': {
                    'powered': False,
                    'window': 0,
                    'name': 'ThinkSmart',
                    'devices': [],
                },
                'alarms_detail': {
                    'items': [],
                    'missed': [],
                    'ringing': None,
                },
                'health': {'audio': None, 'hub': None, 'ip': ''},
                'mic_on': None,
                'shutter_closed': None,
                'airplay_mirroring': False,
                'intercom_in_call': False,
                'intercom_viewers': 0,
                'intercom_listeners': 0,
                'voice_state': 'idle',
                'desk_timer': None,
                'reports_count': 0,
                'sleep_active': False,
                'sys_stats': _read_sys_stats(),
                'update': update_out,
                'hub_version': {'current': {}, 'check': None, 'update': None, 'checks': [], 'active_sessions': 0},
            }

            # Summary
            if sel_llm == 'ClaudeCLI':
                llm_lbl = f"本机 Claude CLI · {claude_cli_out['model']}"
                llm_ok = True
            else:
                act_v = next((v for v in vendor_list if v['entry'] == sel_llm), None)
                act_gb = next((gx for gx in grok_ents if gx['entry'] == sel_llm), None)
                llm_lbl = f"{act_v['label']} · {act_v['model'] or '未填模型'}" if act_v else (f"Grok Bot · {act_gb['bot_name']}" if act_gb else sel_llm)
                llm_ok = bool(act_v and act_v['configured']) if act_v else True
            tts_map = {
                'IndexStreamTTS': '克隆流式 · IndexStream',
                'EdgeTTS': f"本机内置 · EdgeTTS ({voice_out['tts']['edge']['voice'].replace('zh-CN-', '').replace('Neural', '')})",
                'HuoshanDoubleStreamTTSV2': '火山豆包双向流式 TTS',
                'CosyVoiceSiliconflow': '硅基流动 CosyVoice2',
                'OpenAITTS': 'OpenAI 兼容 TTS',
            }
            asr_map = {
                'MacASR': f"网关识别 · {voice_out['asr']['mac']['model_name']}",
                'SherpaXASR': '本机离线 · Sherpa-ONNX',
                'DoubaoStreamASRV2': '豆包云端流式识别',
            }
            summary = {
                'llm_label': llm_lbl,
                'llm_ok': llm_ok,
                'tts_label': tts_map.get(sel_tts, sel_tts),
                'asr_label': asr_map.get(sel_asr, sel_asr),
                'weather_ok': apis_out['qweather']['configured'],
                'weather_city': home_city,
                'search_count': sum(1 for x in (ws_k_set, mt_set, am_set) if x),
            }
            return {
                'llm': llm_out,
                'voice': voice_out,
                'apis': apis_out,
                'study': study_out,
                'extra': extra_out,
                'device': device_out,
                'summary': summary,
            }

    async def _full_settings(self) -> dict:
        snap = await asyncio.get_running_loop().run_in_executor(None, self._read_settings_sync)
        qq_cfg_disk = await asyncio.get_running_loop().run_in_executor(None, read_json, QQ_CFG_DIR / 'config.json', {})
        disk_rmode = str((qq_cfg_disk.get('radio_audio_mode') if isinstance(qq_cfg_disk, dict) else '') or 'hifi')
        if disk_rmode not in ('hifi', 'voice', 'raw'):
            disk_rmode = 'hifi'
        disk_pmode = str((qq_cfg_disk.get('play_mode') if isinstance(qq_cfg_disk, dict) else '') or 'loop')
        if disk_pmode not in ('loop', 'shuffle', 'single'):
            disk_pmode = 'loop'
        default_sleep_sounds = [
            {'id': 'last', 'name': '沿用上次助眠声'},
            {'id': 'deep_sleep', 'name': '混响配方 · 深度入眠（慢浪+颂钵）'},
            {'id': 'cozy_read', 'name': '混响配方 · 围炉夜读（柴火+轻琴）'},
            {'id': 'deep_study', 'name': '混响配方 · 深度自习（细雨+书房）'},
            {'id': 'rain', 'name': '窗外细雨'},
            {'id': 'waves', 'name': '海岸慢浪'},
            {'id': 'sleep_piano', 'name': '雨夜轻琴'},
            {'id': 'night', 'name': '夏夜虫鸣'},
            {'id': 'forest', 'name': '林间微风'},
            {'id': 'stream', 'name': '山涧溪流'},
            {'id': 'fireplace', 'name': '壁炉柴火'},
            {'id': 'singing_bowl', 'name': '禅意颂钵'},
            {'id': 'pink_noise', 'name': '粉红噪音'},
            {'id': 'white_noise', 'name': '柔和白噪音'},
        ]
        qq_info = {
            'online': False, 'logged_in': False, 'nickname': '', 'vip': False,
            'quality': 'lossless', 'max_cache_mb': 200,
            'radio_audio_mode': disk_rmode, 'play_mode': disk_pmode,
            'wake': True, 'wake_time': '06:30',
            'sleep_volume': 25, 'sleep_session': None, 'sleep_sounds': default_sleep_sounds,
        }

        async def _probe_qq():
            nonlocal qq_info
            with contextlib.suppress(Exception):
                async with self.sess.get(QQ_API + '/api/music/status', timeout=aiohttp.ClientTimeout(total=1.5)) as r:
                    if r.status == 200:
                        qj = json.loads((await read_resp_bounded(r, 262144)).decode('utf-8'))
                        up = qj.get('user_profile') if isinstance(qj.get('user_profile'), dict) else {}
                        try:
                            q_mb = max(10, min(500, int(qj.get('max_cache_mb') or 200)))
                        except (TypeError, ValueError):
                            q_mb = 200
                        rm_val = str(qj.get('radio_audio_mode') or disk_rmode)
                        if rm_val not in ('hifi', 'voice', 'raw'):
                            rm_val = disk_rmode
                        pm_val = str(qj.get('play_mode') or disk_pmode)
                        if pm_val not in ('loop', 'shuffle', 'single'):
                            pm_val = disk_pmode
                        qq_info.update({
                            'online': True,
                            'logged_in': bool(up.get('userId') or up.get('nickname')),
                            'nickname': str(up.get('nickname') or '')[:32],
                            'vip': bool(up.get('isVip')),
                            'quality': str(qj.get('quality_level') or 'lossless'),
                            'max_cache_mb': q_mb,
                            'radio_audio_mode': rm_val,
                            'play_mode': pm_val,
                        })
            with contextlib.suppress(Exception):
                async with self.sess.get(QQ_API + '/api/music/sleep_session', timeout=aiohttp.ClientTimeout(total=1.2)) as rs:
                    if rs.status == 200:
                        sj = json.loads((await read_resp_bounded(rs, 65536)).decode('utf-8'))
                        if isinstance(sj, dict) and sj.get('ok'):
                            qq_info['wake'] = sj.get('wake') is not False
                            wt = str(sj.get('wake_time') or '06:30').strip()[:8]
                            if re.match(r'^\d{2}:\d{2}$', wt):
                                qq_info['wake_time'] = wt
                            qq_info['sleep_volume'] = _int_or(sj.get('sleep_volume'), 25, 5, 100)
                            if isinstance(sj.get('session'), dict):
                                s_pub = sj['session']
                                qq_info['sleep_session'] = {
                                    'label': str(s_pub.get('label') or '助眠轻音')[:40],
                                    'night': bool(s_pub.get('night')),
                                    'volume': _int_or(s_pub.get('volume'), qq_info['sleep_volume'], 5, 100),
                                    'remaining': _int_or(s_pub.get('remaining'), 0, 0, 86400) if s_pub.get('remaining') is not None else None,
                                }
                            snd_list = [{'id': 'last', 'name': '沿用上次助眠声'}]
                            seen_sids = {'last'}
                            for rc in (sj.get('recipes') or []):
                                if isinstance(rc, dict) and rc.get('id') and rc['id'] not in seen_sids:
                                    seen_sids.add(rc['id'])
                                    snd_list.append({'id': str(rc['id'])[:32], 'name': f"混响配方 · {str(rc.get('name') or rc['id'])[:24]}"})
                            for pr in (sj.get('presets') or []):
                                if isinstance(pr, dict) and pr.get('id') and pr['id'] not in seen_sids:
                                    seen_sids.add(pr['id'])
                                    snd_list.append({'id': str(pr['id'])[:32], 'name': str(pr.get('name') or pr['id'])[:24]})
                            if len(snd_list) > 1:
                                qq_info['sleep_sounds'] = snd_list

        async def _probe_memory():
            with contextlib.suppress(Exception):
                merged_m, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
                mem_u = self._hub_reload_url(merged_m).replace('/kiosk/internal/reload', '/kiosk/internal/memory')
                async with self.sess.get(mem_u, headers={'X-TS-Internal-Reload': '1'}, timeout=aiohttp.ClientTimeout(total=1.5)) as r:
                    if r.status == 200:
                        mj = json.loads((await read_resp_bounded(r, 131072)).decode('utf-8'))
                        devs = mj.get('devices') if isinstance(mj.get('devices'), list) else []
                        pref_d = snap['llm']['long_memory'].get('device') or ''
                        picked = None
                        if pref_d:
                            picked = next((x for x in devs if isinstance(x, dict) and x.get('device') == pref_d), None)
                        if picked is None:
                            picked = next((x for x in devs if isinstance(x, dict) and 'selftest' not in str(x.get('device', ''))), None) or (devs[0] if devs else None)
                        if isinstance(picked, dict) and isinstance(picked.get('memory'), dict):
                            mo = picked['memory']
                            snap['llm']['long_memory'] = {
                                'device': str(picked.get('device') or pref_d or 'default'),
                                'summary': str(mo.get('summary') or ''),
                                'version': int(mo.get('version') or 0),
                                'updated': mo.get('updated'),
                                'source': str(mo.get('source') or ''),
                            }

        async def _probe_display():
            with contextlib.suppress(Exception):
                async with self.sess.get(MONITOR_API + '/api/display', timeout=aiohttp.ClientTimeout(total=1.2)) as r:
                    if r.status == 200:
                        dj = json.loads((await read_resp_bounded(r, 32768)).decode('utf-8'))
                        if isinstance(dj, dict):
                            snap['device']['online'] = True
                            snap['device']['auto_brightness'] = bool(dj.get('auto', True))
                            if isinstance(dj.get('level'), (int, float)):
                                snap['device']['brightness'] = max(0, min(100, int(dj['level'])))
                            snap['device']['lux'] = dj.get('lux')
                            snap['device']['sensor_ok'] = bool(dj.get('sensor_ok'))
                            if 'screen_on' in dj and dj.get('screen_on') is not None:
                                snap['device']['screen_on'] = bool(dj['screen_on'])
                            snap['device']['dimmed'] = bool(dj.get('dimmed'))

        async def _probe_client():
            with contextlib.suppress(Exception):
                async with self.sess.get(CLIENT_API + '/events', timeout=aiohttp.ClientTimeout(total=1.2, sock_read=0.8)) as r:
                    if r.status == 200:
                        while True:
                            line = await r.content.readline()
                            if not line:
                                break
                            s = line.decode('utf-8', errors='replace').strip()
                            if s.startswith('data:'):
                                cj = json.loads(s[5:].strip())
                                if isinstance(cj, dict):
                                    snap['device']['online'] = True
                                    st_obj = cj.get('status') if isinstance(cj.get('status'), dict) else cj
                                    al_obj = cj.get('alarms') if isinstance(cj.get('alarms'), dict) else {}
                                    if 'mic' in st_obj and st_obj.get('mic') is not None:
                                        snap['device']['mic_on'] = bool(st_obj['mic'])
                                    if isinstance(st_obj.get('state'), str) and st_obj['state']:
                                        snap['device']['voice_state'] = str(st_obj['state']).strip()[:24]
                                    rep_v = st_obj.get('reports')
                                    if isinstance(rep_v, list):
                                        snap['device']['reports_count'] = len(rep_v)
                                    elif isinstance(rep_v, int) and not isinstance(rep_v, bool):
                                        snap['device']['reports_count'] = max(0, rep_v)
                                    v_raw = st_obj.get('volume', cj.get('volume'))
                                    if isinstance(v_raw, (int, float)):
                                        snap['device']['volume'] = max(0, min(100, int(v_raw)))
                                    av_raw = al_obj.get('volume', cj.get('alarm_volume'))
                                    if isinstance(av_raw, (int, float)):
                                        snap['device']['alarm_volume'] = max(0, min(100, int(av_raw)))
                                    at_raw = al_obj.get('tone') or cj.get('alarm_tone')
                                    if at_raw in ALARM_TONE_IDS:
                                        snap['device']['alarm_tone'] = at_raw
                                    if isinstance(al_obj, dict) and ('items' in al_obj or 'ringing' in al_obj or 'missed' in al_obj):
                                        al_items = []
                                        for it in (al_obj.get('items') or [])[:20]:
                                            if isinstance(it, dict) and it.get('id') is not None:
                                                al_items.append({
                                                    'id': it.get('id'),
                                                    'kind': str(it.get('kind') or 'alarm')[:16],
                                                    'when': str(it.get('when') or it.get('time') or '')[:40],
                                                    'at': it.get('at'),
                                                    'label': str(it.get('label') or it.get('say') or '')[:60],
                                                    'repeat': str(it.get('repeat') or '')[:24],
                                                })
                                        snap['device']['alarms_detail'] = {
                                            'items': al_items,
                                            'missed_count': len(al_obj.get('missed') or []) if isinstance(al_obj.get('missed'), list) else 0,
                                            'ringing': bool(al_obj.get('ringing')),
                                        }
                                    so = st_obj.get('screen_off') if isinstance(st_obj.get('screen_off'), dict) else cj.get('screen_off')
                                    if isinstance(so, dict):
                                        snap['device']['screen_off'] = {
                                            'enabled': bool(so.get('enabled', True)),
                                            'minutes': int(so.get('minutes') or 5) if int(so.get('minutes') or 5) in SCREEN_OFF_MINUTES_SET else 5,
                                        }
                                    if 'sleep_active' in st_obj:
                                        snap['device']['sleep_active'] = bool(st_obj['sleep_active'])
                                    sl = st_obj.get('sleep') if isinstance(st_obj.get('sleep'), dict) else cj.get('sleep')
                                    if isinstance(sl, dict):
                                        if 'active' in sl:
                                            snap['device']['sleep_active'] = bool(sl['active'])
                                        snap['device']['sleep'] = {
                                            'enabled': bool(sl.get('enabled', False)),
                                            'start': str(sl.get('start') or '22:00'),
                                            'end': str(sl.get('end') or '07:00'),
                                        }
                                    if isinstance(cj.get('bluetooth'), dict):
                                        bt_o = cj['bluetooth']
                                        snap['device']['bluetooth'] = bool(bt_o.get('enabled', True))
                                        bt_devs = []
                                        for bd in (bt_o.get('devices') or [])[:12]:
                                            if isinstance(bd, dict) and bd.get('address'):
                                                bt_devs.append({
                                                    'address': str(bd.get('address') or '')[:32],
                                                    'name': str(bd.get('name') or bd.get('address') or '')[:60],
                                                    'connected': bool(bd.get('connected')),
                                                    'streaming': bool(bd.get('streaming')),
                                                })
                                        snap['device']['bluetooth_detail'] = {
                                            'powered': bool(bt_o.get('powered')),
                                            'window': max(0, int(bt_o.get('window') or 0)) if isinstance(bt_o.get('window'), (int, float)) else 0,
                                            'name': str(bt_o.get('name') or 'ThinkSmart')[:40],
                                            'devices': bt_devs,
                                        }
                                    elif isinstance(cj.get('bluetooth'), bool):
                                        snap['device']['bluetooth'] = cj['bluetooth']
                                    if isinstance(cj.get('version'), str) and cj['version'] and not snap['device']['update']['client_current']:
                                        snap['device']['update']['client_current'] = cj['version'][:32]
                                    if isinstance(cj.get('update'), dict):
                                        cu = cj['update']
                                        if cu.get('current'):
                                            snap['device']['update']['client_current'] = str(cu['current'])[:32]
                                        if 'available' in cu:
                                            snap['device']['update']['client_available'] = str(cu.get('available') or '')[:32]
                                break
            with contextlib.suppress(Exception):
                async with self.sess.get(CLIENT_API + '/api/health', timeout=aiohttp.ClientTimeout(total=0.8)) as rh:
                    if rh.status == 200:
                        hj = json.loads((await read_resp_bounded(rh, 8192)).decode('utf-8'))
                        if isinstance(hj, dict):
                            snap['device']['health']['audio'] = bool(hj.get('audio')) if 'audio' in hj else None
                            snap['device']['health']['hub'] = bool(hj.get('hub')) if 'hub' in hj else None
            with contextlib.suppress(Exception):
                async with self.sess.get(CLIENT_API + '/api/network', timeout=aiohttp.ClientTimeout(total=0.8)) as rn:
                    if rn.status == 200:
                        nj = json.loads((await read_resp_bounded(rn, 8192)).decode('utf-8'))
                        if isinstance(nj, dict) and nj.get('ip'):
                            snap['device']['health']['ip'] = str(nj['ip'])[:48]

        async def _probe_diag():
            with contextlib.suppress(Exception):
                merged_d, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
                diag_u = self._hub_diag_url(merged_d)
                async with self.sess.get(diag_u, headers={'X-TS-Internal-Reload': '1'}, timeout=aiohttp.ClientTimeout(total=1.5)) as rd:
                    if rd.status == 200:
                        dj = json.loads((await read_resp_bounded(rd, 131072)).decode('utf-8'))
                        if isinstance(dj, dict) and dj.get('ok'):
                            if isinstance(dj.get('asr_recent'), list):
                                snap['voice']['asr']['recent'] = dj['asr_recent'][:30]
                            if isinstance(dj.get('call_learning'), dict):
                                snap['llm']['call_names']['learning'] = _normalize_call_learning(dj['call_learning'])
                            if isinstance(dj.get('grokbot'), dict):
                                snap['llm']['grokbot'] = dj['grokbot']
                            raw_hu = dj.get('hub_update')
                            if isinstance(raw_hu, dict):
                                hu_st = str(raw_hu.get('state') or ('running' if raw_hu.get('running') else 'idle'))
                                hub_upd_norm = {
                                    'state': hu_st,
                                    'running': bool(raw_hu.get('running')) or hu_st == 'running',
                                    'step': str(raw_hu.get('step') or '')[:60],
                                    'error': _redact_err(raw_hu.get('error') or '')[:120] if raw_hu.get('error') else None,
                                    'restart_needed': bool(raw_hu.get('restart_needed')),
                                }
                            else:
                                hub_upd_norm = None
                            snap['device']['hub_version'] = {
                                'current': dj.get('hub_version') if isinstance(dj.get('hub_version'), dict) else {},
                                'check': dj.get('hub_check') if isinstance(dj.get('hub_check'), dict) else None,
                                'update': hub_upd_norm,
                                'checks': dj.get('hub_checks') if isinstance(dj.get('hub_checks'), list) else [],
                                'active_sessions': _int_or(dj.get('active_sessions'), 0, 0, 99),
                            }

        async def _probe_study():
            with contextlib.suppress(Exception):
                async with self.sess.get(STUDY_API + '/api/study', timeout=aiohttp.ClientTimeout(total=0.9)) as rs:
                    if rs.status == 200:
                        sj = json.loads((await read_resp_bounded(rs, 32768)).decode('utf-8'))
                        if isinstance(sj, dict):
                            if 'enabled' in sj:
                                snap['study']['enabled'] = bool(sj['enabled'])
                            ao = sj.get('auto_off_at')
                            snap['study']['auto_off_at'] = int(ao) if isinstance(ao, (int, float)) and ao > 0 else None
                            if isinstance(sj.get('last'), dict):
                                lo = sj['last']
                                snap['study']['last'] = {
                                    'ts': int(lo['ts']) if isinstance(lo.get('ts'), (int, float)) else 0,
                                    'present': bool(lo['present']) if 'present' in lo and lo.get('present') is not None else None,
                                    'posture': str(lo.get('posture') or '')[:24],
                                    'issue': str(lo.get('issue') or '')[:60],
                                    'spoken': bool(lo.get('spoken')),
                                    'error': str(lo.get('error') or '')[:80],
                                }
                            if isinstance(sj.get('today'), dict):
                                td = sj['today']
                                rem_cnt = len(td['reminders']) if isinstance(td.get('reminders'), list) else _int_or(td.get('reminders_count'), 0, 0, 9999)
                                snap['study']['today'] = {
                                    'checks': _int_or(td.get('checks'), 0, 0, 99999),
                                    'present': _int_or(td.get('present'), 0, 0, 99999),
                                    'reminders_count': rem_cnt,
                                    'focus_sessions': _int_or(td.get('focus_sessions'), 0, 0, 999),
                                    'focus_minutes': _int_or(td.get('focus_minutes'), 0, 0, 9999),
                                }
            with contextlib.suppress(Exception):
                async with self.sess.get(STUDY_API + '/api/study/focus', timeout=aiohttp.ClientTimeout(total=0.9)) as rf:
                    if rf.status == 200:
                        fj = json.loads((await read_resp_bounded(rf, 16384)).decode('utf-8'))
                        if isinstance(fj, dict):
                            snap['study']['focus'] = {
                                'active': bool(fj.get('active') or fj.get('running') or fj.get('phase') in ('focus', 'break')),
                                'phase': str(fj.get('phase') or ''),
                                'remaining': max(0, int(fj.get('remaining') or fj.get('seconds_left') or 0)) if isinstance(fj.get('remaining') or fj.get('seconds_left'), (int, float)) else 0,
                                'minutes': _int_or(fj.get('minutes'), 25, 1, 180),
                                'break_minutes': _int_or(fj.get('break_minutes'), 5, 0, 60),
                            }

        async def _probe_intercom_and_home():
            with contextlib.suppress(Exception):
                async with self.sess.get(
                    INTERCOM_API + '/api/intercom/status',
                    headers={'X-Intercom-Msg': '1', 'Origin': INTERCOM_API},
                    timeout=aiohttp.ClientTimeout(total=0.8),
                ) as ri:
                    if ri.status == 200:
                        ij = json.loads((await read_resp_bounded(ri, 8192)).decode('utf-8'))
                        if isinstance(ij, dict) and ij.get('ok'):
                            if 'shutter_closed' in ij:
                                snap['device']['shutter_closed'] = bool(ij['shutter_closed'])
                            if snap['device']['mic_on'] is None and 'hw_mic_muted' in ij:
                                snap['device']['mic_on'] = not bool(ij['hw_mic_muted'])
                            snap['device']['airplay_mirroring'] = bool(ij.get('mirroring'))
                            snap['device']['intercom_in_call'] = bool(ij.get('in_call'))
                            snap['device']['intercom_viewers'] = _int_or(ij.get('viewers'), 0, 0, 99)
                            snap['device']['intercom_listeners'] = _int_or(ij.get('listeners'), 0, 0, 99)
            with contextlib.suppress(Exception):
                async with self.sess.get(
                    HOME_API + '/api/home/timer',
                    headers={'Host': '127.0.0.1:8095'},
                    timeout=aiohttp.ClientTimeout(total=0.8),
                ) as rt:
                    if rt.status == 200:
                        tj = json.loads((await read_resp_bounded(rt, 16384)).decode('utf-8'))
                        tm_o = tj.get('timer') if isinstance(tj, dict) else None
                        if isinstance(tm_o, dict) and tm_o.get('phase') in ('running', 'paused', 'done'):
                            snap['device']['desk_timer'] = {
                                'phase': str(tm_o.get('phase') or ''),
                                'mode': str(tm_o.get('mode') or 'countdown')[:16],
                                'label': str(tm_o.get('label') or '')[:40],
                                'remaining': _int_or(tm_o.get('remaining'), 0, 0, 86400),
                            }

        await asyncio.gather(
            _probe_qq(), _probe_display(), _probe_client(),
            _probe_memory(), _probe_diag(), _probe_study(), _probe_intercom_and_home(),
        )
        snap['extra']['qqmusic'] = qq_info
        return snap

    async def _probe_client_idle(self) -> bool:
        with contextlib.suppress(Exception):
            async with self.sess.get(CLIENT_API + '/events', timeout=aiohttp.ClientTimeout(total=1.0, sock_read=0.6)) as r:
                if r.status == 200:
                    while True:
                        line = await r.content.readline()
                        if not line:
                            break
                        s = line.decode('utf-8', errors='replace').strip()
                        if s.startswith('data:'):
                            cj = json.loads(s[5:].strip())
                            if isinstance(cj, dict):
                                st_obj = cj.get('status') if isinstance(cj.get('status'), dict) else cj
                                return str(st_obj.get('state') or 'idle') in ('idle', 'sleeping')
                            break
        return False

    async def get_settings(self, _request):
        return jok(**(await self._full_settings()))

    async def save_settings_memory(self, request):
        d = await json_body(request, 32768)
        summary = d.get('summary')
        if not isinstance(summary, str):
            raise ApiError('记忆内容格式不对')
        if len(summary) > 5000:
            raise ApiError('长期记忆最多 5000 字')
        ccfg = await asyncio.get_running_loop().run_in_executor(None, read_json, CLIENT_CFG, {})
        def_dev = str((ccfg.get('device_id') if isinstance(ccfg, dict) else '') or 'default').strip() or 'default'
        device = str(d.get('device') or def_dev).strip()[:64] or def_dev
        raw_ver = d.get('version')
        expect_ver = int(raw_ver) if isinstance(raw_ver, int) and not isinstance(raw_ver, bool) else None

        merged, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
        mem_u = self._hub_reload_url(merged).replace('/kiosk/internal/reload', '/kiosk/internal/memory')
        hub_handled = False
        try:
            async with self.sess.post(
                mem_u,
                json={'device': device, 'summary': summary, 'version': expect_ver},
                headers={'X-TS-Internal-Reload': '1'},
                timeout=aiohttp.ClientTimeout(total=4, sock_connect=1.5),
            ) as r:
                rj = json.loads((await read_resp_bounded(r, 65536)).decode('utf-8'))
                if r.status == 200 and rj.get('ok'):
                    hub_handled = True
                    mem_out = rj.get('memory') or {}
                elif r.status == 409:
                    raise ApiError(str(rj.get('error') or '编辑期间小智自动更新了记忆，请刷新后再修改'), 409)
                elif r.status >= 400:
                    raise ApiError(str(rj.get('error') or f'保存长期记忆失败（HTTP {r.status}）'), r.status)
        except ApiError:
            raise
        except Exception:
            hub_handled = False

        if not hub_handled:
            ok, mem_out = await asyncio.get_running_loop().run_in_executor(
                None, _sqlite_write_memory, HUB_DIR / 'data' / 'chat.db', device, summary, expect_ver
            )
            if not ok:
                raise ApiError('编辑期间记忆已被更新，请刷新查看最新内容后再改', 409)

        note_s = '已清空长期记忆档案' if not summary.strip() else '小智长期记忆档案已保存生效（下次对话立即引用）'
        log.info('settings/memory updated: device=%s len=%d', device, len(summary))
        return jok(note=note_s, memory={'device': device, **(mem_out if isinstance(mem_out, dict) else {})})

    async def save_settings_llm(self, request):
        d = await json_body(request, 65536)
        action = str(d.get('action') or '').strip()
        if action in ('call_learn_start', 'call_learn_stop'):
            raw_tgt = str(d.get('target') or '').strip()[:64]
            target = _resolve_call_entry(raw_tgt) if raw_tgt else ''
            if action == 'call_learn_start' and raw_tgt and not target:
                raise ApiError('不支持这个呼名目标模型')
            merged_l, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
            try:
                async with self.sess.post(
                    self._hub_diag_url(merged_l),
                    json={'action': 'learn_start' if action == 'call_learn_start' else 'learn_stop', 'target': target or None},
                    headers={'X-TS-Internal-Reload': '1'},
                    timeout=aiohttp.ClientTimeout(total=4, sock_connect=1.5),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 32768)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or f'呼名录入请求返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '小智语音网关'), 502)
            l_note = (
                '已开启 90 秒呼名语音试录：请直接唤醒大屏并说出你想用的模型别称（最多录 3 次，不会触发聊天回复）'
                if action == 'call_learn_start'
                else '已停止呼名语音试录'
            )
            return jok(note=l_note, call_learning=_normalize_call_learning(rj.get('call_learning')))

        if action in ('grokbot_login', 'grokbot_login_cancel', 'grokbot_logout', 'grokbot_refresh', 'grokbot_save_bots'):
            merged_g, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
            g_body = {'action': action}
            if action == 'grokbot_save_bots':
                g_body['bots'] = d.get('bots') if isinstance(d.get('bots'), list) else []
            try:
                async with self.sess.post(
                    self._hub_diag_url(merged_g),
                    json=g_body,
                    headers={'X-TS-Internal-Reload': '1'},
                    timeout=aiohttp.ClientTimeout(total=25, sock_connect=2),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 262144)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or f'Grok Bot 接口返回 HTTP {r.status}'), 400 if r.status == 400 else 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '小智语音网关'), 502)
            extra_snap = (await self._full_settings()) if action in ('grokbot_save_bots', 'grokbot_logout') else {}
            return jok(
                note=rj.get('note') or '',
                url=rj.get('url') or '',
                grokbot=rj.get('grokbot') or {},
                **extra_snap,
            )

        if action == 'preview_prompt':
            merged_p, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
            p_in = d.get('prompt')
            p_str = str(p_in if isinstance(p_in, str) else (merged_p.get('prompt') or ''))[:8000]
            try:
                async with self.sess.post(
                    self._hub_diag_url(merged_p),
                    json={'action': 'preview_prompt', 'prompt': p_str},
                    headers={'X-TS-Internal-Reload': '1'},
                    timeout=aiohttp.ClientTimeout(total=5, sock_connect=1.5),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 131072)).decode('utf-8'))
                    if r.status == 200 and rj.get('ok'):
                        return jok(template=rj.get('template') or 'hub-base-prompt.txt',
                                   rendered=rj.get('rendered') or p_str,
                                   tools=rj.get('tools') or '')
            except Exception:
                pass
            return jok(template=str(merged_p.get('prompt_template') or 'hub-base-prompt.txt'), rendered=p_str, tools='')

        vid = str(d.get('vendor') or '').strip()
        mode = 'cli' if (d.get('mode') == 'cli' or vid == 'claude_cli') else 'api'
        keep_default = bool(d.get('keep_default', False))

        vendor = None
        def _parse_opt_gen_params(obj: dict):
            p_temp = None
            if 'temperature' in obj:
                rt = str(obj.get('temperature') if obj.get('temperature') is not None else '').strip()
                if rt == '':
                    p_temp = ''
                else:
                    try:
                        p_temp = round(max(0.0, min(2.0, _finite_float(rt))), 2)
                    except (TypeError, ValueError, OverflowError):
                        raise ApiError('回复随机度 temperature 请填 0~2 之间的数字（或留空）')
            p_max = None
            if 'max_tokens' in obj:
                rm = str(obj.get('max_tokens') if obj.get('max_tokens') is not None else '').strip()
                if rm == '':
                    p_max = ''
                else:
                    try:
                        p_max = max(64, min(32768, int(_finite_float(rm))))
                    except (TypeError, ValueError, OverflowError):
                        raise ApiError('最大回复长度 max_tokens 请填 64~32768 之间的整数（或留空）')
            return p_temp, p_max

        base_url = ''
        model = ''
        raw_key = ''
        opt_temp, opt_max_tok = None, None
        ccli_payload = None
        if mode == 'cli':
            cc_in = d.get('claude_cli') if isinstance(d.get('claude_cli'), dict) else {}
            c_mod = str(cc_in.get('model') or 'haiku').strip()[:60]
            if not c_mod or any(ch.isspace() for ch in c_mod):
                raise ApiError('请选择或填写正确的 Claude CLI 模型')
            c_eff = str(cc_in.get('effort') or 'low').strip()
            if c_eff not in CLAUDE_CLI_EFFORTS:
                c_eff = 'low'
            c_lvl = str(cc_in.get('level') or 'read').strip()
            if c_lvl not in CLAUDE_CLI_LEVELS:
                c_lvl = 'read'
            c_web = bool(cc_in.get('web', False))
            c_dir = str(cc_in.get('workdir') or '~/xiaozhi-workspace').strip()[:200] or '~/xiaozhi-workspace'
            c_fil = str(cc_in.get('first_reply_filler') if cc_in.get('first_reply_filler') is not None else '好的，我看一下').strip()[:60]
            c_bg = _int_or(cc_in.get('background_after_seconds'), 30, 10, 300)
            c_bg_max = _int_or(cc_in.get('background_max_seconds'), 7200, 0, 14400) if 'background_max_seconds' in cc_in else None
            c_tt = _int_or(cc_in.get('turn_timeout_seconds'), 600, 60, 3600)
            c_stall = _int_or(cc_in.get('stall_timeout_seconds'), 30, 0, 600) if 'stall_timeout_seconds' in cc_in else None
            c_idle = _int_or(cc_in.get('idle_timeout_seconds'), 300, 0, 3600) if 'idle_timeout_seconds' in cc_in else None
            c_res = _int_or(cc_in.get('resume_window_minutes'), 120, 0, 1440) if 'resume_window_minutes' in cc_in else None
            c_fdelay = _int_or(cc_in.get('filler_delay_seconds'), 2, 0, 15) if 'filler_delay_seconds' in cc_in else None
            c_pint = _int_or(cc_in.get('progress_interval_seconds'), 30, 0, 300) if 'progress_interval_seconds' in cc_in else None
            c_cli_p = None
            if 'cli_path' in cc_in:
                raw_cp = str(cc_in.get('cli_path') or '').strip()[:200]
                if any(ord(ch) < 32 for ch in raw_cp):
                    raise ApiError('claude 可执行路径格式不对')
                c_cli_p = raw_cp
            c_rkw = None
            if 'resume_keywords' in cc_in:
                rk_in = cc_in.get('resume_keywords')
                rk_parts = rk_in if isinstance(rk_in, list) else re.split(r'[,，、;\n]+', str(rk_in or ''))
                c_rkw = [str(x).strip()[:24] for x in rk_parts[:15] if str(x).strip()] or ['接着上次聊']
            c_pr = str(cc_in.get('progress_reply') if cc_in.get('progress_reply') is not None else '还在处理，请稍等').strip()[:60]
            ccli_payload = (c_mod, c_eff, c_lvl, c_web, c_dir, c_fil, c_bg, c_tt, c_pr, c_stall, c_idle, c_res, c_bg_max, c_fdelay, c_pint, c_cli_p, c_rkw)
        else:
            vendor = VENDOR_BY_ID.get(vid)
            if vendor is None:
                raise ApiError('不支持这个大模型厂商')
            base_url = check_safe_url(strip_chat_completions_suffix(d.get('base_url', '')), '大模型接口地址', allow_empty=False)
            model = str(d.get('model') or '').strip()
            if not model or len(model) > 120 or any(ch.isspace() for ch in model):
                raise ApiError('请填写正确的模型名称（不能含空格）')
            raw_key = str(d.get('api_key') or '').strip()
            if raw_key:
                check_no_ws_secret(raw_key, '大模型 API Key')
            opt_temp, opt_max_tok = _parse_opt_gen_params(d)

        extra_drafts = []
        raw_drafts = d.get('vendor_drafts')
        if isinstance(raw_drafts, dict):
            for d_vid, d_obj in list(raw_drafts.items())[:12]:
                if not isinstance(d_obj, dict) or d_vid == vid:
                    continue
                dv = VENDOR_BY_ID.get(str(d_vid).strip())
                if dv is None:
                    continue
                d_url_raw = strip_chat_completions_suffix(d_obj.get('base_url', ''))
                d_mod = str(d_obj.get('model') or '').strip()
                d_key = str(d_obj.get('api_key') or '').strip()
                d_clr = bool(d_obj.get('clear_api_key', False))
                d_temp, d_max = _parse_opt_gen_params(d_obj)
                if not d_url_raw and not d_key and not d_clr and d_temp is None and d_max is None:
                    continue
                d_url = check_safe_url(d_url_raw, f'{dv["label"]} 接口地址', allow_empty=False)
                if not d_mod or len(d_mod) > 120 or any(ch.isspace() for ch in d_mod):
                    raise ApiError(f'请填写正确的 {dv["label"]} 模型名称（不能含空格）')
                if d_key:
                    check_no_ws_secret(d_key, f'{dv["label"]} API Key')
                extra_drafts.append((dv, d_url, d_mod, d_key, d_clr, d_temp, d_max))

        prompt = d.get('prompt')
        if prompt is not None:
            if not isinstance(prompt, str) or len(prompt) > 8000:
                raise ApiError('角色提示词最多 8000 字')

        ch_patch = None
        if isinstance(d.get('chat_history'), dict):
            ch_in = d['chat_history']
            ch_max = _int_or(ch_in.get('max_messages'), 30, 2, 100)
            ch_max_tok = _int_or(ch_in.get('max_tokens'), 8000, 1000, 64000) if 'max_tokens' in ch_in else None
            ch_disp_lim = _int_or(ch_in.get('display_limit'), 100, 20, 500) if 'display_limit' in ch_in else None
            ch_smin = _int_or(ch_in.get('summary_min'), 6, 4, 40) if 'summary_min' in ch_in else None
            ch_schars = _int_or(ch_in.get('summary_chars'), 500, 200, 3000) if 'summary_chars' in ch_in else None
            ch_sllm = None
            if 'summary_llm' in ch_in:
                raw_sl = str(ch_in.get('summary_llm') or '').strip()
                ch_sllm = _resolve_call_entry(raw_sl) if raw_sl else ''
            ch_patch = (bool(ch_in.get('enabled', True)), ch_max, bool(ch_in.get('summary', True)), ch_smin, ch_schars, ch_sllm, ch_max_tok, ch_disp_lim)

        cn_patch = None
        if isinstance(d.get('call_names'), dict):
            cn_in = d['call_names']
            cn_rep = str(cn_in.get('reply') or '我在，请说').strip()[:40] or '我在，请说'
            cn_models = _parse_call_models_input(cn_in['models']) if 'models' in cn_in else None
            cn_patch = (bool(cn_in.get('enabled', True)), cn_rep, cn_models)

        tut = d.get('tutor') if isinstance(d.get('tutor'), dict) else None
        tut_vals = None
        tut_cam_push = None
        if tut is not None:
            t_fast = str(tut.get('fast_model') or 'gpt-6.1-terra').strip()[:120]
            t_deep = str(tut.get('deep_model') or 'gpt-6.1-sol').strip()[:120]
            t_bak = str(tut.get('backup_model') or 'deepseek-v4.1-flash').strip()[:120]
            for lbl, mv in (('主力快讲模型', t_fast), ('深度推理模型', t_deep), ('兜底备份模型', t_bak)):
                if not mv or any(ch.isspace() for ch in mv):
                    raise ApiError(f'{lbl}格式不对')
            mob_url = str(tut.get('mobile_entry_url') or '').strip()
            if mob_url:
                check_safe_url(mob_url, '手机拍题二维码入口网址', allow_empty=False)
            c_rot = None
            if 'cam_rotate' in tut and tut.get('cam_rotate') is not None:
                raw_rot = tut.get('cam_rotate')
                if isinstance(raw_rot, bool):
                    raise ApiError('摄像头旋转角度必须是 0 / 90 / 180 / 270')
                try:
                    rv = int(raw_rot) % 360
                except (TypeError, ValueError, OverflowError):
                    raise ApiError('摄像头旋转角度必须是 0 / 90 / 180 / 270')
                if rv not in (0, 90, 180, 270):
                    raise ApiError('摄像头旋转角度必须是 0 / 90 / 180 / 270')
                c_rot = rv
            c_zoom = None
            if 'cam_zoom' in tut and tut.get('cam_zoom') is not None:
                try:
                    zv = round(_finite_float(tut.get('cam_zoom')), 1)
                except (TypeError, ValueError, OverflowError):
                    raise ApiError('摄像头画面倍率必须是 1.0 / 1.5 / 2.0')
                if zv not in (1.0, 1.5, 2.0):
                    raise ApiError('摄像头画面倍率必须是 1.0 / 1.5 / 2.0')
                c_zoom = zv
            c_bri = bool(tut['cam_bright']) if 'cam_bright' in tut else None
            tut_vals = (t_fast, t_deep, t_bak, mob_url, c_rot, c_zoom, c_bri)

        reload_secs = ['LLM']
        cn_reply_changed = False

        def _write():
            nonlocal tut_cam_push, cn_reply_changed
            with self.cfg_lock:
                merged, _ = load_hub_configs()
                llms = merged.get('LLM') if isinstance(merged.get('LLM'), dict) else {}
                sel_m = merged.get('selected_module') if isinstance(merged.get('selected_module'), dict) else {}
                cur_intent = str(sel_m.get('Intent') or 'function_call')
                ops = []

                if mode == 'cli':
                    cur_ccli = llms.get('ClaudeCLI') if isinstance(llms.get('ClaudeCLI'), dict) else {}
                    c_mod, c_eff, c_lvl, c_web, c_dir, c_fil, c_bg, c_tt, c_pr, c_stall, c_idle, c_res, c_bg_max, c_fdelay, c_pint, c_cli_p, c_rkw = ccli_payload
                    eff_cli_p = c_cli_p if c_cli_p is not None else clean_text_val(cur_ccli.get('cli_path'))
                    if not keep_default and not _find_claude_cli(eff_cli_p):
                        raise ApiError('这台设备上尚未安装 Claude Code 命令行（claude），无法切换为默认语音模型；请选用云端或自建网关厂商')
                    perm_mode, core_tools = CLAUDE_CLI_LEVELS[c_lvl]
                    tools = list(core_tools) + (list(CLAUDE_WEB_TOOLS) if c_web else [])
                    ops.extend([
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'type'], 'value': 'claude_cli'},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'model'], 'value': c_mod},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'effort'], 'value': c_eff},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'permission_mode'], 'value': perm_mode},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'allowed_tools'], 'value': tools},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'workdir'], 'value': c_dir},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'first_reply_filler'], 'value': c_fil},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'background_after_seconds'], 'value': c_bg},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'turn_timeout_seconds'], 'value': c_tt},
                        {'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'progress_reply'], 'value': c_pr},
                    ])
                    if c_stall is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'stall_timeout_seconds'], 'value': c_stall})
                    if c_idle is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'idle_timeout_seconds'], 'value': c_idle})
                    if c_res is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'resume_window_minutes'], 'value': c_res})
                    if c_bg_max is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'background_max_seconds'], 'value': c_bg_max})
                    if c_fdelay is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'filler_delay_seconds'], 'value': c_fdelay})
                    if c_pint is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'progress_interval_seconds'], 'value': c_pint})
                    if c_cli_p is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'cli_path'], 'value': c_cli_p} if c_cli_p else {'op': 'del', 'path': ['LLM', 'ClaudeCLI', 'cli_path']})
                    if c_rkw is not None:
                        ops.append({'op': 'set', 'path': ['LLM', 'ClaudeCLI', 'resume_keywords'], 'value': c_rkw})
                    if not keep_default:
                        ops.append({'op': 'set', 'path': ['selected_module', 'LLM'], 'value': 'ClaudeCLI'})
                        if cur_intent != 'nointent':
                            ops.append({'op': 'set', 'path': ['admin', 'api_intent'], 'value': cur_intent})
                            ops.append({'op': 'set', 'path': ['selected_module', 'Intent'], 'value': 'nointent'})
                            reload_secs.append('Intent')
                else:
                    ent = llms.get(vendor['entry']) if isinstance(llms.get(vendor['entry']), dict) else {}
                    existing_key = '' if is_placeholder_secret(ent.get('api_key')) else str(ent.get('api_key')).strip()
                    final_key = '' if d.get('clear_api_key') else (raw_key or existing_key)
                    if not final_key and vendor['id'] != 'custom' and not d.get('clear_api_key'):
                        raise ApiError(f'请先填写 {vendor["label"]} 的 API Key')
                    if d.get('clear_api_key') and not keep_default and vendor['id'] != 'custom':
                        raise ApiError(f'当前默认模型 {vendor["label"]} 需要保留有效的 API Key')
                    uf = 'url' if ('url' in ent and 'base_url' not in ent) else 'base_url'
                    ops.extend([
                        {'op': 'set', 'path': ['LLM', vendor['entry'], 'type'], 'value': 'openai'},
                        {'op': 'set', 'path': ['LLM', vendor['entry'], uf], 'value': base_url},
                        {'op': 'set', 'path': ['LLM', vendor['entry'], 'model_name'], 'value': model},
                    ])
                    if opt_temp is not None:
                        ops.append({'op': 'del', 'path': ['LLM', vendor['entry'], 'temperature']} if opt_temp == '' else {'op': 'set', 'path': ['LLM', vendor['entry'], 'temperature'], 'value': opt_temp})
                    if opt_max_tok is not None:
                        ops.append({'op': 'del', 'path': ['LLM', vendor['entry'], 'max_tokens']} if opt_max_tok == '' else {'op': 'set', 'path': ['LLM', vendor['entry'], 'max_tokens'], 'value': opt_max_tok})
                    if raw_key or d.get('clear_api_key') or not ent:
                        ops.append({'op': 'set', 'path': ['LLM', vendor['entry'], 'api_key'], 'value': final_key})
                    if not keep_default:
                        ops.append({'op': 'set', 'path': ['selected_module', 'LLM'], 'value': vendor['entry']})
                        if cur_intent == 'nointent':
                            saved_intent = str(((merged.get('admin') or {}).get('api_intent')) or 'function_call')
                            ops.append({'op': 'set', 'path': ['selected_module', 'Intent'], 'value': saved_intent})
                            reload_secs.append('Intent')

                for dv, d_url, d_mod, d_key, d_clr, d_temp, d_max in extra_drafts:
                    dent = llms.get(dv['entry']) if isinstance(llms.get(dv['entry']), dict) else {}
                    ex_k = '' if is_placeholder_secret(dent.get('api_key')) else str(dent.get('api_key')).strip()
                    fin_k = '' if d_clr else (d_key or ex_k)
                    duf = 'url' if ('url' in dent and 'base_url' not in dent) else 'base_url'
                    ops.extend([
                        {'op': 'set', 'path': ['LLM', dv['entry'], 'type'], 'value': 'openai'},
                        {'op': 'set', 'path': ['LLM', dv['entry'], duf], 'value': d_url},
                        {'op': 'set', 'path': ['LLM', dv['entry'], 'model_name'], 'value': d_mod},
                    ])
                    if d_temp is not None:
                        ops.append({'op': 'del', 'path': ['LLM', dv['entry'], 'temperature']} if d_temp == '' else {'op': 'set', 'path': ['LLM', dv['entry'], 'temperature'], 'value': d_temp})
                    if d_max is not None:
                        ops.append({'op': 'del', 'path': ['LLM', dv['entry'], 'max_tokens']} if d_max == '' else {'op': 'set', 'path': ['LLM', dv['entry'], 'max_tokens'], 'value': d_max})
                    if d_key or d_clr or not dent:
                        ops.append({'op': 'set', 'path': ['LLM', dv['entry'], 'api_key'], 'value': fin_k})

                if prompt is not None:
                    if prompt.strip():
                        ops.append({'op': 'set', 'path': ['prompt'], 'value': prompt})
                    else:
                        ops.append({'op': 'del', 'path': ['prompt']})
                if ch_patch is not None:
                    ops.extend([
                        {'op': 'set', 'path': ['chat_history', 'enabled'], 'value': ch_patch[0]},
                        {'op': 'set', 'path': ['chat_history', 'max_messages'], 'value': ch_patch[1]},
                        {'op': 'set', 'path': ['chat_history', 'summary'], 'value': ch_patch[2]},
                    ])
                    if ch_patch[3] is not None:
                        ops.append({'op': 'set', 'path': ['chat_history', 'summary_min'], 'value': ch_patch[3]})
                    if ch_patch[4] is not None:
                        ops.append({'op': 'set', 'path': ['chat_history', 'summary_chars'], 'value': ch_patch[4]})
                    if ch_patch[5] is not None:
                        if ch_patch[5]:
                            ops.append({'op': 'set', 'path': ['chat_history', 'summary_llm'], 'value': ch_patch[5]})
                        else:
                            ops.append({'op': 'del', 'path': ['chat_history', 'summary_llm']})
                    if ch_patch[6] is not None:
                        ops.append({'op': 'set', 'path': ['chat_history', 'max_tokens'], 'value': ch_patch[6]})
                    if ch_patch[7] is not None:
                        ops.append({'op': 'set', 'path': ['chat_history', 'display_limit'], 'value': ch_patch[7]})
                    reload_secs.append('chat_history')
                if cn_patch is not None:
                    prev_cn_rep = str(((merged.get('call_names') or {}).get('reply')) or '我在，请说').strip()
                    if cn_patch[1] != prev_cn_rep:
                        cn_reply_changed = True
                    ops.extend([
                        {'op': 'set', 'path': ['call_names', 'enabled'], 'value': cn_patch[0]},
                        {'op': 'set', 'path': ['call_names', 'reply'], 'value': cn_patch[1]},
                    ])
                    if cn_patch[2] is not None:
                        if cn_patch[2]:
                            ops.append({'op': 'set', 'path': ['call_names', 'models'], 'value': cn_patch[2]})
                        else:
                            ops.append({'op': 'del', 'path': ['call_names', 'models']})
                    reload_secs.append('call_names')
                mutate_hub_yaml(ops, self.cfg_lock)

                if not keep_default:
                    ag_p = HUB_DIR / 'data' / 'agent_sessions.json'
                    if ag_p.is_file():
                        ag_d = read_json(ag_p, {})
                        if isinstance(ag_d, dict) and ag_d.get('last_model'):
                            ag_d['last_model'] = {}
                            write_private_json(ag_p, ag_d)

                if tut_vals is not None:
                    t_fast, t_deep, t_bak, mob_url, c_rot, c_zoom, c_bri = tut_vals
                    prefs = read_json(TUTOR_STATE / 'prefs.json', {})
                    if not isinstance(prefs, dict):
                        prefs = {}
                    prefs['tutor_models'] = {'fast': t_fast, 'deep': t_deep, 'backup': t_bak}
                    if mob_url:
                        prefs['mobile_entry_url'] = mob_url
                    elif 'mobile_entry_url' in tut:
                        prefs.pop('mobile_entry_url', None)
                    prev_cam = (prefs.get('cam_rotate'), prefs.get('cam_zoom'), prefs.get('cam_bright'))
                    if c_rot is not None:
                        prefs['cam_rotate'] = c_rot
                    if c_zoom is not None:
                        prefs['cam_zoom'] = c_zoom
                    if c_bri is not None:
                        prefs['cam_bright'] = c_bri
                    next_cam = (prefs.get('cam_rotate'), prefs.get('cam_zoom'), prefs.get('cam_bright'))
                    if next_cam != prev_cam:
                        tut_cam_push = (c_rot, c_zoom, c_bri)
                    write_private_json(TUTOR_STATE / 'prefs.json', prefs)

        await asyncio.get_running_loop().run_in_executor(None, _write)
        if tut_cam_push is not None:
            p_rot, p_zoom, p_bri = tut_cam_push
            if p_rot is not None:
                with contextlib.suppress(Exception):
                    async with self.sess.post(
                        TUTOR_API + '/api/tutor/camera/rotate',
                        json={'rotate': p_rot},
                        headers={'Origin': TUTOR_API},
                        timeout=aiohttp.ClientTimeout(total=1.5),
                    ) as cr:
                        await cr.read()
            ctrl_b = {}
            if p_zoom is not None:
                ctrl_b['zoom'] = p_zoom
            if p_bri is not None:
                ctrl_b['bright_boost'] = p_bri
            if ctrl_b:
                with contextlib.suppress(Exception):
                    async with self.sess.post(
                        TUTOR_API + '/api/tutor/camera/ctrl',
                        json=ctrl_b,
                        headers={'Origin': TUTOR_API},
                        timeout=aiohttp.ClientTimeout(total=1.5),
                    ) as cr2:
                        await cr2.read()
        note = await self._notify_hub_reload(reload_secs, reset_device_llm=not keep_default)
        if cn_reply_changed:
            await self._clear_hub_tts_cache()
        if keep_default and vendor is not None:
            note = f'已保存「{vendor["label"]}」参数（当前默认主对话模型保持不变）'
        if await self._probe_client_idle():
            with contextlib.suppress(Exception):
                async with self.sess.post(
                    CLIENT_API + '/api/model',
                    json={'kind': 'sync' if keep_default else 'default'},
                    timeout=aiohttp.ClientTimeout(total=2.5),
                ) as resp:
                    await resp.read()
        log.info('settings/llm updated: mode=%s vendor=%s keep_default=%s', mode, 'claude_cli' if mode == 'cli' else vendor['id'], keep_default)
        return jok(note=note, **(await self._full_settings()))

    async def save_settings_voice(self, request):
        d = await json_body(request, 65536)
        if str(d.get('action') or '').strip() == 'clear_tts_cache':
            await self._clear_hub_tts_cache()
            log.info('settings/voice clear_tts_cache')
            return jok(note='已清理唤醒词问候与呼名切换的旧语音缓存（下次唤醒将按当前音色重新生成）', **(await self._full_settings()))

        tts_in = d.get('tts') if isinstance(d.get('tts'), dict) else {}
        asr_in = d.get('asr') if isinstance(d.get('asr'), dict) else {}
        vad_in = d.get('vad') if isinstance(d.get('vad'), dict) else None
        wk_in = d.get('wakeup') if isinstance(d.get('wakeup'), dict) else None
        tts_sel = str(tts_in.get('selected') or 'EdgeTTS').strip()
        if tts_sel not in ('IndexStreamTTS', 'EdgeTTS', 'HuoshanDoubleStreamTTSV2', 'CosyVoiceSiliconflow', 'OpenAITTS'):
            raise ApiError('不支持这个语音合成方案')
        asr_sel = str(asr_in.get('selected') or 'SherpaXASR').strip()
        if asr_sel not in ('MacASR', 'SherpaXASR', 'DoubaoStreamASRV2'):
            raise ApiError('不支持这个语音识别方案')

        ops = [
            {'op': 'set', 'path': ['selected_module', 'TTS'], 'value': tts_sel},
            {'op': 'set', 'path': ['selected_module', 'ASR'], 'value': asr_sel},
        ]
        reload_secs = ['TTS', 'ASR']

        if 'prebuffer_count' in tts_in:
            try:
                prebuf = max(0, min(60, int(tts_in.get('prebuffer_count') if tts_in.get('prebuffer_count') is not None else 30)))
            except (TypeError, ValueError):
                prebuf = 30
            ops.append({'op': 'set', 'path': ['tts_audio_prebuffer_count'], 'value': prebuf})
            reload_secs.append('tts_audio_prebuffer_count')
        if 'send_delay_ms' in tts_in and tts_in.get('send_delay_ms') is not None:
            s_dly = _int_or(tts_in.get('send_delay_ms'), 0, 0, 120)
            ops.append({'op': 'set', 'path': ['tts_audio_send_delay'], 'value': s_dly})
            reload_secs.append('tts_audio_send_delay')
        if 'timeout' in tts_in:
            try:
                t_tout = max(5, min(60, int(tts_in.get('timeout') or 15)))
            except (TypeError, ValueError):
                t_tout = 15
            ops.append({'op': 'set', 'path': ['tts_timeout'], 'value': t_tout})

        # Validate TTS inputs (active mode required; other filled modes also saved so multi-tab edits never drop)
        sync_study_tts = None
        ic = tts_in.get('index_stream') if isinstance(tts_in.get('index_stream'), dict) else {}
        if tts_sel == 'IndexStreamTTS' or str(ic.get('api_url') or '').strip():
            api_url = check_safe_url(ic.get('api_url', ''), '流式克隆 TTS 地址', allow_empty=False)
            voice = str(ic.get('voice') or 'xiaohe').strip()[:64] or 'xiaohe'
            ops.extend([
                {'op': 'set', 'path': ['TTS', 'IndexStreamTTS', 'type'], 'value': 'index_stream'},
                {'op': 'set', 'path': ['TTS', 'IndexStreamTTS', 'api_url'], 'value': api_url},
                {'op': 'set', 'path': ['TTS', 'IndexStreamTTS', 'voice'], 'value': voice},
                {'op': 'set', 'path': ['TTS', 'IndexStreamTTS', 'audio_format'], 'value': 'pcm'},
                {'op': 'set', 'path': ['TTS', 'IndexStreamTTS', 'output_dir'], 'value': 'tmp/'},
            ])
            if tts_sel == 'IndexStreamTTS' and d.get('sync_tts_to_study'):
                sync_study_tts = (api_url, voice)

        ec = tts_in.get('edge') if isinstance(tts_in.get('edge'), dict) else {}
        if tts_sel == 'EdgeTTS' or str(ec.get('voice') or '').strip() or 'rate' in ec or 'pitch' in ec or 'volume' in ec:
            ev = str(ec.get('voice') or 'zh-CN-XiaoxiaoNeural').strip()
            if ev not in EDGE_VOICE_SET:
                if tts_sel == 'EdgeTTS':
                    raise ApiError('请选择支持的 EdgeTTS 音色')
            else:
                ops.extend([
                    {'op': 'set', 'path': ['TTS', 'EdgeTTS', 'type'], 'value': 'edge'},
                    {'op': 'set', 'path': ['TTS', 'EdgeTTS', 'voice'], 'value': ev},
                    {'op': 'set', 'path': ['TTS', 'EdgeTTS', 'output_dir'], 'value': 'tmp/'},
                ])
                if 'rate' in ec:
                    ops.append({'op': 'set', 'path': ['TTS', 'EdgeTTS', 'rate'], 'value': _int_or(ec.get('rate'), 0, -50, 50)})
                if 'pitch' in ec:
                    ops.append({'op': 'set', 'path': ['TTS', 'EdgeTTS', 'pitch'], 'value': _int_or(ec.get('pitch'), 0, -50, 50)})
                if 'volume' in ec:
                    ops.append({'op': 'set', 'path': ['TTS', 'EdgeTTS', 'volume'], 'value': f"{_int_or(ec.get('volume'), 50, -50, 100):+d}"})
                if tts_sel == 'EdgeTTS' and d.get('sync_tts_to_study'):
                    sync_study_tts = ('', ev)

        hc = tts_in.get('huoshan') if isinstance(tts_in.get('huoshan'), dict) else {}
        if tts_sel == 'HuoshanDoubleStreamTTSV2' or str(hc.get('appid') or '').strip() or str(hc.get('access_token') or '').strip() or hc.get('clear_access_token'):
            ws_url = check_safe_url(hc.get('ws_url') or 'wss://openspeech.bytedance.com/api/v3/tts/bidirection',
                                    '火山 WebSocket 地址', allow_empty=False, schemes=('ws', 'wss'))
            appid = str(hc.get('appid') or '').strip()[:64]
            speaker = str(hc.get('speaker') or 'zh_female_wanwanxiaohe_moon_bigtts').strip()[:96]
            res_id = str(hc.get('resource_id') or 'seed-tts-2.0').strip()[:64]
            tok = check_no_ws_secret(hc.get('access_token', ''), '火山 Access Token')
            if tts_sel == 'HuoshanDoubleStreamTTSV2' and not appid:
                raise ApiError('请填写火山 TTS 的 AppID')
            if appid or tok or hc.get('clear_access_token'):
                ops.extend([
                    {'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'type'], 'value': 'huoshan_double_stream'},
                    {'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'ws_url'], 'value': ws_url},
                    {'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'appid'], 'value': appid},
                    {'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'speaker'], 'value': speaker},
                    {'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'resource_id'], 'value': res_id},
                ])
                if 'speech_rate' in hc:
                    ops.append({'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'audio_params', 'speech_rate'], 'value': _int_or(hc.get('speech_rate'), 0, -50, 100)})
                if 'loudness_rate' in hc:
                    ops.append({'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'audio_params', 'loudness_rate'], 'value': _int_or(hc.get('loudness_rate'), 0, -50, 100)})
                if tok or hc.get('clear_access_token'):
                    ops.append({'op': 'set', 'path': ['TTS', 'HuoshanDoubleStreamTTSV2', 'access_token'], 'value': '' if hc.get('clear_access_token') else tok})

        sc = tts_in.get('siliconflow') if isinstance(tts_in.get('siliconflow'), dict) else {}
        if tts_sel == 'CosyVoiceSiliconflow' or str(sc.get('access_token') or '').strip() or sc.get('clear_access_token'):
            mod = str(sc.get('model') or 'FunAudioLLM/CosyVoice2-0.5B').strip()[:120]
            voi = str(sc.get('voice') or 'FunAudioLLM/CosyVoice2-0.5B:alex').strip()[:120]
            tok = check_no_ws_secret(sc.get('access_token', ''), '硅基流动 API Key')
            ops.extend([
                {'op': 'set', 'path': ['TTS', 'CosyVoiceSiliconflow', 'type'], 'value': 'siliconflow'},
                {'op': 'set', 'path': ['TTS', 'CosyVoiceSiliconflow', 'model'], 'value': mod},
                {'op': 'set', 'path': ['TTS', 'CosyVoiceSiliconflow', 'voice'], 'value': voi},
            ])
            if tok or sc.get('clear_access_token'):
                ops.append({'op': 'set', 'path': ['TTS', 'CosyVoiceSiliconflow', 'access_token'], 'value': '' if sc.get('clear_access_token') else tok})

        oc = tts_in.get('openai_tts') if isinstance(tts_in.get('openai_tts'), dict) else {}
        oa_url_raw = str(oc.get('api_url') or '').strip()
        oa_ak_raw = str(oc.get('api_key') or '').strip()
        if tts_sel == 'OpenAITTS' or oa_ak_raw or oc.get('clear_api_key') or (oa_url_raw and oa_url_raw != 'https://api.openai.com/v1/audio/speech'):
            a_url = check_safe_url(oa_url_raw or 'https://api.openai.com/v1/audio/speech', 'OpenAI TTS 地址')
            mod = str(oc.get('model') or 'tts-1').strip()[:80]
            voi = str(oc.get('voice') or 'alloy').strip()[:80]
            fmt = str(oc.get('format') or '').strip().lower()
            try:
                spd = round(max(0.5, min(2.0, _finite_float(oc.get('speed') if oc.get('speed') is not None else 1.0))), 2)
            except (TypeError, ValueError, OverflowError):
                spd = 1.0
            ak = check_no_ws_secret(oa_ak_raw, 'OpenAI TTS API Key')
            ops.extend([
                {'op': 'set', 'path': ['TTS', 'OpenAITTS', 'type'], 'value': 'openai'},
                {'op': 'set', 'path': ['TTS', 'OpenAITTS', 'api_url'], 'value': a_url},
                {'op': 'set', 'path': ['TTS', 'OpenAITTS', 'model'], 'value': mod},
                {'op': 'set', 'path': ['TTS', 'OpenAITTS', 'voice'], 'value': voi},
                {'op': 'set', 'path': ['TTS', 'OpenAITTS', 'speed'], 'value': spd},
                {'op': 'set', 'path': ['TTS', 'OpenAITTS', 'output_dir'], 'value': 'tmp/'},
            ])
            if fmt in ('pcm', 'wav', 'mp3'):
                ops.append({'op': 'set', 'path': ['TTS', 'OpenAITTS', 'format'], 'value': fmt})
            if ak or oc.get('clear_api_key'):
                ops.append({'op': 'set', 'path': ['TTS', 'OpenAITTS', 'api_key'], 'value': '' if oc.get('clear_api_key') else ak})

        # Validate ASR inputs
        mc = asr_in.get('mac') if isinstance(asr_in.get('mac'), dict) else {}
        if asr_sel == 'MacASR' or str(mc.get('base_url') or '').strip():
            b_url = normalize_mac_asr_url(mc.get('base_url', ''))
            m_name = str(mc.get('model_name') or 'Qwen3-ASR-1.7B').strip()[:80]
            lang = str(mc.get('language') or 'zh').strip()[:16]
            prm = str(mc.get('prompt') or '')[:500]
            try:
                m_tout = max(2, min(30, int(_finite_float(mc.get('timeout') if mc.get('timeout') is not None else 8))))
            except (TypeError, ValueError, OverflowError):
                m_tout = 8
            try:
                m_ctout = round(max(0.5, min(10.0, _finite_float(mc.get('connect_timeout') if mc.get('connect_timeout') is not None else 1.5))), 1)
            except (TypeError, ValueError, OverflowError):
                m_ctout = 1.5
            try:
                m_ret = max(0, min(3, int(mc.get('retries') if mc.get('retries') is not None and not isinstance(mc.get('retries'), bool) else 1)))
            except (TypeError, ValueError, OverflowError):
                m_ret = 1
            ak = check_no_ws_secret(mc.get('api_key', ''), '语音识别 API Key')
            ops.extend([
                {'op': 'set', 'path': ['ASR', 'MacASR', 'type'], 'value': 'openai'},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'base_url'], 'value': b_url},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'model_name'], 'value': m_name},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'language'], 'value': lang},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'prompt'], 'value': prm},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'timeout'], 'value': m_tout},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'connect_timeout'], 'value': m_ctout},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'retries'], 'value': m_ret},
                {'op': 'set', 'path': ['ASR', 'MacASR', 'output_dir'], 'value': 'tmp/'},
            ])
            if 'use_proxy' in mc:
                ops.append({'op': 'set', 'path': ['ASR', 'MacASR', 'use_proxy'], 'value': bool(mc.get('use_proxy'))})
            if ak or mc.get('clear_api_key'):
                ops.append({'op': 'set', 'path': ['ASR', 'MacASR', 'api_key'], 'value': '' if mc.get('clear_api_key') else ak})

        shc = asr_in.get('sherpa') if isinstance(asr_in.get('sherpa'), dict) else {}
        if asr_sel == 'SherpaXASR' or 'num_threads' in shc or 'model_dir' in shc or 'segment_pause_ms' in shc or 'hotwords_score' in shc:
            nth = _int_or(shc.get('num_threads'), 2, 1, 4)
            ops.extend([
                {'op': 'set', 'path': ['ASR', 'SherpaXASR', 'type'], 'value': 'sherpa_onnx_local'},
                {'op': 'set', 'path': ['ASR', 'SherpaXASR', 'num_threads'], 'value': nth},
                {'op': 'set', 'path': ['ASR', 'SherpaXASR', 'output_dir'], 'value': 'tmp/'},
            ])
            if 'segment_pause_ms' in shc:
                ops.append({'op': 'set', 'path': ['ASR', 'SherpaXASR', 'segment_pause_ms'], 'value': _int_or(shc.get('segment_pause_ms'), 0, 0, 3000)})
            if 'hotwords_score' in shc and shc.get('hotwords_score') not in (None, ''):
                try:
                    hws = round(max(1.0, min(5.0, _finite_float(shc['hotwords_score']))), 1)
                except (TypeError, ValueError, OverflowError):
                    hws = 1.5
                ops.append({'op': 'set', 'path': ['ASR', 'SherpaXASR', 'hotwords_score'], 'value': hws})
            if shc.get('model_dir'):
                mdir = str(shc.get('model_dir') or '').strip()
                mmeta = SHERPA_MODEL_BY_DIR.get(mdir)
                if mmeta:
                    ops.extend([
                        {'op': 'set', 'path': ['ASR', 'SherpaXASR', 'model_dir'], 'value': mdir},
                        {'op': 'set', 'path': ['ASR', 'SherpaXASR', 'model_type'], 'value': mmeta['type']},
                    ])

        dc = asr_in.get('doubao') if isinstance(asr_in.get('doubao'), dict) else {}
        if asr_sel == 'DoubaoStreamASRV2' or str(dc.get('appid') or '').strip() or str(dc.get('api_key') or '').strip() or str(dc.get('access_token') or '').strip() or dc.get('clear_api_key') or dc.get('clear_access_token'):
            auth_mode = str(dc.get('auth_mode') or 'appid').strip()
            appid = str(dc.get('appid') or '').strip()[:64]
            res_id = str(dc.get('resource_id') or 'volc.seedasr.sauc.duration').strip()[:64]
            tok = check_no_ws_secret(dc.get('access_token', ''), '豆包 ASR Access Token')
            ak = check_no_ws_secret(dc.get('api_key', ''), '豆包 ASR API Key')
            ops.extend([
                {'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'type'], 'value': 'doubao_stream'},
                {'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'resource_id'], 'value': res_id},
            ])
            if 'enable_multilingual' in dc:
                ops.append({'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'enable_multilingual'], 'value': bool(dc.get('enable_multilingual'))})
            if auth_mode == 'api_key':
                ops.append({'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'appid'], 'value': ''})
                if ak or dc.get('clear_api_key'):
                    ops.append({'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'api_key'], 'value': '' if dc.get('clear_api_key') else ak})
                if dc.get('clear_access_token'):
                    ops.append({'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'access_token'], 'value': ''})
            else:
                if asr_sel == 'DoubaoStreamASRV2' and not appid:
                    raise ApiError('请填写豆包语音识别的 AppID（或切换到新版单 API Key 鉴权）')
                if appid:
                    ops.append({'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'appid'], 'value': appid})
                if tok or dc.get('clear_access_token'):
                    ops.append({'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'access_token'], 'value': '' if dc.get('clear_access_token') else tok})
                ops.append({'op': 'set', 'path': ['ASR', 'DoubaoStreamASRV2', 'api_key'], 'value': ''})

        sess_in = d.get('session') if isinstance(d.get('session'), dict) else None
        if sess_in is not None:
            if 'no_voice_close_sec' in sess_in:
                nv_s = _int_or(sess_in.get('no_voice_close_sec'), 120, 30, 600)
                ops.append({'op': 'set', 'path': ['close_connection_no_voice_time'], 'value': nv_s})
            if 'end_prompt_enabled' in sess_in or 'end_prompt_reply' in sess_in or 'end_prompt_prompt' in sess_in:
                ep_en = bool(sess_in.get('end_prompt_enabled', True))
                ep_rp = str(sess_in.get('end_prompt_reply') if sess_in.get('end_prompt_reply') is not None else '好一会儿没听到你说话，我先休息啦，有事再叫我。').strip()[:120]
                ops.extend([
                    {'op': 'set', 'path': ['end_prompt', 'enable'], 'value': ep_en},
                    {'op': 'set', 'path': ['end_prompt', 'reply'], 'value': ep_rp},
                ])
                if 'end_prompt_prompt' in sess_in:
                    ep_pr = str(sess_in.get('end_prompt_prompt') or '').strip()[:200] or '请你以“时间过得真快”为开头，用富有感情、依依不舍的话来结束这场对话吧！'
                    ops.append({'op': 'set', 'path': ['end_prompt', 'prompt'], 'value': ep_pr})
            if 'exit_commands' in sess_in:
                raw_ex_c = sess_in.get('exit_commands')
                if isinstance(raw_ex_c, list):
                    ex_list = [str(x).strip()[:20] for x in raw_ex_c[:20] if str(x).strip()]
                else:
                    ex_list = [x.strip()[:20] for x in re.split(r'[;；,，、\r\n]+', str(raw_ex_c or '')) if x.strip()][:20]
                ops.append({'op': 'set', 'path': ['exit_commands'], 'value': ex_list or ['退出', '关闭']})
            if 'enable_greeting' in sess_in:
                ops.append({'op': 'set', 'path': ['enable_greeting'], 'value': bool(sess_in.get('enable_greeting', True))})
            if 'wakeup_cache' in sess_in:
                ops.append({'op': 'set', 'path': ['enable_wakeup_words_response_cache'], 'value': bool(sess_in.get('wakeup_cache', True))})
            if 'stop_tts_notify' in sess_in:
                ops.append({'op': 'set', 'path': ['enable_stop_tts_notify'], 'value': bool(sess_in.get('stop_tts_notify', False))})
            if 'websocket_ping' in sess_in:
                ops.append({'op': 'set', 'path': ['enable_websocket_ping'], 'value': bool(sess_in.get('websocket_ping', False))})
            if 'delete_audio' in sess_in:
                ops.append({'op': 'set', 'path': ['delete_audio'], 'value': bool(sess_in.get('delete_audio', True))})
            if 'system_error_response' in sess_in:
                err_rp = str(sess_in.get('system_error_response') or '').strip()[:120] or '现在有点忙，我们稍后再试吧。'
                ops.append({'op': 'set', 'path': ['system_error_response'], 'value': err_rp})

        if 'hotwords' in asr_in:
            hw_list = _parse_hotwords_input(asr_in.get('hotwords'))
            ops.append({'op': 'set', 'path': ['asr_hotwords'], 'value': hw_list})
            reload_secs.append('asr_hotwords')

        if 'corrections' in asr_in:
            corr_map = _parse_corrections_input(asr_in.get('corrections'))
            ops.append({'op': 'set', 'path': ['asr_corrections'], 'value': corr_map})

        if vad_in is not None:
            ops.append({'op': 'set', 'path': ['VAD', 'SileroVAD', 'type'], 'value': 'silero'})
            if 'silence_ms' in vad_in:
                raw_sil = vad_in.get('silence_ms')
                if isinstance(raw_sil, bool):
                    raise ApiError('停顿断句时长必须是 200~5000 毫秒之间的整数')
                try:
                    vms = max(200, min(5000, int(raw_sil if raw_sil not in (None, '') else 800)))
                except (TypeError, ValueError, OverflowError):
                    raise ApiError('停顿断句时长必须是 200~5000 毫秒之间的整数')
                ops.append({'op': 'set', 'path': ['VAD', 'SileroVAD', 'min_silence_duration_ms'], 'value': vms})
            if 'threshold' in vad_in and vad_in.get('threshold') not in (None, ''):
                try:
                    vthr = round(max(0.10, min(0.90, _finite_float(vad_in.get('threshold')))), 2)
                except (TypeError, ValueError, OverflowError):
                    vthr = 0.50
                ops.append({'op': 'set', 'path': ['VAD', 'SileroVAD', 'threshold'], 'value': vthr})
            if 'threshold_low' in vad_in and vad_in.get('threshold_low') not in (None, ''):
                try:
                    vlow = round(max(0.05, min(0.80, _finite_float(vad_in.get('threshold_low')))), 2)
                except (TypeError, ValueError, OverflowError):
                    vlow = 0.30
                ops.append({'op': 'set', 'path': ['VAD', 'SileroVAD', 'threshold_low'], 'value': vlow})
            reload_secs.append('VAD')

        wk_patch = None
        if wk_in is not None:
            raw_ww = wk_in.get('wake_words')
            if isinstance(raw_ww, list):
                existing_cc = read_json(CLIENT_CFG, {})
                existing_ww = set(existing_cc.get('wake_words') or []) if isinstance(existing_cc, dict) else set()
                clean_ww = []
                for item in raw_ww[:6]:
                    s = str(item or '').strip()
                    if (s in WAKE_PRESET_MAP or s in existing_ww) and s not in clean_ww:
                        clean_ww.append(s)
                if not clean_ww:
                    raise ApiError('请至少保留一个唤醒词')
            else:
                clean_ww = None
            raw_kg = wk_in.get('kws_gain_db', 10)
            raw_fs = wk_in.get('follow_up_seconds', 10)
            if isinstance(raw_kg, bool) or isinstance(raw_fs, bool):
                raise ApiError('唤醒灵敏度或免唤醒续听时长格式不对')
            try:
                k_thr = round(max(0.10, min(0.60, _finite_float(wk_in.get('kws_threshold', 0.25)))), 2)
                k_gain = max(0, min(24, int(raw_kg)))
                f_sec = max(0, min(30, int(raw_fs)))
            except (TypeError, ValueError, OverflowError):
                raise ApiError('唤醒灵敏度或免唤醒续听时长格式不对')
            wk_patch = {'kws_threshold': k_thr, 'kws_gain_db': k_gain, 'follow_up_seconds': f_sec}
            if 'kws_score' in wk_in and wk_in.get('kws_score') not in (None, ''):
                try:
                    wk_patch['kws_score'] = round(max(1.0, min(4.0, _finite_float(wk_in.get('kws_score')))), 1)
                except (TypeError, ValueError, OverflowError):
                    raise ApiError('唤醒词得分加权格式不对')
            if 'kws_paths' in wk_in and wk_in.get('kws_paths') not in (None, ''):
                wk_patch['kws_paths'] = _int_or(wk_in.get('kws_paths'), 8, 4, 16)
            if 'wake_verify' in wk_in:
                wk_patch['wake_verify'] = 'hub' if (wk_in.get('wake_verify') in ('hub', True, 1, '1')) else ''
            if clean_ww is not None:
                wk_patch['wake_words'] = clean_ww
                # 同步机身唤醒词中文词面到小智 wakeup_words，使静默唤醒与秒级预缓存应答对「小麦小麦」等真正生效
                ww_sync = []
                for w_item in clean_ww:
                    lbl = re.sub(r'[^\w\u4e00-\u9fff]+', '', w_item.split('@', 1)[1]) if '@' in w_item else ''
                    if lbl and lbl not in ww_sync:
                        ww_sync.append(lbl)
                for keep_w in ('小麦小麦', '哈喽小麦', '小麦同学', '嘿你好呀', '你好小智'):
                    if keep_w not in ww_sync:
                        ww_sync.append(keep_w)
                ops.append({'op': 'set', 'path': ['wakeup_words'], 'value': ww_sync})

        study_num_payload = None
        wk_changed = False
        tts_cache_stale = False

        def _write():
            nonlocal study_num_payload, wk_changed, tts_cache_stale
            with self.cfg_lock:
                merged, _ = load_hub_configs()
                ttss = merged.get('TTS') if isinstance(merged.get('TTS'), dict) else {}
                asrs = merged.get('ASR') if isinstance(merged.get('ASR'), dict) else {}
                prev_tts_sel = str((merged.get('selected_module') or {}).get('TTS') or '')
                prev_tts_ent = json.dumps(ttss.get(prev_tts_sel) or {}, sort_keys=True, ensure_ascii=False)
                if tts_sel == 'HuoshanDoubleStreamTTSV2':
                    cur_tok = (ttss.get('HuoshanDoubleStreamTTSV2') or {}).get('access_token')
                    if ((tts_in.get('huoshan') or {}).get('clear_access_token') or is_placeholder_secret(cur_tok)) and not (tts_in.get('huoshan') or {}).get('access_token'):
                        raise ApiError('请填写火山 TTS 的 Access Token')
                elif tts_sel == 'CosyVoiceSiliconflow':
                    cur_tok = (ttss.get('CosyVoiceSiliconflow') or {}).get('access_token')
                    if ((tts_in.get('siliconflow') or {}).get('clear_access_token') or is_placeholder_secret(cur_tok)) and not (tts_in.get('siliconflow') or {}).get('access_token'):
                        raise ApiError('请填写硅基流动的 API Key')
                oa_in_d = tts_in.get('openai_tts') if isinstance(tts_in.get('openai_tts'), dict) else {}
                oa_u_eff = str(oa_in_d.get('api_url') or clean_text_val((ttss.get('OpenAITTS') or {}).get('api_url')) or 'https://api.openai.com/v1/audio/speech')
                cur_oa_tok = (ttss.get('OpenAITTS') or {}).get('api_key')
                if tts_sel == 'OpenAITTS' and 'api.openai.com' in oa_u_eff.lower():
                    if (oa_in_d.get('clear_api_key') or is_placeholder_secret(cur_oa_tok)) and not oa_in_d.get('api_key'):
                        raise ApiError('使用 OpenAI 官方 TTS 接口时请填写 API Key')
                elif (tts_sel == 'OpenAITTS' or oa_url_raw) and 'api.openai.com' not in oa_u_eff.lower():
                    if is_placeholder_secret(cur_oa_tok) and not oa_in_d.get('api_key') and not oa_in_d.get('clear_api_key'):
                        ops.append({'op': 'set', 'path': ['TTS', 'OpenAITTS', 'api_key'], 'value': 'local'})
                if asr_sel == 'DoubaoStreamASRV2':
                    dc = asr_in.get('doubao') if isinstance(asr_in.get('doubao'), dict) else {}
                    db_cur = asrs.get('DoubaoStreamASRV2') if isinstance(asrs.get('DoubaoStreamASRV2'), dict) else {}
                    if str(dc.get('auth_mode') or 'appid').strip() == 'api_key':
                        if (dc.get('clear_api_key') or is_placeholder_secret(db_cur.get('api_key'))) and not dc.get('api_key'):
                            raise ApiError('请填写豆包语音识别的 API Key')
                    else:
                        if (dc.get('clear_access_token') or is_placeholder_secret(db_cur.get('access_token'))) and not dc.get('access_token'):
                            raise ApiError('请填写豆包语音识别的 Access Token')
                mutate_hub_yaml(ops, self.cfg_lock)
                merged_after, _ = load_hub_configs()
                next_tts_ent = json.dumps(((merged_after.get('TTS') or {}).get(tts_sel)) or {}, sort_keys=True, ensure_ascii=False)
                if tts_sel != prev_tts_sel or next_tts_ent != prev_tts_ent:
                    tts_cache_stale = True
                if sync_study_tts is not None:
                    scfg = read_json(STUDY_STATE / 'config.json', {})
                    if not isinstance(scfg, dict):
                        scfg = {}
                    prev_st_tts = (str(scfg.get('tts_url') or ''), str(scfg.get('voice') or 'xiaohe'))
                    scfg['tts_url'], scfg['voice'] = sync_study_tts
                    write_private_json(STUDY_STATE / 'config.json', scfg)
                    if prev_st_tts != sync_study_tts:
                        _clear_study_voice_cache()
                    study_num_payload = {
                        'auto_off_hours': max(0, min(12, int(scfg.get('auto_off_hours') if scfg.get('auto_off_hours') is not None else 3))),
                        'interval': max(30, min(600, int(scfg.get('interval') or 60))),
                        'cooldown': max(60, min(3600, int(scfg.get('cooldown') or 300))),
                        'min_gap': max(30, min(1800, int(scfg.get('min_gap') or 90))),
                        'burst_frames': max(1, min(4, int(scfg.get('burst_frames') or 3))),
                        'burst_gap': max(1, min(8, int(scfg.get('burst_gap') or 4))),
                    }
                if wk_patch is not None:
                    cur_cc = read_json(CLIENT_CFG, {})
                    if not isinstance(cur_cc, dict):
                        cur_cc = {}
                    def_ww = [w['raw'] for w in WAKE_PRESETS]
                    cur_ww = cur_cc.get('wake_words') if isinstance(cur_cc.get('wake_words'), list) else def_ww
                    cur_wv = 'hub' if str(cur_cc.get('wake_verify') or '').strip() == 'hub' else ''
                    try:
                        cur_thr = round(float(cur_cc.get('kws_threshold', 0.25)), 2)
                        cur_sc = round(float(cur_cc.get('kws_score', 2.0)), 1)
                        cur_gain = int(cur_cc.get('kws_gain_db', 10))
                        cur_fsec = int(cur_cc.get('follow_up_seconds', 10))
                    except (TypeError, ValueError):
                        cur_thr, cur_sc, cur_gain, cur_fsec = 0.25, 2.0, 10, 10
                    cur_paths = _int_or(cur_cc.get('kws_paths'), 8, 4, 16)
                    diff_wk = (
                        ('wake_words' in wk_patch and wk_patch['wake_words'] != cur_ww)
                        or ('wake_verify' in wk_patch and wk_patch['wake_verify'] != cur_wv)
                        or ('kws_score' in wk_patch and wk_patch['kws_score'] != cur_sc)
                        or ('kws_paths' in wk_patch and wk_patch['kws_paths'] != cur_paths)
                        or wk_patch['kws_threshold'] != cur_thr
                        or wk_patch['kws_gain_db'] != cur_gain
                        or wk_patch['follow_up_seconds'] != cur_fsec
                    )
                    if not CLIENT_CFG.is_file() or diff_wk:
                        wk_changed = diff_wk
                        write_client_cfg_patch(wk_patch)

        await asyncio.get_running_loop().run_in_executor(None, _write)
        if study_num_payload is not None:
            with contextlib.suppress(Exception):
                async with self.sess.post(
                    STUDY_API + '/api/study/config',
                    json=study_num_payload,
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    await resp.read()
        note = await self._notify_hub_reload(reload_secs)
        if tts_cache_stale:
            await self._clear_hub_tts_cache()
        if wk_changed:
            note += '（机身离线唤醒词与灵敏度已写入，重启设备后生效）'
        log.info('settings/voice updated: tts=%s asr=%s wk_changed=%s', tts_sel, asr_sel, wk_changed)
        return jok(note=note, **(await self._full_settings()))

    async def save_settings_apis(self, request):
        d = await json_body(request, 65536)
        if d.get('action') == 'clear_news_seen':
            seen_p = HUB_DIR / 'data' / 'newsnow_seen.json'
            await asyncio.get_running_loop().run_in_executor(None, write_private_json, seen_p, {})
            log.info('settings/apis clear_news_seen')
            return jok(note='已清空已读新闻防重记录，下次语音点播新闻将重新拉取最新热榜')

        qw = d.get('qweather') if isinstance(d.get('qweather'), dict) else {}
        ws = d.get('web_search') if isinstance(d.get('web_search'), dict) else {}
        nw = d.get('news') if isinstance(d.get('news'), dict) else {}
        ha_in = d.get('home_assistant') if isinstance(d.get('home_assistant'), dict) else None
        rf_in = d.get('ragflow') if isinstance(d.get('ragflow'), dict) else None
        vl_in = d.get('vllm') if isinstance(d.get('vllm'), dict) else None
        cn_rss_in = d.get('chinanews') if isinstance(d.get('chinanews'), dict) else None
        cn_rss_patch = None
        if cn_rss_in is not None:
            cn_rss_patch = {}
            for rk, rlbl in (
                ('default_rss_url', '中新网综合要闻 RSS 地址'),
                ('society_rss_url', '中新网社会频道 RSS 地址'),
                ('world_rss_url', '中新网国际频道 RSS 地址'),
                ('finance_rss_url', '中新网财经频道 RSS 地址'),
            ):
                if rk in cn_rss_in:
                    ru = str(cn_rss_in.get(rk) or '').strip()
                    cn_rss_patch[rk] = check_safe_url(ru, rlbl, allow_empty=True) if ru else ''
        raw_plugs = d.get('plugins_enabled') if isinstance(d.get('plugins_enabled'), list) else None
        tool_tout = None
        if 'tool_call_timeout' in d and d.get('tool_call_timeout') is not None:
            try:
                tool_tout = max(10, min(120, int(d.get('tool_call_timeout') or 30)))
            except (TypeError, ValueError):
                tool_tout = 30
        mcp_ep_val = None
        if 'mcp_endpoint' in d:
            raw_mep = str(d.get('mcp_endpoint') or '').strip()
            mcp_ep_val = check_safe_url(raw_mep, 'MCP 远端接入点', allow_empty=True, schemes=('ws', 'wss')) if raw_mep else ''

        ha_patch = None
        if ha_in is not None:
            ha_en = bool(ha_in.get('enabled', False))
            raw_ha_u = str(ha_in.get('base_url') or '').strip()
            ha_url = check_safe_url(raw_ha_u, 'Home Assistant 地址', allow_empty=True) if raw_ha_u else ''
            ha_key = check_no_ws_secret(ha_in.get('api_key', ''), 'Home Assistant 令牌')
            ha_clr = bool(ha_in.get('clear_api_key', False))
            raw_devs = ha_in.get('devices')
            if isinstance(raw_devs, list):
                ha_devs = [str(x).strip()[:160] for x in raw_devs[:60] if str(x).strip()]
            else:
                ha_devs = [ln.strip()[:160] for ln in str(raw_devs or '').splitlines() if ln.strip()][:60]
            if ha_en and not ha_url:
                raise ApiError('开启 Home Assistant 语音控制前请先填写服务地址')
            ha_patch = (ha_en, ha_url, ha_key, ha_clr, ha_devs)

        rf_patch = None
        if rf_in is not None:
            rf_en = bool(rf_in.get('enabled', False))
            raw_rf_u = str(rf_in.get('base_url') or '').strip()
            rf_url = check_safe_url(raw_rf_u, 'RAGFlow 地址', allow_empty=True) if raw_rf_u else ''
            rf_key = check_no_ws_secret(rf_in.get('api_key', ''), 'RAGFlow API Key')
            rf_clr = bool(rf_in.get('clear_api_key', False))
            raw_ds = rf_in.get('dataset_ids')
            if isinstance(raw_ds, list):
                rf_ds = [str(x).strip()[:80] for x in raw_ds[:30] if str(x).strip()]
            else:
                rf_ds = [x.strip()[:80] for x in re.split(r'[;；,，、\s]+', str(raw_ds or '')) if x.strip()][:30]
            rf_desc = str(rf_in.get('description') or '').strip()[:300]
            if rf_en and (not rf_url or not rf_ds):
                raise ApiError('开启 RAGFlow 知识库前请填写服务地址与至少 1 个知识库 ID')
            rf_patch = (rf_en, rf_url, rf_key, rf_clr, rf_ds, rf_desc)

        vl_patch = None
        if vl_in is not None:
            raw_vl_u = strip_chat_completions_suffix(vl_in.get('base_url', ''))
            vl_url = check_safe_url(raw_vl_u, '视觉识图接口地址', allow_empty=True) if raw_vl_u else ''
            vl_mod = str(vl_in.get('model') or '').strip()[:120]
            vl_key = check_no_ws_secret(vl_in.get('api_key', ''), '视觉识图 API Key')
            vl_clr = bool(vl_in.get('clear_api_key', False))
            if vl_url and (not vl_mod or any(ch.isspace() for ch in vl_mod)):
                raise ApiError('填写视觉识图地址时请填写正确的视觉模型名称（不能含空格）')
            vl_patch = (vl_url, vl_mod, vl_key, vl_clr)

        qw_host = normalize_qweather_host(qw.get('api_host', ''))
        qw_key = check_no_ws_secret(qw.get('api_key', ''), '和风天气 API Key')
        if qw_key and not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', qw_key):
            raise ApiError('和风天气 API Key 格式不对（8~128 位字母数字）')
        city = str(qw.get('city') or '北京').strip()
        if not (1 <= len(city) <= 20):
            raise ApiError('主城市名请在 1~20 个字以内')
        raw_ex = qw.get('extra_cities')
        extra_cities = []
        if isinstance(raw_ex, list):
            for x in raw_ex[:8]:
                xs = str(x).strip()
                if 0 < len(xs) <= 20 and xs != city and xs not in extra_cities:
                    extra_cities.append(xs)
        else:
            extra_cities = ['上海', '广州', '深圳']

        ws_enabled = bool(ws.get('enabled', True))
        ws_url = check_safe_url(ws.get('base_url') or 'https://api.deepseek.com/anthropic/v1', '联网搜索地址', allow_empty=True)
        ws_mod = str(ws.get('model') or 'deepseek-v4-flash').strip()[:80]
        try:
            ws_max = max(1, min(10, int(ws.get('max_uses') or 5)))
        except (TypeError, ValueError):
            ws_max = 5
        ws_key = check_no_ws_secret(ws.get('api_key', ''), '联网搜索 API Key')
        mt_key = check_no_ws_secret(ws.get('metaso_key', ''), '秘塔搜索 Key')
        am_key = check_no_ws_secret(ws.get('amap_key', ''), '高德地图 Key')

        news_src = _parse_news_sources(nw.get('news_sources') or '澎湃新闻;百度热搜;财联社')
        try:
            pool_sz = max(6, min(40, int(nw.get('pool_size') or 20)))
        except (TypeError, ValueError):
            pool_sz = 20
        try:
            batch_sz = max(1, min(8, int(nw.get('batch_size') or 3)))
        except (TypeError, ValueError):
            batch_sz = 3
        try:
            seen_d = max(1, min(30, int(nw.get('seen_days') or 3)))
        except (TypeError, ValueError):
            seen_d = 3
        try:
            det_ch = max(300, min(8000, int(nw.get('detail_chars') or 2000)))
        except (TypeError, ValueError):
            det_ch = 2000
        focus_kw = str(nw.get('focus_keywords') or '').strip()[:1000] if 'focus_keywords' in nw else None
        news_url = None
        if 'url' in nw:
            raw_nu = str(nw.get('url') or '').strip()
            news_url = check_safe_url(raw_nu, '自建热榜镜像地址', allow_empty=True) if raw_nu else ''

        def _write():
            with self.cfg_lock:
                merged, _ = load_hub_configs()
                fc = ((merged.get('Intent') or {}).get('function_call') or {})
                cur_funcs = [str(f).strip() for f in (fc.get('functions') or ['get_weather', 'get_news_from_newsnow', 'get_news_from_chinanews', 'play_music', 'web_search']) if str(f).strip()]
                if raw_plugs is not None:
                    chosen = [str(p).strip() for p in raw_plugs if str(p).strip() in INTENT_PLUGIN_IDS]
                    if 'enabled' in ws:
                        if ws_enabled and 'web_search' not in chosen:
                            chosen.append('web_search')
                        elif not ws_enabled and 'web_search' in chosen:
                            chosen = [f for f in chosen if f != 'web_search']
                    extras = [f for f in cur_funcs if f not in INTENT_PLUGIN_IDS]
                    next_funcs = []
                    for f in chosen + extras:
                        if f not in next_funcs:
                            next_funcs.append(f)
                    cur_funcs = next_funcs
                else:
                    if ws_enabled and 'web_search' not in cur_funcs:
                        cur_funcs.append('web_search')
                    elif not ws_enabled and 'web_search' in cur_funcs:
                        cur_funcs = [f for f in cur_funcs if f != 'web_search']
                if ha_patch is not None:
                    if ha_patch[0]:
                        cur_ha_k = ((merged.get('plugins') or {}).get('home_assistant') or {}).get('api_key')
                        if (ha_patch[3] or is_placeholder_secret(cur_ha_k)) and not ha_patch[2]:
                            raise ApiError('开启 Home Assistant 语音控制前请填写长期访问令牌')
                        for hf in HASS_PLUGIN_FUNCS:
                            if hf not in cur_funcs:
                                cur_funcs.append(hf)
                    else:
                        cur_funcs = [f for f in cur_funcs if f not in HASS_PLUGIN_FUNCS]
                if rf_patch is not None:
                    if rf_patch[0]:
                        cur_rf_k = ((merged.get('plugins') or {}).get('search_from_ragflow') or {}).get('api_key')
                        if (rf_patch[3] or is_placeholder_secret(cur_rf_k)) and not rf_patch[2]:
                            raise ApiError('开启 RAGFlow 知识库前请填写 API Key')
                        if 'search_from_ragflow' not in cur_funcs:
                            cur_funcs.append('search_from_ragflow')
                    else:
                        cur_funcs = [f for f in cur_funcs if f != 'search_from_ragflow']

                ops = [
                    {'op': 'set', 'path': ['plugins', 'get_weather', 'api_host'], 'value': qw_host},
                    {'op': 'set', 'path': ['plugins', 'get_weather', 'location'], 'value': city},
                    {'op': 'set', 'path': ['plugins', 'get_weather', 'default_location'], 'value': city},
                    {'op': 'set', 'path': ['Intent', 'function_call', 'type'], 'value': 'function_call'},
                    {'op': 'set', 'path': ['Intent', 'function_call', 'functions'], 'value': cur_funcs},
                    {'op': 'del', 'path': ['plugins', 'web_search', 'provider']},
                    {'op': 'del', 'path': ['plugins', 'web_search', 'max_results']},
                    {'op': 'set', 'path': ['plugins', 'web_search', 'base_url'], 'value': ws_url},
                    {'op': 'set', 'path': ['plugins', 'web_search', 'model'], 'value': ws_mod},
                    {'op': 'set', 'path': ['plugins', 'web_search', 'max_uses'], 'value': ws_max},
                    {'op': 'set', 'path': ['plugins', 'get_news_from_newsnow', 'news_sources'], 'value': news_src},
                    {'op': 'set', 'path': ['plugins', 'get_news_from_newsnow', 'pool_size'], 'value': pool_sz},
                    {'op': 'set', 'path': ['plugins', 'get_news_from_newsnow', 'batch_size'], 'value': batch_sz},
                    {'op': 'set', 'path': ['plugins', 'get_news_from_newsnow', 'seen_days'], 'value': seen_d},
                    {'op': 'set', 'path': ['plugins', 'get_news_from_newsnow', 'detail_chars'], 'value': det_ch},
                ]
                if tool_tout is not None:
                    ops.append({'op': 'set', 'path': ['tool_call_timeout'], 'value': tool_tout})
                if mcp_ep_val is not None:
                    if mcp_ep_val:
                        ops.append({'op': 'set', 'path': ['mcp_endpoint'], 'value': mcp_ep_val})
                    else:
                        ops.append({'op': 'del', 'path': ['mcp_endpoint']})
                if ha_patch is not None:
                    _, ha_u, ha_k, ha_clr, ha_dv = ha_patch
                    if ha_u:
                        ops.append({'op': 'set', 'path': ['plugins', 'home_assistant', 'base_url'], 'value': ha_u})
                    if ha_dv or 'devices' in (ha_in or {}):
                        ops.append({'op': 'set', 'path': ['plugins', 'home_assistant', 'devices'], 'value': ha_dv})
                    if ha_k or ha_clr:
                        ops.append({'op': 'set', 'path': ['plugins', 'home_assistant', 'api_key'], 'value': '' if ha_clr else ha_k})
                if rf_patch is not None:
                    _, rf_u, rf_k, rf_clr, rf_dids, rf_dsc = rf_patch
                    if rf_u:
                        ops.append({'op': 'set', 'path': ['plugins', 'search_from_ragflow', 'base_url'], 'value': rf_u})
                    if rf_dids or 'dataset_ids' in (rf_in or {}):
                        ops.append({'op': 'set', 'path': ['plugins', 'search_from_ragflow', 'dataset_ids'], 'value': rf_dids})
                    if rf_dsc:
                        ops.append({'op': 'set', 'path': ['plugins', 'search_from_ragflow', 'description'], 'value': rf_dsc})
                    if rf_k or rf_clr:
                        ops.append({'op': 'set', 'path': ['plugins', 'search_from_ragflow', 'api_key'], 'value': '' if rf_clr else rf_k})
                if vl_patch is not None:
                    vl_u, vl_m, vl_k, vl_clr = vl_patch
                    if vl_u:
                        vllms_m = merged.get('VLLM') if isinstance(merged.get('VLLM'), dict) else {}
                        sel_v = str((merged.get('selected_module') or {}).get('VLLM') or 'ChatGLMVLLM')
                        cur_vl = (vllms_m.get('OpenAIVLLM') if isinstance(vllms_m.get('OpenAIVLLM'), dict) else {}) or {}
                        sel_vl = (vllms_m.get(sel_v) if isinstance(vllms_m.get(sel_v), dict) else {}) or {}
                        def _clean_vk(raw_vk):
                            s_vk = str(raw_vk or '').strip()
                            return '' if (not s_vk or s_vk == 'local' or is_placeholder_secret(s_vk)) else s_vk
                        ex_vl_k = _clean_vk(cur_vl.get('api_key')) or _clean_vk(sel_vl.get('api_key'))
                        fin_vl_k = '' if vl_clr else (vl_k or ex_vl_k)
                        cloud_vl = any(dom in vl_u.lower() for dom in ('bigmodel.cn', 'aliyuncs.com', 'volces.com', 'openai.com', 'anthropic.com', 'googleapis.com'))
                        if not fin_vl_k and cloud_vl and not vl_clr:
                            raise ApiError('使用云端视觉识图接口时请填写 API Key')
                        if not fin_vl_k and not cloud_vl and not vl_clr:
                            fin_vl_k = 'local'
                        ops.extend([
                            {'op': 'set', 'path': ['selected_module', 'VLLM'], 'value': 'OpenAIVLLM'},
                            {'op': 'set', 'path': ['VLLM', 'OpenAIVLLM', 'type'], 'value': 'openai'},
                            {'op': 'set', 'path': ['VLLM', 'OpenAIVLLM', 'base_url'], 'value': vl_u},
                            {'op': 'set', 'path': ['VLLM', 'OpenAIVLLM', 'model_name'], 'value': vl_m},
                            {'op': 'set', 'path': ['VLLM', 'OpenAIVLLM', 'api_key'], 'value': fin_vl_k},
                        ])
                    else:
                        if str((merged.get('selected_module') or {}).get('VLLM') or '') == 'OpenAIVLLM':
                            ops.append({'op': 'set', 'path': ['selected_module', 'VLLM'], 'value': 'ChatGLMVLLM'})
                        ops.append({'op': 'del', 'path': ['VLLM', 'OpenAIVLLM']})
                if focus_kw is not None:
                    if focus_kw:
                        ops.append({'op': 'set', 'path': ['plugins', 'get_news_from_newsnow', 'focus_keywords'], 'value': focus_kw})
                    else:
                        ops.append({'op': 'del', 'path': ['plugins', 'get_news_from_newsnow', 'focus_keywords']})
                if news_url is not None:
                    if news_url:
                        ops.append({'op': 'set', 'path': ['plugins', 'get_news_from_newsnow', 'url'], 'value': news_url})
                    else:
                        ops.append({'op': 'del', 'path': ['plugins', 'get_news_from_newsnow', 'url']})
                if cn_rss_patch is not None:
                    for rk, rv in cn_rss_patch.items():
                        if rv:
                            ops.append({'op': 'set', 'path': ['plugins', 'get_news_from_chinanews', rk], 'value': rv})
                        else:
                            ops.append({'op': 'del', 'path': ['plugins', 'get_news_from_chinanews', rk]})
                if qw_key or qw.get('clear_api_key'):
                    ops.append({'op': 'set', 'path': ['plugins', 'get_weather', 'api_key'], 'value': '' if qw.get('clear_api_key') else qw_key})
                if ws_key or ws.get('clear_api_key'):
                    ops.append({'op': 'set', 'path': ['plugins', 'web_search', 'api_key'], 'value': '' if ws.get('clear_api_key') else ws_key})
                if mt_key or ws.get('clear_metaso_key'):
                    ops.append({'op': 'set', 'path': ['plugins', 'web_search', 'metaso_key'], 'value': '' if ws.get('clear_metaso_key') else mt_key})
                if am_key or ws.get('clear_amap_key'):
                    ops.append({'op': 'set', 'path': ['plugins', 'web_search', 'amap_key'], 'value': '' if ws.get('clear_amap_key') else am_key})
                mutate_hub_yaml(ops, self.cfg_lock)

                hcfg = read_json(HOME_STATE / 'config.json', {})
                if not isinstance(hcfg, dict):
                    hcfg = {}
                hcfg['city'] = city
                hcfg['extra_cities'] = extra_cities
                write_private_json(HOME_STATE / 'config.json', hcfg)

        await asyncio.get_running_loop().run_in_executor(None, _write)
        with contextlib.suppress(Exception):
            async with self.sess.get(
                HOME_API + '/api/home/weather/detail?force=1',
                headers={'Host': '127.0.0.1:8095'},
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                await resp.read()
        note = await self._notify_hub_reload(['Intent', 'VLLM'] if vl_patch is not None else ['Intent'])
        log.info('settings/apis updated: city=%s ws_enabled=%s', city, ws_enabled)
        return jok(note=note, **(await self._full_settings()))

    async def save_settings_study(self, request):
        d = await json_body(request, 16384)
        if d.get('action') == 'toggle_study':
            en = bool(d.get('enabled', False))
            try:
                async with self.sess.post(
                    STUDY_API + '/api/study/toggle',
                    json={'enabled': en},
                    timeout=aiohttp.ClientTimeout(total=4, sock_connect=2),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or (isinstance(rj, dict) and rj.get('ok') is False):
                        raise ApiError(str((rj or {}).get('error') or f'学习陪伴返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '学习陪伴服务'), 502)
            return jok(note='已开启学习陪伴坐姿与专注守护' if en else '已关闭学习陪伴', **(await self._full_settings()))

        if d.get('action') == 'check_now':
            speak_b = bool(d.get('speak', False))
            try:
                async with self.sess.post(
                    STUDY_API + '/api/study/check-now',
                    json={'speak': speak_b},
                    timeout=aiohttp.ClientTimeout(total=25, sock_connect=2),
                ) as r:
                    raw_c = await read_resp_bounded(r, 65536)
                    if r.status == 409:
                        raise ApiError('大屏已有一次检测正在进行中，请稍候几秒再试', 409)
                    rj = json.loads(raw_c.decode('utf-8', errors='replace')) if raw_c else {}
                    if r.status != 200 or (isinstance(rj, dict) and rj.get('ok') is False):
                        raise ApiError(str((rj or {}).get('error') or f'立即检测返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '学习陪伴服务'), 502)
            st_lbl = str(rj.get('status') or rj.get('state') or rj.get('posture') or rj.get('skip') or '完成')
            rem_s = str(rj.get('remind') or rj.get('message') or rj.get('summary') or '').strip()[:60]
            return jok(
                check_res=rj,
                note=f"单次抓拍检测完成（状态：{st_lbl}" + (f" · {rem_s}" if rem_s else '') + '）',
            )

        if d.get('action') in ('focus_start', 'focus_stop'):
            is_st = d.get('action') == 'focus_start'
            f_body = {'action': 'start' if is_st else 'stop'}
            if is_st:
                f_body['minutes'] = _int_or(d.get('minutes'), 25, 1, 180)
                f_body['break_minutes'] = _int_or(d.get('break_minutes'), 5, 0, 60)
                f_body['with_study'] = bool(d.get('with_study', True))
            try:
                async with self.sess.post(
                    STUDY_API + '/api/study/focus',
                    json=f_body,
                    timeout=aiohttp.ClientTimeout(total=5, sock_connect=2),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or (isinstance(rj, dict) and rj.get('ok') is False):
                        raise ApiError(str((rj or {}).get('error') or f'番茄钟服务返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '学习陪伴番茄钟'), 502)
            f_note = f"已开启大屏番茄专注（{f_body['minutes']} 分钟专注 + {f_body['break_minutes']} 分钟休息）" if is_st else '已结束大屏番茄专注计时'
            return jok(note=f_note, **(await self._full_settings()))

        v_url = check_safe_url(d.get('vision_url', ''), '学习视觉接口地址', allow_empty=True)
        t_url = check_safe_url(d.get('tts_url', ''), '学习陪伴语音地址', allow_empty=True)
        voice = str(d.get('voice') or 'xiaohe').strip()[:64] or 'xiaohe'
        try:
            auto_off = max(0, min(12, int(d.get('auto_off_hours', 3))))
            interval = max(30, min(600, int(d.get('interval', 60))))
            cooldown = max(60, min(3600, int(d.get('cooldown', 300))))
            min_gap = max(30, min(1800, int(d.get('min_gap', 90))))
            burst_frames = max(1, min(4, int(d.get('burst_frames', 3))))
            burst_gap = max(1, min(8, int(d.get('burst_gap', 4))))
        except (TypeError, ValueError):
            raise ApiError('时间间隔与连拍参数必须是数字')
        sec_in = str(d.get('secret') or '').strip()
        if sec_in and not re.fullmatch(r'[0-9a-fA-F]{64}', sec_in):
            raise ApiError('端到端加密密钥必须是 64 位十六进制字符（可点「随机生成」一键生成）')

        study_num_patch = {
            'auto_off_hours': auto_off,
            'interval': interval,
            'cooldown': cooldown,
            'min_gap': min_gap,
            'burst_frames': burst_frames,
            'burst_gap': burst_gap,
        }
        study_patch = {
            'vision_url': v_url,
            'tts_url': t_url,
            'voice': voice,
            **study_num_patch,
        }

        def _write():
            with self.cfg_lock:
                scfg = read_json(STUDY_STATE / 'config.json', {})
                if not isinstance(scfg, dict):
                    scfg = {}
                prev_tts = (str(scfg.get('tts_url') or ''), str(scfg.get('voice') or 'xiaohe'))
                scfg.update(study_patch)
                write_private_json(STUDY_STATE / 'config.json', scfg)
                if prev_tts != (t_url, voice):
                    _clear_study_voice_cache()
                if sec_in:
                    write_private_text(STUDY_STATE / 'secret', sec_in.lower() + '\n')
                elif d.get('clear_secret'):
                    with contextlib.suppress(FileNotFoundError):
                        (STUDY_STATE / 'secret').unlink()

        await asyncio.get_running_loop().run_in_executor(None, _write)
        with contextlib.suppress(Exception):
            async with self.sess.post(
                STUDY_API + '/api/study/config',
                json=study_num_patch,
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                await resp.read()
        log.info('settings/study updated')
        return jok(note='学习陪伴配置已保存并立即生效', **(await self._full_settings()))

    async def save_settings_extra(self, request):
        d = await json_body(request, 16384)
        act = str(d.get('action') or '').strip()
        if act == 'qq_bind':
            raw_bind = str(d.get('input') or '').strip()
            if not raw_bind or len(raw_bind) > 4096:
                raise ApiError('请输入有效的 QQ 号或包含 qm_keyst 的 y.qq.com Cookie')
            try:
                async with self.sess.post(
                    QQ_API + '/api/music/bind',
                    json={'input': raw_bind},
                    timeout=aiohttp.ClientTimeout(total=6, sock_connect=2),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or '绑定 QQ 音乐账号失败'), 400)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大屏 QQ 音乐服务'), 502)
            prof = rj.get('profile') if isinstance(rj.get('profile'), dict) else {}
            nick_s = str(prof.get('nickname') or 'QQ 音乐用户')[:32]
            log.info('settings/extra qq_bind ok (vip=%s)', bool(prof.get('isVip')))
            return jok(note=f'已绑定大屏音乐账号：{nick_s}', **(await self._full_settings()))

        if act in ('qq_qr_create', 'qq_qr_check', 'qq_logout'):
            try:
                if act == 'qq_qr_create':
                    q_mode = 'wx' if d.get('mode') == 'wx' else 'qq'
                    async with self.sess.post(QQ_API + '/api/music/qr/create', json={'mode': q_mode}, timeout=aiohttp.ClientTimeout(total=10, sock_connect=2)) as r:
                        rj = json.loads((await read_resp_bounded(r, 262144)).decode('utf-8'))
                        if r.status != 200 or not rj.get('ok'):
                            raise ApiError(str(rj.get('error') or '生成登录二维码失败'), 502)
                        return jok(mode=rj.get('mode') or q_mode, qrsig=rj.get('qrsig') or '', qr_img=rj.get('qr_img') or '')
                if act == 'qq_qr_check':
                    q_mode = 'wx' if d.get('mode') == 'wx' else 'qq'
                    q_sig = str(d.get('qrsig') or '').strip()[:512]
                    if not q_sig:
                        raise ApiError('缺少二维码凭证 qrsig')
                    async with self.sess.post(QQ_API + '/api/music/qr/check', json={'qrsig': q_sig, 'mode': q_mode}, timeout=aiohttp.ClientTimeout(total=8, sock_connect=2)) as r:
                        rj = json.loads((await read_resp_bounded(r, 65536)).decode('utf-8'))
                        done_ok = bool(rj.get('logged_in') or rj.get('status') == 'done' or rj.get('code') == 0 or rj.get('user_profile'))
                        extra_snap = (await self._full_settings()) if done_ok else {}
                        return jok(qr_res=rj, done=done_ok, **extra_snap)
                async with self.sess.post(QQ_API + '/api/music/logout', json={}, timeout=aiohttp.ClientTimeout(total=5, sock_connect=2)) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or '退出登录失败'), 502)
                return jok(note='已退出大屏 QQ 音乐账号', **(await self._full_settings()))
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大屏 QQ 音乐服务'), 502)

        if act == 'qq_sleep':
            s_act = str(d.get('sleep_action') or 'start').strip()
            if s_act not in ('start', 'stop', 'extend', 'screen_off'):
                raise ApiError('不支持的睡前助眠操作')
            if s_act == 'start':
                snd_id = str(d.get('sound') or 'last').strip()[:48]
                dur_raw = str(d.get('duration') or '45').strip()
                s_vol = _int_or(d.get('volume'), 25, 5, 60)
                s_body = {'action': 'start', 'sound': snd_id, 'volume': s_vol}
                if dur_raw == 'night':
                    s_body['night'] = True
                else:
                    s_body['minutes'] = _int_or(dur_raw, 45, 5, 480)
                s_note = '已在大屏启动睡前助眠轻音'
            elif s_act == 'extend':
                s_body = {'action': 'extend', 'minutes': _int_or(d.get('minutes'), 30, 5, 180)}
                s_note = '已将助眠播放延长 30 分钟'
            elif s_act == 'screen_off':
                s_body = {'action': 'screen_off'}
                s_note = '已调暗关闭大屏助眠背光'
            else:
                s_body = {'action': 'stop'}
                s_note = '已结束大屏助眠会话并恢复音量'
            try:
                async with self.sess.post(
                    QQ_API + '/api/music/sleep_session',
                    json=s_body,
                    timeout=aiohttp.ClientTimeout(total=6, sock_connect=2),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or '助眠控制失败'), 400 if r.status == 400 else 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大屏助眠服务'), 502)
            return jok(note=str(rj.get('message') or s_note)[:80], **(await self._full_settings()))

        if act == 'qq_quality':
            q_lvl = str(d.get('quality_level') or 'lossless').strip()
            if q_lvl not in ('lossless', 'hq', 'standard'):
                raise ApiError('请选择支持的在线播放音质档位')
            try:
                q_mb = max(10, min(500, int(d.get('max_cache_mb') or 200)))
            except (TypeError, ValueError):
                q_mb = 200
            r_mode = str(d.get('radio_audio_mode') or '').strip()
            if r_mode and r_mode not in ('hifi', 'voice', 'raw'):
                raise ApiError('请选择支持的电台与听书音效模式')
            p_mode = str(d.get('play_mode') or '').strip()
            if p_mode and p_mode not in ('loop', 'shuffle', 'single'):
                raise ApiError('请选择支持的默认循环播放模式')
            wake_on = bool(d['wake']) if 'wake' in d else None
            wake_t = None
            if 'wake_time' in d and d.get('wake_time'):
                wt_s = str(d['wake_time']).strip()
                if re.match(r'^\d:\d\d$', wt_s):
                    wt_s = '0' + wt_s
                m_wt = re.match(r'^(\d{2}):(\d{2})$', wt_s)
                hm = (int(m_wt.group(1)), int(m_wt.group(2))) if m_wt else None
                if not hm or hm[1] > 59 or not ((4, 30) <= hm <= (10, 0)):
                    raise ApiError('晨间温柔叫醒时间请在 04:30 ~ 10:00 之间')
                wake_t = wt_s
            try:
                async with self.sess.post(
                    QQ_API + '/api/music/quality',
                    json={'quality_level': q_lvl, 'max_cache_mb': q_mb},
                    timeout=aiohttp.ClientTimeout(total=4, sock_connect=2),
                ) as r:
                    if r.status != 200:
                        raise ApiError(f'QQ 音乐服务返回 HTTP {r.status}', 502)
                if r_mode:
                    async with self.sess.post(
                        QQ_API + '/api/music/radio/audio_mode',
                        json={'mode': r_mode},
                        timeout=aiohttp.ClientTimeout(total=4, sock_connect=2),
                    ) as rr:
                        if rr.status != 200:
                            raise ApiError('切换电台音效模式失败', 502)
                if p_mode:
                    async with self.sess.post(
                        QQ_API + '/api/music/control',
                        json={'action': p_mode, 'source': 'admin'},
                        timeout=aiohttp.ClientTimeout(total=4, sock_connect=2),
                    ) as rp:
                        if rp.status != 200:
                            raise ApiError('切换默认播放模式失败', 502)
                if wake_on is not None or wake_t is not None:
                    wb = {'action': 'wake'}
                    if wake_on is not None:
                        wb['on'] = wake_on
                    if wake_t is not None:
                        wb['time'] = wake_t
                    async with self.sess.post(
                        QQ_API + '/api/music/sleep_session',
                        json=wb,
                        timeout=aiohttp.ClientTimeout(total=4, sock_connect=2),
                    ) as rw:
                        if rw.status != 200:
                            rj_w = json.loads((await read_resp_bounded(rw, 16384)).decode('utf-8'))
                            raise ApiError(str(rj_w.get('error') or '保存晨间温柔叫醒设置失败'), 400)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大屏 QQ 音乐服务'), 502)
            log.info('settings/extra qq_quality: quality=%s cache_mb=%d radio=%s', q_lvl, q_mb, r_mode or '-')
            return jok(note='音乐音质、电台音效与晨间叫醒设置已保存生效', **(await self._full_settings()))

        raw_host = str(d.get('nezha_host') or '').strip().rstrip('/')
        if raw_host and not raw_host.startswith(('http://', 'https://')):
            raw_host = 'https://' + raw_host
        nz_host = check_safe_url(raw_host, '哪吒监控面板地址', allow_empty=True)
        nz_prx = str(d.get('nezha_proxy') or '').strip()
        if nz_prx.lower().startswith(('socks5://', 'socks5h://')):
            # thinksmart-monitor 使用原生 aiohttp（仅支持 HTTP 代理协议；Clash 7890 为 HTTP+SOCKS 混合端口）
            nz_prx = 'http://' + nz_prx.split('://', 1)[1]
        if nz_prx:
            check_safe_url(nz_prx, '哪吒 HTTP 代理地址', allow_empty=False, schemes=('http', 'https'))

        def _write():
            with self.cfg_lock:
                mcfg = read_json(MONITOR_STATE / 'config.json', {})
                if not isinstance(mcfg, dict):
                    mcfg = {}
                mcfg['nezha_host'] = nz_host or 'https://nezha.example.com'
                mcfg['nezha_proxy'] = nz_prx
                write_private_json(MONITOR_STATE / 'config.json', mcfg)

        await asyncio.get_running_loop().run_in_executor(None, _write)
        log.info('settings/extra updated: nezha_host=%s', _hostname(nz_host) if nz_host else 'default')
        return jok(note='探针设置已保存（2 秒内自动热生效）', **(await self._full_settings()))

    async def save_settings_device(self, request):
        d = await json_body(request, 16384)
        action = str(d.get('action') or '').strip()
        if action == 'clear_history':
            mem_only = bool(d.get('memory_only', False))
            cleared = False
            cli_err = None
            try:
                async with self.sess.post(
                    CLIENT_API + '/api/history/clear',
                    json={'memory_only': mem_only},
                    timeout=aiohttp.ClientTimeout(total=5, sock_connect=2),
                ) as r:
                    if r.status == 200:
                        cleared = True
                    else:
                        cli_err = ApiError(f'清除对话历史返回 HTTP {r.status}', 502)
            except Exception as e:
                cli_err = e
            if not cleared:
                ccfg_h = await asyncio.get_running_loop().run_in_executor(None, read_json, CLIENT_CFG, {})
                def_dev = str(d.get('device') or (ccfg_h.get('device_id') if isinstance(ccfg_h, dict) else '') or '').strip()[:64]
                if not def_dev:
                    mem_sq = await asyncio.get_running_loop().run_in_executor(None, _sqlite_read_memory, HUB_DIR / 'data' / 'chat.db', '')
                    def_dev = str(mem_sq.get('device') or 'default').strip()[:64] or 'default'
                merged_c, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
                clr_u = self._hub_reload_url(merged_c).replace('/kiosk/internal/reload', '/kiosk/chat/clear')
                with contextlib.suppress(Exception):
                    async with self.sess.post(
                        clr_u,
                        json={'device': def_dev, 'memory_only': mem_only},
                        timeout=aiohttp.ClientTimeout(total=4, sock_connect=1.5),
                    ) as rh:
                        rh_j = json.loads((await read_resp_bounded(rh, 32768)).decode('utf-8'))
                        if rh.status == 200 and isinstance(rh_j, dict) and rh_j.get('ok'):
                            cleared = True
                if not cleared:
                    if isinstance(cli_err, ApiError):
                        raise cli_err
                    raise ApiError(fmt_net_err(cli_err or RuntimeError('unreachable'), '机身语音客户端'), 502)
            msg = '已清除大屏长期记忆摘要（保留近期聊天记录）' if mem_only else '已清空大屏全部对话记录与长期记忆'
            log.info('settings/device clear_history: memory_only=%s', mem_only)
            return jok(note=msg, **(await self._full_settings()))

        if action == 'wake_screen':
            lvl = max(15, min(100, _int_or(d.get('level'), 60, 15, 100)))
            try:
                async with self.sess.post(
                    MONITOR_API + '/api/display/preview',
                    json={'level': lvl},
                    headers={'Origin': MONITOR_API},
                    timeout=aiohttp.ClientTimeout(total=3, sock_connect=1.5),
                ) as r:
                    if r.status != 200:
                        raise ApiError(f'点亮屏幕背光返回 HTTP {r.status}', 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大屏显示服务'), 502)
            await asyncio.sleep(0.1)
            return jok(note='已点亮家里大屏背光（不改变自动感光设置）', **(await self._full_settings()))

        if action == 'reset_device_llm':
            def _clr_ag():
                with self.cfg_lock:
                    ag_p = HUB_DIR / 'data' / 'agent_sessions.json'
                    if ag_p.is_file():
                        ag_d = read_json(ag_p, {})
                        if isinstance(ag_d, dict) and ag_d.get('last_model'):
                            ag_d['last_model'] = {}
                            write_private_json(ag_p, ag_d)

            await asyncio.get_running_loop().run_in_executor(None, _clr_ag)
            await self._notify_hub_reload([], reset_device_llm=True)
            with contextlib.suppress(Exception):
                async with self.sess.post(CLIENT_API + '/api/model', json={'kind': 'default'}, timeout=aiohttp.ClientTimeout(total=2.5)) as r:
                    await r.read()
            log.info('settings/device reset_device_llm')
            return jok(note='家里大屏已恢复跟随后台默认模型', **(await self._full_settings()))

        if action == 'bt_ctl':
            bt_body = {}
            if 'window' in d:
                bt_body['window'] = bool(d['window'])
            for k in ('disconnect', 'remove'):
                if d.get(k):
                    addr = str(d[k]).strip()[:32]
                    if not re.fullmatch(r'[0-9A-Fa-f:]{11,24}', addr):
                        raise ApiError('蓝牙设备地址格式不对')
                    bt_body[k] = addr
            if not bt_body:
                raise ApiError('缺少蓝牙操作参数')
            try:
                async with self.sess.post(CLIENT_API + '/api/bluetooth', json=bt_body, timeout=aiohttp.ClientTimeout(total=5, sock_connect=2)) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or f'蓝牙控制返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '机身蓝牙服务'), 502)
            await asyncio.sleep(0.25)
            bt_msg = '已开放蓝牙搜索配对（手机蓝牙里找 ThinkSmart 连接）' if bt_body.get('window') else ('已移除该蓝牙设备' if 'remove' in bt_body else '已断开该蓝牙设备')
            return jok(note=bt_msg, **(await self._full_settings()))

        if action == 'alarm_ctl':
            if d.get('stop_ringing'):
                ep_a, body_a, note_a = '/api/alarms/stop', {}, '已停止大屏响铃'
            elif d.get('snooze'):
                ep_a, body_a, note_a = '/api/alarms/snooze', {}, '已设置贪睡，10 分钟后再响'
            elif d.get('clear_missed'):
                ep_a, body_a, note_a = '/api/alarms/missed/clear', {}, '已清除漏响闹钟提示'
            elif 'delete_id' in d:
                raw_aid = d.get('delete_id')
                if isinstance(raw_aid, bool):
                    raise ApiError('闹钟编号格式不对')
                try:
                    aid = int(raw_aid)
                except (TypeError, ValueError, OverflowError):
                    raise ApiError('闹钟编号格式不对')
                ep_a, body_a, note_a = '/api/alarms/delete', {'id': aid}, '已删除该闹钟 / 提醒'
            else:
                raise ApiError('缺少闹钟操作参数')
            try:
                async with self.sess.post(CLIENT_API + ep_a, json=body_a, timeout=aiohttp.ClientTimeout(total=4, sock_connect=2)) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or f'闹钟控制返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '机身闹钟服务'), 502)
            await asyncio.sleep(0.15)
            return jok(note=note_a, **(await self._full_settings()))

        if action == 'abort_voice':
            try:
                async with self.sess.post(CLIENT_API + '/api/abort', json={}, timeout=aiohttp.ClientTimeout(total=4, sock_connect=2)) as r:
                    if r.status != 200:
                        raise ApiError(f'打断语音播报返回 HTTP {r.status}', 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '机身语音客户端'), 502)
            await asyncio.sleep(0.15)
            return jok(note='已打断大屏当前语音播报与对话', **(await self._full_settings()))

        if action == 'stop_desk_timer':
            try:
                async with self.sess.post(
                    HOME_API + '/api/home/timer',
                    json={'action': 'stop'},
                    headers={'Host': '127.0.0.1:8095'},
                    timeout=aiohttp.ClientTimeout(total=4, sock_connect=2),
                ) as r:
                    if r.status != 200:
                        raise ApiError(f'关闭桌面计时器返回 HTTP {r.status}', 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大屏首页服务'), 502)
            await asyncio.sleep(0.1)
            return jok(note='已关闭大屏桌面计时器', **(await self._full_settings()))

        if action == 'play_reports':
            try:
                async with self.sess.post(CLIENT_API + '/api/reports/play', json={}, timeout=aiohttp.ClientTimeout(total=4, sock_connect=2)) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or f'播报待汇报任务返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '机身播报服务'), 502)
            await asyncio.sleep(0.2)
            return jok(note='已通知大屏播报后台待汇报任务', **(await self._full_settings()))

        if action in ('update_check', 'update_install'):
            ep = '/api/update/check' if action == 'update_check' else '/api/update/install'
            ub = {} if action == 'update_check' else {
                'version': str(d.get('version') or '')[:32],
                'system': str(d.get('system') or '')[:32],
            }
            try:
                async with self.sess.post(CLIENT_API + ep, json=ub, timeout=aiohttp.ClientTimeout(total=6, sock_connect=2)) as r:
                    rj = json.loads((await read_resp_bounded(r, 16384)).decode('utf-8'))
                    if r.status == 409:
                        raise ApiError(str(rj.get('error') or '大屏正在对话中，请等对话结束后再安装更新'), 409)
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(str(rj.get('error') or '当前设备未启用在线更新服务'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '机身更新服务'), 502)
            await asyncio.sleep(1.2 if action == 'update_check' else 0.4)
            u_note = '已触发检查更新（几秒后点刷新即可看到最新结果）' if action == 'update_check' else '已提交系统升级任务，设备将在校验通过后自动更新'
            return jok(note=u_note, **(await self._full_settings()))

        if action == 'hub_update_check':
            merged_h, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
            try:
                async with self.sess.post(
                    self._hub_diag_url(merged_h),
                    json={'action': 'hub_update_check'},
                    headers={'X-TS-Internal-Reload': '1'},
                    timeout=aiohttp.ClientTimeout(total=20, sock_connect=2),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 65536)).decode('utf-8'))
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(_redact_err(rj.get('error') or f'检查小智中枢更新返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '小智语音网关'), 502)
            hc = rj.get('hub_check') if isinstance(rj.get('hub_check'), dict) else {}
            st = str(hc.get('state') or '').strip()
            if hc.get('error') or st in ('error', 'unsupported'):
                err_m = _redact_err(hc.get('error') or hc.get('message') or '无法连接远端仓库')[:140]
                raise ApiError(f'检查小智中枢更新失败：{err_m}', 502)
            cur_short = str(((hc.get('current') or {}).get('short')) or '')[:16]
            lat_short = str(((hc.get('latest') or {}).get('short')) or '')[:16]
            lat_subj = str(((hc.get('latest') or {}).get('subject')) or '')[:48]
            if st == 'latest' or hc.get('up_to_date'):
                h_note = f'小智中枢当前已是最新提交（{cur_short}）' if cur_short else '小智中枢当前已是最新版本'
            elif st in ('dirty', 'diverged'):
                h_note = f"小智中枢暂无法自动快进更新：{str(hc.get('message') or st)[:90]}"
            elif st == 'available' or (isinstance(hc.get('behind'), int) and hc.get('behind') > 0):
                bh = hc.get('behind')
                lat_tail = f'（最新：{lat_short} {lat_subj}）'.rstrip(' ）') + '）' if lat_short else ''
                if isinstance(bh, int) and bh > 0:
                    h_note = f'发现小智中枢有 {bh} 个新提交可更新{lat_tail}'
                else:
                    h_note = f'发现小智中枢有新版本可更新{lat_tail}'
            else:
                h_note = str(hc.get('message') or '已完成小智中枢版本检查')[:100]
            return jok(note=h_note, **(await self._full_settings()))

        if action == 'hub_update_start':
            merged_h, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
            try:
                async with self.sess.post(
                    self._hub_diag_url(merged_h),
                    json={'action': 'hub_update_start'},
                    headers={'X-TS-Internal-Reload': '1'},
                    timeout=aiohttp.ClientTimeout(total=12, sock_connect=2),
                ) as r:
                    rj = json.loads((await read_resp_bounded(r, 65536)).decode('utf-8'))
                    if r.status == 409:
                        raise ApiError(_redact_err(rj.get('error') or '当前暂不满足自动升级条件，请先点检查小智中枢更新或等语音对话结束'), 409)
                    if r.status != 200 or not rj.get('ok'):
                        raise ApiError(_redact_err(rj.get('error') or f'启动小智中枢更新返回 HTTP {r.status}'), 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '小智语音网关'), 502)
            await asyncio.sleep(0.3)
            return jok(note='已启动小智中枢在线升级，拉取完成并自检通过后将自动平滑重启网关', **(await self._full_settings()))

        auto_b = bool(d.get('auto_brightness', True))
        try:
            b_lvl = max(0, min(100, int(d.get('brightness', 100))))
            vol = max(0, min(100, int(d.get('volume', 60))))
            avol = max(0, min(100, int(d.get('alarm_volume', 70))))
        except (TypeError, ValueError):
            raise ApiError('亮度与音量必须是 0~100 的整数')
        atone = str(d.get('alarm_tone') or 'coffee').strip()
        if atone not in ALARM_TONE_IDS:
            raise ApiError('不支持这个闹钟铃声')
        so_in = d.get('screen_off') if isinstance(d.get('screen_off'), dict) else {}
        try:
            so_min = int(so_in.get('minutes') or 5)
        except (TypeError, ValueError):
            so_min = 5
        if so_min not in SCREEN_OFF_MINUTES_SET:
            raise ApiError('自动息屏时长请选择 1 / 3 / 5 / 10 / 30 分钟')
        screen_off_val = {'enabled': bool(so_in.get('enabled', True)), 'minutes': so_min}

        sl_in = d.get('sleep') if isinstance(d.get('sleep'), dict) else {}
        sl_start = str(sl_in.get('start') or '22:00').strip()
        sl_end = str(sl_in.get('end') or '07:00').strip()
        if re.match(r'^\d:\d\d$', sl_start):
            sl_start = '0' + sl_start
        if re.match(r'^\d:\d\d$', sl_end):
            sl_end = '0' + sl_end
        if not (re.match(r'^([01]\d|2[0-3]):[0-5]\d$', sl_start) and re.match(r'^([01]\d|2[0-3]):[0-5]\d$', sl_end)):
            raise ApiError('夜间免打扰时间请填写 HH:MM 格式（如 22:00）')
        sleep_val = {'enabled': bool(sl_in.get('enabled', False)), 'start': sl_start, 'end': sl_end}
        bt_en = bool(d.get('bluetooth', True))

        prev_cc = await asyncio.get_running_loop().run_in_executor(None, read_json, CLIENT_CFG, {})
        prev_bt = bool(prev_cc.get('bluetooth', True)) if isinstance(prev_cc, dict) else True

        # 1) Push display first (:8092 requires single-key body + Origin; manual level internally syncs :8090/api/brightness)
        disp_body = {'auto': True} if auto_b else {'level': b_lvl}
        disp_ok = False
        ack_disp = None
        disp_warn = ''
        with contextlib.suppress(Exception):
            async with self.sess.post(
                MONITOR_API + '/api/display',
                json=disp_body,
                headers={'Origin': MONITOR_API},
                timeout=aiohttp.ClientTimeout(total=2.5),
            ) as r:
                raw_d = await read_resp_bounded(r, 16384)
                dj = {}
                with contextlib.suppress(Exception):
                    dj = json.loads(raw_d.decode('utf-8'))
                if r.status == 200 and isinstance(dj, dict):
                    disp_ok = True
                    ack_disp = dj
                elif r.status == 400 and isinstance(dj, dict):
                    disp_ok = True
                    disp_warn = str(dj.get('error') or '当前暂无环境光读数，请先手动调节亮度')[:80]

        # 2) Push client endpoints sequentially on loopback (<10ms total, avoiding concurrent config.json writes)
        cli_steps = [
            ('/api/volume', {'volume': vol}, 2.5),
            ('/api/alarm-volume', {'volume': avol}, 2.5),
            ('/api/alarm-tone', {'tone': atone}, 2.5),
            ('/api/screen-off', screen_off_val, 2.5),
            ('/api/sleep', sleep_val, 2.5),
        ]
        if not disp_ok and not auto_b:
            cli_steps.append(('/api/brightness', {'level': b_lvl}, 2.5))
        if bt_en != prev_bt or d.get('force_bluetooth'):
            cli_steps.append(('/api/bluetooth', {'enabled': bt_en}, 5.0))

        for path_s, body_s, t_sec in cli_steps:
            with contextlib.suppress(Exception):
                async with self.sess.post(
                    CLIENT_API + path_s,
                    json=body_s,
                    timeout=aiohttp.ClientTimeout(total=t_sec),
                ) as resp:
                    await resp.read()

        # 3) Persist after HTTP handlers finish so offline mode & mock tests stay consistent without file races
        def _write():
            with self.cfg_lock:
                patch = {
                    'alarm_volume': avol,
                    'alarm_tone': atone,
                    'screen_off': screen_off_val,
                    'sleep': sleep_val,
                    'bluetooth': bt_en,
                }
                if not auto_b:
                    patch['brightness'] = b_lvl
                write_client_cfg_patch(patch)

        await asyncio.get_running_loop().run_in_executor(None, _write)
        snap = await self._full_settings()
        if isinstance(ack_disp, dict):
            snap['device']['auto_brightness'] = bool(ack_disp.get('auto', auto_b))
            if not snap['device']['auto_brightness'] and isinstance(ack_disp.get('level'), (int, float)):
                snap['device']['brightness'] = max(0, min(100, int(ack_disp['level'])))
        note_s = f'音量与免打扰已生效（自动亮度：{disp_warn}）' if disp_warn else '大屏显示、音量与免打扰设置已实时生效'
        log.info('settings/device updated: auto=%s level=%s vol=%s bt=%s', auto_b, b_lvl, vol, bt_en)
        return jok(note=note_s, **snap)

    async def test_settings(self, request):
        d = await json_body(request, 32768)
        kind = str(d.get('kind') or '').strip()
        merged, _ = await asyncio.get_running_loop().run_in_executor(None, load_hub_configs)
        t0 = time.monotonic()

        if kind == 'claude_cli':
            cur_ccli = ((merged.get('LLM') or {}).get('ClaudeCLI')) or {}
            req_cp = str(d.get('cli_path') or '').strip()[:200]
            if any(ord(ch) < 32 for ch in req_cp):
                raise ApiError('claude 可执行路径格式不对')
            bin_p = _find_claude_cli(req_cp or clean_text_val(cur_ccli.get('cli_path')))
            if not bin_p:
                raise ApiError('这台设备上尚未安装 Claude Code 命令行（claude），请选用云端或自建网关厂商', 404)
            try:
                proc = await asyncio.create_subprocess_exec(
                    bin_p, '--version',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out_b, err_b = await _communicate_bounded(proc, None, 5.0)
                ms = round((time.monotonic() - t0) * 1000)
                ver_s = (out_b or err_b or b'').decode('utf-8', errors='replace').strip().splitlines()
                ver_label = (ver_s[0][:60] if ver_s else Path(bin_p).name)
                if proc.returncode != 0:
                    raise ApiError(f'Claude CLI 返回异常（code={proc.returncode}）：{ver_label}', 502)
                return jok(ms=ms, installed=True, message=f'本机 Claude CLI 可用（{ver_label} · {ms} ms）')
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(f'调用本机 Claude CLI 失败：{type(e).__name__}', 502)

        if kind == 'llm_models':
            vid = str(d.get('vendor') or 'deepseek').strip()
            v = VENDOR_BY_ID.get(vid)
            ent = ((merged.get('LLM') or {}).get(v['entry']) if v else None) or {}
            uf = 'url' if ('url' in ent and 'base_url' not in ent) else 'base_url'
            raw_b = strip_chat_completions_suffix(d.get('base_url') or clean_text_val(ent.get(uf)) or (v['base_url'] if v else ''))
            b_url = check_safe_url(raw_b, '接口地址')
            ak = str(d.get('api_key') or '').strip()
            if not ak and not d.get('clear_api_key') and not is_placeholder_secret(ent.get('api_key')):
                ak = str(ent.get('api_key')).strip()
            b_clean = b_url.rstrip('/')
            if not b_clean.endswith(('/v1', '/v3', '/v4', '/openai')):
                b_clean += '/v1'
            models_url = b_clean + '/models'
            hdrs = {'Accept': 'application/json'}
            if ak:
                hdrs['Authorization'] = f'Bearer {ak}'
                if 'anthropic' in b_clean.lower():
                    hdrs['x-api-key'] = ak
                    hdrs['anthropic-version'] = '2023-06-01'
            await _verify_outbound_url(models_url, '接口地址')
            try:
                async with self.sess.get(
                    models_url,
                    headers=hdrs,
                    proxy=_clash_proxy_for_url(models_url),
                    timeout=aiohttp.ClientTimeout(total=12, sock_connect=5),
                    allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 524288)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status != 200:
                        msg_s = _redact_err(raw.decode('utf-8', errors='replace'), ak)
                        raise ApiError(f'拉取模型列表返回 HTTP {r.status}：{msg_s[:120]}', 502)
                    j = json.loads(raw.decode('utf-8'))
                    items = j.get('data') if isinstance(j.get('data'), list) else (j.get('models') if isinstance(j.get('models'), list) else [])
                    out_models = []
                    for it in items:
                        mid = str(it.get('id') or it.get('name') or '').strip() if isinstance(it, dict) else str(it or '').strip()
                        if mid.startswith('models/'):
                            mid = mid[7:]
                        if mid and len(mid) <= 120 and not any(ch.isspace() for ch in mid) and mid not in out_models:
                            out_models.append(mid)
                            if len(out_models) >= 120:
                                break
                    if not out_models:
                        raise ApiError('该接口未返回可用模型列表（可直接在上方手动输入模型名称）', 502)
                    return jok(ms=ms, models=out_models, message=f'已从接口拉取 {len(out_models)} 个可用模型（{ms} ms），点击胶囊即可选用')
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大模型列表接口'), 502)

        if kind == 'llm':
            vid = str(d.get('vendor') or 'deepseek').strip()
            v = VENDOR_BY_ID.get(vid)
            ent = ((merged.get('LLM') or {}).get(v['entry']) if v else None) or {}
            uf = 'url' if ('url' in ent and 'base_url' not in ent) else 'base_url'
            raw_b = strip_chat_completions_suffix(d.get('base_url') or clean_text_val(ent.get(uf)) or (v['base_url'] if v else ''))
            b_url = check_safe_url(raw_b, '接口地址')
            model = str(d.get('model') or clean_text_val(ent.get('model_name')) or '').strip()
            if not model:
                raise ApiError('请先填写模型名称')
            ak = str(d.get('api_key') or '').strip()
            if not ak and not d.get('clear_api_key') and not is_placeholder_secret(ent.get('api_key')):
                ak = str(ent.get('api_key')).strip()
            if not ak and vid != 'custom':
                raise ApiError('请先填写 API Key')
            b_clean = b_url.rstrip('/')
            if not b_clean.endswith(('/v1', '/v3', '/v4', '/openai')):
                b_clean += '/v1'
            chat_url = b_clean + '/chat/completions'
            hdrs = {'Content-Type': 'application/json'}
            b_low = b_clean.lower()
            m_low = model.lower()
            if ak:
                hdrs['Authorization'] = f'Bearer {ak}'
                if 'anthropic' in b_low:
                    hdrs['x-api-key'] = ak
                    hdrs['anthropic-version'] = '2023-06-01'
            req_body = {
                'model': model,
                'messages': [{'role': 'user', 'content': '请只回复OK两个字母'}],
                'max_tokens': 128,
                'stream': False,
            }
            if 'reasoner' not in m_low:
                if any(dom in b_low for dom in ('anthropic.com', 'deepseek.com', 'bigmodel.cn', 'moonshot.cn', 'volces.com')):
                    req_body['thinking'] = {'type': 'disabled'}
                elif 'aliyuncs.com' in b_low:
                    req_body['enable_thinking'] = False
            await _verify_outbound_url(chat_url, '接口地址')
            try:
                async with self.sess.post(
                    chat_url,
                    json=req_body,
                    headers=hdrs,
                    proxy=_clash_proxy_for_url(chat_url),
                    timeout=aiohttp.ClientTimeout(total=22, sock_connect=6),
                    allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 131072)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status != 200:
                        msg_s = _redact_err(raw.decode('utf-8', errors='replace'), ak)
                        raise ApiError(f'连接返回 HTTP {r.status}：{msg_s[:120]}', 502)
                    j = json.loads(raw.decode('utf-8'))
                    msg_obj = ((j.get('choices') or [{}])[0].get('message') or {})
                    reply = str(msg_obj.get('content') or msg_obj.get('reasoning_content') or 'OK').strip()[:40]
                    return jok(ms=ms, message=f'连通正常（{ms} ms）· 回复：{reply}')
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '大模型接口'), 502)

        if kind == 'tutor_llm':
            llms_t = merged.get('LLM') if isinstance(merged.get('LLM'), dict) else {}
            oa_e = dict(llms_t['OpenAILLM']) if isinstance(llms_t.get('OpenAILLM'), dict) else {}
            ds_e = dict(llms_t['DeepSeekLLM']) if isinstance(llms_t.get('DeepSeekLLM'), dict) else {}
            vd_in = d.get('vendor_drafts') if isinstance(d.get('vendor_drafts'), dict) else {}
            for vid_k, tgt_e in (('openai', oa_e), ('deepseek', ds_e)):
                dr = vd_in.get(vid_k)
                if isinstance(dr, dict):
                    if dr.get('base_url'):
                        tgt_e['base_url'] = str(dr['base_url']).strip()
                        tgt_e.pop('url', None)
                    if dr.get('model'):
                        tgt_e['model_name'] = str(dr['model']).strip()
                    if dr.get('clear_api_key'):
                        tgt_e['api_key'] = ''
                    elif dr.get('api_key'):
                        tgt_e['api_key'] = str(dr['api_key']).strip()

            def _ep_of(ent_d):
                raw_u = strip_chat_completions_suffix(clean_text_val(ent_d.get('url') or ent_d.get('base_url')))
                if not raw_u:
                    return '', ''
                u_ok = check_safe_url(raw_u, '讲题网关地址').rstrip('/')
                if not u_ok.endswith(('/v1', '/v3', '/v4', '/openai')):
                    u_ok += '/v1'
                k_ok = '' if is_placeholder_secret(ent_d.get('api_key')) else str(ent_d.get('api_key')).strip()
                return u_ok, k_ok

            sel_id = str(((merged.get('selected_module') or {}).get('LLM')) or '').strip()
            sel_e = dict(llms_t[sel_id]) if (sel_id and sel_id != 'ClaudeCLI' and isinstance(llms_t.get(sel_id), dict)) else {}
            sel_v_obj = VENDOR_BY_ENTRY.get(sel_id)
            if sel_v_obj and isinstance(vd_in.get(sel_v_obj['id']), dict):
                dr_s = vd_in[sel_v_obj['id']]
                if dr_s.get('base_url'):
                    sel_e['base_url'] = str(dr_s['base_url']).strip()
                    sel_e.pop('url', None)
                if dr_s.get('model'):
                    sel_e['model_name'] = str(dr_s['model']).strip()
                if dr_s.get('clear_api_key'):
                    sel_e['api_key'] = ''
                elif dr_s.get('api_key'):
                    sel_e['api_key'] = str(dr_s['api_key']).strip()
            oa_v1, oa_k = _ep_of(oa_e)
            ds_v1, ds_k = _ep_of(ds_e)
            sel_v1, sel_k = _ep_of(sel_e)
            pri_v1, pri_k = (oa_v1, oa_k) if (oa_v1 and oa_k) else ((sel_v1, sel_k) if (sel_v1 and sel_k) else (ds_v1, ds_k))
            if not pri_v1 or not pri_k:
                raise ApiError('请先在上方配置大模型的接口地址与 API Key（默认复用小智大模型）')

            t_prefs = read_json(TUTOR_STATE / 'prefs.json', {})
            tm_p = (t_prefs.get('tutor_models') or {}) if isinstance(t_prefs, dict) else {}
            xz_m = clean_text_val(sel_e.get('model_name'))
            ds_def = clean_text_val(ds_e.get('model_name')) or xz_m or 'deepseek-v4.1-flash'
            m_fast = str(d.get('fast_model') or tm_p.get('fast') or xz_m or 'gpt-6.1-terra').strip()[:120]
            m_deep = str(d.get('deep_model') or tm_p.get('deep') or xz_m or 'gpt-6.1-sol').strip()[:120]
            m_bak = str(d.get('backup_model') or tm_p.get('backup') or ds_def).strip()[:120]

            async def _probe_tier(lbl_t: str, mod_t: str):
                t_st = time.monotonic()
                is_ds = bool(mod_t == ds_def or mod_t.lower().startswith('deepseek'))
                u_t, k_t = (ds_v1, ds_k) if (is_ds and ds_v1 and ds_k) else (pri_v1, pri_k)
                cu = u_t + '/chat/completions'
                rb = {'model': mod_t, 'messages': [{'role': 'user', 'content': '只回复OK'}], 'max_tokens': 64, 'stream': False}
                if 'deepseek.com' in cu.lower() and 'reasoner' not in mod_t.lower():
                    rb['thinking'] = {'type': 'disabled'}
                await _verify_outbound_url(cu, '讲题网关地址')
                try:
                    async with self.sess.post(
                        cu, json=rb, headers={'Authorization': f'Bearer {k_t}', 'Content-Type': 'application/json'},
                        proxy=_clash_proxy_for_url(cu), timeout=aiohttp.ClientTimeout(total=20, sock_connect=5), allow_redirects=False,
                    ) as rr:
                        raw_t = await read_resp_bounded(rr, 65536)
                        dt_ms = round((time.monotonic() - t_st) * 1000)
                        if rr.status != 200:
                            err_s = _redact_err(raw_t.decode('utf-8', errors='replace'), k_t)[:90]
                            return False, f'{lbl_t} {mod_t} 失败（HTTP {rr.status}：{err_s}）'
                        return True, f'{lbl_t} {mod_t}（{dt_ms}ms）'
                except Exception as ex_t:
                    return False, f'{lbl_t} {mod_t} 异常（{fmt_net_err(ex_t, "讲题网关")}）'

            tier_res = await asyncio.gather(
                _probe_tier('快讲', m_fast),
                _probe_tier('深推', m_deep),
                _probe_tier('备份', m_bak),
            )
            ms_all = round((time.monotonic() - t0) * 1000)
            bad_tiers = [msg for ok_t, msg in tier_res if not ok_t]
            if bad_tiers:
                raise ApiError('；'.join(bad_tiers), 502)
            return jok(ms=ms_all, message='三档讲题模型全部就绪 · ' + ' / '.join(msg for _, msg in tier_res))

        if kind == 'qweather':
            gw = (merged.get('plugins') or {}).get('get_weather') or {}
            host = normalize_qweather_host(d.get('api_host') or clean_text_val(gw.get('api_host')))
            if not host:
                raise ApiError('请填写和风天气 API Host')
            key = str(d.get('api_key') or '').strip()
            if not key and not d.get('clear_api_key') and not is_placeholder_secret(gw.get('api_key')):
                key = str(gw.get('api_key')).strip()
            if not key:
                raise ApiError('请填写和风天气 API Key')
            city = str(d.get('city') or clean_text_val(gw.get('default_location')) or '北京').strip()[:20]
            base_u = f'{QWEATHER_PROTO}://{host}'
            await _verify_outbound_url(base_u, '和风天气 API Host')
            try:
                async with self.sess.get(
                    f'{base_u}/geo/v2/city/lookup',
                    params={'location': city, 'number': 1, 'lang': 'zh'},
                    headers={'X-QW-Api-Key': key},
                    timeout=aiohttp.ClientTimeout(total=12, sock_connect=5),
                    allow_redirects=False,
                ) as r1:
                    g_raw = json.loads((await read_resp_bounded(r1, 131072)).decode('utf-8'))
                    if r1.status != 200 or str(g_raw.get('code')) != '200':
                        raise ApiError(f"城市查询失败（HTTP {r1.status} / code={g_raw.get('code')}），请检查 API Host 与 Key", 502)
                    loc = (g_raw.get('location') or [{}])[0]
                    loc_id = str(loc.get('id') or '')
                    loc_nm = str(loc.get('name') or city)
                async with self.sess.get(
                    f'{base_u}/v7/weather/now',
                    params={'location': loc_id},
                    headers={'X-QW-Api-Key': key},
                    timeout=aiohttp.ClientTimeout(total=12, sock_connect=5),
                    allow_redirects=False,
                ) as r2:
                    w_raw = json.loads((await read_resp_bounded(r2, 131072)).decode('utf-8'))
                    ms = round((time.monotonic() - t0) * 1000)
                    if r2.status != 200 or str(w_raw.get('code')) != '200':
                        raise ApiError(f"实况拉取失败（code={w_raw.get('code')}）", 502)
                    nw = w_raw.get('now') or {}
                    return jok(ms=ms, message=f"{loc_nm}：{nw.get('text', '')} {nw.get('temp', '')}°C · 湿度 {nw.get('humidity', '')}%（{ms} ms）")
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '和风天气接口'), 502)

        if kind == 'voice_weather':
            gw = (merged.get('plugins') or {}).get('get_weather') or {}
            city = str(d.get('city') or clean_text_val(gw.get('location') or gw.get('default_location')) or '北京').strip()[:20]
            if not city:
                city = '北京'
            q_cands = [city]
            if len(city) > 2 and city.endswith(('市', '区', '县', '盟', '旗')):
                q_cands.append(city[:-1])
            prx_om = _clash_local_proxy(OPEN_METEO_GEO_URL)
            prx_list = (None, prx_om) if prx_om else (None,)
            loc_hit = None
            last_om_err = None
            for cand_p in prx_list:
                for qc in q_cands:
                    try:
                        async with self.sess.get(
                            OPEN_METEO_GEO_URL,
                            params={'name': qc, 'count': 3, 'language': 'zh', 'format': 'json'},
                            proxy=cand_p,
                            timeout=aiohttp.ClientTimeout(total=8, sock_connect=4),
                            allow_redirects=False,
                        ) as rg:
                            if rg.status != 200:
                                last_om_err = ApiError(f'地理编码返回 HTTP {rg.status}', 502)
                                continue
                            gj = json.loads((await read_resp_bounded(rg, 131072)).decode('utf-8', errors='replace'))
                            res_l = gj.get('results') if isinstance(gj, dict) and isinstance(gj.get('results'), list) else []
                            if res_l and isinstance(res_l[0], dict):
                                loc_hit = (res_l[0], cand_p)
                                break
                    except Exception as ex_g:
                        last_om_err = ex_g
                if loc_hit:
                    break
            if not loc_hit:
                if isinstance(last_om_err, ApiError):
                    raise last_om_err
                if last_om_err:
                    raise ApiError(fmt_net_err(last_om_err, 'Open-Meteo 语音天气'), 502)
                raise ApiError(f'未识别到城市「{city}」，请换个常见城市或区县名称', 404)
            loc_obj, used_prx = loc_hit
            lat, lon = loc_obj.get('latitude'), loc_obj.get('longitude')
            loc_nm = str(loc_obj.get('name') or city)
            adm1 = str(loc_obj.get('admin1') or '')
            if adm1 and adm1 != loc_nm:
                loc_nm = f'{loc_nm}（{adm1}）'
            try:
                async with self.sess.get(
                    OPEN_METEO_FORECAST_URL,
                    params={
                        'latitude': lat, 'longitude': lon,
                        'current': 'temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m',
                        'timezone': 'auto',
                    },
                    proxy=used_prx,
                    timeout=aiohttp.ClientTimeout(total=8, sock_connect=4),
                    allow_redirects=False,
                ) as rf:
                    if rf.status != 200:
                        raise ApiError(f'Open-Meteo 预报返回 HTTP {rf.status}', 502)
                    fj = json.loads((await read_resp_bounded(rf, 131072)).decode('utf-8', errors='replace'))
                    ms = round((time.monotonic() - t0) * 1000)
                    cur_w = (fj.get('current') or {}) if isinstance(fj, dict) else {}
                    w_desc = _WMO_WEATHER_ZH.get(_int_or(cur_w.get('weather_code'), -1, -1, 99), '实时天气')
                    temp_v = cur_w.get('temperature_2m', '')
                    hum_v = cur_w.get('relative_humidity_2m', '')
                    return jok(ms=ms, message=f"语音免 Key 天气可用 · {loc_nm}：{w_desc} {temp_v}°C · 湿度 {hum_v}%（{ms} ms）")
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, 'Open-Meteo 语音天气'), 502)

        if kind == 'web_search':
            ws = (merged.get('plugins') or {}).get('web_search') or {}
            b_url = check_safe_url(d.get('base_url') or clean_text_val(ws.get('base_url')) or 'https://api.deepseek.com/anthropic/v1', '联网搜索地址')
            mod = str(d.get('model') or clean_text_val(ws.get('model')) or 'deepseek-v4-flash').strip()[:80]
            wk = str(d.get('api_key') or '').strip()
            if not wk and not d.get('clear_api_key') and not is_placeholder_secret(ws.get('api_key')) and not ws.get('provider'):
                wk = str(ws.get('api_key')).strip()
            if not wk:
                raise ApiError('请先填写原生联网搜索 API Key')
            msg_url = anthropic_messages_url(b_url)
            await _verify_outbound_url(msg_url, '联网搜索地址')
            try:
                async with self.sess.post(
                    msg_url,
                    json={
                        'model': mod,
                        'max_tokens': 256,
                        'thinking': {'type': 'disabled'},
                        'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 1}],
                        'messages': [{'role': 'user', 'content': '用一句话告诉我今天中国北京天气或新闻简讯'}],
                    },
                    headers={'x-api-key': wk, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                    proxy=_clash_proxy_for_url(msg_url),
                    timeout=aiohttp.ClientTimeout(total=20, sock_connect=5),
                    allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 262144)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status != 200:
                        txt_e = _redact_err(raw.decode('utf-8', errors='replace'), wk)[:120]
                        if r.status in (401, 403):
                            raise ApiError(f'联网搜索 API Key 无效或无权限（HTTP {r.status}）', 502)
                        if r.status == 402:
                            raise ApiError('联网搜索账户余额不足（HTTP 402）', 502)
                        if r.status == 404:
                            raise ApiError('接口地址不是 Anthropic 协议端点（HTTP 404）', 502)
                        raise ApiError(f'原生搜索接口返回 HTTP {r.status}：{txt_e}', 502)
                    j = json.loads(raw.decode('utf-8'))
                    blocks = j.get('content') if isinstance(j.get('content'), list) else []
                    searched = any(isinstance(b, dict) and b.get('type') in ('server_tool_use', 'web_search_tool_result') for b in blocks)
                    if not searched:
                        raise ApiError('接口未触发 web_search 工具，请确认该地址与模型支持原生联网搜索', 502)
                    return jok(ms=ms, message=f'原生联网搜索可用（{ms} ms · {mod}）')
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '原生联网搜索接口'), 502)

        if kind == 'metaso':
            ws = (merged.get('plugins') or {}).get('web_search') or {}
            mk = str(d.get('metaso_key') or '').strip()
            if not mk and not d.get('clear_metaso_key') and not is_placeholder_secret(ws.get('metaso_key')):
                mk = str(ws.get('metaso_key')).strip()
            if not mk:
                raise ApiError('请先填写秘塔 Metaso Key')
            try:
                async with self.sess.post(
                    METASO_TEST_URL,
                    json={'q': '今日要闻', 'scope': 'webpage', 'size': 1},
                    headers={'Authorization': f'Bearer {mk}', 'Accept': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=15, sock_connect=5),
                    allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 131072)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status != 200:
                        raise ApiError(f'秘塔接口返回 HTTP {r.status}，请检查 Key 或额度', 502)
                    j = json.loads(raw.decode('utf-8'))
                    cnt = len(j.get('webpages') or j.get('results') or [])
                    return jok(ms=ms, message=f'秘塔搜索可用（{ms} ms，返回 {cnt} 条结果）')
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '秘塔接口'), 502)

        if kind == 'amap':
            ws = (merged.get('plugins') or {}).get('web_search') or {}
            ak = str(d.get('amap_key') or '').strip()
            if not ak and not d.get('clear_amap_key') and not is_placeholder_secret(ws.get('amap_key')):
                ak = str(ws.get('amap_key')).strip()
            if not ak:
                raise ApiError('请先填写高德地图 Web 服务 Key')
            city = str(d.get('city') or '北京').strip()[:20]
            try:
                async with self.sess.get(
                    AMAP_TEST_URL,
                    params={'address': city, 'key': ak},
                    timeout=aiohttp.ClientTimeout(total=12, sock_connect=5),
                    allow_redirects=False,
                ) as r:
                    j = json.loads((await read_resp_bounded(r, 131072)).decode('utf-8'))
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status != 200 or str(j.get('status')) != '1':
                        raise ApiError(f"高德返回：{j.get('info') or r.status}（请确认为 Web 服务类型 Key）", 502)
                    geos = j.get('geocodes') or [{}]
                    loc_s = str(geos[0].get('formatted_address') or city)
                    return jok(ms=ms, message=f'高德地图可用（{ms} ms · {loc_s}）')
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '高德地图接口'), 502)

        if kind == 'news':
            nn = ((merged.get('plugins') or {}).get('get_news_from_newsnow')) or {}
            src_str = _parse_news_sources(d.get('news_sources') or nn.get('news_sources') or '财联社')
            first_ch = src_str.split(';')[0]
            cid = NEWSNOW_CHANNEL_MAP.get(first_ch, 'cls-telegraph')
            custom_u = str(d['url'] if 'url' in d else (nn.get('url') or '')).strip()
            if custom_u:
                api_base = check_safe_url(custom_u, '自建热榜镜像地址')
                await _verify_outbound_url(api_base, '自建热榜镜像地址')
            else:
                api_base = NEWSNOW_API_URL
            agg_err = None
            try:
                async with self.sess.get(
                    api_base + urllib.parse.quote(cid),
                    headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'},
                    proxy=_clash_proxy_for_url(api_base),
                    timeout=aiohttp.ClientTimeout(total=6 if custom_u else 4, sock_connect=3),
                    allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 262144)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status == 200:
                        j = json.loads(raw.decode('utf-8'))
                        items = j.get('items') if isinstance(j, dict) and isinstance(j.get('items'), list) else []
                        if items:
                            top_t = str((items[0] or {}).get('title') or '').strip()[:30]
                            return jok(ms=ms, message=f'{first_ch}拉取正常（{ms} ms · 共 {len(items)} 条，首条：{top_t}）')
                        agg_err = f'「{first_ch}」暂未返回新闻条目'
                    else:
                        agg_err = f'热榜聚合站返回 HTTP {r.status}'
            except Exception as e:
                agg_err = fmt_net_err(e, '热榜聚合接口')
            if custom_u or not NEWSNOW_API_URL.startswith('https://newsnow.busiyi.world'):
                raise ApiError(agg_err or '热榜新闻接口拉取失败', 502)
            # 与小智 get_news_from_newsnow.py 完全一致：聚合站受阻时自动走华尔街见闻 / 新浪 7x24 直连兜底
            fb_targets = (
                [('华尔街见闻直连', 'https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=5', 'wscn'),
                 ('新浪 7×24 直连', 'https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=5&zhibo_id=152', 'sina')]
                if cid in ('wallstreetcn-quick', 'gelonghui') else
                [('新浪 7×24 直连', 'https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=5&zhibo_id=152', 'sina'),
                 ('华尔街见闻直连', 'https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=5', 'wscn')]
            )
            for fb_lbl, fb_url, fb_kind in fb_targets:
                with contextlib.suppress(Exception):
                    async with self.sess.get(
                        fb_url,
                        headers={'User-Agent': 'Mozilla/5.0'},
                        timeout=aiohttp.ClientTimeout(total=6, sock_connect=3),
                        allow_redirects=False,
                    ) as r2:
                        if r2.status != 200:
                            continue
                        fj = json.loads((await read_resp_bounded(r2, 1048576)).decode('utf-8', errors='replace'))
                        titles = []
                        if fb_kind == 'wscn':
                            for it in (((fj.get('data') or {}).get('items')) or []):
                                if isinstance(it, dict):
                                    t_s = re.sub(r'<[^>]+>', '', str(it.get('title') or it.get('content_text') or it.get('content') or '')).strip()
                                    if t_s:
                                        titles.append(t_s)
                        else:
                            for it in (((((fj.get('result') or {}).get('data') or {}).get('feed') or {}).get('list')) or []):
                                if isinstance(it, dict):
                                    t_s = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', str(it.get('rich_text') or ''))).strip()
                                    if t_s:
                                        titles.append(t_s)
                        if titles:
                            ms = round((time.monotonic() - t0) * 1000)
                            return jok(ms=ms, message=f'快讯直连通道正常（{ms} ms · {fb_lbl}返回 {len(titles)} 条，首条：{titles[0][:28]}）')
            raise ApiError(agg_err or '热榜与直连快讯均拉取失败，请检查网络', 502)

        if kind == 'chinanews':
            cn_c = ((merged.get('plugins') or {}).get('get_news_from_chinanews')) or {}
            raw_rss = str(d.get('rss_url') or clean_text_val(cn_c.get('default_rss_url')) or CHINANEWS_DEFAULT_RSS).strip()
            rss_u = check_safe_url(raw_rss, '中新网 RSS 地址')
            await _verify_outbound_url(rss_u, '中新网 RSS 地址')
            try:
                async with self.sess.get(
                    rss_u,
                    headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/rss+xml, application/xml, text/xml, */*'},
                    timeout=aiohttp.ClientTimeout(total=10, sock_connect=4),
                    allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 524288)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status != 200:
                        raise ApiError(f'中新网 RSS 返回 HTTP {r.status}', 502)
                    xml_txt = raw.decode('utf-8', errors='replace')
                    if '<!ENTITY' in xml_txt.upper():
                        raise ApiError('RSS XML 包含不允许的外部实体定义', 400)
                    root = ET.fromstring(xml_txt)
                    titles = []
                    for it in root.findall('.//item'):
                        tn = it.find('title')
                        if tn is not None and tn.text and tn.text.strip():
                            titles.append(re.sub(r'\s+', ' ', tn.text).strip())
                    if not titles:
                        raise ApiError('RSS 源未解析到有效新闻条目', 502)
                    return jok(ms=ms, message=f'中新网 RSS 连通正常（{ms} ms · 共 {len(titles)} 条，首条：{titles[0][:28]}）')
            except ApiError:
                raise
            except ET.ParseError:
                raise ApiError('RSS XML 格式解析失败，请确认地址为有效 RSS 源', 502)
            except Exception as e:
                raise ApiError(fmt_net_err(e, '中新网 RSS 源'), 502)

        if kind == 'asr':
            sel = str(d.get('selected') or 'SherpaXASR').strip()
            if sel == 'SherpaXASR':
                req_md = str(d.get('model_dir') or '').strip()
                if req_md:
                    if len(req_md) > 200 or '\\' in req_md or '\x00' in req_md or os.path.isabs(req_md) or '..' in Path(req_md).parts:
                        raise ApiError('本机离线模型路径格式不对')
                    p = HUB_DIR / req_md
                    try:
                        if not p.resolve().is_relative_to(HUB_DIR.resolve()):
                            raise ApiError('本机离线模型路径超出允许目录')
                    except OSError:
                        raise ApiError('本机离线模型路径不可访问')
                else:
                    shp_a = ((merged.get('ASR') or {}).get('SherpaXASR')) or {}
                    _rel, p = self._resolve_sherpa_dir(shp_a)
                if p.is_dir():
                    return jok(ms=1, message=f'本机离线识别模型就绪（{p.name}）')
                raise ApiError(f'未找到本机离线模型目录：{p.name}', 404)
            if sel == 'MacASR':
                mac_c = ((merged.get('ASR') or {}).get('MacASR')) or {}
                u = normalize_mac_asr_url(d.get('base_url') or clean_text_val(mac_c.get('base_url')))
                mod = str(d.get('model_name') or clean_text_val(mac_c.get('model_name')) or 'Qwen3-ASR-1.7B').strip()[:80]
                ak = str(d.get('api_key') or '').strip()
                if not ak and not d.get('clear_api_key') and not is_placeholder_secret(mac_c.get('api_key')):
                    ak = str(mac_c.get('api_key')).strip()
                use_prx = bool(d['use_proxy']) if 'use_proxy' in d else bool(mac_c.get('use_proxy', False))
                wav_ping = _wav_wrap_pcm16(b'\x00\x00' * 6400, 16000)
                form = aiohttp.FormData()
                form.add_field('model', mod)
                form.add_field('file', wav_ping, filename='ping.wav', content_type='audio/wav')
                hdrs = {'Authorization': f'Bearer {ak}'} if ak else {}
                await _verify_outbound_url(u, '语音识别接口地址')
                try:
                    async with self.sess.post(
                        u, data=form, headers=hdrs,
                        proxy=(_clash_local_proxy(u) if use_prx else None),
                        timeout=aiohttp.ClientTimeout(total=12, sock_connect=5), allow_redirects=False,
                    ) as r:
                        raw = await read_resp_bounded(r, 65536)
                        ms = round((time.monotonic() - t0) * 1000)
                        if r.status == 200:
                            return jok(ms=ms, message=f'识别网关转写实测通过（{ms} ms · {mod}）')
                        if r.status in (401, 403):
                            raise ApiError(f'语音识别网关鉴权失败（HTTP {r.status}），请检查 API Key', 502)
                        if r.status == 404:
                            raise ApiError('语音识别端点不存在（HTTP 404），请检查识别地址', 502)
                        err_s = _redact_err(raw.decode('utf-8', errors='replace'), ak)
                        raise ApiError(f'识别网关返回 HTTP {r.status}：{err_s[:100]}', 502)
                except ApiError:
                    raise
                except Exception as e:
                    raise ApiError(fmt_net_err(e, '语音识别网关'), 502)
            if sel == 'DoubaoStreamASRV2':
                db_c = ((merged.get('ASR') or {}).get('DoubaoStreamASRV2')) or {}
                saved_ak = '' if (d.get('clear_api_key') or is_placeholder_secret(db_c.get('api_key'))) else str(db_c.get('api_key')).strip()
                saved_tok = '' if (d.get('clear_access_token') or is_placeholder_secret(db_c.get('access_token'))) else str(db_c.get('access_token')).strip()
                saved_app = clean_text_val(db_c.get('appid'))
                auth_mode = str(d.get('auth_mode') or ('api_key' if (saved_ak and not saved_app) else 'appid')).strip()
                res_id = str(d.get('resource_id') or clean_text_val(db_c.get('resource_id')) or 'volc.seedasr.sauc.duration').strip()[:64]
                if auth_mode == 'api_key':
                    ak = str(d.get('api_key') or '').strip() or saved_ak
                    if not ak:
                        raise ApiError('请先填写豆包语音识别的 API Key')
                    probe_cfg = {'api_key': ak, 'resource_id': res_id}
                else:
                    appid = str(d.get('appid') or '').strip()[:64] or saved_app
                    tok = str(d.get('access_token') or '').strip() or saved_tok
                    if not appid or not tok:
                        raise ApiError('请先填写豆包语音识别的 AppID 与 Access Token')
                    probe_cfg = {'appid': appid, 'access_token': tok, 'resource_id': res_id}
                if HUB_PY.is_file() and (HUB_DIR / 'core' / 'providers' / 'asr' / 'doubao_stream.py').is_file():
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            str(HUB_PY), '-B', '-c',
                            'import asyncio, json, sys; '
                            'from core.providers.asr.doubao_stream import check_connection; '
                            'cfg = json.loads(sys.stdin.read()); '
                            'err = asyncio.run(check_connection(cfg)); '
                            'print(json.dumps({"err": err or ""}, ensure_ascii=False))',
                            cwd=str(HUB_DIR),
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        out_b, _err_b = await _communicate_bounded(
                            proc, json.dumps(probe_cfg, ensure_ascii=False).encode('utf-8'), 14.0
                        )
                        ms = round((time.monotonic() - t0) * 1000)
                        if proc.returncode == 0 and out_b.strip():
                            res_j = json.loads(out_b.decode('utf-8', errors='replace').splitlines()[-1])
                            if res_j.get('err'):
                                err_db = _redact_err(res_j['err'], saved_ak, saved_tok, probe_cfg.get('api_key', ''), probe_cfg.get('access_token', ''))[:120]
                                raise ApiError(f"豆包识别握手失败：{err_db}", 502)
                            return jok(ms=ms, message=f'豆包流式识别握手成功（{ms} ms · {res_id}）')
                        raise ApiError('豆包流式识别检测异常，请检查网络或配置', 502)
                    except ApiError:
                        raise
                    except asyncio.TimeoutError:
                        raise ApiError('豆包流式识别连接超时，请检查网络或代理设置', 502)
                    except Exception as e:
                        raise ApiError(f'豆包流式识别测试失败：{type(e).__name__}', 502)
                return jok(ms=1, message=f'豆包识别凭据格式校验通过（{res_id}）')
            raise ApiError('不支持的语音识别方案')

        if kind == 'study_vision':
            scfg = read_json(STUDY_STATE / 'config.json', {})
            v_url = check_safe_url(d.get('vision_url') or (scfg.get('vision_url') if isinstance(scfg, dict) else ''), '学习视觉接口地址')
            sec_s = str(d.get('secret') or '').strip()
            if not sec_s and not d.get('clear_secret'):
                sp = STUDY_STATE / 'secret'
                if sp.is_file():
                    sec_s = sp.read_text(encoding='utf-8').strip()
            if not re.fullmatch(r'[0-9a-fA-F]{64}', sec_s):
                raise ApiError('请先填写或生成 64 位端到端加密密钥')
            sealed = _seal_study_envelope(bytes.fromhex(sec_s), {'prompt': '只回复OK', 'max_tokens': 8, 'images_b64': []})
            await _verify_outbound_url(v_url, '学习视觉接口地址')
            try:
                async with self.sess.post(
                    v_url, json=sealed,
                    timeout=aiohttp.ClientTimeout(total=18, sock_connect=5), allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 65536)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status == 200:
                        return jok(ms=ms, message=f'学习视觉网关与加密密钥双向验证通过（{ms} ms）')
                    if r.status in (401, 403):
                        raise ApiError(f'加密密钥不匹配或被拒绝（HTTP {r.status}），请确认大屏与视觉网关使用同一把 64 位密钥', 502)
                    if r.status == 404:
                        raise ApiError('学习视觉端点不存在（HTTP 404），请检查地址末尾路径（通常为 /study/v1）', 502)
                    raise ApiError(f'学习视觉网关返回 HTTP {r.status}：{_redact_err(raw.decode("utf-8", errors="replace"), sec_s)[:100]}', 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '学习视觉网关'), 502)

        if kind == 'tts_preview':
            txt = re.sub(r'\s+', ' ', str(d.get('text') or '你好，我是家里的小智语音助手，现在声音正常。')).strip()[:120]
            if not txt:
                txt = '你好，我是家里的小智语音助手，现在声音正常。'
            sel = str(d.get('selected') or 'EdgeTTS').strip()
            if sel == 'IndexStreamTTS':
                idx_c = ((merged.get('TTS') or {}).get('IndexStreamTTS')) or {}
                a_url = check_safe_url(d.get('api_url') or clean_text_val(idx_c.get('api_url')), '克隆 TTS 地址')
                voice = str(d.get('voice') or clean_text_val(idx_c.get('voice')) or 'xiaohe').strip()[:64]
                await _verify_outbound_url(a_url, '克隆 TTS 地址')
                try:
                    async with self.sess.post(
                        a_url, json={'text': txt, 'character': voice},
                        timeout=aiohttp.ClientTimeout(total=35, sock_connect=6, sock_read=20), allow_redirects=False,
                    ) as r:
                        pcm = await read_resp_bounded(r, 4 * 1024 * 1024)
                        ms = round((time.monotonic() - t0) * 1000)
                        if r.status != 200 or len(pcm) < 1000:
                            raise ApiError(f'克隆 TTS 返回 HTTP {r.status}（{len(pcm)} 字节）', 502)
                        if pcm[:4] == b'RIFF':
                            wav_b = pcm
                            pcm_bytes = max(0, len(wav_b) - 44)
                        else:
                            if len(pcm) % 2 == 1:
                                pcm = pcm[:-1]
                            pcm_bytes = len(pcm)
                            wav_b = _wav_wrap_pcm16(pcm, 24000)
                        sec = round(max(0.1, pcm_bytes / 48000), 1)
                        return jok(ms=ms, duration=sec, mime='audio/wav',
                                   audio_b64=base64.b64encode(wav_b).decode('ascii'),
                                   message=f'克隆音色试听合成完成（约 {sec} 秒 · {ms} ms）')
                except ApiError:
                    raise
                except Exception as e:
                    raise ApiError(fmt_net_err(e, '克隆 TTS 服务'), 502)

            if sel == 'EdgeTTS':
                edg_c = ((merged.get('TTS') or {}).get('EdgeTTS')) or {}
                ev = str(d.get('voice') or clean_text_val(edg_c.get('voice')) or 'zh-CN-XiaoxiaoNeural').strip()
                if ev not in EDGE_VOICE_SET:
                    ev = 'zh-CN-XiaoxiaoNeural'
                e_rate = _int_or(d.get('rate') if 'rate' in d else edg_c.get('rate'), 0, -50, 50)
                e_pitch = _int_or(d.get('pitch') if 'pitch' in d else edg_c.get('pitch'), 0, -50, 50)
                e_vol = _int_or(d.get('volume') if 'volume' in d else edg_c.get('volume'), 50, -50, 100)
                edge_bin = HUB_DIR / '.venv' / 'bin' / 'edge-tts'
                if edge_bin.is_file():
                    tmp_mp3 = STATE_DIR / f'.pv-{secrets.token_hex(4)}.mp3'
                    try:
                        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
                        p = await asyncio.create_subprocess_exec(
                            str(edge_bin), '--voice', ev, f'--rate={e_rate:+}%', f'--pitch={e_pitch:+}Hz', f'--volume={e_vol:+}%',
                            f'--text={txt}', '--write-media', str(tmp_mp3),
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                        await _communicate_bounded(p, None, 20.0)
                        if p.returncode == 0 and tmp_mp3.is_file():
                            mp3_b = tmp_mp3.read_bytes()
                            ms = round((time.monotonic() - t0) * 1000)
                            if len(mp3_b) > 500:
                                return jok(ms=ms, mime='audio/mpeg', audio_b64=base64.b64encode(mp3_b).decode('ascii'),
                                           message=f'本机 EdgeTTS 试听合成完成（{ms} ms）')
                    except (asyncio.TimeoutError, OSError):
                        raise ApiError('本机 EdgeTTS 试听合成超时或失败，请检查网络连通性', 502)
                    finally:
                        with contextlib.suppress(OSError):
                            tmp_mp3.unlink(missing_ok=True)
                if (merged.get('selected_module') or {}).get('TTS') != 'EdgeTTS' or edge_bin.is_file():
                    raise ApiError('本机 EdgeTTS 试听合成失败，请检查网络连通性', 502)

            if sel == 'CosyVoiceSiliconflow':
                cs_c = ((merged.get('TTS') or {}).get('CosyVoiceSiliconflow')) or {}
                mod = str(d.get('model') or clean_text_val(cs_c.get('model')) or 'FunAudioLLM/CosyVoice2-0.5B').strip()[:120]
                voi = str(d.get('voice') or clean_text_val(cs_c.get('voice')) or 'FunAudioLLM/CosyVoice2-0.5B:alex').strip()[:120]
                tok = str(d.get('access_token') or '').strip()
                if not tok and not d.get('clear_access_token') and not is_placeholder_secret(cs_c.get('access_token')):
                    tok = str(cs_c.get('access_token')).strip()
                if not tok:
                    raise ApiError('请先填写硅基流动 API Key 再试听')
                try:
                    async with self.sess.post(
                        SILICONFLOW_TTS_URL,
                        json={'model': mod, 'input': txt, 'voice': voi, 'response_format': 'mp3'},
                        headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'},
                        timeout=aiohttp.ClientTimeout(total=25, sock_connect=6), allow_redirects=False,
                    ) as r:
                        raw = await read_resp_bounded(r, 4 * 1024 * 1024)
                        ms = round((time.monotonic() - t0) * 1000)
                        if r.status != 200 or len(raw) < 400:
                            err_t = _redact_err(raw.decode('utf-8', errors='replace'), tok)[:120]
                            raise ApiError(f'硅基流动 TTS 返回 HTTP {r.status}：{err_t}', 502)
                        mime = 'audio/wav' if raw[:4] == b'RIFF' else 'audio/mpeg'
                        return jok(ms=ms, mime=mime, audio_b64=base64.b64encode(raw).decode('ascii'),
                                   message=f'硅基 CosyVoice2 试听合成完成（{ms} ms）')
                except ApiError:
                    raise
                except Exception as e:
                    raise ApiError(fmt_net_err(e, '硅基流动 TTS'), 502)

            if sel == 'OpenAITTS':
                oa_c = ((merged.get('TTS') or {}).get('OpenAITTS')) or {}
                a_url = check_safe_url(d.get('api_url') or clean_text_val(oa_c.get('api_url')) or 'https://api.openai.com/v1/audio/speech', 'OpenAI TTS 地址')
                mod = str(d.get('model') or clean_text_val(oa_c.get('model')) or 'tts-1').strip()[:80]
                voi = str(d.get('voice') or clean_text_val(oa_c.get('voice')) or 'alloy').strip()[:80]
                fmt = str(d.get('format') or clean_text_val(oa_c.get('format')) or 'mp3').strip().lower()
                if fmt not in ('pcm', 'wav', 'mp3'):
                    fmt = 'mp3'
                try:
                    spd = round(max(0.5, min(2.0, _finite_float(d.get('speed') if d.get('speed') is not None else (oa_c.get('speed') if oa_c.get('speed') is not None else 1.0)))), 2)
                except (TypeError, ValueError, OverflowError):
                    spd = 1.0
                ak = str(d.get('api_key') or '').strip()
                if not ak and not d.get('clear_api_key') and not is_placeholder_secret(oa_c.get('api_key')):
                    ak = str(oa_c.get('api_key')).strip()
                if not ak:
                    if 'api.openai.com' in a_url.lower():
                        raise ApiError('请先填写 OpenAI TTS 的 API Key 再试听')
                    ak = 'local'
                await _verify_outbound_url(a_url, 'OpenAI TTS 地址')
                try:
                    async with self.sess.post(
                        a_url,
                        json={'model': mod, 'input': txt, 'voice': voi, 'response_format': fmt, 'speed': spd},
                        headers={'Authorization': f'Bearer {ak}', 'Content-Type': 'application/json'},
                        proxy=_clash_proxy_for_url(a_url),
                        timeout=aiohttp.ClientTimeout(total=25, sock_connect=6), allow_redirects=False,
                    ) as r:
                        raw = await read_resp_bounded(r, 4 * 1024 * 1024)
                        ms = round((time.monotonic() - t0) * 1000)
                        if r.status != 200 or len(raw) < 400:
                            err_t = _redact_err(raw.decode('utf-8', errors='replace'), ak)[:120]
                            raise ApiError(f'OpenAI TTS 返回 HTTP {r.status}：{err_t}', 502)
                        if raw[:4] == b'RIFF':
                            mime, out_b = 'audio/wav', raw
                        elif raw[:3] == b'ID3' or _has_mpeg_sync(raw[:16]):
                            mime, out_b = 'audio/mpeg', raw
                        elif raw[:4] == b'OggS':
                            mime, out_b = 'audio/ogg', raw
                        else:
                            pcm_b = raw[:-1] if len(raw) % 2 == 1 else raw
                            mime, out_b = 'audio/wav', _wav_wrap_pcm16(pcm_b, 24000)
                        return jok(ms=ms, mime=mime, audio_b64=base64.b64encode(out_b).decode('ascii'),
                                   message=f'OpenAI TTS 试听合成完成（{ms} ms）')
                except ApiError:
                    raise
                except Exception as e:
                    raise ApiError(fmt_net_err(e, 'OpenAI TTS'), 502)

            if sel == 'HuoshanDoubleStreamTTSV2' and ((merged.get('selected_module') or {}).get('TTS') != 'HuoshanDoubleStreamTTSV2'):
                raise ApiError('火山双向流式音色走设备双工通道：请先点下方「保存配置」，再唤醒大屏小智直接体验', 501)

            # Fallback to XiaoZhi /kiosk/tts when matching the active saved TTS
            kiosk_u = self._hub_reload_url(merged).replace('/kiosk/internal/reload', '/kiosk/tts')
            try:
                async with self.sess.post(
                    kiosk_u, json={'text': txt},
                    timeout=aiohttp.ClientTimeout(total=22, sock_connect=3), allow_redirects=False,
                ) as r:
                    wav_b = await read_resp_bounded(r, 4 * 1024 * 1024)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status == 200 and len(wav_b) > 500:
                        return jok(ms=ms, mime='audio/wav', audio_b64=base64.b64encode(wav_b).decode('ascii'),
                                   message=f'试听合成完成（{ms} ms）')
                    err_j = {}
                    with contextlib.suppress(Exception):
                        err_j = json.loads(wav_b.decode('utf-8'))
                    raise ApiError(err_j.get('error') or f'当前语音合成暂不支持网页直接试听（HTTP {r.status}），请保存后唤醒设备体验', 501)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '语音合成服务'), 502)

        if kind == 'nezha':
            mcfg = read_json(MONITOR_STATE / 'config.json', {})
            if not isinstance(mcfg, dict):
                mcfg = {}
            raw_h = str(d.get('nezha_host') or mcfg.get('nezha_host') or 'https://nezha.example.com').strip().rstrip('/')
            if raw_h and not raw_h.startswith(('http://', 'https://')):
                raw_h = 'https://' + raw_h
            nz_h = check_safe_url(raw_h, '哪吒监控面板地址')
            raw_p = str(d['nezha_proxy'] if 'nezha_proxy' in d else (mcfg['nezha_proxy'] if 'nezha_proxy' in mcfg else 'http://127.0.0.1:7890')).strip()
            if raw_p.lower().startswith(('socks5://', 'socks5h://')):
                raw_p = 'http://' + raw_p.split('://', 1)[1]
            nz_p = check_safe_url(raw_p, '哪吒代理地址', schemes=('http', 'https')) if raw_p else None
            svc_url = nz_h.rstrip('/') + '/api/v1/service'
            nz_info = await _verify_outbound_url(svc_url, '哪吒监控面板地址')
            if nz_p:
                await _verify_outbound_url(nz_p, '哪吒代理地址')
            last_err = None
            for cand_p in ((nz_p, None) if nz_p else (None,)):
                if cand_p and nz_info.get('local'):
                    continue
                try:
                    async with self.sess.get(
                        svc_url,
                        proxy=cand_p,
                        timeout=aiohttp.ClientTimeout(total=8, sock_connect=4),
                        allow_redirects=False,
                    ) as r:
                        raw = await read_resp_bounded(r, 1048576)
                        ms = round((time.monotonic() - t0) * 1000)
                        if r.status != 200:
                            ae = ApiError(f'哪吒面板返回 HTTP {r.status}', 502)
                            if cand_p and r.status >= 500:
                                last_err = ae
                                continue
                            raise ae
                        j = json.loads(raw.decode('utf-8', errors='replace'))
                        data_obj = j.get('data') if isinstance(j, dict) and isinstance(j.get('data'), dict) else {}
                        svcs = len(data_obj.get('services') or {}) if isinstance(data_obj.get('services'), dict) else 0
                        via = '经代理' if cand_p else '直连'
                        return jok(ms=ms, message=f'哪吒面板连通正常（{ms} ms · {via} · 监控项 {svcs} 个）')
                except ApiError as ae:
                    last_err = ae
                    break
                except Exception as e:
                    last_err = e
            if isinstance(last_err, ApiError):
                raise last_err
            raise ApiError(fmt_net_err(last_err or Exception(), '哪吒监控面板'), 502)

        if kind == 'hass':
            ha_c = ((merged.get('plugins') or {}).get('home_assistant')) or {}
            u = check_safe_url(d.get('base_url') or clean_text_val(ha_c.get('base_url')), 'Home Assistant 地址').rstrip('/')
            ak = str(d.get('api_key') or '').strip()
            if not ak and not d.get('clear_api_key') and not is_placeholder_secret(ha_c.get('api_key')):
                ak = str(ha_c.get('api_key')).strip()
            if not ak:
                raise ApiError('请先填写 Home Assistant 长期访问令牌')
            await _verify_outbound_url(u, 'Home Assistant 地址')
            try:
                async with self.sess.get(
                    u + '/api/',
                    headers={'Authorization': f'Bearer {ak}', 'Accept': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=8, sock_connect=4), allow_redirects=False,
                ) as r:
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status == 200:
                        return jok(ms=ms, message=f'Home Assistant 连通正常（{ms} ms）')
                    if r.status in (401, 403):
                        raise ApiError(f'Home Assistant 令牌鉴权失败（HTTP {r.status}）', 502)
                    raise ApiError(f'Home Assistant 返回 HTTP {r.status}', 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, 'Home Assistant'), 502)

        if kind == 'ragflow':
            rf_c = ((merged.get('plugins') or {}).get('search_from_ragflow')) or {}
            u = check_safe_url(d.get('base_url') or clean_text_val(rf_c.get('base_url')), 'RAGFlow 地址').rstrip('/')
            ak = str(d.get('api_key') or '').strip()
            if not ak and not d.get('clear_api_key') and not is_placeholder_secret(rf_c.get('api_key')):
                ak = str(rf_c.get('api_key')).strip()
            if not ak:
                raise ApiError('请先填写 RAGFlow API Key')
            await _verify_outbound_url(u, 'RAGFlow 地址')
            try:
                async with self.sess.get(
                    u + '/api/v1/datasets?page=1&page_size=1',
                    headers={'Authorization': f'Bearer {ak}', 'Accept': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=8, sock_connect=4), allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 65536)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status == 200:
                        j = json.loads(raw.decode('utf-8', errors='replace'))
                        if isinstance(j, dict) and j.get('code') not in (None, 0):
                            raise ApiError(f"RAGFlow 返回错误：{_redact_err(j.get('message') or j.get('code'), ak)[:80]}", 502)
                        return jok(ms=ms, message=f'RAGFlow 知识库连通正常（{ms} ms）')
                    raise ApiError(f'RAGFlow 返回 HTTP {r.status}', 502)
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, 'RAGFlow'), 502)

        if kind == 'vllm':
            vllms = merged.get('VLLM') if isinstance(merged.get('VLLM'), dict) else {}
            sel_v = str(((merged.get('selected_module') or {}).get('VLLM')) or 'OpenAIVLLM')
            vl_c = (vllms.get('OpenAIVLLM') or vllms.get(sel_v) or {})
            if not isinstance(vl_c, dict):
                vl_c = {}
            uf = 'url' if ('url' in vl_c and 'base_url' not in vl_c) else 'base_url'
            raw_b = strip_chat_completions_suffix(d.get('base_url') or clean_text_val(vl_c.get(uf)))
            b_url = check_safe_url(raw_b, '视觉识图接口地址')
            mod = str(d.get('model') or clean_text_val(vl_c.get('model_name')) or '').strip()[:120]
            if not mod:
                raise ApiError('请先填写视觉识图模型名称')
            ak = check_no_ws_secret(d.get('api_key', ''), '视觉识图 API Key')
            if not ak and not d.get('clear_api_key') and not is_placeholder_secret(vl_c.get('api_key')):
                ak = str(vl_c.get('api_key')).strip()
            cloud_vl = any(dom in b_url.lower() for dom in ('bigmodel.cn', 'aliyuncs.com', 'volces.com', 'openai.com', 'anthropic.com', 'googleapis.com'))
            if not ak:
                if cloud_vl:
                    raise ApiError('请先填写视觉识图 API Key')
                ak = 'local'
            b_clean = b_url.rstrip('/')
            if not b_clean.endswith(('/v1', '/v3', '/v4', '/openai')):
                b_clean += '/v1'
            chat_url = b_clean + '/chat/completions'
            tiny_png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ioAAAAASUVORK5CYII='
            await _verify_outbound_url(chat_url, '视觉识图接口地址')
            try:
                async with self.sess.post(
                    chat_url,
                    json={
                        'model': mod,
                        'max_tokens': 64,
                        'stream': False,
                        'messages': [{
                            'role': 'user',
                            'content': [
                                {'type': 'text', 'text': '请只回复OK两个字母'},
                                {'type': 'image_url', 'image_url': {'url': tiny_png}},
                            ],
                        }],
                    },
                    headers={'Authorization': f'Bearer {ak}', 'Content-Type': 'application/json', 'Accept': 'application/json'},
                    proxy=_clash_proxy_for_url(chat_url),
                    timeout=aiohttp.ClientTimeout(total=20, sock_connect=5),
                    allow_redirects=False,
                ) as r:
                    raw = await read_resp_bounded(r, 262144)
                    ms = round((time.monotonic() - t0) * 1000)
                    if r.status != 200:
                        err_t = _redact_err(raw.decode('utf-8', errors='replace'), ak)
                        if r.status in (401, 403):
                            raise ApiError(f'视觉识图鉴权失败（HTTP {r.status}），请检查 API Key', 502)
                        if r.status == 404:
                            raise ApiError('视觉识图端点不存在（HTTP 404），请检查接口地址或模型名', 502)
                        raise ApiError(f'视觉识图返回 HTTP {r.status}：{err_t[:120]}', 502)
                    j = json.loads(raw.decode('utf-8', errors='replace'))
                    ch0 = ((j.get('choices') or [{}])[0]) if isinstance(j, dict) else {}
                    msg_o = (ch0.get('message') or {}) if isinstance(ch0, dict) else {}
                    rep = re.sub(r'\s+', ' ', str(msg_o.get('content') or msg_o.get('reasoning_content') or '')).strip()[:24]
                    return jok(ms=ms, message=f'视觉识图模型连通正常（{ms} ms · {mod}）' + (f' · 回复：{rep}' if rep else ''))
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(fmt_net_err(e, '视觉识图接口'), 502)

        if kind == 'alarm_tone_preview':
            atone = str(d.get('tone') or 'coffee').strip()
            if atone not in ALARM_TONE_IDS:
                raise ApiError('不支持这个闹钟铃声')
            t_lbl = next((x['label'] for x in ALARM_TONES if x['id'] == atone), atone)
            ogg_fn = ALARM_TONE_FILES.get(atone)
            if ogg_fn:
                ogg_p = CLIENT_SOUNDS_DIR / ogg_fn
                if ogg_p.is_file() and not ogg_p.is_symlink():
                    raw_o = ogg_p.read_bytes()
                    if raw_o:
                        return jok(ms=1, mime='audio/ogg', audio_b64=base64.b64encode(raw_o).decode('ascii'),
                                   message=f'已在网页端播放铃声「{t_lbl}」（家里大屏不发声）')
            wav_m = await asyncio.get_running_loop().run_in_executor(None, _synth_marimba_wav)
            return jok(ms=2, mime='audio/wav', audio_b64=base64.b64encode(wav_m).decode('ascii'),
                       message=f'已在网页端播放铃声「{t_lbl}」（家里大屏不发声）')

        raise ApiError('不支持的测试类型')


async def bridge_ws(a, b):
    async def pump(src, dst):
        with contextlib.suppress(ConnectionError, RuntimeError, aiohttp.ClientError):
            async for msg in src:
                if msg.type == WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                else:
                    break
    t1 = asyncio.ensure_future(pump(a, b))
    t2 = asyncio.ensure_future(pump(b, a))
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t1, t2):
            t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


async def read_limited(request, limit: int) -> bytes:
    buf = bytearray()
    try:
        async for chunk in request.content.iter_chunked(8192):
            buf.extend(chunk)
            if len(buf) > limit:
                raise ApiError('请求太大', 413)
    except (aiohttp.ClientPayloadError, ConnectionError):
        raise ApiError('没有传完，请重试', 400)
    return bytes(buf)


async def json_body(request, limit: int = 16384) -> dict:
    raw = await read_limited(request, limit)
    if not raw.strip():
        return {}
    try:
        d = json.loads(
            raw.decode('utf-8'),
            parse_constant=lambda _c: (_ for _ in ()).throw(ValueError('non-finite')),
        )
    except (ValueError, UnicodeDecodeError):
        raise ApiError('请求格式不对')
    if not isinstance(d, dict):
        raise ApiError('请求格式不对')
    return d


PUBLIC_API = {'/admin/api/login', '/admin/api/session'}
PURGE_TASK_KEY = web.AppKey('purge_task', asyncio.Task)


def make_app(admin: Admin) -> web.Application:
    seen_fwd = {'done': False}

    @web.middleware
    async def guard(request, handler):
        admin.last_active_mono = time.monotonic()
        path = request.path
        if not seen_fwd['done'] and request.headers.get('X-TS-Orig-Host'):
            seen_fwd['done'] = True     # 只记一次：经 intercom 转发进来的请求带了哪些转发头（只记名字，不记内容）
            log.info('forwarded headers present: %s', ','.join(
                k for k in ('X-Real-IP', 'X-Forwarded-For', 'X-Forwarded-Proto', 'X-Forwarded-Host') if k in request.headers) or 'none')
        try:
            peer = request.headers.get('X-TS-Peer')
            wan_req = is_wan_request(request)
            if wan_req:
                if not admin.wan_enabled():
                    if path == '/admin/':
                        return web.Response(
                            status=403, content_type='text/html', charset='utf-8', headers=NO_STORE,
                            text="<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>外网访问未开启</title><body style='font-family:system-ui;background:#0b0f17;color:#eef2f8;display:grid;place-items:center;min-height:90vh;margin:0;padding:24px;text-align:center'><div style='max-width:360px;background:#141a26;border:1px solid rgba(255,255,255,.1);border-radius:20px;padding:24px'><h3 style='margin:0 0 10px'>外网访问未开启</h3><p style='margin:0;color:#8a93a6;font-size:14px;line-height:1.6'>当前管理后台仅限家里内网及专属隧道访问。如需从公网访问，请先在内网进入后台打开「外网访问」按钮。</p></div></body>",
                        )
                    raise ApiError('外网访问未开启，请先在后台开启外网访问', 403)
            elif peer is not None and not peer_ok(peer):
                raise ApiError('只能连上指定的 VPS 节点或在家里局域网打开', 403)
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and not same_origin_ok(request):
                raise ApiError('来源不对，请从管理后台页面操作', 403)
            if request.method == 'GET' and request.headers.get('Upgrade', '').lower() == 'websocket' and not same_origin_ok(request):
                raise ApiError('来源不对', 403)
            needs_login = (path.startswith('/admin/api/') and path not in PUBLIC_API) or path.startswith('/admin/clash-ui')
            if needs_login and not is_vpn_tunnel(request):
                if (wan_req or admin.auth.record() is not None) and not admin.auth.check(request.cookies.get(COOKIE)):
                    if path.startswith('/admin/clash-ui'):
                        raise web.HTTPFound('/admin/#clash')
                    return web.json_response({'ok': False, 'error': '请先登录', 'login': True}, status=401, headers=NO_STORE)
            return await handler(request)
        except ApiError as e:
            return web.json_response({'ok': False, 'error': e.msg}, status=e.status, headers=NO_STORE)

    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    r = app.router
    r.add_get('/admin', admin.redirect_slash)
    r.add_get('/admin/', admin.page)
    r.add_get(r'/admin/{name:favicon\.svg|icon\.png}', admin.asset)
    r.add_get('/admin/api/session', admin.session)
    r.add_post('/admin/api/login', admin.login)
    r.add_post('/admin/api/logout', admin.logout)
    r.add_post('/admin/api/password', admin.change_password)
    r.add_post('/admin/api/wan_access', admin.set_wan_access)
    r.add_get('/admin/api/songs', admin.songs)
    r.add_post('/admin/api/songs/upload', admin.upload)
    r.add_get('/admin/api/songs/file', admin.song_file)
    r.add_post('/admin/api/songs/delete', admin.delete)
    r.add_post('/admin/api/songs/restore', admin.restore)
    r.add_post('/admin/api/songs/purge', admin.purge)
    r.add_post('/admin/api/songs/rename', admin.rename)
    r.add_post('/admin/api/songs/move', admin.move)
    r.add_post('/admin/api/songs/mkdir', admin.mkdir)
    r.add_post('/admin/api/songs/rmdir', admin.rmdir)
    r.add_post('/admin/api/songs/play', admin.play)
    r.add_get('/admin/api/songs/import', admin.import_status)
    r.add_post('/admin/api/songs/import', admin.import_start)
    r.add_post('/admin/api/songs/import/cancel', admin.import_cancel)
    r.add_get('/admin/api/music/status', admin.music_status)
    r.add_post('/admin/api/music/control', admin.music_control)
    r.add_get('/admin/api/settings', admin.get_settings)
    r.add_post('/admin/api/settings/llm', admin.save_settings_llm)
    r.add_post('/admin/api/settings/memory', admin.save_settings_memory)
    r.add_post('/admin/api/settings/voice', admin.save_settings_voice)
    r.add_post('/admin/api/settings/apis', admin.save_settings_apis)
    r.add_post('/admin/api/settings/study', admin.save_settings_study)
    r.add_post('/admin/api/settings/extra', admin.save_settings_extra)
    r.add_post('/admin/api/settings/device', admin.save_settings_device)
    r.add_post('/admin/api/settings/test', admin.test_settings)
    r.add_get('/admin/api/clash', admin.clash)
    r.add_route('*', '/admin/api/clash/{tail:.*}', admin.clash)
    async def _clash_ui_slash(_request):
        raise web.HTTPPermanentRedirect('/admin/clash-ui/')

    r.add_get('/admin/clash-ui', _clash_ui_slash)
    r.add_get('/admin/clash-ui/{path:.*}', admin.clash_ui)

    async def _startup(app_):
        admin.sess = aiohttp.ClientSession(trust_env=False)
        loop = asyncio.get_running_loop()
        with contextlib.suppress(Exception):
            await loop.run_in_executor(None, cleanup_partials, 0)

        async def _purge_loop():
            while True:
                with contextlib.suppress(Exception):
                    await loop.run_in_executor(None, admin._locked, admin.trash.purge)
                await asyncio.sleep(3600)
        app_[PURGE_TASK_KEY] = asyncio.ensure_future(_purge_loop())

    async def _shutdown(_app):
        for r_up in list(admin.open_clash_reqs):
            with contextlib.suppress(Exception):
                r_up.close()
        close_coros = []
        for ws_d, ws_u in list(admin.open_ws):
            close_coros.append(ws_u.close())
            close_coros.append(ws_d.close())
        if close_coros:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*close_coros, return_exceptions=True), timeout=1.5)

    async def _cleanup(app_):
        if admin.job and admin.job.state == 'running':
            admin.job.cancel = True
            if admin.job_thread and admin.job_thread.is_alive():
                await asyncio.get_running_loop().run_in_executor(None, admin.job_thread.join, 1.5)
        t = app_.get(PURGE_TASK_KEY)
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        if admin.sess:
            await admin.sess.close()

    app.on_startup.append(_startup)
    app.on_shutdown.append(_shutdown)
    app.on_cleanup.append(_cleanup)
    return app


def cli_set_password() -> int:
    raw = sys.stdin.readline().rstrip('\r\n')
    if STATE_DIR.is_symlink():
        print('拒绝操作符号链接状态目录', file=sys.stderr)
        return 2
    if raw == '':
        for name in ('sessions.json', 'auth.json'):
            fp = STATE_DIR / name
            if fp.is_symlink():
                continue
            with contextlib.suppress(FileNotFoundError):
                fp.unlink()
        WanGate(STATE_DIR).set_enabled(False)
        print('已取消管理后台密码：现在和远程可视一样，只有连上指定的 VPS 节点或在家里局域网才能打开。')
        return 0
    if ':' in raw:
        u_part, pw = raw.split(':', 1)
        try:
            uname = clean_username(u_part)
        except ApiError as e:
            print(e.msg, file=sys.stderr)
            return 2
    else:
        uname = Auth(STATE_DIR).username()
        pw = raw
    if len(pw) < MIN_PASSWORD:
        print(f'密码至少 {MIN_PASSWORD} 位，没有修改。', file=sys.stderr)
        return 2
    if len(pw) > 128:
        print('密码太长了，没有修改。', file=sys.stderr)
        return 2
    write_private_json(STATE_DIR / 'sessions.json', {})          # 所有设备都要用新密码重新登录
    write_private_json(STATE_DIR / 'auth.json', hash_password(pw, uname))
    print(f'管理后台账号（{uname}）与密码已设置，所有设备需要用新凭据重新登录。')
    return 0


def _systemd_listen_socket():
    """Return non-blocking listening socket passed by systemd socket activation (fd=3), else None."""
    try:
        pid_ok = os.environ.get('LISTEN_PID') == str(os.getpid())
        fds = int(os.environ.get('LISTEN_FDS', '0') or '0')
    except ValueError:
        return None
    if not pid_ok or fds < 1:
        return None
    os.environ.pop('LISTEN_PID', None)
    os.environ.pop('LISTEN_FDS', None)
    os.environ.pop('LISTEN_FDNAMES', None)
    sock = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM, fileno=3)
    sock.setblocking(False)
    return sock


def _admin_busy(admin: Admin) -> bool:
    return (
        bool(admin.open_ws)
        or bool(admin.open_clash_reqs)
        or sum(admin.inflight_bytes.values()) > 0
        or bool(admin.job and admin.job.state == 'running')
    )


async def _serve_admin(admin: Admin):
    sd_sock = _systemd_listen_socket()
    runner = web.AppRunner(make_app(admin), access_log=None, handle_signals=False)
    await runner.setup()
    stop_ev = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_ev.set)

    idle_task = None
    try:
        if sd_sock is not None:
            site = web.SockSite(runner, sd_sock, shutdown_timeout=5.0)
            await site.start()
            idle_sec = max(0, int(os.environ.get('ADMIN_IDLE_EXIT_SEC', '900') or '900'))
            log.info('socket-activated mode (idle auto-exit=%ds)', idle_sec)
            if idle_sec > 0:
                async def _idle_watch():
                    while not stop_ev.is_set():
                        await asyncio.sleep(min(15.0, max(0.5, idle_sec / 3.0)))
                        if _admin_busy(admin):
                            admin.last_active_mono = time.monotonic()
                            continue
                        if time.monotonic() - admin.last_active_mono >= idle_sec:
                            log.info('idle for %ds, parking cleanly until next request', idle_sec)
                            stop_ev.set()
                            return
                idle_task = asyncio.create_task(_idle_watch())
        else:
            site = web.TCPSite(runner, host=BIND, port=PORT, reuse_address=True, shutdown_timeout=5.0)
            await site.start()
        await stop_ev.wait()
    finally:
        if idle_task is not None:
            idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await idle_task
        await runner.cleanup()


def main():
    ap = argparse.ArgumentParser(description='联想 View 管理后台')
    ap.add_argument('--set-password', action='store_true', help='从标准输入读一行作为新密码')
    args = ap.parse_args()
    if args.set_password:
        sys.exit(cli_set_password())
    os.umask(0o022)
    admin = Admin()
    if not admin.secret:
        log.warning('mihomo secret not found; Clash calls go without a key')
    asyncio.run(_serve_admin(admin))


if __name__ == '__main__':
    main()
