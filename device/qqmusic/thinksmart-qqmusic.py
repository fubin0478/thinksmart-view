#!/usr/bin/env python3
"""Standalone QQ Music Touchscreen & Voice Service for Lenovo ThinkSmart View.

Completely independent of /opt/thinksmart/client/ so XiaoZhi OTA updates (thinksmart-update)
never overwrite or conflict with this service.

Features:
- Serves the 1280x800 Touchscreen QQ Music Car/Pad UI on http://0.0.0.0:8091/
- QQ Music Official Search, Toplists (热歌榜/飙升榜/新歌榜/流行指数榜/港台/内地/欧美),
  HD Album Art, and LRC Synchronized Lyrics
- Direct on-device QQ QR Code Login (手机QQ / QQ音乐App 扫码登录) + QQ号歌单一键同步
- USB Flash Drive (/mnt/usb) & Local Music support
- 48 kHz float32 stereo decoding fed through the non-invasive AEC Unix socket bridge
  (/run/user/1000/qqmusic-aec.sock) so "小麦小麦" wake word echo cancellation works
  seamlessly while music is playing (with automatic PipeWire pacat fallback)
- Watches XiaoZhi's SSE stream (http://127.0.0.1:8090/events) externally to auto-duck
  music during voice conversations and auto-end the 8s follow-up window when starting music.
"""
import asyncio
import base64
import concurrent.futures
import contextlib
import html
import http.cookiejar
import io
import ipaddress
import json
import logging
import math
import os
import queue
import random
import re
import secrets
import signal
import socket
import stat
import subprocess
import threading
try:
    threading.stack_size(1048576)
except (RuntimeError, ValueError):
    pass
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aiohttp import web

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
import functools as _functools
import hashlib as _hashlib
from array import array as _array

_np = None
_np_tried = False


def _get_np():
    global _np, _np_tried
    if _np is not None:
        return _np
    if not _np_tried:
        _np_tried = True
        try:
            import numpy as _np_mod
            _np = _np_mod
        except Exception:  # pragma: no cover
            _np = None
    return _np


_COMPACT_DUMPS = _functools.partial(json.dumps, ensure_ascii=False, separators=(',', ':'))
AIRPLAY_AUDIO_FLAG = Path('/run/user/1000/uxplay-audio-active')
AIRPLAY_MIRROR_FLAG = Path('/run/user/1000/uxplay-mirroring-active')
AIRPLAY_YIELD_FLAG = Path('/run/user/1000/qqmusic-yield-airplay')


def _yield_airplay_audio_if_active() -> None:
    with contextlib.suppress(Exception):
        if AIRPLAY_AUDIO_FLAG.exists() and not AIRPLAY_MIRROR_FLAG.exists():
            AIRPLAY_YIELD_FLAG.write_text('1', encoding='utf-8')


def _reap_subproc(p) -> None:
    if p is None:
        return
    with contextlib.suppress(Exception):
        p.kill()
    try:
        p.wait(timeout=1)
    except subprocess.TimeoutExpired:
        threading.Thread(target=p.wait, daemon=True).start()
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format='%(levelname)s qqmusic: %(message)s')
log = logging.getLogger('qqmusic')

OUT_RATE = 48000
CONFIG_PATH = Path('/home/ubuntu/.config/thinksmart-qqmusic/config.json')
SESSION_PATH = Path('/home/ubuntu/.config/thinksmart-qqmusic/session.json')
AMBIENT_DIR = Path('/home/ubuntu/.config/thinksmart-qqmusic/ambient')
UI_HTML_PATH = Path('/usr/local/share/thinksmart-qqmusic/index.html')
AEC_SOCK_PATH = '/run/user/1000/qqmusic-aec.sock'

AMBIENT_PRESETS = {
    'rain': {'id': 'rain', 'name': '窗外细雨', 'icon': '🌧️', 'desc': '真实屋檐落雨与林叶雨声 · 沉浸读写', 'mid': '002hY0WV09MJBV'},
    'waves': {'id': 'waves', 'name': '海岸慢浪', 'icon': '🌊', 'desc': '真实沙滩慢浪轻拍与退潮 · 舒压助眠', 'mid': '002LY8IS2yJwjf'},
    'forest': {'id': 'forest', 'name': '林间微风', 'icon': '🌲', 'desc': '真实山林微风拂叶与远鸟 · 清醒专注', 'mid': '00433gdV1PqMvf'},
    'stream': {'id': 'stream', 'name': '山涧溪流', 'icon': '💧', 'desc': '真实3D山涧清泉潺潺流水 · 屏蔽杂音', 'mid': '001r6u9J29OLyo'},
    'night': {'id': 'night', 'name': '夏夜虫鸣', 'icon': '🌙', 'desc': '真实静夜微风与轻柔虫鸣 · 睡前安神', 'mid': '000Nm2Gk2CQ2Fy'},
    'sleep_piano': {'id': 'sleep_piano', 'name': '雨夜轻琴', 'icon': '🎹', 'desc': '细雨伴温柔舒缓轻钢琴 · 伴读入睡', 'mid': '000KgHAM0GrU8C'},
    'fireplace': {'id': 'fireplace', 'name': '壁炉柴火', 'icon': '🔥', 'desc': '温暖松木噼啪轻燃声 · 围炉夜读', 'mid': '001QsCxP0bXlma'},
    'library': {'id': 'library', 'name': '书房自习', 'icon': '📖', 'desc': '安静书房轻翻书页与落笔 · 沉浸伴学', 'mid': '001f9TaR0ZwJJF'},
    'singing_bowl': {'id': 'singing_bowl', 'name': '禅意颂钵', 'icon': '🎐', 'desc': '空灵颂钵泛音自然余韵 · 冥想安神', 'mid': '001WQ8rf2c4Ord'},
    # 2026-11-12 (Claude): generated on the device (exactly periodic, so the loop has no seam)
    'white_noise': {'id': 'white_noise', 'name': '柔和白噪音', 'icon': '🌫️', 'desc': '均匀柔和的白噪音 · 盖住杂音', 'mid': ''},
    'pink_noise': {'id': 'pink_noise', 'name': '粉红噪音', 'icon': '🌸', 'desc': '比白噪音更柔和 · 安稳入睡', 'mid': ''},
    'brown_noise': {'id': 'brown_noise', 'name': '低沉褐噪音', 'icon': '🌰', 'desc': '像远处瀑布的低沉声 · 深度放松', 'mid': ''},
}

AMBIENT_RECIPES = {
    'deep_study': {'id': 'deep_study', 'name': '深度自习', 'icon': '📚', 'preset': 'rain', 'layer': 'library', 'layer_gain': 0.45, 'desc': '窗外细雨 + 书房自习 45%'},
    'cozy_read': {'id': 'cozy_read', 'name': '围炉夜读', 'icon': '🛋️', 'preset': 'fireplace', 'layer': 'sleep_piano', 'layer_gain': 0.45, 'desc': '壁炉柴火 + 雨夜轻琴 45%'},
    'deep_sleep': {'id': 'deep_sleep', 'name': '深度入眠', 'icon': '🌙', 'preset': 'waves', 'layer': 'singing_bowl', 'layer_gain': 0.35, 'desc': '海岸慢浪 + 禅意颂钵 35%'},
}

# ---- 2026-11-12 (Claude): sleep sounds and sleep sessions ----------------------------------------------------------
# The loops are FLAC: ffmpeg -stream_loop repeats FLAC sample-exactly, the old 60 s AAC loops left a ~19 ms gap every
# lap. Each is a 2-5 minute piece of the full QQ track with a crossfaded seam (the piano piece whole), all at -20 LUFS.
AMBIENT_EXTS = ('.flac', '.m4a')
SLEEP_STATE_PATH = Path('/home/ubuntu/.config/thinksmart-qqmusic/sleep.json')
BACKLIGHT_PATH = Path('/sys/class/backlight/backlight/brightness')
SLEEP_BLANK = 10                # the idle blanker counts a backlight at or below 10 as a blanked screen
SLEEP_MORNING = (6, 30)         # 付总 11-12: the night is 00:00-06:30 every day. All-night sessions end here, and the
SLEEP_DAY_END = (0, 0)          # day volume comes back here for a session that ended in the night (else right away)
SLEEP_EVENING = (20, 0)         # from here to the morning the night view may turn the screen off without a session
NIGHT_VIEW_PATH = Path('/run/user/1000/thinksmart-night-view')   # the night view is up: the blanker keeps it dim
SUNRISE_PATH = Path('/run/user/1000/thinksmart-sunrise.json')    # 温柔叫醒: the blanker brightens the screen slowly
CALL_FLAG_PATH = Path('/run/user/1000/intercom-call-active')
MIRROR_FLAG_PATH = Path('/run/user/1000/uxplay-mirroring-active')
SLEEP_WAKE_SOUND = 'forest'     # 林间微风: wind in the leaves, birds far away
SLEEP_WAKE_TIME = (6, 30)       # 温柔叫醒: the default time; the user sets it on the sleep page (04:30-10:00)
SLEEP_WAKE_VOLUME = (20, 50)    # 付总: the volume goes from 20% up to 50% over SLEEP_WAKE_RAMP ...
SLEEP_WAKE_RAMP = 600           # ... 10 minutes ...
SLEEP_WAKE_HOLD = 300           # ... stays at 50% for 5 more ...
SLEEP_WAKE_FADE = 60            # ... and fades out over a minute (2 s after a touch)
SLEEP_WAKE_SCREEN_MAX = 50      # 付总: the screen brightens from its own level up to 50% (the brightness panel's %)
WEEKDAY_NAMES = '一二三四五六日'
try:   # the calendar's holidays (the home page uses the same library): no 温柔叫醒 on a day off
    import sys as _sys
    _vendor_dir = str(Path(__file__).resolve().parent.parent / 'vendor')
    for _vp in (_vendor_dir, '/opt/thinksmart-addons/vendor'):
        if _vp not in _sys.path:
            _sys.path.append(_vp)
    from lunar_python.util import HolidayUtil as _HolidayUtil
except Exception:   # noqa: BLE001 - without it, only weekends count as days off
    _HolidayUtil = None
SLEEP_DEFAULT_VOLUME = 25
SLEEP_DEFAULT_MINUTES = 45
SLEEP_DEFAULT_FADE = 600        # a sleep session fades out over its last 10 minutes
SLEEP_NIGHT_SETTLE = 1800       # all night: after 30 minutes the sound goes down ...
SLEEP_NIGHT_RAMP = 600          # ... over 10 minutes ...
SLEEP_NIGHT_DB = -10.0          # ... by this much, until the morning
SLEEP_PLAYLISTS = (             # checked 11-12: most of their songs play with this account (paid-only songs are skipped)
    {'id': '8401506970', 'name': '助眠钢琴', 'desc': '经典助眠钢琴曲 · 安静沉入梦'},
    {'id': '7275679373', 'name': '睡前轻音乐', 'desc': '舒缓纯音乐 · 放松入睡'},
    {'id': '7333764360', 'name': '钢琴摇篮曲', 'desc': '森林童谣钢琴版 · 哄孩子睡'},
)
SLEEP_COMBOS = (               # 11-12 (付总): every sleep sound's matching second layer: main, layer, share, name
    ('rain', 'fireplace', 0.40, '雨夜壁炉'),
    ('waves', 'singing_bowl', 0.35, '深度入眠'),
    ('forest', 'stream', 0.35, '林间溪涧'),
    ('stream', 'sleep_piano', 0.30, '溪边琴音'),
    ('night', 'waves', 0.35, '海边夏夜'),
    ('fireplace', 'rain', 0.45, '炉边听雨'),
    ('white_noise', 'rain', 0.30, '白噪细雨'),
    ('pink_noise', 'waves', 0.35, '粉噪海浪'),
    ('brown_noise', 'rain', 0.40, '褐噪细雨'),
    ('singing_bowl', 'stream', 0.35, '溪畔冥想'),
    ('sleep_piano', 'fireplace', 0.30, '围炉轻琴'),
)  # the home page has the same list (SP_PAIR)
SLEEP_SOUND_NAMES = {           # what people call them (voice / last choice) -> ambient preset or recipe
    '雨声': 'rain', '下雨': 'rain', '下雨声': 'rain', '细雨': 'rain', '窗外细雨': 'rain',
    '海浪': 'waves', '海浪声': 'waves', '大海': 'waves', '海岸慢浪': 'waves', '潮汐': 'waves',
    '森林': 'forest', '微风': 'forest', '风声': 'forest', '林间微风': 'forest', '鸟鸣': 'forest',
    '溪流': 'stream', '流水': 'stream', '溪水': 'stream', '山涧溪流': 'stream', '流水声': 'stream',
    '虫鸣': 'night', '夏夜虫鸣': 'night', '虫鸣声': 'night',
    '轻琴': 'sleep_piano', '雨夜轻琴': 'sleep_piano',
    '壁炉': 'fireplace', '柴火': 'fireplace', '壁炉柴火': 'fireplace', '篝火': 'fireplace',
    '书房': 'library', '翻书': 'library', '书房自习': 'library',
    '颂钵': 'singing_bowl', '禅意颂钵': 'singing_bowl', '冥想': 'singing_bowl',
    '白噪音': 'white_noise', '柔和白噪音': 'white_noise', '粉红噪音': 'pink_noise', '粉噪音': 'pink_noise',
    '褐噪音': 'brown_noise', '褐噪': 'brown_noise', '棕噪音': 'brown_noise', '低沉褐噪音': 'brown_noise',
    '深度入眠': 'deep_sleep', '深度助眠': 'deep_sleep', '围炉夜读': 'cozy_read', '深度自习': 'deep_study',
}


def _next_sleep_restore_at(now=None) -> float:
    """When the day volume comes back: in a few seconds in the day, at the next 06:30 at night (00:00-06:30)."""
    now = time.time() if now is None else now
    if not _is_sleep_night(now):
        return now + 3.0
    lt = time.localtime(now)
    h, mi = SLEEP_MORNING
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
    if target <= now:
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, h, mi, 0, 0, 0, -1))
    return max(now + 3.0, target)


def _seconds_until(hhmm, now=None):
    """Seconds from now to the next HH:MM (today, or tomorrow when it is less than a minute away); None if invalid."""
    m = re.match(r'^\s*(\d{1,2})\s*[:：点]\s*(\d{1,2})?\s*(?:分)?\s*$', str(hhmm or ''))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    now = time.time() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
    if target - now < 60:
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, h, mi, 0, 0, 0, -1))
    return target - now


def _in_hm_window(hm, start, end) -> bool:
    return start <= hm < end if start <= end else (hm >= start or hm < end)


def _is_sleep_night(now=None) -> bool:
    lt = time.localtime(time.time() if now is None else now)
    return _in_hm_window((lt.tm_hour, lt.tm_min), SLEEP_DAY_END, SLEEP_MORNING)


def _is_sleep_evening(now=None) -> bool:
    lt = time.localtime(time.time() if now is None else now)
    return _in_hm_window((lt.tm_hour, lt.tm_min), SLEEP_EVENING, SLEEP_MORNING)


_HOLIDAYS_EXTRA_PATH = Path(os.environ.get('HOLIDAYS_EXTRA_PATH', '/var/lib/thinksmart-home/holidays_extra.json'))
_HOLIDAYS_EXTRA_CACHE = {'sig': 'unset', 'map': {}}


def _load_holidays_extra(path=None) -> dict:
    p = Path(_HOLIDAYS_EXTRA_PATH if path is None else path)
    try:
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
            return {}
        sig = (str(p), st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        _HOLIDAYS_EXTRA_CACHE['sig'] = None
        _HOLIDAYS_EXTRA_CACHE['map'] = {}
        return {}
    if _HOLIDAYS_EXTRA_CACHE.get('sig') != sig:
        parsed = {}
        try:
            raw = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                for d_s in (*(raw.get('workdays') or ()), *(raw.get('work') or ())):
                    if isinstance(d_s, str):
                        parsed[d_s.strip()] = True
                for d_s in (*(raw.get('holidays') or ()), *(raw.get('off') or ())):
                    if isinstance(d_s, str):
                        parsed[d_s.strip()] = False
                for k, v in raw.items():
                    if isinstance(k, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', k) and isinstance(v, bool):
                        parsed[k] = v
        except Exception:
            parsed = {}
        _HOLIDAYS_EXTRA_CACHE['sig'] = sig
        _HOLIDAYS_EXTRA_CACHE['map'] = parsed
    return _HOLIDAYS_EXTRA_CACHE['map']


def _wake_day_info(y, m, d):
    """(day off?, why) by the calendar: holidays_extra.json first, then statutory HolidayUtil, then weekends."""
    import datetime as _dt
    iso = f'{y:04d}-{m:02d}-{d:02d}'
    extra = _load_holidays_extra()
    if iso in extra:
        is_work = bool(extra[iso])
        return (not is_work), ('调休上班' if is_work else '节假日休假')
    wd = _dt.date(y, m, d).isoweekday()
    if _HolidayUtil is not None:
        try:
            h = _HolidayUtil.getHoliday(y, m, d)
        except Exception:   # noqa: BLE001
            h = None
        if h is not None:
            return (not h.isWork()), (f'{h.getName()}调休上班' if h.isWork() else f'{h.getName()}假期')
    return wd >= 6, ('周末' if wd >= 6 else '')


def _wake_hm(st) -> tuple:
    m = re.match(r'^(\d{1,2}):(\d{2})$', str((st or {}).get('wake_time') or ''))
    if m and (4, 30) <= (int(m.group(1)), int(m.group(2))) <= (10, 0) and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2))
    return SLEEP_WAKE_TIME


def _wake_next(st, now=None) -> dict:
    """The next 温柔叫醒 morning: today before its time, else tomorrow; whether it is a day off."""
    now = time.time() if now is None else now
    h, mi = _wake_hm(st)
    lt = time.localtime(now)
    at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
    later = now >= at
    if later:
        lt = time.localtime(at + 86400 + 3600)   # tomorrow (an hour of slack for clock changes)
    off, why = _wake_day_info(lt.tm_year, lt.tm_mon, lt.tm_mday)
    return {'date': '%04d-%02d-%02d' % (lt.tm_year, lt.tm_mon, lt.tm_mday), 'when': '明天' if later else '今天',
            'weekday': '周' + WEEKDAY_NAMES[lt.tm_wday], 'off': off, 'why': why, 'time': '%d:%02d' % (h, mi)}


def _sunrise_mark_stopped():
    """Tell the screen owner the wake-up is over (it goes back to its normal brightness)."""
    try:
        data = json.loads(_read_regular_text(SUNRISE_PATH, 4096))
        if isinstance(data, dict) and 'stopped' not in data:
            data['stopped'] = int(time.time())
            _write_runtime_json(SUNRISE_PATH, data)
    except (OSError, ValueError):
        pass


def _write_runtime_json(path: Path, data: dict):
    if path.is_symlink():
        raise OSError(f'Refusing symlink runtime path: {path}')
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o644)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o644)
            json.dump(data, f)
        if path.is_symlink():
            raise OSError(f'Refusing symlink runtime path: {path}')
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_sleep_state() -> dict:
    try:
        d = json.loads(_read_regular_text(SLEEP_STATE_PATH, 65536, private=True))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_SLEEP_STATE_LOCK = threading.Lock()


def _write_sleep_state(d: dict):
    with _SLEEP_STATE_LOCK:
        tmp = SLEEP_STATE_PATH.with_name(f'.sleep.json.{os.getpid()}.{threading.get_ident()}.tmp')
        created = False
        try:
            SLEEP_STATE_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if SLEEP_STATE_PATH.is_symlink():
                raise OSError('Refusing symlink sleep state path')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(d, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            if SLEEP_STATE_PATH.is_symlink():
                raise OSError('Refusing symlink sleep state path')
            os.replace(tmp, SLEEP_STATE_PATH)
            created = False
        except OSError as e:
            log.warning('sleep state not saved: %s', e)
        finally:
            if created:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)


def _clean_sleep_sound(raw):
    """{'kind': 'ambient', 'preset', 'layer', 'layer_gain'} | {'kind': 'playlist', 'id'} | {'kind': 'current'} | None."""
    if isinstance(raw, str):
        raw = {'name': raw}
    if not isinstance(raw, dict):
        return None
    kind = raw.get('kind')
    if kind == 'current':
        return {'kind': 'current'}
    if kind == 'playlist' or (kind is None and raw.get('id')):
        pid = str(raw.get('id') or '')
        return {'kind': 'playlist', 'id': pid} if any(p['id'] == pid for p in SLEEP_PLAYLISTS) else None
    name = str(raw.get('preset') or raw.get('name') or '').strip().lower()
    for suf in ('的声音', '声音', '音乐', '白噪音模式'):
        if name.endswith(suf) and name[:-len(suf)] in SLEEP_SOUND_NAMES:
            name = name[:-len(suf)]
    for m, l, g, n in SLEEP_COMBOS:
        if name == n:
            return {'kind': 'ambient', 'preset': m, 'layer': l, 'layer_gain': g}
    for p in SLEEP_PLAYLISTS:
        if name and (name == p['name'] or name == p['id'] or (len(name) >= 2 and name in p['name'])):
            return {'kind': 'playlist', 'id': p['id']}
    if name in ('钢琴', '助眠音乐', '睡眠音乐', '催眠曲', '轻音乐', '纯音乐'):
        return {'kind': 'playlist', 'id': SLEEP_PLAYLISTS[0]['id'] if name in ('钢琴', '助眠音乐', '睡眠音乐', '催眠曲') else SLEEP_PLAYLISTS[1]['id']}
    if name in ('摇篮曲', '儿歌', '宝宝'):
        return {'kind': 'playlist', 'id': SLEEP_PLAYLISTS[2]['id']}
    preset = SLEEP_SOUND_NAMES.get(name, name)
    if preset in AMBIENT_RECIPES:
        return {'kind': 'ambient', 'preset': preset, 'layer': '', 'layer_gain': None}
    if preset not in AMBIENT_PRESETS:
        return None
    lay = str(raw.get('layer') or '').strip().lower()
    lay = SLEEP_SOUND_NAMES.get(lay, lay)
    lay = lay if (lay in AMBIENT_PRESETS and lay != preset) else ''
    lg = raw.get('layer_gain')
    lg = round(max(0.1, min(1.0, float(lg))), 2) if (lay and type(lg) in (int, float) and math.isfinite(lg)) else None
    return {'kind': 'ambient', 'preset': preset, 'layer': lay, 'layer_gain': lg}


def _sleep_sound_label(snd) -> str:
    if not snd:
        return ''
    if snd['kind'] == 'playlist':
        return next((p['name'] for p in SLEEP_PLAYLISTS if p['id'] == snd['id']), '助眠音乐')
    if snd['kind'] == 'current':
        return '正在播放的'
    for m, l, _g, n in SLEEP_COMBOS:
        if snd.get('preset') == m and snd.get('layer') == l:
            return n
    p = snd.get('preset')
    if p in AMBIENT_RECIPES:
        return AMBIENT_RECIPES[p]['name']
    name = AMBIENT_PRESETS.get(p, {}).get('name', '')
    lay = snd.get('layer')
    return f'{name} + {AMBIENT_PRESETS[lay]["name"]}' if (lay and lay in AMBIENT_PRESETS) else name


def _qq_playable_tracks(tracks: list) -> list:
    """Songs this account can play (one batched vkey request per 60 songs); on any error the list is kept as is."""
    out = []
    anon_hdrs = {'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Origin': 'https://y.qq.com', 'Content-Type': 'application/json'}
    anon_comm = {'ct': 24, 'cv': 0, 'uin': '0', 'format': 'json'}
    try:
        cfg = load_config()
        has_ck = bool((cfg.get('cookies_str') or cfg.get('qm_keyst') or '').strip())
        refreshed = False
        for i in range(0, min(len(tracks), 120), 60):
            part = [t for t in tracks[i:i + 60] if t.get('mid')]
            if not part:
                continue
            mids = [t['mid'] for t in part]
            fns = [f"C400{t.get('media_mid') or t['mid']}.m4a" for t in part]
            stypes = [0] * len(part)

            def _batch_vkey(use_anon: bool):
                u = '0' if use_anon else str(load_config().get('uin') or '10000')
                c = anon_comm if use_anon else _qq_comm()
                h = anon_hdrs if use_anon else _qq_headers()
                pl = {'comm': c, 'req_0': {'module': 'vkey.GetVkeyServer', 'method': 'CgiGetVkey', 'param': {
                    'guid': '10000', 'songmid': mids, 'filename': fns,
                    'songtype': stypes, 'uin': u, 'loginflag': 1, 'platform': '20'}}}
                rq = urllib.request.Request('https://u.y.qq.com/cgi-bin/musicu.fcg', data=json.dumps(pl).encode('utf-8'), headers=h)
                with _safe_urlopen(rq, timeout=6) as r:
                    return json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))

            d = _batch_vkey(False)
            ok = {x.get('songmid') for x in (d.get('req_0', {}).get('data', {}).get('midurlinfo') or []) if x.get('purl')}
            auth_err = d.get('code') in (1000, 2001) or (d.get('req_0') or {}).get('code') in (1000, 2001)
            if has_ck and not refreshed and (auth_err or not ok) and _refresh_musickey_from_cookies():
                refreshed = True
                d = _batch_vkey(False)
                ok = {x.get('songmid') for x in (d.get('req_0', {}).get('data', {}).get('midurlinfo') or []) if x.get('purl')}
                auth_err = d.get('code') in (1000, 2001) or (d.get('req_0') or {}).get('code') in (1000, 2001)
            if has_ck and (auth_err or not ok):
                d2 = _batch_vkey(True)
                ok2 = {x.get('songmid') for x in (d2.get('req_0', {}).get('data', {}).get('midurlinfo') or []) if x.get('purl')}
                if ok2:
                    ok = ok2
            out.extend(t for t in part if t['mid'] in ok)
    except Exception as e:
        log.warning('sleep playlist check failed (%s); playing it unchecked', type(e).__name__)
        return tracks
    return out

USB_MNT_DIR = Path('/mnt/usb')
LOCAL_DIRS = [
    USB_MNT_DIR,
    Path('/home/ubuntu/Music'),
    Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/music'),
]
AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.wma'}

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

TOPLISTS = [
    {'id': 26, 'name': '🔥 热歌榜', 'desc': 'QQ音乐全网最热流行单曲'},
    {'id': 62, 'name': '🚀 飙升榜', 'desc': '热度急速上升的热门新曲'},
    {'id': 27, 'name': '🆕 新歌榜', 'desc': '最新发行的华语及全球新歌'},
    {'id': 4,  'name': '💎 流行指数榜', 'desc': '潮流风向标热门金曲'},
    {'id': 5,  'name': '🇨🇳 内地榜', 'desc': '内地最受欢迎流行歌曲'},
    {'id': 59, 'name': '🇭🇰 香港地区榜', 'desc': '经典与最新粤语金曲'},
    {'id': 61, 'name': '🇹🇼 台湾地区榜', 'desc': '华语流行经典热歌'},
    {'id': 3,  'name': '🌍 欧美榜', 'desc': '欧美流行热单精选'},
    {'id': 16, 'name': '🇰🇷 韩国榜', 'desc': 'K-Pop 潮流韩语热歌榜'},
    {'id': 17, 'name': '🇯🇵 日本榜', 'desc': 'J-Pop 与动漫流行金曲'},
]

_SESSION_LOCK = threading.Lock()
_CFG_LOCK = threading.RLock()
_CFG_CACHE = None
_CFG_SIG = None
_REFRESH_LOCK = threading.Lock()
_LAST_REFRESH_FAIL = (None, 0.0)
_LAST_REFRESH_OK = (None, 0.0)
_DEAD_PSKEY = None
_QR_LOCK = threading.Lock()
_QR_SESSIONS = {}
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_RING_VOL_PATH = Path('/home/ubuntu/.local/share/thinksmart-client/alarms/ring.json')
_MAX_HTTP_BYTES = 2 * 1024 * 1024


def _read_regular_text(path: Path, max_bytes: int = 2 * 1024 * 1024, private: bool = False) -> str:
    if private:
        with contextlib.suppress(OSError):
            pst = os.lstat(path.parent)
            # 带粘滞位的是公用目录（/tmp），不收紧：以 root 跑测试时曾把 /tmp 收成 0700（2026-11-25 Claude）
            if stat.S_ISDIR(pst.st_mode) and not pst.st_mode & stat.S_ISVTX and (pst.st_mode & 0o077) != 0 and pst.st_uid == os.getuid():
                os.chmod(path.parent, 0o700)
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
        return raw.decode('utf-8')
    finally:
        os.close(fd)


def _read_http_bytes(resp, max_bytes: int = _MAX_HTTP_BYTES) -> bytes:
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('HTTP response exceeds maximum size')
    return data


_BLOCKED_HOST_SUFFIXES = (
    '.localhost', '.local', '.internal', '.lan', '.home.arpa',
    '.nip.io', '.sslip.io', '.xip.io', '.localtest.me', '.lvh.me',
)


def _is_safe_stream_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not u or len(u) > 4096 or any(ord(c) <= 0x20 or ord(c) == 0x7f for c in u):
        return False
    try:
        sp = urllib.parse.urlsplit(u)
        _ = sp.port
    except ValueError:
        return False
    if sp.scheme not in ('http', 'https') or sp.username is not None or sp.password is not None:
        return False
    host = (sp.hostname or '').strip().lower().rstrip('.')
    if not host or host in ('localhost', 'ip6-localhost', 'ip6-loopback') or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return '.' in host
    if getattr(ip, 'ipv4_mapped', None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def _has_only_global_ips(host: str) -> bool:
    h = (host or '').strip().lower().rstrip('.')
    if not h or h in ('localhost', 'ip6-localhost', 'ip6-loopback') or h.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(h)
        if getattr(ip, 'ipv4_mapped', None) is not None:
            ip = ip.ipv4_mapped
        return ip.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = (info[4][0] or '').split('%', 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if getattr(ip, 'ipv4_mapped', None) is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    return True


def _cookie_root(hn: str) -> str:
    h = (hn or '').strip().lower().rstrip('.')
    for root in ('qq.com', 'gtimg.cn', 'bilibili.com'):
        if h == root or h.endswith('.' + root):
            return root
    return h


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        abs_url = urllib.parse.urljoin(req.full_url, newurl)
        if not _is_safe_stream_url(abs_url):
            if fp is not None:
                with contextlib.suppress(Exception):
                    fp.close()
            raise urllib.error.URLError('Blocked unsafe redirect URL')
        old_sp = urllib.parse.urlsplit(req.full_url)
        new_sp = urllib.parse.urlsplit(abs_url)
        if old_sp.scheme == 'https' and new_sp.scheme != 'https':
            if fp is not None:
                with contextlib.suppress(Exception):
                    fp.close()
            raise urllib.error.URLError('Blocked HTTPS to HTTP redirect downgrade')
        if not _has_only_global_ips(new_sp.hostname or ''):
            if fp is not None:
                with contextlib.suppress(Exception):
                    fp.close()
            raise urllib.error.URLError('Blocked redirect to non-global IP')
        if fp is not None:
            body = fp.read(65537)
            if len(body) > 65536:
                with contextlib.suppress(Exception):
                    fp.close()
                raise urllib.error.URLError('Redirect response body too large')
        new_req = super().redirect_request(req, fp, code, msg, headers, abs_url)
        if new_req is not None and _cookie_root(old_sp.hostname) != _cookie_root(new_sp.hostname):
            for hdr_d in (new_req.headers, new_req.unredirected_hdrs):
                for hk in [k for k in list(hdr_d.keys()) if k.lower() in ('cookie', 'authorization')]:
                    hdr_d.pop(hk, None)
        return new_req



_LAST_TRIM_MONO = 0.0


def _trim_idle_heap() -> bool:
    global _LAST_TRIM_MONO
    now_m = time.monotonic()
    if now_m - _LAST_TRIM_MONO < 30.0:
        return False
    _LAST_TRIM_MONO = now_m
    try:
        import ctypes
        import gc
        gc.collect()
        ctypes.CDLL('libc.so.6').malloc_trim(0)
        return True
    except Exception:
        return False


_ORIG_URLOPEN = urllib.request.urlopen
_SAFE_OPENER = None
_SAFE_OPENER_SSL_CTX = None
_SAFE_OPENER_LOCK = threading.Lock()
_HTTP_CONN_POOL = {}
_HTTP_POOL_LOCK = threading.Lock()


class _PooledHttpResp:
    def __init__(self, body: bytes, status: int, headers, final_url: str):
        self._buf = io.BytesIO(body)
        self.status = status
        self.code = status
        self.headers = headers
        self._url = final_url

    def read(self, amt: int = -1) -> bytes:
        return self._buf.read() if (amt is None or amt < 0) else self._buf.read(amt)

    def info(self):
        return self.headers

    def geturl(self) -> str:
        return self._url

    def close(self):
        self._buf.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _get_safe_opener():
    global _SAFE_OPENER, _SAFE_OPENER_SSL_CTX
    if _SAFE_OPENER is not None:
        return _SAFE_OPENER
    with _SAFE_OPENER_LOCK:
        if _SAFE_OPENER is None:
            import ssl as _ssl_mod
            _SAFE_OPENER_SSL_CTX = _ssl_mod.create_default_context()
            _SAFE_OPENER = urllib.request.build_opener(
                _SafeRedirectHandler,
                urllib.request.HTTPSHandler(context=_SAFE_OPENER_SSL_CTX),
            )
        return _SAFE_OPENER


def _pooled_safe_urlopen(req, timeout: float = 6):
    import http.client as _hc
    _get_safe_opener()
    if isinstance(req, str):
        cur_url = req
        method = 'GET'
        body_data = None
        base_hdrs = {'User-Agent': UA}
    else:
        cur_url = req.full_url
        method = req.get_method()
        body_data = req.data
        base_hdrs = {k: v for k, v in req.header_items()}
        base_hdrs.setdefault('User-Agent', UA)
    for _hop in range(6):
        sp = urllib.parse.urlsplit(cur_url)
        scheme = (sp.scheme or '').lower()
        host = (sp.hostname or '').strip()
        if scheme not in ('http', 'https') or not host or sp.username is not None or sp.password is not None:
            raise urllib.error.URLError(f'Refusing unsafe URL: {cur_url!r}')
        if not _has_only_global_ips(host):
            raise urllib.error.URLError(f'Refusing non-global host: {host!r}')
        port = sp.port or (443 if scheme == 'https' else 80)
        path_q = (sp.path or '/') + (f'?{sp.query}' if sp.query else '')
        pool_key = (scheme, host.lower(), port)
        req_hdrs = dict(base_hdrs)
        req_hdrs.setdefault('Connection', 'keep-alive')
        if body_data is not None and not any(k.lower() == 'content-length' for k in req_hdrs):
            req_hdrs['Content-Length'] = str(len(body_data))
        raw_resp = None
        conn = None
        for attempt in range(2):
            now_m = time.monotonic()
            conn = None
            if attempt == 0:
                with _HTTP_POOL_LOCK:
                    bucket = _HTTP_CONN_POOL.get(pool_key)
                    while bucket:
                        cand_c, cand_ts = bucket.pop()
                        if (now_m - cand_ts) < 45.0 and getattr(cand_c, 'sock', None) is not None:
                            conn = cand_c
                            break
                        with contextlib.suppress(Exception):
                            cand_c.close()
            fresh_conn = conn is None
            if fresh_conn:
                conn = (
                    _hc.HTTPSConnection(host, port, timeout=timeout, context=_SAFE_OPENER_SSL_CTX)
                    if scheme == 'https'
                    else _hc.HTTPConnection(host, port, timeout=timeout)
                )
            elif getattr(conn, 'sock', None) is not None:
                with contextlib.suppress(Exception):
                    conn.sock.settimeout(timeout)
            try:
                conn.request(method, path_q, body=body_data, headers=req_hdrs)
                r = conn.getresponse()
                chunks, total = [], 0
                max_b = 4 * 1024 * 1024
                while True:
                    part = r.read(65536)
                    if not part:
                        break
                    total += len(part)
                    if total > max_b:
                        conn.close()
                        raise urllib.error.URLError('Response exceeds safety cap')
                    chunks.append(part)
                raw_resp = (r.status, r.reason, r.headers, b''.join(chunks), r.will_close)
                if not r.will_close:
                    with _HTTP_POOL_LOCK:
                        bkt = _HTTP_CONN_POOL.setdefault(pool_key, [])
                        if len(bkt) >= 4:
                            old_c, _ = bkt.pop(0)
                            with contextlib.suppress(Exception):
                                old_c.close()
                        bkt.append((conn, time.monotonic()))
                else:
                    conn.close()
                break
            except (_hc.RemoteDisconnected, _hc.BadStatusLine, BrokenPipeError, ConnectionResetError, OSError):
                with contextlib.suppress(Exception):
                    conn.close()
                if fresh_conn or attempt == 1:
                    raise
        status, reason, resp_hdrs, resp_bytes, _ = raw_resp
        if status in (301, 302, 303, 307, 308):
            loc = resp_hdrs.get('Location')
            if loc:
                cur_url = urllib.parse.urljoin(cur_url, loc)
                if status in (301, 302, 303) and method == 'POST':
                    method = 'GET'
                    body_data = None
                    base_hdrs = {k: v for k, v in base_hdrs.items() if k.lower() not in ('content-type', 'content-length')}
                continue
        if status >= 400:
            raise urllib.error.HTTPError(cur_url, status, reason, resp_hdrs, io.BytesIO(resp_bytes))
        return _PooledHttpResp(resp_bytes, status, resp_hdrs, cur_url)
    raise urllib.error.URLError('Too many redirects')


def _safe_urlopen(req, timeout: float = 6):
    try:
        if urllib.request.urlopen is not _ORIG_URLOPEN:
            return urllib.request.urlopen(req, timeout=timeout)
        return _pooled_safe_urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as he:
        with contextlib.suppress(Exception):
            he.close()
        raise


_ORIG_SAFE_URLOPEN = _safe_urlopen


_SAFE_FFMPEG_URL_CACHE = {}
_SAFE_FFMPEG_URL_LOCK = threading.Lock()


def _resolve_safe_ffmpeg_url(url: str, hdrs: dict = None, is_live: bool = False) -> str:
    cur = (url or '').strip()
    orig_cur = cur
    if not _is_safe_stream_url(cur):
        return ''
    sp0 = urllib.parse.urlsplit(cur)
    if not _has_only_global_ips(sp0.hostname or ''):
        return ''
    if urllib.request.urlopen is not _ORIG_URLOPEN:
        return cur

    cur_hdrs = {
        str(k).translate({10: None, 13: None}): str(v).translate({10: None, 13: None})
        for k, v in (hdrs or {}).items()
        if str(k).translate({10: None, 13: None})
    }
    cur_hdrs.setdefault('User-Agent', UA)
    cache_key = (cur, tuple(sorted((k.lower(), v) for k, v in cur_hdrs.items())))
    now_m = time.monotonic()
    with _SAFE_FFMPEG_URL_LOCK:
        hit = _SAFE_FFMPEG_URL_CACHE.get(cache_key)
    if hit and (now_m - hit[0]) < (3600.0 if hit[1] == orig_cur else 240.0):
        hit_host = urllib.parse.urlsplit(hit[1]).hostname or ''
        if _is_safe_stream_url(hit[1]) and _has_only_global_ips(hit_host):
            return hit[1]

    class _NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedir)
    for _ in range(5):
        probe_hdrs = dict(cur_hdrs, Range='bytes=0-0')
        req = urllib.request.Request(cur, headers=probe_hdrs)
        code = 0
        loc = ''
        try:
            with opener.open(req, timeout=3.5) as resp:
                code = getattr(resp, 'status', 200)
                loc = resp.headers.get('Location', '')
        except urllib.error.HTTPError as he:
            code = he.code
            loc = he.headers.get('Location', '')
            he.close()
        except Exception:
            return ''
        if code not in (301, 302, 303, 307, 308):
            if 200 <= code < 300:
                with _SAFE_FFMPEG_URL_LOCK:
                    if len(_SAFE_FFMPEG_URL_CACHE) >= 128 and cache_key not in _SAFE_FFMPEG_URL_CACHE:
                        _SAFE_FFMPEG_URL_CACHE.pop(next(iter(_SAFE_FFMPEG_URL_CACHE)), None)
                    _SAFE_FFMPEG_URL_CACHE[cache_key] = (time.monotonic(), cur)
            return cur
        if not loc:
            return ''
        nxt = urllib.parse.urljoin(cur, loc.strip())
        if not _is_safe_stream_url(nxt):
            return ''
        old_sp = urllib.parse.urlsplit(cur)
        new_sp = urllib.parse.urlsplit(nxt)
        if old_sp.scheme == 'https' and new_sp.scheme != 'https':
            return ''
        if not _has_only_global_ips(new_sp.hostname or ''):
            return ''
        if _cookie_root(old_sp.hostname or '') != _cookie_root(new_sp.hostname or ''):
            for hk in [k for k in list(cur_hdrs.keys()) if k.lower() in ('cookie', 'authorization')]:
                cur_hdrs.pop(hk, None)
        if is_live and new_sp.path.lower().endswith(('.mp3', '.aac', '.m3u8')):
            with _SAFE_FFMPEG_URL_LOCK:
                if len(_SAFE_FFMPEG_URL_CACHE) >= 128 and cache_key not in _SAFE_FFMPEG_URL_CACHE:
                    _SAFE_FFMPEG_URL_CACHE.pop(next(iter(_SAFE_FFMPEG_URL_CACHE)), None)
                _SAFE_FFMPEG_URL_CACHE[cache_key] = (time.monotonic(), nxt)
            return nxt
        cur = nxt
    return ''


def _evict_safe_ffmpeg_url(url: str) -> None:
    u = (url or '').strip()
    if not u:
        return
    with _SAFE_FFMPEG_URL_LOCK:
        for k in [k for k in _SAFE_FFMPEG_URL_CACHE if k[0] == u]:
            _SAFE_FFMPEG_URL_CACHE.pop(k, None)


def _is_allowed_local_audio(url: str) -> bool:
    if not isinstance(url, str) or not os.path.isabs(url.strip()):
        return False
    try:
        rp = Path(url).resolve()
        if not rp.is_file() or rp.suffix.lower() not in AUDIO_EXTS:
            return False
        for d in LOCAL_DIRS:
            try:
                if d.exists() and rp.is_relative_to(d.resolve()):
                    return True
            except OSError:
                continue
    except (OSError, ValueError):
        return False
    return False


_USB_MNT_MEMO = [0.0, True]


def _usb_mounted() -> bool:
    """2026-11-24 Claude：U 盘现在挂没挂着（只读 /proc/self/mounts，不碰 U 盘本身）。读不到时当作挂着。"""
    use_memo = (_usb_mounted is _ORIG_USB_MOUNTED)
    now_m = time.monotonic()
    if use_memo and (now_m - _USB_MNT_MEMO[0]) < 1.0:
        return _USB_MNT_MEMO[1]
    try:
        with open('/proc/self/mounts', encoding='utf-8', errors='replace') as f:
            res = any(line.split(' ')[1:2] == [str(USB_MNT_DIR)] for line in f)
    except OSError:
        res = True
    if use_memo:
        _USB_MNT_MEMO[0] = now_m
        _USB_MNT_MEMO[1] = res
    return res


_ORIG_USB_MOUNTED = _usb_mounted


def _local_track_gone(t, usb_on=None) -> bool:
    """2026-11-24 Claude：本机 / U 盘歌曲的文件已经不在了（多半是 U 盘拔了）。
    U 盘上的歌只看 U 盘挂没挂着；本机文件夹在系统盘上，直接看文件在不在。"""
    if not isinstance(t, dict) or not t.get('is_local'):
        return False
    u = t.get('url')
    if not isinstance(u, str) or not u.strip():
        return False
    if u.startswith(str(USB_MNT_DIR) + '/') or '/Music/usb/' in u:
        return not (_usb_mounted() if usb_on is None else usb_on)
    try:
        return not os.path.isfile(u)
    except (OSError, ValueError):
        return True
_BT_PAUSE_CODE = (
    'import dbus; bus = dbus.SystemBus(private=True)\n'
    'try:\n'
    '    om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")\n'
    '    for p, ifs in om.GetManagedObjects().items():\n'
    '        mp = ifs.get("org.bluez.MediaPlayer1")\n'
    '        if mp and mp.get("Status") == "playing":\n'
    '            dbus.Interface(bus.get_object("org.bluez", p), "org.bluez.MediaPlayer1").Pause()\n'
    'finally:\n'
    '    bus.close()\n'
)


def load_config() -> dict:
    global _CFG_CACHE, _CFG_SIG
    with _CFG_LOCK:
        try:
            st = os.lstat(CONFIG_PATH)
            if not stat.S_ISREG(st.st_mode):
                raise OSError('Refusing non-regular config')
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if _CFG_CACHE is not None and _CFG_SIG == sig:
            return dict(_CFG_CACHE)
        try:
            if sig is not None:
                loaded = json.loads(_read_regular_text(CONFIG_PATH, 262144, private=True))
                if isinstance(loaded, dict):
                    _CFG_CACHE = loaded
                    _CFG_SIG = sig
                    return dict(_CFG_CACHE)
        except Exception as e:
            log.warning('Failed to read config: %s', e)
        _CFG_CACHE = {
            'uin': '',
            'qm_keyst': '',
            'cookies_str': '',
            'quality_level': 'lossless',
            'user_profile': None,
        }
        _CFG_SIG = sig
        return dict(_CFG_CACHE)


_DIR201_CACHE = None
_DIR201_GEN = 0
_DIR201_LOCK = threading.Lock()


def save_config(cfg: dict):
    global _CFG_CACHE, _CFG_SIG, _DIR201_CACHE, _DIR201_GEN, _LAST_REFRESH_FAIL, _LAST_REFRESH_OK, _DEAD_PSKEY
    auth_changed = True
    with _CFG_LOCK:
        if isinstance(_CFG_CACHE, dict):
            auth_changed = any(cfg.get(k) != _CFG_CACHE.get(k) for k in ('uin', 'qm_keyst', 'cookies_str', 'quality_level'))
        _CFG_CACHE = dict(cfg)
        tmp = CONFIG_PATH.parent / f'.{CONFIG_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp'
        created = False
        try:
            CONFIG_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                pst = os.lstat(CONFIG_PATH.parent)
                if stat.S_ISDIR(pst.st_mode) and not pst.st_mode & stat.S_ISVTX and pst.st_uid == os.getuid():
                    os.chmod(CONFIG_PATH.parent, 0o700)
            if CONFIG_PATH.is_symlink():
                raise OSError('Refusing symlink config path')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(fd, 'w', encoding='utf-8') as tf:
                os.fchmod(tf.fileno(), 0o600)
                tf.write(json.dumps(cfg, ensure_ascii=False, indent=2))
                tf.flush()
                os.fsync(tf.fileno())
            if CONFIG_PATH.is_symlink():
                raise OSError('Refusing symlink config path')
            os.replace(tmp, CONFIG_PATH)
            created = False
            st = CONFIG_PATH.stat()
            _CFG_SIG = (st.st_ino, st.st_mtime_ns, st.st_size)
        except Exception as e:
            log.error('Failed to save config: %s', e)
        finally:
            if created:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    if auth_changed:
        _LAST_REFRESH_FAIL = (None, 0.0)
        if not str(cfg.get('uin') or '').strip():
            _LAST_REFRESH_OK = (None, 0.0)
            _DEAD_PSKEY = None
        with _DIR201_LOCK:
            _DIR201_CACHE = None
            _DIR201_GEN += 1


def _is_wx_login(cfg: dict, extra_cookie: str = '') -> bool:
    mk = str(cfg.get('qm_keyst') or '')
    if mk.startswith('Q_H_L_'):
        return False
    if mk.startswith('W_X_'):
        return True
    prof = cfg.get('user_profile') if isinstance(cfg.get('user_profile'), dict) else {}
    if prof.get('loginType') == 'qq':
        return False
    if prof.get('loginType') == 'wx':
        return True
    ck = extra_cookie or str(cfg.get('cookies_str') or '')
    return 'wxuin=' in ck or 'tmeLoginType=1' in ck


def _qq_headers(extra_cookie: str = '') -> dict:
    cfg = load_config()
    ck = extra_cookie or cfg.get('cookies_str', '')
    is_wx = _is_wx_login(cfg, ck)
    if cfg.get('uin') and cfg.get('qm_keyst'):
        mk = cfg['qm_keyst']
        if re.search(r'(?:^|;\s*)qm_keyst=', ck):
            ck = re.sub(r'(^|;\s*)qm_keyst=[^;\s]*', lambda m: m.group(1) + f'qm_keyst={mk}', ck)
            if re.search(r'(?:^|;\s*)qqmusic_key=', ck):
                ck = re.sub(r'(^|;\s*)qqmusic_key=[^;\s]*', lambda m: m.group(1) + f'qqmusic_key={mk}', ck)
            else:
                ck = f'{ck}; qqmusic_key={mk}'
            if not re.search(r'(?:^|;\s*)uin=', ck):
                ck = f"{ck}; uin={cfg['uin']}"
            if is_wx:
                if not re.search(r'(?:^|;\s*)wxuin=', ck):
                    ck = f"{ck}; wxuin={cfg['uin']}"
                if not re.search(r'(?:^|;\s*)tmeLoginType=', ck):
                    ck = f'{ck}; tmeLoginType=1'
        else:
            mk_part = (
                f"wxuin={cfg['uin']}; uin={cfg['uin']}; qm_keyst={mk}; qqmusic_key={mk}; tmeLoginType=1"
                if is_wx else f"uin={cfg['uin']}; qm_keyst={mk}; qqmusic_key={mk}"
            )
            ck = f"{ck}; {mk_part}" if ck else mk_part
    h = {
        'User-Agent': UA,
        'Referer': 'https://y.qq.com/',
        'Origin': 'https://y.qq.com',
        'Content-Type': 'application/json',
    }
    if ck:
        h['Cookie'] = ck
    return h


def _qq_comm() -> dict:
    cfg = load_config()
    uin = str(cfg.get('uin') or '0')
    comm = {'ct': 24, 'cv': 0, 'uin': uin, 'format': 'json'}
    if cfg.get('qm_keyst'):
        comm['authst'] = cfg['qm_keyst']
        comm['qqmusic_key'] = cfg['qm_keyst']
        comm['qm_keyst'] = cfg['qm_keyst']
        if _is_wx_login(cfg):
            comm['tmeLoginType'] = 1
    return comm


def _hash33(s: str) -> int:
    e = 0
    for ch in s:
        e += (e << 5) + ord(ch)
    return 2147483647 & e


def _gtk(skey: str) -> int:
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


# ---- QQ & WeChat Official QR Code Login (支持手机QQ / 微信 / QQ音乐App 扫码) ----

def qq_qr_create(mode: str = 'qq') -> dict:
    mode = (mode if isinstance(mode, str) and mode else 'qq').lower()
    if mode == 'wx':
        wx_url = (
            'https://open.weixin.qq.com/connect/qrconnect?appid=wx48db31d50e334801'
            '&redirect_uri=https%3A%2F%2Fy.qq.com%2Fportal%2Fwx_redirect.html%3Flogin_type%3D2'
            '%26surl%3Dhttps%253A%252F%252Fy.qq.com%252F&response_type=code&scope=snsapi_login'
            '&state=STATE#wechat_redirect'
        )
        req = urllib.request.Request(wx_url, headers={'User-Agent': UA})
        try:
            with _safe_urlopen(req, timeout=6) as r:
                html = _read_http_bytes(r).decode('utf-8', errors='replace')
            m = re.search(r'/connect/qrcode/([a-zA-Z0-9_-]+)', html)
            if not m:
                return {'ok': False, 'error': '未获取到微信二维码 uuid'}
            uuid = m.group(1)
            img_url = f'https://open.weixin.qq.com/connect/qrcode/{uuid}'
            with _safe_urlopen(urllib.request.Request(img_url, headers={'User-Agent': UA}), timeout=6) as ir:
                img_b64 = 'data:image/jpeg;base64,' + base64.b64encode(_read_http_bytes(ir)).decode('ascii')
            log.info('Created WeChat QR code: uuid=%s...', uuid[:10])
            return {'ok': True, 'mode': 'wx', 'qrsig': uuid, 'qr_img': img_b64}
        except Exception as e:
            log.warning('WeChat QR create failed: %s', e)
            return {'ok': False, 'error': '微信二维码生成失败，请点击刷新重试'}

    # Default: QQ Official QR Login
    xlogin_url = (
        'https://xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=716027609&daid=383&style=33'
        '&login_text=%E7%99%BB%E5%BD%95&hide_title_bar=1&hide_border=1&target=self'
        '&s_url=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump&pt_3rd_aid=100497308'
    )
    qr_url = (
        'https://ssl.ptlogin2.qq.com/ptqrshow?'
        'appid=716027609&e=2&l=M&s=4&d=72&v=4&t=0.5&daid=383&pt_3rd_aid=100497308'
    )
    try:
        pt_login_sig = ''
        with _safe_urlopen(urllib.request.Request(xlogin_url, headers={'User-Agent': UA}), timeout=5) as xr:
            x_ck = '\n'.join(xr.headers.get_all('Set-Cookie') or [])
            ms = re.search(r'pt_login_sig=([^;\s]+)', x_ck)
            if ms:
                pt_login_sig = ms.group(1)

        req = urllib.request.Request(qr_url, headers={'User-Agent': UA, 'Referer': xlogin_url})
        with _safe_urlopen(req, timeout=6) as r:
            png_bytes = _read_http_bytes(r)
            cookies = '\n'.join(r.headers.get_all('Set-Cookie') or [])
            m = re.search(r'qrsig=([^;\s]+)', cookies)
            qrsig = m.group(1) if m else ''
        if not qrsig:
            return {'ok': False, 'error': '未获取到 QQ 二维码签名'}
        now_ts = time.monotonic()
        with _QR_LOCK:
            for old_k in [k for k, v in list(_QR_SESSIONS.items()) if now_ts - v.get('time', 0) > 600]:
                _QR_SESSIONS.pop(old_k, None)
            while len(_QR_SESSIONS) >= 16 and qrsig not in _QR_SESSIONS:
                _QR_SESSIONS.pop(next(iter(_QR_SESSIONS)), None)
            _QR_SESSIONS[qrsig] = {'pt_login_sig': pt_login_sig, 'time': now_ts}
        b64 = 'data:image/png;base64,' + base64.b64encode(png_bytes).decode('ascii')
        log.info('Created QQ QR code: qrsig=%s...', qrsig[:12])
        return {'ok': True, 'mode': 'qq', 'qrsig': qrsig, 'qr_img': b64}
    except Exception as e:
        log.warning('QQ QR create failed: %s', e)
        return {'ok': False, 'error': 'QQ二维码生成失败，请点击刷新重试'}


def _exchange_code_for_musickey(code: str, appid: str = '', fallback_uin: str = '', fallback_nick: str = '', extra_cookies: dict = None) -> dict:
    extra_cookies = {k: v for k, v in (extra_cookies or {}).items() if v}
    graph_p_skey = extra_cookies.get('p_skey', '') if not appid else ''
    p_skey = extra_cookies.get('p_skey', '') or extra_cookies.get('skey', '')
    gtk_val = _gtk(p_skey) if p_skey else 5381

    if appid:
        # WeChat Login (login_type == 2)
        login_payload = {
            'comm': {'g_tk': gtk_val, 'platform': 'yqq', 'ct': 24, 'cv': 0, 'tmeAppID': 'qqmusic', 'tmeLoginType': '1'},
            'req': {
                'module': 'music.login.LoginServer',
                'method': 'Login',
                'param': {'code': code, 'strAppid': appid},
            },
        }
    else:
        # QQ Login (login_type == 1, matches y.qq.com/portal/wx_redirect.html)
        login_payload = {
            'comm': {'g_tk': gtk_val, 'platform': 'yqq', 'ct': 24, 'cv': 0},
            'req': {
                'module': 'QQConnectLogin.LoginServer',
                'method': 'QQLogin',
                'param': {'code': code},
            },
        }

    qm_keyst = ''
    uin = fallback_uin
    nick = fallback_nick
    headurl = f'https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100' if uin else ''
    if code:
        try:
            ck_hdr = '; '.join(f'{k}={v}' for k, v in extra_cookies.items() if v)
            hdrs = {'User-Agent': UA, 'Referer': 'https://y.qq.com/portal/wx_redirect.html', 'Content-Type': 'application/x-www-form-urlencoded'}
            if ck_hdr:
                hdrs['Cookie'] = ck_hdr
            lreq = urllib.request.Request(
                'https://u.y.qq.com/cgi-bin/musicu.fcg',
                data=json.dumps(login_payload).encode('utf-8'),
                headers=hdrs,
            )
            with _safe_urlopen(lreq, timeout=6) as lr:
                for sc in lr.headers.get_all('Set-Cookie') or []:
                    part = sc.split(';', 1)[0].strip()
                    if '=' in part:
                        k, v = part.split('=', 1)
                        if v:
                            extra_cookies[k] = v
                ldata = json.loads(_read_http_bytes(lr).decode('utf-8', errors='replace'))
                rinfo = (ldata.get('req') or {}).get('data') or {}
                qm_keyst = rinfo.get('musickey') or extra_cookies.get('qm_keyst') or extra_cookies.get('qqmusic_key') or ''
                if rinfo.get('str_musicid') or rinfo.get('musicid'):
                    uin = str(rinfo.get('str_musicid') or rinfo.get('musicid'))
                if rinfo.get('nick'):
                    nick = rinfo['nick']
                if rinfo.get('logo') or rinfo.get('headurl'):
                    headurl = rinfo.get('logo') or rinfo.get('headurl')
                if not appid:
                    for ck_k, rk_k in (
                        ('psrf_qqopenid', 'openid'),
                        ('psrf_qqrefresh_token', 'refresh_token'),
                        ('psrf_qqaccess_token', 'access_token'),
                        ('psrf_qqunionid', 'unionid'),
                        ('psrf_musickey', 'refresh_key'),
                    ):
                        rv = rinfo.get(rk_k)
                        if isinstance(rv, str) and re.fullmatch(r'[A-Za-z0-9_.-]{4,512}', rv.strip()) and not extra_cookies.get(ck_k):
                            extra_cookies[ck_k] = rv.strip()
                log.info('OAuth exchange code=%s musickey_len=%d uin=%s nick=%s',
                         (ldata.get('req') or {}).get('code'), len(qm_keyst), uin, nick)
        except Exception as e:
            log.warning('LoginServer exchange error: %s', e)

    if not uin or not qm_keyst:
        log.warning('OAuth exchange missing uin or qm_keyst; aborting config overwrite')
        return None

    all_cookies = dict(extra_cookies)
    if uin:
        all_cookies['uin'] = uin
        if appid:
            all_cookies['wxuin'] = uin
            all_cookies['tmeLoginType'] = '1'
        else:
            all_cookies.pop('wxuin', None)
            all_cookies.pop('tmeLoginType', None)
    if qm_keyst:
        all_cookies['qm_keyst'] = qm_keyst
        all_cookies['qqmusic_key'] = qm_keyst

    profile = {
        'userId': uin or 'vip_user',
        'nickname': nick or f'QQ音乐用户_{uin}',
        'avatarUrl': headurl,
        'isVip': bool(qm_keyst),
        'loginType': 'wx' if appid else 'qq',
    }
    with _CFG_LOCK:
        cfg = load_config()
        cfg['uin'] = uin
        cfg['qm_keyst'] = qm_keyst
        if graph_p_skey:
            cfg['graph_p_skey'] = graph_p_skey
        else:
            cfg.pop('graph_p_skey', None)
        cfg['cookies_str'] = '; '.join(f'{k}={v}' for k, v in all_cookies.items() if v)
        cfg['user_profile'] = profile
        save_config(cfg)
    _clear_dir201_cache()
    log.info('Saved QQ Music login profile: %s (uin=%s)', profile['nickname'], uin)
    return profile


def _complete_qq_oauth(check_sig_url: str, nickname_hint: str = '') -> dict:
    parts = urllib.parse.urlsplit(str(check_sig_url or '').strip())
    host = (parts.hostname or '').lower().rstrip('.')
    if parts.scheme != 'https' or parts.username is not None or not (host == 'qq.com' or host.endswith('.qq.com')):
        raise ValueError(f'Refusing untrusted check_sig URL host: {host!r}')
    if urllib.request.urlopen is _ORIG_URLOPEN and not _has_only_global_ips(host):
        raise ValueError(f'Refusing non-global check_sig URL host: {host!r}')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
    opener.addheaders = [('User-Agent', UA), ('Referer', 'https://xui.ptlogin2.qq.com/')]

    raw_set_cookies = []
    try:
        with opener.open(check_sig_url, timeout=6) as r:
            raw_set_cookies.extend(r.headers.get_all('Set-Cookie') or [])
    except urllib.error.HTTPError as he:
        raw_set_cookies.extend(he.headers.get_all('Set-Cookie') or [])
        he.close()

    cookie_dict = {}
    graph_cookies = {}
    req_host = (urllib.parse.urlsplit(check_sig_url).hostname or '').lower()
    for c in cj:
        if c.value:
            cookie_dict[c.name] = c.value
            if 'graph.qq.com' in (c.domain or '').lower():
                graph_cookies[c.name] = c.value
    for sc in raw_set_cookies:
        part = sc.split(';', 1)[0].strip()
        if '=' in part:
            k, v = part.split('=', 1)
            if v:
                cookie_dict[k] = v
                m_dom = re.search(r';\s*domain\s*=\s*([^;\s]+)', sc, re.I)
                dom = m_dom.group(1).strip('.').lower() if m_dom else req_host
                if dom == 'graph.qq.com' or dom.endswith('.graph.qq.com'):
                    graph_cookies[k] = v
    cookie_dict.update(graph_cookies)

    uin = re.sub(r'^o0*', '', cookie_dict.get('p_uin', '') or cookie_dict.get('uin', ''))
    p_skey = cookie_dict.get('p_skey', '') or cookie_dict.get('skey', '')
    log.info('check_sig cookies: uin=%s has_p_skey=%s keys=%s', uin, bool(p_skey), list(cookie_dict.keys()))

    code = ''
    if p_skey:
        auth_data = urllib.parse.urlencode({
            'response_type': 'code',
            'client_id': '100497308',
            'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/',
            'scope': 'get_user_info,get_app_friends',
            'state': 'state',
            'switch': '',
            'from_ptlogin': '1',
            'src': '1',
            'update_auth': '1',
            'openapi': '1010_1030',
            'g_tk': str(_gtk(p_skey)),
            'auth_time': str(int(time.time() * 1000)),
            'ui': '00000000-0000-0000-0000-000000000000',
        }).encode('utf-8')
        cookie_hdr = '; '.join(f'{k}={v}' for k, v in cookie_dict.items())
        loc = ''
        try:
            if urllib.request.urlopen is _ORIG_URLOPEN and not _has_only_global_ips('graph.qq.com'):
                raise ValueError('Refusing non-global graph.qq.com host')
            req_auth = urllib.request.Request(
                'https://graph.qq.com/oauth2.0/authorize',
                data=auth_data,
                headers={
                    'User-Agent': UA,
                    'Referer': 'https://graph.qq.com/oauth2.0/show',
                    'Cookie': cookie_hdr,
                },
            )
            with opener.open(req_auth, timeout=6) as r_auth:
                loc = r_auth.geturl()
        except urllib.error.HTTPError as he:
            loc = he.headers.get('Location', '')
            he.close()
        log.info('graph.qq.com authorize redirect Location: %s', re.sub(r'([?&]code=)[^&#]+', r'\1***', loc)[:120])
        m_code = re.search(r'[?&]code=([^&#]+)', loc)
        if m_code:
            code = m_code.group(1)

    return _exchange_code_for_musickey(code, appid='', fallback_uin=uin, fallback_nick=nickname_hint, extra_cookies=cookie_dict)


def qq_qr_check(qrsig: str, mode: str = 'qq') -> dict:
    if not isinstance(qrsig, str) or not qrsig.strip():
        return {'ok': False, 'error': '缺少 qrsig'}
    qrsig = qrsig.strip()
    if len(qrsig) > 512 or any(ord(c) < 0x20 or ord(c) == 0x7f or c in ';\r\n' for c in qrsig):
        return {'ok': False, 'error': '无效的 qrsig'}
    mode = (mode if isinstance(mode, str) and mode else 'qq').lower()

    if mode == 'wx':
        q_uuid = urllib.parse.quote(qrsig, safe='')
        lp_url = f'https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={q_uuid}&_={int(time.time() * 1000)}'
        req = urllib.request.Request(lp_url, headers={'User-Agent': UA, 'Referer': 'https://open.weixin.qq.com/'})
        try:
            with _safe_urlopen(req, timeout=2.5) as r:
                txt = _read_http_bytes(r).decode('utf-8', errors='replace').strip()
            m_err = re.search(r'wx_errcode=(\d+)', txt)
            m_code = re.search(r"wx_code='([^']*)'", txt)
            errcode = int(m_err.group(1)) if m_err else 408
            wx_code = m_code.group(1) if m_code else ''
            if errcode != 408:
                log.info('WeChat QR poll uuid=%s... errcode=%s has_code=%s', qrsig[:10], errcode, bool(wx_code))
            if errcode == 405 and wx_code:
                profile = _exchange_code_for_musickey(wx_code, appid='wx48db31d50e334801')
                if not profile:
                    return {'ok': False, 'error': '微信登录授权失败，请点击刷新二维码重试'}
                return {'ok': True, 'code': 0, 'message': '微信登录成功', 'profile': profile}
            if errcode == 404:
                return {'ok': True, 'code': 67, 'message': '微信已扫码，请在手机微信上点击确认'}
            if errcode in (402, 403, 500):
                return {'ok': True, 'code': 65, 'message': '微信二维码已过期，请点击刷新'}
            return {'ok': True, 'code': 66, 'message': '等待微信扫码...'}
        except (TimeoutError, socket.timeout):
            return {'ok': True, 'code': 66, 'message': '等待微信扫码...'}
        except urllib.error.URLError as ue:
            if 'timed out' in str(ue).lower():
                return {'ok': True, 'code': 66, 'message': '等待微信扫码...'}
            log.warning('WeChat QR poll URLError: %s', ue)
            return {'ok': False, 'error': '二维码连接波动，请点击刷新重试'}
        except OSError as oe:
            if 'timed out' in str(oe).lower():
                return {'ok': True, 'code': 66, 'message': '等待微信扫码...'}
            log.warning('WeChat QR poll OSError: %s', oe)
            return {'ok': False, 'error': '二维码连接波动，请点击刷新重试'}
        except Exception as e:
            log.warning('WeChat QR poll error: %s', e)
            return {'ok': False, 'error': '二维码服务暂时不可用，请点击刷新重试'}

    # QQ QR Check
    with _QR_LOCK:
        sess = dict(_QR_SESSIONS.get(qrsig) or {})
    pt_login_sig = str(sess.get('pt_login_sig') or '').translate({10: None, 13: None, 59: None})
    q_login_sig = urllib.parse.quote(pt_login_sig, safe='')
    token = _hash33(qrsig)
    chk_url = (
        f'https://ssl.ptlogin2.qq.com/ptqrlogin?u1=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump'
        f'&ptqrtoken={token}&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052'
        f'&action=0-0-{int(time.time() * 1000)}&js_ver=24032716&js_type=1'
        f'&login_sig={q_login_sig}&pt_uistyle=40&aid=716027609&daid=383&pt_3rd_aid=100497308'
    )
    ck = f'qrsig={qrsig}' + (f'; pt_login_sig={pt_login_sig}' if pt_login_sig else '')
    req = urllib.request.Request(
        chk_url,
        headers={
            'User-Agent': UA,
            'Referer': 'https://xui.ptlogin2.qq.com/',
            'Cookie': ck,
        },
    )
    try:
        with _safe_urlopen(req, timeout=6) as r:
            txt = _read_http_bytes(r).decode('utf-8', errors='replace').strip()
        parts = re.findall(r"'([^']*)'", txt)
        if len(parts) < 5 or not parts[0].isdigit():
            log.info('QQ QR unexpected response: %s', txt[:120])
            return {'ok': True, 'code': -1, 'message': '二维码状态异常，请点击刷新重试'}
        code = int(parts[0])
        jump_url = parts[2]
        msg = parts[4]
        nick = parts[5] if len(parts) > 5 else ''
        if code != 66:
            log.info('QQ QR poll code=%d msg=%s nick=%s jump=%s', code, msg, nick, bool(jump_url))
        if code == 0 and jump_url:
            with _QR_LOCK:
                _QR_SESSIONS.pop(qrsig, None)
            profile = _complete_qq_oauth(jump_url, nick)
            if not profile:
                return {'ok': False, 'error': 'QQ登录授权失败，请点击刷新二维码重试'}
            return {'ok': True, 'code': 0, 'message': '登录成功', 'profile': profile}
        if code == 65:
            with _QR_LOCK:
                _QR_SESSIONS.pop(qrsig, None)
        return {'ok': True, 'code': code, 'message': msg, 'nickname': nick}
    except Exception as e:
        log.warning('qq_qr_check error: %s', e)
        return {'ok': False, 'error': '二维码服务暂时不可用，请点击刷新重试'}


def qq_bind_uin(raw_input: str) -> dict:
    """Bind a QQ Number (UIN) or full y.qq.com Cookie string."""
    if isinstance(raw_input, int) and not isinstance(raw_input, bool):
        raw_input = str(raw_input)
    raw = raw_input.strip() if isinstance(raw_input, str) else ''
    if not raw:
        return {'ok': False, 'error': '请输入 QQ 号或 Cookie'}
    with _CFG_LOCK:
        cfg = load_config()
        if raw.isascii() and raw.isdigit():
            uin = raw.lstrip('0')
            if not (5 <= len(uin) <= 20):
                return {'ok': False, 'error': '请输入 5～20 位有效账号'}
            if str(cfg.get('uin') or '') != uin:
                cfg['qm_keyst'] = ''
                cfg['cookies_str'] = ''
                cfg.pop('graph_p_skey', None)
            profile = {
                'userId': uin,
                'nickname': f'QQ音乐用户 ({uin})',
                'avatarUrl': f'https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100' if len(uin) <= 12 else '',
                'isVip': bool(cfg.get('qm_keyst')),
            }
            cfg['uin'] = uin
            cfg['user_profile'] = profile
            save_config(cfg)
            _clear_dir201_cache()
            return {'ok': True, 'profile': profile}

        # Parse full Cookie string (containing uin=...; qm_keyst=...) — strip control/non-ASCII header chars
        clean_raw = re.sub(r'[\x00-\x1f\x7f-\uffff]+', '', raw).strip()
        m_uin = re.search(r'(?:^|;\s*)(?:wxuin|p_uin|uin)=o?0*([1-9][0-9]{4,19})(?=[;\s]|$)', clean_raw)
        m_key = re.search(r'(?:^|;\s*)(?:qm_keyst|qqmusic_key)=([^;\s]{1,1024})', clean_raw)
        if not m_key and not re.fullmatch(r'[A-Za-z0-9_.=-]{16,1024}', clean_raw):
            return {'ok': False, 'error': '请输入有效 QQ 号或包含 qm_keyst 的 Cookie'}
        prev_uin = str(cfg.get('uin') or '').strip()
        uin = m_uin.group(1) if m_uin else (prev_uin if prev_uin.isascii() and prev_uin.isdigit() else '10000')
        qm_keyst = m_key.group(1) if m_key else clean_raw
        is_wx = qm_keyst.startswith('W_X_') or 'wxuin=' in clean_raw or 'tmeLoginType=1' in clean_raw
        profile = {
            'userId': uin,
            'nickname': f'QQ音乐绿钻VIP ({uin})',
            'avatarUrl': f'https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100' if (not is_wx and uin.isascii() and uin.isdigit() and len(uin) <= 12) else '',
            'isVip': True,
            'loginType': 'wx' if is_wx else 'qq',
        }
        cfg['uin'] = uin
        cfg['qm_keyst'] = qm_keyst
        cfg.pop('graph_p_skey', None)
        if '=' in clean_raw:
            cfg['cookies_str'] = clean_raw[:4096]
        elif is_wx:
            cfg['cookies_str'] = f'wxuin={uin}; uin={uin}; qm_keyst={qm_keyst}; qqmusic_key={qm_keyst}; tmeLoginType=1'
        else:
            cfg['cookies_str'] = f'uin={uin}; qm_keyst={qm_keyst}; qqmusic_key={qm_keyst}'
        cfg['user_profile'] = profile
        save_config(cfg)
        _clear_dir201_cache()
        return {'ok': True, 'profile': profile}


def _clear_dir201_cache():
    global _DIR201_CACHE, _DIR201_GEN
    with _DIR201_LOCK:
        _DIR201_CACHE = None
        _DIR201_GEN += 1


def qq_logout() -> dict:
    with _CFG_LOCK:
        cfg = load_config()
        cfg['uin'] = ''
        cfg['qm_keyst'] = ''
        cfg.pop('graph_p_skey', None)
        cfg['cookies_str'] = ''
        cfg['user_profile'] = None
        save_config(cfg)
    _clear_dir201_cache()
    return {'ok': True}


# ---- QQ Music Data & Stream Resolution ----

def _default_quality_label(cfg: dict = None) -> str:
    q = (cfg if isinstance(cfg, dict) else load_config()).get('quality_level', 'lossless')
    if q == 'lossless':
        return 'SQ 无损 FLAC'
    if q == 'standard':
        return 'AAC 极高音质'
    return 'HQ 320k MP3'


def _format_qq_song(s: dict, purl_map: dict = None, default_q: str = None) -> dict:
    mid = s.get('mid') or s.get('songmid') or ''
    media_mid = (s.get('file') or {}).get('media_mid') or s.get('strMediaMid') or mid
    title = (s.get('name') or s.get('songname') or '').strip()
    singers = s.get('singer') or []
    artist = '/'.join(x.get('name', '') for x in singers if x.get('name'))
    album_obj = s.get('album') or {}
    album_name = (album_obj.get('name') or s.get('albumname') or '').strip()
    albummid = album_obj.get('mid') or s.get('albummid') or ''
    cover = f'https://y.gtimg.cn/music/photo_new/T002R500x500M000{albummid}.jpg' if albummid else ''
    thumb = f'https://y.gtimg.cn/music/photo_new/T002R150x150M000{albummid}.jpg' if albummid else ''
    duration = int(s.get('interval') or 0)
    pay_obj = s.get('pay') or {}
    pay_play = pay_obj.get('pay_play') or pay_obj.get('payplay', 0)

    url_info = (purl_map or {}).get(mid) or {}
    if default_q is None:
        default_q = _default_quality_label()
    return {
        'mid': mid,
        'media_mid': media_mid,
        'title': title,
        'artist': artist,
        'album': album_name,
        'albummid': albummid,
        'cover': cover,
        'thumb': thumb,
        'duration': duration,
        'vip': bool(pay_play),
        'source': url_info.get('source', 'QQ音乐·SQ无损'),
        'quality': url_info.get('quality', default_q),
        'url': url_info.get('url'),
        'headers': {'User-Agent': UA, 'Referer': 'https://y.qq.com/'},
        'is_local': False,
    }


def _refresh_musickey_from_cookies() -> bool:
    """Silently refresh qm_keyst (musickey) via QQ p_skey OAuth or saved 60-day refresh_token."""
    global _LAST_REFRESH_FAIL, _LAST_REFRESH_OK, _DEAD_PSKEY

    def _upsert_ck(ck_s: str, key: str, val: str) -> str:
        pat = rf'(^|;\s*){re.escape(key)}=[^;\s]*'
        if re.search(pat, ck_s):
            return re.sub(pat, lambda m: f'{m.group(1)}{key}={val}', ck_s, count=1)
        return f'{ck_s}; {key}={val}' if ck_s else f'{key}={val}'

    with _REFRESH_LOCK:
        cfg = load_config()
        cookies = str(cfg.get('cookies_str') or '')
        uin = str(cfg.get('uin') or '')
        m = re.search(r'(?:^|;\s*)p_skey=([^;\s]+)', cookies)
        p_skey = str(cfg.get('graph_p_skey') or '').strip() or (m.group(1) if m else '')
        m_rt = re.search(r'(?:^|;\s*)psrf_qqrefresh_token=([^;\s]+)', cookies)
        m_oid = re.search(r'(?:^|;\s*)psrf_qqopenid=([^;\s]+)', cookies)
        m_at = re.search(r'(?:^|;\s*)psrf_qqaccess_token=([^;\s]+)', cookies)
        m_uid = re.search(r'(?:^|;\s*)psrf_qqunionid=([^;\s]+)', cookies)
        m_rk = re.search(r'(?:^|;\s*)psrf_musickey=([^;\s]+)', cookies)
        qq_ref_tok = m_rt.group(1) if m_rt else ''
        qq_openid = m_oid.group(1) if m_oid else ''
        qq_acc_tok = m_at.group(1) if m_at else ''
        qq_unionid = m_uid.group(1) if m_uid else ''
        qq_ref_key = (m_rk.group(1) if m_rk else '') or str(cfg.get('qm_keyst') or '').strip()

        if not uin or (not p_skey and not (qq_ref_tok and qq_openid)):
            return False
        if cfg.get('graph_p_skey') and p_skey:
            cookies = _upsert_ck(cookies, 'p_skey', p_skey)
        key_id = (uin, p_skey, qq_ref_tok)
        if _LAST_REFRESH_FAIL[0] == key_id and (time.monotonic() - _LAST_REFRESH_FAIL[1]) < 60.0:
            return False
        cur_mk = str(cfg.get('qm_keyst') or '').strip()
        if cur_mk and _LAST_REFRESH_OK[0] == (uin, cur_mk) and (time.monotonic() - _LAST_REFRESH_OK[1]) < 300.0:
            return False

        def _commit_refreshed(d_obj: dict, used_via: str) -> bool:
            global _LAST_REFRESH_OK, _DEAD_PSKEY
            mk = str((d_obj or {}).get('musickey') or '').strip()
            if not mk or not re.fullmatch(r'[A-Za-z0-9_.=-]{4,1024}', mk):
                return False
            with _CFG_LOCK:
                cur_cfg = load_config()
                cur_ck = str(cur_cfg.get('cookies_str') or '')
                if str(cur_cfg.get('uin') or '') != uin:
                    log.info('Skipping musickey save because account changed or logged out during refresh')
                    return False
                if used_via == 'p_skey':
                    cur_m = re.search(r'(?:^|;\s*)p_skey=([^;\s]+)', cur_ck)
                    cur_p_skey = str(cur_cfg.get('graph_p_skey') or '').strip() or (cur_m.group(1) if cur_m else '')
                    if not cur_p_skey or cur_p_skey != p_skey:
                        log.info('Skipping musickey save because account changed or logged out during refresh')
                        return False
                else:
                    cur_mrt = re.search(r'(?:^|;\s*)psrf_qqrefresh_token=([^;\s]+)', cur_ck)
                    if not cur_mrt or cur_mrt.group(1) != qq_ref_tok:
                        log.info('Skipping musickey save because account changed or logged out during refresh')
                        return False
                cur_ck = _upsert_ck(cur_ck, 'qm_keyst', mk)
                if cur_ck or used_via == 'refresh_token':
                    cur_ck = _upsert_ck(cur_ck, 'qqmusic_key', mk)
                if used_via == 'refresh_token':
                    for ck_k, rk_k in (
                        ('psrf_qqrefresh_token', 'refresh_token'),
                        ('psrf_qqaccess_token', 'access_token'),
                        ('psrf_musickey', 'refresh_key'),
                    ):
                        rv = d_obj.get(rk_k)
                        if isinstance(rv, str) and re.fullmatch(r'[A-Za-z0-9_.=-]{4,1024}', rv.strip()):
                            cur_ck = _upsert_ck(cur_ck, ck_k, rv.strip())
                    mct = d_obj.get('musickeyCreateTime')
                    if isinstance(mct, int) and not isinstance(mct, bool) and mct > 0:
                        cur_ck = _upsert_ck(cur_ck, 'psrf_musickey_createtime', str(mct))
                cur_cfg['qm_keyst'] = mk
                cur_cfg['cookies_str'] = cur_ck
                save_config(cur_cfg)
            _LAST_REFRESH_OK = ((uin, mk), time.monotonic())
            if used_via == 'refresh_token' and p_skey:
                _DEAD_PSKEY = p_skey
            elif used_via == 'p_skey' and _DEAD_PSKEY == p_skey:
                _DEAD_PSKEY = None
            _clear_dir201_cache()
            log.info('Silently refreshed QQ Music VIP musickey via %s (len=%d)', used_via, len(mk))
            return True

        # 1) Try p_skey authorize flow first when available (preserves existing QQ OAuth refresh flow)
        if p_skey and not (qq_ref_tok and qq_openid and p_skey == _DEAD_PSKEY) and not (urllib.request.urlopen is _ORIG_URLOPEN and not _has_only_global_ips('graph.qq.com')):
            g_tk = _gtk(p_skey)

            class StopRedir(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(StopRedir())
            data = urllib.parse.urlencode({
                'response_type': 'code',
                'client_id': '100497308',
                'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/',
                'scope': 'get_user_info,get_app_friends',
                'state': 'state',
                'switch': '',
                'from_ptlogin': '1',
                'src': '1',
                'update_auth': '1',
                'openapi': '1010_1030',
                'g_tk': str(g_tk),
                'auth_time': str(int(time.time() * 1000)),
                'ui': uin,
            }).encode('utf-8')
            req = urllib.request.Request(
                'https://graph.qq.com/oauth2.0/authorize',
                data=data,
                headers={
                    'User-Agent': UA,
                    'Cookie': cookies,
                    'Referer': 'https://graph.qq.com/oauth2.0/show?which=Login&display=pc&response_type=code&client_id=100497308&redirect_uri=https%3A%2F%2Fy.qq.com%2Fportal%2Fwx_redirect.html%3Flogin_type%3D1%26surl%3Dhttps%3A%2F%2Fy.qq.com%2F&state=state',
                },
            )
            loc = ''
            try:
                with opener.open(req, timeout=5) as resp:
                    loc = resp.headers.get('Location', '')
            except urllib.error.HTTPError as e:
                loc = e.headers.get('Location', '')
                e.close()
            except Exception:
                loc = ''

            mc = re.search(r'code=([A-Za-z0-9_-]+)', loc)
            if mc:
                payload = {
                    'comm': {'g_tk': g_tk, 'platform': 'yqq', 'ct': 24, 'cv': 0},
                    'req': {
                        'module': 'QQConnectLogin.LoginServer',
                        'method': 'QQLogin',
                        'param': {'code': mc.group(1)},
                    },
                }
                try:
                    req2 = urllib.request.Request(
                        'https://u.y.qq.com/cgi-bin/musicu.fcg',
                        data=json.dumps(payload).encode('utf-8'),
                        headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json', 'Cookie': cookies},
                    )
                    with _safe_urlopen(req2, timeout=5) as r2:
                        body = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
                        d = (body.get('req') or {}).get('data') or {}
                        if _commit_refreshed(d, 'p_skey'):
                            return True
                except Exception as e:
                    log.warning('Musickey refresh failed: %s', e)

        # 2) Fallback: 60-day OAuth psrf_qqrefresh_token + psrf_qqopenid renewal (works after 24-48h p_skey expires)
        if qq_ref_tok and qq_openid:
            if urllib.request.urlopen is _ORIG_URLOPEN and not _has_only_global_ips('u.y.qq.com'):
                _LAST_REFRESH_FAIL = (key_id, time.monotonic())
                return False
            payload_rt = {
                'comm': {'ct': '24', 'cv': '0', 'format': 'json'},
                'req': {
                    'module': 'QQConnectLogin.LoginServer',
                    'method': 'QQLogin',
                    'param': {
                        'openid': qq_openid,
                        'access_token': qq_acc_tok,
                        'refresh_token': qq_ref_tok,
                        'refresh_key': qq_ref_key,
                        'unionid': qq_unionid,
                        'musicid': int(uin) if uin.isdigit() else 0,
                        'musickey': str(cfg.get('qm_keyst') or '').strip(),
                        'str_musicid': uin,
                        'loginMode': 2,
                        'appid': 100497308,
                    },
                },
            }
            try:
                req_rt = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps(payload_rt).encode('utf-8'),
                    headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'},
                )
                with _safe_urlopen(req_rt, timeout=5) as rr:
                    body_rt = json.loads(_read_http_bytes(rr).decode('utf-8', errors='replace'))
                    d_rt = (body_rt.get('req') or {}).get('data') or {}
                    if _commit_refreshed(d_rt, 'refresh_token'):
                        return True
            except Exception as e:
                log.warning('Musickey refresh_token renewal failed: %s', e)

        _LAST_REFRESH_FAIL = (key_id, time.monotonic())
        return False


def resolve_qq_vkey(mid: str, media_mid: str = '', _retried_refresh: bool = False, _anon: bool = False) -> dict:
    """Resolve official QQ Music stream URL in a single batched call (prioritizing SQ Lossless FLAC)."""
    if not mid:
        return None
    mmid = media_mid or mid
    cfg = load_config()
    has_ck = bool((cfg.get('cookies_str') or cfg.get('qm_keyst') or '').strip())
    uin = '10000' if _anon else str(cfg.get('uin') or '10000')
    q_mode = cfg.get('quality_level', 'lossless')

    candidates = []
    if q_mode == 'lossless':
        candidates.append((f'F000{mmid}.flac', 'SQ 无损 FLAC'))
        candidates.append((f'M800{mmid}.mp3', 'HQ 320k MP3'))
    elif q_mode == 'hq':
        candidates.append((f'M800{mmid}.mp3', 'HQ 320k MP3'))
        candidates.append((f'F000{mmid}.flac', 'SQ 无损 FLAC'))
    candidates.append((f'C400{mmid}.m4a', 'AAC 极高音质'))
    candidates.append((f'M500{mmid}.mp3', '标准音质'))

    filenames = [c[0] for c in candidates]
    comm = {'ct': 24, 'cv': 0, 'uin': '0', 'format': 'json'} if _anon else _qq_comm()
    hdrs = (
        {'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Origin': 'https://y.qq.com', 'Content-Type': 'application/json'}
        if _anon else _qq_headers()
    )
    payload = {
        'comm': comm,
        'req_0': {
            'module': 'vkey.GetVkeyServer',
            'method': 'CgiGetVkey',
            'param': {
                'guid': '10000',
                'songmid': [mid] * len(filenames),
                'filename': filenames,
                'songtype': [0] * len(filenames),
                'uin': uin,
                'loginflag': 1,
                'platform': '20',
            },
        },
    }
    try:
        req = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            data=json.dumps(payload).encode('utf-8'),
            headers=hdrs,
        )
        with _safe_urlopen(req, timeout=5) as r:
            vdata = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
            d0 = vdata.get('req_0', {}).get('data', {})
            sips = d0.get('sip') if isinstance(d0.get('sip'), list) else []
            sip = next(
                (u.strip() for u in sips if isinstance(u, str) and _is_safe_stream_url(u.strip())),
                'https://dl.stream.qqmusic.qq.com/',
            )
            if not sip.endswith('/'):
                sip += '/'
            purl_by_fn = {
                info.get('filename'): info.get('purl')
                for info in (d0.get('midurlinfo') or [])
                if info.get('purl')
            }
            for filename, q_label in candidates:
                if purl_by_fn.get(filename):
                    cand_url = sip + str(purl_by_fn[filename]).lstrip('/')
                    if not _is_safe_stream_url(cand_url):
                        continue
                    log.info('Resolved %s (%s) -> %s', mid, q_label, filename)
                    return {
                        'url': cand_url,
                        'quality': q_label,
                        'source': f'QQ音乐·{q_label}',
                        '_url_ts': time.monotonic(),
                        'headers': {'User-Agent': UA, 'Referer': 'https://y.qq.com/'},
                    }
    except Exception as e:
        log.warning('resolve_qq_vkey error: %s', e)

    if not _anon and not _retried_refresh and _refresh_musickey_from_cookies():
        return resolve_qq_vkey(mid, media_mid, _retried_refresh=True)
    if not _anon and has_ck:
        return resolve_qq_vkey(mid, media_mid, _retried_refresh=True, _anon=True)
    return None


_BILI_CJ = None
_BILI_CJ_TS = 0.0
_BILI_CJ_LOCK = threading.Lock()


def resolve_hires_fallback(query: str, title_hint: str = '', artist_hint: str = '') -> dict:
    """High-bitrate full-track stream resolver when a track is VIP-locked without a VIP key."""
    global _BILI_CJ, _BILI_CJ_TS
    try:
        if urllib.request.urlopen is _ORIG_URLOPEN and (
            not _has_only_global_ips('www.bilibili.com') or not _has_only_global_ips('api.bilibili.com')
        ):
            return None
        now = time.monotonic()
        with _BILI_CJ_LOCK:
            cj = _BILI_CJ
            need_init = cj is None or (now - _BILI_CJ_TS) > 3600.0
            if need_init:
                cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), _SafeRedirectHandler)
        opener.addheaders = [('User-Agent', UA), ('Referer', 'https://www.bilibili.com/')]
        if need_init:
            with opener.open('https://www.bilibili.com/', timeout=4) as r0:
                r0.read(64)
            with _BILI_CJ_LOCK:
                _BILI_CJ = cj
                _BILI_CJ_TS = time.monotonic()

        q = f'{query} 完整版 无损'
        surl = 'https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=' + urllib.parse.quote(q)
        with opener.open(surl, timeout=4) as r:
            sdata = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
            results = sdata.get('data', {}).get('result', [])
        if not results:
            return None

        t_hint = (title_hint or '').strip().lower()
        a_hint = (artist_hint or '').strip().lower()
        best_item = None
        best_score = -1
        for item in results[:8]:
            dur_str = str(item.get('duration', '0:0'))
            parts = [int(x) for x in dur_str.split(':') if x.isdigit()]
            if len(parts) == 3:
                secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                secs = parts[0] * 60 + parts[1]
            else:
                secs = 240
            if not (60 <= secs <= 720):
                continue
            raw_title = html.unescape(re.sub(r'<[^>]+>', '', str(item.get('title', '')))).lower()
            score = 0
            if t_hint and t_hint in raw_title:
                score += 2
            if a_hint and a_hint in raw_title:
                score += 1
            if any(w in raw_title for w in ('伴奏', 'ktv', '翻唱', '教学', 'cover')):
                score -= 2
            if score > best_score:
                best_score = score
                best_item = item
        if not best_item or (t_hint and best_score <= 0):
            return None
        chosen = best_item

        bvid = str(chosen.get('bvid') or '').strip()
        if not re.fullmatch(r'BV[0-9A-Za-z]{8,16}', bvid):
            return None
        vurl = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
        with opener.open(vurl, timeout=4) as vr:
            vdata = json.loads(_read_http_bytes(vr).decode('utf-8', errors='replace'))
        raw_cid = (vdata.get('data') or {}).get('cid')
        if isinstance(raw_cid, bool):
            return None
        cid = int(raw_cid)
        if cid <= 0:
            return None
        dur = int((vdata.get('data') or {}).get('duration') or 240)

        purl = f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&fnval=16'
        with opener.open(purl, timeout=4) as pr:
            pdata = json.loads(_read_http_bytes(pr).decode('utf-8', errors='replace'))
        dash_dur = int(((pdata.get('data') or {}).get('dash') or {}).get('duration') or 0)
        if 30 <= dash_dur <= 900:
            dur = dash_dur
        audios = pdata.get('data', {}).get('dash', {}).get('audio', [])
        if not audios:
            return None
        best = sorted((a for a in audios if isinstance(a, dict)), key=lambda x: x.get('bandwidth', 0), reverse=True)
        if not best:
            return None
        best = best[0]
        cand_urls = [
            *(best.get('backupUrl') if isinstance(best.get('backupUrl'), list) else []),
            *(best.get('backup_url') if isinstance(best.get('backup_url'), list) else []),
            best.get('baseUrl'),
            best.get('base_url'),
        ]
        stream_url = next(
            (u.strip() for u in cand_urls if isinstance(u, str) and _is_safe_stream_url(u.strip())),
            '',
        )
        if not stream_url:
            return None
        bw_kbps = best.get('bandwidth', 192000) // 1000
        return {
            'url': stream_url,
            'quality': f'Hi-Res ({bw_kbps}kbps)',
            'source': 'QQ音乐·Hi-Res音源',
            'headers': {'User-Agent': UA, 'Referer': 'https://www.bilibili.com/'},
            'duration': dur,
            '_url_ts': time.monotonic(),
        }
    except urllib.error.HTTPError as he:
        he.close()
        if he.code in (403, 412):
            with _BILI_CJ_LOCK:
                _BILI_CJ = None
                _BILI_CJ_TS = 0.0
        log.warning('Fallback audio error: %s', he)
        return None
    except Exception as e:
        log.warning('Fallback audio error: %s', e)
        return None


def qq_search(query: str, limit: int = 25, page: int = 1) -> list:
    if isinstance(query, (int, float)) and not isinstance(query, bool):
        query = str(query)
    elif not isinstance(query, str):
        query = ''
    lim = max(1, min(60, int(limit)))
    pg = max(1, min(50, int(page)))
    req_body = {
        'method': 'DoSearchForQQMusicDesktop',
        'module': 'music.search.SearchCgiService',
        'param': {
            'grp': 1,
            'num_per_page': lim,
            'page_num': pg,
            'query': query,
            'search_type': 0,
        },
    }

    def _call_search(use_cookies: bool):
        raw_uin = str(load_config().get('uin') or '0').strip() if use_cookies else '0'
        uin_val = int(raw_uin) if (raw_uin.isascii() and raw_uin.isdigit()) else 0
        payload = {'comm': {'ct': 19, 'cv': 1859, 'uin': uin_val}, 'req': req_body}
        hdrs = _qq_headers() if use_cookies else {
            'User-Agent': UA,
            'Referer': 'https://y.qq.com/',
            'Content-Type': 'application/json',
        }
        r_obj = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            data=json.dumps(payload).encode('utf-8'),
            headers=hdrs,
        )
        with _safe_urlopen(r_obj, timeout=5) as r:
            data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        r_node = data.get('req') or {}
        r_code = _musicu_code(data, r_node, 0)
        body = (r_node.get('data') or {}).get('body') or {}
        song_box = body.get('song') or {}
        meta = (r_node.get('data') or {}).get('meta') or {}
        songs = song_box.get('list') or []
        total_num = int(meta.get('sum') or song_box.get('totalnum') or 0)
        next_pg = int(meta.get('nextpage') or 0)
        return r_code, songs, (next_pg > 0 or total_num > pg * lim or len(songs) >= lim)

    def _call_shc_fallback():
        fb_url = (
            'https://shc.y.qq.com/soso/fcgi-bin/search_for_qq_cp'
            f'?format=json&inCharset=utf8&outCharset=utf-8&n={lim}&p={pg}&w='
            + urllib.parse.quote(query, safe='')
        )
        r_fb = urllib.request.Request(fb_url, headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/'})
        with _safe_urlopen(r_fb, timeout=5) as r:
            fb_data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        if fb_data.get('code', 0) != 0:
            return []
        return (((fb_data.get('data') or {}).get('song') or {}).get('list')) or []

    try:
        cfg = load_config()
        has_ck = bool(cfg.get('cookies_str') or cfg.get('qm_keyst'))
        code, songs, more_hint = _call_search(has_ck)
        if has_ck and (code != 0 or not songs):
            if code in (1000, 2001) and _refresh_musickey_from_cookies():
                code, songs, more_hint = _call_search(True)
            if code != 0 or not songs:
                code, songs, more_hint = _call_search(False)
        if code != 0 or not songs:
            songs = _call_shc_fallback()
            more_hint = len(songs) >= lim
        dq = _default_quality_label(cfg)
        out = [_format_qq_song(s, default_q=dq) for s in songs if isinstance(s, dict) and (s.get('mid') or s.get('songmid'))]
        hm_key = (query, lim, pg)
        with _SEARCH_HM_LOCK:
            if len(_SEARCH_HAS_MORE) >= 64 and hm_key not in _SEARCH_HAS_MORE:
                _SEARCH_HAS_MORE.pop(next(iter(_SEARCH_HAS_MORE)), None)
            _SEARCH_HAS_MORE[hm_key] = bool(more_hint and out)
        return out
    except Exception as e:
        log.warning('qq_search error: %s', e)
        return []


_SEARCH_HAS_MORE = {}
_SEARCH_HM_LOCK = threading.Lock()
_SMARTBOX_CACHE = {}
_SMARTBOX_LOCK = threading.Lock()


def _clean_smartbox_query(raw) -> str:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = str(raw)
    elif not isinstance(raw, str):
        return ''
    return re.sub(r'[\x00-\x1f\x7f]+', '', raw).strip()[:48]


def _get_cached_smartbox(q: str):
    if not q:
        return {'ok': True, 'words': [], 'items': []}
    now = time.monotonic()
    with _SMARTBOX_LOCK:
        hit = _SMARTBOX_CACHE.get(q)
        if hit and (now - hit[0]) < 180.0:
            return hit[1]
    return None


def qq_smartbox(query: str) -> dict:
    q = _clean_smartbox_query(query)
    cached = _get_cached_smartbox(q)
    if cached is not None:
        return cached
    url = (
        'https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg'
        '?is_xml=0&format=json&key=' + urllib.parse.quote(q, safe='')
        + '&g_tk=5381&loginUin=0&hostUin=0&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0'
    )
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/'})
    words = []
    seen_words = set()
    items = []
    seen_items = set()

    def _add_word(w: str):
        w = html.unescape(str(w or '')).strip()
        w_base = re.sub(r'\s*[\(（][^\)）]{1,24}[\)）]\s*$', '', w).strip() or w
        for cand in (w_base, w):
            if 1 <= len(cand) <= 24 and cand not in seen_words and len(words) < 12:
                seen_words.add(cand)
                words.append(cand)

    try:
        with _safe_urlopen(req, timeout=2.5) as r:
            data = (json.loads(_read_http_bytes(r).decode('utf-8', errors='replace')).get('data') or {})
        for sg in ((data.get('singer') or {}).get('itemlist') or [])[:4]:
            if not isinstance(sg, dict):
                continue
            name = html.unescape(str(sg.get('name') or '')).strip()[:80]
            if not name:
                continue
            _add_word(name)
            ik = f'singer:{name}'
            if ik not in seen_items and len(items) < 8:
                seen_items.add(ik)
                items.append({'type': 'singer', 'title': name, 'sub': '歌手', 'query': name})
        for so in ((data.get('song') or {}).get('itemlist') or [])[:8]:
            if not isinstance(so, dict):
                continue
            name = html.unescape(str(so.get('name') or '')).strip()[:100]
            singer = html.unescape(str(so.get('singer') or '')).strip()[:80]
            if not name:
                continue
            _add_word(name)
            if singer and '/' not in singer:
                _add_word(singer)
            ik = f'song:{name}:{singer}'
            if ik not in seen_items and len(items) < 8:
                seen_items.add(ik)
                q_str = f'{name} {singer}'.strip()[:120]
                items.append({'type': 'song', 'title': name, 'sub': singer or '单曲', 'query': q_str})
        res = {'ok': True, 'words': words, 'items': items}
        with _SMARTBOX_LOCK:
            if len(_SMARTBOX_CACHE) >= 96:
                _SMARTBOX_CACHE.pop(next(iter(_SMARTBOX_CACHE)), None)
            _SMARTBOX_CACHE[q] = (time.monotonic(), res)
        return res
    except Exception as e:
        log.debug('qq_smartbox error: %s', e)
        return {'ok': True, 'words': [], 'items': []}


def qq_toplist(top_id: int = 26, limit: int = 40) -> list:
    req_1 = {
        'module': 'musicToplist.ToplistInfoServer',
        'method': 'GetDetail',
        'param': {'topId': int(top_id), 'offset': 0, 'num': limit, 'period': ''},
    }
    try:
        cfg = load_config()
        has_ck = bool(cfg.get('cookies_str') or cfg.get('qm_keyst'))
        payload = {'comm': _qq_comm(), 'req_1': req_1}
        req = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            data=json.dumps(payload).encode('utf-8'),
            headers=_qq_headers(),
        )
        with _safe_urlopen(req, timeout=6) as r:
            data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        r1 = data.get('req_1') or {}
        r1_code = _musicu_code(data, r1, 0)
        songs = (r1.get('data') or {}).get('songInfoList') or []
        if has_ck and (r1_code != 0 or not songs):
            if r1_code in (1000, 2001) and _refresh_musickey_from_cookies():
                req_retry = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': _qq_comm(), 'req_1': req_1}).encode('utf-8'),
                    headers=_qq_headers(),
                )
                with _safe_urlopen(req_retry, timeout=6) as rr:
                    dr = json.loads(_read_http_bytes(rr).decode('utf-8', errors='replace'))
                rr1 = dr.get('req_1') or {}
                r1_code = rr1.get('code') if 'code' in rr1 else dr.get('code', 0)
                songs = (rr1.get('data') or {}).get('songInfoList') or songs
            if r1_code != 0 or not songs:
                req_anon = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': {'ct': 24, 'cv': 0}, 'req_1': req_1}).encode('utf-8'),
                    headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'},
                )
                with _safe_urlopen(req_anon, timeout=6) as r2:
                    d2 = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
                songs = ((d2.get('req_1') or {}).get('data') or {}).get('songInfoList') or songs
        dq = _default_quality_label(cfg)
        return [_format_qq_song(s, default_q=dq) for s in songs if s.get('mid')]
    except Exception as e:
        log.warning('qq_toplist error: %s', e)
        return []


_LYRIC_CACHE = {}
_LYRIC_LOCK = threading.Lock()


def qq_lyric(mid: str) -> list:
    """Fetch and parse LRC synchronized lyrics from QQ Music."""
    if not isinstance(mid, str) or not mid.strip():
        return []
    mid = mid.strip()
    with _LYRIC_LOCK:
        cached = _LYRIC_CACHE.get(mid)
    if cached is not None:
        return list(cached)
    q_mid = urllib.parse.quote(mid, safe='')
    url = f'https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={q_mid}&format=json&nobase64=1'
    cfg_l = load_config()
    has_ck_l = bool((cfg_l.get('cookies_str') or cfg_l.get('qm_keyst') or '').strip())

    def _parse_lrc_data(d: dict) -> list:
        raw = html.unescape(str(d.get('lyric') or '')).replace('\r\n', '\n').replace('\r', '\n')
        parsed = []
        for line in raw.splitlines():
            s = line.strip()
            tags = re.findall(r'\[(\d+):(\d+(?:[.:]\d+)?)\]', s)
            if not tags:
                continue
            txt = re.sub(r'^(?:\[\d+:\d+(?:[.:]\d+)?\])+', '', s).strip()
            if txt and not txt.startswith('offset:'):
                for mm, ss in tags:
                    parsed.append({'time': round(int(mm) * 60 + float(ss.replace(':', '.')), 2), 'text': txt})
        parsed.sort(key=lambda x: x['time'])
        return parsed

    try:
        req = urllib.request.Request(url, headers=_qq_headers())
        with _safe_urlopen(req, timeout=4) as r:
            data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        lines = _parse_lrc_data(data)
        if not lines and has_ck_l and data.get('retcode', data.get('code')) in (1000, 2001):
            req_anon = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/'})
            with _safe_urlopen(req_anon, timeout=4) as r2:
                data2 = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
            lines2 = _parse_lrc_data(data2)
            if lines2 or data2.get('retcode', data2.get('code')) == 0:
                data, lines = data2, lines2
        # 2026-11-07 Claude: cache only real answers. An error reply (QQ fails now and then; a bad mid
        # gives retcode 1101) has no lyric and must not pin "no lyrics" on the song until a restart.
        if lines or data.get('retcode', data.get('code')) == 0:
            with _LYRIC_LOCK:
                if len(_LYRIC_CACHE) >= 64:
                    _LYRIC_CACHE.pop(next(iter(_LYRIC_CACHE)), None)
                _LYRIC_CACHE[mid] = list(lines)
        return lines
    except Exception as e:
        log.warning('qq_lyric error: %s', e)
        return []


def _ambient_file_for(preset: str):
    for ext in AMBIENT_EXTS:  # 2026-11-12: the seamless FLAC loop when present, else the old AAC one
        try:
            p = AMBIENT_DIR / f'{preset}{ext}'
            if p.exists() and not p.is_symlink() and p.is_file() and p.stat().st_size > 16384:
                return p
        except OSError:
            pass
    return None


def _fallback_ambient_pcm(preset: str) -> bytes:
    """Offline fallback 4.0s 48kHz stereo float32 loop when cached .m4a is absent (e.g. unit tests)."""
    n = OUT_RATE * 4
    np = _get_np()
    if np is None:
        buf = _array('f', [0.0] * (n * 2))
        for i in range(0, min(n, 4800)):
            v = 0.04 * math.sin(2.0 * math.pi * 220.0 * i / OUT_RATE)
            buf[2 * i] = v
            buf[2 * i + 1] = v
        return buf.tobytes()
    rng = np.random.default_rng({
        'rain': 101, 'waves': 303, 'forest': 404, 'stream': 455, 'night': 505, 'sleep_piano': 606,
        'fireplace': 717, 'library': 828, 'singing_bowl': 939,
    }.get(preset, 707))
    freqs = np.fft.rfftfreq(n, d=1.0 / OUT_RATE)
    f_safe = np.maximum(freqs, 0.25)
    env = (f_safe ** (-0.65)).astype(np.float32) / (1.0 + (120.0 / f_safe) ** 3) / (1.0 + (f_safe / 3600.0) ** 3)
    env[0] = 0.0
    ph_l = rng.uniform(0.0, 2.0 * math.pi, size=freqs.shape[0])
    ph_r = rng.uniform(0.0, 2.0 * math.pi, size=freqs.shape[0])
    ch_l = np.fft.irfft(env * (np.cos(ph_l) + 1j * np.sin(ph_l)), n=n).astype(np.float32)
    ch_r = np.fft.irfft(env * (np.cos(ph_r) + 1j * np.sin(ph_r)), n=n).astype(np.float32)
    ch_l = (ch_l / max(1e-6, float(np.std(ch_l)))) * 0.08
    ch_r = (ch_r / max(1e-6, float(np.std(ch_r)))) * 0.08
    stereo = np.empty((n, 2), dtype=np.float32)
    stereo[:, 0] = np.clip(ch_l, -0.7, 0.7)
    stereo[:, 1] = np.clip(ch_r, -0.7, 0.7)
    return stereo.tobytes()


_USER_PLS_CACHE = {'key': None, 'ts': 0.0, 'ttl': 180.0, 'val': None}
_USER_PLS_LOCK = threading.Lock()
_LOCAL_ALL_CACHE = {'sig': None, 'val': None}
_LOCAL_ALL_LOCK = threading.Lock()


def qq_user_playlists(force: bool = False) -> list:
    cfg = load_config()
    uin = str(cfg.get('uin') or '').strip()
    if not uin or not (uin.isascii() and uin.isdigit()):
        return []
    use_ram_cache = (not force and _safe_urlopen is _ORIG_SAFE_URLOPEN and urllib.request.urlopen is _ORIG_URLOPEN)
    pls_key = (uin, str(cfg.get('qm_keyst') or '')[:16], _DIR201_GEN)
    if use_ram_cache:
        with _USER_PLS_LOCK:
            if (
                _USER_PLS_CACHE['key'] == pls_key
                and _USER_PLS_CACHE['val'] is not None
                and (time.monotonic() - _USER_PLS_CACHE['ts']) < _USER_PLS_CACHE.get('ttl', 180.0)
            ):
                return list(_USER_PLS_CACHE['val'])
    fav_out = []
    created_out = []
    created_ok = [False]
    created_http_ok = [False]
    dir201_http_ok = False
    created_lock = threading.Lock()
    has_ck = bool((cfg.get('cookies_str') or cfg.get('qm_keyst') or '').strip())

    def _fetch_created(use_cookies: bool = True):
        # 2. Fetch user-created playlists via fcg_user_created_diss
        try:
            url2 = f'https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss?hostuin={uin}&sin=0&size=40&r={int(time.time())}&format=json&utf8=1'
            hdrs = _qq_headers() if use_cookies else {'User-Agent': UA}
            hdrs['Referer'] = 'https://y.qq.com/portal/profile.html'
            req2 = urllib.request.Request(url2, headers=hdrs)
            with _safe_urlopen(req2, timeout=5) as r2:
                j2 = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
                d2 = j2.get('data') or {}
                ok_code = j2.get('code', j2.get('retcode', 0)) == 0 and ('disslist' in d2 or j2.get('code', 0) == 0)
                batch = []
                for item in d2.get('disslist') or []:
                    tid = item.get('tid')
                    cnt = int(item.get('song_cnt') or 0)
                    name = (item.get('diss_name') or '').strip()
                    if not tid or cnt <= 0 or name in ('我喜欢', '默认收藏', 'QZone背景音乐'):
                        continue
                    batch.append({
                        'id': str(tid),
                        'dissid': str(tid),
                        'name': f'🎵 {name}',
                        'count': cnt,
                        'cover': item.get('diss_cover') or '',
                    })
                with created_lock:
                    created_http_ok[0] = True
                    if ok_code:
                        created_ok[0] = True
                    if batch and not created_out:
                        created_out.extend(batch)
        except Exception as e:
            log.warning('qq_user_playlists created_diss error: %s', e)

    t_created = threading.Thread(target=_fetch_created, args=(has_ck,), daemon=True)
    t_created.start()

    # 1. Fetch "我喜欢" (dirid=201) via music.srfDissInfo.aiDissInfo
    global _DIR201_CACHE
    with _DIR201_LOCK:
        d201_gen = _DIR201_GEN
    refreshed_ck = False
    try:
        req_body = {
            'module': 'music.srfDissInfo.aiDissInfo',
            'method': 'uniform_get_Dissinfo',
            'param': {'dirid': 201, 'hostUin': int(uin), 'sin': 0, 'size': 160, 'orderlist': 1},
        }

        def _req_dir201(use_ck: bool):
            comm = _qq_comm() if use_ck else {'ct': 24, 'cv': 0, 'uin': uin, 'format': 'json'}
            hdrs = _qq_headers() if use_ck else {'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'}
            req1 = urllib.request.Request(
                'https://u.y.qq.com/cgi-bin/musicu.fcg',
                data=json.dumps({'comm': comm, 'req': req_body}).encode('utf-8'),
                headers=hdrs,
            )
            with _safe_urlopen(req1, timeout=5) as r1:
                j1 = json.loads(_read_http_bytes(r1).decode('utf-8', errors='replace'))
            rn = j1.get('req') or {}
            rc = _musicu_code(j1, rn, 0)
            return rc, (rn.get('data') or {})

        r_code, d1 = _req_dir201(has_ck)
        dir201_http_ok = True
        if has_ck and r_code in (1000, 2001) and _refresh_musickey_from_cookies():
            refreshed_ck = True
            cfg = load_config()
            r_code, d1 = _req_dir201(True)
        if has_ck and r_code != 0:
            r_code, d1 = _req_dir201(False)

        if r_code == 0 and d1:
            dirinfo = d1.get('dirinfo') or {}
            songnum = dirinfo.get('songnum') or d1.get('total_song_num') or 0
            diss_id = dirinfo.get('id') or 'dir_201'
            dq = _default_quality_label(cfg)
            songs = d1.get('songlist') or []
            with _DIR201_LOCK:
                if _DIR201_GEN == d201_gen:
                    _DIR201_CACHE = (uin, dq, time.monotonic(), [_format_qq_song(s, default_q=dq) for s in songs[:160] if s.get('mid')])
            fav_out.append({
                'id': 'dir_201',
                'dissid': str(diss_id),
                'name': '❤️ 我喜欢',
                'count': int(songnum),
                'cover': dirinfo.get('picurl') or 'https://y.gtimg.cn/mediastyle/y/img/cover_love_300.jpg',
            })
    except Exception as e:
        log.warning('Failed to fetch dirid=201: %s', e)

    t_created.join(timeout=6.0)
    with created_lock:
        ok1 = created_ok[0]
    if not ok1 and refreshed_ck:
        _fetch_created(True)
        with created_lock:
            ok1 = created_ok[0]
    if not ok1 and has_ck:
        _fetch_created(False)
    with created_lock:
        res_pls = fav_out + list(created_out)
        http_ok_any = dir201_http_ok or created_http_ok[0]
    if refreshed_ck:
        pls_key = (uin, str(cfg.get('qm_keyst') or '')[:16], _DIR201_GEN)
    if use_ram_cache and (res_pls or http_ok_any):
        with _USER_PLS_LOCK:
            _USER_PLS_CACHE['key'] = pls_key
            _USER_PLS_CACHE['ts'] = time.monotonic()
            _USER_PLS_CACHE['ttl'] = 180.0 if res_pls else 60.0
            _USER_PLS_CACHE['val'] = list(res_pls)
    return res_pls


# ---- 2026-11-12 (Claude): 我喜欢 (dirid 201) for the playing song ----------------------------------------------------
_SONG_ID_CACHE = {}   # mid -> (numeric song id, song type); playlist writes take the numeric id
_SONG_ID_LOCK = threading.Lock()


def _qq_client_comm(cfg: dict = None) -> dict:
    """The comm the QQ Music client sends: playlist writes refuse the web-style one (code 80105)."""
    cfg = cfg or load_config()
    return {'ct': '11', 'cv': '13020508', 'v': '13020508', 'tmeAppID': 'qqmusic', 'format': 'json',
            'inCharset': 'utf-8', 'outCharset': 'utf-8', 'qq': str(cfg.get('uin') or ''),
            'authst': str(cfg.get('qm_keyst') or ''), 'tmeLoginType': '1' if _is_wx_login(cfg) else '2'}


def _musicu_code(j: dict, rq: dict, default=0):
    top = j.get('code') if isinstance(j, dict) else None
    sub = rq.get('code') if isinstance(rq, dict) and 'code' in rq else None
    if top in (1000, 2001):
        return top
    if sub is None:
        return default if top is None else top
    if sub == 0 and top not in (None, 0):
        return top
    return sub


def _qq_musicu_one(comm: dict, req: dict, timeout: float = 8) -> tuple:
    """One musicu.fcg call: (code, data)."""
    body = json.dumps({'comm': comm, 'req_1': req}).encode('utf-8')
    use_cookies = bool(isinstance(comm, dict) and any(k in comm for k in ('authst', 'qq', 'uin')))
    hdrs = _qq_headers() if use_cookies else {
        'User-Agent': UA,
        'Referer': 'https://y.qq.com/',
        'Origin': 'https://y.qq.com',
        'Content-Type': 'application/json',
    }
    r = urllib.request.Request('https://u.y.qq.com/cgi-bin/musicu.fcg', data=body, headers=hdrs)
    with _safe_urlopen(r, timeout=timeout) as resp:
        j = json.loads(_read_http_bytes(resp).decode('utf-8', errors='replace'))
    rq = j.get('req_1') if isinstance(j.get('req_1'), dict) else {}
    return _musicu_code(j, rq, None), (rq.get('data') if isinstance(rq.get('data'), dict) else {})


def qq_song_id(mid: str) -> tuple:
    """(numeric id, type) of a QQ song, or (None, None)."""
    with _SONG_ID_LOCK:
        hit = _SONG_ID_CACHE.get(mid)
    if hit:
        return hit
    try:
        code, d = _qq_musicu_one({'ct': 24, 'cv': 0, 'format': 'json'},
                                 {'module': 'music.pf_song_detail_svr', 'method': 'get_song_detail_yqq', 'param': {'song_mid': mid}})
    except Exception as e:   # noqa: BLE001
        log.warning('song id lookup failed: %s', type(e).__name__)
        return None, None
    ti = d.get('track_info') if isinstance(d.get('track_info'), dict) else {}
    sid, stype = ti.get('id'), ti.get('type')
    if code != 0 or type(sid) is not int or sid <= 0:
        return None, None
    res = (sid, stype if type(stype) is int else 0)
    with _SONG_ID_LOCK:
        if len(_SONG_ID_CACHE) > 500:
            _SONG_ID_CACHE.clear()
        _SONG_ID_CACHE[mid] = res
    return res


def qq_fav_state(mid: str):
    """True / False: is the song in 我喜欢; None when it cannot be told (no login, network)."""
    cfg = load_config()
    if not cfg.get('uin') or not cfg.get('qm_keyst'):
        return None
    req = {'module': 'music.musicasset.SongFavRead', 'method': 'IsSongFanByMid', 'param': {'v_songMid': [mid]}}
    try:
        code, d = _qq_musicu_one(_qq_client_comm(cfg), req, 6)
        if code in (1000, 2001) and _refresh_musickey_from_cookies():
            code, d = _qq_musicu_one(_qq_client_comm(), req, 6)
    except Exception as e:   # noqa: BLE001
        log.warning('我喜欢 state failed: %s', type(e).__name__)
        return None
    fan = d.get('m_fan') if isinstance(d.get('m_fan'), dict) else {}
    return bool(fan.get(mid)) if code == 0 and mid in fan else None


def qq_fav_set(mid: str, on: bool) -> dict:
    """Add the song to 我喜欢 (on) or take it out."""
    global _DIR201_CACHE, _DIR201_GEN
    cfg = load_config()
    if not cfg.get('uin') or not cfg.get('qm_keyst'):
        return {'ok': False, 'login': False, 'error': '还没有登录 QQ 音乐，请在「账号信息」里扫码登录'}
    sid, stype = qq_song_id(mid)
    if not sid:
        return {'ok': False, 'error': '这首歌查不到编号，没法收藏'}
    req = {'module': 'music.musicasset.PlaylistDetailWrite', 'method': 'AddSonglist' if on else 'DelSonglist',
           'param': {'dirId': 201, 'v_songInfo': [{'songType': stype, 'songId': sid}]}}
    try:
        code, _ = _qq_musicu_one(_qq_client_comm(cfg), req)
        if code in (1000, 2001) and _refresh_musickey_from_cookies():
            code, _ = _qq_musicu_one(_qq_client_comm(), req)
    except Exception as e:   # noqa: BLE001
        log.warning('我喜欢 write failed: %s', type(e).__name__)
        return {'ok': False, 'error': '连不上 QQ 音乐，请稍后再试'}
    if code in (1000, 2001):
        return {'ok': False, 'login': False, 'error': 'QQ 音乐登录过期了，请在「账号信息」里重新扫码登录'}
    if code != 0:
        log.warning('我喜欢 write refused: code %s', code)
        return {'ok': False, 'error': f'QQ 音乐没有接受（{code}），请稍后再试'}
    with _DIR201_LOCK:
        _DIR201_CACHE = None
        _DIR201_GEN += 1
    log.info('我喜欢: %s %s', 'added' if on else 'removed', mid)
    return {'ok': True, 'mid': mid, 'liked': bool(on), 'message': '已加入「我喜欢」' if on else '已从「我喜欢」移除'}


def qq_playlist_tracks(dissid: str, limit: int = 160) -> list:
    global _DIR201_CACHE
    cfg = load_config()
    uin = str(cfg.get('uin') or '0').strip()
    dissid_str = str(dissid or '').strip()
    has_ck = bool((cfg.get('cookies_str') or cfg.get('qm_keyst') or '').strip())

    # 1. If "我喜欢" (dir_201), fetch via uniform_get_Dissinfo dirid=201
    if dissid_str == 'dir_201' and uin.isascii() and uin.isdigit():
        dq = _default_quality_label(cfg)
        with _DIR201_LOCK:
            c201 = _DIR201_CACHE
            d201_gen = _DIR201_GEN
        if c201 and c201[0] == uin and c201[1] == dq and (time.monotonic() - c201[2]) < 20.0 and limit <= 160:
            return list(c201[3][:limit])
        try:
            req_body = {
                'module': 'music.srfDissInfo.aiDissInfo',
                'method': 'uniform_get_Dissinfo',
                'param': {'dirid': 201, 'hostUin': int(uin), 'sin': 0, 'size': limit, 'orderlist': 1},
            }

            def _req_tracks201(use_ck: bool):
                comm = _qq_comm() if use_ck else {'ct': 24, 'cv': 0, 'uin': uin, 'format': 'json'}
                hdrs = _qq_headers() if use_ck else {'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'}
                req1 = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': comm, 'req': req_body}).encode('utf-8'),
                    headers=hdrs,
                )
                with _safe_urlopen(req1, timeout=6) as r1:
                    j1 = json.loads(_read_http_bytes(r1).decode('utf-8', errors='replace'))
                rn = j1.get('req') or {}
                rc = _musicu_code(j1, rn, 0)
                return rc, (rn.get('data') or {})

            r_code, d1 = _req_tracks201(has_ck)
            if has_ck and r_code in (1000, 2001) and _refresh_musickey_from_cookies():
                cfg = load_config()
                r_code, d1 = _req_tracks201(True)
            if has_ck and r_code != 0:
                r_code, d1 = _req_tracks201(False)
            if r_code == 0 and d1:
                songs = d1.get('songlist') or []
                dq = _default_quality_label(cfg)
                out = [_format_qq_song(s, default_q=dq) for s in songs[:limit] if s.get('mid')]
                if limit >= 160:
                    with _DIR201_LOCK:
                        if _DIR201_GEN == d201_gen:
                            _DIR201_CACHE = (uin, dq, time.monotonic(), list(out[:160]))
                return out
            return []
        except Exception as e:
            log.warning('qq_playlist_tracks dir_201 error: %s', e)
            return []

    # 2. Normal playlist dissid
    if not dissid_str:
        return []
    q_dissid = urllib.parse.quote(dissid_str, safe='')
    q_uin = urllib.parse.quote(uin if (uin.isascii() and uin.isdigit()) else '0', safe='')
    url = (
        f'https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg'
        f'?type=1&json=1&utf8=1&onlysong=0&new_format=1&disstid={q_dissid}&loginUin={q_uin}&format=json'
    )

    def _req_diss(use_ck: bool):
        hdrs = _qq_headers() if use_ck else {'User-Agent': UA, 'Referer': 'https://y.qq.com/'}
        with _safe_urlopen(urllib.request.Request(url, headers=hdrs), timeout=6) as r:
            return json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))

    try:
        data = _req_diss(has_ck)
        if has_ck and data.get('code') in (1000, 2001) and _refresh_musickey_from_cookies():
            cfg = load_config()
            data = _req_diss(True)
        if has_ck and not (data.get('cdlist') or []):
            data = _req_diss(False)
        cdlist = data.get('cdlist') or []
        if cdlist:
            songs = cdlist[0].get('songlist') or []
            dq = _default_quality_label(cfg)
            return [_format_qq_song(s, default_q=dq) for s in songs[:limit] if s.get('mid')]
    except Exception as e:
        log.warning('qq_playlist_tracks error: %s', e)
    return []


def local_lib_sig() -> str:
    """本机歌曲文件夹（不含 U 盘，U 盘看 usb_sig）有没有变：加歌、删歌、改名都会改文件夹的 mtime。两次 stat，开销很小。"""
    parts = []
    for d in LOCAL_DIRS:
        if d == USB_MNT_DIR:
            continue
        try:
            st = d.stat()
            parts.append(f'{st.st_ino}:{st.st_mtime_ns}')
        except OSError:
            parts.append('0')
    return '|'.join(parts)


def usb_mount_sig(mnt_dir=None) -> str:
    target = mnt_dir if mnt_dir is not None else USB_MNT_DIR
    try:
        st = target.stat()
        parent_st = target.parent.stat()
        if st.st_dev == parent_st.st_dev:
            return '0'
        return f'{st.st_dev}:{st.st_ino}:{int(st.st_mtime_ns)}'
    except OSError:
        return '0'


def search_local(query: str = '', limit: int = 50) -> list:
    # /home/ubuntu/Music/usb points back to /mnt/usb. Follow directory links only inside
    # allowed LOCAL_DIRS roots, visit each inode once, and bound work before returning to the UI.
    # 2026-11-20 Claude（付总要求）：大屏歌单和语音点歌传 limit=500（默认仍 50）；结果按路径自然排序
    # （同一文件夹在一起，01、02…10 按数字），不再是目录读出来的乱序。
    def _sorted(lst):
        import re as _re
        lst.sort(key=lambda t: [int(p) if p.isdigit() else p for p in _re.split(r'(\d+)', str(t.get('url', '')).lower())])
        return lst

    q = (query or '').strip().lower()
    cap = max(1, int(limit or 50))
    scan_cap = max(cap, 2000)
    skip_dirs = {'$recycle.bin', 'system volume information'}
    allowed_roots = []
    for d in LOCAL_DIRS:
        try:
            if d.exists():
                allowed_roots.append(d.resolve())
        except OSError:
            continue
    if not allowed_roots:
        return []
    out = []
    seen_dirs = set()
    seen_files = set()
    pending = list(reversed(LOCAL_DIRS))
    music_hints = ('music', 'audio', 'songs', 'flac', 'mp3', '音乐', '歌曲')
    entries = 0
    while pending and len(seen_dirs) < 1024 and entries < 10000 and len(out) < scan_cap:
        directory = Path(pending.pop())
        try:
            dir_rp = directory.resolve()
            if not any(dir_rp.is_relative_to(rt) for rt in allowed_roots):
                continue
            st = directory.stat()
            identity = (st.st_dev, st.st_ino)
            if identity in seen_dirs:
                continue
            seen_dirs.add(identity)
            subdirs = []
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > 10000:
                        break
                    p = Path(child.path)
                    cname = p.name
                    if cname.startswith('.') or cname.lower() in skip_dirs:
                        continue
                    try:
                        if child.is_dir(follow_symlinks=True):
                            child_rp = p.resolve()
                            if any(child_rp.is_relative_to(rt) for rt in allowed_roots):
                                subdirs.append(p)
                            continue
                        if p.suffix.lower() not in AUDIO_EXTS:
                            continue
                        rp = p.resolve()
                        if not rp.is_file() or not any(rp.is_relative_to(rt) for rt in allowed_roots):
                            continue
                        if q and q not in p.stem.lower() and not any(
                            rp.is_relative_to(rt) and q in '/'.join(rp.relative_to(rt).parts).lower()
                            for rt in allowed_roots
                        ):
                            continue
                        st = rp.stat()
                        identity = (st.st_dev, st.st_ino)
                        if identity in seen_files:
                            continue
                        seen_files.add(identity)
                        out.append({
                            'mid': '',
                            'title': p.stem,
                            'artist': 'U盘/本地音乐',
                            'album': p.parent.name,
                            'cover': '',
                            'duration': 0,
                            'source': 'U盘无损/本地' if '/mnt/usb' in str(rp) else '本地音乐',
                            'quality': p.suffix.upper().lstrip('.'),
                            'url': str(rp),
                            'headers': {},
                            'is_local': True,
                        })
                        if len(out) >= scan_cap:
                            break
                    except OSError:
                        continue
            if subdirs:
                subdirs.sort(
                    key=lambda d: (0 if any(h in d.name.lower() for h in music_hints) else 1, d.name.lower()),
                    reverse=True,
                )
                pending.extend(subdirs)
        except OSError:
            continue
    return _sorted(out)[:cap]


_ORIG_SEARCH_LOCAL = search_local


# ==================== Internet Radio R3: National/Local/HK-MO FM + Playable Kids/English/Crosstalk + Resume & Episodes ====================
import base64 as _b64

RADIO_CATEGORIES = [
    {'id': 'all', 'name': '全部精选'},
    {'id': 'fm', 'name': '全国广播'},
    {'id': 'music_fm', 'name': '音乐电台'},
    {'id': 'kids', 'name': '故事英语'},
    {'id': 'crosstalk', 'name': '相声评书'},
    {'id': 'fav', 'name': '收藏电台'},
]


def _radio_emblem_uri(bg1: str, bg2: str, accent: str, badge: str, mark: str) -> str:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{bg1}"/><stop offset="1" stop-color="{bg2}"/>'
        f'</linearGradient></defs>'
        f'<rect width="200" height="200" rx="28" fill="url(#g)"/>'
        f'<circle cx="164" cy="38" r="56" fill="none" stroke="{accent}" stroke-opacity="0.16" stroke-width="8"/>'
        f'<circle cx="164" cy="38" r="32" fill="none" stroke="{accent}" stroke-opacity="0.24" stroke-width="6"/>'
        f'<rect x="18" y="24" width="92" height="30" rx="15" fill="{accent}" fill-opacity="0.2"/>'
        f'<text x="64" y="45" text-anchor="middle" fill="{accent}" font-family="Noto Sans CJK SC,sans-serif" font-size="15" font-weight="700">{badge}</text>'
        f'<text x="100" y="132" text-anchor="middle" fill="#ffffff" font-family="Noto Sans CJK SC,sans-serif" font-size="50" font-weight="700">{mark}</text>'
        f'</svg>'
    )
    return 'data:image/svg+xml;base64,' + _b64.b64encode(svg.encode('utf-8')).decode('ascii')


RADIO_STATIONS = [
    # --- 高品质音乐电台精选 (music_fm, 28 HTTPS AAC/HE-AAC HD Music Stations) ---
    {
        'id': 'cnr_yyzs', 'cat': 'music_fm', 'kind': 'live',
        'title': '音乐之声', 'sub': '央广华语流行金曲伴听', 'badge': '48k 高清', 'mark': '音乐',
        'colors': ('#13463a', '#0c2420', '#6ff2c5'),
        'url': 'https://ngcdn001.cnr.cn/live/yyzs/index.m3u8',
        'aliases': ('音乐之声', '央广音乐之声', 'musicradio', '音乐广播'),
    },
    {
        'id': 'xm_2629', 'cat': 'music_fm', 'kind': 'live',
        'title': '怀旧金曲广播', 'sub': '80与90年代经典老歌', 'badge': '经典老歌', 'mark': '怀旧',
        'colors': ('#4a2f1d', '#24170e', '#ffc299'),
        'url': 'https://live.ximalaya.com/radio-first-page-app/live/2629/64.m3u8',
        'aliases': ('怀旧金曲', '怀旧金曲广播', '怀旧音乐广播', '老歌电台', '经典老歌电台'),
    },
    {
        'id': 'asia_pop', 'cat': 'music_fm', 'kind': 'live',
        'title': '亚洲音乐台', 'sub': 'AsiaFM全天无广告热歌', 'badge': 'AsiaFM', 'mark': '亚音',
        'colors': ('#17444a', '#0c2226', '#7cecf5'),
        'url': 'https://ls.qingting.fm/live/15318569/64k.m3u8',
        'aliases': ('亚洲音乐台', 'asiafm', 'asiafm亚洲音乐'),
    },
    {
        'id': 'asia_classic', 'cat': 'music_fm', 'kind': 'live',
        'title': '亚洲经典台', 'sub': 'AsiaFM岁月华语金曲', 'badge': 'AsiaFM', 'mark': '经典',
        'colors': ('#4d3419', '#261a0c', '#ffd18a'),
        'url': 'https://ls.qingting.fm/live/15318341/64k.m3u8',
        'aliases': ('亚洲经典台', 'asiafm亚洲经典', '亚洲经典音乐台'),
    },
    {
        'id': 'asia_yue', 'cat': 'music_fm', 'kind': 'live',
        'title': '亚洲粤语台', 'sub': 'AsiaFM全天纯正港乐', 'badge': '粤语金曲', 'mark': '粤歌',
        'colors': ('#472139', '#24101d', '#ffa3d4'),
        'url': 'https://ls.qingting.fm/live/15318703/64k.m3u8',
        'aliases': ('亚洲粤语台', '粤语音乐台', '粤语金曲电台', '港乐电台'),
    },
    {
        'id': 'asia_hit', 'cat': 'music_fm', 'kind': 'live',
        'title': '亚洲热歌台', 'sub': 'AsiaFM潮流新歌热单', 'badge': '潮流热歌', 'mark': '热歌',
        'colors': ('#521f2c', '#291016', '#ff9eb5'),
        'url': 'https://ls.qingting.fm/live/20500149/64k.m3u8',
        'aliases': ('亚洲热歌台', '流行热歌电台', '热歌电台'),
    },
    {
        'id': 'sh_1017', 'cat': 'music_fm', 'kind': 'live',
        'title': '上海动感101', 'sub': '华语流行热歌FM101.7', 'badge': 'FM 101.7', 'mark': '101',
        'colors': ('#521f38', '#290f1c', '#ff9ec9'),
        'url': 'https://ls.qingting.fm/live/274/64k.m3u8',
        'aliases': ('上海动感101', '动感101'),
    },
    {
        'id': 'sh_1037', 'cat': 'music_fm', 'kind': 'live',
        'title': '上海最爱103.7', 'sub': 'LoveRadio经典情歌台', 'badge': 'FM 103.7', 'mark': '最爱',
        'colors': ('#4f2234', '#26111a', '#ffa6c4'),
        'url': 'https://ls.qingting.fm/live/273/64k.m3u8',
        'aliases': ('上海最爱103.7', 'loveradio', '上海loveradio', '最爱调频'),
    },
    {
        'id': 'gd_993', 'cat': 'music_fm', 'kind': 'live',
        'title': '广东音乐之声', 'sub': '粤语华语发烧金曲99.3', 'badge': 'FM 99.3', 'mark': '粤乐',
        'colors': ('#1a453e', '#0d2420', '#7cf0dc'),
        'url': 'https://ls.qingting.fm/live/1260/64k.m3u8',
        'aliases': ('广东音乐之声', 'fm993'),
    },
    {
        'id': 'sz_971', 'cat': 'music_fm', 'kind': 'live',
        'title': '深圳飞扬971', 'sub': '鹏城潮流流行金曲台', 'badge': 'FM 97.1', 'mark': '飞扬',
        'colors': ('#1b3d59', '#0d1f2e', '#8acbff'),
        'url': 'https://ls.qingting.fm/live/1271/64k.m3u8',
        'aliases': ('深圳飞扬971', '飞扬971', '深圳音乐广播'),
    },
    {
        'id': 'bj_974', 'cat': 'music_fm', 'kind': 'live',
        'title': '北京音乐97.4', 'sub': '首都流行欧美金曲97.4', 'badge': '48k 高清', 'mark': '京乐',
        'colors': ('#2a3359', '#151a2e', '#a6baff'),
        'url': 'https://brtv-radiolive.rbc.cn/alive/fm974.m3u8',
        'aliases': ('北京音乐广播', '北京音乐台', 'fm974'),
    },
    {
        'id': 'qt_4875', 'cat': 'music_fm', 'kind': 'live',
        'title': '怀旧好声音', 'sub': '年代经典华语老歌连播', 'badge': '岁月金曲', 'mark': '留声',
        'colors': ('#4a3219', '#24180c', '#ffd096'),
        'url': 'https://ls.qingting.fm/live/4875/64k.m3u8',
        'aliases': ('怀旧好声音', '年代经典电台'),
    },
    {
        'id': 'cnr_dszs', 'cat': 'music_fm', 'kind': 'live',
        'title': '经典音乐台', 'sub': '央广交响古典名曲赏析', 'badge': '48k 古典', 'mark': '古典',
        'colors': ('#1e2f5c', '#101830', '#9ec2ff'),
        'url': 'https://ngcdn001.cnr.cn/live/dszs/index.m3u8',
        'aliases': ('经典音乐广播', '经典音乐台', '古典音乐台'),
    },
    {
        'id': 'sh_947', 'cat': 'music_fm', 'kind': 'live',
        'title': '上海经典947', 'sub': '交响室内乐轻音乐调频', 'badge': 'FM 94.7', 'mark': '947',
        'colors': ('#283257', '#14192b', '#a8baff'),
        'url': 'https://ls.qingting.fm/live/267/64k.m3u8',
        'aliases': ('上海经典947', '经典947'),
    },
    {
        'id': 'qt_jazz', 'cat': 'music_fm', 'kind': 'live',
        'title': '爵士轻音乐台', 'sub': '慵懒爵士与午后纯音乐', 'badge': '爵士纯音', 'mark': '爵士',
        'colors': ('#2e284d', '#171426', '#c2b3ff'),
        'url': 'https://ls.qingting.fm/live/5022308/64k.m3u8',
        'aliases': ('爵士轻音乐台', '爵士电台', '轻音乐电台', '纯音乐电台'),
    },
    {
        'id': 'qt_4804', 'cat': 'music_fm', 'kind': 'live',
        'title': '古典音乐广播', 'sub': '钢琴协奏与古典纯音', 'badge': '古典纯音', 'mark': '琴韵',
        'colors': ('#243554', '#121a29', '#9ec2ff'),
        'url': 'https://ls.qingting.fm/live/4804/64k.m3u8',
        'aliases': ('古典音乐广播', '钢琴电台'),
    },
    {
        'id': 'qt_4915', 'cat': 'music_fm', 'kind': 'live',
        'title': '欧美金曲电台', 'sub': '欧美流行热单与经典', 'badge': '欧美流行', 'mark': '欧美',
        'colors': ('#1c3b57', '#0e1d2b', '#8ccaff'),
        'url': 'https://ls.qingting.fm/live/4915/64k.m3u8',
        'aliases': ('欧美金曲电台', '欧美音乐台', '英文歌电台'),
    },
    {
        'id': 'cri_nhzs', 'cat': 'music_fm', 'kind': 'live',
        'title': '南海之声', 'sub': '清新华语轻音乐伴听', 'badge': '48k 轻音', 'mark': '南海',
        'colors': ('#174540', '#0c2421', '#80f2e2'),
        'url': 'https://sk.cri.cn/nhzs.m3u8',
        'aliases': ('南海之声', '南海轻音乐'),
    },
    {
        'id': 'zj_968', 'cat': 'music_fm', 'kind': 'live',
        'title': '浙江动听968', 'sub': '华东旗舰潮流音乐调频', 'badge': 'FM 96.8', 'mark': '动听',
        'colors': ('#19453c', '#0d241f', '#7ef0d6'),
        'url': 'https://ls.qingting.fm/live/4866/64k.m3u8',
        'aliases': ('浙江动听968', '浙江音乐调频', '动听968'),
    },
    {
        'id': 'js_975', 'cat': 'music_fm', 'kind': 'live',
        'title': '江苏经典流行', 'sub': '岁月金曲伴听FM97.5', 'badge': 'FM 97.5', 'mark': '苏乐',
        'colors': ('#3d3319', '#1f1a0c', '#f5d487'),
        'url': 'https://ls.qingting.fm/live/4938/64k.m3u8',
        'aliases': ('江苏经典流行', '江苏经典流行音乐'),
    },
    {
        'id': 'js_897', 'cat': 'music_fm', 'kind': 'live',
        'title': '江苏魅力897', 'sub': '热歌新曲活力调频89.7', 'badge': 'FM 89.7', 'mark': '897',
        'colors': ('#47243d', '#24121f', '#f5a8de'),
        'url': 'https://ls.qingting.fm/live/4936/64k.m3u8',
        'aliases': ('江苏魅力897', '江苏音乐广播', '魅力897'),
    },
    {
        'id': 'sc_955', 'cat': 'music_fm', 'kind': 'live',
        'title': '四川岷江音乐', 'sub': '西南流行金曲FM95.5', 'badge': 'FM 95.5', 'mark': '岷江',
        'colors': ('#1a473a', '#0d241d', '#80f0cb'),
        'url': 'https://ls.qingting.fm/live/1111/64k.m3u8',
        'aliases': ('四川岷江音乐', '岷江音乐台', '岷江音乐广播'),
    },
    {
        'id': 'cq_1947', 'cat': 'music_fm', 'kind': 'live',
        'title': '重庆音乐广播', 'sub': '山城潮流金曲FM88.1', 'badge': 'FM 88.1', 'mark': '渝乐',
        'colors': ('#4f251e', '#26120f', '#ffab9c'),
        'url': 'https://ls.qingting.fm/live/1947/64k.m3u8',
        'aliases': ('重庆音乐广播', '重庆音乐台'),
    },
    {
        'id': 'cs_886', 'cat': 'music_fm', 'kind': 'live',
        'title': '长沙音乐88.6', 'sub': '湘江青春流行热歌调频', 'badge': 'FM 88.6', 'mark': '湘乐',
        'colors': ('#4d2922', '#261411', '#ffb3a3'),
        'url': 'https://ls.qingting.fm/live/4969/64k.m3u8',
        'aliases': ('长沙音乐88.6', '长沙音乐广播'),
    },
    {
        'id': 'nm_yyzs', 'cat': 'music_fm', 'kind': 'live',
        'title': '内蒙音乐之声', 'sub': '草原流行热歌FM93.6', 'badge': '内蒙93.6', 'mark': '蒙乐',
        'colors': ('#1b4540', '#0d2420', '#80f0de'),
        'url': 'https://ls.qingting.fm/live/1886/64k.m3u8',
        'aliases': ('内蒙音乐之声', '内蒙古音乐之声', '内蒙古音乐广播'),
    },
    {
        'id': 'nm_cyzs', 'cat': 'music_fm', 'kind': 'live',
        'title': '内蒙草原之声', 'sub': '天籁长调民歌与草原情', 'badge': '草原天籁', 'mark': '草原',
        'colors': ('#214726', '#102413', '#96f0a2'),
        'url': 'https://ls.qingting.fm/live/20973/64k.m3u8',
        'aliases': ('内蒙草原之声', '内蒙古草原之声', '草原之声'),
    },
    {
        'id': 'hk_rthk2', 'cat': 'music_fm', 'kind': 'live',
        'title': '香港电台二台', 'sub': 'RTHK港乐流行文化台', 'badge': 'RTHK 2', 'mark': '港二',
        'colors': ('#253459', '#121a2e', '#a3bdff'),
        'url': 'https://rthkradio2-live.akamaized.net/hls/live/2040078/radio2/index_64_a.m3u8',
        'aliases': ('香港电台第二台', '香港电台二台', '香港二台', 'rthk2'),
    },
    {
        'id': 'mo_fm995', 'cat': 'music_fm', 'kind': 'live',
        'title': '澳门绿邨电台', 'sub': '澳门FM99.5粤语金曲台', 'badge': '澳门99.5', 'mark': '澳门',
        'colors': ('#1b4532', '#0d241a', '#85f0bd'),
        'url': 'https://macaofm995.com/hls1/fm995.m3u8',
        'aliases': ('澳门电台', '澳门绿邨电台', '绿邨电台', '澳门广播'),
    },

    # --- 全国与本地综合广播电台 (fm, 22 HTTPS AAC/HE-AAC Stations) ---
    {
        'id': 'cnr_zgzs', 'cat': 'fm', 'kind': 'live',
        'title': '中国之声', 'sub': '央广权威新闻综合频率', 'badge': '央广新闻', 'mark': '央广',
        'colors': ('#5c1d24', '#2b1015', '#ff9b9b'),
        'url': 'https://ngcdn001.cnr.cn/live/zgzs/index.m3u8',
        'aliases': ('中国之声', '央广新闻', '新闻广播', '中央人民广播电台', '本地广播', '本地电台'),
    },
    {
        'id': 'cnr_jjzs', 'cat': 'fm', 'kind': 'live',
        'title': '经济之声', 'sub': '财经资讯与市场观察', 'badge': '财经 FM', 'mark': '财经',
        'colors': ('#4d3614', '#261b0a', '#ffd27d'),
        'url': 'https://ngcdn001.cnr.cn/live/jjzs/index.m3u8',
        'aliases': ('经济之声', '财经广播', '天下财经'),
    },
    {
        'id': 'tj_xiangsheng', 'cat': 'fm', 'kind': 'live',
        'title': '天津相声广播', 'sub': 'FM92.1全天名家相声台', 'badge': 'FM 92.1', 'mark': '津相',
        'colors': ('#54251c', '#29120e', '#ffab99'),
        'url': 'https://ls.qingting.fm/live/336/64k.m3u8',
        'aliases': ('天津相声广播', '相声广播', 'fm921', '天津相声台'),
    },
    {
        'id': 'bj_1039', 'cat': 'fm', 'kind': 'live',
        'title': '北京交通103.9', 'sub': '王牌路况与汽车脱口秀', 'badge': 'FM 103.9', 'mark': '交通',
        'colors': ('#1b3e59', '#0d1f2e', '#82d4ff'),
        'url': 'https://brtv-radiolive.rbc.cn/alive/fm1039.m3u8',
        'aliases': ('北京交通广播', '北京交通1039', '北京交通台', '1039交通广播'),
    },
    {
        'id': 'cnr_wyzs', 'cat': 'fm', 'kind': 'live',
        'title': '文艺之声', 'sub': '小说联播与人文品读', 'badge': '文艺 FM', 'mark': '文艺',
        'colors': ('#3d2452', '#1d1228', '#d9a8ff'),
        'url': 'https://ngcdn001.cnr.cn/live/wyzs/index.m3u8',
        'aliases': ('文艺之声', '文艺广播'),
    },
    {
        'id': 'cri_hqzx', 'cat': 'fm', 'kind': 'live',
        'title': '环球资讯', 'sub': '24小时全球整点快讯', 'badge': '国际台', 'mark': '环球',
        'colors': ('#173f52', '#0c202b', '#7fd8ff'),
        'url': 'https://sk.cri.cn/905.m3u8',
        'aliases': ('环球资讯', '环球资讯广播', '国际新闻广播'),
    },
    {
        'id': 'bj_1006', 'cat': 'fm', 'kind': 'live',
        'title': '北京新闻100.6', 'sub': '即时新闻与民生热点', 'badge': 'FM 100.6', 'mark': '京闻',
        'colors': ('#542520', '#291210', '#ffa599'),
        'url': 'https://brtv-radiolive.rbc.cn/alive/fm1006.m3u8',
        'aliases': ('北京新闻广播', '北京新闻台'),
    },
    {
        'id': 'bj_876', 'cat': 'fm', 'kind': 'live',
        'title': '北京文艺87.6', 'sub': '广播剧小说与相声联播', 'badge': 'FM 87.6', 'mark': '京艺',
        'colors': ('#45284d', '#221426', '#e2abff'),
        'url': 'https://brtv-radiolive.rbc.cn/alive/fm876.m3u8',
        'aliases': ('北京文艺广播', '北京文艺台'),
    },
    {
        'id': 'gd_1262', 'cat': 'fm', 'kind': 'live',
        'title': '广东珠江经济', 'sub': '粤语民生财经第一台', 'badge': 'FM 97.4', 'mark': '珠江',
        'colors': ('#473318', '#24190c', '#ffd08a'),
        'url': 'https://ls.qingting.fm/live/1262/64k.m3u8',
        'aliases': ('珠江经济台', '广东珠江经济', '珠江台'),
    },
    {
        'id': 'cnr_xczs', 'cat': 'fm', 'kind': 'live',
        'title': '中国乡村之声', 'sub': '民生乡情与戏曲曲艺', 'badge': '央广民生', 'mark': '乡音',
        'colors': ('#2e4720', '#172410', '#a6eb8a'),
        'url': 'https://ngcdn001.cnr.cn/live/xczs/index.m3u8',
        'aliases': ('中国乡村之声', '乡村之声'),
    },
    {
        'id': 'cnr_lnzs', 'cat': 'fm', 'kind': 'live',
        'title': '老年之声', 'sub': '健康养生与经典戏曲', 'badge': '长辈 FM', 'mark': '养生',
        'colors': ('#4a2c1d', '#24150e', '#ffb894'),
        'url': 'https://ngcdn001.cnr.cn/live/lnzs/index.m3u8',
        'aliases': ('老年之声', '长辈电台'),
    },
    {
        'id': 'cri_hyhq', 'cat': 'fm', 'kind': 'live',
        'title': '华语环球广播', 'sub': '全球华人时事与文化', 'badge': '国际台', 'mark': '华语',
        'colors': ('#1c3f47', '#0e2024', '#8ce6f5'),
        'url': 'https://sk.cri.cn/hyhq.m3u8',
        'aliases': ('华语环球', '华语环球广播'),
    },
    {
        'id': 'nm_hhht_jt', 'cat': 'fm', 'kind': 'live',
        'title': '呼市交通广播', 'sub': '呼和浩特实时路况生活', 'badge': '呼市97.1', 'mark': '呼交',
        'colors': ('#174736', '#0c241b', '#7ef2c2'),
        'url': 'https://ls.qingting.fm/live/5021545/64k.m3u8',
        'aliases': ('呼市交通广播', '呼和浩特交通广播', '呼和浩特广播'),
    },
    {
        'id': 'nm_hhht_zh', 'cat': 'fm', 'kind': 'live',
        'title': '呼市综合广播', 'sub': '呼和浩特本地新闻民生', 'badge': '呼市107.4', 'mark': '呼闻',
        'colors': ('#522420', '#291210', '#ffa89e'),
        'url': 'https://ls.qingting.fm/live/5022391/64k.m3u8',
        'aliases': ('呼市综合广播', '呼市新闻广播', '呼和浩特综合广播', '呼和浩特新闻广播'),
    },
    {
        'id': 'nm_hhht_wy', 'cat': 'fm', 'kind': 'live',
        'title': '呼市文艺广播', 'sub': '青城曲艺评书与文化', 'badge': '呼市99.8', 'mark': '呼艺',
        'colors': ('#45284d', '#221426', '#e0abff'),
        'url': 'https://ls.qingting.fm/live/5022063/64k.m3u8',
        'aliases': ('呼市文艺广播', '呼和浩特文艺广播'),
    },
    {
        'id': 'nm_xwzh', 'cat': 'fm', 'kind': 'live',
        'title': '内蒙新闻综合', 'sub': '内蒙古权威新闻与政风', 'badge': '内蒙新闻', 'mark': '蒙闻',
        'colors': ('#54231e', '#29110f', '#ffa399'),
        'url': 'https://ls.qingting.fm/live/1883/64k.m3u8',
        'aliases': ('内蒙新闻综合', '内蒙古新闻广播', '内蒙古新闻综合广播', '内蒙古广播'),
    },
    {
        'id': 'nm_jtzs', 'cat': 'fm', 'kind': 'live',
        'title': '内蒙交通之声', 'sub': '全区高速路况出行伴听', 'badge': '内蒙105.6', 'mark': '蒙交',
        'colors': ('#1b3d54', '#0d1f2b', '#87d4ff'),
        'url': 'https://ls.qingting.fm/live/1884/64k.m3u8',
        'aliases': ('内蒙交通之声', '内蒙古交通之声', '内蒙古交通广播'),
    },
    {
        'id': 'cnr_dwq', 'cat': 'fm', 'kind': 'live',
        'title': '大湾区之声', 'sub': '粤语资讯与湾区民生', 'badge': '粤港澳', 'mark': '湾区',
        'colors': ('#194247', '#0d2226', '#7ce8f0'),
        'url': 'https://ngcdn001.cnr.cn/live/hxzs/index.m3u8',
        'aliases': ('大湾区之声', '粤语广播'),
    },
    {
        'id': 'cnr_xgzs', 'cat': 'fm', 'kind': 'live',
        'title': '央广香港之声', 'sub': '香江民生与湾区快讯', 'badge': '香港 FM', 'mark': '香江',
        'colors': ('#4d2238', '#26111c', '#ffa3cf'),
        'url': 'https://ngcdn001.cnr.cn/live/xgzs/index.m3u8',
        'aliases': ('香港之声', '央广香港之声'),
    },
    {
        'id': 'hk_rthk1', 'cat': 'fm', 'kind': 'live',
        'title': '香港电台一台', 'sub': 'RTHK粤语新闻与时事', 'badge': 'RTHK 1', 'mark': '港一',
        'colors': ('#4f271d', '#26130e', '#ffb099'),
        'url': 'https://rthkradio1-live.akamaized.net/hls/live/2035313/radio1/index_64_a.m3u8',
        'aliases': ('香港电台第一台', '香港电台一台', '香港一台', 'rthk1'),
    },
    {
        'id': 'hk_rthk_pth', 'cat': 'fm', 'kind': 'live',
        'title': '香港普通话台', 'sub': 'RTHK普通话财经与民生', 'badge': 'RTHK 普', 'mark': '港普',
        'colors': ('#3d2b52', '#1f1629', '#cfb0ff'),
        'url': 'https://rthkradiopth-live.akamaized.net/hls/live/2040082/radiopth/index_64_a.m3u8',
        'aliases': ('香港普通话台', '香港电台普通话台', 'rthk普通话'),
    },
    {
        'id': 'hk_metro_info', 'cat': 'fm', 'kind': 'live',
        'title': '香港新城知讯', 'sub': '香港都会流行与生活', 'badge': '新城997', 'mark': '知讯',
        'colors': ('#1c424d', '#0e2226', '#85e3ff'),
        'url': 'https://1603884249.rsc.cdn77.org/1603884249/index.m3u8',
        'aliases': ('香港新城知讯台', '新城知讯台', '新城电台'),
    },

    # --- 少儿故事 · 校园名著 · 英语磨耳朵 (kids, 16 playable channels) ---
    {
        'id': 'kids_mixiaoquan', 'cat': 'kids', 'kind': 'channel',
        'title': '米小圈上学记', 'sub': '幽默小学生校园故事', 'badge': '校园故事', 'mark': '米圈',
        'colors': ('#4f3417', '#26190b', '#ffd08a'),
        'queries': ['米小圈上学记 DJ长音频', '米小圈上学记'], 'min_dur': 90, 'sort_ep': True,
        'aliases': ('米小圈', '米小圈上学记', '校园故事'),
    },
    {
        'id': 'kids_oxford', 'cat': 'kids', 'kind': 'channel',
        'title': '牛津树英语', 'sub': '60集分级绘本磨耳朵', 'badge': '英语启蒙', 'mark': '牛津',
        'colors': ('#183e54', '#0c202b', '#87d9ff'),
        'album_mids': ['001uRIKL27FDqL'], 'queries': ['牛津阅读树 英语'], 'min_dur': 60, 'sort_ep': True,
        'aliases': ('牛津树', '牛津树英语', '牛津阅读树', '英语绘本'),
    },
    {
        'id': 'kids_nce', 'cat': 'kids', 'kind': 'channel',
        'title': '新概念英语', 'sub': '第一册逐课连播磨耳朵', 'badge': '英语课文', 'mark': '新概',
        'colors': ('#1e3459', '#0f1a2e', '#9ebdff'),
        'album_mids': ['000Gzql346y1FI'], 'queries': ['新概念英语第一册课文朗读'], 'min_dur': 45, 'sort_ep': True,
        'aliases': ('新概念英语', '新概念课文', '英语朗读'),
    },
    {
        'id': 'kids_en_listen', 'cat': 'kids', 'kind': 'channel',
        'title': '趣味英语绘本', 'sub': '英文经典小故事磨耳朵', 'badge': '英语故事', 'mark': '绘本',
        'colors': ('#194542', '#0d2422', '#82f0e6'),
        'queries': ['英语绘本故事 儿童', '少儿英语启蒙 多彩故事树', '少儿英语故事'], 'min_dur': 55, 'sort_ep': True,
        'aliases': ('趣味英语听力', '趣味英语绘本', '英语听力', '英语磨耳朵', '少儿英语'),
    },
    {
        'id': 'kids_poem', 'cat': 'kids', 'kind': 'channel',
        'title': '唐诗三百首', 'sub': '亲子读古诗60集精讲', 'badge': '国学古诗', 'mark': '唐诗',
        'colors': ('#453520', '#241b10', '#f5cf93'),
        'album_mids': ['001eWBcC0Xc0iq'], 'queries': ['唐诗三百首 儿童朗读'], 'min_dur': 28, 'sort_ep': True,
        'aliases': ('唐诗三百首', '古诗词朗读', '唐诗笠翁对韵', '读古诗'),
    },
    {
        'id': 'kids_shenglv', 'cat': 'kids', 'kind': 'channel',
        'title': '声律启蒙笠翁', 'sub': '云对雨雪对风韵律诵读', 'badge': '国学韵律', 'mark': '声律',
        'colors': ('#3d3422', '#1f1a11', '#f0d399'),
        'album_mids': ['0009KnHt0UNSAI'], 'queries': ['声律启蒙 笠翁对韵 儿童'], 'min_dur': 65, 'sort_ep': True,
        'aliases': ('声律启蒙', '笠翁对韵', '声律启蒙笠翁'),
    },
    {
        'id': 'kids_holmes', 'cat': 'kids', 'kind': 'channel',
        'title': '福尔摩斯探案', 'sub': '经典悬疑推理名著连播', 'badge': '必读名著', 'mark': '探案',
        'colors': ('#2e364f', '#171b29', '#adc2ff'),
        'queries': ['福尔摩斯探案 DJ长音频', '福尔摩斯探案集 血字的研究', '福尔摩斯探案 有声'],
        'min_dur': 150, 'sort_ep': True, 'require_zh': True,
        'aliases': ('福尔摩斯', '福尔摩斯探案', '福尔摩斯探案集'),
    },
    {
        'id': 'kids_honglou', 'cat': 'kids', 'kind': 'channel',
        'title': '红楼梦有声书', 'sub': '古典文学必读名著朗读', 'badge': '必读名著', 'mark': '红楼',
        'colors': ('#52212d', '#291017', '#ffa3b8'),
        'queries': ['红楼梦 原著朗读', '红楼梦 有声小说'], 'exclude_kw': ('京华烟云',),
        'min_dur': 300, 'sort_ep': True, 'require_zh': True,
        'aliases': ('红楼梦', '红楼梦有声书', '红楼梦朗读'),
    },
    {
        'id': 'kids_kaishu', 'cat': 'kids', 'kind': 'channel',
        'title': '凯叔讲故事', 'sub': '经典古诗探案与童话', 'badge': '儿童故事', 'mark': '凯叔',
        'colors': ('#1c4538', '#0f261f', '#7ef2c4'),
        'queries': ['凯叔 声律启蒙', '凯叔讲故事', '猴子警长探案记'], 'min_dur': 75, 'sort_ep': False,
        'aliases': ('凯叔讲故事', '凯叔'),
    },
    {
        'id': 'kids_fairy', 'cat': 'kids', 'kind': 'channel',
        'title': '世界经典童话', 'sub': '格林与安徒生童话集', 'badge': '经典童话', 'mark': '童话',
        'colors': ('#46264d', '#231326', '#ebb0ff'),
        'queries': ['格林童话', '安徒生童话 儿童故事'], 'min_dur': 90, 'sort_ep': False, 'require_zh': True,
        'aliases': ('世界经典童话', '经典童话', '格林童话', '安徒生童话', '童话故事'),
    },
    {
        'id': 'kids_idiom', 'cat': 'kids', 'kind': 'channel',
        'title': '中华成语故事', 'sub': '爆笑父与子学成语60集', 'badge': '国学启蒙', 'mark': '成语',
        'colors': ('#4f3417', '#26190b', '#ffd08a'),
        'album_mids': ['004SDvW536t7Jd'], 'queries': ['成语故事 儿童'], 'min_dur': 80, 'sort_ep': True,
        'aliases': ('成语故事', '中华成语故事'),
    },
    {
        'id': 'kids_why', 'cat': 'kids', 'kind': 'channel',
        'title': '十万个为什么', 'sub': '趣味少儿科学百科问答', 'badge': '少儿科普', 'mark': '科普',
        'colors': ('#183e54', '#0c202b', '#87d9ff'),
        'queries': ['十万个为什么 儿童', '科学故事 儿童'], 'min_dur': 90, 'sort_ep': False, 'require_zh': True,
        'aliases': ('十万个为什么', '科普故事', '少儿科普'),
    },
    {
        'id': 'kids_dino', 'cat': 'kids', 'kind': 'channel',
        'title': '恐龙探险故事', 'sub': '霸王龙与火山龙历险记', 'badge': '恐龙百科', 'mark': '恐龙',
        'colors': ('#1f472a', '#102415', '#8ef0a8'),
        'album_mids': ['002h0npW2NggJQ'], 'queries': ['恐龙故事 宝宝巴士'], 'min_dur': 80, 'sort_ep': True,
        'aliases': ('恐龙故事', '恐龙探险故事', '霸王龙故事'),
    },
    {
        'id': 'kids_xiyou', 'cat': 'kids', 'kind': 'channel',
        'title': '西游记故事', 'sub': '孙悟空大闹天宫连播', 'badge': '名著故事', 'mark': '西游',
        'colors': ('#57261c', '#2b130e', '#ffa38f'),
        'queries': ['西游记 少儿故事', '西游记 宝宝巴士', '孙悟空 故事 儿童'],
        'exclude_kw': ('跳舞', '儿歌dj'), 'min_dur': 110, 'sort_ep': True, 'require_zh': True,
        'aliases': ('西游记故事', '西游记少儿故事', '孙悟空故事'),
    },
    {
        'id': 'kids_history', 'cat': 'kids', 'kind': 'channel',
        'title': '上下五千年', 'sub': '华夏历史名人典故连播', 'badge': '历史故事', 'mark': '历史',
        'colors': ('#453520', '#241b10', '#f5cf93'),
        'queries': ['中华上下五千年 故事', '中国历史故事 儿童'], 'min_dur': 180, 'sort_ep': True, 'require_zh': True,
        'aliases': ('中华上下五千年', '历史故事', '上下五千年'),
    },
    {
        'id': 'kids_bedtime', 'cat': 'kids', 'kind': 'channel',
        'title': '温馨睡前故事', 'sub': '60集轻柔晚安故事伴眠', 'badge': '晚安伴睡', 'mark': '晚安',
        'colors': ('#252c54', '#13172b', '#abbaff'),
        'album_mids': ['000z64vU0thjpb'], 'queries': ['宝宝巴士 睡前故事'], 'min_dur': 80, 'sort_ep': False,
        'aliases': ('睡前故事', '晚安故事', '温馨睡前故事'),
    },

    # --- 相声与名家评书 (crosstalk, 10 playable channels) ---
    {
        'id': 'ct_guodegang', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '郭德纲相声', 'sub': '德云社经典对口长篇', 'badge': '经典相声', 'mark': '德云',
        'colors': ('#54231c', '#29110d', '#ffa696'),
        'queries': ['郭德纲 于谦 相声', '郭德纲 相声 经典'], 'min_dur': 300, 'sort_ep': False, 'require_zh': True,
        'aliases': ('郭德纲', '郭德纲相声', '于谦', '德云社', '德云社相声'),
    },
    {
        'id': 'ct_yueyunpeng', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '岳云鹏相声', 'sub': '妙言趣语欢快相声专场', 'badge': '欢快相声', 'mark': '岳孙',
        'colors': ('#4f2e18', '#26160b', '#ffc28f'),
        'queries': ['岳云鹏 孙越 相声'], 'min_dur': 240, 'sort_ep': False, 'require_zh': True,
        'aliases': ('岳云鹏', '岳云鹏相声', '孙越'),
    },
    {
        'id': 'ct_master', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '马三立侯宝林', 'sub': '津京相声泰斗传世名段', 'badge': '大师相声', 'mark': '泰斗',
        'colors': ('#3d3322', '#1f1a11', '#ebd2a4'),
        'queries': ['马三立 相声', '侯宝林 相声'], 'min_dur': 180, 'sort_ep': False, 'require_zh': True,
        'aliases': ('马三立', '马三立相声', '侯宝林', '侯宝林相声', '大师相声'),
    },
    {
        'id': 'ct_liubaorui', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '刘宝瑞单口', 'sub': '君臣斗珍珠翡翠白玉汤', 'badge': '单口大王', 'mark': '单口',
        'colors': ('#2e3a4f', '#171d29', '#a8c6ff'),
        'queries': ['刘宝瑞 相声', '刘宝瑞 单口', '传统单口相声'], 'min_dur': 220, 'sort_ep': False, 'require_zh': True,
        'aliases': ('刘宝瑞', '单口相声', '刘宝瑞单口相声'),
    },
    {
        'id': 'ct_shantianfang', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '单田芳评书', 'sub': '风尘三侠白眉大侠连播', 'badge': '名家评书', 'mark': '单老',
        'colors': ('#4d2424', '#261212', '#ffadad'),
        'queries': ['单田芳 评书', '单田芳 白眉大侠', '单田芳 隋唐演义'], 'min_dur': 450, 'sort_ep': True, 'require_zh': True,
        'aliases': ('单田芳', '单田芳评书', '白眉大侠', '隋唐演义评书'),
    },
    {
        'id': 'ct_sanguo', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '三国演义评书', 'sub': '长篇三国演义按回连播', 'badge': '长篇评书', 'mark': '三国',
        'colors': ('#1e3b47', '#0f1d24', '#8ce0ff'),
        'queries': ['三国演义 DJ长音频', '袁阔成 三国演义'],
        'must_kw': ('三国',),
        'exclude_kw': ('老郭', '郭德纲', '插曲', '片尾', '主题曲', '电视剧原声'),
        'min_dur': 450, 'sort_ep': True, 'require_zh': True,
        'aliases': ('三国演义评书', '袁阔成', '袁阔成三国演义', '讲三国'),
    },
    {
        'id': 'ct_shuihu', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '水浒传评书', 'sub': '武松打虎梁山好汉传奇', 'badge': '长篇评书', 'mark': '水浒',
        'colors': ('#294022', '#142111', '#a8eb96'),
        'queries': ['评书 武松', '田连元 水浒传', '单田芳 水浒传'],
        'must_kw': ('水浒', '武松', '宋江', '梁山', '鲁智深', '林冲'),
        'exclude_kw': ('好汉歌', '主题曲', '插曲'),
        'min_dur': 450, 'sort_ep': True, 'require_zh': True,
        'aliases': ('水浒传评书', '田连元', '田连元水浒传', '武松评书'),
    },
    {
        'id': 'ct_pingshu_mix', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '刘兰芳岳飞传', 'sub': '精忠报国岳飞传全回连播', 'badge': '经典评书', 'mark': '岳飞',
        'colors': ('#472c3e', '#24161f', '#f5addb'),
        'album_mids': ['00194k7N4EJrFc'], 'queries': ['刘兰芳 岳飞传'], 'min_dur': 400, 'sort_ep': True,
        'aliases': ('刘兰芳', '岳飞传', '刘兰芳评书', '刘兰芳岳飞传', '评书电台'),
    },
    {
        'id': 'ct_yangjia', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '杨家将呼家将', 'sub': '传统忠烈袍带评书连播', 'badge': '传统评书', 'mark': '杨家',
        'colors': ('#4d2d1e', '#26160f', '#ffbe9e'),
        'queries': ['呼家将 DJ长音频', '杨家将 评书 刘兰芳', '薛家将 评书'],
        'must_kw': ('呼家将', '杨家将', '薛家将'),
        'exclude_kw': ('凤凰花开', '这就是爱', '超级英雄', '无畏的模样'),
        'min_dur': 450, 'sort_ep': True, 'require_zh': True,
        'aliases': ('杨家将', '杨家将评书', '呼家将', '薛家将'),
    },
    {
        'id': 'ct_baogong', 'cat': 'crosstalk', 'kind': 'channel',
        'title': '三侠五义包公', 'sub': '包公断案七侠五义评书', 'badge': '公案评书', 'mark': '包公',
        'colors': ('#263452', '#131a29', '#a3c2ff'),
        'queries': ['三侠五义 评书', '包公案 评书', '七侠五义 评书'],
        'min_dur': 450, 'sort_ep': True, 'require_zh': True,
        'aliases': ('三侠五义', '包公案', '七侠五义', '包公评书'),
    },
]

for _st in RADIO_STATIONS:
    _c1, _c2, _acc = _st['colors']
    _st['cover'] = _radio_emblem_uri(_c1, _c2, _acc, _st['badge'], _st['mark'])

RADIO_BY_ID = {s['id']: s for s in RADIO_STATIONS}
RADIO_PROG_PATH = None
_RADIO_PROG_LOCK = threading.Lock()


def _get_radio_prog_path() -> Path:
    return RADIO_PROG_PATH if RADIO_PROG_PATH is not None else (SESSION_PATH.parent / 'radio_progress.json')
_RADIO_CH_CACHE = {}
_RADIO_CH_LOCK = threading.Lock()
_PREWARMED_LIVE = False
_PREWARM_LOCK = threading.Lock()

_RADIO_SKIP_POP_TITLES = {
    '勇气大爆发', '数鸭子', '女儿国', '女儿情', '海林故事', '滚滚长江东逝水',
    '历史的天空', '片头曲', '趁着夜色上路', 'Six In A Bed', '秦韵唐诗故乡情',
    '好汉歌', '这一拜', '子夜四时歌', '全部免费', '发刊词', '隆重上架',
}


def _clean_radio_ep_title(raw_title: str) -> str:
    s = str(raw_title or '').strip()
    if not s:
        return ''
    # Remove DJ/upload tags & file extensions
    s = re.sub(r'[\(\[（【]\s*(?:DJ长音频|DJ版|有声读物|有声小说|长音频|完整版)\s*[\)\]）】]', '', s, flags=re.I)
    s = re.sub(r'\.(?:1?mp3|m4a|wav|flac)\b', '', s, flags=re.I)
    # Strip leading promo bracket like 【6岁+学成语】 or 【成长故事】 when main title follows
    m_lead = re.match(r'^【[^】]{1,14}】\s*(.+)$', s)
    if m_lead and len(m_lead.group(1).strip()) >= 2:
        s = m_lead.group(1).strip()
    # Strip trailing promo bracket (closed or truncated unclosed 【宝宝巴士...)
    s = re.sub(r'\s*【(?:宝宝巴士|免费|点击|关注|订阅|更多)[^】]*】?$', '', s)
    # Replace underscore separators with single spaces
    s = re.sub(r'_+', ' ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' -—·|_')
    return s or str(raw_title).strip()


def _norm_ep_dedup_key(title: str) -> str:
    s = _clean_radio_ep_title(title).lower()
    s = re.sub(r'[\s:：·\-—_.,，。！？!?\(\)（）\[\]【】《》"\']+', '', s)
    return s


def _extract_ep_num(title: str):
    # 1. Graded stage-book pattern like "牛津树1-25" or "A-01" -> 1*1000 + 25
    mg = re.search(r'(?:牛津树|level|book|stage)?\s*(\d{1,2})\s*[-—]\s*0*(\d{1,3})\b', title, re.I)
    if mg:
        return int(mg.group(1)) * 1000 + int(mg.group(2))
    # 2. Explicit Chinese chapter/episode marker
    m = re.search(r'第\s*0*(\d{1,4})\s*[集回章课讲篇部节]', title)
    if m:
        return int(m.group(1))
    m_cn = re.search(r'第([一二三四五六七八九十百零两]{1,5})[集回章课讲篇部节]', title)
    if m_cn:
        cn_map = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
        val, cur = 0, 0
        for ch in m_cn.group(1):
            if ch == '百':
                val += (cur or 1) * 100
                cur = 0
            elif ch == '十':
                val += (cur or 1) * 10
                cur = 0
            elif ch in cn_map:
                cur = cn_map[ch]
        val += cur
        if val > 0:
            return val
    # 3. Leading 2-4 digit episode index (e.g. "001珍珠翡翠白玉汤", "三国演义0002", "我是小学生02")
    m2 = re.match(r'^\s*(?:[\[\(【（][^\]\)】）]{1,12}[\]\)】）]\s*)?0*(\d{1,4})(?:[\s._\-—:：集回]|[\u4e00-\u9fff])', title)
    if m2:
        return int(m2.group(1))
    m3 = re.search(r'(?:lesson|ep|episode|part|vol\.?|book|chapter|story)\s*0*(\d{1,4})\b', title, re.I)
    if m3:
        return int(m3.group(1))
    m4 = re.search(r'0*(\d{2,4})(?:\s*$|\s*[\(\[（])', title)
    if m4:
        return int(m4.group(1))
    return None


def _prewarm_live_radio_hosts():
    global _PREWARMED_LIVE
    with _PREWARM_LOCK:
        if _PREWARMED_LIVE:
            return
        _PREWARMED_LIVE = True

    def _bg():
        seen_hosts = set()
        for s in RADIO_STATIONS:
            if s.get('kind') != 'live':
                continue
            u = s.get('url') or ''
            h = urllib.parse.urlsplit(u).hostname or ''
            if u.endswith('.m3u8') and h not in seen_hosts:
                seen_hosts.add(h)
                try:
                    _resolve_safe_ffmpeg_url(u, {}, is_live=True)
                except Exception:
                    pass

    threading.Thread(target=_bg, daemon=True).start()


def qq_album_tracks(albummid: str, limit: int = 60) -> list:
    if not albummid:
        return []
    req_0 = {
        'module': 'music.musichallAlbum.AlbumSongList',
        'method': 'GetAlbumSongList',
        'param': {'albumMid': str(albummid), 'begin': 0, 'num': int(limit), 'order': 2},
    }
    try:
        cfg = load_config()
        has_ck = bool(cfg.get('cookies_str') or cfg.get('qm_keyst'))
        req = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            data=json.dumps({'comm': _qq_comm(), 'req_0': req_0}).encode('utf-8'),
            headers=_qq_headers(),
        )
        with _safe_urlopen(req, timeout=6) as r:
            d = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        r0 = d.get('req_0') or {}
        r0_code = _musicu_code(d, r0, 0)
        raw = (r0.get('data') or {}).get('songList') or []
        if has_ck and (r0_code != 0 or not raw):
            if r0_code in (1000, 2001) and _refresh_musickey_from_cookies():
                req_retry = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': _qq_comm(), 'req_0': req_0}).encode('utf-8'),
                    headers=_qq_headers(),
                )
                with _safe_urlopen(req_retry, timeout=6) as rr:
                    dr = json.loads(_read_http_bytes(rr).decode('utf-8', errors='replace'))
                rr0 = dr.get('req_0') or {}
                r0_code = rr0.get('code') if 'code' in rr0 else dr.get('code', 0)
                raw = (rr0.get('data') or {}).get('songList') or raw
            if r0_code != 0 or not raw:
                req_anon = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': {'ct': 24, 'cv': 0}, 'req_0': req_0}).encode('utf-8'),
                    headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'},
                )
                with _safe_urlopen(req_anon, timeout=6) as r2:
                    d2 = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
                raw = ((d2.get('req_0') or {}).get('data') or {}).get('songList') or raw
        default_q = _default_quality_label(cfg)
        return [
            _format_qq_song(x.get('songInfo') or x, default_q=default_q)
            for x in raw
            if isinstance(x, dict) and ((x.get('songInfo') or x).get('mid') or (x.get('songInfo') or x).get('songmid'))
        ]
    except Exception as e:
        log.warning('qq_album_tracks(%r) failed: %s', albummid, e)
        return []


RADIO_AUDIO_MODES = {
    'hifi': '臻享增强',
    'voice': '清澈人声',
    'raw': '原声直通',
}


def _get_radio_audio_mode(cfg: dict = None) -> str:
    m = (cfg if isinstance(cfg, dict) else load_config()).get('radio_audio_mode', 'hifi')
    return m if m in RADIO_AUDIO_MODES else 'hifi'


def _build_audio_filter(track: dict, radio_audio_mode: str = None) -> str:
    base_soxr = f'aresample={OUT_RATE}:resampler=soxr:precision=28'
    if not isinstance(track, dict) or not track.get('radio_id'):
        return base_soxr
    mode = radio_audio_mode if radio_audio_mode in RADIO_AUDIO_MODES else _get_radio_audio_mode()
    if mode == 'raw':
        return base_soxr
    if mode == 'voice':
        return (
            'highpass=f=85,'
            'equalizer=f=2900:t=q:w=1.0:g=3.0,'
            'treble=g=1.6:f=7500,'
            'acompressor=threshold=-19dB:ratio=2.4:attack=10:release=180:makeup=2.4dB,'
            f'{base_soxr},'
            'alimiter=limit=0.95:attack=3:release=50:level=disabled'
        )
    if track.get('is_live'):
        return (
            'highpass=f=52,'
            'equalizer=f=115:t=q:w=1.0:g=2.4,'
            'equalizer=f=3400:t=q:w=1.2:g=1.8,'
            'treble=g=2.2:f=8500,'
            'acompressor=threshold=-19dB:ratio=2.0:attack=15:release=220:makeup=2.0dB,'
            f'{base_soxr},'
            'alimiter=limit=0.95:attack=3:release=50:level=disabled'
        )
    return (
        'highpass=f=65,'
        'equalizer=f=155:t=q:w=1.1:g=1.8,'
        'equalizer=f=3100:t=q:w=1.1:g=2.5,'
        'treble=g=1.5:f=8000,'
        'acompressor=threshold=-20dB:ratio=2.2:attack=12:release=200:makeup=2.2dB,'
        f'{base_soxr},'
        'alimiter=limit=0.95:attack=3:release=50:level=disabled'
    )


def _radio_live_quality_label(url: str) -> str:
    h = urllib.parse.urlsplit(url or '').hostname or ''
    if any(k in h for k in ('cnr.cn', 'rbc.cn', 'cri.cn', 'macaofm995')):
        return '48kHz AAC 高清'
    return 'AAC 立体声高清'


def _radio_live_track(st: dict) -> dict:
    return {
        'mid': '',
        'media_mid': '',
        'radio_id': st['id'],
        'is_live': True,
        'is_local': False,
        'title': st['title'],
        'artist': st['badge'],
        'album': '网络广播电台',
        'desc': st['sub'],
        'cover': st['cover'],
        'thumb': st['cover'],
        'duration': 0,
        'vip': False,
        'source': '网络电台',
        'quality': _radio_live_quality_label(st['url']),
        'url': st['url'],
        'headers': {},
    }


def resolve_radio_channel_tracks(st: dict, shuffle: bool = False, force_refresh: bool = False) -> list:
    sid = st['id']
    now_m = time.monotonic()
    with _RADIO_CH_LOCK:
        cached = _RADIO_CH_CACHE.get(sid)
    if cached and (now_m - cached[0]) < 1800.0 and cached[1] and not force_refresh:
        out = [dict(x) for x in cached[1]]
        if shuffle and len(out) > 1:
            random.shuffle(out)
        return out

    min_dur = int(st.get('min_dur') or 60)
    must_kw = tuple(st.get('must_kw') or ())
    exclude_kw = tuple(st.get('exclude_kw') or ())
    require_zh = bool(st.get('require_zh'))
    raw_tracks = []

    # 1. Curated ordered QQ Music albumMids first (single-book sequential chapters!)
    for amid in (st.get('album_mids') or ()):
        got_alb = qq_album_tracks(amid, limit=60)
        if got_alb:
            raw_tracks.extend(got_alb)

    # 2. Supplement or fallback via keyword search (parallelized on live queries for ~4x faster cold-start)
    if len(raw_tracks) < 20:
        queries = list(st.get('queries') or [st['title']])
        search_tasks = [(qi, pg, q) for qi, q in enumerate(queries[:4]) for pg in (1, 2)]
        if hasattr(qq_search, 'assert_called') or len(search_tasks) <= 1:
            for _, pg, q in search_tasks:
                try:
                    got = qq_search(q, limit=30, page=pg)
                    if got:
                        raw_tracks.extend(got)
                except Exception as e:
                    log.warning('radio channel search %r p%d failed: %s', q, pg, e)
                if len(raw_tracks) >= 100:
                    break
        else:
            def _fetch_search_page(task):
                qi, pg, q = task
                try:
                    return (qi, pg, qq_search(q, limit=30, page=pg) or [])
                except Exception as e:
                    log.warning('radio channel search %r p%d failed: %s', q, pg, e)
                    return (qi, pg, [])

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(search_tasks))) as ex:
                for _, _, got in sorted(ex.map(_fetch_search_page, search_tasks), key=lambda r: (r[0], r[1])):
                    if got:
                        raw_tracks.extend(got)
                    if len(raw_tracks) >= 120:
                        break

    filtered = []
    seen_mids = set()
    seen_stems = set()
    for t in raw_tracks:
        if not isinstance(t, dict):
            continue
        raw_t = str(t.get('title') or '').strip()
        if not raw_t or any(bad in raw_t for bad in _RADIO_SKIP_POP_TITLES):
            continue
        if exclude_kw and any(ek.lower() in raw_t.lower() for ek in exclude_kw):
            continue
        if must_kw and not any(mk.lower() in (raw_t + ' ' + str(t.get('album') or '')).lower() for mk in must_kw):
            continue
        if require_zh and not re.search(r'[\u4e00-\u9fff]', raw_t):
            continue
        # Block Hangul / German audiobook noise
        if re.search(r'[\uac00-\ud7af]', raw_t) or re.search(r'\b(?:Kapitel|Teil|Studie)\b', raw_t):
            continue
        dur = int(t.get('duration') or 0)
        if dur > 0 and dur < min_dur:
            continue
        clean_t = _clean_radio_ep_title(raw_t)
        if not clean_t:
            continue
        mid = t.get('mid') or ''
        stem = _norm_ep_dedup_key(clean_t)
        if (mid and mid in seen_mids) or (stem and stem in seen_stems):
            continue
        if mid:
            seen_mids.add(mid)
        if stem:
            seen_stems.add(stem)
        item = dict(t)
        item['title'] = clean_t
        item['radio_id'] = sid
        item['is_live'] = False
        item['source'] = st['badge']
        if not item.get('cover'):
            item['cover'] = st['cover']
            item['thumb'] = st['cover']
        filtered.append(item)

    # 3. Verify stream playability via batched vkey check (skipping when qq_search is unit-test mocked or urls preset)
    if filtered and not hasattr(qq_search, 'assert_called') and not all(x.get('url') for x in filtered):
        v_ok = _qq_playable_tracks(filtered[:90])
        if len(v_ok) >= 3:
            filtered = v_ok

    if st.get('sort_ep') and len(filtered) > 2 and not st.get('album_mids'):
        numbered = [(i, _extract_ep_num(x.get('title', '')), x) for i, x in enumerate(filtered)]
        if sum(1 for _, n, _ in numbered if n is not None) >= max(2, len(filtered) // 3):
            numbered.sort(key=lambda tup: (0 if tup[1] is not None else 1, tup[1] if tup[1] is not None else 999999, tup[0]))
            filtered = [tup[2] for tup in numbered]
    elif st.get('sort_ep') and st.get('album_mids') and len(filtered) > 2:
        # Sort graded books like Oxford Reading Tree (3-10 -> 1-01) when episode numbers exist
        numbered = [(i, _extract_ep_num(x.get('title', '')), x) for i, x in enumerate(filtered)]
        if sum(1 for _, n, _ in numbered if n is not None) >= (len(filtered) // 2):
            numbered.sort(key=lambda tup: (0 if tup[1] is not None else 1, tup[1] if tup[1] is not None else 999999, tup[0]))
            filtered = [tup[2] for tup in numbered]

    if filtered:
        with _RADIO_CH_LOCK:
            if len(_RADIO_CH_CACHE) >= 16 and sid not in _RADIO_CH_CACHE:
                _RADIO_CH_CACHE.pop(next(iter(_RADIO_CH_CACHE)), None)
            _RADIO_CH_CACHE[sid] = (now_m, [dict(x, _ep_idx=i) for i, x in enumerate(filtered[:60])])

    out = [dict(x, _ep_idx=i) for i, x in enumerate(filtered[:60])]
    if shuffle and len(out) > 1:
        random.shuffle(out)
    return out


def match_radio_voice_query(raw_q: str, clean_q: str):
    """Match natural voice commands for FM radio, local/HK/MO radio, kids/English stories, crosstalk, and pingshu."""
    s_all = (raw_q or '').lower().strip()
    s_cln = (clean_q or '').lower().strip(' ，。！？,.!?《》"\'“”')
    for pre in (
        '请帮我', '帮我', '继续播放', '继续听', '接着听', '接着放', '给孩子', '给宝宝', '给我们', '我想听', '想听',
        '来一段', '来段', '说一段', '说段', '讲一段', '讲个', '讲一首', '放一段', '放段', '听一段', '听段', '听一听',
        '播放', '打开', '开启', '放个', '来个', '放', '听',
    ):
        if s_cln.startswith(pre) and len(s_cln) > len(pre):
            s_cln = s_cln[len(pre):].strip()
    if not s_cln:
        return None
    for suf in ('广播电台', '电台', '广播', '频道', '调频'):
        if s_cln.endswith(suf) and len(s_cln) > len(suf):
            cand = s_cln[:-len(suf)].strip()
            for st in RADIO_STATIONS:
                if cand == st['title'].lower() or any(cand == a.lower() for a in st.get('aliases', ())):
                    return st['id']
    for st in RADIO_STATIONS:
        if s_cln == st['title'].lower() or any(s_cln == a.lower() for a in st.get('aliases', ())):
            return st['id']
    _song_cues = ('这首歌', '那首歌', '歌曲', '单曲', '主题曲', '片尾曲', '插曲', '原唱', '合唱', '之歌')
    _radio_cues = ('电台', '广播', '调频', '频道', '相声', '评书', '故事', '绘本', '磨耳朵', 'fm')
    has_song_cue = any(w in s_cln for w in _song_cues) or ('的' in s_cln and not any(w in s_cln for w in _radio_cues))
    if not has_song_cue:
        for st in RADIO_STATIONS:
            for a in (st['title'], *st.get('aliases', ())):
                al = a.lower()
                if len(al) >= 3 and (al in s_cln or al in s_all):
                    return st['id']
    if s_cln in ('收音机', '广播', '电台', '网络收音机', '网络电台', '新闻电台', '新闻', '放广播', '听广播', '打开电台', '打开收音机'):
        return 'cnr_zgzs'
    if s_cln in ('音乐电台', '音乐台', '高音质电台', '高音质音乐电台', '高清音乐电台', '流行音乐电台', '华语音乐台', '音乐调频'):
        return 'cnr_yyzs'
    if s_cln in ('轻音乐', '纯音乐', '爵士', '爵士乐', '爵士音乐', '钢琴曲', '古典轻音乐'):
        return 'qt_jazz'
    if s_cln in ('粤语电台', '粤语歌电台', '港乐电台', '港乐台', '粤语音乐台'):
        return 'asia_yue'
    if s_cln in ('古典音乐', '交响乐', '古典电台'):
        return 'cnr_dszs'
    if s_cln in ('老歌电台', '经典老歌', '怀旧老歌', '怀旧电台'):
        return 'xm_2629'
    if s_cln in ('儿童故事', '故事', '少儿故事', '讲个故事', '讲故事', '给孩子讲故事', '儿童电台', '故事电台'):
        return 'kids_kaishu'
    if s_cln in ('英语', '磨耳朵', '少儿英语', '英语故事'):
        return 'kids_oxford'
    if s_cln in ('相声', '听相声', '说段相声', '来段相声', '相声电台'):
        return 'ct_guodegang'
    if s_cln in ('评书', '听评书', '来段评书', '评书电台', '听书'):
        return 'ct_shantianfang'
    return None


class QQMusicEngine:
    """Standalone 48kHz Stereo Player with Progress Tracking, Lyrics, and Voice Ducking."""
    ambient_preset = None
    ambient_layer = None
    ambient_layer_gain = 0.45
    _ambient_buf = None
    _ambient_layer_buf = None
    _ambient_worker_gen = -1
    _proc2 = None
    _quality_seq = 0

    def __init__(self):
        self._lock = threading.Lock()
        self._vol_lock = threading.Lock()
        self._quality_seq = 0
        self.queue = []
        self.index = 0
        self.playing = False
        self.paused = False
        self.ducked = False
        self.listen_duck = False
        self.alarm_ringing = False
        self.bt_streaming = False
        self._bt_pause_sent_mono = 0.0
        self._local_vol_mono = 0.0
        self._just_started_mono = None
        self.boot_id = secrets.randbits(48) or 1
        self.open_ui_seq = 0
        self._open_ui_mono = 0.0
        self.queue_seq = 1
        self._cached_q_seq = 0
        self._cached_queue_pub = []
        self.lyrics_seq = 1
        self.lyrics = []
        self.elapsed_base = 0.0
        self.play_started_mono = 0.0
        self.volume = 55
        self._proc = None
        self._proc2 = None
        self._pacat = None
        self._gen = 0
        self._starting_gen = -1
        self._fail_streak = 0
        self._shutting_down = False
        self.aec_clients = {}
        self._aec_lock = threading.Lock()
        self._defer_save_timer = None
        # Round 109 sleep timer: 'time' pauses at sleep_deadline (monotonic), 'tracks' pauses after
        # sleep_tracks_left tracks have ended naturally; the last SLEEP_FADE_SECONDS fade out smoothly.
        self.sleep_mode = None
        self.sleep_deadline = 0.0
        self.sleep_total = 0
        self.sleep_tracks_left = 0
        # 2026-11-12: fade length per timer, all-night mode, sleep sessions (sound + timer + own volume)
        self.sleep_fade = self.SLEEP_FADE_SECONDS
        self.sleep_night = False
        self.sleep_started_mono = 0.0
        self.sleep_ends_wall = None
        self._sg_last = (1.0, 0.0)
        self.sleep_session = None
        self._sleep_idle_since = None
        self._sleep_state = _read_sleep_state()
        self.ambient_preset = None
        self.ambient_layer = None
        self.ambient_layer_gain = 0.45
        self.ambient_seq = 0
        self._ambient_buf = None
        self._ambient_layer_buf = None
        self._ambient_worker_gen = -1
        self.study_duck_until = 0.0
        self._duck_leases = {}
        init_mode = load_config().get('play_mode', 'loop')
        self.play_mode = init_mode if init_mode in ('loop', 'single', 'shuffle') else 'loop'
        self._read_initial_volume()
        self._radio_prog = {}
        self._radio_prog_dirty = False
        self._radio_prog_saved_mono = 0.0
        self._load_radio_progress()
        self._load_session()

    def _clear_duck_locked(self):
        self.study_duck_until = 0.0
        leases = getattr(self, '_duck_leases', None)
        if isinstance(leases, dict):
            leases.clear()

    def _clear_duck_except_home_locked(self):
        now_m = time.monotonic()
        leases = getattr(self, '_duck_leases', None)
        home_until = 0.0
        if isinstance(leases, dict):
            raw_h = leases.get('home', 0.0)
            if isinstance(raw_h, (int, float)) and raw_h > now_m:
                home_until = float(raw_h)
            leases.clear()
            if home_until > 0.0:
                leases['home'] = home_until
        self.study_duck_until = home_until

    @staticmethod
    def _safe_dur(val) -> int:
        if isinstance(val, bool) or val is None:
            return 0
        try:
            f = float(val)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(f) or f <= 0:
            return 0
        return min(86400, int(round(f)))

    @classmethod
    def _public_track(cls, t: dict):
        if not isinstance(t, dict):
            return None
        dur = cls._safe_dur(t.get('duration'))
        return {
            'mid': str(t.get('mid') or ''),
            'media_mid': str(t.get('media_mid') or ''),
            'file': str(t.get('url') or '') if t.get('is_local') else '',
            'title': str(t.get('title') or ''),
            'artist': str(t.get('artist') or ''),
            'album': str(t.get('album') or ''),
            'cover': str(t.get('cover') or ''),
            'thumb': str(t.get('thumb') or t.get('cover') or ''),
            'source': str(t.get('source') or 'QQ音乐'),
            'quality': str(t.get('quality') or ''),
            'duration': dur,
            'vip': bool(t.get('vip')),
            'is_local': bool(t.get('is_local')),
            'is_live': bool(t.get('is_live')),
            'radio_id': str(t.get('radio_id') or ''),
            'desc': str(t.get('desc') or ''),
        }

    def _load_radio_progress(self):
        try:
            p = _get_radio_prog_path()
            if not p.exists() or p.is_symlink():
                return
            raw = json.loads(_read_regular_text(p, 262144, private=True))
            if isinstance(raw, dict):
                clean = {}
                for k, v in list(raw.items())[:80]:
                    if isinstance(k, str) and k in RADIO_BY_ID and isinstance(v, dict):
                        clean[k] = {
                            'mid': str(v.get('mid') or '')[:64],
                            'title': str(v.get('title') or '')[:120],
                            'idx': max(0, int(v.get('idx') or 0)),
                            'sec': max(0.0, round(float(v.get('sec') or 0.0), 1)),
                            'dur': max(0, int(v.get('dur') or 0)),
                            'updated': int(v.get('updated') or 0),
                        }
                self._radio_prog = clean
        except Exception as e:
            log.warning('Failed to load radio progress: %s', e)

    def _snapshot_radio_progress_locked(self) -> bool:
        if not (self.playing and 0 <= self.index < len(self.queue)):
            return False
        cur = self.queue[self.index]
        rid = str(cur.get('radio_id') or '')
        if not rid or cur.get('is_live') or rid not in RADIO_BY_ID:
            return False
        dur = self._safe_dur(cur.get('duration'))
        sec = max(0.0, float(self.elapsed_base or 0.0))
        idx = self.index
        target = cur
        if dur > 30 and sec >= (dur - 8) and len(self.queue) > 1:
            idx = (self.index + 1) % len(self.queue)
            target = self.queue[idx]
            dur = self._safe_dur(target.get('duration'))
            sec = 0.0
        ep_idx = target.get('_ep_idx')
        entry = {
            'mid': str(target.get('mid') or '')[:64],
            'title': str(target.get('title') or '')[:120],
            'idx': int(ep_idx) if isinstance(ep_idx, int) and not isinstance(ep_idx, bool) else idx,
            'sec': round(sec, 1),
            'dur': dur,
            'updated': int(time.time()),
        }
        prev = self._radio_prog.get(rid)
        if not isinstance(prev, dict) or (
            prev.get('mid') != entry['mid']
            or prev.get('title') != entry['title']
            or prev.get('idx') != entry['idx']
            or prev.get('sec') != entry['sec']
            or prev.get('dur') != entry['dur']
        ):
            self._radio_prog[rid] = entry
            self._radio_prog_dirty = True
            return True
        return False

    def _flush_radio_progress(self, force: bool = False):
        p = _get_radio_prog_path()
        with _RADIO_PROG_LOCK:
            with self._lock:
                if not self._radio_prog_dirty and not force:
                    return
                snap = dict(self._radio_prog)
                self._radio_prog_dirty = False
                self._radio_prog_saved_mono = time.monotonic()
            tmp = p.parent / f'.{p.name}.{os.getpid()}.{threading.get_ident()}.tmp'
            created = False
            try:
                p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                pst = os.lstat(p.parent)
                if p.is_symlink():
                    raise OSError('Refusing symlink radio progress path')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                created = True
                with os.fdopen(fd, 'w', encoding='utf-8') as tf:
                    os.fchmod(tf.fileno(), 0o600)
                    if os.geteuid() == 0 and pst.st_uid != 0:
                        with contextlib.suppress(OSError):
                            os.fchown(tf.fileno(), pst.st_uid, pst.st_gid)
                    tf.write(json.dumps(snap, ensure_ascii=False))
                    tf.flush()
                    os.fsync(tf.fileno())
                if p.is_symlink():
                    raise OSError('Refusing symlink radio progress path')
                os.replace(tmp, p)
                created = False
            except Exception as e:
                with self._lock:
                    self._radio_prog_dirty = True
                log.warning('Failed to save radio progress: %s', e)
            finally:
                if created:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)

    def clear_radio_progress(self, station_id: str) -> dict:
        sid = str(station_id or '').strip()
        if not sid or sid not in RADIO_BY_ID:
            return {'ok': False, 'error': '未找到该频道'}
        with self._lock:
            removed = self._radio_prog.pop(sid, None) is not None
            if removed:
                self._radio_prog_dirty = True
        if removed:
            self._flush_radio_progress(force=True)
        return {'ok': True, 'id': sid, 'cleared': removed}

    def _load_session(self):
        try:
            if not SESSION_PATH.exists() or SESSION_PATH.is_symlink():
                return
            data = json.loads(_read_regular_text(SESSION_PATH, private=True))
            if not isinstance(data, dict):
                return
            q = data.get('queue')
            if data.get('playing') and isinstance(q, list) and q:
                cleaned_q = []
                for x in q[:200]:
                    if not isinstance(x, dict):
                        continue
                    d = dict(x)
                    rid = str(d.get('radio_id') or '')
                    st_r = RADIO_BY_ID.get(rid) if rid else None
                    if d.get('is_live'):
                        d['url'] = st_r['url'] if st_r and st_r.get('kind') == 'live' else None
                        if not d['url']:
                            continue
                        d['quality'] = _radio_live_quality_label(d['url'])
                    elif not d.get('is_local'):
                        d['url'] = None
                    if st_r and not d.get('cover'):
                        d['cover'] = st_r['cover']
                    if not d.get('thumb') and d.get('cover'):
                        d['thumb'] = d['cover']
                    cleaned_q.append(d)
                self.queue = cleaned_q
                if self.queue:
                    raw_idx = data.get('index')
                    try:
                        idx_val = 0 if isinstance(raw_idx, bool) or raw_idx is None else int(raw_idx)
                    except (TypeError, ValueError, OverflowError):
                        idx_val = 0
                    self.index = max(0, min(idx_val, len(self.queue) - 1))
                    if self.queue[self.index].get('radio_id') and self.play_mode in ('shuffle', 'single'):
                        self.play_mode = 'loop'
                    raw_eb = data.get('elapsed_base')
                    try:
                        eb_val = 0.0 if isinstance(raw_eb, bool) or raw_eb is None else float(raw_eb)
                    except (TypeError, ValueError, OverflowError):
                        eb_val = 0.0
                    self.elapsed_base = eb_val if math.isfinite(eb_val) and eb_val >= 0.0 else 0.0
                    lyr = data.get('lyrics')
                    self.lyrics = [x for x in lyr[:300] if isinstance(x, dict)] if isinstance(lyr, list) else []
                    self.playing = True
                    self.paused = True
        except Exception as e:
            log.warning('Failed to load session: %s', e)

    def save_session(self, defer: bool = False):
        # Serialize the snapshot with its write: an older background save must not
        # replace a newer user action or share its temporary file.
        if defer:
            with self._lock:
                if getattr(self, '_shutting_down', False):
                    return
                old_t = getattr(self, '_defer_save_timer', None)
                if old_t is not None:
                    return
                tmr = threading.Timer(2.0, self._flush_deferred_save)
                tmr.daemon = True
                self._defer_save_timer = tmr
                tmr.start()
            return
        with self._lock:
            old_t = getattr(self, '_defer_save_timer', None)
            self._defer_save_timer = None
        if old_t is not None:
            old_t.cancel()
        with _SESSION_LOCK:
            self._save_session_locked()

    def _save_session_safe(self, defer: bool = False):
        fn = self.save_session
        if defer:
            try:
                import inspect as _insp
                if 'defer' in _insp.signature(fn).parameters:
                    return fn(defer=True)
            except (TypeError, ValueError):
                pass
        return fn()

    def _flush_deferred_save(self):
        with self._lock:
            self._defer_save_timer = None
            if getattr(self, '_shutting_down', False):
                return
        with _SESSION_LOCK:
            self._save_session_locked()

    def _save_session_locked(self):
        try:
            flush_rp = False
            with self._lock:
                if getattr(self, '_shutting_down', False):
                    return
                if self._snapshot_radio_progress_locked() or self._radio_prog_dirty:
                    flush_rp = True
                playing = bool(self.playing and self.queue)
                data = {
                    'playing': playing,
                    'index': self.index if playing else 0,
                    'elapsed_base': round(self.elapsed_base, 1) if playing else 0.0,
                    'lyrics': list(self.lyrics[:300]) if playing else [],
                    'queue': [
                        {
                            'mid': x.get('mid', ''),
                            'media_mid': x.get('media_mid', ''),
                            'title': x.get('title', ''),
                            'artist': x.get('artist', ''),
                            'album': x.get('album', ''),
                            'cover': ('' if (x.get('radio_id') in RADIO_BY_ID and str(x.get('cover', '')).startswith('data:image/svg+xml;base64,')) else x.get('cover', '')),
                            'thumb': ((x.get('thumb') or '') if x.get('thumb') != x.get('cover') else ''),
                            'duration': self._safe_dur(x.get('duration')),
                            'vip': bool(x.get('vip')),
                            'source': x.get('source', ''),
                            'quality': x.get('quality', ''),
                            'url': x.get('url') if x.get('is_local') else None,
                            'is_local': bool(x.get('is_local')),
                            'is_live': bool(x.get('is_live')),
                            'radio_id': x.get('radio_id', ''),
                            'desc': x.get('desc', ''),
                            **({'_ep_idx': x['_ep_idx']} if isinstance(x.get('_ep_idx'), int) and not isinstance(x.get('_ep_idx'), bool) and x['_ep_idx'] >= 0 else {}),
                        }
                        for x in (self.queue[:200] if playing else [])
                    ],
                }
            SESSION_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                pst = os.lstat(SESSION_PATH.parent)
                if stat.S_ISDIR(pst.st_mode) and not pst.st_mode & stat.S_ISVTX and pst.st_uid == os.getuid():
                    os.chmod(SESSION_PATH.parent, 0o700)
            tmp = SESSION_PATH.parent / f'.{SESSION_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp'
            created = False
            try:
                if SESSION_PATH.is_symlink():
                    raise OSError('Refusing symlink session path')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                created = True
                with os.fdopen(fd, 'w', encoding='utf-8') as tf:
                    os.fchmod(tf.fileno(), 0o600)
                    tf.write(json.dumps(data, ensure_ascii=False))
                    tf.flush()
                    os.fsync(tf.fileno())
                    st0 = os.fstat(tf.fileno())
                st1 = os.lstat(tmp)
                if not stat.S_ISREG(st1.st_mode) or (st1.st_dev, st1.st_ino) != (st0.st_dev, st0.st_ino) or SESSION_PATH.is_symlink():
                    raise OSError('Session temp file changed unexpectedly')
                os.replace(tmp, SESSION_PATH)
                created = False
            finally:
                if created:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
            if flush_rp:
                self._flush_radio_progress()
        except Exception as e:
            log.warning('Failed to save session: %s', e)

    def set_play_mode(self, mode: str) -> str:
        if mode not in ('loop', 'single', 'shuffle'):
            mode = 'loop'
        with self._lock:
            self.play_mode = mode
        with _CFG_LOCK:
            cfg = load_config()
            if cfg.get('play_mode') != mode:
                cfg['play_mode'] = mode
                save_config(cfg)
        return mode

    def _read_initial_volume(self):
        try:
            if _RING_VOL_PATH.exists() and not _RING_VOL_PATH.is_symlink():
                rv = json.loads(_read_regular_text(_RING_VOL_PATH, 65536)).get('volume')
                if isinstance(rv, (int, float)) and not isinstance(rv, bool) and math.isfinite(rv):
                    self.volume = max(0, min(100, int(round(rv))))
                    return
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'],
                env={**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000'},
                text=True,
                timeout=2,
            )
            m = re.search(r'([\d.]+)', out)
            if m:
                self.volume = int(round(float(m.group(1)) * 100))
        except Exception:
            pass

    def _pause_bluetooth_if_streaming(self):
        now_m = time.monotonic()
        with self._lock:
            if not self.bt_streaming or (now_m - getattr(self, '_bt_pause_sent_mono', 0.0)) < 2.0:
                return
            self._bt_pause_sent_mono = now_m

        def _worker():
            try:
                subprocess.run(
                    ['/usr/bin/python3', '-c', _BT_PAUSE_CODE],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    @property
    def just_started(self) -> bool:
        return self._just_started_mono is not None and (time.monotonic() - self._just_started_mono) < 15.0

    @just_started.setter
    def just_started(self, val: bool):
        self._just_started_mono = time.monotonic() if (val and (self.ducked or getattr(self, 'listen_duck', False))) else None

    def set_volume(self, vol: int = None, delta: int = None) -> int:
        if (vol is None and delta is None) or isinstance(vol, bool) or isinstance(delta, bool):
            return self.volume
        with self._lock:
            try:
                if delta is not None:
                    self.volume = max(0, min(100, self.volume + int(float(delta))))
                elif vol is not None:
                    self.volume = max(0, min(100, int(float(vol))))
            except (TypeError, ValueError, OverflowError):
                return self.volume
            self._vol_seq = getattr(self, '_vol_seq', 0) + 1
            my_seq = self._vol_seq
            self._local_vol_mono = time.monotonic()
            new_vol = self.volume
        with self._vol_lock:
            with self._lock:
                if getattr(self, '_vol_applied_seq', 0) >= my_seq:
                    return self.volume
                apply_seq = getattr(self, '_vol_seq', my_seq)
                self._local_vol_mono = time.monotonic()
                new_vol = self.volume
            synced = False
            try:
                req = urllib.request.Request(
                    'http://127.0.0.1:8090/api/volume',
                    data=json.dumps({'volume': new_vol}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with _LOCAL_OPENER.open(req, timeout=1.5) as r:
                    res = json.loads(r.read(256).decode('utf-8', errors='replace'))
                    synced = r.status == 200 and isinstance(res, dict) and res.get('ok') is True
            except urllib.error.HTTPError as he:
                he.close()
                synced = False
            except Exception:
                synced = False
            if not synced:
                try:
                    subprocess.run(
                        ['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{new_vol / 100.0:.2f}'],
                        env={**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000'},
                        timeout=2,
                    )
                except Exception:
                    pass
            with self._lock:
                self._vol_applied_seq = max(getattr(self, '_vol_applied_seq', 0), apply_seq)
                self._local_vol_mono = time.monotonic()
                self._vol_synced_xz = synced
        return new_vol

    def get_elapsed(self) -> float:
        with self._lock:
            if not self.playing:
                return 0.0
            return self.elapsed_base

    _AEC_FLUSH_PKT = b'QQM_FLUSH_BUFFER' * 480  # 7680 bytes

    def _send_aec_flush(self):
        with self._aec_lock:
            for sock in list(self.aec_clients):
                try:
                    sock.sendall(self._AEC_FLUSH_PKT)
                except Exception:
                    self.aec_clients.pop(sock, None)
                    try:
                        sock.close()
                    except Exception:
                        pass

    def set_ducked(self, ducked: bool):
        do_flush = False
        with self._lock:
            if self.ducked == bool(ducked):
                return
            self.ducked = bool(ducked)
            if self.ducked:
                do_flush = True
            elif not getattr(self, 'listen_duck', False):
                self._just_started_mono = None
        if do_flush:
            self._send_aec_flush()

    def set_listen_duck(self, active: bool):
        do_flush = False
        with self._lock:
            old = bool(getattr(self, 'listen_duck', False))
            self.listen_duck = bool(active)
            if self.listen_duck and not old and not self.ducked:
                do_flush = True
        if do_flush:
            self._send_aec_flush()

    SLEEP_FADE_SECONDS = 20.0

    def _clear_sleep_locked(self):
        self.sleep_mode = None
        self.sleep_deadline = 0.0
        self.sleep_total = 0
        self.sleep_tracks_left = 0
        self.sleep_fade = self.SLEEP_FADE_SECONDS
        self.sleep_night = False
        self.sleep_ends_wall = None
        self._sg_last = (1.0, time.monotonic())

    def _sleep_track_left_locked(self):
        t = self.queue[self.index] if (self.playing and 0 <= self.index < len(self.queue)) else None
        dur = self._safe_dur(t.get('duration')) if t else 0
        return dur, (max(0.0, dur - self.elapsed_base) if dur else 0.0)

    def _sleep_public_locked(self):
        if self.sleep_mode == 'time':
            if getattr(self, '_preview', None) is not None:
                return None  # a preview's own short timer is not a sleep timer for the pages
            remaining = max(0.0, self.sleep_deadline - time.monotonic())
            fade = float(getattr(self, 'sleep_fade', self.SLEEP_FADE_SECONDS) or self.SLEEP_FADE_SECONDS)
            ends = getattr(self, 'sleep_ends_wall', None)
            return {'mode': 'time', 'total': self.sleep_total, 'remaining': int(math.ceil(remaining)),
                    'fading': remaining <= fade, 'fade': int(fade), 'night': bool(getattr(self, 'sleep_night', False)),
                    'ends_at': int(ends) if ends else None}
        if self.sleep_mode == 'tracks':
            dur, left = self._sleep_track_left_locked()
            last = self.sleep_tracks_left == 1
            return {'mode': 'tracks', 'total': self.sleep_total, 'tracks_left': self.sleep_tracks_left,
                    'remaining': int(math.ceil(left)) if (last and dur) else None,
                    'fading': bool(last and dur > 2 * self.SLEEP_FADE_SECONDS and left <= self.SLEEP_FADE_SECONDS)}
        return None

    def sleep_status(self):
        with self._lock:
            return self._sleep_public_locked()

    def set_sleep(self, minutes=None, tracks=None, cancel=False, fade=None, until=None, night=False):
        """minutes 1-240 | tracks 1-50 | until 'HH:MM' | night (until the morning, quieter after 30 minutes).
        fade: seconds of fade-out at the end (default 20 s; sleep sessions 600 s), at most a third of the timer."""
        def _int(v, lo, hi):
            if v is None or isinstance(v, bool):
                return None
            try:
                n = int(float(v))
            except (TypeError, ValueError, OverflowError):
                return None
            return n if lo <= n <= hi else None

        fd = None
        if fade is not None and not isinstance(fade, bool):
            try:
                fd = float(fade)
            except (TypeError, ValueError, OverflowError):
                fd = None
            fd = fd if (fd is not None and math.isfinite(fd) and 3.0 <= fd <= 1800.0) else None
        secs = None
        if night is True:
            secs = _seconds_until('%02d:%02d' % SLEEP_MORNING)
            if secs is None or secs > 12 * 3600:  # "all night" in the day: three hours
                secs = 3 * 3600.0
        elif until is not None and minutes is None and tracks is None:
            secs = _seconds_until(until)
            if secs is None or secs > 12 * 3600:
                return {'ok': False, 'error': '到点时间要写成 HH:MM，且在 12 小时以内'}
        cancel_sess = False
        with self._lock:
            if cancel:
                if getattr(self, '_preview', None) is not None:
                    return {'ok': True, 'sleep': None, 'message': '没有设置定时关闭'}
                had = self.sleep_mode is not None
                cancel_sess = getattr(self, 'sleep_session', None) is not None
                self._clear_sleep_locked()
            else:
                had = False
        if cancel:
            if cancel_sess:
                self._sleep_session_end('cancel')
            return {'ok': True, 'sleep': None, 'message': '已取消定时关闭' if had else '没有设置定时关闭'}
        with self._lock:
            m, n = _int(minutes, 1, 240), _int(tracks, 1, 50)
            q_list = getattr(self, 'queue', ())
            q_idx = getattr(self, 'index', -1)
            cur_live = bool(getattr(self, 'playing', False) and 0 <= q_idx < len(q_list) and isinstance(q_list[q_idx], dict) and q_list[q_idx].get('is_live'))
            if secs is None and m is None and n is not None and (getattr(self, 'ambient_preset', None) or cur_live):
                m = max(15 if cur_live else 10, min(240, n * 15))
                n = None
            if secs is None and m is not None:
                secs = m * 60.0
            if secs is not None:
                if night is True:
                    fade_s = 180.0
                else:
                    fade_s = fd if fd is not None else self.SLEEP_FADE_SECONDS
                    fade_s = min(fade_s, max(self.SLEEP_FADE_SECONDS, secs / 3.0))
                self.sleep_mode = 'time'
                self.sleep_deadline = time.monotonic() + secs
                self.sleep_total = max(1, int(math.ceil(secs / 60.0)))
                self.sleep_tracks_left = 0
                self.sleep_fade = fade_s
                self.sleep_night = night is True
                self.sleep_started_mono = time.monotonic()
                self.sleep_ends_wall = time.time() + secs
                end_txt = time.strftime('%H:%M', time.localtime(self.sleep_ends_wall))
                if night is True:
                    msg = f'好的，整夜播放：半小时后自动调小，早上 {end_txt} 停止'
                elif until is not None and minutes is None:
                    msg = f'好的，{end_txt} 停止' + (f'，最后 {int(round(fade_s / 60))} 分钟慢慢变小' if fade_s >= 60 else '，到点前慢慢变小')
                elif fade_s >= 60:
                    msg = f'好的，{self.sleep_total} 分钟后停止，最后 {int(round(fade_s / 60))} 分钟慢慢变小'
                else:
                    msg = f'好的，{self.sleep_total} 分钟后音乐会慢慢变小并停止'
            elif n is not None:
                self.sleep_mode = 'tracks'
                self.sleep_deadline = 0.0
                self.sleep_total = n
                self.sleep_tracks_left = n
                self.sleep_fade = self.SLEEP_FADE_SECONDS
                self.sleep_night = False
                self.sleep_ends_wall = None
                msg = '好的，播完这首就停' if n == 1 else f'好的，再播 {n} 首就停'
            else:
                return {'ok': False, 'error': '定时时长需在 1-240 分钟，或 1-50 首'}
            pv = getattr(self, '_preview', None)
            if pv is not None:
                self._preview = None
                st = getattr(self, '_sleep_state', None)
                if isinstance(st, dict) and not isinstance(st.get('restore'), dict) and type(pv.get('prev_volume')) is int and pv['prev_volume'] != self.volume:
                    st['restore'] = {'day_volume': int(pv['prev_volume']), 'sleep_volume': int(self.volume), 'at': _next_sleep_restore_at()}
                    _write_sleep_state(self._snap_sleep_state_locked())
            pub = self._sleep_public_locked()
        log.info('Sleep timer set: %s', pub)
        return {'ok': True, 'sleep': pub, 'message': msg}

    def _sleep_gain_locked(self) -> float:
        """Gain (0..1) for the PCM block about to be played; 1.0 when no fade is due.
        Short fades (<= 60 s) use x*x; long ones fall in even loudness steps to -45 dB. All night: after
        SLEEP_NIGHT_SETTLE the sound goes down by SLEEP_NIGHT_DB. The gain never jumps up: at most +6 dB a second."""
        now = time.monotonic()
        g = 1.0
        if self.sleep_mode == 'time':
            rem = self.sleep_deadline - now
            fade = float(getattr(self, 'sleep_fade', self.SLEEP_FADE_SECONDS) or self.SLEEP_FADE_SECONDS)
            if rem < fade:
                x = max(0.0, rem / fade)
                g = x * x if fade <= 60.0 else (0.0 if rem <= 0 else 10 ** (-2.25 * (1.0 - x)))
            if getattr(self, 'sleep_night', False):
                k = (now - getattr(self, 'sleep_started_mono', now) - SLEEP_NIGHT_SETTLE) / SLEEP_NIGHT_RAMP
                if k > 0:
                    g *= 10 ** (SLEEP_NIGHT_DB / 20.0 * min(1.0, k))
        elif self.sleep_mode == 'tracks' and self.sleep_tracks_left == 1 and not getattr(self, 'ambient_preset', None):
            dur, rem = self._sleep_track_left_locked()
            if dur > 2 * self.SLEEP_FADE_SECONDS and rem < self.SLEEP_FADE_SECONDS:
                x = max(0.0, rem / self.SLEEP_FADE_SECONDS)
                g = x * x
        wk = getattr(self, '_wake', None)
        if wk is not None and getattr(self, 'ambient_preset', None) == wk['preset'] and wk.get('seq') in (None, getattr(self, 'ambient_seq', 0)) and not (self.playing and not self.paused):
            t = now - wk['start']
            if t < 3.0:
                g *= (max(0.0, t) / 3.0) ** 2   # a soft start; the volume (20% -> 50%) does the rest
            left = wk['end'] - now
            fade = float(wk.get('fade') or SLEEP_WAKE_FADE)
            if left < fade:
                g *= max(0.0, left / fade) ** 2
        last_g, last_t = getattr(self, '_sg_last', (1.0, 0.0))
        if g > last_g + 1e-4:
            g = min(g, max(last_g, 0.003) * 10 ** (0.3 * min(1.0, max(0.0, now - last_t))))
        self._sg_last = (g, now)
        return g

    @staticmethod
    def _apply_gain(buf: bytearray, g0: float, g1: float) -> None:
        """Scale one block of interleaved stereo float32 PCM in place, ramping g0 -> g1 (no zipper noise)."""
        frames = len(buf) // 8
        if frames <= 0:
            return
        np = _get_np()
        if np is not None:
            a = np.frombuffer(buf, dtype=np.float32, count=frames * 2).reshape(frames, 2)
            if abs(g1 - g0) < 1e-6:
                a *= np.float32(g1)
            else:
                a *= np.linspace(g0, g1, frames, dtype=np.float32)[:, None]
            return
        arr = _array('f')
        arr.frombytes(bytes(buf[:frames * 8]))
        step = (g1 - g0) / frames
        for i in range(frames):
            g = g0 + step * i
            arr[2 * i] *= g
            arr[2 * i + 1] *= g
        buf[:frames * 8] = arr.tobytes()

    @staticmethod
    def _mix_layer_block(buf: bytearray, lbuf: bytearray, lg0: float, lg1: float) -> None:
        """Mix secondary stereo float32 PCM block into primary block with smooth gain ramp & headroom."""
        frames = min(len(buf), len(lbuf)) // 8
        if frames <= 0:
            return
        np = _get_np()
        if np is not None:
            a = np.frombuffer(buf, dtype=np.float32, count=frames * 2).reshape(frames, 2)
            b = np.frombuffer(lbuf, dtype=np.float32, count=frames * 2).reshape(frames, 2)
            if abs(lg1 - lg0) < 1e-6:
                g = np.float32(lg1)
                hr = np.float32(1.0 / math.sqrt(1.0 + float(lg1 * lg1) * 0.45))
                np.clip((a + b * g) * hr, -0.95, 0.95, out=a)
            else:
                ramp = np.linspace(lg0, lg1, frames, dtype=np.float32)[:, None]
                mid_g = (lg0 + lg1) * 0.5
                hr = np.float32(1.0 / math.sqrt(1.0 + mid_g * mid_g * 0.45))
                np.clip((a + b * ramp) * hr, -0.95, 0.95, out=a)
            return
        arr_a = _array('f')
        arr_b = _array('f')
        arr_a.frombytes(bytes(buf[:frames * 8]))
        arr_b.frombytes(bytes(lbuf[:frames * 8]))
        step = (lg1 - lg0) / frames
        mid_g = (lg0 + lg1) * 0.5
        hr = 1.0 / math.sqrt(1.0 + mid_g * mid_g * 0.45)
        for i in range(frames):
            g = lg0 + step * i
            arr_a[2 * i] = max(-0.95, min(0.95, (arr_a[2 * i] + arr_b[2 * i] * g) * hr))
            arr_a[2 * i + 1] = max(-0.95, min(0.95, (arr_a[2 * i + 1] + arr_b[2 * i + 1] * g) * hr))
        buf[:frames * 8] = arr_a.tobytes()

    def _ambient_public_locked(self):
        ap = getattr(self, 'ambient_preset', None)
        if not ap or ap not in AMBIENT_PRESETS:
            return None
        meta = AMBIENT_PRESETS[ap]
        lay = getattr(self, 'ambient_layer', None)
        lay_meta = AMBIENT_PRESETS.get(lay) if (lay and lay != ap) else None
        lg = round(float(getattr(self, 'ambient_layer_gain', 0.45)), 2)
        aseq = int(getattr(self, 'ambient_seq', 0))
        if lay_meta:
            return {
                'preset': ap,
                'seq': aseq,
                'name': f'{meta["name"]} + {lay_meta["name"]}',
                'primary_name': meta['name'],
                'icon': meta['icon'],
                'desc': f'{meta["name"]}叠加{lay_meta["name"]}（{int(round(lg * 100))}%）· 双层立体声混音',
                'layer': lay,
                'layer_name': lay_meta['name'],
                'layer_icon': lay_meta['icon'],
                'layer_gain': lg,
            }
        return {
            'preset': ap,
            'seq': aseq,
            'name': meta['name'],
            'primary_name': meta['name'],
            'icon': meta['icon'],
            'desc': meta['desc'],
            'layer': '',
            'layer_name': '',
            'layer_icon': '',
            'layer_gain': lg,
        }

    def _call_set_ambient_internal(self, *args, **kwargs):
        tls = getattr(self, '_preview_op_tls', None)
        if tls is None:
            tls = threading.local()
            self._preview_op_tls = tls
        prev = getattr(tls, 'active', False)
        tls.active = True
        try:
            return self.set_ambient(*args, **kwargs)
        finally:
            tls.active = prev

    def set_ambient(self, preset: str, volume=None, layer=None, layer_gain=None) -> dict:
        tls = getattr(self, '_preview_op_tls', None)
        internal_op = bool(getattr(self, '_in_preview_op', False) or (tls is not None and getattr(tls, 'active', False)))
        if not internal_op:
            self._abort_preview(restore_vol=(volume is None), clear_timer=True)
        p = str(preset or '').strip().lower()
        if p in ('', 'off', 'stop', 'none', 'cancel', '0', 'false', '关闭', '停止', '静音'):
            if volume is not None and not isinstance(volume, bool):
                self.set_volume(volume)
            had = False
            flush = False
            end_sleep = False
            with self._lock:
                if getattr(self, 'ambient_preset', None):
                    had = True
                    self._clear_duck_except_home_locked()
                    self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
                    self.ambient_preset = None
                    self.ambient_layer = None
                    self._ambient_buf = None
                    self._ambient_layer_buf = None
                    self._ambient_worker_gen = -1
                    flush = self._stop_proc_locked()
                if (not internal_op
                        and not getattr(self, '_sleep_starting', False)
                        and getattr(self, 'sleep_session', None) is not None):
                    self._clear_sleep_locked()
                    end_sleep = True
            if end_sleep:
                self._sleep_session_end('stop')
            if flush:
                self._send_aec_flush()
            return {'ok': True, 'ambient': None, 'message': '已关闭环境音' if had else '环境音未开启'}
        recipe_alias = {
            '深度自习': 'deep_study', '沉浸自习': 'deep_study',
            '围炉夜读': 'cozy_read', '阅读放空': 'cozy_read',
            '深度入眠': 'deep_sleep', '深度助眠': 'deep_sleep',
        }
        alias = {
            '雨声': 'rain', '细雨': 'rain', '窗外细雨': 'rain', '下雨': 'rain', '下雨声': 'rain',
            '海浪': 'waves', '大海': 'waves', '海岸慢浪': 'waves', '潮汐': 'waves', '海浪声': 'waves',
            '森林': 'forest', '微风': 'forest', '林间微风': 'forest', '山谷微风': 'forest', '风声': 'forest', '鸟鸣': 'forest',
            '溪流': 'stream', '流水': 'stream', '山涧溪流': 'stream', '溪水': 'stream',
            '虫鸣': 'night', '夏夜虫鸣': 'night', '白噪音': 'white_noise', '柔和白噪音': 'white_noise',
            '褐噪': 'brown_noise', '褐噪音': 'brown_noise', '静心褐噪': 'brown_noise', '低沉褐噪音': 'brown_noise', '棕噪音': 'brown_noise',
            '粉噪音': 'pink_noise', '粉红噪音': 'pink_noise', '粉噪': 'pink_noise',
            '轻琴': 'sleep_piano', '雨夜轻琴': 'sleep_piano', '钢琴': 'sleep_piano', '助眠': 'sleep_piano', '催眠': 'sleep_piano',
            '壁炉': 'fireplace', '柴火': 'fireplace', '壁炉柴火': 'fireplace', '篝火': 'fireplace', '烤火': 'fireplace', 'fire': 'fireplace',
            '书房': 'library', '图书馆': 'library', '翻书': 'library', '写字': 'library', '书房自习': 'library', '自习室': 'library',
            '颂钵': 'singing_bowl', '禅意颂钵': 'singing_bowl', '冥想': 'singing_bowl', '钵音': 'singing_bowl', 'bowl': 'singing_bowl',
        }
        rec_key = recipe_alias.get(p, p)
        if rec_key in AMBIENT_RECIPES:
            rec = AMBIENT_RECIPES[rec_key]
            p = rec['preset']
            if layer is None:
                layer = rec['layer']
            if layer_gain is None:
                layer_gain = rec['layer_gain']
        p = alias.get(p, p)
        if p not in AMBIENT_PRESETS:
            return {'ok': False, 'error': f'未知环境音: {preset}'}
        lay = None
        if layer is not None:
            if isinstance(layer, bool):
                return {'ok': False, 'error': '无效叠加音'}
            l_raw = str(layer).strip().lower()
            if l_raw not in ('', 'off', 'stop', 'none', 'cancel', '0', '关闭', '不叠加'):
                l_res = alias.get(l_raw, l_raw)
                if l_res not in AMBIENT_PRESETS:
                    return {'ok': False, 'error': f'未知叠加音: {layer}'}
                if l_res != p:
                    lay = l_res
        lg = getattr(self, 'ambient_layer_gain', 0.45)
        if layer_gain is not None:
            if isinstance(layer_gain, bool):
                return {'ok': False, 'error': '无效叠加比例'}
            try:
                lg_f = float(layer_gain)
            except (TypeError, ValueError, OverflowError):
                return {'ok': False, 'error': '无效叠加比例'}
            if not math.isfinite(lg_f):
                return {'ok': False, 'error': '无效叠加比例'}
            if lg_f > 1.001:
                lg_f = lg_f / 100.0
            lg = round(max(0.10, min(1.00, lg_f)), 2)
        if volume is not None and not isinstance(volume, bool):
            self.set_volume(volume)

        with self._lock:
            self._clear_duck_except_home_locked()
            if getattr(self, 'sleep_mode', None) == 'tracks':
                m_conv = max(15, min(240, max(1, int(getattr(self, 'sleep_tracks_left', 1) or 1)) * 15))
                now_m = time.monotonic()
                self.sleep_mode = 'time'
                self.sleep_started_mono = now_m
                self.sleep_deadline = now_m + m_conv * 60
                self.sleep_ends_wall = time.time() + m_conv * 60
                self.sleep_total = m_conv
                self.sleep_tracks_left = 0
            if (
                getattr(self, 'ambient_preset', None) == p
                and getattr(self, 'ambient_layer', None) == lay
                and getattr(self, '_ambient_worker_gen', -1) == self._gen
                and not getattr(self, '_shutting_down', False)
            ):
                self.ambient_layer_gain = lg
                self.just_started = True
                ss_cur = getattr(self, 'sleep_session', None)
                if isinstance(ss_cur, dict) and not internal_op:
                    new_snd = {'kind': 'ambient', 'preset': p, 'layer': lay or '', 'layer_gain': lg if lay else None}
                    ss_cur['sound'] = new_snd
                    ss_cur['label'] = _sleep_sound_label(new_snd)
                pub = self._ambient_public_locked()
                vol = self.volume
                return {'ok': True, 'ambient': pub, 'volume': vol, 'message': f'正在播放{pub["icon"]} {pub["name"]}'}

        m4a_path = _ambient_file_for(p)
        pcm_bytes = None
        if m4a_path is None:
            cached = getattr(self, '_ambient_buf', None)
            pcm_bytes = cached[1] if (isinstance(cached, tuple) and cached[0] == p) else _fallback_ambient_pcm(p)
        lay_m4a = _ambient_file_for(lay) if lay else None
        lay_pcm = None
        if lay and lay_m4a is None:
            lcached = getattr(self, '_ambient_layer_buf', None)
            lay_pcm = lcached[1] if (isinstance(lcached, tuple) and lcached[0] == lay) else _fallback_ambient_pcm(lay)
        paused_song = False
        with self._lock:
            self._clear_duck_except_home_locked()
            if getattr(self, 'sleep_mode', None) == 'tracks':
                m_conv = max(15, min(240, max(1, int(getattr(self, 'sleep_tracks_left', 1) or 1)) * 15))
                now_m = time.monotonic()
                self.sleep_mode = 'time'
                self.sleep_started_mono = now_m
                self.sleep_deadline = now_m + m_conv * 60
                self.sleep_ends_wall = time.time() + m_conv * 60
                self.sleep_total = m_conv
                self.sleep_tracks_left = 0
            had_proc = self._stop_proc_locked()
            if self.playing and not self.paused:
                self.paused = True
                paused_song = True
            self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
            self.ambient_preset = p
            self.ambient_layer = lay
            self.ambient_layer_gain = lg
            ss_cur = getattr(self, 'sleep_session', None)
            if isinstance(ss_cur, dict) and not internal_op:
                new_snd = {'kind': 'ambient', 'preset': p, 'layer': lay or '', 'layer_gain': lg if lay else None}
                ss_cur['sound'] = new_snd
                ss_cur['label'] = _sleep_sound_label(new_snd)
            if getattr(self, '_wake', None) is None and getattr(self, 'sleep_mode', None) != 'time':
                self._sg_last = (1.0, time.monotonic())
            if pcm_bytes is not None:
                self._ambient_buf = (p, pcm_bytes)
            else:
                self._ambient_buf = None
            if lay_pcm is not None:
                self._ambient_layer_buf = (lay, lay_pcm)
            else:
                self._ambient_layer_buf = None
            self.just_started = True
            gen = self._gen
            self._ambient_worker_gen = gen
            pub = self._ambient_public_locked()
            vol = self.volume
        if had_proc:
            self._send_aec_flush()
        if paused_song:
            self.save_session()
        _yield_airplay_audio_if_active()
        self._pause_bluetooth_if_streaming()
        threading.Thread(
            target=self._ambient_worker,
            args=(gen, p, m4a_path, pcm_bytes, lay, lay_m4a, lay_pcm),
            daemon=True,
        ).start()
        return {'ok': True, 'ambient': pub, 'volume': vol, 'message': f'正在播放{pub["icon"]} {pub["name"]}'}

    def _ambient_worker(self, gen: int, preset: str, m4a_path, pcm_bytes, layer: str = None, lay_m4a=None, lay_pcm=None):
        block = 960 * 2 * 4  # 7680 bytes = 20ms @ 48kHz stereo float32
        env = {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000', 'PULSE_SERVER': 'unix:/run/user/1000/pulse/native'}
        proc = None
        proc2 = None
        pacat = None
        try:
            if m4a_path is not None:
                try:
                    proc = subprocess.Popen(
                        [
                            'ffmpeg', '-loglevel', 'error', '-nostdin',
                            '-threads', '1', '-filter_threads', '1',
                            '-stream_loop', '-1', '-i', str(m4a_path),
                            '-f', 'f32le', '-ac', '2', '-ar', str(OUT_RATE), 'pipe:1',
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    with self._lock:
                        if (
                            gen != self._gen
                            or getattr(self, 'ambient_preset', None) != preset
                            or getattr(self, 'ambient_layer', None) != layer
                            or getattr(self, '_shutting_down', False)
                        ):
                            return
                        self._proc = proc
                except Exception as e:
                    log.warning('Ambient ffmpeg decode fallback (%s): %s', preset, e)
                    if proc is not None:
                        _reap_subproc(proc)
                        if proc.stdout:
                            with contextlib.suppress(Exception):
                                proc.stdout.close()
                    proc = None
                    if pcm_bytes is None:
                        pcm_bytes = _fallback_ambient_pcm(preset)

            if layer and lay_m4a is not None:
                try:
                    proc2 = subprocess.Popen(
                        [
                            'ffmpeg', '-loglevel', 'error', '-nostdin',
                            '-threads', '1', '-filter_threads', '1',
                            '-stream_loop', '-1', '-i', str(lay_m4a),
                            '-f', 'f32le', '-ac', '2', '-ar', str(OUT_RATE), 'pipe:1',
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    with self._lock:
                        if (
                            gen != self._gen
                            or getattr(self, 'ambient_preset', None) != preset
                            or getattr(self, 'ambient_layer', None) != layer
                            or getattr(self, '_shutting_down', False)
                        ):
                            return
                        self._proc2 = proc2
                except Exception as e:
                    log.warning('Ambient layer ffmpeg fallback (%s): %s', layer, e)
                    if proc2 is not None:
                        _reap_subproc(proc2)
                        if proc2.stdout:
                            with contextlib.suppress(Exception):
                                proc2.stdout.close()
                    proc2 = None
                    if lay_pcm is None:
                        lay_pcm = _fallback_ambient_pcm(layer)

            total = ((len(pcm_bytes) // block) * block) if pcm_bytes else 0
            lay_total = ((len(lay_pcm) // block) * block) if lay_pcm else 0
            if proc is None and total <= 0:
                return
            pos = 0
            lay_pos = 0
            buf = bytearray(block)
            mv = memoryview(buf)
            lbuf = bytearray(block) if layer else None
            lmv = memoryview(lbuf) if lbuf is not None else None
            fade_prev = 0.0
            lg_prev = float(getattr(self, 'ambient_layer_gain', 0.45))
            blk_idx = 0
            next_t = time.monotonic() - 0.08
            pacat_retry_mono = 0.0
            while True:
                with self._lock:
                    if (
                        gen != self._gen
                        or getattr(self, 'ambient_preset', None) != preset
                        or getattr(self, 'ambient_layer', None) != layer
                        or getattr(self, '_shutting_down', False)
                    ):
                        break
                    muted = bool(self.alarm_ringing or self.ducked or (time.monotonic() < getattr(self, 'study_duck_until', 0.0)))
                    sleep_g = self._sleep_gain_locked()
                    if getattr(self, 'listen_duck', False):
                        sleep_g *= 0.25
                    cur_lg = float(getattr(self, 'ambient_layer_gain', 0.45))
                if muted:
                    fade_prev = 0.0
                    blk_idx = 0
                    time.sleep(0.04)
                    next_t = time.monotonic() - 0.08
                    continue

                if proc is not None:
                    got = self._readinto_exact(proc.stdout, mv)
                    if not got:
                        with self._lock:
                            stopped = (
                                gen != self._gen
                                or getattr(self, 'ambient_preset', None) != preset
                                or getattr(self, 'ambient_layer', None) != layer
                                or getattr(self, '_shutting_down', False)
                            )
                        if stopped:
                            break
                        log.warning('Ambient ffmpeg runtime EOF fallback (%s)', preset)
                        _reap_subproc(proc)
                        if proc.stdout:
                            with contextlib.suppress(Exception):
                                proc.stdout.close()
                        with self._lock:
                            if getattr(self, '_proc', None) is proc:
                                self._proc = None
                        proc = None
                        if pcm_bytes is None:
                            pcm_bytes = _fallback_ambient_pcm(preset)
                            total = (len(pcm_bytes) // block) * block
                        if total <= 0:
                            break
                        buf[:] = pcm_bytes[pos:pos + block]
                        pos = (pos + block) % total
                        next_t = time.monotonic() - 0.08
                    elif got < block:
                        aligned = (got // 8) * 8
                        mv[aligned:] = b'\x00' * (block - aligned)
                else:
                    buf[:] = pcm_bytes[pos:pos + block]
                    pos += block
                    if pos >= total:
                        pos = 0

                if layer and lbuf is not None:
                    has_lay = False
                    if proc2 is not None:
                        lgot = self._readinto_exact(proc2.stdout, lmv)
                        if lgot:
                            if lgot < block:
                                laligned = (lgot // 8) * 8
                                lmv[laligned:] = b'\x00' * (block - laligned)
                            has_lay = True
                        else:
                            with self._lock:
                                lstopped = (
                                    gen != self._gen
                                    or getattr(self, 'ambient_preset', None) != preset
                                    or getattr(self, 'ambient_layer', None) != layer
                                    or getattr(self, '_shutting_down', False)
                                )
                            if lstopped:
                                break
                            _reap_subproc(proc2)
                            if proc2.stdout:
                                with contextlib.suppress(Exception):
                                    proc2.stdout.close()
                            with self._lock:
                                if getattr(self, '_proc2', None) is proc2:
                                    self._proc2 = None
                            proc2 = None
                            if lay_pcm is None:
                                lay_pcm = _fallback_ambient_pcm(layer)
                                lay_total = (len(lay_pcm) // block) * block
                            next_t = time.monotonic()
                    if proc2 is None and lay_total > 0:
                        lbuf[:] = lay_pcm[lay_pos:lay_pos + block]
                        lay_pos += block
                        if lay_pos >= lay_total:
                            lay_pos = 0
                        has_lay = True
                    if has_lay:
                        self._mix_layer_block(buf, lbuf, lg_prev, cur_lg)
                    lg_prev = cur_lg

                blk_idx += 1
                fade_in = min(1.0, blk_idx / 40.0)  # silky 0.8s fade-in on start / resume
                tgt_g = fade_in * sleep_g
                if tgt_g < 0.9999 or fade_prev < 0.9999:
                    self._apply_gain(buf, fade_prev, tgt_g)
                fade_prev = tgt_g

                sent_to_aec = False
                abort_blk = False
                with self._aec_lock:
                    if (
                        gen != self._gen
                        or getattr(self, 'ambient_preset', None) != preset
                        or getattr(self, 'ambient_layer', None) != layer
                        or getattr(self, '_shutting_down', False)
                        or getattr(self, 'alarm_ringing', False)
                        or self.ducked
                        or time.monotonic() < getattr(self, 'study_duck_until', 0.0)
                    ):
                        abort_blk = True
                    else:
                        for sock in list(self.aec_clients):
                            try:
                                sock.sendall(mv)
                                sent_to_aec = True
                            except Exception:
                                self.aec_clients.pop(sock, None)
                                with contextlib.suppress(Exception):
                                    sock.close()
                if abort_blk:
                    fade_prev = 0.0
                    continue
                if not sent_to_aec:
                    try:
                        if pacat is None and time.monotonic() >= pacat_retry_mono:
                            pacat = subprocess.Popen(
                                ['pacat', '--rate=48000', '--channels=2', '--format=float32le', '--latency-msec=60'],
                                stdin=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                env=env,
                            )
                            with self._lock:
                                if gen != self._gen or getattr(self, 'ambient_preset', None) != preset or getattr(self, 'ambient_layer', None) != layer:
                                    break
                                self._pacat = pacat
                        if pacat is not None:
                            pacat.stdin.write(mv)
                            pacat.stdin.flush()
                    except OSError:
                        pacat_retry_mono = time.monotonic() + 0.3
                        if pacat is not None:
                            _reap_subproc(pacat)
                            if pacat.stdin:
                                with contextlib.suppress(Exception):
                                    pacat.stdin.close()
                            pacat = None
                        with self._lock:
                            if self._pacat is not None and gen == self._gen:
                                self._pacat = None
                    if pacat is None and proc is not None:
                        time.sleep(0.02)
                elif pacat is not None:
                    _reap_subproc(pacat)
                    if pacat.stdin:
                        with contextlib.suppress(Exception):
                            pacat.stdin.close()
                    pacat = None
                    with self._lock:
                        if self._pacat is not None and gen == self._gen:
                            self._pacat = None

                if proc is None:
                    next_t += 0.02
                    rem = next_t - time.monotonic()
                    if rem > 0.001:
                        time.sleep(rem)
                    elif rem < -0.15:
                        next_t = time.monotonic()
        finally:
            with self._lock:
                if proc is not None and self._proc is proc:
                    self._proc = None
                if proc2 is not None and getattr(self, '_proc2', None) is proc2:
                    self._proc2 = None
                if getattr(self, '_ambient_worker_gen', -1) == gen:
                    self._ambient_worker_gen = -1
                if self._pacat is pacat:
                    self._pacat = None
            for p_sub in (proc, proc2, pacat):
                if p_sub is not None:
                    _reap_subproc(p_sub)
                    for s in (getattr(p_sub, 'stdin', None), getattr(p_sub, 'stdout', None), getattr(p_sub, 'stderr', None)):
                        if s is not None:
                            with contextlib.suppress(Exception):
                                s.close()

    def sleep_tick(self) -> bool:
        """Called twice a second: pause once the 'time' sleep timer has run out."""
        with self._lock:
            if getattr(self, '_sleep_starting', False) or self.sleep_mode != 'time' or time.monotonic() < self.sleep_deadline:
                return False
            active = self.playing and not self.paused
            had_amb = bool(getattr(self, 'ambient_preset', None))
            self._clear_sleep_locked()
            if active or had_amb:
                self._sg_last = (0.0, time.monotonic())
        if had_amb:
            self._call_set_ambient_internal('off')
        if active:
            self.control('pause')
        with self._lock:
            if not getattr(self, 'ambient_preset', None) and getattr(self, 'sleep_mode', None) != 'time':
                self._sg_last = (1.0, time.monotonic())
        log.info('Sleep timer reached -> %s', 'paused' if (active or had_amb) else 'nothing playing')
        self._preview_end()
        self._sleep_session_end('timer')
        return True

    # ---- 2026-11-12 (Claude): sleep sessions ---------------------------------------------------------------------
    # One call: a bedtime sound, its own volume, a timer with a long fade (or until a time / all night). The volume the
    # family uses in the day comes back at 06:30 when the sound stopped in the night (00:00-06:30), else right away,
    # unless someone changed it meanwhile.
    def _sleep_session_public_locked(self):
        ss = getattr(self, 'sleep_session', None)
        if not ss or ss.get('stopping'):
            return None
        timed = self.sleep_mode == 'time'
        return {'label': ss['label'], 'kind': ss['sound']['kind'], 'preset': ss['sound'].get('preset') or '',
                'playlist': ss['sound'].get('id') or '', 'night': bool(ss.get('night')), 'volume': ss['volume'],
                'fade': int(ss['fade']), 'started': int(ss['started']),
                'ends_at': int(self.sleep_ends_wall) if (timed and self.sleep_ends_wall) else None,
                'remaining': int(math.ceil(max(0.0, self.sleep_deadline - time.monotonic()))) if timed else None}

    def _snap_sleep_state_locked(self) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self._sleep_state.items()}

    def sleep_session_info(self) -> dict:
        with self._lock:
            pub = self._sleep_session_public_locked()
            st = self._snap_sleep_state_locked()
        last = st.get('last') if isinstance(st.get('last'), dict) else None
        return {'ok': True, 'session': pub, 'last': last,
                'sleep_volume': int(st.get('sleep_volume') or SLEEP_DEFAULT_VOLUME),
                'restore': st.get('restore') if isinstance(st.get('restore'), dict) else None,
                'presets': [dict(p, mid=None) for p in AMBIENT_PRESETS.values()],
                'recipes': list(AMBIENT_RECIPES.values()), 'playlists': list(SLEEP_PLAYLISTS),
                'combos': [{'preset': m, 'layer': l, 'layer_gain': g, 'name': n} for m, l, g, n in SLEEP_COMBOS],
                'morning': '%02d:%02d' % SLEEP_MORNING, 'night_from': '%02d:%02d' % SLEEP_DAY_END,
                'wake': st.get('wake') is not False, 'wake_time': '%02d:%02d' % _wake_hm(st), 'wake_next': _wake_next(st),
                'wake_volume': list(SLEEP_WAKE_VOLUME), 'wake_screen_max': SLEEP_WAKE_SCREEN_MAX}

    def sleep_session_start(self, sound=None, minutes=None, until=None, night=False, fade=None, volume=None) -> dict:
        with self._lock:
            self._sleep_op_seq = getattr(self, '_sleep_op_seq', 0) + 1
            op_seq = self._sleep_op_seq
            self._sleep_active_start_seq = op_seq
            st_init = self._snap_sleep_state_locked()
        last = st_init.get('last') if isinstance(st_init.get('last'), dict) else {}
        use_last = sound in (None, '', 'last', '上次')
        snd = _clean_sleep_sound(last.get('sound') if use_last else sound)
        if snd is None:
            if not use_last:
                return {'ok': False, 'error': f'没有找到助眠声音：{sound}'}
            snd = {'kind': 'ambient', 'preset': 'rain', 'layer': 'fireplace', 'layer_gain': 0.4}
        timer = {}
        if night is True:
            timer = {'night': True}
        elif until:
            timer = {'until': str(until)}
        elif minutes is not None:
            timer = {'minutes': minutes}
        elif use_last and isinstance(last.get('timer'), dict):
            timer = dict(last['timer'])
        if not timer:
            timer = {'minutes': SLEEP_DEFAULT_MINUTES}
        if fade is None:
            fade = last.get('fade', SLEEP_DEFAULT_FADE) if use_last else SLEEP_DEFAULT_FADE
        try:
            raw_v = volume if (volume is not None and not isinstance(volume, bool)) else (st_init.get('sleep_volume') or SLEEP_DEFAULT_VOLUME)
            vol = int(round(float(raw_v)))
        except (TypeError, ValueError, OverflowError):
            vol = SLEEP_DEFAULT_VOLUME
        vol = max(5, min(100, vol))
        if snd['kind'] == 'current':
            with self._lock:
                sounding = bool(getattr(self, 'ambient_preset', None)) or (self.playing and not self.paused)
            if not sounding:
                return {'ok': False, 'error': '现在没有在播放，先选一个助眠声音吧'}
        tracks = None
        if snd['kind'] == 'playlist':
            tracks = qq_playlist_tracks(snd['id'], 160)
            random.shuffle(tracks)
            tracks = _qq_playable_tracks(tracks)[:60]
            if not tracks:
                return {'ok': False, 'error': '这个助眠歌单暂时放不了，换一个试试'}
        # the volume first, so the sound never starts loud
        with self._lock:
            if getattr(self, '_sleep_op_seq', op_seq) != op_seq or getattr(self, 'alarm_ringing', False):
                return {'ok': False, 'error': '已取消'}
            st = self._sleep_state
            self.sleep_session = None
            self._clear_sleep_locked()
            pv = getattr(self, '_preview', None)
            self._preview = None  # a session started during a preview takes over; the day volume is the one from before it
            wk = getattr(self, '_wake', None)
            self._wake = None
            wk_day = (wk.get('day_vol') if (isinstance(wk, dict) and not wk.get('user_vol') and type(wk.get('day_vol')) is int) else None)
            cur_vol = pv['prev_volume'] if pv else (wk_day if wk_day is not None else self.volume)
            r = st.get('restore') if isinstance(st.get('restore'), dict) else None
            reuse_r = bool(
                r and type(r.get('day_volume')) is int
                and (r.get('at') is None or (type(r.get('sleep_volume')) is int and abs(cur_vol - r['sleep_volume']) <= 1))
            )
            day = int(r['day_volume']) if reuse_r else cur_vol
            st['restore'] = {'day_volume': day, 'sleep_volume': vol, 'at': None}
            self._sleep_starting_count = getattr(self, '_sleep_starting_count', 0) + 1
            self._sleep_starting = True
        if wk is not None:
            _sunrise_mark_stopped()
        try:
            self.set_volume(vol)
            if snd['kind'] == 'ambient':
                res = self.set_ambient(snd['preset'], None, snd.get('layer') or ('off' if snd['preset'] in AMBIENT_PRESETS else None),
                                       snd.get('layer_gain'))
            elif snd['kind'] == 'playlist':
                res = self.play_tracks(tracks, 0)
            else:
                res = {'ok': True}
            if not res.get('ok'):
                with self._lock:
                    st_snap = self._snap_sleep_state_locked()
                _write_sleep_state(st_snap)
                self._sleep_session_end('failed')
                return {'ok': False, 'error': res.get('error') or '声音没能开始'}
            with self._lock:
                if getattr(self, '_sleep_op_seq', op_seq) != op_seq or getattr(self, 'alarm_ringing', False):
                    superseded_abort = True
                    newer_start_abort = (
                        getattr(self, '_sleep_active_start_seq', 0) not in (0, op_seq)
                        and not getattr(self, 'alarm_ringing', False)
                    )
                else:
                    superseded_abort = False
                    newer_start_abort = False
                    self._sg_last = (1.0, time.monotonic())
            if superseded_abort:
                if not newer_start_abort:
                    if snd['kind'] == 'ambient':
                        self.set_ambient('off')
                    elif snd['kind'] == 'playlist':
                        self.control('stop')
                    self._sleep_session_end('cancelled')
                return {'ok': False, 'error': '已取消'}
            tres = self.set_sleep(timer.get('minutes'), None, False, fade, timer.get('until'), timer.get('night') is True)
            if not tres.get('ok'):
                tres = self.set_sleep(SLEEP_DEFAULT_MINUTES, None, False, fade)
                timer = {'minutes': SLEEP_DEFAULT_MINUTES}
            label = _sleep_sound_label(snd)
            with self._lock:
                if getattr(self, '_sleep_op_seq', op_seq) != op_seq or getattr(self, 'alarm_ringing', False):
                    superseded_commit = True
                    newer_start_commit = (
                        getattr(self, '_sleep_active_start_seq', 0) not in (0, op_seq)
                        and not getattr(self, 'alarm_ringing', False)
                    )
                    if not newer_start_commit:
                        self._clear_sleep_locked()
                else:
                    superseded_commit = False
                    newer_start_commit = False
                    st = self._sleep_state
                    self.sleep_session = {'sound': snd, 'label': label, 'started': time.time(), 'night': timer.get('night') is True,
                                          'fade': float(getattr(self, 'sleep_fade', SLEEP_DEFAULT_FADE)), 'volume': vol}
                    self._sleep_idle_since = None
                    if snd['kind'] != 'current':
                        st['last'] = {'sound': snd, 'timer': timer, 'fade': fade}
                    st['sleep_volume'] = max(5, min(60, int(vol)))
                    st['last_started'] = int(time.time())   # 温柔叫醒 runs the morning after a night with sleep mode
                    st_snap = self._snap_sleep_state_locked()
                    pub = self._sleep_session_public_locked()
            if superseded_commit:
                if not newer_start_commit:
                    if snd['kind'] == 'ambient':
                        self.set_ambient('off')
                    elif snd['kind'] == 'playlist':
                        self.control('stop')
                    self._sleep_session_end('cancelled')
                return {'ok': False, 'error': '已取消'}
            _write_sleep_state(st_snap)
        finally:
            with self._lock:
                self._sleep_starting_count = max(0, getattr(self, '_sleep_starting_count', 1) - 1)
                self._sleep_starting = self._sleep_starting_count > 0
        log.info('Sleep session: %s, %s, volume %d (day %d)', label, timer, vol, day)
        msg = f'好的，{label}。' + str(tres.get('message') or '').replace('好的，', '') + f'。音量调到 {vol}%，晚安'
        return {'ok': True, 'session': pub, 'message': msg}

    def sleep_session_extend(self, minutes=30) -> dict:
        try:
            add = 30 if isinstance(minutes, bool) else max(5, min(120, int(float(minutes))))
        except (TypeError, ValueError, OverflowError):
            add = 30
        with self._lock:
            if self.sleep_mode != 'time':
                return {'ok': False, 'error': '现在没有定时'}
            if self.sleep_night:
                return {'ok': False, 'error': '整夜模式会在早上自己停'}
            now = time.monotonic()
            left = max(self.sleep_deadline, now) - now
            self.sleep_deadline = now + left + add * 60
            self.sleep_total = max(1, int(math.ceil((self.sleep_deadline - self.sleep_started_mono) / 60.0)))
            self.sleep_ends_wall = time.time() + (self.sleep_deadline - now)
            pub = self._sleep_session_public_locked()
            end_txt = time.strftime('%H:%M', time.localtime(self.sleep_ends_wall))
        log.info('Sleep timer extended by %d min (ends %s)', add, end_txt)
        return {'ok': True, 'session': pub, 'message': f'好的，再放 {add} 分钟，{end_txt} 停止'}

    def sleep_session_stop(self) -> dict:
        with self._lock:
            self._sleep_op_seq = getattr(self, '_sleep_op_seq', 0) + 1
            self._sleep_active_start_seq = 0
            has_wk = getattr(self, '_wake', None) is not None
            has_ss = getattr(self, 'sleep_session', None) is not None
            has_pv = getattr(self, '_preview', None) is not None
        if has_wk:
            self._wake_stop_soon('stop')
            if not has_ss:
                return {'ok': True, 'message': '好的，已停止'}
        if not has_ss:
            if has_pv:
                return self.sleep_preview_stop()
            with self._lock:
                if bool(getattr(self, 'ambient_preset', None)):
                    self.sleep_mode = 'time'
                    self.sleep_deadline = time.monotonic() + 2.5
                    self.sleep_fade = 2.5
                    self.sleep_night = False
                    self.sleep_tracks_left = 0
                    return {'ok': True, 'message': '好的，已停止'}
            return {'ok': True, 'message': '睡眠模式未开启'}
        with self._lock:
            sounding = bool(getattr(self, 'ambient_preset', None)) or (self.playing and not self.paused)
            if sounding:  # a short fade; sleep_tick stops the sound and ends the session
                if isinstance(getattr(self, 'sleep_session', None), dict):
                    self.sleep_session['stopping'] = True
                self.sleep_mode = 'time'
                self.sleep_deadline = time.monotonic() + 2.5
                self.sleep_fade = 2.5
                self.sleep_night = False
                self.sleep_tracks_left = 0
            else:
                self._clear_sleep_locked()
        if not sounding:
            self._sleep_session_end('stop')
        return {'ok': True, 'message': '好的，已停止'}

    def _sleep_session_end(self, why: str):
        st_snap = None
        day_vol_log = None
        restore_at_log = None
        with self._lock:
            had = getattr(self, 'sleep_session', None) is not None
            self.sleep_session = None
            self._sleep_idle_since = None
            st = getattr(self, '_sleep_state', None)
            r = st.get('restore') if isinstance(st, dict) else None
            if isinstance(r, dict) and r.get('at') is None:
                r['at'] = _next_sleep_restore_at()
                day_vol_log = r.get('day_volume')
                restore_at_log = r['at']
                st_snap = self._snap_sleep_state_locked()
        if st_snap is not None:
            _write_sleep_state(st_snap)
            log.info('Sleep session ended (%s); day volume %s back at %s', why, day_vol_log,
                     time.strftime('%m-%d %H:%M', time.localtime(restore_at_log)))
        elif had:
            log.info('Sleep session ended (%s)', why)

    # ---- 试听 (11-12): the chosen sound at the sleep volume for half a minute; no session, no timer shown, no dark screen
    def _preview_public_locked(self):
        pv = getattr(self, '_preview', None)
        if not pv or self.sleep_mode != 'time':
            return None
        return {'label': pv['label'], 'remaining': int(math.ceil(max(0.0, self.sleep_deadline - time.monotonic())))}

    def sleep_preview(self, sound=None, volume=None, seconds=30) -> dict:
        with self._lock:
            if getattr(self, 'sleep_session', None) is not None:
                return {'ok': False, 'error': '睡眠正在进行，不用试听'}
            sv_def = self._sleep_state.get('sleep_volume')
        snd = _clean_sleep_sound(sound)
        if snd is None or snd['kind'] != 'ambient':
            return {'ok': False, 'error': '歌单不能试听，直接开始就能听' if (snd and snd['kind'] == 'playlist') else '没有找到这个声音'}
        try:
            raw_v = volume if (volume is not None and not isinstance(volume, bool)) else (sv_def or SLEEP_DEFAULT_VOLUME)
            vol = int(round(float(raw_v)))
        except (TypeError, ValueError, OverflowError):
            vol = SLEEP_DEFAULT_VOLUME
        vol = max(5, min(100, vol))
        try:
            s_f = 30.0 if isinstance(seconds, bool) else float(seconds)
            secs = max(5.0, min(60.0, s_f)) if math.isfinite(s_f) else 30.0
        except (TypeError, ValueError, OverflowError):
            secs = 30.0
        with self._lock:
            pv = getattr(self, '_preview', None)
            wk = getattr(self, '_wake', None)
            self._wake = None
            wk_day = (wk.get('day_vol') if (isinstance(wk, dict) and not wk.get('user_vol') and type(wk.get('day_vol')) is int) else None)
            prev = pv['prev_volume'] if pv else (wk_day if wk_day is not None else self.volume)
            preview_tok = {'prev_volume': prev, 'volume': vol, 'label': _sleep_sound_label(snd)}
            self._preview = preview_tok
            if self.sleep_mode == 'time':
                self.sleep_deadline = time.monotonic() + secs + 5.0
            cur = self.volume
        if wk is not None:
            _sunrise_mark_stopped()
        if cur != vol:
            self.set_volume(vol)
        res = self._call_set_ambient_internal(
            snd['preset'], None, snd.get('layer') or ('off' if snd['preset'] in AMBIENT_PRESETS else None),
            snd.get('layer_gain'),
        )
        if not res.get('ok'):
            with self._lock:
                superseded = self._preview is not preview_tok
            if not superseded:
                self._preview_end()
            return {'ok': False, 'error': res.get('error') or '声音没能播放'}
        with self._lock:
            if self._preview is not preview_tok:
                return {'ok': False, 'error': '试听已被新的操作替换'}
            now = time.monotonic()
            self.sleep_mode = 'time'
            self.sleep_deadline = now + secs
            self.sleep_total = 1
            self.sleep_tracks_left = 0
            self.sleep_fade = 2.0
            self.sleep_night = False
            self.sleep_started_mono = now
            self.sleep_ends_wall = time.time() + secs
            self._sg_last = (1.0, now)
            pub = self._preview_public_locked()
        log.info('Sleep preview: %s at volume %d for %ds', pub['label'] if pub else '?', vol, int(secs))
        return {'ok': True, 'preview': pub, 'message': f'试听：{pub["label"] if pub else ""}'}

    def sleep_preview_stop(self) -> dict:
        with self._lock:
            if getattr(self, '_preview', None) is None:
                return {'ok': True, 'message': '没有在试听'}
            now = time.monotonic()
            if self.sleep_mode == 'time' and self.sleep_deadline > now + 1.2:
                self.sleep_deadline = now + 1.2  # a short fade; sleep_tick stops it and gives the volume back
                self.sleep_fade = 1.2
        return {'ok': True, 'message': '已停止试听'}

    def _abort_preview(self, restore_vol: bool = True, clear_timer: bool = True):
        with self._lock:
            pv = getattr(self, '_preview', None)
            if pv is None:
                return
            self._preview = None
            if clear_timer and self.sleep_mode == 'time' and getattr(self, 'sleep_session', None) is None:
                self._clear_sleep_locked()
            cur = self.volume
            ext_busy = bool(
                getattr(self, 'alarm_ringing', False)
                or getattr(self, 'bt_streaming', False)
                or CALL_FLAG_PATH.exists()
                or MIRROR_FLAG_PATH.exists()
                or MIRROR_FLAG_PATH.with_name('uxplay-audio-active').exists()
            )
            pv_vol = pv.get('volume')
            prev_vol = pv.get('prev_volume')
            need_vol = bool(restore_vol and cur == pv_vol and prev_vol != cur)
            need_defer = bool(restore_vol and ext_busy and type(prev_vol) is int and type(pv_vol) is int and prev_vol != pv_vol)
            st_snap = None
            if need_defer:
                st = getattr(self, '_sleep_state', None)
                if isinstance(st, dict):
                    st['restore'] = {'day_volume': int(prev_vol), 'sleep_volume': cur if cur == pv_vol else int(pv_vol), 'at': time.time()}
                    st_snap = self._snap_sleep_state_locked()
        if st_snap is not None:
            _write_sleep_state(st_snap)
        elif need_vol and not ext_busy:
            self.set_volume(prev_vol)

    def _preview_end(self):
        self._abort_preview(restore_vol=True, clear_timer=False)

    def sleep_screen_off(self) -> dict:
        with self._lock:
            active = getattr(self, 'sleep_session', None) is not None
            alarm = getattr(self, 'alarm_ringing', False)
        if not active and not _is_sleep_evening():
            return {'ok': False, 'error': '睡眠模式没在进行'}
        if alarm or CALL_FLAG_PATH.exists() or MIRROR_FLAG_PATH.exists():
            return {'ok': False, 'error': '当前正在通话、投屏或响铃，不能熄屏'}
        try:
            if BACKLIGHT_PATH.is_symlink():
                raise OSError('Refusing symlink backlight path')
            BACKLIGHT_PATH.write_text(f'{SLEEP_BLANK}\n')  # the idle blanker pauses the renderer; a touch wakes it
        except OSError as e:
            return {'ok': False, 'error': f'熄屏失败：{type(e).__name__}'}
        return {'ok': True}

    def sleep_night_view(self, on) -> dict:
        """The night view is up (or gone): the blanker keeps the screen at its lowest level while it is up."""
        try:
            if on is True:
                _write_runtime_json(NIGHT_VIEW_PATH, {'until': int(time.time() + 12 * 3600)})
            else:
                NIGHT_VIEW_PATH.unlink(missing_ok=True)
        except OSError as e:
            return {'ok': False, 'error': f'夜间画面状态没写成：{type(e).__name__}'}
        return {'ok': True}

    def sleep_set_volume(self, volume) -> dict:
        """2026-11-25 Claude：设置页里的「助眠音量」——下次开睡眠模式用的音量（5–60）。正在助眠时不改现在的音量。"""
        if (
            isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or volume != volume
            or volume in (float('inf'), float('-inf'))
            or int(volume) != volume
            or not 5 <= volume <= 60
        ):
            return {'ok': False, 'error': '助眠音量要在 5 到 60 之间'}
        with self._lock:
            self._sleep_state['sleep_volume'] = int(volume)
            st_snap = self._snap_sleep_state_locked()
        _write_sleep_state(st_snap)
        return {'ok': True, 'sleep_volume': int(volume)}

    def sleep_set_wake(self, on=None, time_s=None) -> dict:
        """The 温柔叫醒 switch and/or its time ('HH:MM', 04:30-10:00)."""
        if on is None and time_s is None:
            return {'ok': False, 'error': '没有要改的'}
        if on is not None and not isinstance(on, bool):
            return {'ok': False, 'error': '开关只能是开或关'}
        hm_set = None
        if time_s is not None:
            m = re.match(r'^(\d{1,2}):(\d{2})$', str(time_s))
            hm = (int(m.group(1)), int(m.group(2))) if m else None
            if not hm or hm[1] > 59 or not ((4, 30) <= hm <= (10, 0)):
                return {'ok': False, 'error': '叫醒时间要在 4:30 到 10:00 之间'}
            hm_set = '%02d:%02d' % hm
        with self._lock:
            st = self._sleep_state
            if hm_set is not None:
                st['wake_time'] = hm_set
            if on is not None:
                st['wake'] = on
            st_snap = self._snap_sleep_state_locked()
            wake_off = st.get('wake') is False
            has_wake = getattr(self, '_wake', None) is not None
            nxt = _wake_next(st)
            hm_out = '%02d:%02d' % _wake_hm(st)
        _write_sleep_state(st_snap)
        if wake_off and has_wake:
            self._wake_stop_soon('the switch')
        log.info('Sleep: 温柔叫醒 %s at %s', 'off' if wake_off else 'on', nxt['time'])
        if wake_off:
            msg = '温柔叫醒已关闭'
        elif nxt['off']:
            msg = f'温柔叫醒定在 {nxt["time"]}；{nxt["when"]}是{nxt["why"] or "休息日"}，不叫'
        else:
            msg = f'温柔叫醒定在 {nxt["time"]}（工作日）'
        return {'ok': True, 'wake': not wake_off, 'wake_time': hm_out, 'wake_next': nxt, 'message': msg}

    def _wake_stop_soon(self, why):
        """A touch / the switch: the birds go in 2 seconds, the screen goes back."""
        with self._lock:
            wk = getattr(self, '_wake', None)
            if wk is None or wk.get('stopping'):
                return
            now = time.monotonic()
            wk['stopping'] = why
            wk['fade'] = 2.0
            wk['end'] = min(wk['end'], now + 2.0)
        _sunrise_mark_stopped()
        log.info('温柔叫醒: stopped (%s)', why)

    def sleep_wake_tick(self):
        """Every 2 s. At the user's time (default 06:30), on a workday by the calendar, the morning after a night with
        sleep mode: the volume goes 20% -> 50% and birds play; the screen owner brightens the screen up to 50%.
        Not over music, an alarm, a call or AirPlay; a sleep sound still on makes way; once a morning."""
        with self._lock:
            wk = getattr(self, '_wake', None)
            st = self._snap_sleep_state_locked()
            ss_active = getattr(self, 'sleep_session', None) is not None
        if wk is not None:
            self._wake_step(wk)
            return
        now = time.time()
        if NIGHT_VIEW_PATH.exists() and not _is_sleep_evening(now) and not ss_active:
            with contextlib.suppress(OSError):
                NIGHT_VIEW_PATH.unlink()
        if st.get('wake') is False:
            return
        h, mi = _wake_hm(st)
        lt = time.localtime(now)
        at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        day = time.strftime('%Y-%m-%d', lt)
        if not (at <= now < at + 900) or st.get('wake_done') == day:
            return
        started = st.get('last_started')
        if not (type(started) in (int, float) and at - 15 * 3600 <= started < at):
            return
        off, why = _wake_day_info(lt.tm_year, lt.tm_mon, lt.tm_mday)
        if off:
            with self._lock:
                self._sleep_state['wake_done'] = day
                st_snap = self._snap_sleep_state_locked()
            _write_sleep_state(st_snap)
            log.info('温柔叫醒 skipped: a day off (%s)', why or 'calendar')
            return
        with self._lock:
            ss = getattr(self, 'sleep_session', None)
            alarm = getattr(self, 'alarm_ringing', False)
            music = getattr(self, 'playing', False) and not getattr(self, 'paused', False)
            preview = getattr(self, '_preview', None) is not None
            other = bool(getattr(self, 'ambient_preset', None)) and ss is None
        if ss is not None and not alarm:   # the sleep sound is still on (all night / a long timer): it makes way
            if not ss.get('stopping'):
                log.info('温柔叫醒: the sleep sound makes way')
                self.sleep_session_stop()
            return
        busy_ext = (alarm or music or preview or other or CALL_FLAG_PATH.exists()
                    or MIRROR_FLAG_PATH.exists() or MIRROR_FLAG_PATH.with_name('uxplay-audio-active').exists())
        v0 = SLEEP_WAKE_VOLUME[0]
        mono = time.monotonic()
        with self._lock:
            cur_st = self._sleep_state
            cur_st['wake_done'] = day
            if busy_ext:
                st_snap = self._snap_sleep_state_locked()
                self._wake = None
            else:
                r = cur_st.get('restore')
                day_vol = int(r['day_volume']) if isinstance(r, dict) and type(r.get('day_volume')) is int else int(getattr(self, 'volume', 50))
                cur_st['restore'] = None   # the wake-up gives the day volume back itself, when it is over
                st_snap = self._snap_sleep_state_locked()
                self._sg_last = (0.003, mono)
                self._wake = {'start': mono, 'ramp': float(SLEEP_WAKE_RAMP), 'preset': SLEEP_WAKE_SOUND,
                              'end': mono + SLEEP_WAKE_RAMP + SLEEP_WAKE_HOLD + SLEEP_WAKE_FADE, 'fade': float(SLEEP_WAKE_FADE),
                              'day_vol': day_vol, 'vol': v0, 'user_vol': False, 'sun': int(now)}
        _write_sleep_state(st_snap)
        if busy_ext:
            log.info('温柔叫醒 skipped: something is playing, an alarm, a call or AirPlay')
            return
        self.set_volume(v0)
        with contextlib.suppress(OSError):
            _write_runtime_json(SUNRISE_PATH, {'start': int(now), 'ramp': SLEEP_WAKE_RAMP,
                                               'hold': SLEEP_WAKE_HOLD + SLEEP_WAKE_FADE, 'max': SLEEP_WAKE_SCREEN_MAX})
        res = self.set_ambient(SLEEP_WAKE_SOUND, None, 'off', None)
        if not res.get('ok'):
            with self._lock:
                self._wake = None
            _sunrise_mark_stopped()
            if getattr(self, 'volume', None) == v0:
                self.set_volume(day_vol)
            log.warning('温柔叫醒: the sound did not start (%s)', res.get('error'))
            return
        with self._lock:
            wk_cur = getattr(self, '_wake', None)
            if isinstance(wk_cur, dict):
                wk_cur['seq'] = (res.get('ambient') or {}).get('seq')
        log.info('温柔叫醒: %s, volume %d%% -> %d%%, the screen up to %d%%', AMBIENT_PRESETS[SLEEP_WAKE_SOUND]['name'],
                 SLEEP_WAKE_VOLUME[0], SLEEP_WAKE_VOLUME[1], SLEEP_WAKE_SCREEN_MAX)

    def _wake_step(self, wk):
        """A wake-up in progress: the volume step, a touch, its end."""
        now_m = time.monotonic()
        with self._lock:
            mine = getattr(self, 'ambient_preset', None) == wk['preset'] and wk.get('seq') in (None, getattr(self, 'ambient_seq', 0))
            music = getattr(self, 'playing', False) and not getattr(self, 'paused', False)
            alarm = getattr(self, 'alarm_ringing', False)
            bt_busy = bool(getattr(self, 'bt_streaming', False))
            cur = int(getattr(self, 'volume', 0))
            stopping = bool(wk.get('stopping'))
            user_vol = bool(wk.get('user_vol'))
            wk_vol = wk.get('vol')
            wk_day_vol = wk.get('day_vol')
            wk_sun = wk.get('sun')
            wk_end = wk.get('end')
        ext_busy = bool(alarm or bt_busy or CALL_FLAG_PATH.exists() or MIRROR_FLAG_PATH.exists() or MIRROR_FLAG_PATH.with_name('uxplay-audio-active').exists())
        if not stopping:
            touched = False
            try:
                data = json.loads(_read_regular_text(SUNRISE_PATH, 4096))
                touched = isinstance(data, dict) and data.get('start') == wk_sun and 'stopped' in data
            except (OSError, ValueError):
                pass
            if touched:
                self._wake_stop_soon('a touch on the screen')
                return
        if not mine or music or ext_busy or now_m >= wk_end:
            st_snap = None
            with self._lock:
                if getattr(self, '_wake', None) is wk:
                    self._wake = None
                self._sg_last = (1.0, now_m)
                if not user_vol and type(wk_day_vol) is int and type(wk_vol) is int and wk_day_vol != wk_vol and ext_busy:
                    st = getattr(self, '_sleep_state', None)
                    if isinstance(st, dict):
                        st['restore'] = {'day_volume': wk_day_vol, 'sleep_volume': cur if cur == wk_vol else wk_vol, 'at': time.time()}
                        st_snap = self._snap_sleep_state_locked()
            if mine:
                self.set_ambient('off')
            if st_snap is not None:
                _write_sleep_state(st_snap)
            elif not user_vol and cur == wk_vol and wk_day_vol != cur and not ext_busy:
                self.set_volume(wk_day_vol)   # the volume from before (unless someone set one meanwhile)
            if now_m < wk_end or not mine:
                _sunrise_mark_stopped()
            log.info('温柔叫醒 finished%s', '' if mine and not music and not ext_busy else ' (the sound was changed / music / busy)')
            return
        if user_vol or stopping:
            return
        if cur != wk_vol:   # someone set a volume: theirs from now on
            with self._lock:
                wk['user_vol'] = True
            return
        v0, v1 = SLEEP_WAKE_VOLUME
        want = int(round(v0 + (v1 - v0) * min(1.0, max(0.0, now_m - wk['start']) / wk['ramp'])))
        if want != cur:
            with self._lock:
                wk['vol'] = want
            self.set_volume(want)

    def on_alarm_ringing(self) -> bool:
        """When a wake-up alarm rings during a sleep session, preview, or sunrise wake, stop bedtime audio
        immediately so it does not resume after the alarm is dismissed, and mark day_volume due once alarm clears."""
        with self._lock:
            ss = getattr(self, 'sleep_session', None)
            pv = getattr(self, '_preview', None)
            wk = getattr(self, '_wake', None)
            starting = bool(getattr(self, '_sleep_starting', False))
            if ss is None and pv is None and wk is None and not starting:
                return False
            self._sleep_op_seq = getattr(self, '_sleep_op_seq', 0) + 1
            self._sleep_active_start_seq = 0
            self._preview = None
            self._wake = None
            active = bool(self.playing and not self.paused) if ss is not None else False
            had_amb = bool(getattr(self, 'ambient_preset', None))
            if ss is not None or self.sleep_mode == 'time':
                self._clear_sleep_locked()
            if active or had_amb:
                self._sg_last = (0.0, time.monotonic())
            st = getattr(self, '_sleep_state', None)
            if isinstance(st, dict):
                if pv is not None and type(pv.get('prev_volume')) is int and type(pv.get('volume')) is int and pv['prev_volume'] != pv['volume']:
                    st['restore'] = {'day_volume': int(pv['prev_volume']), 'sleep_volume': int(pv['volume']), 'at': time.time()}
                elif wk is not None and not wk.get('user_vol') and type(wk.get('day_vol')) is int and type(wk.get('vol')) is int and wk['day_vol'] != wk['vol']:
                    st['restore'] = {'day_volume': int(wk['day_vol']), 'sleep_volume': int(wk['vol']), 'at': time.time()}
        if had_amb:
            self._call_set_ambient_internal('off')
        if active:
            self.control('pause')
        with self._lock:
            if not getattr(self, 'ambient_preset', None) and getattr(self, 'sleep_mode', None) != 'time':
                self._sg_last = (1.0, time.monotonic())
        if wk is not None:
            _sunrise_mark_stopped()
        if ss is not None:
            self._sleep_session_end('alarm')
        st_snap = None
        with self._lock:
            st = getattr(self, '_sleep_state', None)
            r = st.get('restore') if isinstance(st, dict) else None
            if isinstance(r, dict):
                r['at'] = time.time()
                st_snap = self._snap_sleep_state_locked()
        if st_snap is not None:
            _write_sleep_state(st_snap)
        return True

    def sleep_housekeeping(self):
        """Every 2 s: bring the day volume back when it is due; end a session whose sound was stopped another way."""
        do_rp_flush = False
        with self._lock:
            if self.playing and not self.paused and (time.monotonic() - self._radio_prog_saved_mono) >= 60.0:
                if self._snapshot_radio_progress_locked():
                    do_rp_flush = True
        if do_rp_flush:
            self._flush_radio_progress()
        if getattr(self, 'alarm_ringing', False) and (
            getattr(self, 'sleep_session', None) is not None
            or getattr(self, '_preview', None) is not None
            or getattr(self, '_wake', None) is not None
        ):
            self.on_alarm_ringing()
        st_write = None
        with self._lock:
            r = self._sleep_state.get('restore')
            if not getattr(self, '_sleep_recovered', False):
                # once, right after a (re)start: a session cut short by the restart still gets its day volume back. Only
                # here: later on, a restore without a time belongs to a session that is being started (11-12 race fix).
                self._sleep_recovered = True
                if isinstance(r, dict) and r.get('at') is None and getattr(self, 'sleep_session', None) is None and not getattr(self, '_sleep_starting', False):
                    r['at'] = _next_sleep_restore_at()
                    st_write = self._snap_sleep_state_locked()
        if st_write is not None:
            _write_sleep_state(st_write)
        if isinstance(r, dict) and r.get('at') and time.time() >= float(r['at']) and not getattr(self, 'alarm_ringing', False):
            do_restore_vol = None
            st_clear = None
            with self._lock:
                cur_r = self._sleep_state.get('restore')
                cur = self.volume
                busy = (getattr(self, 'sleep_session', None) is not None
                        or getattr(self, '_sleep_starting', False)
                        or getattr(self, '_preview', None) is not None
                        or getattr(self, '_wake', None) is not None
                        or getattr(self, 'bt_streaming', False)
                        or CALL_FLAG_PATH.exists()
                        or MIRROR_FLAG_PATH.exists()
                        or MIRROR_FLAG_PATH.with_name('uxplay-audio-active').exists())
                if not busy and cur_r is r and cur_r.get('at') is not None:
                    if type(r.get('sleep_volume')) is int and abs(cur - r['sleep_volume']) <= 1 and type(r.get('day_volume')) is int:
                        do_restore_vol = (r['day_volume'], cur)
                    else:
                        log.info('Sleep: volume was changed to %d meanwhile, leaving it', cur)
                    self._sleep_state['restore'] = None
                    st_clear = self._snap_sleep_state_locked()
            if do_restore_vol is not None:
                self.set_volume(do_restore_vol[0])
                log.info('Sleep: day volume %d is back (was %d)', do_restore_vol[0], do_restore_vol[1])
            if st_clear is not None:
                _write_sleep_state(st_clear)
        if isinstance(r, dict) and r.get('at') is None and getattr(self, 'sleep_session', None) is not None:
            st_upd = None
            with self._lock:
                ss = getattr(self, 'sleep_session', None)
                cur = self.volume
                sounding_ss = bool(getattr(self, 'ambient_preset', None)) or (self.playing and not self.paused)
                ext_busy = bool(
                    getattr(self, 'alarm_ringing', False)
                    or getattr(self, 'bt_streaming', False)
                    or CALL_FLAG_PATH.exists()
                    or MIRROR_FLAG_PATH.exists()
                    or MIRROR_FLAG_PATH.with_name('uxplay-audio-active').exists()
                )
                if ss is not None and not ss.get('stopping') and sounding_ss and not ext_busy:
                    ss['volume'] = cur
                    cur_r = self._sleep_state.get('restore')
                    if isinstance(cur_r, dict) and cur_r.get('at') is None and cur_r.get('sleep_volume') != cur:
                        cur_r['sleep_volume'] = cur
                        self._sleep_state['sleep_volume'] = max(5, min(60, int(cur)))
                        st_upd = self._snap_sleep_state_locked()
            if st_upd is not None:
                _write_sleep_state(st_upd)
        with self._lock:
            if getattr(self, 'sleep_session', None) is None:
                return
            if (
                CALL_FLAG_PATH.exists()
                or MIRROR_FLAG_PATH.exists()
                or MIRROR_FLAG_PATH.with_name('uxplay-audio-active').exists()
            ):
                self._sleep_idle_since = None
                return
            sounding = bool(getattr(self, 'ambient_preset', None)) or (self.playing and not self.paused)
            if sounding:
                self._sleep_idle_since = None
                return
            if self._sleep_idle_since is None:
                self._sleep_idle_since = time.monotonic()
                return
            if time.monotonic() - self._sleep_idle_since < 8.0:
                return
            if self.sleep_mode == 'time':
                self._clear_sleep_locked()
        self._sleep_session_end('sound stopped')

    def current_track(self):
        with self._lock:
            if 0 <= self.index < len(self.queue):
                return self.queue[self.index]
            return None

    def status(self, lite: bool = False, qseq: int = None, lseq: int = None):
        with self._lock:
            t = self.queue[self.index] if (self.playing and 0 <= self.index < len(self.queue)) else None
            playing = self.playing
            paused = self.paused
            alarm_ringing = self.alarm_ringing
            elapsed = round(self.elapsed_base if playing else 0.0, 1)
            dur = self._safe_dur(t.get('duration')) if t else 0
            amb_pub = self._ambient_public_locked()
            ui_mono = getattr(self, '_open_ui_mono', 0.0) or 0.0
            ui_fresh = bool(self.open_ui_seq > 0 and ui_mono > 0.0 and 0.0 <= (time.monotonic() - ui_mono) < 10.0)
            ducked = bool(self.ducked or time.monotonic() < getattr(self, 'study_duck_until', 0.0))
            if lite:
                return {
                    'playing': bool(playing and not paused and not amb_pub),
                    'paused': paused,
                    'ducked': ducked,
                    'alarm_ringing': alarm_ringing,
                    'boot_id': self.boot_id,
                    'open_ui_seq': self.open_ui_seq,
                    'open_ui_fresh': ui_fresh,
                    'open_ui_tab': (getattr(self, '_open_ui_tab', '') if ui_fresh else ''),
                    'elapsed': elapsed,
                    'duration': dur,
                    'volume': getattr(self, 'volume', 55),
                    'play_mode': getattr(self, 'play_mode', 'loop'),
                    'lyrics_seq': getattr(self, 'lyrics_seq', 1),
                    'ambient': amb_pub,
                    'sleep': self._sleep_public_locked(),
                    'sleep_session': self._sleep_session_public_locked(),
                    'sleep_preview': self._preview_public_locked(),
                    'track': {
                        'mid': t.get('mid', ''),
                        'file': t.get('url', '') if t.get('is_local') else '',
                        'title': t.get('title', ''),
                        'artist': t.get('artist', ''),
                        'cover': t.get('thumb') or t.get('cover', ''),
                        'is_live': bool(t.get('is_live')),
                        'radio_id': t.get('radio_id', ''),
                        'desc': t.get('desc', ''),
                        'missing': _local_track_gone(t),
                    } if t else None,
                }
            q_seq = self.queue_seq
            l_seq = self.lyrics_seq
            volume = self.volume
            started_m = getattr(self, 'play_started_mono', 0.0)
            index = self.index
            queue_len = len(self.queue)
            play_mode = self.play_mode
            sleep_pub = self._sleep_public_locked()
            sess_pub = self._sleep_session_public_locked()
            prev_pub = self._preview_public_locked()
            lyrics_snap = list(self.lyrics) if lseq != l_seq else None
            if qseq != q_seq:
                if self._cached_q_seq != q_seq:
                    self._cached_queue_pub = [
                        {
                            'mid': x.get('mid', ''),
                            'media_mid': x.get('media_mid', ''),
                            'title': x.get('title', ''),
                            'artist': x.get('artist', ''),
                            'album': x.get('album', ''),
                            'cover': (f"/api/music/radio/emblem/{x['radio_id']}.svg" if (x.get('radio_id') in RADIO_BY_ID and str(x.get('cover', '')).startswith('data:image/svg+xml;base64,')) else x.get('cover', '')),
                            'thumb': ((x.get('thumb') or '') if x.get('thumb') != x.get('cover') else ''),
                            'duration': self._safe_dur(x.get('duration')),
                            'vip': bool(x.get('vip')),
                            'source': x.get('source', ''),
                            'quality': x.get('quality', ''),
                            'url': x.get('url') if x.get('is_local') else None,
                            'is_local': bool(x.get('is_local')),
                            'is_live': bool(x.get('is_live')),
                            'radio_id': x.get('radio_id', ''),
                            'desc': x.get('desc', ''),
                            **({'_ep_idx': x['_ep_idx']} if isinstance(x.get('_ep_idx'), int) and not isinstance(x.get('_ep_idx'), bool) and x['_ep_idx'] >= 0 else {}),
                        }
                        for x in self.queue[:200]
                    ]
                    self._cached_q_seq = q_seq
                queue_snap = self._cached_queue_pub
            else:
                queue_snap = None
        cfg = load_config()
        dur = self._safe_dur(t.get('duration')) if t else 0
        resp = {
            'playing': bool(playing and not paused and not amb_pub),
            'paused': paused,
            'buffering': bool(playing and not paused and not amb_pub and not started_m),
            'ducked': ducked,
            'alarm_ringing': alarm_ringing,
            'boot_id': self.boot_id,
            'open_ui_seq': self.open_ui_seq,
            'open_ui_fresh': ui_fresh,
            'open_ui_tab': (getattr(self, '_open_ui_tab', '') if ui_fresh else ''),
            'queue_seq': q_seq,
            'lyrics_seq': l_seq,
            'volume': volume,
            'elapsed': elapsed,
            'duration': dur,
            'track': {
                'mid': t.get('mid', ''),
                'file': t.get('url', '') if t.get('is_local') else '',
                'title': t.get('title', ''),
                'artist': t.get('artist', ''),
                'album': t.get('album', ''),
                'cover': t.get('cover', ''),
                'source': t.get('source', 'QQ音乐'),
                'quality': (
                    (f"{t.get('quality') or 'AAC 高清'} · {RADIO_AUDIO_MODES[_get_radio_audio_mode(cfg)]}"
                     if _get_radio_audio_mode(cfg) != 'raw' else (t.get('quality') or 'AAC 高清'))
                    if t.get('radio_id') else t.get('quality', '')
                ),
                'duration': dur,
                'is_live': bool(t.get('is_live')),
                'radio_id': t.get('radio_id', ''),
                'desc': t.get('desc', ''),
                'missing': _local_track_gone(t),
            } if t else None,
            'index': index,
            'queue_len': queue_len,
            'play_mode': play_mode,
            'ambient': amb_pub,
            'sleep': sleep_pub,
            'sleep_session': sess_pub,
            'sleep_preview': prev_pub,
            'user_profile': cfg.get('user_profile'),
            'quality_level': cfg.get('quality_level', 'lossless'),
            'radio_audio_mode': _get_radio_audio_mode(cfg),
            'max_cache_mb': cfg.get('max_cache_mb', 200),
        }
        if lyrics_snap is not None:
            resp['lyrics'] = lyrics_snap
        if queue_snap is not None:
            resp['queue'] = queue_snap
        return resp

    def radio_catalog_public(self) -> dict:
        _prewarm_live_radio_hosts()
        cfg = load_config()
        favs_raw = cfg.get('radio_favs')
        favs = [str(x) for x in favs_raw if isinstance(x, str) and x in RADIO_BY_ID] if isinstance(favs_raw, list) else []
        fav_set = set(favs)
        with self._lock:
            self._snapshot_radio_progress_locked()
            prog_snap = {k: dict(v) for k, v in self._radio_prog.items()}
            cur_t = self.queue[self.index] if (self.playing and 0 <= self.index < len(self.queue)) else None
            active_id = str(cur_t.get('radio_id') or '') if cur_t else ''
            is_playing = bool(self.playing and not self.paused and not getattr(self, 'ambient_preset', None))
        stations_pub = [
            {
                'id': s['id'],
                'cat': s['cat'],
                'kind': s['kind'],
                'title': s['title'],
                'sub': s['sub'],
                'badge': s['badge'],
                'mark': s['mark'],
                'cover': s['cover'],
                'fav': s['id'] in fav_set,
                'resume': prog_snap.get(s['id']) if s['kind'] == 'channel' else None,
            }
            for s in RADIO_STATIONS
        ]
        return {
            'ok': True,
            'categories': RADIO_CATEGORIES,
            'stations': stations_pub,
            'favs': favs,
            'active_radio_id': active_id,
            'playing': is_playing,
            'radio_audio_mode': _get_radio_audio_mode(cfg),
        }

    def set_radio_audio_mode(self, mode: str) -> dict:
        m = str(mode or '').strip().lower()
        if m not in RADIO_AUDIO_MODES:
            return {'ok': False, 'error': '不支持的电台音质模式'}
        with _CFG_LOCK:
            cfg = load_config()
            prev_m = _get_radio_audio_mode(cfg)
            cfg['radio_audio_mode'] = m
            save_config(cfg)
        restarted_live = False
        stopped_paused = False
        stopped_live = False
        if m != prev_m:
            with self._lock:
                cur_t = self.queue[self.index] if (self.playing and 0 <= self.index < len(self.queue)) else None
                if cur_t and cur_t.get('radio_id') and not getattr(self, 'ambient_preset', None):
                    if self.paused:
                        stopped_paused = self._stop_proc_locked()
                    else:
                        cur_seek = 0.0 if cur_t.get('is_live') else max(0.0, float(self.elapsed_base or 0.0))
                        stopped_live = self._stop_proc_locked()
                        gen = self._gen
                        restarted_live = True
            if stopped_paused or stopped_live:
                self._send_aec_flush()
            if restarted_live:
                threading.Thread(target=self._start_current_track, args=(cur_seek, gen), daemon=True).start()
        return {
            'ok': True,
            'radio_audio_mode': m,
            'label': RADIO_AUDIO_MODES[m],
            'message': f"电台音质已切换为：{RADIO_AUDIO_MODES[m]}",
        }

    def radio_episodes_public(self, station_id: str, refresh: bool = False) -> dict:
        sid = str(station_id or '').strip()
        st = RADIO_BY_ID.get(sid)
        if not st or st.get('kind') != 'channel':
            return {'ok': False, 'error': '该电台为实时直播，无分集列表'}
        tracks = resolve_radio_channel_tracks(st, shuffle=False, force_refresh=bool(refresh))
        with self._lock:
            self._snapshot_radio_progress_locked()
            resume = dict(self._radio_prog[sid]) if sid in self._radio_prog else None
        pub_eps = [self._public_track(t) for t in tracks]
        return {
            'ok': True,
            'station': {'id': st['id'], 'title': st['title'], 'sub': st['sub'], 'badge': st['badge'], 'cover': st['cover']},
            'episodes': pub_eps,
            'resume': resume,
        }

    def toggle_radio_fav(self, station_id: str, on=None) -> dict:
        sid = str(station_id or '').strip()
        st = RADIO_BY_ID.get(sid)
        if not st:
            return {'ok': False, 'error': '未找到该电台'}
        with _CFG_LOCK:
            cfg = load_config()
            raw = cfg.get('radio_favs')
            favs = [str(x) for x in raw if isinstance(x, str) and x in RADIO_BY_ID] if isinstance(raw, list) else []
            want = (sid not in favs) if on is None else bool(on)
            if want and sid not in favs:
                favs.insert(0, sid)
            elif not want and sid in favs:
                favs = [x for x in favs if x != sid]
            cfg['radio_favs'] = favs[:40]
            save_config(cfg)
            saved_favs = list(cfg['radio_favs'])
        return {
            'ok': True,
            'id': sid,
            'fav': want,
            'favs': saved_favs,
            'message': f"已收藏「{st['title']}」" if want else f"已取消收藏「{st['title']}」",
        }

    def play_radio(self, station_id: str, shuffle: bool = False, open_ui: bool = False,
                   track_index: int = None, restart: bool = False) -> dict:
        sid = str(station_id or '').strip()
        st = RADIO_BY_ID.get(sid)
        if not st:
            return {'ok': False, 'error': '未找到该电台频道'}
        if st['kind'] == 'live':
            live_list = [s for s in RADIO_STATIONS if s['kind'] == 'live']
            tracks = [_radio_live_track(s) for s in live_list]
            idx = next((i for i, s in enumerate(live_list) if s['id'] == sid), 0)
            res = self.play_tracks(tracks, idx, open_ui=open_ui, force_loop=True)
            if res.get('ok'):
                res['radio'] = {'id': sid, 'kind': 'live', 'title': st['title'], 'badge': st['badge']}
                res['message'] = f"正在直播：{st['title']}"
            return res

        tracks = resolve_radio_channel_tracks(st, shuffle=bool(shuffle))
        if not tracks:
            return {'ok': False, 'error': f"暂时无法获取「{st['title']}」节目单，请稍后重试"}

        start_idx = 0
        start_sec = 0.0
        resumed = False
        if track_index is not None and not isinstance(track_index, bool):
            try:
                start_idx = max(0, min(int(track_index), len(tracks) - 1))
            except (TypeError, ValueError, OverflowError):
                start_idx = 0
        elif not shuffle and not restart:
            with self._lock:
                self._snapshot_radio_progress_locked()
                prog = dict(self._radio_prog[sid]) if sid in self._radio_prog else None
            if prog:
                p_mid = prog.get('mid') or ''
                p_title = prog.get('title') or ''
                hit_i = next((i for i, x in enumerate(tracks) if (p_mid and x.get('mid') == p_mid) or (p_title and x.get('title') == p_title)), None)
                if hit_i is None and 0 <= int(prog.get('idx') or 0) < len(tracks):
                    hit_i = int(prog.get('idx') or 0)
                if hit_i is not None:
                    start_idx = hit_i
                    raw_s = float(prog.get('sec') or 0.0)
                    ep_d = self._safe_dur(tracks[hit_i].get('duration'))
                    if raw_s >= 3.0 and (ep_d <= 0 or raw_s < max(3.0, ep_d - 6)):
                        start_sec = raw_s
                    resumed = (hit_i > 0 or start_sec >= 3.0)

        res = self.play_tracks(tracks, start_idx, open_ui=open_ui, start_sec=start_sec, force_loop=True)
        if res.get('ok'):
            if restart:
                flushed = False
                with self._lock:
                    if self._radio_prog.pop(sid, None) is not None:
                        self._radio_prog_dirty = True
                        flushed = True
                if flushed:
                    self._flush_radio_progress(force=True)
            ep_t = tracks[start_idx].get('title', '')
            mm, ss = divmod(int(round(start_sec)), 60)
            res['radio'] = {
                'id': sid, 'kind': 'channel', 'title': st['title'], 'badge': st['badge'],
                'count': len(tracks), 'index': start_idx, 'sec': round(start_sec, 1), 'resumed': resumed,
            }
            if resumed:
                pos_s = f' {mm:02d}:{ss:02d}' if start_sec >= 3.0 else ''
                res['message'] = f"续播「{st['title']}」第 {start_idx + 1} 集{pos_s}"
            elif track_index is not None:
                res['message'] = f"正在播放第 {start_idx + 1} 集：{ep_t[:16]}"
            else:
                res['message'] = f"已开启「{st['title']}」（共 {len(tracks)} 集连播）"
        return res

    def play_tracks(self, tracks: list, start_index: int = 0, open_ui: bool = False,
                    start_sec: float = 0.0, force_loop: bool = False):
        self._abort_preview(restore_vol=True, clear_timer=True)
        norm_tracks = []
        for x in (tracks if isinstance(tracks, list) else []):
            if not isinstance(x, dict):
                continue
            rid = str(x.get('radio_id') or '')
            cov = str(x.get('cover') or '')
            if rid in RADIO_BY_ID and (not cov or cov.startswith('/api/music/radio/emblem/')):
                x = {**x, 'cover': RADIO_BY_ID[rid]['cover']}
            norm_tracks.append(x)
        tracks = norm_tracks
        if not tracks:
            return {'ok': False, 'error': '没有可播放的歌曲'}
        try:
            idx = 0 if isinstance(start_index, bool) else int(start_index)
        except (TypeError, ValueError, OverflowError):
            idx = 0
        idx = max(0, min(idx, len(tracks) - 1))
        if len(tracks) > 200:
            start = max(0, min(idx - 100, len(tracks) - 200))
            tracks = tracks[start:start + 200]
            idx -= start
        init_sec = max(0.0, float(start_sec or 0.0)) if not tracks[idx].get('is_live') else 0.0
        persist_loop = False
        end_sleep = False
        with self._lock:
            self._snapshot_radio_progress_locked()
            self._clear_duck_except_home_locked()
            if (not getattr(self, '_sleep_starting', False)
                    and getattr(self, 'sleep_session', None) is not None):
                self._clear_sleep_locked()
                end_sleep = True
            if tracks[idx].get('is_live') and getattr(self, 'sleep_mode', None) == 'tracks':
                m_conv = max(15, min(240, max(1, int(getattr(self, 'sleep_tracks_left', 1) or 1)) * 15))
                now_m = time.monotonic()
                self.sleep_mode = 'time'
                self.sleep_started_mono = now_m
                self.sleep_deadline = now_m + m_conv * 60
                self.sleep_total = m_conv
                self.sleep_tracks_left = 0
                self.sleep_ends_wall = time.time() + m_conv * 60
            if getattr(self, 'ambient_preset', None):
                self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
            self.ambient_preset = None
            self.ambient_layer = None
            self._ambient_buf = None
            self._ambient_layer_buf = None
            had_proc = self._stop_proc_locked()
            if getattr(self, 'sleep_mode', None) != 'time':
                self._sg_last = (1.0, time.monotonic())
            if force_loop and self.play_mode in ('shuffle', 'single'):
                self.play_mode = 'loop'
                persist_loop = True
            self.queue = list(tracks)
            self.queue_seq += 1
            self.index = idx
            self.playing = True
            self.paused = False
            self._fail_streak = 0
            self.elapsed_base = init_sec
            self.play_started_mono = 0.0
            self.lyrics = []
            self.lyrics_seq += 1
            self.just_started = True
            if open_ui:
                self.open_ui_seq += 1
                self._open_ui_mono = time.monotonic()
                self._open_ui_tab = 'radio' if tracks[idx].get('radio_id') else ''
            else:
                self._open_ui_tab = ''
            gen = self._gen
        if end_sleep:
            self._sleep_session_end('replaced')
        if persist_loop:
            self.set_play_mode('loop')
        if had_proc:
            self._send_aec_flush()
        _yield_airplay_audio_if_active()
        self._pause_bluetooth_if_streaming()
        self.save_session()
        threading.Thread(target=self._start_current_track, args=(init_sec, gen), daemon=True).start()
        t = self.current_track() or {}
        return {
            'ok': True,
            'title': t.get('title', ''),
            'artist': t.get('artist', ''),
            'source': t.get('source', 'QQ音乐·SQ无损'),
            'quality': t.get('quality', 'SQ 无损 FLAC'),
            'count': len(tracks),
        }

    def play_query(self, query: str, source: str = 'auto'):
        if isinstance(query, (int, float)) and not isinstance(query, bool):
            query = str(query)
        raw_q = (query if isinstance(query, str) else '').strip()
        raw_clean = raw_q.lower().strip(' ，。！？,.!?《》"\'“”')
        if raw_clean in (
            'stop_sleep', '关闭睡眠', '关闭睡眠模式', '退出睡眠', '退出睡眠模式', '结束睡眠', '结束睡眠模式',
            '取消睡眠', '取消睡眠模式', '关闭助眠', '停止助眠', '关掉助眠', '不睡了', '起床了', '我醒了',
        ):
            res = self.sleep_session_stop()
            return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
        if raw_clean in (
            '关闭音乐', '关掉音乐', '停止播放', '暂停播放', '别唱了', '别放了', '不听了', '别讲了', '别说了',
            '关闭电台', '关掉电台', '停止电台', '关闭收音机', '关掉收音机', '停止收音机', '关闭广播', '关掉广播', '停止广播',
        ):
            if getattr(self, 'sleep_session', None) is not None:
                res = self.sleep_session_stop()
                return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
            res = self.control('pause')
            return dict(res, mode_only=True, message=res.get('message') or '已暂停播放') if isinstance(res, dict) else res
        if raw_clean in ('打开电台界面', '打开收音机界面', '电台界面', '收音机界面', '看看电台', '电台列表'):
            res = self.control('open_radio')
            return dict(res, mode_only=True, title='电台', source='网络电台', message=res.get('message') or '已打开电台界面')
        sleep_only_tails = {
            '睡眠', '睡眠模式', '助眠', '助眠模式', '助眠音乐', '睡眠音乐', '催眠曲', '摇篮曲', '睡前轻音乐', '钢琴摇篮曲', '睡前音乐',
        } | {n.lower() for _, _, _, n in SLEEP_COMBOS}
        for sp in ('关闭', '关掉', '停止', '取消', '停掉', '别放', '不要'):
            if raw_clean.startswith(sp):
                tail = raw_clean[len(sp):].strip()
                if tail in sleep_only_tails:
                    res = self.sleep_session_stop()
                    return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
                if tail in ('音乐', '歌', '歌曲', '播放', '电台', '收音机', '广播', '网络电台', '网络收音机', '故事', '相声', '评书', '听书'):
                    if getattr(self, 'sleep_session', None) is not None:
                        res = self.sleep_session_stop()
                        return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
                    res = self.control('pause')
                    return dict(res, mode_only=True, message=res.get('message') or '已暂停播放') if isinstance(res, dict) else res
                if tail in (
                    '环境音', '自然音', '自然环境音', '白噪音', '背景音', '助眠音', '音效',
                    '雨声', '细雨', '窗外细雨', '海浪', '海浪声', '海岸慢浪', '森林', '微风', '林间微风', '风声',
                    '溪流', '溪流声', '流水', '流水声', '山涧溪流', '虫鸣', '虫鸣声', '夏夜虫鸣', '褐噪', '褐噪音',
                    '柔和白噪音', '粉红噪音', '粉噪音', '低沉褐噪音', '棕噪音',
                    '轻琴', '雨夜轻琴', '助眠钢琴', '壁炉', '柴火', '柴火声', '壁炉柴火', '篝火',
                    '书房', '翻书声', '书房自习', '颂钵', '颂钵声', '禅意颂钵', '深度自习', '围炉夜读', '深度入眠',
                ):
                    if getattr(self, 'sleep_session', None) is not None:
                        res = self.sleep_session_stop()
                        return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
                    res = self.set_ambient('off')
                    return (
                        dict(res, mode_only=True, title='自然环境音', source='自然环境音', message=res.get('message') or '已关闭环境音')
                        if isinstance(res, dict) and 'ambient' in res else res
                    )
        matched_mode = None
        if '单曲循环' in raw_q:
            matched_mode = 'single'
        elif '随机播放' in raw_q:
            matched_mode = 'shuffle'
        elif '列表循环' in raw_q or '顺序播放' in raw_q:
            matched_mode = 'loop'
        if matched_mode:
            self.set_play_mode(matched_mode)
        q = raw_q
        for kw in ('单曲循环', '随机播放', '列表循环', '顺序播放'):
            q = q.replace(kw, '').strip()
        for p in (
            '请帮我', '帮我', '用qq音乐', '用QQ音乐', '在QQ音乐',
            '切换为', '切换成', '切成', '设为', '设置成', '设置', '改成', '改为',
            '开启', '打开', '播放', '放一首', '放首', '来一首', '来首',
            '放点', '来点', '播点', '放个', '来个', '听点', '我想听', '听一下', '听听',
        ):
            if q.lower().startswith(p.lower()):
                q = q[len(p):].strip()
        for res_pre in ('一首', '首歌', '一段', '首'):
            if q.startswith(res_pre) and len(q) > len(res_pre):
                q = q[len(res_pre):].strip()
                break
        if matched_mode and q in ('', '模式', '播放模式'):
            with self._lock:
                has_q = bool(self.queue)
            if has_q:
                res = self.control(matched_mode)
                t = self.current_track() or {}
                return dict(res, mode_only=True, title=t.get('title', ''), artist=t.get('artist', ''))
            q = ''

        raw_ql = raw_q.lower()
        explicit_song = any(
            k in raw_ql
            for k in ('一首', '这首', '那首', '首歌', '歌曲', '单曲', '原唱', '翻唱', 'live', 'mv', '专辑', '歌单', '我喜欢', '收藏', '热歌', '新歌', '飙升')
        )
        amb_q = q.lower().strip(' ，。！？,.!?《》"\'“”')
        had_amb_suf = False
        for suf in ('环境音', '自然音', '白噪音', '背景音', '助眠音', '音效', '声音', '配方', '助眠'):
            if amb_q.endswith(suf) and len(amb_q) > len(suf):
                amb_q = amb_q[:-len(suf)].strip()
                had_amb_suf = True
        for pre in ('一点', '一段', '一些', '点', '些'):
            if amb_q.startswith(pre) and len(amb_q) > len(pre):
                amb_q = amb_q[len(pre):].strip()
        amb_exact_map = {
            '深度自习': 'deep_study', '沉浸自习': 'deep_study',
            '围炉夜读': 'cozy_read', '阅读放空': 'cozy_read',
            '深度入眠': 'deep_sleep', '深度助眠': 'deep_sleep',
            '雨夜轻琴': 'sleep_piano', '助眠钢琴': 'sleep_piano', '睡眠钢琴': 'sleep_piano',
            '壁炉柴火': 'fireplace', '壁炉': 'fireplace', '壁炉声': 'fireplace', '柴火': 'fireplace', '柴火声': 'fireplace', '篝火': 'fireplace', '篝火声': 'fireplace',
            '书房自习': 'library', '图书馆': 'library', '翻书声': 'library', '自习室': 'library',
            '禅意颂钵': 'singing_bowl', '颂钵': 'singing_bowl', '颂钵声': 'singing_bowl', '冥想音': 'singing_bowl',
            '窗外细雨': 'rain', '雨声': 'rain', '细雨': 'rain', '下雨声': 'rain', '白噪音': 'white_noise',
            '粉红噪音': 'pink_noise', '粉噪音': 'pink_noise', '低沉褐噪音': 'brown_noise', '棕噪音': 'brown_noise',
            '海岸慢浪': 'waves', '海浪': 'waves', '海浪声': 'waves', '潮汐声': 'waves',
            '森林微风': 'forest', '林间微风': 'forest', '山谷微风': 'forest', '微风声': 'forest', '风声': 'forest',
            '山涧溪流': 'stream', '潺潺流水': 'stream', '溪流': 'stream', '溪流声': 'stream', '流水声': 'stream',
            '夏夜虫鸣': 'night', '虫鸣': 'night', '虫鸣声': 'night', '静心褐噪': 'brown_noise', '褐噪': 'brown_noise', '褐噪音': 'brown_noise',
        }
        amb_suf_only = {'潮汐': 'waves', '翻书': 'library', '森林': 'forest', '流水': 'stream', '大海': 'waves', '冥想': 'singing_bowl', '柔和': 'white_noise'}

        def _wrap_amb_res(res):
            if isinstance(res, dict) and res.get('ok') and 'ambient' in res:
                amb_obj = res.get('ambient') or {}
                nm = amb_obj.get('name') or ''
                return dict(
                    res,
                    mode_only=True,
                    title=nm,
                    source='自然环境音',
                    message=res.get('message') or (f'已为您开启自然环境音：{nm}' if nm else '已开启自然环境音'),
                )
            return res

        if not explicit_song:
            if amb_q in amb_exact_map:
                return _wrap_amb_res(self.set_ambient(amb_exact_map[amb_q]))
            if had_amb_suf and amb_q in amb_suf_only:
                return _wrap_amb_res(self.set_ambient(amb_suf_only[amb_q]))
        src_norm = str(source or 'auto').strip().lower()
        explicit_local = src_norm in ('local', 'usb') or ('u盘' in raw_ql or ('本地' in raw_ql and not any(w in raw_ql for w in ('广播', '电台', '调频'))))
        if not explicit_song and not explicit_local:
            matched_radio_id = match_radio_voice_query(raw_q, q)
            if matched_radio_id:
                shuf_r = ('随机' in raw_q or '随便' in raw_q or '换一批' in raw_q)
                rst_r = ('从头' in raw_q or '第一集' in raw_q or '第1集' in raw_q)
                ep_idx = None
                m_ep = re.search(r'第\s*(?:\d{1,4}|[一二三四五六七八九十百零两]{1,5})\s*[集回章课讲篇部节期]', raw_q)
                if m_ep:
                    ep_n = _extract_ep_num(m_ep.group(0))
                    if ep_n:
                        ep_idx = max(0, ep_n - 1)
                return self.play_radio(matched_radio_id, shuffle=shuf_r, track_index=ep_idx, restart=rst_r)

        if explicit_local:
            lq = re.sub(r'^(?:里的|里|的)+', '', re.sub(r'(?i)u盘|本地', '', q).strip()).strip()
            if lq.lower() in ('歌', '音乐', '歌曲', '随便', '随机', '好听的歌', 'random'):
                lq = ''
            loc = search_local(lq, 500)
            if loc:
                if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                    random.shuffle(loc)
                return self.play_tracks(loc, 0)
            return {'ok': False, 'error': f'未在本机或U盘找到「{lq or q or "音乐"}」'}

        if any(k in raw_ql for k in ('我喜欢', '收藏', '我的歌单', 'kpop', '好听', '纯音乐', '娜娜', '最近爱听', '歌单')):
            pls = qq_user_playlists()
            for pl in pls:
                clean_name = pl['name'].replace('❤️', '').replace('🎵', '').strip().lower()
                if clean_name and clean_name in raw_ql:
                    tracks = qq_playlist_tracks(pl['id'])
                    if tracks:
                        if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                            random.shuffle(tracks)
                        return self.play_tracks(tracks, 0)
            if pls and any(k in raw_ql for k in ('我喜欢', '收藏', '我的歌单')):
                tracks = qq_playlist_tracks(pls[0]['id'])
                if tracks:
                    if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                        random.shuffle(tracks)
                    return self.play_tracks(tracks, 0)

        toplist_map = {
            '热歌': 26, '热歌榜': 26,
            '飙升': 62, '飙升榜': 62,
            '新歌': 27, '新歌榜': 27,
            '流行指数': 4, '流行指数榜': 4, '流行榜': 4,
            '内地': 5, '内地榜': 5,
            '香港地区': 59, '香港地区榜': 59, '香港榜': 59, '港歌榜': 59,
            '台湾地区': 61, '台湾地区榜': 61, '台湾榜': 61,
            '欧美': 3, '欧美榜': 3,
            '韩国': 16, '韩国榜': 16,
            '日本': 17, '日本榜': 17,
        }
        top_id = toplist_map.get(q)
        if top_id is None and (not q or q.lower() in ('歌', '音乐', '歌曲', '随便', '随机', '好听的歌', 'random')):
            top_id = 26
        if top_id is not None:
            tracks = qq_toplist(top_id, limit=35)
            if tracks:
                if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                    random.shuffle(tracks)
                return self.play_tracks(tracks, 0)

        tracks = qq_search(q, limit=20)
        if not tracks:
            return {'ok': False, 'error': f'未在 QQ 音乐找到「{q}」'}
        return self.play_tracks(tracks, 0)

    def seek(self, sec: float):
        if isinstance(sec, bool):
            return {'ok': False}
        try:
            sec_f = float(sec)
        except (TypeError, ValueError, OverflowError):
            return {'ok': False}
        if not math.isfinite(sec_f):
            return {'ok': False}
        with self._lock:
            can_seek = (0 <= self.index < len(self.queue) and not self.queue[self.index].get('is_live'))
        if can_seek and getattr(self, '_preview', None) is not None:
            self._abort_preview(restore_vol=True, clear_timer=True)
        with self._lock:
            if not (0 <= self.index < len(self.queue)):
                return {'ok': False}
            if self.queue[self.index].get('is_live'):
                return {'ok': True, 'live': True, 'elapsed': round(self.elapsed_base, 1)}
            target_sec = max(0.0, min(sec_f, 86400.0))
            dur = self._safe_dur(self.queue[self.index].get('duration'))
            if dur > 1 and target_sec >= dur - 1:
                target_sec = float(dur - 1)
            self._clear_duck_except_home_locked()
            was_amb = bool(getattr(self, 'ambient_preset', None))
            if was_amb:
                self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
            self.ambient_preset = None
            self.ambient_layer = None
            self._ambient_buf = None
            self._ambient_layer_buf = None
            had_proc = self._stop_proc_locked()
            if getattr(self, 'sleep_mode', None) != 'time':
                self._sg_last = (1.0, time.monotonic())
            if not self.playing:
                self.playing = True
                self.paused = True
            was_paused = self.paused
            same_pos = was_paused and not had_proc and abs(target_sec - self.elapsed_base) < 0.05
            self._fail_streak = 0
            self.elapsed_base = target_sec
            gen = self._gen
            ss_cur = getattr(self, 'sleep_session', None)
            end_sleep = isinstance(ss_cur, dict) and ((ss_cur.get('sound') or {}).get('kind') == 'ambient' or was_amb)
            if end_sleep:
                self._clear_sleep_locked()
        if end_sleep:
            self._sleep_session_end('replaced')
        if had_proc:
            self._send_aec_flush()
        if not was_paused:
            threading.Thread(target=self._start_current_track, args=(target_sec, gen), daemon=True).start()
        if not same_pos:
            self.save_session()
        return {'ok': True, 'elapsed': target_sec}

    def control(self, action: str, index: int = None, seconds: float = None, source: str = None):
        action = (action if isinstance(action, str) else '').lower().strip()
        mode_labels = {'loop': '列表循环', 'shuffle': '随机播放', 'single': '单曲循环'}
        if action in ('mode', 'toggle_mode', '切换模式', 'loop', '列表循环', '顺序播放', 'single', 'repeat_one', '单曲循环', 'shuffle', 'random', '随机', '随机播放'):
            with self._lock:
                if action in ('mode', 'toggle_mode', '切换模式'):
                    cycle = {'loop': 'shuffle', 'shuffle': 'single', 'single': 'loop'}
                    new_m = cycle.get(self.play_mode, 'loop')
                elif action in ('single', 'repeat_one', '单曲循环'):
                    new_m = 'single'
                elif action in ('shuffle', 'random', '随机', '随机播放'):
                    new_m = 'shuffle'
                else:
                    new_m = 'loop'
                self.play_mode = new_m
            self.set_play_mode(new_m)
            return {'ok': True, 'play_mode': new_m, 'message': f'已切换为{mode_labels[new_m]}'}
        if action in ('open', 'open_ui', '打开', '打开音乐', 'open_radio', '打开电台', 'open_account', 'open_fav', '打开我喜欢', '打开收藏'):
            want_tab = 'radio' if action in ('open_radio', '打开电台') else ('account' if action == 'open_account' else ('fav' if action in ('open_fav', '打开我喜欢', '打开收藏') else ''))
            with self._lock:
                self.open_ui_seq += 1
                self._open_ui_mono = time.monotonic()
                self._open_ui_tab = want_tab
            msg = '已打开电台界面' if want_tab == 'radio' else ('已打开我喜欢歌单' if want_tab == 'fav' else '已打开QQ音乐界面')
            return {'ok': True, 'state': 'opened', 'tab': want_tab or 'mine', 'message': msg}
        if action in ('duck', 'unduck'):
            do_duck_flush = False
            active = False
            src_key = str(source).strip().lower()[:16] if isinstance(source, str) and source.strip() else 'study'
            with self._lock:
                now_m = time.monotonic()
                leases = getattr(self, '_duck_leases', None)
                if not isinstance(leases, dict):
                    leases = {}
                    self._duck_leases = leases
                for k in [k for k, v in leases.items() if v <= now_m]:
                    leases.pop(k, None)
                active = bool(getattr(self, 'ambient_preset', None) or (self.playing and not self.paused))
                if action == 'duck':
                    dur_s = 18.0
                    if seconds is not None and not isinstance(seconds, bool):
                        with contextlib.suppress(TypeError, ValueError, OverflowError):
                            sf = float(seconds)
                            if math.isfinite(sf):
                                dur_s = max(3.0, min(65.0, sf))
                    was_study_duck = now_m < getattr(self, 'study_duck_until', 0.0)
                    leases[src_key] = max(leases.get(src_key, 0.0), now_m + dur_s)
                    self.study_duck_until = max(getattr(self, 'study_duck_until', 0.0), max(leases.values(), default=0.0))
                    do_duck_flush = not was_study_duck and active
                else:
                    leases.pop(src_key, None)
                    self.study_duck_until = max((v for v in leases.values() if v > now_m), default=0.0)
            if do_duck_flush:
                self._send_aec_flush()
            return {'ok': True, 'state': 'ducked' if action == 'duck' else 'unducked', 'active': active}
        if getattr(self, '_preview', None) is not None and action in (
            'stop', '停止', '关闭', 'jump', 'next', '下一首', '换一首', '下一台', '换台', '下一集',
            'prev', '上一首', '上一台', '上一集', 'pause', '暂停', 'resume', 'play', '继续',
        ):
            self._abort_preview(restore_vol=True, clear_timer=True)
        end_sleep_reason = None
        with self._lock:
            resp = None
            flush_aec = False
            save_needed = True
            if action in (
                'stop', '停止', '关闭', 'jump', 'next', '下一首', '换一首', 'prev', '上一首',
                '下一台', '换台', '下一集', '上一台', '上一集',
                'pause', '暂停', 'resume', 'play', '继续',
            ):
                self._clear_duck_except_home_locked()
                if getattr(self, 'ambient_preset', None):
                    self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
                self._ambient_buf = None
                self._ambient_layer_buf = None
                if getattr(self, 'sleep_mode', None) != 'time':
                    self._sg_last = (1.0, time.monotonic())
            if action in ('stop', '停止', '关闭'):
                if getattr(self, 'sleep_session', None) is not None:
                    end_sleep_reason = 'stop'
                    self._clear_sleep_locked()
                self.ambient_preset = None
                self.ambient_layer = None
                self._snapshot_radio_progress_locked()
                self.just_started = False
                self.playing = False
                self.paused = False
                self._fail_streak = 0
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                if self.lyrics:
                    self.lyrics = []
                    self.lyrics_seq += 1
                flush_aec = self._stop_proc_locked()
                resp = {'ok': True, 'state': 'stopped', 'message': '已停止播放'}
            elif action in ('pause', '暂停'):
                self.just_started = False
                was_amb = bool(getattr(self, 'ambient_preset', None))
                preempted = source in ('intercom', 'airplay') or CALL_FLAG_PATH.exists()
                if not getattr(self, '_in_preview_op', False) and not preempted:
                    ss_cur = getattr(self, 'sleep_session', None)
                    if isinstance(ss_cur, dict) and ((ss_cur.get('sound') or {}).get('kind') == 'ambient' or was_amb):
                        self._clear_sleep_locked()
                        end_sleep_reason = 'stop'
                self.ambient_preset = None
                self.ambient_layer = None
                was_active = bool(self.playing and not self.paused)
                if was_active:
                    self.paused = True
                if was_amb:
                    self._stop_proc_locked()
                flush_aec = was_active or was_amb
                save_needed = was_active or was_amb
                resp = {'ok': True, 'state': 'paused', 'message': '已暂停'}
            elif action in ('resume', 'play', '继续'):
                if self.queue and 0 <= self.index < len(self.queue) and _local_track_gone(self.queue[self.index]):
                    usb_on = _usb_mounted()
                    playable_idx = next(
                        ((self.index + step) % len(self.queue)
                         for step in range(len(self.queue))
                         if not _local_track_gone(self.queue[(self.index + step) % len(self.queue)], usb_on)),
                        None,
                    )
                    if playable_idx is None:
                        on_usb = any(isinstance(x, dict) and str(x.get('url') or '').startswith(str(USB_MNT_DIR) + '/') for x in self.queue)
                        msg = 'U盘已拔出，列表里的歌放不了，插回 U 盘或换首歌吧' if on_usb else '这几首歌的文件已经不在了，换首歌吧'
                        return {'ok': False, 'error': msg, 'message': msg}
                    if playable_idx != self.index:
                        self.index = playable_idx
                        self.elapsed_base = 0.0
                        self.lyrics = []
                        self.lyrics_seq = getattr(self, 'lyrics_seq', 0) + 1
                if self.queue:
                    was_amb = bool(getattr(self, 'ambient_preset', None))
                    ss_cur = getattr(self, 'sleep_session', None)
                    if isinstance(ss_cur, dict) and ((ss_cur.get('sound') or {}).get('kind') == 'ambient' or was_amb):
                        self._clear_sleep_locked()
                        end_sleep_reason = 'replaced'
                    self.ambient_preset = None
                    self.ambient_layer = None
                    was_paused = not self.playing or self.paused or was_amb
                    self.just_started = True
                    self.playing = True
                    self.paused = False
                    self._fail_streak = 0
                    if was_amb or (self._proc is None and (was_paused or self._starting_gen != self._gen)):
                        flush_aec = self._stop_proc_locked()
                        gen = self._gen
                        res_sec = 0.0 if self.queue[self.index].get('is_live') else self.elapsed_base
                        threading.Thread(target=self._start_current_track, args=(res_sec, gen), daemon=True).start()
                    resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
                else:
                    return {'ok': False, 'error': '当前没有暂停的音乐'}
            elif action == 'jump' and index is not None:
                if isinstance(index, bool):
                    return {'ok': False}
                try:
                    j_idx = int(index)
                except (TypeError, ValueError, OverflowError):
                    return {'ok': False}
                if 0 <= j_idx < len(self.queue):
                    if _local_track_gone(self.queue[j_idx]):
                        on_usb = str(self.queue[j_idx].get('url') or '').startswith(str(USB_MNT_DIR) + '/')
                        msg = 'U盘已拔出，这首歌放不了，插回 U 盘或换一首吧' if on_usb else '这首歌的文件已经不在了，换一首吧'
                        return {'ok': False, 'error': msg, 'message': msg}
                    was_amb = bool(getattr(self, 'ambient_preset', None))
                    ss_cur = getattr(self, 'sleep_session', None)
                    if isinstance(ss_cur, dict) and ((ss_cur.get('sound') or {}).get('kind') == 'ambient' or was_amb):
                        self._clear_sleep_locked()
                        end_sleep_reason = 'replaced'
                    self.ambient_preset = None
                    self.ambient_layer = None
                    flush_aec = self._stop_proc_locked()
                    prev_idx = self.index
                    self.just_started = True
                    self.index = j_idx
                    self.playing = True
                    self.paused = False
                    self._fail_streak = 0
                    self.elapsed_base = 0.0
                    self.play_started_mono = 0.0
                    if self.index != prev_idx:
                        self.lyrics = []
                        self.lyrics_seq = getattr(self, 'lyrics_seq', 0) + 1
                    gen = self._gen
                    threading.Thread(target=self._start_current_track, args=(0.0, gen), daemon=True).start()
                    resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
                else:
                    return {'ok': False}
            elif action in ('next', '下一首', '换一首', '下一台', '换台', '下一集'):
                if not self.queue:
                    return {'ok': False, 'error': '播放列表为空'}
                has_local = any(isinstance(x, dict) and x.get('is_local') for x in self.queue)
                usb_on = _usb_mounted() if has_local else True
                playable = [i for i, x in enumerate(self.queue) if not _local_track_gone(x, usb_on)]
                if not playable:
                    on_usb = any(isinstance(x, dict) and str(x.get('url') or '').startswith(str(USB_MNT_DIR) + '/') for x in self.queue)
                    msg = 'U盘已拔出，列表里的歌放不了，插回 U 盘或换首歌吧' if on_usb else '这几首歌的文件已经不在了，换首歌吧'
                    return {'ok': False, 'error': msg, 'message': msg}
                was_amb = bool(getattr(self, 'ambient_preset', None))
                ss_cur = getattr(self, 'sleep_session', None)
                if isinstance(ss_cur, dict) and ((ss_cur.get('sound') or {}).get('kind') == 'ambient' or was_amb):
                    self._clear_sleep_locked()
                    end_sleep_reason = 'replaced'
                self.ambient_preset = None
                self.ambient_layer = None
                flush_aec = self._stop_proc_locked()
                prev_idx = self.index
                if getattr(self, 'play_mode', 'loop') == 'shuffle' and len(playable) > 1:
                    cands = [i for i in playable if i != self.index] or playable
                    self.index = random.choice(cands)
                else:
                    n_q = len(self.queue)
                    self.index = next(
                        ((self.index + step) % n_q for step in range(1, n_q + 1)
                         if not _local_track_gone(self.queue[(self.index + step) % n_q], usb_on)),
                        playable[0],
                    )
                self.just_started = True
                self.playing = True
                self.paused = False
                self._fail_streak = 0
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                self._snapshot_radio_progress_locked()
                if self.index != prev_idx:
                    self.lyrics = []
                    self.lyrics_seq = getattr(self, 'lyrics_seq', 0) + 1
                gen = self._gen
                threading.Thread(target=self._start_current_track, args=(0.0, gen), daemon=True).start()
                resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
            elif action in ('prev', '上一首', '上一台', '上一集'):
                if not self.queue:
                    return {'ok': False, 'error': '播放列表为空'}
                has_local = any(isinstance(x, dict) and x.get('is_local') for x in self.queue)
                usb_on = _usb_mounted() if has_local else True
                playable = [i for i, x in enumerate(self.queue) if not _local_track_gone(x, usb_on)]
                if not playable:
                    on_usb = any(isinstance(x, dict) and str(x.get('url') or '').startswith(str(USB_MNT_DIR) + '/') for x in self.queue)
                    msg = 'U盘已拔出，列表里的歌放不了，插回 U 盘或换首歌吧' if on_usb else '这几首歌的文件已经不在了，换首歌吧'
                    return {'ok': False, 'error': msg, 'message': msg}
                was_amb = bool(getattr(self, 'ambient_preset', None))
                ss_cur = getattr(self, 'sleep_session', None)
                if isinstance(ss_cur, dict) and ((ss_cur.get('sound') or {}).get('kind') == 'ambient' or was_amb):
                    self._clear_sleep_locked()
                    end_sleep_reason = 'replaced'
                self.ambient_preset = None
                self.ambient_layer = None
                flush_aec = self._stop_proc_locked()
                prev_idx = self.index
                n_q = len(self.queue)
                self.index = next(
                    ((self.index - step) % n_q for step in range(1, n_q + 1)
                     if not _local_track_gone(self.queue[(self.index - step) % n_q], usb_on)),
                    playable[-1],
                )
                self.just_started = True
                self.playing = True
                self.paused = False
                self._fail_streak = 0
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                if self.index != prev_idx:
                    self.lyrics = []
                    self.lyrics_seq = getattr(self, 'lyrics_seq', 0) + 1
                gen = self._gen
                threading.Thread(target=self._start_current_track, args=(0.0, gen), daemon=True).start()
                resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
        if resp is not None:
            if flush_aec:
                self._send_aec_flush()
            if end_sleep_reason:
                self._sleep_session_end(end_sleep_reason)
            if resp.get('state') == 'playing':
                _yield_airplay_audio_if_active()
                self._pause_bluetooth_if_streaming()
            if save_needed:
                self.save_session()
            return resp
        return {'ok': False, 'error': f'未知指令: {action}'}

    def _stop_proc_locked(self) -> bool:
        self._gen += 1
        p2 = getattr(self, '_proc2', None)
        had_proc = self._proc is not None or p2 is not None or self._pacat is not None
        for p in (self._proc, p2, self._pacat):
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
        self._proc = None
        self._proc2 = None
        self._pacat = None
        return had_proc

    def _start_current_track(self, seek_sec: float = 0.0, expected_gen: int = None):
        with self._lock:
            if expected_gen is not None and expected_gen != self._gen:
                return
            had_proc = self._stop_proc_locked()
            can_start = bool(self.playing and not self.paused and (0 <= self.index < len(self.queue)))
            if can_start:
                gen = self._gen
                q_seq = getattr(self, '_quality_seq', 0)
                self._starting_gen = gen
                track = dict(self.queue[self.index])
                if track.get('is_live'):
                    seek_sec = 0.0
                    live_lines = [
                        {'time': 0.0, 'text': f"正在直播 · {track.get('title', '网络电台')}"},
                        {'time': 0.1, 'text': track.get('desc') or track.get('artist') or '实时广播伴听 · 可喊“小智”随时换台'},
                    ]
                    if self.lyrics != live_lines:
                        self.lyrics = live_lines
                        self.lyrics_seq += 1
                self.elapsed_base = float(seek_sec)
                self.play_started_mono = 0.0
                need_lrc = (not self.lyrics) and (not track.get('is_live'))
        if had_proc:
            self._send_aec_flush()
        if not can_start:
            return

        # Fetch synchronized lyrics in background
        if need_lrc and track.get('mid'):
            def _load_lrc(m, g):
                lrc = qq_lyric(m)
                changed = False
                with self._lock:
                    if g == self._gen and lrc != self.lyrics:
                        self.lyrics = lrc
                        self.lyrics_seq += 1
                        changed = True
                if changed:
                    self.save_session(defer=True)
            threading.Thread(target=_load_lrc, args=(track['mid'], gen), daemon=True).start()

        # Live radio tracks use permanent HLS/Icecast URLs from RADIO_BY_ID
        if track.get('is_live'):
            st_live = RADIO_BY_ID.get(str(track.get('radio_id') or ''))
            if st_live and st_live.get('kind') == 'live':
                track['url'] = st_live['url']
        # Refresh signed remote URL if missing or older than 1 hour (vkey expiry)
        elif not track.get('is_local') and track.get('url'):
            try:
                url_expired = track.get('_url_ts') is None or time.monotonic() - float(track['_url_ts']) > 3600
            except (TypeError, ValueError):
                url_expired = True
            if url_expired:
                track['url'] = None

        # Probe duration once for local/USB files so UI progress & seek work accurately
        if track.get('is_local') and track.get('url') and not _is_allowed_local_audio(track['url']):
            log.warning('Refusing local audio outside allowed directories: %r', track['url'])
            with self._lock:
                if self._starting_gen == gen:
                    self._starting_gen = -1
            self._advance_on_eof(gen, force_next=True)
            return
        if track.get('is_local') and track.get('url') and not track.get('duration'):
            try:
                local_p = str(Path(track['url']).resolve())
                out = subprocess.check_output(
                    ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', '--', local_p],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2.5,
                ).strip()
                dur_f = float(out)
                if math.isfinite(dur_f) and dur_f > 0:
                    track['duration'] = max(1, int(round(dur_f)))
                    with self._lock:
                        if gen == self._gen and 0 <= self.index < len(self.queue):
                            self.queue[self.index] = track
                            self.queue_seq += 1
                            self._cached_q_seq = 0
                    self.save_session(defer=True)
            except Exception:
                pass

        # Resolve stream URL if needed
        if not track.get('url'):
            with self._lock:
                if gen != self._gen or not self.playing or self.paused:
                    if self._starting_gen == gen:
                        self._starting_gen = -1
                    return
            if track.get('mid'):
                vk = resolve_qq_vkey(track['mid'], track.get('media_mid', ''))
                if vk and vk.get('url'):
                    track.update(vk)
            if not track.get('url') and not track.get('radio_id'):
                with self._lock:
                    if gen != self._gen or not self.playing or self.paused:
                        if self._starting_gen == gen:
                            self._starting_gen = -1
                        return
                q = f"{track.get('artist', '')} {track.get('title', '')}".strip()
                fb = resolve_hires_fallback(q, title_hint=track.get('title', ''), artist_hint=track.get('artist', ''))
                if fb and fb.get('url'):
                    track.update(fb)
                    track['_retried'] = True

            meta_changed = False
            with self._lock:
                if gen == self._gen and 0 <= self.index < len(self.queue):
                    if getattr(self, '_quality_seq', 0) != q_seq:
                        track['_url_ts'] = 0
                    prev_tr = self.queue[self.index]
                    if (
                        prev_tr.get('duration') != track.get('duration')
                        or prev_tr.get('quality') != track.get('quality')
                        or prev_tr.get('source') != track.get('source')
                    ):
                        self.queue_seq += 1
                        meta_changed = True
                    self.queue[self.index] = track
                    self._cached_q_seq = 0
            if meta_changed:
                self.save_session(defer=True)

        with self._lock:
            if gen != self._gen or not self.playing or self.paused:
                if self._starting_gen == gen:
                    self._starting_gen = -1
                return

        if not track.get('url'):
            with self._lock:
                if self._starting_gen == gen:
                    self._starting_gen = -1
            self._advance_on_eof(gen, force_next=True)
            return

        threading.Thread(target=self._decode_worker, args=(gen, track, seek_sec), daemon=True).start()

    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        chunk = stream.read(n)
        if not chunk or len(chunk) == n:
            return chunk or b''
        buf = bytearray(chunk)
        while len(buf) < n:
            more = stream.read(n - len(buf))
            if not more:
                break
            buf.extend(more)
        return bytes(buf)

    @staticmethod
    def _readinto_exact(stream, mv: memoryview) -> int:
        n = len(mv)
        got = stream.readinto(mv)
        if not got or got == n:
            return got or 0
        while got < n:
            more = stream.readinto(mv[got:])
            if not more:
                break
            got += more
        return got

    def _decode_worker(self, gen: int, track: dict, seek_sec: float):
        url = track['url']
        is_local = bool(track.get('is_local'))
        if is_local:
            if not _is_allowed_local_audio(url):
                with self._lock:
                    if self._starting_gen == gen:
                        self._starting_gen = -1
                log.warning('Blocked disallowed local path in decoder: %r', url)
                self._advance_on_eof(gen, force_next=True)
                return
            url = str(Path(url).resolve())
        else:
            orig_url = url
            resolved_url = _resolve_safe_ffmpeg_url(url, track.get('headers'), is_live=bool(track.get('is_live')))
            if not resolved_url:
                track['url'] = None
                with self._lock:
                    if self._starting_gen == gen:
                        self._starting_gen = -1
                    if gen == self._gen and 0 <= self.index < len(self.queue):
                        self.queue[self.index]['url'] = None
                _sp_blk = urllib.parse.urlsplit(url)
                _safe_blk = _sp_blk._replace(query='***' if _sp_blk.query else '', fragment='').geturl()
                log.warning('Blocked/unreachable stream URL in decoder: %r', _safe_blk)
                if not track.get('_retried') and not track.get('is_local') and not track.get('radio_id'):
                    with self._lock:
                        if gen != self._gen or not self.playing or self.paused:
                            return
                    track['_retried'] = True
                    q = f"{track.get('artist', '')} {track.get('title', '')}".strip()
                    fb = resolve_hires_fallback(q, title_hint=track.get('title', ''), artist_hint=track.get('artist', ''))
                    if fb and fb.get('url'):
                        track.update(fb)
                        meta_changed = False
                        with self._lock:
                            if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                                prev_tr = self.queue[self.index]
                                if (
                                    prev_tr.get('duration') != track.get('duration')
                                    or prev_tr.get('quality') != track.get('quality')
                                    or prev_tr.get('source') != track.get('source')
                                ):
                                    self.queue_seq += 1
                                    meta_changed = True
                                self.queue[self.index] = track
                                self._cached_q_seq = 0
                        if meta_changed:
                            self.save_session(defer=True)
                        self._start_current_track(seek_sec, gen)
                        return
                self._advance_on_eof(gen, force_next=True)
                return
            url = resolved_url
        cmd = ['ffmpeg', '-loglevel', 'error', '-nostdin', '-threads', '1', '-filter_threads', '1']
        if seek_sec > 0:
            cmd.extend(['-ss', f'{seek_sec:.1f}'])
        if is_local:
            cmd.extend(['-protocol_whitelist', 'file,pipe'])
        else:
            hdrs = track.get('headers') or {}
            if hdrs:
                same_root = _cookie_root(urllib.parse.urlsplit(orig_url).hostname or '') == _cookie_root(urllib.parse.urlsplit(url).hostname or '')
                hdr_str = ''.join(
                    f'{str(k).translate({10: None, 13: None})}: {str(v).translate({10: None, 13: None})}\r\n'
                    for k, v in hdrs.items()
                    if str(k).translate({10: None, 13: None}) and (same_root or str(k).strip().lower() not in ('cookie', 'authorization'))
                )
                if hdr_str:
                    cmd.extend(['-headers', hdr_str])
            if track.get('is_live'):
                cmd.extend(['-fflags', 'nobuffer', '-probesize', '32768', '-analyzeduration', '300000'])
            cmd.extend([
                '-rw_timeout', '15000000',
                '-protocol_whitelist', 'http,https,tcp,tls,crypto',
                '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '3',
            ])
        # High-precision conversion + radio broadcast/voice mastering to 48kHz float stereo pipeline.
        af_chain = _build_audio_filter(track)
        cmd.extend(['-i', url, '-vn', '-af', af_chain,
                    '-f', 'f32le', '-ac', '2', '-ar', str(OUT_RATE), 'pipe:1'])

        env = {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000', 'PULSE_SERVER': 'unix:/run/user/1000/pulse/native'}
        pacat = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            with self._lock:
                if self._starting_gen == gen:
                    self._starting_gen = -1
            log.error('Audio pipeline start failed: %s', e)
            self._advance_on_eof(gen, force_next=True)
            return

        stale_proc = False
        with self._lock:
            if self._starting_gen == gen:
                self._starting_gen = -1
            if gen != self._gen:
                stale_proc = True
            else:
                self._proc = proc
                self._pacat = None
        if stale_proc:
            _reap_subproc(proc)
            if proc.stdout:
                with contextlib.suppress(Exception):
                    proc.stdout.close()
            return

        block_bytes = 960 * 2 * 4  # 20ms of 48kHz stereo float32 (7680 bytes)
        pcm_buf = bytearray(block_bytes)
        pcm_mv = memoryview(pcm_buf)
        got_any = False
        pause_started = None
        paused_idle_release = False
        fade_prev = None
        unduck_left = 0
        pacat_retry_mono = 0.0
        live_drain_next = None
        try:
            while True:
                with self._lock:
                    if gen != self._gen or not self.playing:
                        break
                    user_paused = self.paused
                    ducked = self.ducked
                    study_duck = time.monotonic() < getattr(self, 'study_duck_until', 0.0)
                    alarm_ring = bool(getattr(self, 'alarm_ringing', False))
                    is_paused = user_paused or ducked or study_duck or alarm_ring
                    if user_paused:
                        idle_pause_lim = 3.0 if track.get('is_local') else (8.0 if track.get('is_live') else 45.0)
                        if pause_started is None:
                            pause_started = time.monotonic()
                        elif time.monotonic() - pause_started >= idle_pause_lim:
                            # Release idle ffmpeg (~44MB RAM) after pause; live streams reconnect at live edge (0.0s)
                            paused_idle_release = True
                            if self._proc is proc:
                                self._proc = None
                                self._pacat = None
                            log.info('Paused >%.0fs -> releasing idle ffmpeg decoder (resume at %.1fs)',
                                     idle_pause_lim, 0.0 if track.get('is_live') else self.elapsed_base)
                            break
                    else:
                        pause_started = None

                if is_paused:
                    if not user_paused and track.get('is_live'):
                        fade_prev = 0.0
                        unduck_left = 12
                        now_m = time.monotonic()
                        if live_drain_next is None or (now_m - live_drain_next) > 0.15:
                            live_drain_next = now_m
                        live_drain_next += 0.02
                        got_live = self._readinto_exact(proc.stdout, pcm_mv)
                        if not got_live:
                            break
                        rem = live_drain_next - time.monotonic()
                        if rem > 0.001:
                            time.sleep(rem)
                        continue
                    live_drain_next = None
                    time.sleep(0.04)
                    continue

                live_drain_next = None
                pause_started = None
                got = self._readinto_exact(proc.stdout, pcm_mv)
                if not got:
                    break
                if got < block_bytes:
                    # Pad final partial block to exact 7680-byte frame boundary
                    aligned_len = (got // 8) * 8
                    pcm_mv[aligned_len:] = b'\x00' * (block_bytes - aligned_len)

                got_any = True

                # Round 109 sleep timer fade-out + smooth unduck / listen-duck ramp (~240ms)
                with self._lock:
                    fade_target = self._sleep_gain_locked()
                    if getattr(self, 'listen_duck', False):
                        fade_target *= 0.25
                if fade_prev is None:
                    fade_prev = fade_target
                if unduck_left > 0:
                    fade_blk = min(fade_target, max(fade_prev, fade_target * (13 - unduck_left) / 12.0))
                    unduck_left -= 1
                elif fade_target > fade_prev + 0.04:
                    fade_blk = min(fade_target, fade_prev + 0.04)
                elif fade_target < fade_prev - 0.08:
                    fade_blk = max(fade_target, fade_prev - 0.08)
                else:
                    fade_blk = fade_target
                if fade_blk < 0.9999 or fade_prev < 0.9999:
                    self._apply_gain(pcm_buf, fade_prev, fade_blk)
                fade_prev = fade_blk

                # Send PCM to our non-invasive AEC bridge inside thinksmart-client (serialized against flush)
                sent_to_aec = False
                abort_blk = False
                with self._aec_lock:
                    if (
                        gen != self._gen
                        or not self.playing
                        or self.paused
                        or self.ducked
                        or getattr(self, 'alarm_ringing', False)
                        or time.monotonic() < getattr(self, 'study_duck_until', 0.0)
                    ):
                        abort_blk = True
                    else:
                        for sock in list(self.aec_clients):
                            try:
                                sock.sendall(pcm_mv)
                                sent_to_aec = True
                            except Exception:
                                self.aec_clients.pop(sock, None)
                                try:
                                    sock.close()
                                except Exception:
                                    pass
                if abort_blk:
                    fade_prev = 0.0
                    unduck_left = 12
                    continue

                if not sent_to_aec:
                    try:
                        if pacat is None and time.monotonic() >= pacat_retry_mono:
                            pacat = subprocess.Popen(
                                ['pacat', '--rate=48000', '--channels=2', '--format=float32le', '--latency-msec=60'],
                                stdin=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                env=env,
                            )
                            with self._lock:
                                if gen != self._gen or not self.playing:
                                    break
                                self._pacat = pacat
                        if pacat is not None:
                            pacat.stdin.write(pcm_mv)
                            pacat.stdin.flush()
                    except OSError:
                        pacat_retry_mono = time.monotonic() + 0.3
                        if pacat is not None:
                            _reap_subproc(pacat)
                            if pacat.stdin:
                                try:
                                    pacat.stdin.close()
                                except Exception:
                                    pass
                            pacat = None
                        with self._lock:
                            if self._pacat is not None and gen == self._gen:
                                self._pacat = None
                    if pacat is None:
                        time.sleep(0.02)
                elif pacat is not None:
                    _reap_subproc(pacat)
                    if pacat.stdin:
                        try:
                            pacat.stdin.close()
                        except Exception:
                            pass
                    pacat = None
                    with self._lock:
                        if self._pacat is not None and gen == self._gen:
                            self._pacat = None

                with self._lock:
                    if gen == self._gen and not self.paused and not self.ducked and not getattr(self, 'alarm_ringing', False) and time.monotonic() >= getattr(self, 'study_duck_until', 0.0):
                        if not self.play_started_mono:
                            self.play_started_mono = time.monotonic()
                        self.elapsed_base += 0.02
        except Exception:
            pass
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._pacat = None
            for p in (proc, pacat):
                if p is not None:
                    _reap_subproc(p)
                    for s in (p.stdin, p.stdout, p.stderr):
                        if s is not None:
                            try:
                                s.close()
                            except Exception:
                                pass

        with self._lock:
            if gen != self._gen or not self.playing:
                if not self.playing:
                    _trim_idle_heap()
                return
            still_paused = self.paused
            cur_elapsed = self.elapsed_base

        if still_paused:
            if paused_idle_release:
                self.save_session()
                _trim_idle_heap()
            return
        if paused_idle_release:
            self._start_current_track(0.0 if track.get('is_live') else cur_elapsed, gen)
            return

        if track.get('is_live'):
            drop_retries = self._safe_dur(track.get('_drop_retries'))
            if got_any and cur_elapsed >= 15.0:
                drop_retries = 0
            if drop_retries < 3:
                _evict_safe_ffmpeg_url(track.get('url'))
                track['_drop_retries'] = drop_retries + 1
                with self._lock:
                    if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                        self.queue[self.index] = track
                        self._cached_q_seq = 0
                time.sleep(0.8)
                self._start_current_track(0.0, gen)
                return
            log.warning('Live radio stream %r dropped repeatedly -> pausing', track.get('title'))
            with self._lock:
                if gen == self._gen and self.playing:
                    self.paused = True
            self.save_session()
            return

        if got_any:
            dur = self._safe_dur(track.get('duration'))
            drop_retries = self._safe_dur(track.get('_drop_retries'))
            if not track.get('is_local') and dur > 30 and cur_elapsed < (dur - 8) and drop_retries < 2:
                log.warning('Stream EOF early at %.1fs / %ds -> reconnecting at %.1fs', cur_elapsed, dur, cur_elapsed)
                _evict_safe_ffmpeg_url(track.get('url'))
                track['_drop_retries'] = drop_retries + 1
                with self._lock:
                    q_tr = self.queue[self.index] if (gen == self._gen and 0 <= self.index < len(self.queue)) else None
                    eff_ts = 0 if (isinstance(q_tr, dict) and q_tr.get('_url_ts') == 0) else track.get('_url_ts')
                try:
                    url_stale = eff_ts is None or eff_ts == 0 or time.monotonic() - float(eff_ts) > 300
                except (TypeError, ValueError):
                    url_stale = True
                if url_stale or drop_retries >= 1:
                    track['url'] = None
                with self._lock:
                    if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                        self.queue[self.index] = track
                        self._cached_q_seq = 0
                self._start_current_track(cur_elapsed, gen)
                return
            track['_drop_retries'] = 0
            with self._lock:
                self._fail_streak = 0
                if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                    self.queue[self.index]['_drop_retries'] = 0
            self._advance_on_eof(gen, force_next=False)
        else:
            dur = self._safe_dur(track.get('duration'))
            if seek_sec > 0 and dur > 0 and seek_sec >= max(1, dur - 3):
                self._advance_on_eof(gen, force_next=False)
                return
            if not track.get('_retried') and not track.get('is_local') and not track.get('radio_id'):
                with self._lock:
                    if gen != self._gen or not self.playing or self.paused:
                        return
                    q_seq_fb = getattr(self, '_quality_seq', 0)
                had_drop_retries = int(track.get('_drop_retries') or 0) > 0
                track['_retried'] = True
                track['_drop_retries'] = 0
                q = f"{track.get('artist', '')} {track.get('title', '')}".strip()
                fb = resolve_hires_fallback(q, title_hint=track.get('title', ''), artist_hint=track.get('artist', ''))
                if fb and fb.get('url'):
                    track.update(fb)
                    fb_seek = 0.0 if had_drop_retries else seek_sec
                    meta_changed = False
                    with self._lock:
                        if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                            if getattr(self, '_quality_seq', 0) != q_seq_fb:
                                track['_url_ts'] = 0
                            prev_tr = self.queue[self.index]
                            if (
                                prev_tr.get('duration') != track.get('duration')
                                or prev_tr.get('quality') != track.get('quality')
                                or prev_tr.get('source') != track.get('source')
                            ):
                                self.queue_seq += 1
                                meta_changed = True
                            self.queue[self.index] = track
                            self._cached_q_seq = 0
                    if meta_changed:
                        self.save_session(defer=True)
                    self._start_current_track(fb_seek, gen)
                    return
            self._advance_on_eof(gen, force_next=True)

    def _advance_on_eof(self, gen: int, force_next: bool = False):
        with self._lock:
            if gen != self._gen or not self.playing or self.paused or not self.queue:
                return
            self._gen += 1
            next_gen = self._gen
            paused_on_fail = False
            sleep_stop = False
            if not force_next and self.sleep_mode == 'tracks':
                self.sleep_tracks_left -= 1
                if self.sleep_tracks_left <= 0:
                    sleep_stop = True
            if force_next:
                self._fail_streak += 1
                if self._fail_streak >= min(max(len(self.queue), 1), 5):
                    log.warning('Consecutive track failures (%d) -> pausing playback to prevent spin loop', self._fail_streak)
                    self.paused = True
                    self.just_started = False
                    self._fail_streak = 0
                    paused_on_fail = True
            if not paused_on_fail:
                prev_idx = self.index
                if self.play_mode == 'single' and not force_next:
                    pass  # Replay current self.index
                elif self.play_mode == 'shuffle' and len(self.queue) > 1:
                    self.index = (self.index + random.randrange(1, len(self.queue))) % len(self.queue)
                else:
                    self.index = (self.index + 1) % len(self.queue)
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                if self.index != prev_idx:
                    self.lyrics = []
                    self.lyrics_seq += 1
                if sleep_stop:
                    # Sleep timer done: rest on the next track, paused at 0:00 (resume starts it)
                    self.paused = True
                    self.just_started = False
                    self._clear_sleep_locked()
        if sleep_stop or paused_on_fail:
            self._send_aec_flush()
            self._save_session_safe(defer=False)
        else:
            self._save_session_safe(defer=True)
        if sleep_stop and not paused_on_fail:
            log.info('Sleep timer: last track finished -> paused')
            self._sleep_session_end('timer')
            return
        if not paused_on_fail:
            self._start_current_track(0.0, next_gen)


engine = QQMusicEngine()


def start_aec_unix_server():
    """Accept connections from /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py."""
    srv = None
    try:
        if os.path.lexists(AEC_SOCK_PATH):
            st_sock = os.lstat(AEC_SOCK_PATH)
            if not stat.S_ISSOCK(st_sock.st_mode):
                raise OSError(f'Refusing to unlink non-socket AEC path: {AEC_SOCK_PATH}')
            os.unlink(AEC_SOCK_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            srv.bind(AEC_SOCK_PATH)
        finally:
            os.umask(old_umask)
        os.chmod(AEC_SOCK_PATH, 0o600)
        srv.listen(4)
        def _accept_loop():
            while True:
                try:
                    conn, _ = srv.accept()
                    try:
                        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                        conn.settimeout(0.6)
                    except Exception:
                        pass
                    with engine._aec_lock:
                        for old in list(engine.aec_clients):
                            try:
                                if old.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b'':
                                    engine.aec_clients.pop(old, None)
                                    old.close()
                            except (BlockingIOError, InterruptedError):
                                pass
                            except OSError:
                                engine.aec_clients.pop(old, None)
                                try:
                                    old.close()
                                except Exception:
                                    pass
                        while len(engine.aec_clients) >= 2:
                            evict = next(iter(engine.aec_clients))
                            engine.aec_clients.pop(evict, None)
                            with contextlib.suppress(Exception):
                                evict.close()
                        engine.aec_clients[conn] = None
                    log.info('AEC bridge client connected')
                except OSError:
                    if srv.fileno() < 0:
                        break
                    time.sleep(1)
                except Exception:
                    time.sleep(1)
        threading.Thread(target=_accept_loop, daemon=True).start()
        return srv
    except Exception as e:
        if srv is not None:
            with contextlib.suppress(Exception):
                srv.close()
        log.warning('AEC socket setup warning: %s', e)
        return None


async def watch_xiaozhi_events(ctrl_pool=None):
    """Watch XiaoZhi's SSE stream (127.0.0.1:8090/events) externally without modifying XiaoZhi."""
    import aiohttp
    prev_state = 'idle'
    voice_busy = False
    alarm_ringing = False
    prev_bt = None

    def _sync_bt(bt_obj) -> bool:
        nonlocal prev_bt
        if not isinstance(bt_obj, dict):
            return False
        now_bt = any(
            isinstance(d, dict) and d.get('streaming')
            for d in (bt_obj.get('devices') or [])
        )
        with engine._lock:
            engine.bt_streaming = now_bt
            should_pause = (
                prev_bt is not True
                and now_bt
                and ((engine.playing and not engine.paused) or bool(getattr(engine, 'ambient_preset', None)))
                and (time.monotonic() - engine._bt_pause_sent_mono) > 5.0
            )
        prev_bt = now_bt
        return bool(should_pause)

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=35)
    last_err_log = 0.0
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False, read_bufsize=2 * 1024 * 1024) as sess:
        sse_err_streak = 0
        listen_duck = False
        music_follow_task = None

        def _cancel_music_follow():
            nonlocal music_follow_task
            t = music_follow_task
            music_follow_task = None
            if t is not None and not t.done():
                t.cancel()

        async def _delayed_finish_follow_up():
            nonlocal music_follow_task
            try:
                await asyncio.sleep(6.0)
                with engine._lock:
                    still_active = bool((engine.playing and not engine.paused) or getattr(engine, 'ambient_preset', None))
                if alarm_ringing or prev_state != 'listening' or not still_active:
                    return
                async with sess.post('http://127.0.0.1:8090/api/listening/finish', timeout=aiohttp.ClientTimeout(total=2)) as _fr:
                    await _fr.read()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if music_follow_task is asyncio.current_task():
                    music_follow_task = None

        while True:
            try:
                async with sess.get('http://127.0.0.1:8090/events') as resp:
                    resp.raise_for_status()
                    event_type = ''
                    async for raw_line in resp.content:
                        line = raw_line.decode('utf-8', errors='replace').strip()
                        if not line:
                            event_type = ''
                            continue
                        if line.startswith('event:'):
                            event_type = line[6:].strip()
                        elif line.startswith('data:'):
                            data_str = line[5:].strip()
                            try:
                                data = json.loads(data_str)
                            except Exception:
                                continue
                            if not isinstance(data, dict):
                                continue
                            sse_err_streak = 0

                            if event_type == 'volume' and data.get('volume') is not None and not data.get('ring') and not alarm_ringing and not isinstance(data.get('volume'), bool):
                                try:
                                    with engine._lock:
                                        if (time.monotonic() - engine._local_vol_mono) < 1.5:
                                            pass
                                        else:
                                            engine.volume = max(0, min(100, int(float(data['volume']))))
                                            engine._vol_synced_xz = True
                                except (TypeError, ValueError, OverflowError):
                                    pass
                            elif event_type == 'alarms':
                                alarm_ringing = bool(data.get('ringing'))
                                if alarm_ringing:
                                    _cancel_music_follow()
                                with engine._lock:
                                    engine.alarm_ringing = alarm_ringing
                                    if alarm_ringing:
                                        engine._sleep_op_seq = getattr(engine, '_sleep_op_seq', 0) + 1
                                        engine._sleep_active_start_seq = 0
                                    has_sleep_sess = (
                                        getattr(engine, 'sleep_session', None) is not None
                                        or getattr(engine, '_preview', None) is not None
                                        or getattr(engine, '_wake', None) is not None
                                        or getattr(engine, '_sleep_starting', False)
                                    )
                                engine.set_listen_duck(listen_duck and not alarm_ringing)
                                engine.set_ducked(voice_busy or alarm_ringing)
                                if alarm_ringing and has_sleep_sess:
                                    await asyncio.get_running_loop().run_in_executor(ctrl_pool, engine.on_alarm_ringing)
                            elif event_type == 'bluetooth':
                                if _sync_bt(data):
                                    log.info('Bluetooth phone started streaming -> auto-pausing QQ Music')
                                    await asyncio.get_running_loop().run_in_executor(ctrl_pool, engine.control, 'pause')
                            elif event_type in ('snapshot', 'state'):
                                if event_type == 'snapshot':
                                    if isinstance(data.get('alarms'), dict):
                                        alarm_ringing = bool(data['alarms'].get('ringing'))
                                        if alarm_ringing:
                                            _cancel_music_follow()
                                        with engine._lock:
                                            engine.alarm_ringing = alarm_ringing
                                            if alarm_ringing:
                                                engine._sleep_op_seq = getattr(engine, '_sleep_op_seq', 0) + 1
                                                engine._sleep_active_start_seq = 0
                                            snap_sleep_sess = (
                                                getattr(engine, 'sleep_session', None) is not None
                                                or getattr(engine, '_preview', None) is not None
                                                or getattr(engine, '_wake', None) is not None
                                                or getattr(engine, '_sleep_starting', False)
                                            )
                                        engine.set_listen_duck(listen_duck and not alarm_ringing)
                                        if alarm_ringing and snap_sleep_sess:
                                            await asyncio.get_running_loop().run_in_executor(ctrl_pool, engine.on_alarm_ringing)
                                    if isinstance(data.get('bluetooth'), dict):
                                        if _sync_bt(data['bluetooth']):
                                            log.info('Bluetooth phone started streaming -> auto-pausing QQ Music')
                                            await asyncio.get_running_loop().run_in_executor(ctrl_pool, engine.control, 'pause')
                                s_obj = data.get('status') if isinstance(data.get('status'), dict) else data
                                if s_obj.get('volume') is not None and not isinstance(s_obj.get('volume'), bool) and not alarm_ringing:
                                    try:
                                        with engine._lock:
                                            if (time.monotonic() - engine._local_vol_mono) >= 1.5 and getattr(engine, '_vol_synced_xz', True):
                                                engine.volume = max(0, min(100, int(float(s_obj['volume']))))
                                    except (TypeError, ValueError, OverflowError):
                                        pass
                                st = s_obj.get('state', 'idle')
                                voice_busy = st in ('connecting', 'thinking', 'speaking')
                                listen_duck = (st == 'listening')
                                engine.set_listen_duck(listen_duck and not alarm_ringing)
                                engine.set_ducked(voice_busy or alarm_ringing)
                                if st != 'listening' or bool(s_obj.get('recognising')) or alarm_ringing:
                                    _cancel_music_follow()
                                # When audio is actively playing and XiaoZhi transitions speaking -> listening,
                                # wait 6s for any follow-up command ("音量调到60"/"换一首"), then quietly finish the idle mic window.
                                if prev_state == 'speaking' and st == 'listening' and not alarm_ringing:
                                    engine.just_started = False
                                    with engine._lock:
                                        active_audio = bool((engine.playing and not engine.paused) or getattr(engine, 'ambient_preset', None))
                                    if active_audio and not bool(s_obj.get('recognising')):
                                        _cancel_music_follow()
                                        music_follow_task = asyncio.create_task(_delayed_finish_follow_up())
                                prev_state = st
                    raise EOFError('XiaoZhi SSE stream closed')
            except asyncio.CancelledError:
                _cancel_music_follow()
                raise
            except Exception as exc:
                _cancel_music_follow()
                sse_err_streak += 1
                now_m = time.monotonic()
                if now_m - last_err_log >= 60.0:
                    last_err_log = now_m
                    if sse_err_streak < 3 and isinstance(exc, (EOFError, aiohttp.ClientPayloadError, aiohttp.ClientConnectionError)):
                        log.info('XiaoZhi SSE watcher reconnecting (%s)', type(exc).__name__)
                    else:
                        log.warning('XiaoZhi SSE watcher reconnecting (%s)', type(exc).__name__)
                voice_busy = False
                listen_duck = False
                alarm_ringing = False
                if prev_bt is True:
                    prev_bt = None
                with engine._lock:
                    engine.alarm_ringing = False
                    engine.bt_streaming = False
                engine.set_listen_duck(False)
                engine.set_ducked(False)
                prev_state = 'idle'
                await asyncio.sleep(3)


def make_app(ctrl_pool=None) -> web.Application:
    app = web.Application(client_max_size=524288)

    def _is_loopback_remote(remote) -> bool:
        if not remote or not isinstance(remote, str):
            return False
        raw = remote.split('%', 1)[0].strip('[]')
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if getattr(ip, 'ipv4_mapped', None) is not None:
            ip = ip.ipv4_mapped
        return ip.is_loopback

    def _is_trusted_host(host_hdr) -> bool:
        if not host_hdr or not isinstance(host_hdr, str):
            return False
        raw = host_hdr.strip()
        if not raw or any(c in raw for c in ('/', '\\', '?', '#', '@', ' ', '\t', '\r', '\n')):
            return False
        try:
            sp = urllib.parse.urlsplit(f'//{raw}')
            _ = sp.port
            hn = sp.hostname
        except ValueError:
            return False
        if not hn or sp.username is not None or sp.password is not None or sp.path or sp.query or sp.fragment:
            return False
        if '%' in hn:
            return False
        hn_clean = hn[:-1].lower() if hn.endswith('.') else hn.lower()
        if hn_clean == 'localhost':
            return True
        try:
            ip = ipaddress.ip_address(hn_clean)
        except ValueError:
            return False
        if getattr(ip, 'ipv4_mapped', None) is not None:
            ip = ip.ipv4_mapped
        return ip.is_loopback

    _KIOSK_LOOPBACK_ORIGINS = frozenset({
        'http://127.0.0.1:8090', 'http://localhost:8090',
        'http://127.0.0.1:8091', 'http://localhost:8091',
        'http://127.0.0.1:8095', 'http://localhost:8095',
    })

    def _allowed_origin(request) -> str:
        origin = request.headers.get('Origin') or ''
        if not origin:
            return ''
        try:
            po = urllib.parse.urlsplit(origin)
            _ = po.port
        except ValueError:
            return ''
        if po.scheme not in ('http', 'https') or not po.hostname or po.path or po.query or po.fragment or po.username is not None:
            return ''
        if request.host and origin == f'{request.scheme}://{request.host}':
            return origin
        if _is_loopback_remote(request.remote) and origin in _KIOSK_LOOPBACK_ORIGINS:
            return origin
        return ''

    def _apply_cors(resp, allowed):
        if allowed:
            resp.headers['Access-Control-Allow-Origin'] = allowed
            resp.headers['Vary'] = 'Origin'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, If-None-Match'
            resp.headers['Access-Control-Expose-Headers'] = 'ETag'
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        return resp

    @web.middleware
    async def cors_mw(request, handler):
        if not _is_loopback_remote(request.remote) or not _is_trusted_host(request.headers.get('Host')):
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        allowed = _allowed_origin(request) if origin is not None else ''
        if origin is not None and not allowed:
            raise web.HTTPForbidden()
        sfs = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if sfs == 'cross-site':
            if not (
                _is_loopback_remote(request.remote)
                and (
                    (origin is not None and origin in _KIOSK_LOOPBACK_ORIGINS)
                    or (origin is None and request.method in ('GET', 'HEAD')
                        and request.headers.get('Sec-Fetch-Dest') == 'iframe'
                        and request.path in ('/', '/music'))
                )
            ):
                raise web.HTTPForbidden()
        elif sfs and sfs not in ('same-origin', 'none') and not _is_loopback_remote(request.remote) and request.path.startswith('/api/'):
            raise web.HTTPForbidden()
        try:
            if request.method == 'OPTIONS':
                resp = web.Response(status=204)
            else:
                if request.method == 'POST' and request.content_type != 'application/json':
                    raise web.HTTPUnsupportedMediaType()
                resp = await handler(request)
        except web.HTTPException as exc:
            if exc.status != 403:
                _apply_cors(exc, allowed)
            raise
        return _apply_cors(resp, allowed)

    async def _keepalive_304(_req, resp):
        if resp.status == 304:
            resp.headers['Content-Length'] = '0'

    app.middlewares.append(cors_mw)
    app.on_response_prepare.append(_keepalive_304)

    ui_html_cache = {'key': None, 'data': b'', 'etag': ''}
    ui_csp = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.gtimg.cn http://*.gtimg.cn https://gtimg.cn "
        "https://*.qq.com http://*.qq.com https://qq.com https://*.qlogo.cn http://*.qlogo.cn "
        "https://*.qpic.cn http://*.qpic.cn; connect-src 'self'; "
        "frame-ancestors 'self' http://127.0.0.1:8090 http://localhost:8090 "
        "http://127.0.0.1:8095 http://localhost:8095"
    )

    async def index(req):
        try:
            st = os.lstat(UI_HTML_PATH)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 2 * 1024 * 1024:
                raise OSError()
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            if ui_html_cache['key'] != key:
                raw_html = _read_regular_text(UI_HTML_PATH, 2 * 1024 * 1024).encode('utf-8')
                ui_html_cache.update(
                    key=key,
                    data=raw_html,
                    etag=f'"{st.st_ino:x}-{st.st_mtime_ns:x}-{st.st_size:x}"',
                )
        except OSError:
            raise web.HTTPNotFound()
        headers = {
            'Cache-Control': 'no-cache',
            'ETag': ui_html_cache['etag'],
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': ui_csp,
        }
        if req.headers.get('If-None-Match') == ui_html_cache['etag']:
            return web.Response(status=304, headers=headers)
        return web.Response(body=ui_html_cache['data'], content_type='text/html', charset='utf-8', headers=headers)

    sig_cache = {'usb': None, 'local': None, 'ts': 0.0, 'inflight': False}

    def _refresh_sigs():
        try:
            payload = {}
            payload['usb_sig'] = usb_mount_sig()
            payload['local_sig'] = local_lib_sig()
            sig_cache['usb'] = payload['usb_sig']
            sig_cache['local'] = payload['local_sig']
            sig_cache['ts'] = time.monotonic()
        finally:
            sig_cache['inflight'] = False

    async def status(req):
        lite = req.query.get('lite') == '1'
        omit_heavy = req.query.get('omit_heavy') == '1'
        rq = req.query.get('qseq', '')
        rl = req.query.get('lseq', '')
        qseq = int(rq) if (rq.isascii() and rq.isdigit() and len(rq) <= 18) else None
        lseq = int(rl) if (rl.isascii() and rl.isdigit() and len(rl) <= 18) else None
        if lite:
            st_lite = engine.status(lite=True, qseq=qseq, lseq=lseq)
            hdrs = {'Cache-Control': 'no-cache'}
            if isinstance(st_lite, dict) and (not st_lite.get('playing') or st_lite.get('paused')):
                raw_b = _COMPACT_DUMPS(st_lite).encode('utf-8')
                etag = f'"{_hashlib.sha1(raw_b).hexdigest()[:16]}"'
                hdrs['ETag'] = etag
                if req.headers.get('If-None-Match') == etag:
                    return web.Response(status=304, headers=hdrs)
                return web.Response(body=raw_b, content_type='application/json', charset='utf-8', headers=hdrs)
            return web.json_response(st_lite, headers=hdrs, dumps=_COMPACT_DUMPS)
        if omit_heavy:
            qseq = engine.queue_seq
            lseq = engine.lyrics_seq
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(ctrl_pool, lambda: engine.status(lite=False, qseq=qseq, lseq=lseq))
        hdrs_full = {'Cache-Control': 'no-cache'}
        if isinstance(payload, dict) and not omit_heavy:
            now_m = time.monotonic()
            if sig_cache['usb'] is None:
                if not sig_cache['inflight']:
                    sig_cache['inflight'] = True
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(loop.run_in_executor(None, _refresh_sigs), timeout=0.6)
            elif now_m - sig_cache['ts'] >= 2.0 and not sig_cache['inflight']:
                sig_cache['inflight'] = True
                loop.run_in_executor(None, _refresh_sigs)
            payload['usb_sig'] = sig_cache['usb'] if sig_cache['usb'] is not None else '0'
            payload['local_sig'] = sig_cache['local'] if sig_cache['local'] is not None else '0'
        if isinstance(payload, dict) and (not payload.get('playing') or payload.get('paused')):
            raw_full = _COMPACT_DUMPS(payload).encode('utf-8')
            et_full = f'"{_hashlib.sha1(raw_full).hexdigest()[:16]}"'
            hdrs_full['ETag'] = et_full
            if req.headers.get('If-None-Match') == et_full:
                return web.Response(status=304, headers=hdrs_full)
            return web.Response(body=raw_full, content_type='application/json', charset='utf-8', headers=hdrs_full)
        return web.json_response(payload, headers=hdrs_full, dumps=_COMPACT_DUMPS)

    async def _json_body(req) -> dict:
        if req.method == 'GET':
            return {}
        try:
            raw = await req.read()
        except Exception:
            raise web.HTTPBadRequest()
        if not raw or not raw.strip():
            return {}
        try:
            b = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest()
        if not isinstance(b, dict):
            raise web.HTTPBadRequest()
        return b

    async def play(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(
            None, engine.play_query, body.get('query', ''), body.get('source', 'auto')
        )
        return web.json_response(res)

    _SAFE_COVER_RE = re.compile(r'^https?://(?:[a-z0-9-]+\.)*(?:gtimg\.cn|qq\.com|qlogo\.cn|qpic\.cn)/', re.I)

    def _safe_cover_url(u: str) -> str:
        if not isinstance(u, str) or not u:
            return ''
        if any(ord(c) <= 0x20 or ord(c) == 0x7f or c == '\\' for c in u):
            return ''
        if _SAFE_COVER_RE.match(u):
            return u
        return ''

    def _sanitize_http_tracks(raw_tracks, selected_index=None):
        if not isinstance(raw_tracks, list):
            return ([], None) if selected_index is not None else []
        need_local = any(isinstance(x, dict) and x.get('is_local') for x in raw_tracks[:200])
        roots = []
        if need_local:
            for d in LOCAL_DIRS:
                try:
                    if d.exists():
                        roots.append(d.resolve())
                except OSError:
                    pass
        clean = []
        clean_index = None
        def _s(v, lim=300):
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                return str(v).strip()[:lim]
            return ''

        for raw_index, item in enumerate(raw_tracks[:200]):
            if not isinstance(item, dict):
                continue
            rid = _s(item.get('radio_id'), 64)
            st_r = RADIO_BY_ID.get(rid) if rid else None
            if st_r and st_r.get('kind') == 'live':
                if raw_index == selected_index:
                    clean_index = len(clean)
                clean.append(_radio_live_track(st_r))
                continue
            if item.get('is_live'):
                continue
            raw_cov = _s(item.get('cover'), 500)
            raw_th = _s(item.get('thumb'), 500) or raw_cov
            safe_cov = _safe_cover_url(raw_cov) or (st_r['cover'] if st_r else '')
            safe_th = _safe_cover_url(raw_th) or safe_cov
            tr = {
                'mid': _s(item.get('mid'), 128),
                'media_mid': _s(item.get('media_mid'), 128),
                'title': _s(item.get('title'), 300),
                'artist': _s(item.get('artist'), 300),
                'album': _s(item.get('album'), 300),
                'cover': safe_cov,
                'thumb': safe_th,
                'duration': engine._safe_dur(item.get('duration')),
                'vip': bool(item.get('vip')),
                'source': _s(item.get('source'), 64),
                'quality': _s(item.get('quality'), 64),
                'url': None,
                'is_local': False,
                'is_live': False,
                'radio_id': rid if (st_r and st_r.get('kind') == 'channel') else '',
                'desc': _s(item.get('desc'), 200) if (st_r and st_r.get('kind') == 'channel') else '',
                **({'_ep_idx': item['_ep_idx']} if isinstance(item.get('_ep_idx'), int) and not isinstance(item.get('_ep_idx'), bool) and item['_ep_idx'] >= 0 else {}),
            }
            raw_url = item.get('url')
            valid_local = False
            if item.get('is_local'):
                if isinstance(raw_url, str) and raw_url.strip():
                    try:
                        rp = Path(raw_url).resolve()
                        if (
                            rp.is_file()
                            and rp.suffix.lower() in AUDIO_EXTS
                            and any(rp.is_relative_to(rt) for rt in roots)
                        ):
                            tr['url'] = str(rp)
                            tr['is_local'] = True
                            valid_local = True
                    except (OSError, ValueError):
                        pass
                if not valid_local and not tr['mid']:
                    continue
            if not (tr['mid'] or tr['title'] or valid_local):
                continue
            if raw_index == selected_index:
                clean_index = len(clean)
            clean.append(tr)
        return (clean, clean_index) if selected_index is not None else clean

    async def play_tracks(req):
        body = await _json_body(req)
        open_ui = bool(body.get('open_ui'))
        raw_tracks = body.get('tracks', [])
        try:
            raw_index = body.get('index', 0)
            start_index = 0 if isinstance(raw_index, bool) else int(raw_index)
        except (TypeError, ValueError, OverflowError):
            start_index = 0
        if isinstance(raw_tracks, list) and raw_tracks:
            start_index = max(0, min(start_index, len(raw_tracks) - 1))
            if len(raw_tracks) > 200:
                # Keep the requested song inside the bounded window before sanitizing.
                start = max(0, min(start_index - 100, len(raw_tracks) - 200))
                raw_tracks, start_index = raw_tracks[start:start + 200], start_index - start
        has_local = isinstance(raw_tracks, list) and any(isinstance(x, dict) and x.get('is_local') for x in raw_tracks[:200])
        pool = None if has_local else ctrl_pool
        safe_tracks, clean_index = await asyncio.get_running_loop().run_in_executor(
            pool, _sanitize_http_tracks, raw_tracks, start_index
        )
        if clean_index is None:
            return web.json_response({'ok': False, 'error': '选中的歌曲已不可播放，请刷新列表'}, status=400)
        res = await asyncio.get_running_loop().run_in_executor(
            pool, engine.play_tracks, safe_tracks, clean_index, open_ui
        )
        return web.json_response(res)

    async def control(req):
        body = await _json_body(req)
        action = body.get('action')
        if not isinstance(action, str) or not action.strip():
            return web.json_response({'ok': False, 'error': '缺少控制指令'}, status=400)
        sec_arg = body.get('seconds')
        src_arg = body.get('source')
        if src_arg is not None:
            args = (action.strip(), body.get('index'), sec_arg, src_arg)
        elif sec_arg is not None:
            args = (action.strip(), body.get('index'), sec_arg)
        else:
            args = (action.strip(), body.get('index'))
        res = await asyncio.get_running_loop().run_in_executor(ctrl_pool, engine.control, *args)
        return web.json_response(res)

    async def seek(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(
            ctrl_pool, engine.seek, body.get('sec')
        )
        return web.json_response(res)

    async def volume(req):
        body = await _json_body(req)
        v = await asyncio.get_running_loop().run_in_executor(
            ctrl_pool, engine.set_volume, body.get('volume'), body.get('delta')
        )
        return web.json_response({'ok': True, 'volume': v})

    async def search(req):
        body = await _json_body(req)
        raw_limit = body.get('limit', 25)
        raw_page = body.get('page', 1)
        try:
            limit = 25 if isinstance(raw_limit, bool) else max(1, min(60, int(raw_limit)))
        except (TypeError, ValueError, OverflowError):
            limit = 25
        try:
            page = 1 if isinstance(raw_page, bool) else max(1, min(50, int(raw_page)))
        except (TypeError, ValueError, OverflowError):
            page = 1
        q_raw = body.get('query', '')
        tracks = await asyncio.get_running_loop().run_in_executor(
            None, qq_search, q_raw, limit, page
        )
        hm_key = (q_raw if isinstance(q_raw, str) else '', limit, page)
        with _SEARCH_HM_LOCK:
            has_more = _SEARCH_HAS_MORE.get(hm_key, len(tracks) >= limit)
        # a full page (or backend meta hint when grp=1 collapses duplicates) means there may be more
        return web.json_response({'ok': True, 'tracks': tracks, 'page': page, 'has_more': bool(has_more)})

    async def suggest(req):
        q = _clean_smartbox_query(req.query.get('q', ''))
        cached = _get_cached_smartbox(q)
        if cached is not None:
            return web.json_response(cached)
        res = await asyncio.get_running_loop().run_in_executor(None, qq_smartbox, q)
        return web.json_response(res)

    async def toplist(req):
        rq = req.query.get('id', '')
        top_id = int(rq) if (rq.isascii() and rq.isdigit() and len(rq) <= 9) else 26
        tracks = await asyncio.get_running_loop().run_in_executor(None, qq_toplist, top_id, 40)
        return web.json_response({'ok': True, 'toplists': TOPLISTS, 'tracks': tracks})

    def _cached_local_500():
        if search_local is not _ORIG_SEARCH_LOCAL:
            return search_local('', 500)
        sig = (local_lib_sig(), usb_mount_sig())
        with _LOCAL_ALL_LOCK:
            if _LOCAL_ALL_CACHE['sig'] == sig and _LOCAL_ALL_CACHE['val'] is not None:
                return list(_LOCAL_ALL_CACHE['val'])
        tracks = search_local('', 500)
        with _LOCAL_ALL_LOCK:
            _LOCAL_ALL_CACHE['sig'] = sig
            _LOCAL_ALL_CACHE['val'] = list(tracks)
        return tracks

    async def playlists(req):
        force = req.query.get('refresh') == '1'
        loop = asyncio.get_running_loop()
        pls, usb = await asyncio.gather(
            loop.run_in_executor(None, (lambda: qq_user_playlists(force=True)) if force else qq_user_playlists),
            loop.run_in_executor(None, _cached_local_500),
        )
        return web.json_response({'ok': True, 'playlists': pls, 'usb_tracks': usb})

    async def playlist_tracks(req):
        body = await _json_body(req)
        tracks = await asyncio.get_running_loop().run_in_executor(
            None, qq_playlist_tracks, str(body.get('id', ''))
        )
        return web.json_response({'ok': True, 'tracks': tracks})

    async def fav_api(req):
        # 2026-11-12: 我喜欢 for one song. GET ?mid= -> {liked: true|false|null}; POST {mid, on}
        if req.method == 'GET':
            mid = str(req.query.get('mid') or '')
        else:
            body = await _json_body(req)
            mid = str(body.get('mid') or '')
        if not re.fullmatch(r'[0-9A-Za-z]{6,24}', mid):
            return web.json_response({'ok': False, 'error': '不是 QQ 音乐的歌，没法收藏'}, status=400)
        loop = asyncio.get_running_loop()
        if req.method == 'GET':
            liked = await loop.run_in_executor(None, qq_fav_state, mid)
            return web.json_response({'ok': True, 'mid': mid, 'liked': liked})
        if not isinstance(body.get('on'), bool):
            return web.json_response({'ok': False, 'error': '要收藏还是取消没说清'}, status=400)
        res = await loop.run_in_executor(None, qq_fav_set, mid, body['on'])
        return web.json_response(res)

    async def qr_create(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(None, qq_qr_create, body.get('mode', 'qq'))
        return web.json_response(res)

    async def qr_check(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(
            None, qq_qr_check, body.get('qrsig', ''), body.get('mode', 'qq')
        )
        return web.json_response(res)

    async def bind_uin(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(None, qq_bind_uin, body.get('input', ''))
        return web.json_response(res)

    async def logout(_req):
        res = await asyncio.get_running_loop().run_in_executor(None, qq_logout)
        return web.json_response(res)

    async def set_quality(req):
        body = await _json_body(req)
        def _apply_quality():
            with _CFG_LOCK:
                cfg = load_config()
                prev_q = cfg.get('quality_level', 'lossless')
                if body.get('quality_level') in ('lossless', 'hq', 'standard'):
                    cfg['quality_level'] = body['quality_level']
                if body.get('max_cache_mb') is not None and not isinstance(body.get('max_cache_mb'), bool):
                    try:
                        cfg['max_cache_mb'] = max(10, min(500, int(float(body['max_cache_mb']))))
                    except (TypeError, ValueError, OverflowError):
                        pass
                save_config(cfg)
                return cfg, prev_q
        cfg, prev_q = await asyncio.get_running_loop().run_in_executor(None, _apply_quality)
        new_q = cfg.get('quality_level', 'lossless')
        if new_q != prev_q:
            dq = _default_quality_label(cfg)
            stopped_paused = False
            with engine._lock:
                engine._quality_seq = getattr(engine, '_quality_seq', 0) + 1
                if engine.playing and engine.paused and not getattr(engine, 'ambient_preset', None):
                    stopped_paused = engine._stop_proc_locked()
                active_idx = engine.index if (engine.playing and not engine.paused) else -1
                new_queue = []
                for idx, tr in enumerate(engine.queue):
                    if isinstance(tr, dict) and not tr.get('is_local') and not tr.get('is_live'):
                        nt = dict(tr)
                        if idx == active_idx:
                            nt['_url_ts'] = 0
                        else:
                            nt['url'] = None
                            nt['_url_ts'] = None
                            nt['quality'] = dq
                        nt.pop('_retried', None)
                        nt.pop('_drop_retries', None)
                        new_queue.append(nt)
                    else:
                        new_queue.append(tr)
                engine.queue = new_queue
                engine.queue_seq += 1
                engine._cached_q_seq = 0
            if stopped_paused:
                engine._send_aec_flush()
            await asyncio.get_running_loop().run_in_executor(None, engine.save_session)
        return web.json_response({'ok': True, 'quality_level': new_q, 'max_cache_mb': cfg.get('max_cache_mb', 200)})

    async def cache_maintainer():
        """Ensure browser/music cache directories on eMMC never exceed max_cache_mb (default 200MB)."""
        cache_dirs = [
            Path('/home/ubuntu/.cache/thinksmart-qqmusic'),
            Path('/home/ubuntu/.cache/thinksmart-kiosk'),
            Path('/home/ubuntu/.cache/gstreamer-1.0'),
        ]
        def _prune_once():
            cfg = load_config()
            raw_mb = cfg.get('max_cache_mb', 200)
            try:
                mb = 200 if isinstance(raw_mb, bool) else int(raw_mb)
            except (TypeError, ValueError):
                mb = 200
            mb = min(max(mb if mb > 0 else 200, 10), 500)
            max_bytes = mb * 1024 * 1024
            files = []
            total = 0
            for d in cache_dirs:
                if not d.exists() or d.is_symlink():
                    continue
                for root, _, fns in os.walk(str(d)):
                    for fn in fns:
                        if fn.startswith('registry.') or fn.endswith(('.bin', '.db', '.db-wal', '.db-shm', '.db-journal', '.sqlite', '.sqlite-wal', '.sqlite-shm', '.sqlite-journal', '.lock')):
                            continue
                        fp = os.path.join(root, fn)
                        try:
                            st = os.lstat(fp)
                            if not stat.S_ISREG(st.st_mode):
                                continue
                            total += st.st_size
                            files.append((st.st_mtime, st.st_size, fp))
                        except Exception:
                            pass
            if total > max_bytes:
                files.sort(key=lambda x: x[0])  # oldest first
                for _, sz, fp in files:
                    if total <= max_bytes * 0.8:
                        break
                    try:
                        os.unlink(fp)
                        total -= sz
                    except Exception:
                        pass

        await asyncio.sleep(30)
        while True:
            try:
                await asyncio.get_running_loop().run_in_executor(None, _prune_once)
            except Exception:
                pass
            await asyncio.sleep(600)

    async def voice_wake(req):
        if not _is_loopback_remote(req.remote):
            raise web.HTTPForbidden()
        def _do_wake():
            try:
                r = urllib.request.Request('http://127.0.0.1:8090/api/wake', data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
                with _LOCAL_OPENER.open(r, timeout=2.0) as resp:
                    resp.read(64)
                return {'ok': True}
            except urllib.error.HTTPError as he:
                he.close()
                log.warning('voice_wake failed: HTTPError %s', getattr(he, 'code', '?'))
                return {'ok': False, 'error': '唤醒小智失败，请稍后重试'}
            except Exception as e:
                log.warning('voice_wake failed: %s', type(e).__name__)
                return {'ok': False, 'error': '唤醒小智失败，请稍后重试'}
        res = await asyncio.get_running_loop().run_in_executor(None, _do_wake)
        return web.json_response(res)

    async def sleep_timer(req):
        body = await _json_body(req)
        if req.method == 'GET':
            return web.json_response({'ok': True, 'sleep': engine.sleep_status()})
        res = await asyncio.get_running_loop().run_in_executor(
            ctrl_pool, lambda: engine.set_sleep(body.get('minutes'), body.get('tracks'), body.get('cancel') is True,
                                                body.get('fade'), body.get('until'), body.get('night') is True))
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def sleep_session_api(req):
        # 2026-11-12: sleep sessions. GET: state + choices; POST {action: start|stop|extend|screen_off, ...}
        if req.method == 'GET':
            return web.json_response(
                await asyncio.get_running_loop().run_in_executor(ctrl_pool, engine.sleep_session_info)
            )
        body = await _json_body(req)
        act = body.get('action') or 'start'
        loop = asyncio.get_running_loop()
        if act == 'start':
            snd = body.get('sound')
            if snd is not None and not isinstance(snd, (str, dict)):
                return web.json_response({'ok': False, 'error': '无效声音'}, status=400)
            until = body.get('until')
            until = until if isinstance(until, str) and len(until) <= 8 else None
            res = await loop.run_in_executor(None, lambda: engine.sleep_session_start(
                snd, body.get('minutes'), until, body.get('night') is True, body.get('fade'), body.get('volume')))
        elif act == 'stop':
            res = await loop.run_in_executor(ctrl_pool, engine.sleep_session_stop)
        elif act == 'extend':
            res = await loop.run_in_executor(ctrl_pool, lambda: engine.sleep_session_extend(body.get('minutes', 30)))
        elif act == 'screen_off':
            res = await loop.run_in_executor(ctrl_pool, engine.sleep_screen_off)
        elif act == 'preview':
            snd = body.get('sound')
            if snd is not None and not isinstance(snd, (str, dict)):
                return web.json_response({'ok': False, 'error': '无效声音'}, status=400)
            res = await loop.run_in_executor(None, lambda: engine.sleep_preview(snd, body.get('volume'), body.get('seconds', 30)))
        elif act == 'preview_stop':
            res = await loop.run_in_executor(ctrl_pool, engine.sleep_preview_stop)
        elif act == 'night_view':
            res = await loop.run_in_executor(ctrl_pool, engine.sleep_night_view, body.get('on') is True)
        elif act == 'wake':
            res = await loop.run_in_executor(ctrl_pool, engine.sleep_set_wake, body.get('on'), body.get('time'))
        elif act == 'default_volume':
            res = await loop.run_in_executor(ctrl_pool, engine.sleep_set_volume, body.get('volume'))
        else:
            return web.json_response({'ok': False, 'error': '未知操作'}, status=400)
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def lyrics_api(req):
        with engine._lock:
            seq = getattr(engine, 'lyrics_seq', 1)
            t = engine.queue[engine.index] if (engine.playing and 0 <= engine.index < len(engine.queue)) else None
            mid = str(t.get('mid') or '') if t else ''
            lrc = list(engine.lyrics[:300]) if engine.playing else []
        etag = f'"{seq}-{mid}-{len(lrc)}"'
        if req.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag, 'Cache-Control': 'no-cache'})
        return web.json_response({'ok': True, 'seq': seq, 'mid': mid, 'lyrics': lrc},
                                 headers={'ETag': etag, 'Cache-Control': 'no-cache'})

    async def ambient_api(req):
        if req.method == 'GET':
            with engine._lock:
                cur = engine._ambient_public_locked() if hasattr(engine, '_ambient_public_locked') else None
                vol = getattr(engine, 'volume', 55)
            return web.json_response({
                'ok': True,
                'ambient': cur,
                'volume': vol,
                'presets': list(AMBIENT_PRESETS.values()),
                'recipes': list(AMBIENT_RECIPES.values()),
            })
        body = await _json_body(req)
        preset = body.get('preset', 'off')
        if not isinstance(preset, str):
            return web.json_response({'ok': False, 'error': '无效环境音'}, status=400)
        res = await asyncio.get_running_loop().run_in_executor(
            None,
            engine.set_ambient,
            preset,
            body.get('volume'),
            body.get('layer'),
            body.get('layer_gain'),
        )
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def sleep_watcher():
        loop = asyncio.get_running_loop()
        n = 0
        while True:
            await asyncio.sleep(0.5)
            n += 1
            try:
                if engine.sleep_mode == 'time':
                    await loop.run_in_executor(ctrl_pool, engine.sleep_tick)
                if n % 4 == 0:
                    await loop.run_in_executor(ctrl_pool, engine.sleep_wake_tick)
                    await loop.run_in_executor(ctrl_pool, engine.sleep_housekeeping)
            except Exception:
                log.exception('sleep timer tick failed')

    bg_tasks = []

    async def on_startup(_app):
        bg_tasks.append(asyncio.create_task(watch_xiaozhi_events(ctrl_pool=ctrl_pool)))
        bg_tasks.append(asyncio.create_task(cache_maintainer()))
        bg_tasks.append(asyncio.create_task(sleep_watcher()))

    async def on_cleanup(_app):
        for t in bg_tasks:
            t.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        bg_tasks.clear()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get('/', index)
    app.router.add_get('/music', index)
    app.router.add_get('/api/music/status', status)
    app.router.add_post('/api/music/play', play)
    app.router.add_post('/api/music/play_tracks', play_tracks)
    app.router.add_post('/api/music/control', control)
    app.router.add_post('/api/music/seek', seek)
    app.router.add_post('/api/music/volume', volume)
    app.router.add_post('/api/music/search', search)
    app.router.add_get('/api/music/suggest', suggest)
    app.router.add_post('/api/music/voice_wake', voice_wake)
    app.router.add_get('/api/music/toplist', toplist)
    app.router.add_get('/api/music/playlists', playlists)
    app.router.add_post('/api/music/playlist/tracks', playlist_tracks)
    app.router.add_get('/api/music/fav', fav_api)
    app.router.add_post('/api/music/fav', fav_api)
    app.router.add_post('/api/music/qr/create', qr_create)
    app.router.add_post('/api/music/qr/check', qr_check)
    app.router.add_post('/api/music/bind', bind_uin)
    app.router.add_post('/api/music/logout', logout)
    app.router.add_post('/api/music/quality', set_quality)
    app.router.add_get('/api/music/sleep', sleep_timer)
    app.router.add_post('/api/music/sleep', sleep_timer)
    app.router.add_get('/api/music/lyrics', lyrics_api)
    app.router.add_get('/api/music/ambient', ambient_api)
    app.router.add_get('/api/music/sleep_session', sleep_session_api)
    app.router.add_post('/api/music/sleep_session', sleep_session_api)
    emblem_cache = {}

    async def radio_emblem_api(req):
        rid = str(req.match_info.get('id') or '').strip()
        hit = emblem_cache.get(rid)
        if hit is None:
            st = RADIO_BY_ID.get(rid)
            cov = (st or {}).get('cover', '')
            pref = 'data:image/svg+xml;base64,'
            if not isinstance(cov, str) or not cov.startswith(pref):
                raise web.HTTPNotFound()
            try:
                svg_bytes = _b64.b64decode(cov[len(pref):])
            except Exception:
                raise web.HTTPNotFound()
            etag = f'"{_hashlib.sha1(svg_bytes).hexdigest()[:16]}"'
            hit = (svg_bytes, etag)
            emblem_cache[rid] = hit
        svg_bytes, etag = hit
        hdrs = {'ETag': etag, 'Cache-Control': 'public, max-age=86400'}
        if req.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers=hdrs)
        return web.Response(body=svg_bytes, content_type='image/svg+xml', headers=hdrs)

    async def radio_list_api(_req):
        return web.json_response(
            await asyncio.get_running_loop().run_in_executor(ctrl_pool, engine.radio_catalog_public),
            dumps=_COMPACT_DUMPS,
        )

    async def radio_episodes_api(req):
        sid = req.query.get('id', '')
        ref = req.query.get('refresh') == '1'
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, engine.radio_episodes_public, sid, ref)
        return web.json_response(res, status=200 if res.get('ok') else 400, dumps=_COMPACT_DUMPS)

    async def radio_play_api(req):
        body = await _json_body(req)
        sid = body.get('id')
        shuf = bool(body.get('shuffle'))
        open_ui = bool(body.get('open_ui'))
        t_idx = body.get('track_index')
        rst = bool(body.get('restart'))
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, engine.play_radio, sid, shuf, open_ui, t_idx, rst)
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def radio_fav_api(req):
        body = await _json_body(req)
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, engine.toggle_radio_fav, body.get('id'), body.get('fav'))
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def radio_progress_api(req):
        body = await _json_body(req)
        if body.get('clear'):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, engine.clear_radio_progress, body.get('id'))
            return web.json_response(res, status=200 if res.get('ok') else 400)
        return web.json_response({'ok': False, 'error': '不支持的操作'}, status=400)

    async def radio_audio_mode_api(req):
        body = await _json_body(req)
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, engine.set_radio_audio_mode, body.get('mode'))
        return web.json_response(res, status=200 if res.get('ok') else 400)

    app.router.add_post('/api/music/ambient', ambient_api)
    app.router.add_get('/api/music/radio', radio_list_api)
    app.router.add_get('/api/music/radio/emblem/{id}.svg', radio_emblem_api)
    app.router.add_get('/api/music/radio/episodes', radio_episodes_api)
    app.router.add_post('/api/music/radio/play', radio_play_api)
    app.router.add_post('/api/music/radio/fav', radio_fav_api)
    app.router.add_post('/api/music/radio/progress', radio_progress_api)
    app.router.add_post('/api/music/radio/audio_mode', radio_audio_mode_api)
    return app


async def main():
    os.umask(0o077)
    loop = asyncio.get_running_loop()
    io_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix='qqm-io')
    ctrl_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix='qqm-ctrl')
    loop.set_default_executor(io_pool)
    aec_srv = start_aec_unix_server()
    runner = web.AppRunner(make_app(ctrl_pool=ctrl_pool), access_log=None, shutdown_timeout=1.5)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 8091)
    await site.start()
    log.info('Standalone QQ Music service listening on http://127.0.0.1:8091/')
    stop_ev = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_ev.set)
        except NotImplementedError:
            pass
    try:
        await stop_ev.wait()
    finally:
        if aec_srv is not None:
            with contextlib.suppress(Exception):
                aec_srv.close()
            with contextlib.suppress(OSError):
                if os.path.lexists(AEC_SOCK_PATH) and stat.S_ISSOCK(os.lstat(AEC_SOCK_PATH).st_mode):
                    os.unlink(AEC_SOCK_PATH)
        with engine._lock:
            old_dt = getattr(engine, '_defer_save_timer', None)
            engine._defer_save_timer = None
            wk = getattr(engine, '_wake', None)
            pv = getattr(engine, '_preview', None)
            st_wake_restore = None
            cur_vol = int(getattr(engine, 'volume', 0))
            if isinstance(wk, dict):
                if not wk.get('user_vol') and cur_vol == wk.get('vol') and wk.get('day_vol') != cur_vol:
                    st = getattr(engine, '_sleep_state', None)
                    if isinstance(st, dict):
                        st['restore'] = {'day_volume': wk['day_vol'], 'sleep_volume': cur_vol, 'at': time.time()}
                        st_wake_restore = {k: (dict(v) if isinstance(v, dict) else v) for k, v in st.items()}
                engine._wake = None
            if st_wake_restore is None and isinstance(pv, dict):
                prev_vol = pv.get('prev_volume')
                if isinstance(prev_vol, (int, float)) and not isinstance(prev_vol, bool) and cur_vol == pv.get('volume') and int(prev_vol) != cur_vol:
                    st = getattr(engine, '_sleep_state', None)
                    if isinstance(st, dict) and not st.get('restore'):
                        st['restore'] = {'day_volume': int(prev_vol), 'sleep_volume': cur_vol, 'at': time.time()}
                        st_wake_restore = {k: (dict(v) if isinstance(v, dict) else v) for k, v in st.items()}
            if pv is not None:
                engine._preview = None
        if old_dt is not None:
            old_dt.cancel()
        if isinstance(wk, dict):
            _sunrise_mark_stopped()
        if st_wake_restore is not None:
            _write_sleep_state(st_wake_restore)
        with _SESSION_LOCK:
            engine._save_session_locked()
            with engine._lock:
                engine._shutting_down = True
                engine.playing = False
                procs = [p for p in (engine._proc, getattr(engine, '_proc2', None), engine._pacat) if p is not None]
                engine._stop_proc_locked()
        for p in procs:
            try:
                p.wait(timeout=1.0)
            except Exception:
                pass
        with engine._aec_lock:
            for s in list(engine.aec_clients):
                try:
                    s.close()
                except Exception:
                    pass
            engine.aec_clients.clear()
        with contextlib.suppress(Exception):
            await site.stop()
        await runner.cleanup()
        ctrl_pool.shutdown(wait=False, cancel_futures=True)
        io_pool.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(Exception):
            import concurrent.futures.thread as _cft
            for _th in list(getattr(ctrl_pool, '_threads', ())) + list(getattr(io_pool, '_threads', ())):
                _cft._threads_queues.pop(_th, None)


if __name__ == '__main__':
    asyncio.run(main())
