#!/opt/thinksmart-addons/venv/bin/python
"""ThinkSmart View study companion: while switched on, glance at the child once a minute and
gently remind them (in XiaoZhi's voice) about posture, eye distance, phone/toys, dozing off or
drifting attention.

Pipeline (nothing is stored except text results):
  camera   a burst of 3 JPEGs 4 s apart (burst_frames / burst_gap) borrowed from thinksmart-intercom
           (ws://127.0.0.1:18091/ws; the camera helper runs ~10 s and stops again when we disconnect),
           so a one-moment action (picking up a pencil, glancing up) is not taken for a habit;
           lens cover closed -> skip
  judge    the picture is sealed end to end (SHAKE256 stream + HMAC-SHA256, shared secret) and sent
           through the owner's Mac gateway to a local Qwen3-VL-8B (study-vision/vision_bridge.py)
  decide   an issue must show up in N consecutive checks; per-issue cooldown and a minimum gap
  speak    phrases are synthesised once with the same TTS / voice as XiaoZhi and cached; never while
           XiaoZhi is in a conversation, during an intercom call or AirPlay mirroring; music pauses
           for the reminder and resumes afterwards
Switch: manual only (the 「学习陪伴」 button on the XiaoZhi home page), with a safety auto-off after
auto_off_hours (default 3) so the camera is never left sampling all night by accident.
State: /var/lib/thinksmart-study/{config.json,secret,voice/,log/YYYY-MM-DD.jsonl}
API (loopback; CORS only for the kiosk pages on :8090/:8092): GET /api/study, POST /api/study/toggle
{"enabled": bool}, POST /api/study/config, POST /api/study/check-now
Parent page (home LAN, http://<device>:8094/): /usr/local/share/thinksmart-study/parent.html with
GET /api/parent/state, /api/parent/day?date=YYYY-MM-DD, /api/parent/days?n=14 and
POST /api/parent/toggle (same-origin only: needs header X-Study-Parent: 1 + JSON, which a foreign
page cannot send without a CORS preflight we never grant). Text results only - no pictures exist.

Round 109 additions (loopback only):
  GET/POST /api/study/focus  pomodoro focus timer {"action": "start", "minutes": 25, "break_minutes": 5,
           "with_study": true} / {"action": "stop"}; switches the companion on for the focus period
           (and back off afterwards if it switched it on), reminds by voice when focus / break end;
           focus sessions are logged as focus_* events and summarised for the parent page.
  POST /api/voice/say        {"text": "..."} the one shared voice channel for other local services
           (message board read-out): needs header X-TS-Internal: 1 and no Origin, so no web page can
           use it; same rules as reminders (never over XiaoZhi / a call / AirPlay, music paused).
"""
import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import random
import re
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

try:
    from thinksmart_display import send_preview as _send_display_preview
except Exception:
    _send_display_preview = None

STATE = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/thinksmart-study'))
CONFIG_PATH = STATE / 'config.json'
SECRET_PATH = STATE / 'secret'
VOICE_DIR = STATE / 'voice'
LOG_DIR = STATE / 'log'
RUN = Path('/run/user/1000')
_DEFAULT_RUN = RUN
_PLAY_ENV = dict(os.environ, XDG_RUNTIME_DIR=str(RUN), PULSE_SERVER=f'unix:{RUN}/pulse/native')
INTERCOM_WS = 'http://127.0.0.1:18091/ws'
CLIENT_EVENTS = 'http://127.0.0.1:8090/events'
MUSIC = 'http://127.0.0.1:8091/api/music'
PORT = 8093
PARENT_PORT = 8094                       # parent page on the home LAN
PARENT_PAGE = Path('/usr/local/share/thinksmart-study/parent.html')
PARENT_ICONS = {'favicon.svg': 'image/svg+xml', 'apple-touch-icon.png': 'image/png'}  # next to parent.html
FOCUS_PATH = STATE / 'focus.json'          # round 109: running pomodoro survives a service restart
FOCUS_STALE = 600                          # a focus/break that ended >10 min ago (service was down) ends silently
TIMESYNC_FLAG = Path('/run/systemd/timesync/synchronized')
UPTIME_PATH = Path('/proc/uptime')


def _boot_clock_unsynced() -> bool:
    if TIMESYNC_FLAG.exists():
        return False
    try:
        up = float(UPTIME_PATH.read_text(encoding='ascii').split()[0])
        return 0.0 <= up < 90.0
    except (OSError, ValueError, IndexError):
        return False
VOICE_DYNAMIC_DAYS = 3                     # synthesized one-off texts (messages) are pruned after this
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
DISTRACTED = {'sleep', 'phone', 'playing', 'focus'}   # a present check with none of these = focused
LOG = logging.getLogger('study')
LOCAL_TZ = ZoneInfo('Asia/Taipei')
ACTIVITIES = {'studying', 'reading', 'phone', 'playing', 'daydreaming', 'sleeping', 'absent', 'other'}
POSTURES = {'good', 'slouch', 'lying', 'tilted', 'unknown'}

DEFAULTS = {
    'enabled': False,                    # manual switch (home page button)
    'enabled_at': 0,                     # wall clock when switched on
    'auto_off_hours': 3,                 # safety: switch itself off after this long (0 = never)
    'interval': 60,                      # seconds between checks
    'burst_frames': 3,                   # frames per check (1-4), judged together in time order
    'burst_gap': 4,                      # seconds between the frames of one check
    'cooldown': 300,                     # same reminder at most once per this many seconds
    'min_gap': 90,                       # any two reminders at least this far apart
    'vision_url': '',                    # http://<gateway>/<token>/study/v1
    'tts_url': '',                       # http://<gateway>/<token>/tts  (same voice as XiaoZhi)
    'voice': 'xiaohe',
}

PROMPT = """你是陪伴孩子学习的助手，正在通过书桌上正对孩子的摄像头查看孩子的学习状态。{extra}
{frames}只输出一个 JSON 对象，不要输出任何其他文字：
{{"present": 书桌前有没有孩子(true/false),
 "posture": "good"(坐姿端正) / "slouch"(明显驼背、弯腰) / "lying"(趴在桌上) / "tilted"(身体或头明显歪斜) / "unknown"(看不清),
 "too_close": 眼睛离书本或桌面明显过近(少于一拳到一尺)时为 true,
 "activity": "studying"(写作业) / "reading"(看书) / "phone"(看或玩手机、平板) / "playing"(玩玩具或其他东西) / "daydreaming"(发呆、东张西望) / "sleeping"(闭眼、睡着) / "absent"(人不在) / "other",
 "focused": 是否在专心学习(true/false),
 "note": 不超过 20 个字描述你看到的情况}}
判断要保守：不要把拿文具、正常低头写字、短暂抬头或相机偏低造成的透视当成问题。
只有能清楚辨认手机/玩具并持续使用时才判 phone/playing；不要猜测看不清的物体。
看不清或被裁切时用 posture=unknown、activity=other，不要假装确认了正在学习。
单目画面不能精确测量厘米；只有眼睛和书本都清楚且明显贴近时才判 too_close。"""

FRAMES_ONE = '只根据这张图片判断。\n'
FRAMES_MANY = ('上面是按时间先后、每隔约 {gap} 秒连拍的 {n} 张照片。综合这几张判断孩子这段时间的状态：'
               '只有在大多数照片里都持续出现的情况才算（例如每张都在玩手机、每张都趴着）；'
               '只在一张里出现的一瞬间动作（拿起文具、抬头、伸手、转头）不算问题。\n')


def build_prompt(n, gap):
    frames = FRAMES_MANY.format(n=n, gap=round(gap)) if n > 1 else FRAMES_ONE
    return PROMPT.format(extra='', frames=frames)


# issue -> (consecutive checks needed, phrases)
ISSUES = {
    'sleep':   (2, ['是不是有点累了？站起来活动一下再继续吧。', '困了的话，先伸个懒腰、喝口水再学习哦。']),
    'phone':   (2, ['先把手机放一放，专心写完作业再玩吧。', '手机先休息一下，作业写完再看哦。']),
    'playing': (2, ['先专心学习，学完再玩哦。', '东西先放一边，我们专心写作业吧。']),
    'eyes':    (2, ['眼睛离桌面远一点，保护好视力哦。', '头抬高一点，眼睛离书本一尺远哦。']),
    'posture': (2, ['坐姿要端正哦，把背挺直一点。', '小提醒：坐直一点，身体会更舒服哦。']),
    'focus':   (3, ['注意力回来啦，加油继续学习！', '专心一点哦，你可以的，加油！']),
}
SWITCH_PHRASES = {'on': '学习陪伴已开启，加油哦！', 'off': '学习陪伴已关闭，辛苦啦！',
                  'auto_off': '学习陪伴已经开了很久，先自动关闭啦，休息一下吧。'}
KIOSK_ORIGINS = ('http://127.0.0.1:8090', 'http://127.0.0.1:8092', 'http://127.0.0.1:18090', 'http://127.0.0.1:18092',
                 'http://localhost:8090', 'http://localhost:8092', 'http://localhost:18090', 'http://localhost:18092')
ISSUE_NAMES = {'sleep': '犯困', 'phone': '玩手机', 'playing': '玩东西', 'eyes': '离太近',
               'posture': '坐姿', 'focus': '走神'}
FOCUS_BREAK_OVER = '休息结束啦，准备好了就开始下一轮专注吧。'


def focus_start_phrase(minutes):
    return f'好的，开始专注{minutes}分钟，加油！'


def focus_done_phrase(break_minutes):
    return f'专注完成啦！站起来看看窗外，休息{break_minutes}分钟吧。' if break_minutes else '专注完成啦，辛苦啦！'


def _focus_minutes(value, hi=180):
    return value if type(value) is int and 0 <= value <= hi else 0


# ---- config / secret -------------------------------------------------------------------
_CONFIG_CACHE = (None, None)


def _harden_private_dir(d):
    with contextlib.suppress(OSError):
        pst = os.lstat(d)
        if stat.S_ISDIR(pst.st_mode) and (pst.st_mode & 0o077) != 0 and pst.st_uid == os.getuid():
            os.chmod(d, 0o700)


def _read_regular_bytes(path, max_bytes=4 * 1024 * 1024, private=False):
    if private:
        _harden_private_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError('Not a regular bounded file')
        if private and (st.st_mode & 0o077) != 0 and st.st_uid == os.getuid():
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
        chunks = []
        rem = max_bytes + 1
        while rem > 0:
            b = os.read(fd, min(65536, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise OSError('File exceeds max_bytes')
        return (st.st_ino, st.st_mtime_ns, st.st_size), raw
    finally:
        os.close(fd)


def _read_regular_text(path, max_bytes=4 * 1024 * 1024, private=False):
    _, raw = _read_regular_bytes(path, max_bytes, private=private)
    return raw.decode('utf-8')


async def _bounded_json(r, max_bytes=262144):
    content = getattr(r, 'content', None)
    if content is not None and hasattr(content, 'iter_chunked') and not type(content).__module__.startswith('unittest.mock'):
        buf = bytearray()
        async for chunk in content.iter_chunked(min(65536, max_bytes + 1)):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError('JSON response exceeds maximum size')
        return json.loads(bytes(buf).decode('utf-8'))
    if content is not None and callable(getattr(content, 'read', None)):
        raw = await content.read(max_bytes + 1)
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > max_bytes:
                raise ValueError('JSON response exceeds maximum size')
            return json.loads(raw.decode('utf-8'))
    return await r.json()


_PRE_NTP_PENDING = False


def load_config():
    global _CONFIG_CACHE, _PRE_NTP_PENDING
    cfg = dict(DEFAULTS)
    try:
        st = os.lstat(CONFIG_PATH)
        if not stat.S_ISREG(st.st_mode):
            raise OSError('Refusing non-regular config')
        sig = (str(CONFIG_PATH), st.st_ino, st.st_mtime_ns, st.st_size)
        if _CONFIG_CACHE[0] == sig:
            raw = _CONFIG_CACHE[1]
        else:
            raw = json.loads(_read_regular_text(CONFIG_PATH, 262144, private=True))
            if isinstance(raw, dict):
                raw = {k: v for k, v in raw.items() if k in DEFAULTS}
            else:
                raw = {}
            _CONFIG_CACHE = (sig, raw)
        cfg.update(raw)
    except (OSError, ValueError):
        _CONFIG_CACHE = (None, None)
    for key, lo, hi in (('interval', 30, 600), ('cooldown', 60, 3600), ('min_gap', 30, 1800),
                        ('auto_off_hours', 0, 12), ('burst_frames', 1, 4)):
        value = cfg[key]
        if type(value) is not int or not lo <= value <= hi:
            cfg[key] = DEFAULTS[key]
    if type(cfg['burst_gap']) not in (int, float) or not math.isfinite(cfg['burst_gap']) or not 1 <= cfg['burst_gap'] <= 8:
        cfg['burst_gap'] = DEFAULTS['burst_gap']
    if type(cfg['enabled']) is not bool:
        cfg['enabled'] = False
    _PRE_NTP_PENDING = bool(
        cfg['enabled'] and type(cfg['enabled_at']) in (int, float)
        and math.isfinite(cfg['enabled_at']) and cfg['enabled_at'] > time.time() + 120
    )
    if (not cfg['enabled'] or type(cfg['enabled_at']) not in (int, float)
            or not math.isfinite(cfg['enabled_at'])
            or not 0 < cfg['enabled_at'] <= time.time() + 120):
        cfg['enabled_at'], cfg['enabled'] = 0, False
    for key in ('vision_url', 'tts_url', 'voice'):
        if not isinstance(cfg[key], str):
            cfg[key] = DEFAULTS[key]
    return cfg


def save_config(cfg):
    global _CONFIG_CACHE
    CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CONFIG_PATH.parent / f'.{CONFIG_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp'
    created = False
    try:
        if CONFIG_PATH.is_symlink():
            raise OSError('Refusing symlink config path')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        created = True
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(cfg, ensure_ascii=False, indent=1))
            f.flush()
            os.fsync(f.fileno())
        if CONFIG_PATH.is_symlink():
            raise OSError('Refusing symlink config path')
        os.replace(tmp, CONFIG_PATH)
        created = False
    finally:
        if created:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
    try:
        st = CONFIG_PATH.stat()
        _CONFIG_CACHE = ((str(CONFIG_PATH), st.st_ino, st.st_mtime_ns, st.st_size),
                         {k: v for k, v in cfg.items() if k in DEFAULTS})
    except OSError:
        _CONFIG_CACHE = (None, None)


def auto_off_at(cfg):
    hours = cfg.get('auto_off_hours') or 0
    return cfg['enabled_at'] + hours * 3600 if cfg.get('enabled') and hours and cfg.get('enabled_at') else None


# ---- sealing (identical to study-vision/vision_bridge.py on the Mac) --------------------
_KEY_CACHE = {}


def _keys(secret, direction):
    k = (secret, direction)
    pair = _KEY_CACHE.get(k)
    if pair is None:
        enc = hmac.new(secret, b'study-vision enc ' + direction.encode(), hashlib.sha256).digest()
        mac = hmac.new(secret, b'study-vision mac ' + direction.encode(), hashlib.sha256).digest()
        pair = (enc, mac)
        if len(_KEY_CACHE) >= 16:
            _KEY_CACHE.pop(next(iter(_KEY_CACHE)))
        _KEY_CACHE[k] = pair
    return pair


def _xor(data, enc, nonce):
    if not data:
        return b''
    ks = hashlib.shake_256(enc + nonce).digest(len(data))
    return (int.from_bytes(data, 'big') ^ int.from_bytes(ks, 'big')).to_bytes(len(data), 'big')


def seal(secret, direction, obj):
    enc, mac = _keys(secret, direction)
    nonce, ts = os.urandom(16), int(time.time())
    ct = _xor(json.dumps(obj, ensure_ascii=False).encode(), enc, nonce)
    tag = hmac.new(mac, b'v1|%d|' % ts + nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return {'v': 1, 'ts': ts, 'nonce': b64(nonce), 'ct': b64(ct), 'tag': b64(tag)}


def unseal(secret, direction, env, max_age=300):
    enc, mac = _keys(secret, direction)
    if not isinstance(env, dict) or type(env.get('v')) is not int or env['v'] != 1 or type(env.get('ts')) is not int:
        raise PermissionError('bad envelope')
    ts = env['ts']
    try:
        if not all(isinstance(env.get(k), str) for k in ('nonce', 'ct', 'tag')):
            raise PermissionError('bad envelope')
        nonce, ct, tag = (base64.b64decode(env[k], validate=True) for k in ('nonce', 'ct', 'tag'))
    except (KeyError, ValueError, TypeError) as exc:
        raise PermissionError('bad envelope') from exc
    if len(nonce) != 16 or len(tag) != 32:
        raise PermissionError('bad envelope')
    want = hmac.new(mac, b'v1|%d|' % ts + nonce + ct, hashlib.sha256).digest()
    if env.get('v') != 1 or not hmac.compare_digest(tag, want) or abs(time.time() - ts) > max_age:
        raise PermissionError('bad reply seal')
    try:
        reply = json.loads(_xor(ct, enc, nonce))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise PermissionError('bad reply payload') from exc
    if not isinstance(reply, dict):
        raise PermissionError('bad reply payload')
    return reply


def local_now(timestamp=None):
    return dt.datetime.fromtimestamp(time.time() if timestamp is None else timestamp, LOCAL_TZ)


def parse_result(text):
    """Malformed/unclear output is an unknown check, never a successful focused one."""
    if isinstance(text, dict):
        result = text
    else:
        if not isinstance(text, str):
            raise ValueError('model output must be text')
        cleaned = re.sub(r'<think>(?:.*?</think>|.*)', '', text, flags=re.S)
        decoder = json.JSONDecoder()
        result = None
        pos = 0
        for _ in range(32):
            start = cleaned.find('{', pos)
            if start < 0:
                break
            try:
                cand, end = decoder.raw_decode(cleaned, start)
                if isinstance(cand, dict) and 'present' in cand:
                    result = cand
                    break
                pos = end
            except ValueError:
                pos = start + 1
        if result is None:
            raise ValueError('missing model object')
    if not isinstance(result, dict) or any(type(result.get(k)) is not bool for k in ('present', 'too_close', 'focused')):
        raise ValueError('invalid model booleans')
    if (not isinstance(result.get('posture'), str) or result['posture'] not in POSTURES
            or not isinstance(result.get('activity'), str) or result['activity'] not in ACTIVITIES
            or not isinstance(result.get('note'), str)):
        raise ValueError('invalid model fields')
    result = {k: result[k] for k in ('present', 'posture', 'too_close', 'activity', 'focused', 'note')}
    if not result['present'] or result['activity'] == 'absent':
        result.update(present=False, activity='absent', focused=False, posture='unknown', too_close=False)
    elif result['posture'] == 'unknown' and result['activity'] == 'other':
        raise ValueError('unclear frame')
    result['note'] = result['note'][:40]
    return result


# ---- the companion ---------------------------------------------------------------------
class Companion:
    def __init__(self):
        self._secret_sig = None
        try:
            sig, raw_b = _read_regular_bytes(SECRET_PATH, 4096, private=True)
            raw_sec = raw_b.decode('utf-8').strip()
            self.secret = bytes.fromhex(raw_sec) if raw_sec else None
            self._secret_sig = sig if self.secret else None
        except (OSError, ValueError, UnicodeDecodeError):
            self.secret = None
        self.session = None
        self.streak = {k: 0 for k in ISSUES}
        self.last_said = {k: -math.inf for k in ISSUES}
        self.last_any = -math.inf
        self.phrase_turn = {k: 0 for k in ISSUES}
        self.last = None
        self.busy = asyncio.Lock()
        self.vision_down_logged = False
        self.wake = asyncio.Event()
        self.check_task = None
        self.runner_task = None
        self.announcements = set()
        self.focus_announcements = set()
        self.external_announcements = set()
        self.tagged_announcements = {}
        self._deferred_resume = None
        self.music_versions = (-1, -1)
        self.voice_lock = asyncio.Lock()
        self.synthesis_lock = asyncio.Lock()
        self.stopping = False
        self.generation = 0
        self.last_prune = None
        self.pruned_synced = False
        self.focus = self.load_focus()
        self.focus_wake = asyncio.Event()
        self.focus_task = None
        self._enabled_mono = None
        self._enabled_unsynced = False
        self.last_day = local_now().date().isoformat()
        self.last = self._load_day_last(self.last_day)

    @staticmethod
    def _load_day_last(day_iso: str):
        return next((entry for entry in reversed(read_day_public(day_iso))
                     if 'event' not in entry and not entry.get('manual') and entry.get('skip') != 'disabled'), None)

    def last_today(self, today=None):
        is_live = today is None
        if is_live:
            today = local_now().date().isoformat()
        if self.last:
            at = self.last.get('at')
            if type(at) in (int, float) and math.isfinite(at) and local_now(at).date().isoformat() != today:
                if is_live:
                    self.last = None
            else:
                return self.last
        if getattr(self, 'last_day', None) != today:
            try:
                disk_last = self._load_day_last(today)
            except Exception:
                disk_last = None
            if is_live:
                self.last_day = today
                if disk_last is not None:
                    self.last = disk_last
                    return self.last
            elif disk_last is not None:
                return disk_last
        return None

    def reset_streak(self):
        self.streak = {k: 0 for k in ISSUES}

    @staticmethod
    def _focus_use_ends_mono(f: dict) -> bool:
        return 'ends_mono' in f and ('_mono_ends_at' not in f or f.get('ends_at') == f.get('_mono_ends_at'))

    @staticmethod
    def _focus_use_started_mono(f: dict) -> bool:
        return 'started_mono' in f and ('_mono_started_at' not in f or f.get('started_at') == f.get('_mono_started_at'))

    def _focus_holding_study(self) -> bool:
        f = self.focus
        if not (isinstance(f, dict) and f.get('phase') == 'focus' and f.get('with_study')):
            return False
        if self._focus_use_ends_mono(f):
            return float(f['ends_mono']) > time.monotonic()
        return float(f.get('ends_at') or 0) > time.time()

    @staticmethod
    def media_busy():
        return (
            (RUN / 'intercom-call-active').exists()
            or (RUN / 'uxplay-mirroring-active').exists()
            or (RUN / 'uxplay-audio-active').exists()
        )

    @staticmethod
    def _night_view_active() -> bool:
        if RUN != _DEFAULT_RUN:
            return False
        try:
            st = (RUN / 'thinksmart-night-view').lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > 1024:
                return False
            data = json.loads((RUN / 'thinksmart-night-view').read_text('utf-8'))
            return isinstance(data, dict) and float(data.get('until') or 0) > time.time()
        except Exception:
            return False

    @staticmethod
    def _in_quiet_night() -> bool:
        if STATE != Path('/var/lib/thinksmart-study'):
            return False
        lt = time.localtime()
        return (0, 0) <= (lt.tm_hour, lt.tm_min) < (6, 30)

    @classmethod
    def _poke_display(cls):
        if _send_display_preview is None or FOCUS_PATH != Path('/var/lib/thinksmart-study/focus.json'):
            return
        if cls._night_view_active() or (RUN / 'intercom-call-active').exists() or (RUN / 'uxplay-mirroring-active').exists():
            return
        with contextlib.suppress(Exception):
            _send_display_preview({'wake': True})

    # -- camera
    @staticmethod
    def burst_of(cfg):
        n = max(1, min(4, int(cfg.get('burst_frames') or 3)))
        gap = max(1.0, min(8.0, float(cfg.get('burst_gap') or 4)))
        return n, gap

    async def snapshot(self, cfg):
        """A burst of well-exposed frames (time order) plus the intercom status (lens cover, orientation)."""
        def _rot(val):
            if isinstance(val, bool):
                return 0
            try:
                return int(val or 0) % 360
            except (TypeError, ValueError):
                return 0

        n, gap = self.burst_of(cfg)
        due = [1.5 + i * gap for i in range(n)]      # first one once auto-exposure has settled
        status, frames, latest, started, first_rotation = {}, [], None, time.monotonic(), 0
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(due[-1] + 12) as cm, self.session.ws_connect(
                INTERCOM_WS, timeout=aiohttp.ClientWSTimeout(ws_close=2)) as ws:
            with contextlib.suppress(Exception):
                await ws.send_json({'type': 'saver', 'enable': True})
            while len(frames) < n and time.monotonic() - started < due[-1] + 6:
                msg = await ws.receive(timeout=4)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get('type') == 'status':
                        status = data
                        if status.get('in_call') or self.media_busy():
                            return [], dict(status, skip='call_or_mirroring')
                        if status.get('shutter_closed'):
                            return [], status
                        rotation = _rot(status.get('rot_deg'))
                        if frames and rotation != first_rotation:
                            return [], dict(status, skip='orientation_changed')
                    elif data.get('type') == 'orient':
                        status = dict(status, rot_deg=data.get('rot_deg', status.get('rot_deg', 0)))
                        rotation = _rot(status.get('rot_deg'))
                        if frames and rotation != first_rotation:
                            return [], dict(status, skip='orientation_changed')
                elif msg.type == aiohttp.WSMsgType.BINARY and msg.data[:1] == b'\x01':
                    if self.media_busy():
                        return [], dict(status, skip='call_or_mirroring')
                    latest = msg.data
                    elapsed = time.monotonic() - started
                    if elapsed >= due[len(frames)]:
                        if not frames:
                            first_rotation = _rot(status.get('rot_deg'))
                        frames.append(bytes(latest[1:]))
                        for j in range(len(frames), n):
                            min_due = elapsed + (j - len(frames) + 1) * gap
                            if due[j] < min_due:
                                due[j] = min_due
                        new_dl = loop.time() + max(6.0, due[-1] - elapsed + 6.0)
                        if cm.when() is not None and new_dl > cm.when():
                            cm.reschedule(new_dl)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        if not frames and latest:
            frames = [bytes(latest[1:])]
        return frames, status

    # -- vision
    def _ensure_secret(self):
        try:
            st = os.lstat(SECRET_PATH)
            if not stat.S_ISREG(st.st_mode):
                self.secret = None
                self._secret_sig = None
                raise PermissionError('missing study secret')
            cur_sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            if not self.secret or cur_sig != getattr(self, '_secret_sig', None):
                sig, raw_b = _read_regular_bytes(SECRET_PATH, 4096, private=True)
                raw_sec = raw_b.decode('utf-8').strip()
                self.secret = bytes.fromhex(raw_sec) if raw_sec else None
                self._secret_sig = sig if self.secret else None
        except FileNotFoundError as exc:
            self.secret = None
            self._secret_sig = None
            raise PermissionError('missing study secret') from exc
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            if not self.secret:
                raise PermissionError('missing study secret') from exc
        if not self.secret:
            raise PermissionError('missing study secret')
        return self.secret

    async def judge(self, cfg, frames, rot):
        # Landscape / portrait / upside down: the Mac turns the frame upright (clockwise by the
        # intercom's gravity-sensor rot_deg, same convention as its phone page) before the model sees it.
        secret = self._ensure_secret()
        prompt = build_prompt(len(frames), self.burst_of(cfg)[1])
        body = seal(secret, 'req', {'prompt': prompt, 'rotate': rot % 360, 'max_tokens': 200,
                                     'images_b64': [base64.b64encode(f).decode() for f in frames]})
        async with self.session.post(cfg['vision_url'], json=body, allow_redirects=False,
                                     timeout=aiohttp.ClientTimeout(total=90, sock_connect=10)) as r:
            if r.status != 200:
                raise ConnectionError(f'vision HTTP {r.status}')
            try:
                env = await _bounded_json(r, 262144)
            except (ValueError, TypeError, UnicodeDecodeError, aiohttp.ContentTypeError) as exc:
                raise ConnectionError('vision invalid JSON') from exc
            reply = unseal(secret, 'resp', env)
        if reply.get('re') != body['nonce']:
            raise PermissionError('reply for another request')
        if not reply.get('ok'):
            raise ConnectionError(f"model {reply.get('error')}")
        return dict(parse_result(reply.get('text')), **{
            'rot': reply.get('rotated', 0),
            'ms': reply.get('ms'),
            'frames': len(frames),
        })

    @staticmethod
    def issues_of(r):
        if not r['present'] or r['activity'] == 'absent':
            return set()
        found = set()
        if r['activity'] == 'sleeping':
            found.add('sleep')
        if r['activity'] == 'phone':
            found.add('phone')
        if r['activity'] == 'playing':
            found.add('playing')
        if r['too_close']:
            found.add('eyes')
        if r['posture'] in ('slouch', 'lying', 'tilted'):
            found.add('posture')
        if r['activity'] == 'daydreaming' or (not r['focused'] and r['activity'] not in ('studying', 'reading')):
            found.add('focus')
        return found

    # -- voice
    _phrase_names = {}

    def phrase_file(self, text, voice=None):
        v = (voice if voice is not None else load_config().get('voice')) or DEFAULTS['voice']
        key = text if v == DEFAULTS['voice'] else f'{v}:{text}'
        fn = self._phrase_names.get(key)
        if fn is None:
            fn = hashlib.sha1(key.encode()).hexdigest()[:16] + '.pcm'
            if len(self._phrase_names) >= 256:
                self._phrase_names.pop(next(iter(self._phrase_names)))
            self._phrase_names[key] = fn
        return VOICE_DIR / fn

    def _fixed_voice_names(self):
        v = load_config().get('voice') or DEFAULTS['voice']
        fixed = {t for _, (_, ts) in ISSUES.items() for t in ts} | set(SWITCH_PHRASES.values())
        fixed |= {FOCUS_BREAK_OVER} | {focus_done_phrase(b) for b in (0, 5, 10)}
        fixed |= {focus_start_phrase(m) for m in (15, 25, 30, 45, 60)}
        return {self.phrase_file(t, v).name for t in fixed} | {self.phrase_file(t, DEFAULTS['voice']).name for t in fixed}

    def _cap_dynamic_voices(self, cutoff_ts=None):
        with contextlib.suppress(Exception):
            keep = self._fixed_voice_names()
            dyn = []
            for f in VOICE_DIR.glob('*.pcm'):
                st = f.lstat()
                if f.name in keep or not stat.S_ISREG(st.st_mode):
                    continue
                if cutoff_ts is not None and st.st_mtime < cutoff_ts:
                    f.unlink(missing_ok=True)
                else:
                    dyn.append((st.st_mtime, st.st_size, f))
            dyn.sort(key=lambda item: item[0])
            total_bytes = sum(item[1] for item in dyn)
            while len(dyn) > 64 or total_bytes > 16 * 1024 * 1024:
                _, sz, victim = dyn.pop(0)
                total_bytes -= sz
                victim.unlink(missing_ok=True)

    async def prepare_voice(self, cfg):
        VOICE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        v = cfg.get('voice') or DEFAULTS['voice']
        texts = [t for _, (_, ts) in ISSUES.items() for t in ts] + list(SWITCH_PHRASES.values())
        missing = [t for t in texts if not self.phrase_file(t, v).exists()]
        for text in missing:
            if self.stopping or not await self.synthesize(cfg, text):
                return False
        return True

    async def synthesize(self, cfg, text):
        async with self.synthesis_lock:
            return await self._synthesize(cfg, text)

    def _write_voice_pcm(self, dst: Path, pcm: bytes) -> bool:
        if len(pcm) <= 4800 or len(pcm) % 2 != 0:
            return False
        tmp = dst.parent / f'.{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp'
        created = False
        try:
            if dst.is_symlink():
                raise OSError('Refusing symlink voice path')
            pfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(pfd, 'wb') as pf:
                os.fchmod(pf.fileno(), 0o600)
                pf.write(pcm)
                pf.flush()
                os.fsync(pf.fileno())
            if dst.is_symlink():
                raise OSError('Refusing symlink voice path')
            tmp.replace(dst)
            created = False
        finally:
            if created:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
        self._cap_dynamic_voices()
        return True

    async def _audio_bytes_to_pcm24k(self, raw: bytes):
        if not raw:
            return None
        if raw[:4] == b'RIFF' and raw[8:12] == b'WAVE':
            import io as _io, wave as _wave
            with contextlib.suppress(Exception):
                with _wave.open(_io.BytesIO(raw), 'rb') as wf:
                    if wf.getnchannels() == 1 and wf.getsampwidth() == 2 and wf.getframerate() == 24000:
                        return wf.readframes(wf.getnframes())
        is_container = (
            (raw[:4] in (b'RIFF', b'OggS', b'fLaC'))
            or raw[:3] == b'ID3'
            or (len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0)
        )
        if not is_container:
            return raw
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                'ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', 'pipe:0',
                '-ar', '24000', '-ac', '1', '-f', 's16le', 'pipe:1',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            pcm, _ = await asyncio.wait_for(proc.communicate(raw), 15)
            if proc.returncode == 0 and pcm and len(pcm) <= 4 * 1024 * 1024:
                return pcm
        except Exception:
            pass
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        return None

    async def _synthesize_builtin_edge(self, cfg, text):
        if STATE != Path('/var/lib/thinksmart-study'):
            return False
        VOICE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        dst = self.phrase_file(text, cfg.get('voice'))
        with contextlib.suppress(Exception):
            async with self.session.post('http://127.0.0.1:16688/kiosk/tts', json={'text': text},
                                         allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20, sock_connect=3)) as r:
                if r.status == 200:
                    buf = bytearray()
                    async for chunk in r.content.iter_chunked(65536):
                        buf.extend(chunk)
                        if len(buf) > 4 * 1024 * 1024 + 64:
                            return False
                    pcm = await self._audio_bytes_to_pcm24k(bytes(buf))
                    if pcm and self._write_voice_pcm(dst, pcm):
                        return True
        edge_bin = Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/edge-tts')
        if not edge_bin.is_file():
            return False
        v_raw = str(cfg.get('voice') or '').strip()
        voice = v_raw if re.fullmatch(r'[A-Za-z]{2}-[A-Za-z]{2,10}-[A-Za-z0-9-]+Neural', v_raw) else 'zh-CN-XiaoxiaoNeural'
        mp3_tmp = VOICE_DIR / f'.edge-{os.getpid()}-{threading.get_ident()}.mp3'
        p1 = p2 = None
        try:
            p1 = await asyncio.create_subprocess_exec(
                str(edge_bin), '--voice', voice, f'--text={text}', '--write-media', str(mp3_tmp),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await asyncio.wait_for(p1.wait(), 20) != 0 or not mp3_tmp.is_file():
                return False
            p2 = await asyncio.create_subprocess_exec(
                'ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', str(mp3_tmp),
                '-ar', '24000', '-ac', '1', '-f', 's16le', 'pipe:1',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            pcm, _ = await asyncio.wait_for(p2.communicate(), 15)
            if p2.returncode == 0 and len(pcm) <= 4 * 1024 * 1024:
                return self._write_voice_pcm(dst, pcm)
        except Exception as e:
            LOG.warning('builtin edge voice synthesis unavailable (%s)', type(e).__name__)
        finally:
            for p in (p1, p2):
                if p is not None and p.returncode is None:
                    with contextlib.suppress(Exception):
                        p.kill()
                    with contextlib.suppress(Exception):
                        await p.wait()
            with contextlib.suppress(OSError):
                mp3_tmp.unlink(missing_ok=True)
        return False

    async def _synthesize(self, cfg, text):
        tts_url = (cfg.get('tts_url') or '').strip()
        if not tts_url:
            return await self._synthesize_builtin_edge(cfg, text)
        try:
            VOICE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            async with self.session.post(tts_url, json={'text': text, 'character': cfg['voice']},
                                         allow_redirects=False,
                                         timeout=aiohttp.ClientTimeout(total=30, sock_connect=8)) as r:
                raw_buf = bytearray()
                if r.status == 200:
                    async for chunk in r.content.iter_chunked(65536):
                        raw_buf.extend(chunk)
                        if len(raw_buf) > 4 * 1024 * 1024 + 64:
                            return False
                status = r.status
            pcm = await self._audio_bytes_to_pcm24k(bytes(raw_buf)) if status == 200 else None
            if pcm and len(pcm) <= 4 * 1024 * 1024 and self._write_voice_pcm(self.phrase_file(text, cfg.get('voice')), pcm):
                return True
            LOG.warning('voice synthesis returned HTTP %s / %d bytes; will retry', status, len(raw_buf))
        except Exception as e:
            LOG.warning('voice synthesis unavailable (%s); will retry in a minute', type(e).__name__)
        return False

    async def client_idle(self):
        try:
            async with self.session.get(CLIENT_EVENTS, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200:
                    return False
                buf = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > 4 * 1024 * 1024:
                        return False
                    while True:
                        nl = buf.find(b'\n')
                        if nl < 0:
                            break
                        line = bytes(buf[:nl]).strip()
                        del buf[:nl + 1]
                        if line.startswith(b'data:'):
                            try:
                                data = json.loads(line[5:])
                            except ValueError:
                                continue
                            if isinstance(data, dict) and isinstance(data.get('status'), dict):
                                if data['status'].get('state') != 'idle':
                                    return False
                                alarms = data.get('alarms')
                                if isinstance(alarms, dict) and alarms.get('ringing'):
                                    return False
                                bt = data.get('bluetooth')
                                if isinstance(bt, dict) and any(
                                    isinstance(d, dict) and d.get('streaming')
                                    for d in (bt.get('devices') or [])
                                ):
                                    return False
                                return True
        except Exception:
            return False
        return False

    async def music(self, action=None, seconds=28):
        try:
            if action is None:
                async with self.session.get(MUSIC + '/status', params=dict(qseq=self.music_versions[0], lseq=self.music_versions[1]),
                                            timeout=aiohttp.ClientTimeout(total=3)) as r:
                    value = await _bounded_json(r, 524288)
                    if r.status != 200 or not isinstance(value, dict):
                        return None
                    self.music_versions = tuple(value.get(k, -1) if type(value.get(k)) is int else -1
                                                for k in ('queue_seq', 'lyrics_seq'))
                    track = value.get('track')
                    if track is not None and not isinstance(track, dict):
                        return None
                    amb = value.get('ambient')
                    amb_p = amb.get('preset') if isinstance(amb, dict) and isinstance(amb.get('preset'), str) else None
                    return {**{k: value.get(k) for k in ('playing', 'paused', 'boot_id', 'index', 'alarm_ringing')},
                            'sleep_session': isinstance(value.get('sleep_session'), dict),
                            'ambient': amb_p,
                            'track': {k: track.get(k) for k in ('mid', 'songmid', 'file', 'title', 'artist')} if track else None}
            secs = max(28, int(seconds)) if action == 'duck' else 28
            ctrl_payload = ({'action': action, 'seconds': 28} if secs <= 28 else {'action': action, 'seconds': secs}) if action == 'duck' else {'action': action}
            async with self.session.post(MUSIC + '/control', json=ctrl_payload,
                                         timeout=aiohttp.ClientTimeout(total=3)) as r:
                value = await _bounded_json(r, 65536)
                return r.status == 200 and isinstance(value, dict) and value.get('ok') is True
        except Exception:
            return None

    async def say(self, issue):
        texts = ISSUES[issue][1]
        text = texts[self.phrase_turn[issue] % len(texts)]
        ok = await self.play(text)
        if ok:
            self.phrase_turn[issue] += 1
        return ok

    @staticmethod
    def music_identity(status):
        track = status.get('track') or {}
        return (status.get('boot_id'), status.get('index'),
                track.get('songmid') or track.get('mid') or track.get('file'),
                track.get('title'), track.get('artist'))

    def _clear_deferred_resume(self):
        self._deferred_resume = None
        self._deferred_resume_at = 0.0
        dt = getattr(self, '_deferred_resume_task', None)
        self._deferred_resume_task = None
        if isinstance(dt, asyncio.Task) and not dt.done() and dt is not asyncio.current_task():
            dt.cancel()

    def _set_deferred_resume(self, before):
        self._clear_deferred_resume()
        self._deferred_resume = before
        self._deferred_resume_at = time.monotonic()
        with contextlib.suppress(RuntimeError):
            self._deferred_resume_task = asyncio.create_task(self._retry_deferred_resume(before))

    async def _retry_deferred_resume(self, expected_before):
        for _ in range(8):
            await asyncio.sleep(2.5)
            if self.stopping or getattr(self, '_deferred_resume', None) is not expected_before:
                return
            if time.monotonic() - getattr(self, '_deferred_resume_at', 0.0) > 20.0:
                break
            if self.voice_lock.locked() or self.media_busy() or not await self.client_idle():
                continue
            if getattr(self, '_deferred_resume', None) is not expected_before:
                return
            self._deferred_resume = None
            self._deferred_resume_at = 0.0
            self._deferred_resume_task = None
            after = await self.music()
            if (isinstance(after, dict) and after.get('paused') is not False and not after.get('playing')
                    and not after.get('ambient') and not after.get('alarm_ringing')
                    and self.music_identity(after) == self.music_identity(expected_before) and not self.media_busy()):
                await self.music('resume')
            return
        if getattr(self, '_deferred_resume', None) is expected_before:
            self._deferred_resume = None
            self._deferred_resume_at = 0.0
            self._deferred_resume_task = None

    async def play(self, text, from_duck=False, allow_sleep=False):
        need_from_duck = bool(from_duck)
        try:
            async with self.voice_lock:
                if self.stopping or self._night_view_active() or self.media_busy() or not await self.client_idle():
                    return False
                path = self.phrase_file(text)
                if not path.exists():
                    if from_duck:
                        await self.music('duck')
                    if not await self.synthesize(load_config(), text):
                        return False
                    if self.stopping or self._night_view_active() or self.media_busy():
                        return False
                try:
                    pcm_secs = path.stat().st_size / 48000.0
                except OSError:
                    pcm_secs = 0.0
                before = await self.music()
                if isinstance(before, dict) and (before.get('alarm_ringing') or (before.get('sleep_session') and not (from_duck or allow_sleep))):
                    return False
                paused = False
                did_pause_rpc = False
                ducked_amb = False
                proc, waiter = None, None
                need_from_duck = False
                deferred = getattr(self, '_deferred_resume', None)
                if deferred is not None:
                    def_age = time.monotonic() - getattr(self, '_deferred_resume_at', 0.0)
                    if (def_age <= 20.0 and isinstance(before, dict) and not before.get('playing')
                            and before.get('paused') is not False and not before.get('ambient')
                            and self.music_identity(before) == self.music_identity(deferred)):
                        paused = True
                        before = deferred
                    self._clear_deferred_resume()
                try:
                    if not paused and isinstance(before, dict) and before.get('playing'):
                        paused = bool(await self.music('pause'))
                        did_pause_rpc = paused
                        if not paused:
                            return False
                    elif not paused and isinstance(before, dict) and before.get('ambient'):
                        duck_secs = max(28, int(math.ceil(pcm_secs)) + 8)
                        ducked_amb = bool(await self.music('duck', seconds=duck_secs))
                    if self.stopping or self._night_view_active() or self.media_busy() or not await self.client_idle():
                        return False
                    env = _PLAY_ENV if RUN == _DEFAULT_RUN else dict(os.environ, XDG_RUNTIME_DIR=str(RUN), PULSE_SERVER=f'unix:{RUN}/pulse/native')
                    proc = await asyncio.create_subprocess_exec(
                        'pacat', '--playback', '--rate=24000', '--channels=1', '--format=s16le', str(path),
                        env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    waiter = asyncio.create_task(proc.wait())
                    deadline = time.monotonic() + max(20.0, pcm_secs + 8.0)
                    while not waiter.done():
                        done, _ = await asyncio.wait((waiter,), timeout=0.4)
                        if not done and (time.monotonic() >= deadline or self.media_busy() or not await self.client_idle()):
                            return False
                    return waiter.result() == 0
                finally:
                    async def _cleanup():
                        nonlocal did_pause_rpc
                        if proc is not None and proc.returncode is None:
                            with contextlib.suppress(ProcessLookupError):
                                proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), 1)
                            except asyncio.TimeoutError:
                                with contextlib.suppress(ProcessLookupError):
                                    proc.kill()
                                await proc.wait()
                        if waiter is not None:
                            await asyncio.gather(waiter, return_exceptions=True)
                        if paused:
                            if not self.stopping:
                                if not self.media_busy() and await self.client_idle():
                                    after = await self.music()
                                    if (isinstance(after, dict) and after.get('paused') is not False and not after.get('playing')
                                            and not after.get('ambient') and not after.get('alarm_ringing')
                                            and self.music_identity(after) == self.music_identity(before) and not self.media_busy()):
                                        if await self.music('resume'):
                                            did_pause_rpc = True
                                else:
                                    self._set_deferred_resume(before)
                            elif not self.media_busy():
                                with contextlib.suppress(Exception):
                                    async with asyncio.timeout(1.0):
                                        if await self.client_idle():
                                            after = await self.music()
                                            if (isinstance(after, dict) and after.get('paused') is not False and not after.get('playing')
                                                    and not after.get('ambient') and not after.get('alarm_ringing')
                                                    and self.music_identity(after) == self.music_identity(before) and not self.media_busy()):
                                                if await self.music('resume'):
                                                    did_pause_rpc = True
                        if ducked_amb or (from_duck and not did_pause_rpc):
                            await self.music('unduck')
                    cleanup = asyncio.create_task(_cleanup())
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                    if cancelled:
                        raise asyncio.CancelledError()
        finally:
            if need_from_duck and self.session is not None and not self.session.closed:
                unduck_t = asyncio.create_task(self.music('unduck'))
                cancelled_outer = False
                while not unduck_t.done():
                    try:
                        await asyncio.shield(unduck_t)
                    except asyncio.CancelledError:
                        cancelled_outer = True
                if cancelled_outer:
                    raise asyncio.CancelledError()

    # -- one check
    def start_check(self, cfg, allow_speak=True, manual=False):
        if self.busy.locked() or (self.check_task is not None and not self.check_task.done()):
            raise RuntimeError('check already running')
        gen = self.generation
        task = asyncio.create_task(self.check(cfg, allow_speak=allow_speak, manual=manual, _generation=gen))
        self.check_task = task
        return task

    async def check(self, cfg, allow_speak=True, manual=False, _generation=None):
        if self.busy.locked():
            raise RuntimeError('check already running')
        async with self.busy:
            task = asyncio.current_task()
            self.check_task = task
            generation = self.generation if _generation is None else _generation
            try:
                return await self._check(cfg, allow_speak, manual, generation)
            except asyncio.CancelledError:
                if not manual:
                    self.reset_streak()
                raise
            finally:
                if self.check_task is task:
                    self.check_task = None

    async def _check(self, cfg, allow_speak, manual, generation):
        entry = {'at': time.time(), 'interval': cfg['interval'], 'manual': manual}
        def active():
            return not self.stopping and self.generation == generation and (manual or load_config()['enabled'])
        def skipped(reason):
            return self.record(dict(entry, skip=reason))
        if not active():
            return skipped('disabled')
        if self.media_busy():
            return skipped('call_or_mirroring')
        try:
            frames, status = await self.snapshot(cfg)
        except Exception as e:
            return skipped(f'camera:{type(e).__name__}')
        if not active():
            return skipped('disabled')
        if status.get('skip'):
            return skipped(status['skip'])
        if not frames:
            return skipped('lens_cover' if status.get('shutter_closed') else 'no_frame')
        if len(frames) < self.burst_of(cfg)[0]:
            return skipped('incomplete_burst')
        entry['at'] = time.time()
        raw_rot = status.get('rot_deg')
        try:
            rot_deg = 0 if isinstance(raw_rot, bool) else int(raw_rot or 0)
        except (TypeError, ValueError):
            rot_deg = 0
        try:
            r = await self.judge(cfg, frames, rot_deg)
            self.vision_down_logged = False
        except (ValueError, TypeError):
            return skipped('model_result')
        except Exception as e:
            if not self.vision_down_logged:
                LOG.warning('vision unavailable (%s); skipping checks until it is back', type(e).__name__)
                self.vision_down_logged = True
            return skipped(f'vision:{type(e).__name__}')
        finally:
            del frames
        if not active():
            return skipped('disabled')
        entry.update(r)
        found = self.issues_of(r)
        entry['issues'] = sorted(found)
        if manual:
            if allow_speak and found:
                due_m = [k for k in ISSUES if k in found]
                if due_m and active() and not self.media_busy() and await self.client_idle():
                    if await self.say(due_m[0]):
                        entry['reminded'] = due_m[0]
            return self.record(entry)  # diagnostics don't create extra study minutes or streaks
        for k in ISSUES:
            self.streak[k] = self.streak[k] + 1 if k in found else 0
        now = time.monotonic()
        due = [k for k in ISSUES
               if self.streak[k] >= ISSUES[k][0] and now - self.last_said[k] >= cfg['cooldown']]
        if due and allow_speak and now - self.last_any >= cfg['min_gap']:
            if not active():
                return skipped('disabled')
            if not self.media_busy() and await self.client_idle():
                if await self.say(due[0]):
                    self.last_said[due[0]] = self.last_any = time.monotonic()
                    self.streak[due[0]] = 0
                    entry['reminded'] = due[0]
                    LOG.info('reminded: %s', due[0])
                elif active():
                    entry['deferred'] = due[0]
            elif active():
                entry['deferred'] = due[0]
        if not active():
            return skipped('disabled')
        return self.record(entry)

    def record(self, entry):
        if entry.get('skip') and not entry.get('manual'):
            self.reset_streak()
        entry = self.append_log(entry)
        if not entry.get('manual') and entry.get('skip') != 'disabled':
            self.last = {k: entry[k] for k in _PUBLIC_KEEP if k in entry}
            self.last_day = local_now(entry['at']).date().isoformat()
        return entry

    @staticmethod
    def append_log(entry):
        entry = dict(entry)
        entry.setdefault('at', time.time())
        dt_now = local_now(entry['at'])
        entry.setdefault('t', dt_now.strftime('%H:%M:%S'))
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{dt_now.date().isoformat()}.jsonl"
        line_bytes = (json.dumps(entry, ensure_ascii=False) + '\n').encode('utf-8')
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        lfd = os.open(log_path, flags, 0o600)
        try:
            st = os.fstat(lfd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError('Refusing non-regular log file')
            os.fchmod(lfd, 0o600)
            if st.st_size + len(line_bytes) <= 8 * 1024 * 1024:
                with os.fdopen(lfd, 'wb') as f:
                    lfd = -1
                    f.write(line_bytes)
            else:
                LOG.warning('daily study log reached 8 MiB cap; skipping append')
        finally:
            if lfd >= 0:
                os.close(lfd)
        return entry

    def today(self):
        date = local_now().date().isoformat()
        s = summarize_day(date)
        cached = getattr(self, '_today_cache', None)
        if cached and cached[0] is s:
            return cached[1]
        res = {'checks': s['checks'], 'present': s['present'],
               'reminders': {ISSUE_NAMES.get(k, k): v for k, v in s['reminders'].items()},
               'issues': {ISSUE_NAMES.get(k, k): v for k, v in s['issues'].items()},
               'focus': {'sessions': s.get('focus_sessions', 0), 'minutes': s.get('focus_minutes', 0)}}
        self._today_cache = (s, res)
        return res

    # -- loops
    def _announce(self, text):
        task = asyncio.create_task(self.play(text))
        self.announcements.add(task)
        def finished(task):
            self.announcements.discard(task)
            if not task.cancelled():
                with contextlib.suppress(Exception):
                    task.result()
        task.add_done_callback(finished)
        return task

    def _track_focus_ann(self, task):
        if isinstance(task, asyncio.Task) and not task.done():
            fset = getattr(self, 'focus_announcements', None)
            if fset is not None:
                fset.add(task)
                task.add_done_callback(fset.discard)
        return task

    def _cancel_focus_announcements(self):
        fset = getattr(self, 'focus_announcements', None)
        if fset:
            for task in list(fset):
                task.cancel()
            fset.clear()

    def cancel_tagged_announcements(self, tag):
        tmap = getattr(self, 'tagged_announcements', None)
        if not tmap or not isinstance(tag, str) or not tag:
            return 0
        tset = tmap.pop(tag, None)
        if not tset:
            return 0
        n = 0
        for task in list(tset):
            if not task.done():
                task.cancel()
                n += 1
        tset.clear()
        return n

    def switch(self, on, reason='', announce=True):
        cfg = load_config()
        if reason != 'focus' and isinstance(self.focus, dict) and self.focus.get('study_auto'):
            self.focus['study_auto'] = False
            self.save_focus(self.focus)
        if bool(on) == cfg['enabled']:
            if not on and self.check_task is not None:
                self.check_task.cancel()  # even an idempotent off stops a manual diagnostic
        else:
            cfg['enabled'], cfg['enabled_at'] = bool(on), int(time.time()) if on else 0
            if on:
                self._enabled_mono = time.monotonic()
                self._enabled_unsynced = _boot_clock_unsynced()
            else:
                self._enabled_mono = None
                self._enabled_unsynced = False
            save_config(cfg)
            self.generation += 1
            self.reset_streak()
            if self.check_task is not None:
                self.check_task.cancel()
            ext = getattr(self, 'external_announcements', ())
            fset = getattr(self, 'focus_announcements', ())
            for task in list(self.announcements):
                if task not in ext and task not in fset:
                    task.cancel()
            self.wake.set()
            LOG.info('switched %s%s', 'on' if on else 'off', f' ({reason})' if reason else '')
            self.append_log({'event': 'on' if on else 'off', 'by': reason or 'home'})
            if announce and not (reason == 'auto_off' and (self._in_quiet_night() or self._night_view_active())):
                self._announce(SWITCH_PHRASES['auto_off' if reason == 'auto_off' else ('on' if on else 'off')])
        return cfg

    async def shutdown(self):
        self.stopping = True
        tasks = list(self.announcements)
        dt = getattr(self, '_deferred_resume_task', None)
        self._deferred_resume_task = None
        if isinstance(dt, asyncio.Task) and not dt.done() and dt is not asyncio.current_task():
            tasks.append(dt)
        if self.focus_task is not None and self.focus_task is not asyncio.current_task():
            tasks.append(self.focus_task)
        if self.runner_task is not None and self.runner_task is not asyncio.current_task():
            tasks.append(self.runner_task)
        if self.check_task is not None:
            tasks.append(self.check_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        def_res = getattr(self, '_deferred_resume', None)
        def_age = time.monotonic() - getattr(self, '_deferred_resume_at', 0.0)
        if def_res is not None and def_age <= 20.0 and not self.media_busy() and self.session and not self.session.closed:
            with contextlib.suppress(Exception):
                async def _finish_resume():
                    if await self.client_idle():
                        after = await self.music()
                        if (isinstance(after, dict) and after.get('paused') is not False and not after.get('playing')
                                and not after.get('ambient') and not after.get('alarm_ringing')
                                and self.music_identity(after) == self.music_identity(def_res) and not self.media_busy()):
                            await self.music('resume')
                await asyncio.wait_for(_finish_resume(), timeout=1.2)
            self._clear_deferred_resume()

    async def wait_wake(self, delay):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.wake.wait(), max(0, delay))

    async def run(self):
        voice_ready, prepared_voice, tried_voice, last_voice_try = False, None, None, -60.0
        next_check, last_started, last_finished, generation = 0.0, 0.0, 0.0, self.generation
        while not self.stopping:
            # Clear BEFORE reading state, not after an awaited check: no lost off/on wakeup.
            self.wake.clear()
            cfg = load_config()
            if (self.last_prune is None or time.monotonic() - self.last_prune >= 86400
                    or (not getattr(self, 'pruned_synced', True) and not _boot_clock_unsynced())):
                self.prune()
            v_key = (cfg['tts_url'], cfg['voice'])
            if v_key != prepared_voice:
                voice_ready = False
            can_prep_tts = bool(cfg['tts_url'] or STATE == Path('/var/lib/thinksmart-study'))
            if not voice_ready and (v_key != tried_voice or time.monotonic() - last_voice_try > 60) and can_prep_tts:
                last_voice_try = time.monotonic()
                tried_voice = v_key
                voice_ready = await self.prepare_voice(cfg)
                prepared_voice = v_key if voice_ready else None
                cfg = load_config()  # switch may have changed while synthesis was in progress
                can_prep_tts = bool(cfg['tts_url'] or STATE == Path('/var/lib/thinksmart-study'))
            if generation != self.generation:
                next_check, last_started, last_finished, generation = 0.0, 0.0, 0.0, self.generation

            def _sync_enabled_clock(c):
                if c['enabled'] and self._enabled_mono is not None and not _boot_clock_unsynced():
                    want_at = max(1, int(time.time() - max(0.0, time.monotonic() - self._enabled_mono)))
                    if getattr(self, '_enabled_unsynced', False) or abs(want_at - int(c.get('enabled_at') or 0)) > 30:
                        c['enabled_at'] = want_at
                        save_config(c)
                        self._enabled_unsynced = False

            _sync_enabled_clock(cfg)
            focus_hold = self._focus_holding_study()
            off_at = None if (focus_hold or getattr(self, '_enabled_unsynced', False)) else auto_off_at(cfg)
            if off_at and time.time() >= off_at:
                self.switch(False, 'auto_off')
                continue
            if not cfg['enabled'] or not cfg['vision_url']:
                self.reset_streak()
                next_check, last_started, last_finished = 0.0, 0.0, 0.0
                idle_wait = 15 if _PRE_NTP_PENDING else (60 if not voice_ready and can_prep_tts else 600)
                await self.wait_wake(idle_wait)
                continue
            if last_started > 0.0:
                next_check = min(next_check, max(last_finished + 15.0, last_started + max(15, cfg['interval'])))
            delay = max(0, cfg['enabled_at'] + 10 - time.time(), next_check - time.monotonic())
            remaining = max(0, off_at - time.time()) if off_at else None
            if delay:
                wait_s = min(delay, remaining) if remaining is not None else delay
                await self.wait_wake(min(wait_s, 30.0))
                continue
            if self.busy.locked() or (self.check_task is not None and not self.check_task.done()):
                await self.wait_wake(min(1.0, remaining) if remaining is not None else 1.0)
                continue
            started = time.monotonic()
            check = self.start_check(cfg)
            wake = asyncio.create_task(self.wake.wait())
            try:
                while not check.done():
                    focus_hold = self._focus_holding_study()
                    rem = max(0, off_at - time.time()) if (off_at and not focus_hold) else None
                    step_t = min(rem, 30.0) if rem is not None else 30.0
                    done, _ = await asyncio.wait((check, wake), timeout=step_t, return_when=asyncio.FIRST_COMPLETED)
                    if check in done:
                        break
                    if wake not in done:
                        cur_hold = self._focus_holding_study()
                        if off_at and not cur_hold and time.time() >= off_at:
                            self.switch(False, 'auto_off')
                            check.cancel()
                            break
                        continue
                    self.wake.clear()
                    cur_cfg = load_config()
                    _sync_enabled_clock(cur_cfg)
                    cur_hold = self._focus_holding_study()
                    off_at = None if (cur_hold or getattr(self, '_enabled_unsynced', False)) else auto_off_at(cur_cfg)
                    if (not cur_cfg['enabled'] or not cur_cfg['vision_url']
                            or self.generation != generation
                            or (off_at and time.time() >= off_at)):
                        if off_at and time.time() >= off_at and cur_cfg['enabled']:
                            self.switch(False, 'auto_off')
                        check.cancel()
                        break
                    wake = asyncio.create_task(self.wake.wait())
                try:
                    await check
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as e:
                    self.reset_streak()
                    LOG.warning('check failed: %s', type(e).__name__)
            finally:
                check.cancel()
                wake.cancel()
                await asyncio.gather(check, wake, return_exceptions=True)
            if not check.cancelled():
                last_started = started
                last_finished = time.monotonic()
                next_check = last_finished + max(15, load_config()['interval'] - (last_finished - started))

    # -- round 109: retrying announcements, pomodoro focus timer
    def _announce_retry(self, text, tries=12, gap=10.0, from_duck=False):
        """Speak text once; if XiaoZhi / a call / AirPlay is busy, retry every gap seconds (tries times)."""
        duck_state = [bool(from_duck)]
        allow_sleep = bool(from_duck)
        async def attempt():
            try:
                for i in range(max(1, tries)):
                    if self.stopping:
                        return False
                    use_duck, duck_state[0] = duck_state[0], False
                    ok = await (self.play(text, from_duck=True, allow_sleep=True) if use_duck else (self.play(text, allow_sleep=True) if allow_sleep else self.play(text)))
                    if ok:
                        return True
                    if i + 1 < tries:
                        await asyncio.sleep(gap)
                LOG.info('announcement dropped after %d tries', tries)
                return False
            finally:
                if duck_state[0]:
                    duck_state[0] = False
                    await self.music('unduck')
                def_res = getattr(self, '_deferred_resume', None)
                def_age = time.monotonic() - getattr(self, '_deferred_resume_at', 0.0)
                if def_res is not None and def_age <= 20.0 and not self.stopping and not self.media_busy() and await self.client_idle():
                    self._clear_deferred_resume()
                    after = await self.music()
                    if (isinstance(after, dict) and after.get('paused') is not False and not after.get('playing')
                            and not after.get('ambient') and not after.get('alarm_ringing')
                            and self.music_identity(after) == self.music_identity(def_res) and not self.media_busy()):
                        await self.music('resume')
                elif def_res is not None and def_age > 20.0:
                    self._clear_deferred_resume()
        task = asyncio.create_task(attempt())
        self.announcements.add(task)

        def finished(task):
            self.announcements.discard(task)
            if duck_state[0]:
                duck_state[0] = False
                if not self.stopping:
                    ut = asyncio.create_task(self.music('unduck'))
                    self.announcements.add(ut)
                    ut.add_done_callback(self.announcements.discard)
            if not task.cancelled():
                with contextlib.suppress(Exception):
                    task.result()
        task.add_done_callback(finished)
        return task

    @staticmethod
    def load_focus():
        try:
            raw = json.loads(_read_regular_text(FOCUS_PATH, 65536, private=True))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get('phase') not in ('focus', 'break'):
            return None
        try:
            started, ends = float(raw['started_at']), float(raw['ends_at'])
            minutes, brk = raw['minutes'], raw.get('break_minutes', 5)
        except (KeyError, TypeError, ValueError):
            return None
        if (not (math.isfinite(started) and math.isfinite(ends)) or type(minutes) is not int or not 1 <= minutes <= 180
                or type(brk) is not int or not 0 <= brk <= 60):
            return None
        return {'phase': raw['phase'], 'started_at': started, 'ends_at': ends, 'minutes': minutes,
                'break_minutes': brk, 'with_study': raw.get('with_study') is True,
                'study_auto': raw.get('study_auto') is True, 'from_disk': True}

    @staticmethod
    def save_focus(focus):
        if focus is None:
            with contextlib.suppress(OSError):
                if not FOCUS_PATH.is_symlink():
                    FOCUS_PATH.unlink(missing_ok=True)
            return
        clean_focus = {k: v for k, v in focus.items() if k not in ('from_disk', 'started_mono', 'ends_mono', '_mono_started_at', '_mono_ends_at', 'unsynced')}
        created = False
        tmp = FOCUS_PATH.parent / f'.{FOCUS_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            FOCUS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if FOCUS_PATH.is_symlink():
                raise OSError('Refusing symlink focus path')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(json.dumps(clean_focus))
                f.flush()
                os.fsync(f.fileno())
            if FOCUS_PATH.is_symlink():
                raise OSError('Refusing symlink focus path')
            os.replace(tmp, FOCUS_PATH)
            created = False
        except OSError:
            LOG.warning('could not save the focus timer')
        finally:
            if created:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)

    def focus_public(self):
        f = self.focus
        if not f:
            return {'phase': None}
        cap = (f['minutes'] if f['phase'] == 'focus' else max(1, f['break_minutes'])) * 60
        if self._focus_use_ends_mono(f):
            rem_s = float(f['ends_mono']) - time.monotonic()
            if not _boot_clock_unsynced():
                now_w = time.time()
                want_end = now_w + rem_s
                if abs(want_end - float(f.get('ends_at') or 0)) > 5.0:
                    f['ends_at'] = want_end
                    f['started_at'] = want_end - cap
                    f['_mono_ends_at'] = want_end
                    f['_mono_started_at'] = f['started_at']
                    f['unsynced'] = True
        else:
            rem_s = float(f['ends_at']) - time.time()
        return {'phase': f['phase'], 'minutes': f['minutes'], 'break_minutes': f['break_minutes'],
                'started_at': f['started_at'], 'ends_at': f['ends_at'],
                'remaining': min(cap, max(0, int(math.ceil(rem_s)))),
                'with_study': f['with_study']}

    def focus_start(self, minutes, break_minutes=5, with_study=True):
        prev = self.focus
        study_auto = bool(prev and prev['phase'] == 'focus' and prev.get('study_auto'))
        if prev and prev['phase'] == 'focus':
            elapsed_s = (time.monotonic() - float(prev['started_mono'])) if self._focus_use_started_mono(prev) else (time.time() - float(prev['started_at']))
            done = max(0, int(elapsed_s // 60))
            self.append_log({'event': 'focus_stop', 'minutes': prev['minutes'],
                             'minutes_done': min(done, prev['minutes']), 'by': 'restart'})
        cfg = load_config()
        if with_study and not cfg['enabled']:
            self.switch(True, 'focus', announce=False)
            study_auto = True
        elif not with_study and study_auto and cfg['enabled']:
            self.switch(False, 'focus', announce=False)
            study_auto = False
        self._cancel_focus_announcements()
        now = time.time()
        mono = time.monotonic()
        self.focus = {'phase': 'focus', 'started_at': now, 'ends_at': now + minutes * 60, 'minutes': minutes,
                      'break_minutes': break_minutes, 'with_study': bool(with_study), 'study_auto': study_auto,
                      'started_mono': mono, 'ends_mono': mono + minutes * 60,
                      '_mono_started_at': now, '_mono_ends_at': now + minutes * 60, 'unsynced': _boot_clock_unsynced()}
        self.save_focus(self.focus)
        self.append_log({'event': 'focus_start', 'minutes': minutes})
        LOG.info('focus started: %d min (+%d break, companion %s)', minutes, break_minutes, 'on' if with_study else 'off')
        self._poke_display()
        self.focus_wake.set()
        self._track_focus_ann(self._announce(focus_start_phrase(minutes)))
        return self.focus_public()

    def focus_stop(self, reason='home'):
        self._cancel_focus_announcements()
        f = self.focus
        if not f:
            return self.focus_public()
        if f['phase'] == 'focus':
            elapsed_s = (time.monotonic() - float(f['started_mono'])) if self._focus_use_started_mono(f) else (time.time() - float(f['started_at']))
            done = max(0, int(elapsed_s // 60))
            self.append_log({'event': 'focus_stop', 'minutes': f['minutes'],
                             'minutes_done': min(done, f['minutes']), 'by': reason})
            if f.get('study_auto') and load_config()['enabled']:
                with contextlib.suppress(OSError):
                    self.switch(False, 'focus', announce=False)
        self.focus = None
        self.save_focus(None)
        LOG.info('focus stopped (%s)', reason)
        self.focus_wake.set()
        self.wake.set()
        return self.focus_public()

    async def focus_loop(self):
        while not self.stopping:
            try:
                self.focus_wake.clear()
                f = self.focus
                if not f:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.focus_wake.wait(), 3600)
                    continue
                if f.get('from_disk'):
                    if _boot_clock_unsynced():
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self.focus_wake.wait(), 5.0)
                        continue
                    f.pop('from_disk', None)
                dur_s = (f['minutes'] if f['phase'] == 'focus' else max(1, f['break_minutes'])) * 60
                use_mono = self._focus_use_ends_mono(f)
                if use_mono and not _boot_clock_unsynced():
                    rem_m = float(f['ends_mono']) - time.monotonic()
                    want_end = time.time() + rem_m
                    if f.get('unsynced') or abs(want_end - float(f.get('ends_at') or 0)) > 5.0:
                        f['ends_at'] = want_end
                        f['started_at'] = want_end - dur_s
                        f['_mono_ends_at'] = want_end
                        f['_mono_started_at'] = f['started_at']
                        f.pop('unsynced', None)
                        self.save_focus(f)
                delay = (float(f['ends_mono']) - time.monotonic()) if use_mono else (float(f['ends_at']) - time.time())
                if delay > 0:
                    if f.get('phase') in ('focus', 'break'):
                        self._poke_display()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.focus_wake.wait(), min(delay, 15.0))
                    continue
                fresh = (-delay < FOCUS_STALE) if use_mono else ((time.time() - f['ends_at']) < FOCUS_STALE)
                if fresh:
                    self._poke_display()
                if f['phase'] == 'focus':
                    done_at = (time.time() + max(-FOCUS_STALE, min(0.0, delay))) if use_mono else min(time.time(), f['ends_at'])
                    self.append_log({'event': 'focus_done', 'minutes': f['minutes'], 'at': done_at})
                    if f.get('study_auto') and load_config()['enabled']:
                        with contextlib.suppress(OSError):
                            self.switch(False, 'focus', announce=False)
                    brk = f['break_minutes']
                    if brk and fresh:
                        now = time.time()
                        mono_b = time.monotonic()
                        self.focus = {'phase': 'break', 'started_at': now, 'ends_at': now + brk * 60,
                                      'minutes': f['minutes'], 'break_minutes': brk,
                                      'with_study': f['with_study'], 'study_auto': False,
                                      'started_mono': mono_b, 'ends_mono': mono_b + brk * 60,
                                      '_mono_started_at': now, '_mono_ends_at': now + brk * 60, 'unsynced': _boot_clock_unsynced()}
                    else:
                        self.focus = None
                    self.save_focus(self.focus)
                    self.wake.set()
                    LOG.info('focus done (%d min)', f['minutes'])
                    if fresh:
                        self._track_focus_ann(self._announce_retry(focus_done_phrase(brk)))
                else:
                    self.focus = None
                    self.save_focus(None)
                    if fresh:
                        self._track_focus_ann(self._announce_retry(FOCUS_BREAK_OVER))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception('focus_loop error')
                await asyncio.sleep(2.0)

    def prune(self):
        self.last_prune = time.monotonic()
        self.pruned_synced = not _boot_clock_unsynced()
        synced_ok = self.pruned_synced or LOG_DIR != Path('/var/lib/thinksmart-study/log')
        # synthesized one-off texts (message read-outs) are not kept forever; fixed phrases stay
        self._cap_dynamic_voices(cutoff_ts=(time.time() - VOICE_DYNAMIC_DAYS * 86400) if synced_ok else None)
        if synced_ok:
            cutoff = local_now().date() - dt.timedelta(days=29)
            for f in LOG_DIR.glob('*.jsonl'):
                try:
                    if not stat.S_ISREG(f.lstat().st_mode):
                        continue
                    if dt.date.fromisoformat(f.stem) < cutoff:
                        f.unlink(missing_ok=True)
                        key = str(f)
                        _DAY_CACHE.pop(key, None)
                        _DAY_PUBLIC_CACHE.pop(key, None)
                        _SUMMARY_CACHE.pop(key, None)
                except (OSError, ValueError):
                    LOG.warning('could not prune an old study log')
        if self.pruned_synced:
            now_wall = time.time()
            for base_dir in (STATE, VOICE_DIR):
                with contextlib.suppress(OSError):
                    for f in base_dir.iterdir():
                        if not (f.name.startswith('.') and (f.name.endswith('.tmp') or f.name.endswith('.mp3'))):
                            continue
                        with contextlib.suppress(OSError):
                            st = f.lstat()
                            if stat.S_ISREG(st.st_mode) and (now_wall - st.st_mtime) > 3600:
                                f.unlink(missing_ok=True)

    async def supervise(self):
        while not self.stopping:
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reset_streak()
                LOG.error('study loop failed (%s); retry in 10s', type(exc).__name__)
                await asyncio.sleep(10)


# ---- parent page data -------------------------------------------------------------------
_DAY_CACHE = {}
_DAY_PUBLIC_CACHE = {}
_SUMMARY_CACHE = {}
_STATIC_CACHE = {}
_PUBLIC_KEEP = ('t', 'skip', 'event', 'by', 'present', 'posture', 'too_close', 'activity', 'focused',
                'note', 'issues', 'reminded', 'deferred', 'frames', 'at', 'interval', 'manual',
                'minutes', 'minutes_done')


def read_static(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size > 1048576:
        raise OSError('Not a regular static asset')
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    key = str(path)
    cached = _STATIC_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    sig, data = _read_regular_bytes(path, 1048576)
    _STATIC_CACHE[key] = (sig, data)
    return data


def read_day(date, _sig=None):
    path = LOG_DIR / f'{date}.jsonl'
    key = str(path)
    try:
        if _sig is None:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise OSError('Not a regular log file')
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        else:
            sig = _sig
        cached = _DAY_CACHE.get(key)
        if cached and cached[0] == sig:
            return cached[1]
        lines = _read_regular_text(path, 8 * 1024 * 1024, private=True).splitlines()
    except (OSError, UnicodeDecodeError):
        _DAY_CACHE.pop(key, None)
        _DAY_PUBLIC_CACHE.pop(key, None)
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    res = sorted(out, key=lambda e: str(e.get('t', '')))
    if len(_DAY_CACHE) >= 4 and key not in _DAY_CACHE:
        _DAY_CACHE.pop(next(iter(_DAY_CACHE)))
    _DAY_CACHE[key] = (sig, res)
    return res


def read_day_public(date):
    path = LOG_DIR / f'{date}.jsonl'
    key = str(path)
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise OSError('Not a regular log file')
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        _DAY_CACHE.pop(key, None)
        _DAY_PUBLIC_CACHE.pop(key, None)
        return []
    cached = _DAY_PUBLIC_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    entries = read_day(date, sig)
    if not entries:
        return []
    res = [{k: e[k] for k in _PUBLIC_KEEP if k in e} for e in entries]
    if len(_DAY_PUBLIC_CACHE) >= 4 and key not in _DAY_PUBLIC_CACHE:
        _DAY_PUBLIC_CACHE.pop(next(iter(_DAY_PUBLIC_CACHE)))
    _DAY_PUBLIC_CACHE[key] = (sig, res)
    return res


def summarize(entries, pre_sorted=False):
    s = {'checks': 0, 'present': 0, 'focused': 0, 'issues': {}, 'reminders': {}, 'first': None, 'last': None,
         'present_seconds': 0.0, 'focused_seconds': 0.0, 'focus_sessions': 0, 'focus_minutes': 0}
    previous = None
    enabled = None  # old logs before switch events are still readable
    ordered = entries if pre_sorted else sorted(entries, key=lambda item: str(item.get('t', '')))
    for e in ordered:
        try:
            clock = dt.time.fromisoformat(e['t'])
            second = clock.hour * 3600 + clock.minute * 60 + clock.second
        except (KeyError, TypeError, ValueError):
            continue
        ev = e.get('event')
        if isinstance(ev, str) and ev.startswith('focus_'):
            # pomodoro events (round 109) do not interrupt the check timeline
            if ev == 'focus_done':
                s['focus_sessions'] += 1
                s['focus_minutes'] += _focus_minutes(e.get('minutes'))
            elif ev == 'focus_stop':
                s['focus_minutes'] += _focus_minutes(e.get('minutes_done'))
            continue
        if e.get('event') in ('on', 'off'):
            enabled = e['event'] == 'on'
            previous = second
            continue
        if e.get('manual'):
            continue
        if 'skip' in e:
            previous = second
            continue
        try:
            r = parse_result(e)
        except (TypeError, ValueError):
            previous = second
            continue
        interval = e.get('interval', DEFAULTS['interval'])
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 30 <= interval <= 600:
            interval = DEFAULTS['interval']
        seconds = max(0, min(interval, second - previous if previous is not None else second))
        previous = second
        if enabled is False:
            continue
        s['checks'] += 1
        s['first'] = s['first'] or e['t']
        s['last'] = e['t']
        # Derive labels from validated observations, not arbitrary/missing log issue fields.
        issues = Companion.issues_of(r)
        if r['present']:
            s['present'] += 1
            s['present_seconds'] += seconds
            if not issues & DISTRACTED:
                s['focused'] += 1
                s['focused_seconds'] += seconds
        for key in issues:
            s['issues'][key] = s['issues'].get(key, 0) + 1
        if isinstance(e.get('reminded'), str) and e['reminded'] in ISSUES:
            key = e['reminded']
            s['reminders'][key] = s['reminders'].get(key, 0) + 1
    return s


_EMPTY_SUMMARY = summarize((), pre_sorted=True)


def summarize_day(date):
    path = LOG_DIR / f'{date}.jsonl'
    key = str(path)
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise OSError('Not a regular log file')
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        _SUMMARY_CACHE.pop(key, None)
        return _EMPTY_SUMMARY
    cached = _SUMMARY_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    res = summarize(read_day(date, sig), pre_sorted=True)
    if len(_SUMMARY_CACHE) >= 32 and key not in _SUMMARY_CACHE:
        _SUMMARY_CACHE.pop(next(iter(_SUMMARY_CACHE)))
    _SUMMARY_CACHE[key] = (sig, res)
    return res


def app_for(c):
    def _is_trusted_host(host_hdr) -> bool:
        if not host_hdr or not isinstance(host_hdr, str):
            return False
        raw = host_hdr.strip()
        if not raw or any(c in raw for c in ('/', '\\', '?', '#', '@', ' ', '\t', '\r', '\n')):
            return False
        try:
            sp = urlsplit(f'//{raw}')
            _ = sp.port
            hn = sp.hostname
        except ValueError:
            return False
        if not hn or sp.username is not None or sp.password is not None or sp.path or sp.query or sp.fragment:
            return False
        hn_ip = hn.split('%', 1)[0] if '%' in hn else hn
        try:
            ip = ipaddress.ip_address(hn_ip)
            if getattr(ip, 'ipv4_mapped', None) is not None:
                ip = ip.ipv4_mapped
            return not (ip.is_global or ip.is_multicast or ip.is_unspecified)
        except ValueError:
            pass
        if '%' in hn:
            return False
        hn_clean = hn[:-1].lower() if hn.endswith('.') else hn.lower()
        if not hn_clean or '..' in hn_clean:
            return False
        if hn_clean == 'localhost' or '.' not in hn_clean:
            return True
        return hn_clean.endswith(('.local', '.lan', '.home', '.internal', '.home.arpa'))

    def cors(request, resp):
        origin = request.headers.get('Origin')
        if origin in KIOSK_ORIGINS:
            resp.headers['Access-Control-Allow-Origin'] = origin
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, If-None-Match'
            resp.headers['Access-Control-Expose-Headers'] = 'ETag'
            resp.headers['Vary'] = 'Origin'
        return resp

    def _is_trusted_peer(peer) -> bool:
        if peer is None:
            return True
        try:
            ip = ipaddress.ip_address(str(peer).split('%', 1)[0])
            if getattr(ip, 'ipv4_mapped', None) is not None:
                ip = ip.ipv4_mapped
            return not (ip.is_global or ip.is_multicast or ip.is_unspecified)
        except ValueError:
            return False

    @web.middleware
    async def input_errors(request, handler):
        is_kiosk_api = request.remote in ('127.0.0.1', '::1') and request.path.startswith('/api/study')
        try:
            if not _is_trusted_peer(request.remote) or not _is_trusted_host(request.headers.get('Host')):
                raise web.HTTPForbidden()
            sfs = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
            if sfs == 'cross-site' and not (
                is_kiosk_api and request.headers.get('Origin') in KIOSK_ORIGINS
            ):
                raise web.HTTPForbidden()
            if request.path.startswith('/api/parent/') and sfs not in ('', 'same-origin', 'none'):
                raise web.HTTPForbidden()
            response = await handler(request)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = web.json_response({'ok': False, 'error': 'invalid JSON'}, status=400)
            if is_kiosk_api:
                cors(request, response)
        except OSError:
            response = web.json_response({'ok': False, 'error': 'storage unavailable'}, status=503)
            if is_kiosk_api:
                cors(request, response)
        except web.HTTPException as exc:
            exc.headers.setdefault('Cache-Control', 'no-store')
            if is_kiosk_api and exc.status != 403:
                cors(request, exc)
            raise
        response.headers.setdefault('Cache-Control', 'no-store')
        return response

    app = web.Application(client_max_size=4096, middlewares=[input_errors])

    def local(request):
        if request.remote not in ('127.0.0.1', '::1'):
            raise web.HTTPForbidden()

    def local_json(request, optional=False):
        local(request)
        origin = request.headers.get('Origin')
        if origin is not None and origin not in KIOSK_ORIGINS:
            raise web.HTTPForbidden()
        if request.can_read_body and request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        if not optional and not request.can_read_body:
            raise web.HTTPBadRequest()

    def _etag_of(raw_bytes):
        return f'"{hashlib.blake2b(raw_bytes, digest_size=8).hexdigest()}"'

    def _json_etag_resp(request, payload, sem=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        etag = _etag_of(body if sem is None else json.dumps(sem, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag})
        return web.Response(body=body, content_type='application/json', charset='utf-8', headers={'ETag': etag})

    def _effective_auto_off(cfg):
        off = auto_off_at(cfg)
        f = c.focus
        if off and f and f.get('phase') == 'focus' and f.get('with_study') and type(f.get('ends_at')) in (int, float):
            return max(off, int(math.ceil(f['ends_at'])))
        return off

    def state(cfg):
        return {'enabled': cfg['enabled'], 'since': cfg['enabled_at'] or None,
                'auto_off_at': _effective_auto_off(cfg), 'auto_off_hours': cfg.get('auto_off_hours'),
                'last': c.last_today(), 'today': c.today(), 'focus': c.focus_public()}

    async def status(request):
        local(request)
        return cors(request, _json_etag_resp(request, state(load_config())))

    async def preflight(request):
        local(request)
        return cors(request, web.Response(status=204))

    async def toggle(request):
        local_json(request)
        body = await request.json()
        if not isinstance(body, dict) or type(body.get('enabled')) is not bool:
            return cors(request, web.json_response({'ok': False}, status=400))
        return cors(request, web.json_response(dict(ok=True, **state(c.switch(body['enabled'])))))

    async def config(request):
        local_json(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        cfg = load_config()
        limits = {'interval': (30, 600), 'cooldown': (60, 3600), 'min_gap': (30, 1800),
                  'auto_off_hours': (0, 12), 'burst_frames': (1, 4), 'burst_gap': (1, 8)}
        if not body:
            raise web.HTTPBadRequest()
        for key, value in body.items():
            valid_type = type(value) in (int, float) if key == 'burst_gap' else type(value) is int
            if key not in limits or not valid_type or not math.isfinite(value) or not limits[key][0] <= value <= limits[key][1]:
                raise web.HTTPBadRequest()
            cfg[key] = value
        save_config(cfg)
        c.wake.set()
        return cors(request, web.json_response({'ok': True}))

    async def check_now(request):
        local_json(request, optional=True)
        body = await request.json() if request.can_read_body else {}
        if not isinstance(body, dict) or type(body.get('speak', False)) is not bool:
            raise web.HTTPBadRequest()
        if c.busy.locked() or (c.check_task is not None and not c.check_task.done()):
            raise web.HTTPConflict(text='已有检查正在进行')
        try:
            task = c.start_check(load_config(), allow_speak=body.get('speak', False), manual=True)
        except RuntimeError:
            raise web.HTTPConflict(text='已有检查正在进行')
        try:
            return cors(request, web.json_response(await task))
        except RuntimeError:
            raise web.HTTPConflict(text='已有检查正在进行')
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            if cur is not None and cur.cancelling():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            return cors(request, web.json_response({'ok': False, 'skip': 'disabled'}, status=409))

    async def focus_get(request):
        local(request)
        return cors(request, _json_etag_resp(request, c.focus_public()))

    async def focus_post(request):
        local_json(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        action = body.get('action')
        if action == 'start':
            minutes, brk, with_study = body.get('minutes', 25), body.get('break_minutes', 5), body.get('with_study', True)
            if (type(minutes) is not int or not 1 <= minutes <= 180 or type(brk) is not int or not 0 <= brk <= 60
                    or type(with_study) is not bool):
                raise web.HTTPBadRequest()
            return cors(request, web.json_response(dict(ok=True, **c.focus_start(minutes, brk, with_study))))
        if action == 'stop':
            return cors(request, web.json_response(dict(ok=True, **c.focus_stop())))
        raise web.HTTPBadRequest()

    async def voice_say(request):
        # Other local services only: a browser page always sends Origin on a POST and cannot add
        # the custom header without a CORS preflight, which this route never answers.
        local(request)
        if request.headers.get('Origin') is not None or request.headers.get('X-TS-Internal') != '1':
            raise web.HTTPForbidden()
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get('cancel_tag'), str):
            ctag = body['cancel_tag'].strip()[:40]
            if not ctag:
                raise web.HTTPBadRequest()
            return web.json_response({'ok': True, 'cancelled': c.cancel_tagged_announcements(ctag)})
        text = body.get('text') if isinstance(body, dict) else None
        from_duck = isinstance(body, dict) and body.get('from_duck') is True
        tag = body.get('tag').strip()[:40] if isinstance(body, dict) and isinstance(body.get('tag'), str) else ''
        if not isinstance(text, str):
            if from_duck:
                await c.music('unduck')
            raise web.HTTPBadRequest()
        text = ' '.join(text.split())
        if not text or len(text) > 240:
            if from_duck:
                await c.music('unduck')
            raise web.HTTPBadRequest()
        if tag:
            c.cancel_tagged_announcements(tag)
        active_ann = sum(
            1 for x in c.announcements
            if not x.done() and not (hasattr(x, 'cancelling') and x.cancelling())
        )
        if active_ann >= 4:
            if from_duck:
                await c.music('unduck')
            raise web.HTTPTooManyRequests()
        t = c._announce_retry(text, tries=6, gap=5.0, from_duck=True) if from_duck else c._announce_retry(text, tries=6, gap=5.0)
        if isinstance(t, asyncio.Task) and not t.done():
            c.external_announcements.add(t)
            t.add_done_callback(c.external_announcements.discard)
            if tag:
                tmap = getattr(c, 'tagged_announcements', None)
                if tmap is None:
                    tmap = {}
                    c.tagged_announcements = tmap
                tset = tmap.setdefault(tag, set())
                tset.add(t)
                def _rm_tag(done_t, _tag=tag, _tset=tset):
                    _tset.discard(done_t)
                    if not _tset and tmap.get(_tag) is _tset:
                        tmap.pop(_tag, None)
                t.add_done_callback(_rm_tag)
        return web.json_response({'ok': True, 'queued': True})

    def no_store(resp):
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['Referrer-Policy'] = 'no-referrer'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp

    def parent_state(cfg):
        n, gap = c.burst_of(cfg)
        now = time.time()
        today = local_now(now).date().isoformat()
        return {'enabled': cfg['enabled'], 'since': cfg['enabled_at'] or None, 'auto_off_at': _effective_auto_off(cfg),
                'auto_off_hours': cfg.get('auto_off_hours'), 'interval': cfg['interval'],
                'burst': [n, gap], 'last': c.last_today(today), 'now': int(now),
                'today': today}

    async def parent_page(request):
        try:
            html = read_static(PARENT_PAGE)
        except OSError:
            raise web.HTTPNotFound()
        etag = _etag_of(html)
        if request.headers.get('If-None-Match') == etag:
            resp = no_store(web.Response(status=304, headers={'ETag': etag}))
        else:
            resp = no_store(web.Response(body=html, content_type='text/html', charset='utf-8', headers={'ETag': etag}))
        resp.headers['Cache-Control'] = 'no-cache'
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        return resp

    async def parent_icon(request):
        name = request.match_info['name']
        try:
            data = read_static(PARENT_PAGE.parent / name)
        except OSError:
            raise web.HTTPNotFound()
        resp = web.Response(body=data, content_type=PARENT_ICONS[name])
        resp.headers['Cache-Control'] = 'max-age=86400'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp

    async def parent_get_state(request):
        st = parent_state(load_config())
        return no_store(_json_etag_resp(request, st, {k: v for k, v in st.items() if k != 'now'}))

    day_resp_memo = {}

    async def parent_day(request):
        today = local_now().date()
        date = request.query.get('date') or today.isoformat()
        try:
            parsed = dt.date.fromisoformat(date)
            if not DATE_RE.fullmatch(date) or not today - dt.timedelta(days=29) <= parsed <= today:
                raise ValueError()
        except ValueError:
            raise web.HTTPBadRequest()
        interval = load_config()['interval']
        log_path = LOG_DIR / f'{date}.jsonl'
        try:
            lst = os.lstat(log_path)
            log_sig = (lst.st_ino, lst.st_mtime_ns, lst.st_size) if stat.S_ISREG(lst.st_mode) else None
        except OSError:
            log_sig = None
        mkey = (date, interval, log_sig)
        hit = day_resp_memo.get(mkey)
        if hit is None:
            summary = summarize_day(date)
            entries = read_day_public(date)
            body = json.dumps({'date': date, 'interval': interval, 'summary': summary, 'entries': entries},
                              ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            hit = (_etag_of(body), body)
            if len(body) <= 524288:
                if len(day_resp_memo) >= 4 and mkey not in day_resp_memo:
                    day_resp_memo.pop(next(iter(day_resp_memo)))
                day_resp_memo[mkey] = hit
        etag, body = hit
        if request.headers.get('If-None-Match') == etag:
            return no_store(web.Response(status=304, headers={'ETag': etag}))
        return no_store(web.Response(body=body, content_type='application/json', charset='utf-8', headers={'ETag': etag}))

    async def parent_days(request):
        try:
            n = max(1, min(30, int(request.query.get('n', 14))))
        except ValueError:
            raise web.HTTPBadRequest()
        today = local_now().date()
        days = []
        for i in range(n - 1, -1, -1):
            d = (today - dt.timedelta(days=i)).isoformat()
            days.append(dict(date=d, **summarize_day(d)))
        return no_store(_json_etag_resp(request, {'interval': load_config()['interval'], 'days': days}))

    async def parent_toggle(request):
        # Same-origin only: the custom header + JSON body force a CORS preflight for any other
        # site, and we never answer one, so a foreign page in a browser on the LAN cannot switch it.
        origin = request.headers.get('Origin')
        try:
            parsed = urlsplit(origin) if origin is not None else None
            _ = parsed.port if parsed is not None else None
            req_host = (request.host or '').lower() if parsed is not None else ''
            if req_host:
                _ = urlsplit(f'//{req_host}').port
        except ValueError:
            raise web.HTTPForbidden()
        if (request.headers.get('X-Study-Parent') != '1' or request.content_type != 'application/json'
                or (parsed is not None and (parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != req_host
                    or parsed.path or parsed.query or parsed.fragment or parsed.username is not None))):
            raise web.HTTPForbidden()
        body = await request.json()
        if not isinstance(body, dict) or type(body.get('enabled')) is not bool:
            raise web.HTTPBadRequest()
        return no_store(web.json_response(dict(ok=True, **parent_state(c.switch(body['enabled'], 'parent')))))

    async def lifecycle(_app):
        c.session = aiohttp.ClientSession(trust_env=False)
        c.prune()
        c.focus_task = asyncio.create_task(c.focus_loop())
        task = asyncio.create_task(c.supervise())
        c.runner_task = task
        try:
            yield
        finally:
            task.cancel()
            if c.focus_task is not None:
                c.focus_task.cancel()
            await asyncio.gather(task, *([c.focus_task] if c.focus_task is not None else []), return_exceptions=True)
            await c.shutdown()
            await c.session.close()

    async def _keepalive_304(_req, resp):
        if resp.status == 304:
            resp.headers['Content-Length'] = '0'

    app.on_response_prepare.append(_keepalive_304)
    app.cleanup_ctx.append(lifecycle)
    async def stop_companion(_app):
        await c.shutdown()
    app.on_shutdown.append(stop_companion)
    app.router.add_get('/api/study', status)
    app.router.add_post('/api/study/config', config)
    app.router.add_post('/api/study/toggle', toggle)
    app.router.add_route('OPTIONS', '/api/study', preflight)
    app.router.add_route('OPTIONS', '/api/study/config', preflight)
    app.router.add_route('OPTIONS', '/api/study/toggle', preflight)
    app.router.add_route('OPTIONS', '/api/study/check-now', preflight)
    app.router.add_post('/api/study/check-now', check_now)
    app.router.add_get('/api/study/focus', focus_get)
    app.router.add_post('/api/study/focus', focus_post)
    app.router.add_route('OPTIONS', '/api/study/focus', preflight)
    app.router.add_post('/api/voice/say', voice_say)
    app.router.add_get('/', parent_page)
    app.router.add_get(r'/{name:favicon\.svg|apple-touch-icon\.png}', parent_icon)
    app.router.add_get('/api/parent/state', parent_get_state)
    app.router.add_get('/api/parent/day', parent_day)
    app.router.add_get('/api/parent/days', parent_days)
    app.router.add_post('/api/parent/toggle', parent_toggle)
    return app


if __name__ == '__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s study: %(message)s')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _d in (STATE, LOG_DIR, VOICE_DIR):
        _harden_private_dir(_d)
    if not CONFIG_PATH.exists():
        save_config(dict(DEFAULTS))
    # one app, two listeners: the kiosk API on loopback (its handlers also refuse non-loopback
    # peers), and the parent page on the home LAN
    lan = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lan.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lan.bind(('0.0.0.0', PARENT_PORT))
    try:
        web.run_app(app_for(Companion()), host='127.0.0.1', port=PORT, sock=lan, access_log=None, print=None, shutdown_timeout=5.0)
    finally:
        lan.close()
