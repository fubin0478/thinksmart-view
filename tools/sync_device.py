#!/usr/bin/env python3
"""ThinkSmart View 跨平台一键增量同步与自检工具（支持 Windows 与 macOS）。

用法示例：
  python tools/sync_device.py --diff                           # 只读对比共享主仓库与设备线上文件的差异
  python tools/sync_device.py --apply --verify                 # 原子备份并增量同步全部有变更模块、按需重启安全服务并跑整机自检
  python tools/sync_device.py --apply --modules admin,client   # 仅同步指定模块（支持 client,hub,admin,tutor,home,qqmusic,monitor,intercom,study,kiosk,system,tests）

红线保护（硬件与安全铁律）：
  - 严禁自动重启 thinksmart-mic、pipewire、pipewire-pulse、wireplumber（MSM8953 DSP 单次 prepare 限制）
  - 严禁自动重启 thinksmart-intercom-tunnel（保护公网加密隧道不断连）
  - 重启 client / hub 前强制校验大屏处于 idle 空闲态且无活跃对讲通话
  - 自动校验 ubuntu 目录零 root 文件污染、零 .pyc 残留与 xiaozhi-hub 本地 Git 工作树干净
"""
import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = REPO_ROOT / "device"


def _load_local_dev_cfg() -> dict:
    p = REPO_ROOT / ".thinksmart-dev.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


_LOCAL_DEV_CFG = _load_local_dev_cfg()
DEFAULT_HOST = os.environ.get("THINKSMART_SSH_HOST") or str(_LOCAL_DEV_CFG.get("host") or "").strip() or "root@192.168.1.88"
FORBIDDEN_RESTART_UNITS = frozenset({
    "thinksmart-mic", "thinksmart-mic.service",
    "pipewire", "pipewire.service",
    "pipewire-pulse", "pipewire-pulse.service",
    "wireplumber", "wireplumber.service",
    "thinksmart-intercom-tunnel", "thinksmart-intercom-tunnel.service",
})


def default_ssh_key() -> str:
    for raw in (os.environ.get("THINKSMART_SSH_KEY"), _LOCAL_DEV_CFG.get("key")):
        if raw and Path(str(raw)).expanduser().is_file():
            return str(Path(str(raw)).expanduser())
    ssh_dir = Path.home() / ".ssh"
    cands = [ssh_dir / "id_ed25519", ssh_dir / "id_rsa"] + (sorted(p for p in ssh_dir.iterdir() if p.is_file() and not p.name.endswith((".pub", "known_hosts", "known_hosts.old", "config"))) if ssh_dir.is_dir() else [])
    for cand in cands:
        if cand.is_file():
            return str(cand)
    return ""


def collect_module_files(selected_modules=None) -> list[dict]:
    """Return list of {'module', 'local_path', 'remote_path', 'owner', 'mode'} entries."""
    items = []

    def add_file(mod: str, lp: Path, rp: str, owner: str = "root", mode: int = 0o644):
        if selected_modules and mod not in selected_modules:
            return
        if lp.is_file():
            items.append({
                "module": mod,
                "local_path": lp,
                "remote_path": rp,
                "owner": owner,
                "mode": mode,
                "sha256": hashlib.sha256(lp.read_bytes()).hexdigest(),
            })

    def add_tree(mod: str, ldir: Path, rdir: str, owner: str = "root", skip_rel=()):
        if selected_modules and mod not in selected_modules:
            return
        skip_set = set(skip_rel)
        for p in sorted(ldir.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
                continue
            rel = p.relative_to(ldir).as_posix()
            if rel in skip_set or rel.endswith(".example.yaml") or rel.endswith(".example.json") or rel.endswith(".example"):
                continue
            mode = 0o755 if p.suffix == ".sh" else 0o644
            add_file(mod, p, f"{rdir}/{rel}", owner=owner, mode=mode)

    # 1. client
    add_tree("client", DEV_DIR / "xiaozhi-client", "/opt/thinksmart/client/1.0.0-self", owner="root")
    add_file("client", DEV_DIR / "xiaozhi-client/thinksmart_client/bridge.py", "/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py", "root", 0o644)

    # 2. hub (owned by ubuntu!)
    add_tree("hub", DEV_DIR / "xiaozhi-hub", "/home/ubuntu/xiaozhi-hub-server", owner="ubuntu")

    # 3. home
    add_file("home", DEV_DIR / "home/thinksmart-home.py", "/usr/local/bin/thinksmart-home.py", "root", 0o755)
    add_tree("home", DEV_DIR / "home/web", "/usr/local/share/thinksmart-home", "root")

    # 4. qqmusic
    add_file("qqmusic", DEV_DIR / "qqmusic/thinksmart-qqmusic.py", "/usr/local/bin/thinksmart-qqmusic.py", "root", 0o755)
    add_tree("qqmusic", DEV_DIR / "qqmusic/web", "/usr/local/share/thinksmart-qqmusic", "root")

    # 5. monitor
    add_file("monitor", DEV_DIR / "monitor/thinksmart-monitor.py", "/usr/local/bin/thinksmart-monitor.py", "root", 0o755)
    add_tree("monitor", DEV_DIR / "monitor/web", "/usr/local/share/thinksmart-monitor", "root")

    # 6. intercom (never overwrites live sanitized tunnel scripts or restarts tunnel!)
    add_file("intercom", DEV_DIR / "intercom/thinksmart-intercom.py", "/usr/local/bin/thinksmart-intercom.py", "root", 0o755)
    add_file("intercom", DEV_DIR / "intercom/thinksmart-intercom-ui.py", "/usr/local/bin/thinksmart-intercom-ui.py", "root", 0o755)
    add_tree("intercom", DEV_DIR / "intercom/web", "/usr/local/share/thinksmart-intercom", "root")

    # 7. study
    add_file("study", DEV_DIR / "study/thinksmart-study.py", "/usr/local/bin/thinksmart-study.py", "root", 0o755)
    add_tree("study", DEV_DIR / "study/web", "/usr/local/share/thinksmart-study", "root")

    # 8. tutor
    add_file("tutor", DEV_DIR / "tutor/thinksmart-tutor.py", "/usr/local/bin/thinksmart-tutor.py", "root", 0o755)
    add_tree("tutor", DEV_DIR / "tutor/web", "/usr/local/share/thinksmart-tutor", "root")

    # 9. admin
    add_file("admin", DEV_DIR / "admin/thinksmart-admin.py", "/usr/local/bin/thinksmart-admin.py", "root", 0o755)
    add_tree("admin", DEV_DIR / "admin/web", "/usr/local/share/thinksmart-admin", "root")

    # 10. kiosk
    for fn, mode in (
        ("thinksmart-kiosk.py", 0o755),
        ("thinksmart-kiosk-app", 0o755),
        ("thinksmart-kiosk-session", 0o755),
        ("kiosk-mode", 0o755),
        ("idle-blanker.py", 0o755),
        ("thinksmart_display.py", 0o644),
        ("diagnose-wake.sh", 0o755),
    ):
        add_file("kiosk", DEV_DIR / "kiosk" / fn, f"/usr/local/bin/{fn}", "root", mode)
    add_tree("kiosk", DEV_DIR / "kiosk/web", "/usr/local/share/thinksmart-kiosk", "root")

    # 11. system
    for p in sorted((DEV_DIR / "system/bin").glob("*")):
        add_file("system", p, f"/usr/local/bin/{p.name}", "root", 0o755)
    for p in sorted((DEV_DIR / "system/sbin").glob("*")):
        add_file("system", p, f"/usr/local/sbin/{p.name}", "root", 0o755)
    add_tree("system", DEV_DIR / "vendor/lunar_python", "/opt/thinksmart-addons/vendor/lunar_python", "root")

    # 12. configs (systemd units, PipeWire/WirePlumber, udev, PAM; never restarts audio/tunnel units)
    cfg_dir = DEV_DIR / "configs"
    for p in sorted(cfg_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(cfg_dir).as_posix()
        if rel.endswith((".example", ".example.conf", ".example.yaml", ".example.json")):
            continue
        if rel.startswith("etc/"):
            mode = 0o440 if rel.startswith("etc/sudoers.d/") else 0o644
            add_file("configs", p, f"/{rel}", "root", mode)
        elif rel.startswith("systemd-system/"):
            add_file("configs", p, f"/etc/systemd/system/{rel[len('systemd-system/'):]}", "root", 0o644)
        elif rel.startswith("systemd-user/"):
            add_file("configs", p, f"/home/ubuntu/.config/systemd/user/{rel[len('systemd-user/'):]}", "ubuntu", 0o644)
        elif rel.startswith("user-wireplumber/"):
            add_file("configs", p, f"/home/ubuntu/.config/wireplumber/main.lua.d/{rel[len('user-wireplumber/'):]}", "ubuntu", 0o644)
        elif rel.startswith("user-wireplumber-bt/"):
            add_file("configs", p, f"/home/ubuntu/.config/wireplumber/bluetooth.lua.d/{rel[len('user-wireplumber-bt/'):]}", "ubuntu", 0o644)
        elif rel.startswith("user-pipewire/"):
            add_file("configs", p, f"/home/ubuntu/.config/pipewire/{rel[len('user-pipewire/'):]}", "ubuntu", 0o644)

    # 13. tests
    for p in sorted((DEV_DIR / "tests").glob("test_*.py")):
        add_file("tests", p, f"/opt/thinksmart-addons/{p.name}", "root", 0o755)

    return items


def ssh_python(host: str, key: str, script: str, timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no"]
    if key:
        cmd += ["-i", key]
    cmd += [host, "python3", "-B", "-"]
    return subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)


def cmd_diff(host: str, key: str, items: list[dict], prune_mods=None) -> tuple[list[dict], list[dict]]:
    if prune_mods is None:
        prune_mods = {'client', 'hub', 'monitor'}
    req_map = {it["remote_path"]: it["sha256"] for it in items}
    client_keep = [it["remote_path"] for it in items if it["remote_path"].startswith("/opt/thinksmart/client/1.0.0-self/")]
    hub_keep = [it["remote_path"] for it in items if it["remote_path"].startswith("/home/ubuntu/xiaozhi-hub-server/")]
    monitor_keep = [it["remote_path"] for it in items if it["remote_path"].startswith("/usr/local/share/thinksmart-monitor/")]
    remote_sc = (
        "import hashlib, json, os\n"
        f"paths = {json.dumps(list(req_map.keys()))}\n"
        f"prune_client = {'client' in prune_mods}\n"
        f"prune_hub = {'hub' in prune_mods}\n"
        f"prune_monitor = {'monitor' in prune_mods}\n"
        f"client_keep = set({json.dumps(client_keep)})\n"
        f"hub_keep = set({json.dumps(hub_keep)})\n"
        f"monitor_keep = set({json.dumps(monitor_keep)})\n"
        "res = {}\n"
        "for p in paths:\n"
        "    if os.path.isfile(p) and not os.path.islink(p):\n"
        "        res[p] = hashlib.sha256(open(p, 'rb').read()).hexdigest()\n"
        "    else:\n"
        "        res[p] = None\n"
        "stale = []\n"
        "if prune_client:\n"
        "    c_root = '/opt/thinksmart/client/1.0.0-self'\n"
        "    if os.path.isdir(c_root):\n"
        "        for dp, dns, fns in os.walk(c_root):\n"
        "            dns[:] = [d for d in dns if d not in ('__pycache__', 'models', '.venv')]\n"
        "            for fn in fns:\n"
        "                if fn.endswith(('.pyc', '.pyo')):\n"
        "                    continue\n"
        "                fp = os.path.join(dp, fn).replace('\\\\', '/')\n"
        "                if fp not in client_keep and not os.path.islink(fp):\n"
        "                    stale.append({'module': 'client', 'remote_path': fp})\n"
        "if prune_hub:\n"
        "    for h_sub in ('/home/ubuntu/xiaozhi-hub-server/scripts', '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server'):\n"
        "        if os.path.isdir(h_sub):\n"
        "            for dp, dns, fns in os.walk(h_sub):\n"
        "                skip_dirs = {'__pycache__', '.git', '.venv', 'data', 'models', 'music', 'tmp'}\n"
        "                if dp.rstrip('/') == '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/config/assets':\n"
        "                    skip_dirs.add('wakeup_words')\n"
        "                dns[:] = [d for d in dns if d not in skip_dirs]\n"
        "                for fn in fns:\n"
        "                    if fn.endswith(('.pyc', '.pyo')):\n"
        "                        continue\n"
        "                    fp = os.path.join(dp, fn).replace('\\\\', '/')\n"
        "                    if fp not in hub_keep and not os.path.islink(fp):\n"
        "                        stale.append({'module': 'hub', 'remote_path': fp})\n"
        "if prune_monitor:\n"
        "    m_root = '/usr/local/share/thinksmart-monitor'\n"
        "    if os.path.isdir(m_root):\n"
        "        for dp, dns, fns in os.walk(m_root):\n"
        "            dns[:] = [d for d in dns if d != '__pycache__']\n"
        "            for fn in fns:\n"
        "                if fn in ('servers.json', 'traffic.json') or fn.endswith(('.pyc', '.pyo')):\n"
        "                    continue\n"
        "                fp = os.path.join(dp, fn).replace('\\\\', '/')\n"
        "                if fp not in monitor_keep and not os.path.islink(fp):\n"
        "                    stale.append({'module': 'monitor', 'remote_path': fp})\n"
        "print(json.dumps({'hashes': res, 'stale': sorted(stale, key=lambda x: x['remote_path'])}))\n"
    )
    r = ssh_python(host, key, remote_sc, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"SSH diff failed: {r.stderr}")
    payload = json.loads(r.stdout.strip())
    remote_hashes = payload["hashes"]
    diffs = []
    for it in items:
        rh = remote_hashes.get(it["remote_path"])
        if rh != it["sha256"]:
            diffs.append({**it, "status": "missing" if rh is None else "modified"})
    return diffs, payload.get("stale", [])


def cmd_apply(host: str, key: str, diffs: list[dict], stale: list[dict], do_verify: bool) -> None:
    if diffs:
        buf = io.BytesIO()
        manifest = []
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for idx, it in enumerate(diffs):
                arc = f"f_{idx}"
                tf.add(it["local_path"], arcname=arc, recursive=False)
                manifest.append({
                    "arc": arc,
                    "module": it["module"],
                    "remote_path": it["remote_path"],
                    "owner": it["owner"],
                    "mode": it["mode"],
                })
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    else:
        b64 = ""
        manifest = []

    remote_apply = f"""
import base64, hashlib, io, json, os, pwd, shutil, subprocess, sys, tarfile, time, urllib.request

MANIFEST = {json.dumps(manifest)}
STALE = {json.dumps(stale)}
TAR_B64 = '{b64}'
DO_VERIFY = {bool(do_verify)}

ubuntu_pw = pwd.getpwnam('ubuntu')
UID_U, GID_U = ubuntu_pw.pw_uid, ubuntu_pw.pw_gid

def run(cmd, check=True):
    r = subprocess.run(cmd, cwd='/tmp', capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f'Command failed: {{cmd}}\\n{{r.stderr}}\\n{{r.stdout}}')
    return r

def run_u(cmd, check=True):
    return run(['runuser', '-u', 'ubuntu', '--', 'env', 'PYTHONDONTWRITEBYTECODE=1',
                f'XDG_RUNTIME_DIR=/run/user/{{UID_U}}', f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{{UID_U}}/bus', *cmd], check=check)

touched_mods = sorted({{m['module'] for m in MANIFEST}} | {{s['module'] for s in STALE}})
need_kiosk_reload = any(
    m['remote_path'] == '/usr/local/share/thinksmart-monitor/home_overlay.js'
    or (m['module'] == 'kiosk' and m['remote_path'] != '/usr/local/bin/idle-blanker.py')
    for m in MANIFEST
)
need_blanker_restart = any(m['remote_path'] in ('/usr/local/bin/idle-blanker.py', '/usr/local/bin/thinksmart_display.py') for m in MANIFEST)
need_wake_restart = any(m['remote_path'] in ('/usr/local/bin/screen-wake-daemon.py', '/usr/local/bin/thinksmart_display.py') for m in MANIFEST)
need_prox_restart = any(m['remote_path'] == '/usr/local/bin/vcnl4200-proximity-daemon.py' for m in MANIFEST)
need_airplay_restart = any(m['remote_path'] in ('/usr/local/bin/thinksmart-airplay.py', '/usr/local/sbin/thinksmart-airplay-tune') for m in MANIFEST)
need_gpu_wd_restart = any(m['remote_path'] == '/usr/local/bin/gpu-watchdog.py' for m in MANIFEST)
need_usb_wd_restart = any(m['remote_path'] == '/usr/local/sbin/thinksmart-usb-watchdog' for m in MANIFEST)
need_systemd_reload = any(m['remote_path'].startswith(('/etc/systemd/system/', '/etc/systemd/user/', '/home/ubuntu/.config/systemd/user/')) for m in MANIFEST)
need_client_unit_restart = any(m['remote_path'] == '/etc/systemd/system/thinksmart-client.service' for m in MANIFEST)
need_intercom_restart = any(m['remote_path'] in ('/usr/local/bin/thinksmart-intercom.py', '/usr/local/bin/thinksmart-intercom-ui.py') or m['remote_path'].startswith('/usr/local/share/thinksmart-intercom/') for m in MANIFEST)
if any(m in touched_mods for m in ('client', 'hub', 'kiosk')) or need_kiosk_reload or need_client_unit_restart or need_intercom_restart:
    assert not os.path.exists(f'/run/user/{{UID_U}}/intercom-call-active'), 'Intercom call active!'
    try:
        with urllib.request.urlopen('http://127.0.0.1:8090/events', timeout=3) as resp:
            for _ in range(5):
                ln = resp.readline().decode('utf-8', 'replace')
                if ln.startswith('data:'):
                    ev = json.loads(ln[5:].strip())
                    st = (ev.get('status') or {{}}).get('state')
                    assert st in ('idle', 'sleeping'), f'Client busy ({{st}}), refusing restart!'
                    assert not (ev.get('alarms') or {{}}).get('ringing'), 'Alarm ringing, refusing restart!'
                    break
    except AssertionError:
        raise
    except Exception:
        pass

if MANIFEST or STALE:
    ts = time.strftime('%Y%m%d-%H%M%S')
    bk = f'/var/backups/thinksmart-sync-{{ts}}'
    os.makedirs(bk, mode=0o700, exist_ok=True)
    man_lines = []
    for item in (MANIFEST + STALE):
        rp = item['remote_path']
        if os.path.isfile(rp) and not os.path.islink(rp):
            dp = os.path.join(bk, 'files', rp.lstrip('/'))
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(rp, dp)
            h = hashlib.sha256(open(dp, 'rb').read()).hexdigest()
            man_lines.append(f'{{h}}  {{rp}}')
    with open(os.path.join(bk, 'MANIFEST.sha256'), 'w') as mf:
        mf.write('\\n'.join(man_lines) + '\\n')
    os.chmod(os.path.join(bk, 'MANIFEST.sha256'), 0o600)

    if MANIFEST:
        work = f'/tmp/ts_sync_{{os.getpid()}}'
        os.makedirs(work, mode=0o700, exist_ok=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(TAR_B64)), mode='r:gz') as tf:
                tf.extractall(work)
            for item in MANIFEST:
                sp = os.path.join(work, item['arc'])
                rp = item['remote_path']
                parent = os.path.dirname(rp)
                if not os.path.isdir(parent):
                    missing = []
                    cur = parent
                    while cur and cur != '/' and not os.path.isdir(cur):
                        missing.append(cur)
                        cur = os.path.dirname(cur)
                    os.makedirs(parent, mode=0o755, exist_ok=True)
                    if item['owner'] == 'ubuntu':
                        for d in missing:
                            os.chown(d, UID_U, GID_U)
                tmp_p = f'{{rp}}.sync.tmp'
                shutil.copyfile(sp, tmp_p)
                os.chmod(tmp_p, item['mode'])
                if item['owner'] == 'ubuntu':
                    os.chown(tmp_p, UID_U, GID_U)
                else:
                    os.chown(tmp_p, 0, 0)
                os.replace(tmp_p, rp)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    for s in STALE:
        rp = s['remote_path']
        if os.path.isfile(rp) and not os.path.islink(rp):
            os.unlink(rp)
        parent = os.path.dirname(rp)
        while parent not in (
            '/',
            '/opt/thinksmart/client/1.0.0-self',
            '/usr/local/share/thinksmart-monitor',
            '/home/ubuntu/xiaozhi-hub-server',
            '/home/ubuntu/xiaozhi-hub-server/scripts',
            '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server',
        ):
            try:
                for pyc_d in (os.path.join(parent, '__pycache__'),):
                    if os.path.isdir(pyc_d):
                        shutil.rmtree(pyc_d, ignore_errors=True)
                os.rmdir(parent)
                parent = os.path.dirname(parent)
            except OSError:
                break

    if 'hub' in touched_mods and os.path.isdir('/home/ubuntu/xiaozhi-hub-server/.git'):
        run_u(['git', '-C', '/home/ubuntu/xiaozhi-hub-server', 'add', '-A', 'main/xiaozhi-server', 'scripts'], check=False)
        if run_u(['git', '-C', '/home/ubuntu/xiaozhi-hub-server', 'status', '--porcelain', '--untracked-files=no'], check=False).stdout.strip():
            run_u(
                [
                    'git', '-C', '/home/ubuntu/xiaozhi-hub-server',
                    '-c', 'user.name=thinksmart-sync',
                    '-c', 'user.email=sync@localhost',
                    'commit', '-m', f'sync: update hub from unified thinksmart-view ({{ts}})',
                ],
                check=False,
            )

    if need_systemd_reload:
        if any(m['remote_path'].startswith('/etc/systemd/system/') for m in MANIFEST):
            run(['systemctl', 'daemon-reload'])
        if any(m['remote_path'].startswith(('/home/ubuntu/.config/systemd/user/', '/etc/systemd/user/')) for m in MANIFEST):
            run_u(['systemctl', '--user', 'daemon-reload'], check=False)

    # Selective safe service restarts (NEVER mic / pipewire / tunnel!)
    MOD_RESTARTS = {{
        'hub': ('user', 'xiaozhi-hub.service'),
        'client': ('sys', 'thinksmart-client.service'),
        'home': ('sys', 'thinksmart-home.service'),
        'qqmusic': ('sys', 'thinksmart-qqmusic.service'),
        'monitor': ('sys', 'thinksmart-monitor.service'),
        'study': ('sys', 'thinksmart-study.service'),
        'admin': ('sys', 'thinksmart-admin.service'),
        'tutor': ('sys', 'thinksmart-tutor.service'),
    }}
    for mod in touched_mods:
        spec = MOD_RESTARTS.get(mod)
        if not spec:
            continue
        kind, unit = spec
        if kind == 'user':
            run_u(['systemctl', '--user', 'restart', unit])
        elif kind == 'sys':
            run(['systemctl', 'restart', unit])
        elif kind == 'sys_if_active':
            if run(['systemctl', 'is-active', unit], check=False).stdout.strip() == 'active':
                run(['systemctl', 'restart', unit])
    if need_intercom_restart:
        run(['systemctl', 'restart', 'thinksmart-intercom.service'])
    if need_client_unit_restart and 'client' not in touched_mods:
        run(['systemctl', 'restart', 'thinksmart-client.service'])

    if need_kiosk_reload:
        if run(['systemctl', 'is-active', 'thinksmart-kiosk.service'], check=False).stdout.strip() == 'active':
            run(['systemctl', 'restart', 'thinksmart-kiosk.service'])
    if need_blanker_restart:
        if run_u(['systemctl', '--user', 'is-active', 'idle-blanker.service'], check=False).stdout.strip() == 'active':
            run_u(['systemctl', '--user', 'restart', 'idle-blanker.service'])
    if need_wake_restart:
        if run(['systemctl', 'is-active', 'screen-wake.service'], check=False).stdout.strip() == 'active':
            run(['systemctl', 'restart', 'screen-wake.service'])
    if need_prox_restart:
        if run_u(['systemctl', '--user', 'is-active', 'vcnl4200-proximity-daemon.service'], check=False).stdout.strip() == 'active':
            run_u(['systemctl', '--user', 'restart', 'vcnl4200-proximity-daemon.service'])
    if need_airplay_restart:
        if (not os.path.exists(f'/run/user/{{UID_U}}/uxplay-mirroring-active')
                and not os.path.exists(f'/run/user/{{UID_U}}/uxplay-audio-active')
                and run_u(['systemctl', '--user', 'is-active', 'thinksmart-airplay.service'], check=False).stdout.strip() == 'active'):
            run_u(['systemctl', '--user', 'restart', 'thinksmart-airplay.service'])
    if need_gpu_wd_restart:
        if run(['systemctl', 'is-active', 'thinksmart-gpu-watchdog.service'], check=False).stdout.strip() == 'active':
            run(['systemctl', 'restart', 'thinksmart-gpu-watchdog.service'])
    if need_usb_wd_restart:
        if run(['systemctl', 'is-active', 'thinksmart-usb-watchdog.service'], check=False).stdout.strip() == 'active':
            run(['systemctl', 'restart', 'thinksmart-usb-watchdog.service'])

    print(f'SYNCED {{len(MANIFEST)}} updated + {{len(STALE)}} pruned files across modules {{touched_mods}} (backup: {{bk}})')
else:
    print('NO_CHANGES_TO_APPLY')

if DO_VERIFY:
    if 'hub' in touched_mods or 'client' in touched_mods:
        for _ in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8090/api/health', timeout=2) as r:
                    hj = json.loads(r.read().decode())
                    if hj.get('audio') and hj.get('hub'):
                        break
            except Exception:
                pass
            time.sleep(1.0)
    diag = run(['/usr/local/bin/diagnose-wake.sh'])
    assert '0 个错误，0 个警告' in diag.stdout, diag.stdout
    if os.path.isdir('/home/ubuntu/xiaozhi-hub-server/.git'):
        dirty = run_u(['git', '-C', '/home/ubuntu/xiaozhi-hub-server', 'status', '--porcelain', '--untracked-files=no']).stdout.strip()
        assert dirty == '', f'Hub git dirty: {{dirty}}'
        print('VERIFY_OK: diagnose-wake 0 errors / 0 warnings, hub git clean!')
    else:
        print('VERIFY_OK: diagnose-wake 0 errors / 0 warnings (bare-metal mode, no hub .git)!')
"""
    r = ssh_python(host, key, remote_apply, timeout=180)
    sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    if r.returncode != 0:
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(description="ThinkSmart View incremental sync & verification tool")
    ap.add_argument("--host", default=DEFAULT_HOST, help="SSH host (default: THINKSMART_SSH_HOST or root@192.168.1.88)")
    ap.add_argument("--key", default=default_ssh_key(), help="SSH private key path")
    ap.add_argument("--modules", default="", help="Comma-separated modules (default: all)")
    ap.add_argument("--diff", action="store_true", help="Show file differences between repo and device")
    ap.add_argument("--apply", action="store_true", help="Backup and sync changed files to device")
    ap.add_argument("--verify", action="store_true", help="Run diagnose-wake.sh and hygiene checks")
    args = ap.parse_args()

    mods = {m.strip() for m in args.modules.split(",") if m.strip()} or None
    items = collect_module_files(mods)
    diffs, stale = cmd_diff(args.host, args.key, items, prune_mods=(mods if mods is not None else {"client", "hub", "monitor"}))

    if args.diff or not args.apply:
        print(f"Checked {len(items)} tracked files against {args.host}: {len(diffs)} modified/missing, {len(stale)} obsolete remote file(s).")
        for d in diffs:
            print(f"  [{d['status']:8s}] ({d['module']}) {d['remote_path']}")
        for s in stale:
            print(f"  [obsolete] ({s['module']}) {s['remote_path']}")
        if not args.apply and not args.verify:
            return

    if args.apply or args.verify:
        cmd_apply(args.host, args.key, diffs if args.apply else [], stale if args.apply else [], args.verify)


if __name__ == "__main__":
    main()
