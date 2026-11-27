#!/usr/bin/env python3
import errno
import glob
import json
import math
import os
import re
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
I2C_ACCEL_DRIVER = Path('/sys/bus/i2c/drivers/bmc150_accel_i2c')
TOUCH_EVENT = '/dev/input/event1'
ACCEL_DELTA_THRESHOLD = 40
POLL_SECONDS = 0.2
IDLE_ON_SECONDS = 1.5
FROZEN_CHECK_ON_TICKS = 8
FROZEN_MIN_SECONDS = 36.0
RECOVER_COOLDOWN_SECONDS = 60.0
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
ALS_PATH = Path('/run/user/1000/thinksmart-als.json')
AUTO_CURVE = ((0.1, 0), (0.3, 40), (0.8, 65), (1.5, 85), (2.0, 100))
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
_health_state = {'val': None, 'since': 0.0, 'count': 0, 'last_recover': 0.0}


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
    if is_auto and mode and MODE_PATH.is_file():
        try:
            als_d = json.loads(_read_small_text(ALS_PATH))
            ref_ts = time.time() if now is None else now
            if isinstance(als_d, dict) and _num(als_d.get('lux')) and _num(als_d.get('at')) and 0.0 <= float(als_d['lux']) <= 2000.0 and -2.0 <= ref_ts - als_d['at'] <= 15.0:
                lux = max(0.0, float(als_d['lux']))
                pct = float(AUTO_CURVE[-1][1])
                if lux <= AUTO_CURVE[0][0]:
                    pct = float(AUTO_CURVE[0][1])
                else:
                    for x0, x1 in zip(AUTO_CURVE, AUTO_CURVE[1:]):
                        if x0[0] <= lux <= x1[0]:
                            t = (math.log10(lux) - math.log10(x0[0])) / (math.log10(x1[0]) - math.log10(x0[0]))
                            pct = x0[1] + (x1[1] - x0[1]) * t
                            break
                val = 1400 + round((MAX_BRIGHTNESS - 1400) * max(0.0, min(100.0, pct)) / 100.0)
        except Exception:
            pass
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
    if brightness is not None:
        return brightness <= BLANKED_BRIGHTNESS_MAX
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


def _i2c_dev_name_for_iio(ad: Path):
    """Resolve the I2C bus device name (e.g. '0-0018') owning the IIO accel device."""
    try:
        resolved = Path(os.path.realpath(str(ad)))
    except OSError:
        resolved = ad
    for cand in (resolved, *resolved.parents):
        if re.fullmatch(r'\d+-[0-9a-fA-F]{4}', cand.name):
            return cand.name
    return None


def recover_frozen_accel(ad: Path, sleep_fn=time.sleep) -> bool:
    """Rebind the bmc150_accel_i2c driver to clear a BMA253 hardware ADC latchup."""
    global _RESOLVED_ACCEL
    if ad is None:
        return False
    dev_name = _i2c_dev_name_for_iio(ad)
    if not dev_name:
        return False
    unbind_p = I2C_ACCEL_DRIVER / 'unbind'
    bind_p = I2C_ACCEL_DRIVER / 'bind'
    if not (I2C_ACCEL_DRIVER / dev_name).exists() or not unbind_p.exists() or not bind_p.exists():
        return False
    try:
        unbind_p.write_text(dev_name + '\n')
        sleep_fn(0.05)
        bind_p.write_text(dev_name + '\n')
        _RESOLVED_ACCEL = None
        log(f'recovered frozen accelerometer {dev_name} via driver rebind')
        return True
    except OSError as e:
        log(f'failed to recover accelerometer {dev_name}: {e}')
        return False


def check_accel_health(curr, now_mono: float, sleep_fn=time.sleep) -> bool:
    """Detect when BMA253 outputs bit-identical samples continuously across samples & rebind."""
    if curr is None:
        return False
    if _health_state['val'] != curr:
        _health_state['val'] = curr
        _health_state['since'] = now_mono
        _health_state['count'] = 1
        return False
    _health_state['count'] += 1
    if _health_state['count'] < 4:
        return False
    if now_mono - _health_state['since'] < FROZEN_MIN_SECONDS:
        return False
    if _health_state['last_recover'] and now_mono - _health_state['last_recover'] < RECOVER_COOLDOWN_SECONDS:
        return False
    sleep_fn(0.025)
    curr2 = read_accel()
    if curr2 != curr:
        _health_state['val'] = curr2
        _health_state['since'] = now_mono
        _health_state['count'] = 1
        return False
    _health_state['last_recover'] = now_mono
    ad = _resolve_accel_dir()
    if recover_frozen_accel(ad, sleep_fn=sleep_fn):
        _health_state['val'] = None
        _health_state['since'] = now_mono
        _health_state['count'] = 0
        return True
    return False


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
    prev_off = False
    on_ticks = 0

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
            entered_off = off and not prev_off
            prev_off = off
            had_touch = False
            if touch_fd is not None and off:
                try:
                    ready, _, _ = select.select([touch_fd], [], [], 0 if entered_off else POLL_SECONDS)
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
                        if not entered_off:
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
                on_ticks += 1
                if on_ticks >= FROZEN_CHECK_ON_TICKS:
                    on_ticks = 0
                    if check_accel_health(read_accel(), time.monotonic()):
                        _health_state['val'] = None
                stop_event.wait(IDLE_ON_SECONDS)
                continue

            on_ticks = 0
            curr = read_accel()
            if check_accel_health(curr, time.monotonic()):
                curr = read_accel()
                if curr is not None:
                    last_accel = curr
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
