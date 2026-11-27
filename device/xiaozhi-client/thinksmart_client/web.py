"""The page the kiosk shows (127.0.0.1 only): static files, an event stream and button actions."""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web

from . import __version__, net_ctl, settings_api, update
from .app import ROOT, Client, local_ip

for _disp_dir in ('/usr/local/bin', str(ROOT.parent / 'kiosk')):
    if os.path.isdir(_disp_dir) and _disp_dir not in sys.path:
        sys.path.append(_disp_dir)
try:
    import thinksmart_display as _ts_display
except Exception:
    _ts_display = None

log = logging.getLogger(__name__)
WEB = ROOT / 'web'
# changes on every restart (every deploy restarts the client): the page reloads itself when it
# reconnects to a different run, so the kiosk never keeps running an old app.js
BUILD = str(int(time.time()))


def _make_client_display(client: Client):
    if _ts_display is None:
        return None

    class _ClientDisplayControl(_ts_display.DisplayControl):
        async def manual(self, level):
            await client.set_brightness(level)
            try:
                self.save({'auto': False})
            except OSError:
                pass
            self.clear_preview()

    return _ClientDisplayControl(core_url=f"http://127.0.0.1:{client.cfg.get('port', 8090)}")


def _is_loopback_host(hostname: str | None) -> bool:
    return (hostname or '').strip('[]').lower() in ('127.0.0.1', 'localhost', '::1')


def _local_host_ok(request) -> bool:
    host = (request.headers.get('Host') or '').strip()
    if not host:
        return True
    try:
        ph = urlsplit(f'http://{host}')
        port = ph.port
    except ValueError:
        return False
    return _is_loopback_host(ph.hostname) and (port is None or 1 <= port <= 65535)


def _local_origin_ok(request) -> bool:
    sfs = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
    if sfs and sfs not in ('same-origin', 'same-site', 'none'):
        return False
    origin = (request.headers.get('Origin') or '').strip()
    if not origin:
        return True
    if origin.lower() == 'null':
        return False
    try:
        po = urlsplit(origin)
    except ValueError:
        return False
    return po.scheme in ('http', 'https') and _is_loopback_host(po.hostname)


def _reject_json_const(token: str):
    raise ValueError(f'non-finite JSON constant: {token}')


async def _json_dict(request) -> tuple[dict | None, web.Response | None]:
    try:
        raw = await request.text()
    except Exception:
        return None, web.json_response({'ok': False, 'error': '请求内容无法解析'}, status=400)
    if not raw or not raw.strip():
        return None, web.json_response({'ok': False, 'error': '请求内容不能为空'}, status=400)
    try:
        data = json.loads(raw, parse_constant=_reject_json_const)
    except (ValueError, TypeError):
        return None, web.json_response({'ok': False, 'error': '请求内容无法解析'}, status=400)
    if not isinstance(data, dict):
        return None, web.json_response({'ok': False, 'error': '无效参数'}, status=400)
    return data, None


def make_app(client: Client, display_ctl=None):
    @web.middleware
    async def _security_mw(request, handler):
        if request.remote and request.remote not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            return web.json_response({'ok': False, 'error': '仅限本机访问'}, status=403)
        if not _local_host_ok(request):
            return web.json_response({'ok': False, 'error': '非法 Host 头'}, status=403)
        if not _local_origin_ok(request):
            return web.json_response({'ok': False, 'error': '非法来源请求'}, status=403)
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            ctype = (request.content_type or '').strip().lower()
            if ctype in ('application/x-www-form-urlencoded', 'multipart/form-data', 'text/plain'):
                return web.json_response({'ok': False, 'error': '不支持的 Content-Type'}, status=415)
        resp = await handler(request)
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        return resp

    app = web.Application(client_max_size=32768, middlewares=[_security_mw])
    display = display_ctl if display_ctl is not None else _make_client_display(client)
    client.display = display
    _http_sess: aiohttp.ClientSession | None = None

    def _get_session() -> aiohttp.ClientSession:
        nonlocal _http_sess
        if _http_sess is None or _http_sess.closed:
            _http_sess = aiohttp.ClientSession(trust_env=False)
        return _http_sess

    async def _close_session(_app):
        if _http_sess is not None and not _http_sess.closed:
            await _http_sess.close()

    app.on_cleanup.append(_close_session)

    _INDEX_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "media-src 'self' data: blob: http://127.0.0.1:* http://localhost:*; "
        "img-src 'self' data: blob: http://127.0.0.1:* http://localhost:* "
        "https://*.gtimg.cn http://*.gtimg.cn https://gtimg.cn "
        "https://*.qq.com http://*.qq.com https://qq.com "
        "https://*.qpic.cn http://*.qpic.cn "
        "https://*.qlogo.cn http://*.qlogo.cn; "
        "connect-src 'self' http://127.0.0.1:* http://localhost:* ws://127.0.0.1:*; "
        "frame-src 'self' about: http://127.0.0.1:* http://localhost:*; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
    )
    _V2_MIMES = {
        '.html': ('text/html', 'utf-8'),
        '.css': ('text/css', 'utf-8'),
        '.js': ('text/javascript', 'utf-8'),
        '.json': ('application/json', 'utf-8'),
        '.svg': ('image/svg+xml', None),
        '.png': ('image/png', None),
    }
    _static_ram: dict[Path, tuple[tuple[int, int, int], bytes, str]] = {}

    def _prime_ram_file(path: Path):
        try:
            st = path.stat()
            if st.st_size > 1024 * 1024:
                return None
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            hit = _static_ram.get(path)
            if hit is None or hit[0] != sig:
                raw = path.read_bytes()
                etag = f'"{hashlib.blake2b(raw, digest_size=8).hexdigest()}"'
                hit = (sig, raw, etag)
                _static_ram[path] = hit
            return hit
        except OSError:
            return None

    def _serve_ram_file(request, path: Path, content_type: str, charset: str | None = 'utf-8', extra_headers: dict | None = None):
        if 'Range' in request.headers:
            hdrs = {'Cache-Control': 'no-cache', **(extra_headers or {})}
            return web.FileResponse(path, headers=hdrs)
        hit = _prime_ram_file(path)
        if hit is None:
            if path.is_file():
                hdrs = {'Cache-Control': 'no-cache', **(extra_headers or {})}
                return web.FileResponse(path, headers=hdrs)
            raise web.HTTPNotFound()
        _, raw, etag = hit
        hdrs = {'Cache-Control': 'no-cache', 'ETag': etag}
        if extra_headers:
            hdrs.update(extra_headers)
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers=hdrs)
        return web.Response(body=raw, content_type=content_type, charset=charset, headers=hdrs)

    for _warm_p in (
        WEB / 'v2' / 'index.html',
        WEB / 'v2' / 'app.css',
        WEB / 'v2' / 'app.js',
        WEB / 'v2' / 'settings.js',
        Path('/usr/local/share/thinksmart-kiosk/qrcode.js'),
        ROOT.parent / 'kiosk' / 'web' / 'qrcode.js',
    ):
        _prime_ram_file(_warm_p)

    async def index(request):
        # the second design (orb with a face) is the UI since 2026-10-31; /v1 keeps the first one
        return _serve_ram_file(request, WEB / 'v2' / 'index.html', 'text/html', 'utf-8', {
            'Content-Security-Policy': _INDEX_CSP,
        })

    async def static_v2(request):
        name = request.match_info.get('name') or ''
        if not name or '/' in name or '\\' in name or name in ('.', '..'):
            raise web.HTTPNotFound()
        ext = os.path.splitext(name)[1].lower()
        mime, cs = _V2_MIMES.get(ext, ('application/octet-stream', None))
        return _serve_ram_file(request, WEB / 'v2' / name, mime, cs)

    async def events(request):
        if request.query.get('once') == '1':
            snap = dict(client.snapshot())
            if request.query.get('brief') == '1':
                snap.pop('thread', None)
                snap.pop('models', None)
            payload = {**snap, 'build': BUILD, 'version': __version__, 'update': update.read()}
            body = f'event: snapshot\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'.encode('utf-8')
            return web.Response(body=body, headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'})
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'})
        await resp.prepare(request)
        q = asyncio.Queue()
        client.events.queues.add(q)
        try:
            q.put_nowait(('snapshot', {**client.snapshot(), 'build': BUILD, 'version': __version__,
                                       'update': update.read()}))
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode())
                except asyncio.TimeoutError:
                    await resp.write(b': keepalive\n\n')
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            client.events.queues.discard(q)
        return resp

    def action(coro_fn):
        async def handler(_request):
            asyncio.create_task(coro_fn())
            return web.json_response({'ok': True})
        return handler

    async def text(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        asyncio.create_task(client.say(body.get('text', '')))
        return web.json_response({'ok': True})

    async def model(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        return web.json_response(await client.set_model(body.get('kind'), body.get('vendor'), body.get('entry')))

    async def brightness(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        try:
            value = await client.set_brightness(body.get('level'))
        except (TypeError, ValueError, OverflowError) as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        if display is not None:
            try:
                display.save({'auto': False})
            except OSError:
                pass
            display.clear_preview()
        return web.json_response({'ok': True, 'brightness': value})

    async def display_state(_request):
        if display is None:
            b = client.brightness()
            return web.json_response({
                'auto': False, 'level': b.get('level', 100), 'lux': None,
                'sensor_ok': False, 'dimmed': bool(b.get('dimmed')), 'screen_off': False, 'screen_on': True,
            }, headers={'Cache-Control': 'no-store'})
        return web.json_response(display.snapshot(), headers={'Cache-Control': 'no-store'})

    async def display_change(request):
        if not _local_origin_ok(request):
            return web.json_response({'ok': False, 'error': '仅限本机界面调节屏幕设置'}, status=403)
        body, err = await _json_dict(request)
        if err is not None:
            return err
        is_preview = request.path.endswith('/preview')
        if display is None:
            if 'level' not in body:
                return web.json_response({'ok': False, 'error': '当前环境不支持光线感应自动调光'}, status=400)
            try:
                val = await client.set_brightness(body.get('level'))
            except (TypeError, ValueError, OverflowError) as e:
                return web.json_response({'ok': False, 'error': str(e)}, status=400)
            return web.json_response({'ok': True, 'level': val.get('level', body.get('level')), 'auto': False, 'dimmed': bool(val.get('dimmed'))},
                                     headers={'Cache-Control': 'no-store'})
        try:
            value = await (display.preview(body) if is_preview else display.change(body))
        except ValueError as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        except (RuntimeError, OSError) as e:
            return web.json_response({'ok': False, 'error': str(e) or '暂时无法保存屏幕设置'}, status=503)
        return web.json_response({'ok': True, **value}, headers={'Cache-Control': 'no-store'})

    async def _fetch_local_json(url: str, host_hdr: str, method: str = 'GET', payload: dict | None = None, timeout_s: float = 3.0):
        try:
            sess = _get_session()
            hdrs = {'Host': host_hdr}
            if payload is not None:
                hdrs['Content-Type'] = 'application/json'
            req_fn = sess.post if method == 'POST' else sess.get
            kw = {'headers': hdrs, 'timeout': aiohttp.ClientTimeout(total=timeout_s)}
            if payload is not None:
                kw['json'] = payload
            async with req_fn(url, **kw) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
        except Exception:
            return None
        return None

    async def rich_context(request):
        """What a rich card needs besides the words (the capsule's cards, home_overlay.js): only this
        device's own services are asked, read-only, and only for the card's kind."""
        kind = str(request.query.get('kind') or 'all').strip().lower()
        city = str(request.query.get('city') or '').strip()[:24]
        alarm_v = client.alarm_view()
        out = {
            'ok': True,
            'device': {
                'volume': client.volume,
                'alarm_volume': client.alarm_volume,
                'brightness': display.snapshot() if display is not None else client.brightness(),
                'alarms': (alarm_v.get('items') or [])[:4],
            },
        }
        need_home = kind in ('music', 'schedule', 'timer', 'study', 'message', 'countdown', 'all')
        need_cd = kind == 'countdown'
        need_wx = kind in ('weather', 'all')
        need_sb = kind in ('server', 'all')
        need_srv = kind == 'server'
        w_url = ('http://127.0.0.1:8095/api/home/weather/detail?' + urlencode({'city': city})
                 if city else 'http://127.0.0.1:8095/api/home/weather/detail')

        async def _none():
            return None

        hs, cj, wj, sb, sj = await asyncio.gather(
            _fetch_local_json('http://127.0.0.1:8095/api/home/state', '127.0.0.1:8095', timeout_s=2.5) if need_home else _none(),
            _fetch_local_json('http://127.0.0.1:8095/api/home/countdowns', '127.0.0.1:8095', timeout_s=2.5) if need_cd else _none(),
            _fetch_local_json(w_url, '127.0.0.1:8095', timeout_s=4.5) if need_wx else _none(),
            _fetch_local_json('http://127.0.0.1:8092/api/badge', '127.0.0.1:8092', timeout_s=2.5) if need_sb else _none(),
            _fetch_local_json('http://127.0.0.1:8092/api/state', '127.0.0.1:8092', timeout_s=4.0) if need_srv else _none(),
        )
        if need_home:
            if isinstance(hs, dict):
                out['home'] = {k: hs.get(k) for k in ('music', 'timer', 'study', 'countdowns', 'messages')}
            elif kind == 'music':
                ms = await _fetch_local_json('http://127.0.0.1:8091/api/music/status', '127.0.0.1:8091', timeout_s=2.5)
                if isinstance(ms, dict):
                    out['home'] = {'music': ms}
        if need_cd and isinstance(cj, dict):
            out['countdowns'] = [c for c in (cj.get('items') or cj.get('countdowns') or []) if isinstance(c, dict)][:4]
        if need_wx:
            if not (isinstance(wj, dict) and wj.get('ok')):
                wj = await _fetch_local_json('http://127.0.0.1:8095/api/home/weather', '127.0.0.1:8095', timeout_s=2.5)
            if isinstance(wj, dict) and wj.get('ok'):
                now_w = wj.get('now') or {}
                air = wj.get('air') if isinstance(wj.get('air'), dict) else {}
                alert = next((a for a in (wj.get('alerts') or []) if isinstance(a, dict)), None)
                out['weather'] = {
                    'city': wj.get('city') or city or '本地',
                    'now': {k: now_w.get(k) for k in ('temp', 'text', 'icon', 'wind_dir', 'wind_scale', 'humidity', 'feels')},
                    'daily': [{k: d.get(k) for k in ('date', 'max', 'min', 'text', 'icon')} for d in (wj.get('daily') or [])[:5] if isinstance(d, dict)],
                    'hourly': [{k: h.get(k) for k in ('time', 'temp', 'icon', 'pop')} for h in (wj.get('hourly') or [])[:8] if isinstance(h, dict)],
                    'air': {k: air.get(k) for k in ('aqi', 'category')} if air else None,
                    'alert': str(alert.get('title') or alert.get('type_name') or '')[:40] if alert else '',
                }
        if need_sb and isinstance(sb, dict):
            out['server'] = {'text': sb.get('text') or '', 'amber': bool(sb.get('amber')), 'bad': bool(sb.get('bad'))}
        if need_srv and isinstance(sj, dict):
            tr = sj.get('traffic') if isinstance(sj.get('traffic'), dict) else {}
            out['servers'] = {
                'nodes': [{'name': str(v.get('name') or '')[:16], 'alive': bool(v.get('alive')), 'cpu': v.get('cpu'),
                           'up': v.get('net_out_speed'), 'down': v.get('net_in_speed')}
                          for v in (sj.get('servers') or [])[:16] if isinstance(v, dict)],
                'carriers': [{'name': c.get('name'), 'ms': c.get('latest_ms'), 'status': c.get('status')}
                             for c in (sj.get('carriers') or [])[:3] if isinstance(c, dict)],
                'traffic': {'used': tr.get('used_bytes'), 'limit': tr.get('limit_bytes')} if tr.get('available') else None,
            }
        return web.json_response(out, headers={'Cache-Control': 'no-store'})

    async def rich_music_control(request):
        if not _local_origin_ok(request):
            return web.json_response({'ok': False, 'error': '仅限本机界面操作'}, status=403)
        body, err = await _json_dict(request)
        if err is not None:
            return err
        act = str(body.get('action') or '').strip().lower()
        if act not in ('pause', 'resume', 'stop', 'next', 'prev'):
            return web.json_response({'ok': False, 'error': '不支持的控制动作'}, status=400)
        res = await _fetch_local_json('http://127.0.0.1:8091/api/music/control', '127.0.0.1:8091',
                                      method='POST', payload={'action': act}, timeout_s=4.0)
        ms = await _fetch_local_json('http://127.0.0.1:8091/api/music/status', '127.0.0.1:8091', timeout_s=2.5)
        return web.json_response({'ok': bool(res and res.get('ok', True)), 'music': ms}, headers={'Cache-Control': 'no-store'})

    async def screen_off(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        try:
            value = await client.set_screen_off(**{k: body.get(k) for k in ('enabled', 'minutes')})
        except (TypeError, ValueError, OverflowError) as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        return web.json_response({'ok': True, 'screen_off': value})

    async def volume(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        try:
            value = await client.set_volume(body.get('volume'))
        except (TypeError, ValueError, OverflowError) as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        return web.json_response({'ok': True, 'volume': value})

    async def alarm_volume(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        try:
            value = await client.set_alarm_volume(body.get('volume'))
        except (TypeError, ValueError, OverflowError) as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        return web.json_response({'ok': True, 'volume': value})

    async def alarm_tone(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        try:
            if body.get('stop'):
                await client._stop_preview()      # the settings closed while a tone was being tried
            elif body.get('preview'):
                await client.preview_tone(body.get('tone'))
            else:
                client.set_alarm_tone(body.get('tone'))
        except ValueError as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        return web.json_response({'ok': True})

    async def alarm_delete(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        try:
            raw_id = body.get('id')
            if isinstance(raw_id, bool):
                raise ValueError('bool id')
            item = await client.delete_alarm(int(raw_id))
        except (TypeError, ValueError, OverflowError):
            return web.json_response({'ok': False, 'error': 'id must be a number'}, status=400)
        return web.json_response({'ok': bool(item), **({} if item else {'error': '这一条已经没有了'})})

    async def alarm_snooze(_request):
        found = await client.snooze_alarm()
        return web.json_response({'ok': bool(found), **({} if found else {'error': '没有刚响过的闹钟'})})

    async def alarm_stop(_request):
        return web.json_response({'ok': await client.stop_alarm()})

    async def alarm_missed_clear(_request):
        client.clear_missed()
        return web.json_response({'ok': True})

    async def alarms_list(request):
        view = client.alarm_view()
        items = [
            {k: i[k] for k in ('id', 'kind', 'type', 'label', 'at', 'when', 'in', 'repeat', 'snoozed')
             if i.get(k) not in (None, '', False)}
            for i in (view.get('items') or [])[:24]
        ]
        missed = [
            {k: m[k] for k in ('kind', 'label', 'at', 'why', 'noted') if m.get(k) not in (None, '')}
            for m in (view.get('missed') or [])[:6]
        ]
        payload = {
            'ok': True,
            'synced': bool(view.get('synced')),
            'ringing': bool(view.get('ringing')),
            'ringing_info': view.get('ringing'),
            'volume': view.get('volume', 70),
            'tone': view.get('tone', 'coffee'),
            'tones': view.get('tones') or {},
            'missed': missed,
            'items': items,
        }
        sig_src = json.dumps(
            {'s': payload['synced'], 'r': payload['ringing'], 'ri': payload['ringing_info'],
             'v': payload['volume'], 't': payload['tone'], 'm': missed, 'i': items},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        )
        etag = f'"al-{hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:16]}"'
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag, 'Cache-Control': 'no-store'})
        return web.json_response(payload, headers={'ETag': etag, 'Cache-Control': 'no-store'})

    async def alarm_create(request):
        if not _local_origin_ok(request):
            return web.json_response({'ok': False, 'error': '仅限本机界面操作'}, status=403)
        body, err = await _json_dict(request)
        if err is not None:
            return err
        kind = str(body.get('kind') or 'alarm').strip()
        rule = str(body.get('repeat') or '').strip() or None
        repeat = rule and {'type': rule, 'days': body.get('weekdays'), 'day': body.get('month_day')}
        when = {k: body[k] for k in ('hours', 'minutes', 'seconds', 'time', 'date', 'days_from_today')
                if body.get(k) is not None and body.get(k) != ''}
        try:
            created = await client.create_alarm(
                kind=kind,
                label=str(body.get('label') or '').strip()[:60],
                say=str(body.get('say') or '').strip()[:200],
                repeat=repeat,
                **when,
            )
        except (ValueError, TypeError, OverflowError) as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        return web.json_response({'ok': True, 'item': created})

    async def clear_history(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        return web.json_response(await client.clear_history(bool(body.get('memory_only'))))

    async def bluetooth(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        args = {k: body[k] for k in ('enabled', 'window', 'remove', 'disconnect') if k in body}
        ok = await client.bluetooth(**args)
        return web.json_response({'ok': bool(ok), **({} if ok else {'error': '蓝牙没有响应'})})

    _wifi_lock = asyncio.Lock()

    async def airplay_get(_request):
        st = await client.refresh_airplay()
        return web.json_response(st, headers={'Cache-Control': 'no-store'})

    async def airplay_post(request):
        if not _local_origin_ok(request):
            return web.json_response({'ok': False, 'error': '仅限本机界面操作'}, status=403)
        body, err = await _json_dict(request)
        if err is not None:
            return err
        if 'enabled' not in body or not isinstance(body.get('enabled'), bool):
            return web.json_response({'ok': False, 'error': '参数需要布尔值 enabled'}, status=400)
        st = await client.set_airplay(body['enabled'])
        return web.json_response(st, status=200 if st.get('ok') else 502, headers={'Cache-Control': 'no-store'})

    async def wifi_get(request):
        cmd = (request.query.get('cmd') or 'wifi_status').strip()
        loop = asyncio.get_running_loop()
        if cmd == 'wifi_status':
            res = await loop.run_in_executor(None, net_ctl.wifi_status_sync)
        elif cmd == 'scan':
            rescan = request.query.get('rescan') != '0'
            res = await loop.run_in_executor(None, net_ctl.wifi_scan_sync, rescan)
        else:
            return web.json_response({'ok': False, 'reason': 'unknown'}, status=400)
        return web.json_response(res, headers={'Cache-Control': 'no-store'})

    async def wifi_post(request):
        if not _local_origin_ok(request):
            return web.json_response({'ok': False, 'error': '仅限本机界面操作'}, status=403)
        body, err = await _json_dict(request)
        if err is not None:
            return err
        cmd = str(body.get('cmd') or '').strip()
        loop = asyncio.get_running_loop()
        if cmd == 'wifi_status':
            res = await loop.run_in_executor(None, net_ctl.wifi_status_sync)
        elif cmd == 'scan':
            res = await loop.run_in_executor(None, net_ctl.wifi_scan_sync, body.get('rescan') is not False)
        elif cmd == 'connect':
            async with _wifi_lock:
                res = await loop.run_in_executor(None, net_ctl.wifi_connect_sync, body, None)
        elif cmd == 'forget':
            async with _wifi_lock:
                res = await loop.run_in_executor(None, net_ctl.wifi_forget_sync, body)
        else:
            return web.json_response({'ok': False, 'reason': 'unknown'}, status=400)
        return web.json_response(res, headers={'Cache-Control': 'no-store'})

    async def health(_request):
        return web.json_response({'version': __version__, 'ringing': bool(client.ring), **client.health()})

    async def prewarm(_request):
        if (
            not getattr(getattr(client, 'hub', None), 'connected', True)
            and getattr(client, 'state', '') == 'idle'
            and not getattr(client, 'ring', None)
            and hasattr(client, 'ensure_connected')
            and callable(getattr(getattr(client, 'hub', None), 'connect', None))
        ):
            try:
                coro = client.ensure_connected(quiet=True)
            except TypeError:
                coro = client.ensure_connected()
            asyncio.create_task(coro)
        return web.json_response({'ok': True})

    async def power_off(_request):
        return web.json_response(await client.power_off())

    async def qrcode_js(request):
        # the settings' 管理后台 QR code uses the kiosk shell's own generator (MIT, Kazuhiko Arase)
        for f in (Path('/usr/local/share/thinksmart-kiosk/qrcode.js'), ROOT.parent / 'kiosk' / 'web' / 'qrcode.js'):
            if f.is_file():
                return _serve_ram_file(request, f, 'text/javascript', 'utf-8')
        raise web.HTTPNotFound()

    async def network(_request):
        return web.json_response({'ip': local_ip()})

    async def restart_client(request):
        if request.remote not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            return web.json_response({'ok': False, 'error': '仅限本机调用'}, status=403)
        if client.state != 'idle' or client.ring:
            return web.json_response({'ok': False, 'error': '大屏正在对话或响铃中，请结束后再重启'}, status=409)
        asyncio.get_running_loop().call_later(0.15, os._exit, 0)
        return web.json_response({'ok': True})

    async def listening_finish(request):
        if request.remote not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            return web.json_response({'ok': False, 'error': '仅限本机调用'}, status=403)
        finished = await client.finish_follow_up()
        return web.json_response({'ok': True, 'finished': finished})

    async def navigate_page(request):
        body, err = await _json_dict(request)
        if err is not None:
            return err
        try:
            res = client.navigate_screen(body.get('page', 'home'), body.get('section'), body.get('city'))
        except ValueError as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)
        return web.json_response({'ok': True, **res})

    app.router.add_get('/', index)
    app.router.add_get('/v2', index)
    app.router.add_get('/events', events)
    app.router.add_get('/api/alarms', alarms_list)
    app.router.add_post('/api/alarms/create', alarm_create)
    app.router.add_post('/api/wake', action(lambda: client.wake('button')))
    app.router.add_post('/api/end', action(client.end))
    app.router.add_post('/api/listening/finish', listening_finish)
    app.router.add_post('/api/navigate', navigate_page)
    app.router.add_post('/api/abort', action(client.abort))
    app.router.add_post('/api/ptt/start', action(client.ptt_start))
    app.router.add_post('/api/ptt/stop', action(client.ptt_stop))
    app.router.add_post('/api/model', model)
    app.router.add_post('/api/text', text)
    app.router.add_post('/api/brightness', brightness)
    app.router.add_get('/api/display', display_state)
    app.router.add_post('/api/display', display_change)
    app.router.add_post('/api/display/preview', display_change)
    app.router.add_get('/api/rich/context', rich_context)
    app.router.add_post('/api/rich/music', rich_music_control)
    app.router.add_post('/api/screen-off', screen_off)
    app.router.add_post('/api/volume', volume)
    app.router.add_post('/api/alarm-volume', alarm_volume)
    app.router.add_post('/api/alarm-tone', alarm_tone)
    app.router.add_post('/api/alarms/delete', alarm_delete)
    app.router.add_post('/api/alarms/snooze', alarm_snooze)
    app.router.add_post('/api/alarms/stop', alarm_stop)
    app.router.add_post('/api/alarms/missed/clear', alarm_missed_clear)
    app.router.add_post('/api/history/clear', clear_history)
    app.router.add_post('/api/bluetooth', bluetooth)
    app.router.add_get('/api/airplay', airplay_get)
    app.router.add_post('/api/airplay', airplay_post)
    app.router.add_get('/api/wifi', wifi_get)
    app.router.add_post('/api/wifi', wifi_post)
    settings_api.register(app, client, _local_origin_ok)   # 设置页的全部参数
    app.router.add_get('/api/health', health)
    app.router.add_post('/api/prewarm', prewarm)
    app.router.add_get('/api/network', network)
    app.router.add_get('/qrcode.js', qrcode_js)
    app.router.add_post('/api/power-off', power_off)
    app.router.add_post('/api/restart', restart_client)
    app.router.add_get('/static/v2/{name}', static_v2)
    app.router.add_static('/static/', WEB)

    async def revalidate(request, response):
        # the kiosk keeps a disk cache; without this it goes on running the old app.js/css after
        # an update (heuristic freshness from Last-Modified). no-cache = check the ETag every load.
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache'
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    app.on_response_prepare.append(revalidate)
    return app
