"""Shared display preferences; the existing blanker remains the sole backlight owner."""
import asyncio
import json
import math
import os
import socket
import stat
import threading
import time
from pathlib import Path

MODE_PATH = Path('/var/lib/thinksmart-monitor/display.json')
HINT_PATH = Path('/run/user/1000/thinksmart-screen.json')
ALS_PATH = Path('/run/user/1000/thinksmart-als.json')
BACKLIGHT_PATH = Path('/sys/class/backlight/backlight/brightness')
MINIMUM, MAXIMUM = 1400, 4095
PREVIEW_SOCKET = Path('/run/user/1000/thinksmart-display.sock')
PREVIEW_TTL = 2.0


def valid_level(level):
    return type(level) is int and 0 <= level <= 100


def send_preview(body, path=PREVIEW_SOCKET):
    """Wake the backlight owner immediately, with no config writes or polling delay."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.setblocking(False)
        channel.sendto(json.dumps(body).encode(), str(path))


class BrightnessPreview:
    """Ephemeral drag state. The existing blanker remains the sole sysfs writer."""
    def __init__(self, path=PREVIEW_SOCKET):
        self.path = Path(path)
        self.channel = None
        self.level = None
        self.received = None

    def open(self):
        if os.path.lexists(self.path):
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError('Unexpected preview socket owner/type')
            self.path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            old_umask = os.umask(0o177)
            try:
                sock.bind(str(self.path))
            finally:
                os.umask(old_umask)
            self.path.chmod(0o600)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self.channel = sock
        return self

    def receive(self):
        if self.channel is None:
            return False
        woke = False
        for _ in range(32):
            try:
                raw = self.channel.recv(512)
            except BlockingIOError:
                break
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and set(data) == {'level'} and valid_level(data['level']):
                    self.level, self.received = data['level'], time.monotonic()
                    woke = True
                elif isinstance(data, dict) and data == {'clear': True}:
                    self.level, self.received = None, None
                elif isinstance(data, dict) and data == {'wake': True}:
                    woke = True
            except (ValueError, TypeError, UnicodeError):
                pass
        return woke

    def current(self):
        return self.level if self.level is not None and self.received is not None and time.monotonic()-self.received < PREVIEW_TTL else None

    def close(self):
        if self.channel is not None:
            self.channel.close()
            self.channel = None
            self.path.unlink(missing_ok=True)


def read_json(path):
    p = path if isinstance(path, Path) else Path(path)
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(p, flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
                return {}
            chunks, rem = [], 65537
            while rem > 0:
                b = os.read(fd, min(16384, rem))
                if not b:
                    break
                chunks.append(b)
                rem -= len(b)
            raw = b''.join(chunks)
            if len(raw) > 65536:
                return {}
        finally:
            os.close(fd)
        data = json.loads(raw.decode('utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def automatic_for(hint, mode):
    # A subsequent voice/original-settings change takes priority over automatic mode.
    level = hint.get('brightness', 100)
    anchor = mode.get('anchor')
    try:
        return (type(level) in (int, float) and math.isfinite(level) and 0 <= level <= 100
                and mode.get('auto') is True and type(anchor) in (int, float) and math.isfinite(anchor) and anchor == level)
    except OverflowError:
        return False


def ambient(path=ALS_PATH):
    data = read_json(path)
    if type(data.get('lux')) is bool or type(data.get('at')) is bool:
        return None
    try:
        lux, age = float(data['lux']), time.time() - float(data['at'])
        return lux if math.isfinite(lux) and 0 <= lux <= 2000 and -2 <= age < 15 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class DisplayControl:
    def __init__(self, mode=MODE_PATH, hint=HINT_PATH, als=ALS_PATH,
                 backlight=BACKLIGHT_PATH, core_url='http://127.0.0.1:8090', preview_socket=PREVIEW_SOCKET):
        self.mode, self.hint, self.als, self.backlight = map(Path, (mode, hint, als, backlight))
        self.core_url = core_url
        self.preview_socket = Path(preview_socket)
        self.lock = asyncio.Lock()

    def snapshot(self):
        hint, mode = read_json(self.hint), read_json(self.mode)
        b = hint.get('brightness', 100)
        fallback = round(max(0, min(100, b))) if type(b) in (int, float) and math.isfinite(b) else 100
        try:
            raw = int(self.backlight.read_text())
            level = round(max(0, min(100, (raw - MINIMUM) * 100 / (MAXIMUM - MINIMUM)))) if raw > 10 else fallback
        except (OSError, ValueError):
            raw, level = None, fallback
        lux = ambient(self.als)
        return {'auto': automatic_for(hint, mode), 'level': level,
                'lux': round(lux, 2) if lux is not None else None,
                'sensor_ok': lux is not None, 'dimmed': bool(hint.get('dim')),
                'screen_off': not (type(hint.get('idle')) is int and hint.get('idle') == 0), 'screen_on': raw is not None and raw > 10}

    def save(self, mode):
        self.mode.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(mode)
        temporary = self.mode.parent / f'.{self.mode.name}.{os.getpid()}.{threading.get_ident()}.tmp'
        created = False
        try:
            if self.mode.is_symlink():
                raise OSError('Refusing symlink mode path')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if self.mode.is_symlink():
                raise OSError('Refusing symlink mode path')
            os.replace(temporary, self.mode)
            created = False
        finally:
            if created:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    async def change(self, body):
        if not isinstance(body, dict) or len(body) != 1 or not set(body) <= {'auto', 'level'}:
            raise ValueError('请只指定自动模式或亮度')
        saved_level = None
        async with self.lock:
            if 'auto' in body:
                if type(body['auto']) is not bool:
                    raise ValueError('自动模式格式不正确')
                if body['auto']:
                    if ambient(self.als) is None:
                        raise ValueError('暂时没有光线读数，请先手动调整')
                    hint = read_json(self.hint)
                    b = hint.get('brightness', 100)
                    if type(b) not in (int, float) or not math.isfinite(b) or not 0 <= b <= 100:
                        raise ValueError('暂时无法读取屏幕设置')
                    self.save({'auto': True, 'anchor': b})
                    self.clear_preview()
                else:
                    # Freeze the current automatic level when switching to manual.
                    saved_level = self.snapshot()['level']
                    await self.manual(saved_level)
            else:
                level = body['level']
                if not valid_level(level):
                    raise ValueError('亮度需为 0 到 100 的整数')
                saved_level = level
                await self.manual(level)
        value = self.snapshot()
        if saved_level is not None:
            # The sysfs owner may still be consuming the hint; acknowledge the saved
            # target, never return an old physical value that pulls the thumb back.
            value.update(level=saved_level, auto=False)
        return value

    async def preview(self, body):
        if not isinstance(body, dict) or set(body) != {'level'} or not valid_level(body['level']):
            raise ValueError('亮度需为 0 到 100 的整数')
        async with self.lock:
            send_preview(body, self.preview_socket)
        return {'level': body['level'], 'auto': False}

    def clear_preview(self):
        try:
            send_preview({'clear': True}, self.preview_socket)
        except OSError:
            pass  # Old/missing blanker still supports the durable manual API.

    async def manual(self, level):
        import aiohttp
        orig_cls = getattr(self, '_orig_aiohttp_cls', None)
        if orig_cls is None:
            orig_cls = aiohttp.ClientSession
            self._orig_aiohttp_cls = orig_cls

        async def _post_with(session):
            async with session.post(self.core_url + '/api/brightness', json={'level': level}) as response:
                try:
                    content = getattr(response, 'content', None)
                    if content is not None and hasattr(content, 'read'):
                        chunks, total = [], 0
                        while total <= 65536:
                            part = await content.read(min(16384, 65537 - total))
                            if not part:
                                break
                            chunks.append(part)
                            total += len(part)
                        raw = b''.join(chunks)
                        if len(raw) > 65536:
                            raise ValueError('response too large')
                        result = json.loads(raw.decode('utf-8'))
                    else:
                        result = await response.json(content_type=None)
                except (ValueError, TypeError, UnicodeDecodeError) as exc:
                    raise RuntimeError('亮度没有保存，请重试') from exc
                if response.status != 200 or not isinstance(result, dict) or result.get('ok') is not True:
                    raise RuntimeError('亮度没有保存，请重试')

        if aiohttp.ClientSession is orig_cls:
            loop = asyncio.get_running_loop()
            sess = getattr(self, '_http_sess', None)
            if sess is None or sess.closed or getattr(self, '_http_loop', None) is not loop:
                if sess is not None and not sess.closed:
                    try:
                        await sess.close()
                    except Exception:
                        pass
                sess = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False)
                self._http_sess = sess
                self._http_loop = loop
            await _post_with(sess)
        else:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False) as session:
                await _post_with(session)
        self.save({'auto': False})
        self.clear_preview()
