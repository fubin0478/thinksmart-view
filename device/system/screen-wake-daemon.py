#!/usr/bin/env python3
import errno
import glob
import json
import math
import os
import select
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

ACCEL_DIR = Path('/sys/bus/iio/devices/iio:device0')
ACCEL_X = ACCEL_DIR / 'in_accel_x_raw'
ACCEL_Y = ACCEL_DIR / 'in_accel_y_raw'
ACCEL_Z = ACCEL_DIR / 'in_accel_z_raw'
TOUCH_EVENT = '/dev/input/event1'
ACCEL_DELTA_THRESHOLD = 40
POLL_SECONDS = 0.2
IDLE_ON_SECONDS = 0.5
WAKE_COOLDOWN_SECONDS = 2.0
# idle-blanker blanks to 10; anything at or below this counts as a blanked screen
BLANKED_BRIGHTNESS_MAX = 10
MAX_BRIGHTNESS = 4095
MIRROR_FLAG_PATH = Path('/run/user/1000/uxplay-mirroring-active')
INTERCOM_FLAG_PATH = Path('/run/user/1000/intercom-call-active')
USER_BRIGHTNESS_PATH = Path('/run/user/1000/user_brightness')
LEGACY_USER_BRIGHTNESS_PATH = Path('/tmp/user_brightness')
HINT_PATH = Path('/run/user/1000/thinksmart-screen.json')
MODE_PATH = Path('/var/lib/thinksmart-monitor/display.json')
_DEFAULT_RUNTIME_DIR = Path('/run/user/1000')
NIGHT_FROM, NIGHT_TO = (0, 0), (6, 30)
NIGHT_LEVEL = 1400
NIGHT_VIEW_PATH = Path('/run/user/1000/thinksmart-night-view')
SUNRISE_PATH = Path('/run/user/1000/thinksmart-sunrise.json')
NIGHT_TEST = False
SLEEP_DIM_BRIGHTNESS = 1600
PROC_ROOT = Path('/proc')
BACKLIGHT_SYSFS = Path('/sys/class/backlight/backlight/brightness')
DRM_DPMS_PATH = Path('/sys/class/drm/card0-DSI-1/dpms')

last_wake = None
last_accel = None


def log(msg: str) -> None:
    print(f'[screen-wake] {msg}', flush=True)


def _num(v) -> bool:
    return type(v) in (int, float) and math.isfinite(v)


def _read_small_text(p: Path) -> str:
    fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f'Refusing non-regular path: {p}')
        chunks = []
        total = 0
        while True:
            buf = os.read(fd, 4096)
            if not buf:
                break
            total += len(buf)
            if total > 16384:
                raise OSError(f'File too large: {p}')
            chunks.append(buf)
        return b''.join(chunks).decode('utf-8')
    finally:
        os.close(fd)


def should_wake_night_low(now=None) -> bool:
    if HINT_PATH.parent != _DEFAULT_RUNTIME_DIR and not NIGHT_TEST:
        return False
    ts = time.time() if now is None else now
    try:
        sr = json.loads(_read_small_text(SUNRISE_PATH))
        if isinstance(sr, dict) and 'stopped' not in sr:
            st, ramp, hold = sr.get('start'), sr.get('ramp'), sr.get('hold')
            if (_num(st) and _num(ramp) and _num(hold)
                    and 0 < ramp <= 3600 and 0 <= hold <= 7200
                    and st <= ts < st + ramp + hold):
                return False
    except Exception:
        pass
    try:
        nv = json.loads(_read_small_text(NIGHT_VIEW_PATH))
        if isinstance(nv, dict) and _num(nv.get('until')) and ts < nv['until']:
            return True
    except Exception:
        pass
    lt = time.localtime(ts)
    hm = (lt.tm_hour, lt.tm_min)
    return NIGHT_FROM <= hm < NIGHT_TO if NIGHT_FROM <= NIGHT_TO else (hm >= NIGHT_FROM or hm < NIGHT_TO)


def read_wake_brightness(now=None) -> int:
    if should_wake_night_low(now):
        return NIGHT_LEVEL
    hint, mode = {}, {}
    try:
        h = json.loads(_read_small_text(HINT_PATH))
        if isinstance(h, dict):
            hint = h
    except Exception:
        pass
    try:
        m = json.loads(_read_small_text(MODE_PATH))
        if isinstance(m, dict):
            mode = m
    except Exception:
        pass
    b = hint.get('brightness')
    valid_b = type(b) in (int, float) and math.isfinite(b) and 0 <= b <= 100
    anchor_b = b if valid_b else 100
    anchor = mode.get('anchor')
    is_auto = mode.get('auto') is True and type(anchor) in (int, float) and anchor == anchor_b
    val = None
    if not is_auto and valid_b:
        val = 1400 + round((MAX_BRIGHTNESS - 1400) * max(0, min(100, b)) / 100)
    if val is None:
        for p in (USER_BRIGHTNESS_PATH, LEGACY_USER_BRIGHTNESS_PATH):
            try:
                v = int(_read_small_text(p).strip())
                if BLANKED_BRIGHTNESS_MAX < v <= MAX_BRIGHTNESS:
                    val = v
                    break
            except Exception:
                pass
    if val is None:
        val = MAX_BRIGHTNESS
    if hint.get('dim'):
        val = min(SLEEP_DIM_BRIGHTNESS, val)
    return val


def resume_webkit_if_idle() -> None:
    if MIRROR_FLAG_PATH.exists() or INTERCOM_FLAG_PATH.exists():
        return
    try:
        entries = os.scandir(PROC_ROOT)
    except Exception:
        return
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                if entry.stat(follow_symlinks=False).st_uid != 1000:
                    continue
                with open(os.path.join(entry.path, 'cmdline'), 'rb') as f:
                    parts = f.read(512).split(b'\x00')
                argv0 = parts[0]
                is_web = argv0 == b'WebKitWebProcess' or argv0.endswith(b'/WebKitWebProcess')
                is_net = argv0 == b'WebKitNetworkProcess' or argv0.endswith(b'/WebKitNetworkProcess')
                is_kiosk = any(p == b'/usr/local/bin/thinksmart-kiosk.py' or p.endswith(b'/thinksmart-kiosk.py') for p in parts[:4])
                if not (is_web or is_net or is_kiosk):
                    continue
                with open(os.path.join(entry.path, 'stat'), 'r', encoding='utf-8', errors='ignore') as f:
                    state = f.read(256).rsplit(')', 1)[1].split(None, 1)[0]
                if state in ('T', 't'):
                    os.kill(pid, signal.SIGCONT)
            except Exception:
                pass


def write_brightness(val: int) -> bool:
    # Write brightness directly from root -- fast path, no process spawn.
    if BACKLIGHT_SYSFS.is_symlink():
        log('refusing symlink backlight path')
        return False
    try:
        if BACKLIGHT_SYSFS.exists():
            BACKLIGHT_SYSFS.write_text(f'{val}\n')
            return True
        wrote = False
        for p in Path('/sys/class/backlight').glob('*/brightness'):
            if p.is_symlink():
                continue
            p.write_text(f'{val}\n')
            wrote = True
        return wrote
    except Exception:
        if BACKLIGHT_SYSFS.is_symlink():
            return False
        try:
            r = subprocess.run(
                ['tee', str(BACKLIGHT_SYSFS)],
                input=f'{val}\n', text=True,
                capture_output=True, timeout=2
            )
            return r.returncode == 0
        except Exception:
            return False


def drm_state() -> str:
    try:
        return DRM_DPMS_PATH.read_text().strip()
    except Exception:
        return 'unknown'


def read_brightness():
    try:
        return int(BACKLIGHT_SYSFS.read_text().strip())
    except Exception:
        return None


def screen_is_off() -> bool:
    brightness = read_brightness()
    if brightness is not None and brightness <= BLANKED_BRIGHTNESS_MAX:
        return True
    return drm_state() == 'Off'


def wake_display(reason: str) -> bool:
    global last_wake, last_accel
    if not screen_is_off():
        return False
    now = time.monotonic()
    if last_wake is not None and now - last_wake < WAKE_COOLDOWN_SECONDS:
        return False

    resume_webkit_if_idle()
    wake_val = read_wake_brightness()
    ok_bl = write_brightness(wake_val)
    if ok_bl:
        last_wake = now
        last_accel = None
    log(f'wake ({reason}): bl={"ok" if ok_bl else "fail"} ({wake_val}) drm={drm_state()}')
    return ok_bl


_RESOLVED_ACCEL = None


def _resolve_accel_dir():
    global _RESOLVED_ACCEL
    if _RESOLVED_ACCEL is not None and (_RESOLVED_ACCEL / 'in_accel_x_raw').exists():
        return _RESOLVED_ACCEL
    if (ACCEL_DIR / 'in_accel_x_raw').exists():
        _RESOLVED_ACCEL = ACCEL_DIR
        return ACCEL_DIR
    for cand_s in sorted(glob.glob('/sys/bus/iio/devices/iio:device*')):
        cand = Path(cand_s)
        if all((cand / f'in_accel_{ax}_raw').exists() for ax in ('x', 'y', 'z')):
            _RESOLVED_ACCEL = cand
            return cand
    return ACCEL_DIR if ACCEL_DIR.exists() else None


def read_accel():
    global _RESOLVED_ACCEL
    ad = _resolve_accel_dir()
    if ad is None:
        return None
    try:
        x = int((ad / 'in_accel_x_raw').read_text().strip())
        y = int((ad / 'in_accel_y_raw').read_text().strip())
        z = int((ad / 'in_accel_z_raw').read_text().strip())
        return (x, y, z)
    except Exception:
        _RESOLVED_ACCEL = None
        return None


def motion_detected(curr, prev):
    dx = abs(curr[0] - prev[0])
    dy = abs(curr[1] - prev[1])
    dz = abs(curr[2] - prev[2])
    return max(dx, dy, dz) >= ACCEL_DELTA_THRESHOLD, (dx, dy, dz)


def _resolve_touch_event() -> str:
    if TOUCH_EVENT != '/dev/input/event1' and os.path.exists(TOUCH_EVENT):
        return TOUCH_EVENT
    for name_p in sorted(glob.glob('/sys/class/input/event*/device/name')):
        try:
            nm = Path(name_p).read_text(encoding='utf-8', errors='ignore').strip().lower()
            if 'himax' in nm or 'touchscreen' in nm or 'ts' == nm:
                ev_name = Path(name_p).parent.parent.name
                cand = f'/dev/input/{ev_name}'
                if os.path.exists(cand):
                    return cand
        except OSError:
            pass
    return TOUCH_EVENT


def main() -> int:
    if _resolve_accel_dir() is None:
        log('accelerometer path missing')
        return 1

    log('starting as root service (zero I2C poll while screen is on)')

    stop_event = threading.Event()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, lambda *_: stop_event.set())

    touch_fd = None
    last_touch_open = None

    global last_accel
    last_accel = None

    try:
        while not stop_event.is_set():
            if touch_fd is None and (last_touch_open is None or time.monotonic() - last_touch_open >= 5.0):
                last_touch_open = time.monotonic()
                try:
                    ev_path = _resolve_touch_event()
                    touch_fd = os.open(ev_path, os.O_RDONLY | os.O_NONBLOCK)
                    log(f'touch input opened ({ev_path})')
                except Exception as e:
                    log(f'cannot open touch device: {e}')
                    touch_fd = None

            off = screen_is_off()
            had_touch = False
            if touch_fd is not None:
                try:
                    ready, _, _ = select.select([touch_fd], [], [], POLL_SECONDS if off else 0)
                    if ready:
                        got_touch = False
                        while True:
                            try:
                                if not os.read(touch_fd, 4096):
                                    raise OSError(errno.ENODEV, 'touch device EOF')
                                got_touch = True
                            except BlockingIOError:
                                break
                            except OSError as e:
                                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                                    raise
                                break
                        had_touch = True
                        if off and got_touch and wake_display('touch'):
                            continue
                except Exception as e:
                    log(f'touch read error: {e}')
                    try:
                        os.close(touch_fd)
                    except Exception:
                        pass
                    touch_fd = None

            if not off:
                last_accel = None
                stop_event.wait(IDLE_ON_SECONDS)
                continue

            curr = read_accel()
            if curr is not None and last_accel is not None:
                moved, deltas = motion_detected(curr, last_accel)
                if moved:
                    wake_display(f'accel d={deltas}')
            if curr is not None:
                last_accel = curr

            if touch_fd is None or had_touch:
                stop_event.wait(POLL_SECONDS)
    finally:
        if touch_fd is not None:
            try:
                os.close(touch_fd)
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
