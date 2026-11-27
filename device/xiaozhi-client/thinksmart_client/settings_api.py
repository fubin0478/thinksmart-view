"""The settings page's API (127.0.0.1:8090, same origin): GET/POST /api/settings.

One place for every parameter the screen can change. The client answers for its own settings
(assistant.py); the others stay with the service that owns them and are asked over loopback, each
with that service's own checks: the voice gateway (音色、停顿、记忆), QQ音乐 (音质、助眠、温柔叫醒),
the home page (天气城市), 学习陪伴, and this device (状态、重启、关机). Secrets and URLs never pass here.
"""
import asyncio
import json
import os
import shutil
import time
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from . import __version__, assistant
from .app import local_ip

HOME, MUSIC, STUDY = 'http://127.0.0.1:8095', 'http://127.0.0.1:8091', 'http://127.0.0.1:8093'
MUSIC_KEYS = {'quality_level', 'max_cache_mb', 'radio_audio_mode', 'play_mode', 'wake', 'wake_time', 'sleep_volume'}
STUDY_KEYS = {'interval', 'cooldown', 'min_gap', 'auto_off_hours', 'burst_frames', 'burst_gap'}
PARTS = ('assistant', 'music', 'home', 'study', 'system')
MAX_BODY = 16384


def _reject_json_const(token: str):
    raise ValueError(f'non-finite JSON constant: {token}')


class Failed(Exception):
    """A change that didn't go through, with the sentence to show."""

    def __init__(self, text, status=502):
        super().__init__(text)
        self.status = status


def _read(path, limit=4096):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read(limit)
    except OSError:
        return ''


_CPU_THERMAL_PATHS: list[str] | None = None


def device_facts():
    """What the 「系统」 page shows about this device. Cheap reads only (/proc, /sys, statvfs)."""
    global _CPU_THERMAL_PATHS
    mem = {}
    for line in _read('/proc/meminfo', 8192).splitlines():
        k, _, rest = line.partition(':')
        num = rest.strip().split(' ')[0]
        if num.isdigit():
            mem[k] = int(num) * 1024
    temps = []
    if _CPU_THERMAL_PATHS is None:
        found = []
        for i in range(24):
            kind = _read(f'/sys/class/thermal/thermal_zone{i}/type', 64)
            if kind.startswith('cpu'):
                found.append(f'/sys/class/thermal/thermal_zone{i}/temp')
        _CPU_THERMAL_PATHS = found
    for tp in _CPU_THERMAL_PATHS:
        raw = _read(tp, 32).strip()
        if raw.lstrip('-').isdigit():
            temps.append(int(raw) / 1000)
    try:
        disk = shutil.disk_usage('/')
        disk = {'total': disk.total, 'used': disk.used}
    except OSError:
        disk = None
    up = _read('/proc/uptime', 64).split(' ')[0]
    return {'uptime': int(float(up)) if up else None, 'disk': disk,
            'mem': {'total': mem.get('MemTotal'), 'available': mem.get('MemAvailable')} if mem.get('MemTotal') else None,
            'temp': round(max(temps)) if temps else None, 'ip': local_ip(), 'version': __version__}


def register(app, client, origin_ok):
    lock = asyncio.Lock()
    _sess: aiohttp.ClientSession | None = None

    def _get_sess() -> aiohttp.ClientSession:
        nonlocal _sess
        if _sess is None or _sess.closed:
            _sess = aiohttp.ClientSession(trust_env=False)
        return _sess

    async def _close_sess(_app):
        if _sess is not None and not _sess.closed:
            await _sess.close()

    app.on_cleanup.append(_close_sess)

    async def ask(url, body=None, timeout=4.0, headers=None):
        """(status, json or None) from a service on this device; (0, None) when it doesn't answer."""
        host = url.split('/', 3)[2]
        hdrs = {'Host': host, **(headers or {})}
        try:
            s = _get_sess()
            t = aiohttp.ClientTimeout(total=timeout)
            req = s.get(url, headers=hdrs, timeout=t) if body is None else s.post(url, json=body, headers=hdrs, timeout=t)
            async with req as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
                return r.status, data if isinstance(data, dict) else None
        except Exception:
            return 0, None

    async def hub(body=None, timeout=6.0):
        return await ask(client.cfg['hub_http'] + '/kiosk/internal/voice', body, timeout, {'X-TS-Internal-Reload': '1'})

    async def must(what, url, body, timeout=6.0):
        st, data = await ask(url, body, timeout)
        if st == 200 and (data is None or data.get('ok', True)):
            return data or {}
        if st == 0:
            raise Failed(f'{what}没有响应，稍后再试')
        raise Failed(str((data or {}).get('error') or (data or {}).get('message') or f'{what}没接受这个改动'), 400 if st < 500 else 502)

    # ---- read ----

    async def read_assistant():
        st, voice = await hub()
        ok = st == 200 and bool((voice or {}).get('ok'))
        if ok:
            voice.pop('ok', None)
        return {'assistant': assistant.view(client.cfg), 'voice': voice if ok else None}

    async def read_music():
        (s1, m), (s2, sl) = await asyncio.gather(ask(MUSIC + '/api/music/status?omit_heavy=1', timeout=2.5),
                                                 ask(MUSIC + '/api/music/sleep_session', timeout=2.5))
        if s1 != 200 or not m:
            return {'music': None}
        prof = m.get('user_profile') if isinstance(m.get('user_profile'), dict) else {}
        out = {'quality_level': m.get('quality_level') or 'lossless', 'max_cache_mb': m.get('max_cache_mb') or 200,
               'radio_audio_mode': m.get('radio_audio_mode') or 'hifi', 'play_mode': m.get('play_mode') or 'loop',
               'logged_in': bool(prof.get('userId') or prof.get('nickname')), 'vip': bool(prof.get('isVip'))}
        if s2 == 200 and sl:
            out.update(sleep_volume=sl.get('sleep_volume') or 25, wake=sl.get('wake') is not False,
                       wake_time=sl.get('wake_time') or '06:30', wake_next=sl.get('wake_next'))
        return {'music': out}

    async def read_home():
        st, data = await ask(HOME + '/api/home/config', timeout=2.5)
        return {'home': {k: data.get(k) for k in ('city', 'extra_cities')} if st == 200 and data else None}

    async def read_study():
        st, data = await ask(STUDY + '/api/study/config', timeout=2.5)
        if st != 200 or not data or any(data.get(k) is None for k in STUDY_KEYS):
            return {'study': None}
        return {'study': {**{k: data.get(k) for k in STUDY_KEYS}, 'enabled': bool(data.get('enabled'))}}

    async def read_system():
        facts = await asyncio.get_running_loop().run_in_executor(None, device_facts)
        return {'system': {**facts, **client.health(), 'mic': client.keys.mic_on, 'state': client.state}}

    readers = {'assistant': read_assistant, 'music': read_music, 'home': read_home, 'study': read_study, 'system': read_system}

    async def get(request):
        part = request.query.get('part') or ''
        if part == 'all':
            parts = await asyncio.gather(*(fn() for fn in readers.values()), return_exceptions=True)
            merged = {}
            for k, res in zip(readers.keys(), parts):
                if isinstance(res, dict):
                    merged.update(res)
                else:
                    merged[k] = None
            return web.json_response({'ok': True, **merged}, headers={'Cache-Control': 'no-store'})
        if part not in readers:
            return web.json_response({'ok': False, 'error': '没有这一栏'}, status=404)
        return web.json_response({'ok': True, **await readers[part]()}, headers={'Cache-Control': 'no-store'})

    # ---- change ----

    async def set_assistant(values):
        own = {k: v for k, v in values.items() if k in assistant.LIMITS or k in ('wake_words', 'wake_verify', 'wake_sound')}
        rest = {k: v for k, v in values.items() if k not in own}
        if own:
            try:
                await client.set_assistant(own)
            except ValueError as e:
                raise Failed(str(e), 400) from None
            if 'wake_words' in own:
                # the gateway answers these words with its own greeting: keep its list in step
                labels = [assistant.label_of(w) for w in client.cfg['wake_words']]
                await hub({'values': {'wakeup_words': labels + [x for x in ('你好小智', '小麦小麦', '哈喽小麦', '小麦同学', '嘿你好呀') if x not in labels]}})
        if rest:
            st, data = await hub({'values': rest}, timeout=20.0)
            if st != 200 or not (data or {}).get('ok'):
                raise Failed(str((data or {}).get('error') or '语音网关没有响应，稍后再试'), 400 if st == 400 else 502)
            return data.get('note') or ''
        return ''

    async def set_music(values):
        bad = set(values) - MUSIC_KEYS
        if bad:
            raise Failed(f'不支持的设置项：{sorted(bad)[0]}', 400)
        q = {k: values[k] for k in ('quality_level', 'max_cache_mb') if k in values}
        if q:
            if q.get('quality_level', 'hq') not in ('lossless', 'hq', 'standard'):
                raise Failed('没有这个音质', 400)
            await must('音乐服务', MUSIC + '/api/music/quality', q)
        if 'radio_audio_mode' in values:
            await must('音乐服务', MUSIC + '/api/music/radio/audio_mode', {'mode': values['radio_audio_mode']})
        if 'play_mode' in values:
            if values['play_mode'] not in ('loop', 'shuffle', 'single'):
                raise Failed('没有这个播放顺序', 400)
            await must('音乐服务', MUSIC + '/api/music/control', {'action': values['play_mode'], 'source': 'settings'})
        if 'sleep_volume' in values:
            await must('音乐服务', MUSIC + '/api/music/sleep_session', {'action': 'default_volume', 'volume': values['sleep_volume']})
        if 'wake' in values or 'wake_time' in values:
            body = {'action': 'wake'}
            if 'wake' in values:
                body['on'] = values['wake']
            if 'wake_time' in values:
                body['time'] = values['wake_time']
            return (await must('音乐服务', MUSIC + '/api/music/sleep_session', body)).get('message') or ''
        return ''

    async def set_home(values):
        res = await must('主页服务', HOME + '/api/home/config', values, timeout=15.0)
        city = str(values.get('city') or '').strip() if isinstance(values.get('city'), str) else ''
        if city:
            await hub({'values': {'weather_city': city}})
        return res.get('note') or ''

    async def set_study(values):
        bad = set(values) - STUDY_KEYS
        if bad or not values:
            raise Failed(f'不支持的设置项：{sorted(bad)[0]}' if bad else '没有要保存的内容', 400)
        await must('学习陪伴', STUDY + '/api/study/config', values)
        return ''

    async def set_system(values):
        act = values.get('action')
        if client.state != 'idle' or client.ring:
            raise Failed('正在对话或响铃，等结束了再操作', 409)
        if act == 'restart_hub':
            st, data = await ask(client.cfg['hub_http'] + '/kiosk/internal/diag', {'action': 'restart_hub'}, 8.0,
                                 {'X-TS-Internal-Reload': '1'})
            if st != 200 or not (data or {}).get('ok'):
                raise Failed(str((data or {}).get('error') or '语音网关没有响应'), 409 if st == 409 else 502)
            return '语音网关正在重启，大约 15 秒'
        if act == 'restart_client':
            asyncio.get_running_loop().call_later(0.4, os._exit, 0)
            return '语音助手正在重启，大约 5 秒'
        if act == 'reboot':
            res = await client.reboot()
            if not res.get('ok'):
                raise Failed(res.get('error') or '没能重启', 409)
            return '正在重启设备'
        raise Failed('不支持的操作', 400)

    writers = {'assistant': set_assistant, 'music': set_music, 'home': set_home, 'study': set_study, 'system': set_system}

    async def post(request):
        if not origin_ok(request):
            return web.json_response({'ok': False, 'error': '仅限本机界面操作'}, status=403)
        if request.content_length and request.content_length > MAX_BODY:
            return web.json_response({'ok': False, 'error': '内容太长'}, status=413)
        chunks, total = [], 0
        try:
            while True:
                chunk = await request.content.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BODY:
                    return web.json_response({'ok': False, 'error': '内容太长'}, status=413)
                chunks.append(chunk)
            raw = b''.join(chunks).decode('utf-8')
            body = json.loads(raw, parse_constant=_reject_json_const)
            part, values = body['part'], body['values']
        except Exception:
            return web.json_response({'ok': False, 'error': '请求内容无法解析'}, status=400)
        if not isinstance(part, str) or part not in writers or not isinstance(values, dict) or not values:
            return web.json_response({'ok': False, 'error': '没有这一栏'}, status=404)
        try:
            async with lock:
                note = await writers[part](values)
        except Failed as e:
            return web.json_response({'ok': False, 'error': str(e), **await readers[part]()}, status=e.status)
        return web.json_response({'ok': True, 'note': note, 'at': int(time.time()), **await readers[part]()},
                                 headers={'Cache-Control': 'no-store'})

    async def cities_search(request):
        q = (request.query.get('q') or '').strip()[:30]
        if not q:
            return web.json_response({'ok': True, 'items': []}, headers={'Cache-Control': 'no-store'})
        st, data = await ask(HOME + '/api/home/cities/search?' + urlencode({'q': q}), timeout=7.0)
        if st == 200 and isinstance(data, dict) and data.get('ok'):
            return web.json_response({'ok': True, 'items': data.get('items') or []}, headers={'Cache-Control': 'no-store'})
        return web.json_response({'ok': False, 'items': [], 'error': '天气城市搜索暂时没有响应'}, status=502 if st == 0 else 400,
                                 headers={'Cache-Control': 'no-store'})

    app.router.add_get('/api/settings', get)
    app.router.add_post('/api/settings', post)
    app.router.add_get('/api/settings/cities', cities_search)
