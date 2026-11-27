#!/usr/bin/env python3
"""Restart the kiosk browser after an Adreno GPU fault.

On some boots the a306 GPU hits "CP opcode error" during WebKit's initial page render, recovers, and
leaves the browser's GL context wedged (screen frozen on whatever frame was up). Weston itself keeps
working; only the client with the faulted context is stuck.

Watching kmsg for the recovery line and killing thinksmart-kiosk.py makes thinksmart-kiosk-app launch it
again and it loads the page afresh. A fault that didn't freeze anything costs a page reload.
Started before the kiosk session, so a fault right after boot is seen. Runs as root (kernel log, pkill).
"""
import errno
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

FAULT = re.compile(r'hangcheck recover|gpu fault|opcode error|preemption timed out')
SETTLE = 5        # seconds: let the GPU recover and the burst of fault messages end
MIN_GAP = 60      # seconds between restarts, so a fault loop can't keep the screen restarting
KIOSK = '^/usr/bin/python3( -B)? /usr/local/bin/thinksmart-kiosk.py'
OVERLAY_FLAGS = ('/run/user/1000/uxplay-mirroring-active', '/run/user/1000/intercom-call-active')


def log(msg):
    print(msg, flush=True)


def _kiosk_pids():
    """Return PIDs of running ubuntu thinksmart-kiosk.py processes via /proc."""
    pids = []
    try:
        entries = os.scandir('/proc')
    except OSError:
        return pids
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat(follow_symlinks=False).st_uid != 1000:
                    continue
                with open(f'/proc/{entry.name}/cmdline', 'rb') as f:
                    cmd = f.read(4096).replace(b'\x00', b' ').decode('utf-8', 'replace').strip()
                if re.search(KIOSK, cmd):
                    pids.append(int(entry.name))
            except OSError:
                continue
    return pids


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    log('watching the kernel log for GPU faults')
    kmsg = None
    sel = selectors.DefaultSelector()
    try:
        try:
            kfd = os.open('/dev/kmsg', os.O_RDONLY | os.O_NONBLOCK)
            try:
                os.lseek(kfd, 0, os.SEEK_END)
            except OSError:
                pass
            kf = os.fdopen(kfd, 'rb', buffering=0)
            kmsg = SimpleNamespace(stdout=kf, terminate=kf.close, wait=lambda *_a, **_k: 0, kill=kf.close)
        except OSError:
            kmsg = subprocess.Popen(['journalctl', '-k', '-f', '--since=-20s', '-o', 'cat'], stdout=subprocess.PIPE)
            os.set_blocking(kmsg.stdout.fileno(), False)
        sel.register(kmsg.stdout, selectors.EVENT_READ)
        _watch(kmsg, sel)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        sel.close()
        if kmsg is not None:
            kmsg.terminate()
            try:
                kmsg.wait(timeout=2)
            except subprocess.TimeoutExpired:
                kmsg.kill()
                try:
                    kmsg.wait(timeout=2)
                except Exception:
                    pass
            if kmsg.stdout:
                kmsg.stdout.close()


def _watch(kmsg, sel):
    last_restart = None
    fault_at = None
    deferred_logged = False
    pending = b''
    while True:
        timeout = None if fault_at is None else max(0, fault_at + SETTLE - time.monotonic())
        if sel.select(timeout):
            try:
                chunk = os.read(kmsg.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR, errno.EPIPE):
                    continue
                raise
            if not chunk:
                sys.exit('journalctl ended')
            lines = (pending + chunk).split(b'\n')
            pending = lines.pop()
            # A pipe read may end in the middle of a keyword or UTF-8 character.
            # Bound malformed/unterminated lines while keeping a keyword-sized overlap.
            if len(pending) > 65536:
                lines.append(pending)
                pending = pending[-128:]
            for raw in lines:
                line = raw.decode('utf-8', errors='replace')
                if FAULT.search(line):
                    log(f'GPU fault: {line.strip()[:160]}')
                    if fault_at is None:
                        fault_at = time.monotonic()
        # Busy kernel output must not starve a pending recovery deadline.
        if fault_at is None or time.monotonic() < fault_at + SETTLE:
            continue
        if any(os.path.exists(p) for p in OVERLAY_FLAGS):
            if not deferred_logged:
                log('overlay active (call/mirroring), deferring kiosk browser restart')
                deferred_logged = True
            fault_at = time.monotonic()
            continue
        deferred_logged = False
        fault_at = None
        if last_restart is not None and time.monotonic() - last_restart < MIN_GAP:
            log('restarted less than a minute ago, leaving it')
            continue
        old_pids = _kiosk_pids()
        # Resume any SIGSTOP'd WebKit/kiosk processes first so SIGTERM takes effect immediately and leaves no orphans
        try:
            subprocess.run(['pkill', '-CONT', '-u', 'ubuntu', '-f', r'(^|/)WebKit(Web|Network)Process( |$)'], stderr=subprocess.DEVNULL, timeout=5)
            subprocess.run(['pkill', '-CONT', '-u', 'ubuntu', '-f', KIOSK], stderr=subprocess.DEVNULL, timeout=5)
            r = subprocess.run(['pkill', '-u', 'ubuntu', '-f', KIOSK], timeout=5)
            if r.returncode == 0:
                last_restart = time.monotonic()
                if old_pids:
                    still_alive = old_pids
                    for _ in range(15):
                        cur_pids = set(_kiosk_pids())
                        still_alive = [p for p in old_pids if p in cur_pids]
                        if not still_alive:
                            break
                        time.sleep(0.1)
                    if still_alive:
                        subprocess.run(['kill', '-KILL', *[str(p) for p in still_alive]], stderr=subprocess.DEVNULL, timeout=3)
                        subprocess.run(['pkill', '-CONT', '-u', 'ubuntu', '-f', r'(^|/)WebKit(Web|Network)Process( |$)'], stderr=subprocess.DEVNULL, timeout=3)
                log('kiosk browser restarted')
            else:
                log('kiosk browser was not running')
        except subprocess.SubprocessError as e:
            log(f'kiosk restart command timed out/failed: {e}')


if __name__ == '__main__':
    main()
