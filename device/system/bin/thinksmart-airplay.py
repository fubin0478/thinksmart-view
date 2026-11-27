#!/usr/bin/env python3
"""
ThinkSmart View Unified AirPlay (Audio + High-FPS Screen Mirroring + Auto-Rotate) Supervisor.
- Runs custom-built UxPlay 1.74 ("ThinkSmart") with zero per-packet debug overhead,
  2 MB TCP SO_RCVBUF, 4-core multi-threaded avdec_h264 (slice+frame, output-corrupt=false),
  ARM64 NEON 8x8 I420 90r/90l accelerator + smart black-bar auto-crop (libuxplay_autorotate.so),
  and Adreno 506 GPU hardware wp_viewporter scaling via waylandsink with VSync clock pacing.
- Uses a deterministic MAC derived from /etc/machine-id so AirPlay deviceid remains permanent
  across reboots and NetworkManager Wi-Fi MAC randomization.
- Automatically pauses local QQ Music & active XiaoZhi session when AirPlay audio or mirroring starts,
  and coordinates cleanly with Intercom calls (/run/user/1000/intercom-call-active) and idle-blanker.
"""

import hashlib
import json
import math
import os
import signal
import socket
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"))
WAYLAND_SOCKET = RUNTIME_DIR / "wayland-1"
HINT_PATH = RUNTIME_DIR / "thinksmart-screen.json"
MIRROR_FLAG_PATH = RUNTIME_DIR / "uxplay-mirroring-active"
INTERCOM_FLAG_PATH = RUNTIME_DIR / "intercom-call-active"
BACKLIGHT_PATH = Path("/sys/class/backlight/backlight/brightness")
AUTOROTATE_SO = "/usr/local/lib/libuxplay_autorotate.so"
UXPLAY_BIN = "/usr/local/bin/uxplay" if Path("/usr/local/bin/uxplay").exists() else "/usr/bin/uxplay"


def log(msg: str) -> None:
    print(f"[thinksmart-airplay] {msg}", flush=True)


def _should_log_uxplay_line(line: str, last_meta: dict) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith("audio progress (min:sec):") or ("Audio Metadata" in s and s.startswith("===")):
        return False
    for mk in ("Album:", "Artist:", "Title:"):
        if s.startswith(mk):
            if last_meta.get(mk) == s:
                return False
            last_meta[mk] = s
            return True
    return True


def _read_regular_file(path: Path, max_bytes: int = 8192) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError("Not a regular small file")
        return os.read(fd, max_bytes).decode("utf-8")
    finally:
        os.close(fd)


def get_deterministic_mac() -> str:
    """Derive a stable locally-administered unicast MAC from /etc/machine-id."""
    seed = "thinksmart-view"
    try:
        mid = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        if mid:
            seed = mid
    except Exception:
        pass
    digest = hashlib.sha256(f"thinksmart-airplay:{seed}".encode("utf-8")).digest()
    b0 = (digest[0] & 0xFE) | 0x02  # unicast + locally administered
    return f"{b0:02x}:{digest[1]:02x}:{digest[2]:02x}:{digest[3]:02x}:{digest[4]:02x}:{digest[5]:02x}"


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_last_pause_ts: float = 0.0
_screen_lock = threading.Lock()


def pause_bluetooth_media() -> None:
    """Pause any playing Bluetooth A2DP/AVRCP phone stream via BlueZ SystemBus."""
    try:
        import dbus
        bus = dbus.SystemBus(private=True)
        try:
            om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
            for path, ifaces in om.GetManagedObjects().items():
                mp = ifaces.get("org.bluez.MediaPlayer1")
                if mp and mp.get("Status") == "playing":
                    dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.MediaPlayer1").Pause()
        finally:
            bus.close()
    except Exception:
        pass


def pause_conflicting_media(force: bool = False) -> None:
    """Asynchronously pause local QQ Music, Bluetooth audio, and XiaoZhi session/alarms when AirPlay starts."""
    global _last_pause_ts
    now = time.monotonic()
    if not force and _last_pause_ts > 0.0 and now - _last_pause_ts < 2.0:
        return
    _last_pause_ts = now

    def _worker():
        pause_bluetooth_media()
        targets = [
            ("http://127.0.0.1:8091/api/music/control", b'{"action":"pause","source":"airplay"}', {"Content-Type": "application/json"}),
            ("http://127.0.0.1:8095/api/home/messages/play", b'{"stop":true}', {"Content-Type": "application/json", "Host": "127.0.0.1:8095"}),
            ("http://127.0.0.1:8095/api/home/voice_reply", b'{"action":"cancel"}', {"Content-Type": "application/json", "Host": "127.0.0.1:8095"}),
            ("http://127.0.0.1:8090/api/end", b"{}", {"Content-Type": "application/json"}),
            ("http://127.0.0.1:8090/api/alarms/stop", b"{}", {"Content-Type": "application/json"}),
            ("http://127.0.0.1:8090/api/alarm-tone", b'{"stop":true}', {"Content-Type": "application/json"}),
        ]
        if os.path.lexists("/run/systemd/units/invocation:thinksmart-tutor.service"):
            targets.insert(3, ("http://127.0.0.1:8096/api/tutor/voice_record", b'{"action":"cancel"}', {"Content-Type": "application/json", "Host": "127.0.0.1:8096"}))
        for url, payload, hdrs in targets:
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers=hdrs,
                    method="POST",
                )
                with _NO_PROXY_OPENER.open(req, timeout=1.2) as r:
                    r.read(64)
            except urllib.error.HTTPError as e:
                try:
                    e.close()
                except Exception:
                    pass
            except Exception:
                pass
        if MIRROR_FLAG_PATH.exists():
            set_screen_hold(True, require_mirror=True)

    threading.Thread(target=_worker, daemon=True).start()


AIRPLAY_TUNE_BIN = "/usr/local/sbin/thinksmart-airplay-tune"
YIELD_AIRPLAY_FLAG = Path("/run/user/1000/qqmusic-yield-airplay")


def _audio_flag_path() -> Path:
    return MIRROR_FLAG_PATH.with_name("uxplay-audio-active")


def _yield_flag_path() -> Path:
    return MIRROR_FLAG_PATH.with_name("qqmusic-yield-airplay")


def set_audio_active(active: bool) -> None:
    afp = _audio_flag_path()
    try:
        with _screen_lock:
            if not active:
                afp.unlink(missing_ok=True)
                return
            atmp = afp.with_name(f".{afp.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            created = False
            try:
                if afp.is_symlink():
                    raise OSError("Refusing symlink audio flag")
                afd = os.open(atmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
                created = True
                with os.fdopen(afd, "w", encoding="utf-8") as af:
                    os.fchmod(af.fileno(), 0o644)
                    if os.getuid() == 0:
                        try:
                            os.fchown(af.fileno(), 1000, 1000)
                        except OSError:
                            pass
                    af.write("1\n")
                if afp.is_symlink():
                    raise OSError("Refusing symlink audio flag")
                os.replace(atmp, afp)
            finally:
                if created:
                    try:
                        atmp.unlink(missing_ok=True)
                    except OSError:
                        pass
    except Exception:
        pass


def set_screen_hold(hold: bool, require_mirror: bool = False) -> None:
    tmp = HINT_PATH.with_name(f".{HINT_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    created = False
    try:
        with _screen_lock:
            if require_mirror and not MIRROR_FLAG_PATH.exists():
                return
            if hold:
                mtmp = MIRROR_FLAG_PATH.with_name(f".{MIRROR_FLAG_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                mcreated = False
                try:
                    if MIRROR_FLAG_PATH.is_symlink():
                        raise OSError("Refusing symlink mirror flag")
                    mfd = os.open(mtmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
                    mcreated = True
                    with os.fdopen(mfd, "w", encoding="utf-8") as mf:
                        os.fchmod(mf.fileno(), 0o644)
                        if os.getuid() == 0:
                            try:
                                os.fchown(mf.fileno(), 1000, 1000)
                            except OSError:
                                pass
                        mf.write("1\n")
                    if MIRROR_FLAG_PATH.is_symlink():
                        raise OSError("Refusing symlink mirror flag")
                    os.replace(mtmp, MIRROR_FLAG_PATH)
                finally:
                    if mcreated:
                        try:
                            mtmp.unlink(missing_ok=True)
                        except OSError:
                            pass
            else:
                MIRROR_FLAG_PATH.unlink(missing_ok=True)
                if INTERCOM_FLAG_PATH.exists():
                    log("intercom call active -> keeping screen hold")
                    return
            data = {}  # 2026-11-06: no invented brightness/idle defaults; idle-blanker applies its own
            if HINT_PATH.exists() and not HINT_PATH.is_symlink():
                try:
                    loaded = json.loads(_read_regular_file(HINT_PATH))
                    if isinstance(loaded, dict):
                        data.update(loaded)
                except Exception:
                    pass
            data["seq"] = time.time_ns()
            data["hold"] = bool(hold)
            idle_val = data.get("idle")
            if not hold and type(idle_val) is int and idle_val > 0:
                data["after"] = idle_val
            else:
                data.pop("after", None)
            if HINT_PATH.is_symlink():
                raise OSError("Refusing symlink screen hint")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
            created = True
            with os.fdopen(fd, "w", encoding="utf-8") as tf:
                os.fchmod(tf.fileno(), 0o644)
                if os.getuid() == 0:
                    try:
                        os.fchown(tf.fileno(), 1000, 1000)
                    except OSError:
                        pass
                tf.write(json.dumps(data))
            if HINT_PATH.is_symlink():
                raise OSError("Refusing symlink screen hint")
            os.replace(tmp, HINT_PATH)
            if hold and BACKLIGHT_PATH.exists() and not BACKLIGHT_PATH.is_symlink():
                try:
                    cur_bl = int(BACKLIGHT_PATH.read_text(encoding="utf-8").strip())
                    if cur_bl <= 10:
                        ts_now = time.time()
                        sunrise_active = False
                        try:
                            sr = json.loads(_read_regular_file(RUNTIME_DIR / "thinksmart-sunrise.json"))
                            if isinstance(sr, dict) and "stopped" not in sr:
                                st, ramp, shold = sr.get("start"), sr.get("ramp"), sr.get("hold")
                                if (
                                    all(type(v) in (int, float) and math.isfinite(v) for v in (st, ramp, shold))
                                    and 0 < ramp <= 3600 and 0 <= shold <= 7200 and st <= ts_now < st + ramp + shold
                                ):
                                    sunrise_active = True
                        except Exception:
                            pass
                        night_low = False
                        if not sunrise_active:
                            try:
                                nv = json.loads(_read_regular_file(RUNTIME_DIR / "thinksmart-night-view"))
                                if isinstance(nv, dict) and type(nv.get("until")) in (int, float) and ts_now < nv["until"]:
                                    night_low = True
                            except Exception:
                                pass
                            if not night_low:
                                lt = time.localtime(ts_now)
                                night_low = (0, 0) <= (lt.tm_hour, lt.tm_min) < (6, 30)
                        if night_low:
                            target_bl = 1400
                        else:
                            mode_d = {}
                            try:
                                m = json.loads(_read_regular_file(Path("/var/lib/thinksmart-monitor/display.json")))
                                if isinstance(m, dict):
                                    mode_d = m
                            except Exception:
                                pass
                            b = data.get("brightness")
                            valid_b = type(b) in (int, float) and math.isfinite(b) and 0 <= b <= 100
                            anchor = mode_d.get("anchor")
                            anchor_b = b if valid_b else 100
                            is_auto = mode_d.get("auto") is True and type(anchor) in (int, float) and math.isfinite(anchor) and anchor == anchor_b
                            target_bl = None
                            if is_auto and mode_d and Path("/var/lib/thinksmart-monitor/display.json").is_file():
                                try:
                                    als_d = json.loads(_read_regular_file(RUNTIME_DIR / "thinksmart-als.json", 4096))
                                    lux_v = als_d.get("lux") if isinstance(als_d, dict) else None
                                    at_v = als_d.get("at") if isinstance(als_d, dict) else None
                                    if (
                                        type(lux_v) in (int, float)
                                        and math.isfinite(lux_v)
                                        and type(at_v) in (int, float)
                                        and math.isfinite(at_v)
                                        and 0.0 <= float(lux_v) <= 2000.0
                                        and -2.0 <= time.time() - float(at_v) <= 15.0
                                    ):
                                        lux = max(0.0, float(lux_v))
                                        curve = ((0.1, 0), (0.3, 40), (0.8, 65), (1.5, 85), (2.0, 100))
                                        pct = float(curve[-1][1])
                                        if lux <= curve[0][0]:
                                            pct = float(curve[0][1])
                                        else:
                                            for x0, x1 in zip(curve, curve[1:]):
                                                if x0[0] <= lux <= x1[0]:
                                                    t = (math.log10(lux) - math.log10(x0[0])) / (math.log10(x1[0]) - math.log10(x0[0]))
                                                    pct = x0[1] + (x1[1] - x0[1]) * t
                                                    break
                                        target_bl = 1400 + round((4095 - 1400) * max(0.0, min(100.0, pct)) / 100.0)
                                except Exception:
                                    pass
                            if not is_auto and valid_b:
                                target_bl = 1400 + round((4095 - 1400) * max(0, min(100, b)) / 100)
                            if target_bl is None:
                                target_bl = 1600 if is_auto else 2000
                                for ub_path in (Path("/run/user/1000/user_brightness"), Path("/tmp/user_brightness")):
                                    if ub_path.exists() and not ub_path.is_symlink():
                                        try:
                                            v = int(_read_regular_file(ub_path, 64).strip())
                                            if 10 < v <= 4095:
                                                target_bl = v
                                                break
                                        except Exception:
                                            pass
                            if data.get("dim"):
                                target_bl = min(1600, target_bl)
                        if require_mirror and not MIRROR_FLAG_PATH.exists():
                            return
                        bl_str = f"{target_bl}\n"
                        if BACKLIGHT_PATH.is_symlink():
                            raise OSError("Refusing symlink backlight path")
                        try:
                            BACKLIGHT_PATH.write_text(bl_str, encoding="utf-8")
                        except PermissionError:
                            subprocess.run(
                                ["/usr/bin/sudo", "-n", "/usr/bin/tee", str(BACKLIGHT_PATH)],
                                input=bl_str,
                                text=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=2,
                            )
                except Exception:
                    pass
        log(f"screen hold -> {hold} (seq={data['seq']})")
    except Exception as exc:
        log(f"failed to update screen hint: {exc}")
    finally:
        if created:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def set_webkit_paused(paused: bool) -> None:
    if not paused and INTERCOM_FLAG_PATH.exists():
        log("intercom call active -> keeping Kiosk+WebKit paused")
        return
    screen_blanked = False
    if not paused and BACKLIGHT_PATH.exists() and not BACKLIGHT_PATH.is_symlink():
        try:
            screen_blanked = int(BACKLIGHT_PATH.read_text(encoding="utf-8").strip()) <= 10
        except Exception:
            screen_blanked = False

    sig = signal.SIGSTOP if paused else signal.SIGCONT
    my_pid = os.getpid()
    signaled = []
    try:
        entries = os.scandir("/proc")
    except Exception:
        entries = None
    if entries is not None:
        with entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    pid = int(entry.name)
                    if pid == my_pid or entry.stat(follow_symlinks=False).st_uid != 1000:
                        continue
                    with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                        parts = f.read(512).split(b"\x00")[:4]
                    raw0 = parts[0] if parts else b""
                    is_webproc = raw0 == b"WebKitWebProcess" or raw0.endswith(b"/WebKitWebProcess")
                    is_net_or_kiosk = (
                        raw0 == b"WebKitNetworkProcess"
                        or raw0.endswith(b"/WebKitNetworkProcess")
                        or any(
                            p == b"/usr/local/bin/thinksmart-kiosk.py" or p.endswith(b"/thinksmart-kiosk.py")
                            for p in parts[1:4]
                        )
                    )
                    if is_webproc and screen_blanked and not paused:
                        continue
                    if is_webproc or is_net_or_kiosk:
                        try:
                            with open(os.path.join(entry.path, "stat"), "r", encoding="utf-8", errors="ignore") as f:
                                stat_txt = f.read(256)
                            if ")" in stat_txt:
                                pstate = stat_txt.rsplit(")", 1)[1].split(None, 1)[0]
                                if paused and pstate in ("T", "t", "Z", "X"):
                                    continue
                                if not paused and pstate not in ("T", "t"):
                                    continue
                        except OSError:
                            pass
                        os.kill(pid, sig)
                        signaled.append(pid)
                except Exception:
                    pass
    if signaled:
        log(
            f"background Kiosk+WebKit ({signaled}) -> "
            f"{'PAUSED (SIGSTOP)' if paused else 'RESUMED (SIGCONT)'}"
        )


def find_bg_voice_pids() -> list[int]:
    my_pid = os.getpid()
    pids = []
    try:
        entries = os.scandir("/proc")
    except Exception:
        return pids
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                if pid == my_pid or entry.stat(follow_symlinks=False).st_uid != 1000:
                    continue
                with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                    raw_cmd = f.read(512)
                if b"thinksmart_client" in raw_cmd or b"xiaozhi-server" in raw_cmd:
                    pids.append(pid)
            except Exception:
                pass
    return pids


def apply_system_tuning() -> None:
    try:
        r = subprocess.run(
            ["/usr/bin/sudo", "-n", AIRPLAY_TUNE_BIN, "init"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        if r.returncode == 0:
            log("applied IRQ isolation + RPS + schedutil 10ms + Weston/ir3q0 nice -15")
        else:
            log(f"system tuning helper returned {r.returncode}")
    except Exception as exc:
        log(f"system tuning warning: {exc}")


def set_hardware_turbo(active: bool, uxplay_pid: int = 0) -> None:
    mode = "on" if active else "off"
    args = ["/usr/bin/sudo", "-n", AIRPLAY_TUNE_BIN, "turbo", mode]
    if active and uxplay_pid > 0:
        args.append(str(int(uxplay_pid)))
    try:
        r = subprocess.run(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        if r.returncode == 0:
            gov = "performance" if active else "schedutil"
            gpu_mhz = 650 if active else 19
            log(f"hardware turbo -> CPU={gov}, Adreno506_min={gpu_mhz}MHz, uxplay_pid={uxplay_pid}")
        else:
            log(f"hardware turbo helper ({mode}) returned {r.returncode}")
    except Exception as exc:
        log(f"failed to set hardware turbo ({active}): {exc}")


def boost_process_priority(pid: int) -> None:
    try:
        subprocess.run(
            ["/usr/bin/sudo", "-n", AIRPLAY_TUNE_BIN, "boost", str(int(pid))],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        pass


def wait_for_wayland(timeout_sec: int = 60, stop_event: threading.Event | None = None) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return
        if WAYLAND_SOCKET.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    s.connect(str(WAYLAND_SOCKET))
                return
            except Exception:
                pass
        if stop_event is not None:
            if stop_event.wait(0.3):
                return
        else:
            time.sleep(0.3)
    if stop_event is None or not stop_event.is_set():
        log(f"warning: {WAYLAND_SOCKET} not connectable after {timeout_sec}s; proceeding anyway")


def wayland_inode() -> int:
    try:
        return WAYLAND_SOCKET.stat().st_ino
    except Exception:
        return 0


def start_wayland_watchdog(get_proc, interval: float = 5.0, stop_event: threading.Event | None = None, on_stop=None) -> None:
    """uxplay keeps one Wayland connection for its whole life. When thinksmart-kiosk restarts,
    Weston is replaced and that connection dies, so the next mirror session would fail. While
    nobody is mirroring, restart uxplay as soon as the Wayland socket is replaced.
    Also yield any active silent AirPlay session promptly when an intercom call starts or QQ Music starts."""
    def _run():
        interval_f = max(0.05, float(interval))
        last_wl = time.monotonic()
        while True:
            is_busy = MIRROR_FLAG_PATH.exists() or _audio_flag_path().exists()
            step = min(interval_f, 0.2 if is_busy else 1.0)
            if stop_event is not None:
                if stop_event.wait(step):
                    return
            else:
                time.sleep(step)
            proc, launch_ino = get_proc()
            if proc is None or proc.poll() is not None:
                last_wl = time.monotonic()
                continue
            yfp = _yield_flag_path()
            if yfp.exists() and not MIRROR_FLAG_PATH.exists():
                yfp.unlink(missing_ok=True)
                if _audio_flag_path().exists():
                    log("QQ Music started -> yielding AirPlay audio session")
                    if on_stop is not None:
                        on_stop()
                    stop_proc(proc)
                    last_wl = time.monotonic()
                    continue
            if INTERCOM_FLAG_PATH.exists() and (MIRROR_FLAG_PATH.exists() or _audio_flag_path().exists()):
                log("intercom call active -> yielding AirPlay session")
                if on_stop is not None:
                    on_stop()
                stop_proc(proc)
                last_wl = time.monotonic()
                continue
            now_m = time.monotonic()
            if now_m - last_wl >= interval_f:
                last_wl = now_m
                ino = wayland_inode()
                if ino and ino != launch_ino and not MIRROR_FLAG_PATH.exists() and not _audio_flag_path().exists():
                    log("Weston was restarted (Wayland socket replaced) -> restarting idle uxplay")
                    if on_stop is not None:
                        on_stop()
                    stop_proc(proc)

    threading.Thread(target=_run, daemon=True, name="wayland-watchdog").start()


def build_cmd() -> list[str]:
    mac = get_deterministic_mac()
    return [
        "/usr/bin/stdbuf", "-oL", "-eL",
        UXPLAY_BIN,
        "-n", "ThinkSmart",
        "-nh",
        "-m", mac,
        "-srgb", "no",
        "-s", "1280x1280@60",
        "-fps", "30",
        "-vsync", "40",
        "-vp", "h264parse config-interval=-1",
        "-vd", "avdec_h264 thread-type=slice max-threads=4 output-corrupt=false discard-corrupted-frames=true qos=false",
        "-vc", (
            "queue max-size-buffers=6 max-size-bytes=0 max-size-time=0 ! "
            "videoflip name=autoflip ! "
            "videocrop name=autocrop ! "
            "capssetter name=autopar join=true replace=false "
            "caps=video/x-raw,pixel-aspect-ratio=1/1"
        ),
        "-vs", "waylandsink fullscreen=true ts-offset=40000000 max-lateness=-1 qos=false enable-last-sample=false",
        "-as", "pulsesink",
        "-taper",
        "-nohold",
        "-nofreeze",
        "-reset", "9",
    ]


def stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass
    except Exception:
        pass


def main() -> int:
    # Startup failsafe & multi-core IRQ/RPS/Weston tuning
    if MIRROR_FLAG_PATH.exists() or MIRROR_FLAG_PATH.is_symlink():
        set_screen_hold(False)
    MIRROR_FLAG_PATH.unlink(missing_ok=True)
    set_audio_active(False)
    set_webkit_paused(False)
    set_hardware_turbo(False)

    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = str(RUNTIME_DIR)
    env["WAYLAND_DISPLAY"] = "wayland-1"
    env["PULSE_SERVER"] = f"unix:{RUNTIME_DIR}/pulse/native"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={RUNTIME_DIR}/bus"
    env.setdefault("GST_DEBUG", "*:2,vadisplay:0")
    if Path(AUTOROTATE_SO).exists():
        env["LD_PRELOAD"] = AUTOROTATE_SO

    shutting_down = False
    shutdown_event = threading.Event()
    current_proc = None
    launch_ino = 0
    turbo_active = False
    turbo_pid = 0
    short_failures = 0

    def enter_mirror_turbo(uxplay_pid: int = 0) -> None:
        nonlocal turbo_active, turbo_pid
        if turbo_active:
            if uxplay_pid > 0 and uxplay_pid != turbo_pid:
                turbo_pid = uxplay_pid
                set_hardware_turbo(True, uxplay_pid)
            return
        turbo_active = True
        turbo_pid = uxplay_pid
        pause_conflicting_media(force=True)
        set_screen_hold(True)
        set_webkit_paused(True)
        set_hardware_turbo(True, uxplay_pid)

    def exit_mirror_turbo() -> None:
        nonlocal turbo_active, turbo_pid
        if not turbo_active:
            with _screen_lock:
                MIRROR_FLAG_PATH.unlink(missing_ok=True)
            return
        turbo_active = False
        turbo_pid = 0
        set_screen_hold(False)
        set_webkit_paused(False)
        set_hardware_turbo(False)

    def _force_kill_proc(p):
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def handle_sig(signum, _frame):
        nonlocal shutting_down
        shutting_down = True
        shutdown_event.set()
        proc = current_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                t = threading.Timer(1.5, _force_kill_proc, args=(proc,))
                t.daemon = True
                t.start()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)
    wait_for_wayland(60, shutdown_event)
    if shutting_down:
        return 0
    apply_system_tuning()
    tuned_ino = wayland_inode()

    cmd = build_cmd()
    log(f"deterministic AirPlay MAC: {cmd[cmd.index('-m') + 1]}")
    expected_restart = False

    def mark_expected_restart() -> None:
        nonlocal expected_restart
        expected_restart = True

    start_wayland_watchdog(lambda: (current_proc, launch_ino), stop_event=shutdown_event, on_stop=mark_expected_restart)
    try:
        while not shutting_down:
            while not shutting_down and INTERCOM_FLAG_PATH.exists():
                shutdown_event.wait(0.8)
            wait_for_wayland(30, shutdown_event)
            if shutting_down:
                break
            if INTERCOM_FLAG_PATH.exists():
                continue
            launch_ino = wayland_inode()
            if launch_ino and launch_ino != tuned_ino:
                apply_system_tuning()
                tuned_ino = launch_ino
            started_at = time.monotonic()
            expected_restart = False
            _yield_flag_path().unlink(missing_ok=True)
            log(f"launching {UXPLAY_BIN} (Adreno 506 GPU viewport + ARM64 NEON + VSync 30fps)")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            current_proc = proc
            if shutting_down:
                stop_proc(proc)
                break
            boost_process_priority(proc.pid)
            mirror_active = False
            audio_active = False
            last_meta = {}

            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                if _should_log_uxplay_line(line, last_meta):
                    print(line, flush=True)

                if INTERCOM_FLAG_PATH.exists() and (mirror_active or audio_active):
                    expected_restart = True
                    log("intercom call active -> yielding AirPlay session")
                    if audio_active:
                        audio_active = False
                        set_audio_active(False)
                    if mirror_active:
                        mirror_active = False
                        exit_mirror_turbo()
                    stop_proc(proc)
                    break

                if "Error communicating with the wayland server" in line or "Error communicating with wayland" in line:
                    expected_restart = True
                    log("lost the Wayland connection -> restarting uxplay")
                    if audio_active:
                        audio_active = False
                        set_audio_active(False)
                    if mirror_active:
                        mirror_active = False
                        exit_mirror_turbo()
                    stop_proc(proc)
                    break

                if (
                    "raop_rtp starting audio" in line
                    or "Begin streaming to GStreamer audio pipeline" in line
                ):
                    if INTERCOM_FLAG_PATH.exists():
                        expected_restart = True
                        stop_proc(proc)
                        break
                    was_audio = audio_active
                    audio_active = True
                    set_audio_active(True)
                    if not was_audio and not mirror_active:
                        pause_conflicting_media()

                if audio_active and (
                    ("raop_rtp_audio" in line and ("exiting" in line or "stopped" in line or "closed" in line))
                    or "End streaming to GStreamer audio pipeline" in line
                    or "ntp timeout" in line
                    or "Resetting" in line
                    or "Open connections: 0" in line
                    or "lost connection with client" in line
                ):
                    audio_active = False
                    set_audio_active(False)

                if (
                    "raop_rtp_mirror starting mirroring" in line
                    or "Begin streaming to GStreamer video pipeline" in line
                ):
                    if INTERCOM_FLAG_PATH.exists():
                        expected_restart = True
                        stop_proc(proc)
                        break
                    mirror_active = True
                    enter_mirror_turbo(proc.pid)

                elif mirror_active and (
                    "raop_rtp_mirror tcp socket was closed" in line
                    or "raop_rtp_mirror tcp socket is closed" in line
                    or "raop_rtp_mirror exiting TCP thread" in line
                    or "raop_rtp_mirror->running is no longer true" in line
                    or "End streaming to GStreamer video pipeline" in line
                    or "ntp timeout" in line
                    or "Resetting" in line
                    or "Open connections: 0" in line
                    or "lost connection with client" in line
                    or "xdg_wm_base" in line
                    or "Error communicating with wayland" in line
                ):
                    log("mirroring stopped -> restoring UI & closing video window immediately")
                    expected_restart = True
                    mirror_active = False
                    audio_active = False
                    set_audio_active(False)
                    exit_mirror_turbo()
                    stop_proc(proc)
                    break

            rc = proc.wait()
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            current_proc = None
            set_audio_active(False)
            exit_mirror_turbo()
            if not shutting_down:
                cur_ino = wayland_inode()
                display_changed = bool(cur_ino and cur_ino != launch_ino)
                if expected_restart or display_changed or INTERCOM_FLAG_PATH.exists() or time.monotonic() - started_at >= 30:
                    short_failures = 0
                    delay = 0.12
                else:
                    short_failures = min(short_failures + 1, 5)
                    delay = min(2 ** (short_failures - 1), 15)
                    log(f"uxplay exited early (rc={rc}); retrying in {delay}s (failure {short_failures})")
                shutdown_event.wait(delay)
    finally:
        if current_proc is not None:
            stop_proc(current_proc)
        set_audio_active(False)
        exit_mirror_turbo()
        set_webkit_paused(False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
