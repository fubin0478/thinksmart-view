#!/usr/bin/env python3
"""Build a standalone offline/online bare-metal installer bundle for ThinkSmart View (CD-18781Y).

Outputs are written ONLY to --out-dir (default: ../thinksmart-dist), NEVER into the shared
thinksmart-view Git repository, respecting the size and zero-secret rules.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import time


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
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
DEFAULT_OUT_DIR = pathlib.Path(os.environ.get("THINKSMART_OUT_DIR") or str(_LOCAL_DEV_CFG.get("out_dir") or "").strip() or str(REPO_ROOT.parent / "thinksmart-dist"))
DEFAULT_HOST = os.environ.get("THINKSMART_SSH_HOST") or str(_LOCAL_DEV_CFG.get("host") or "").strip() or "root@192.168.1.88"
DEFAULT_KEY = os.environ.get("THINKSMART_SSH_KEY") or str(_LOCAL_DEV_CFG.get("key") or "").strip() or str(pathlib.Path.home() / ".ssh" / "id_ed25519")

MIHOMO_EXAMPLE_YAML = """\
# ThinkSmart View Mihomo 脱敏模板（裸机装机请通过管控面板或 /admin/ 填入真实节点配置）
mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
external-controller: 127.0.0.1:9090
external-ui: /etc/mihomo/ui
"""

GUIDE_MD = """\
# ThinkSmart View (CD-18781Y) 裸机装机与统一后台备份恢复指南

本目录存放 **ThinkSmart View 裸机完整离线装机包** 与 **设备脱敏配置备份包**（均已执行全量脱敏，不含 API Key、密码或隧道真实 IP）。

## 一、交付物说明

1. `thinksmart-baremetal-installer-20261125.tar.gz`（含 `.sha256` 校验文件）：
   - 全量业务源码与前端界面（`xiaozhi-client`、`xiaozhi-hub`、`home`、`qqmusic`、`monitor`、`intercom`、`study`、`tutor`、`admin`、`kiosk`、`system`、`tests`、`vendor/lunar_python`）
   - 58 项底层硬件 / PipeWire / WirePlumber / 蓝牙 / systemd 单元与防爆音规则（`configs/`）
   - 运行时二进制与模型包（`runtime/`：`/opt/thinksmart-addons/venv`、`kws-zh` 中文唤醒模型、`uxplay` + `libuxplay_autorotate.so`、`s5kc505a_cam_helper`、`mihomo` 安装包及 `metacubexd` / GeoIP 数据）
   - 幂等装机脚本 `install.sh`（默认唤醒词 `你好小智`）
2. `thinksmart-config-backup-sanitized-20261125.tar.gz`（含 `.sha256` 校验文件）：
   - 经 `/usr/local/sbin/thinksmart-backup export --mode sanitized` 导出的全机脱敏配置快照（含闹钟、城市天气、倒数日、音乐偏好、亮度显示、小智结构配置与 `WAKE` 唤醒词配置，不含密钥与登录态）。

## 二、全新裸机（已刷入 `thinksmart-ubuntu-20261101.img`）一步装机

```bash
# 1. 在 PC 或设备上校验安装包 SHA256
sha256sum -c thinksmart-baremetal-installer-20261125.tar.gz.sha256

# 2. 将安装包传至设备（例如 /tmp/）并解压
mkdir -p /tmp/ts-installer
tar -xzf /tmp/thinksmart-baremetal-installer-20261125.tar.gz -C /tmp/ts-installer

# 3. 装前结构与脚本自检（只读）
bash /tmp/ts-installer/install.sh --check

# 4. 全量裸机安装（如已有备份包可同时加上 --restore-backup 一步恢复配置）
sudo bash /tmp/ts-installer/install.sh --apply
# 或：
# sudo bash /tmp/ts-installer/install.sh --apply --restore-backup /tmp/thinksmart-config-backup-full.tar.gz

# 5. 重启使底层 PipeWire / WirePlumber / G-Sensor / 自启服务全部生效
sudo reboot
```

> **提示**：如从全新原盘首次上线，重启后可登录统一管理后台 `http://<设备IP>:18091/admin/`（首次默认口令 `admin`/`admin`）或通过「从备份包恢复」导入完整迁移备份，再按需补充大模型 API Key、QQ 音乐扫码与 Mihomo 订阅。

## 三、统一后台整机一键备份 / 恢复

1. **网页端（推荐）**：打开 `http://<设备IP>:18091/admin/` →「系统与设备」卡片右上点「整机备份 / 恢复」（或滚动至 **「整机配置备份导出与恢复」** `#cfgBackupCard`）：
   - **导出脱敏配置包**：自动剥离 API Key、Token、Cookie、QQ 登录态与伴学密钥，适合留档或分享。
   - **导出完整迁移备份**：含 `sqlite3.backup()` 在线热备份的 `chat.db` 对话记录、QQ 登录态、伴学密封密钥及后台口令哈希，适合换机完整迁移。
   - **从备份包恢复**：自动先备份当前线上配置至 `/var/lib/thinksmart-admin/restore-backups/`，校验清单与 SHA256 后原子写回；导入脱敏包时默认勾选「保留本机现有 API Key 与登录态」。
2. **命令行端**：
   - 查看清单：`/usr/local/sbin/thinksmart-backup info --json`
   - 导出备份：`sudo /usr/local/sbin/thinksmart-backup export --mode full --out /tmp/full-backup.tar.gz`
   - 校验检查：`/usr/local/sbin/thinksmart-backup inspect /tmp/full-backup.tar.gz`
   - 恢复还原：`sudo /usr/local/sbin/thinksmart-backup restore /tmp/full-backup.tar.gz`
"""

EXCLUDE_DIR_NAMES = {".venv", "__pycache__", ".git", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".sync-conflict"}


def _ssh_bytes(host: str, key: str, remote_cmd: str, timeout: int = 300) -> bytes:
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no"]
    if key:
        cmd += ["-i", key]
    cmd += [host, remote_cmd]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"SSH failed ({proc.returncode}) for {remote_cmd}: {proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc.stdout


def _should_skip_source(rel_parts: tuple[str, ...], path: pathlib.Path) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in rel_parts):
        return True
    if any(part.startswith("test_") and False for part in rel_parts):
        return False
    if path.suffix in EXCLUDE_SUFFIXES or ".sync-conflict-" in path.name:
        return True
    # Protect live hub runtime/model dirs even if present locally
    posix = "/".join(rel_parts)
    if posix.startswith("xiaozhi-hub/main/xiaozhi-server/"):
        sub = posix[len("xiaozhi-hub/main/xiaozhi-server/"):]
        if sub.startswith(("models/", "music/", "tmp/")):
            return True
        if sub.startswith("data/") and sub != "data/.config.example.yaml":
            return True
    return False


def _add_tar_bytes(tf: tarfile.TarFile, arcname: str, data: bytes, mode: int, mtime: int, manifest: list[dict]) -> None:
    ti = tarfile.TarInfo(name=arcname)
    ti.size = len(data)
    ti.mtime = mtime
    ti.mode = mode
    ti.uid = 0
    ti.gid = 0
    tf.addfile(ti, io.BytesIO(data))
    manifest.append({
        "path": arcname,
        "size": len(data),
        "mode": oct(mode),
        "sha256": hashlib.sha256(data).hexdigest(),
    })


def build_package(
    out_dir: pathlib.Path, host: str, key: str, tag: str, include_hub_runtime: bool = True
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"thinksmart-baremetal-installer-{tag}.tar.gz"
    archive_path = out_dir / archive_name
    tmp_path = out_dir / f".{archive_name}.tmp"
    sha_path = out_dir / f"{archive_name}.sha256"
    guide_path = out_dir / "README-裸机装机与备份恢复指南.md"

    now_ts = int(time.time())
    manifest: list[dict] = []

    print("[1/4] Fetching runtime archives & hardware binaries from live device...")
    addons_venv_tgz = _ssh_bytes(
        host,
        key,
        "tar --exclude='__pycache__' --exclude='*.pyc' -czf - -C /opt/thinksmart-addons/venv .",
        timeout=300,
    )
    kws_zh_tgz = _ssh_bytes(
        host,
        key,
        "tar --exclude='__pycache__' -czf - -C /opt/thinksmart/client/1.0.0-self/models/kws-zh .",
        timeout=180,
    )
    hub_venv_tgz = None
    hub_models_tgz = None
    if include_hub_runtime:
        print("[1/4] Fetching xiaozhi-hub .venv & models/ for full offline bare-metal install...")
        hub_venv_tgz = _ssh_bytes(
            host,
            key,
            "GZIP=-1 tar --exclude='__pycache__' --exclude='*.pyc' -czf - -C /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv .",
            timeout=600,
        )
        hub_models_tgz = _ssh_bytes(
            host,
            key,
            "GZIP=-1 tar --exclude='__pycache__' --exclude='*.pyc' -czf - -C /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/models .",
            timeout=600,
        )
    hw_bins = {
        "runtime/hw-bins/uxplay": (_ssh_bytes(host, key, "cat /usr/local/bin/uxplay"), 0o755),
        "runtime/hw-bins/libuxplay_autorotate.so": (_ssh_bytes(host, key, "cat /usr/local/lib/libuxplay_autorotate.so"), 0o755),
        "runtime/hw-bins/s5kc505a_cam_helper": (_ssh_bytes(host, key, "cat /usr/local/bin/s5kc505a_cam_helper"), 0o755),
    }
    geosite_bytes = _ssh_bytes(host, key, "cat /etc/mihomo/geosite.dat")

    print("[2/4] Reading local mihomo-pkg assets & cleaned device source tree...")
    mihomo_dirs = [out_dir / "mihomo-pkg", REPO_ROOT / "dist" / "mihomo-pkg"]
    mihomo_assets: list[tuple[str, bytes, int]] = []
    have_deb = False
    have_ui = False
    have_mmdb = False
    for src_name, dst_arc in (
        ("mihomo-linux-arm64-v1.19.33.deb", "runtime/mihomo/mihomo-linux-arm64-v1.19.33.deb"),
        ("compressed-dist.tgz", "runtime/mihomo/compressed-dist.tgz"),
        ("country.mmdb", "runtime/mihomo/country.mmdb"),
    ):
        for mdir in mihomo_dirs:
            sp = mdir / src_name
            if sp.is_file():
                mihomo_assets.append((dst_arc, sp.read_bytes(), 0o644))
                if src_name.endswith(".deb"):
                    have_deb = True
                elif src_name == "compressed-dist.tgz":
                    have_ui = True
                elif src_name == "country.mmdb":
                    have_mmdb = True
                break
    if not have_deb:
        mihomo_assets.append(("runtime/mihomo/mihomo", _ssh_bytes(host, key, "cat /usr/bin/mihomo"), 0o755))
    if not have_ui:
        mihomo_assets.append(("runtime/mihomo/compressed-dist.tgz", _ssh_bytes(host, key, "tar -czf - -C /etc/mihomo/ui ."), 0o644))
    if not have_mmdb:
        mihomo_assets.append(("runtime/mihomo/country.mmdb", _ssh_bytes(host, key, "cat /etc/mihomo/country.mmdb"), 0o644))
    mihomo_assets.append(("runtime/mihomo/geosite.dat", geosite_bytes, 0o644))
    mihomo_assets.append(("runtime/mihomo/config.example.yaml", MIHOMO_EXAMPLE_YAML.encode("utf-8"), 0o600))

    print(f"[3/4] Writing standalone tarball {archive_path} ...")
    with tarfile.open(tmp_path, mode="w:gz", compresslevel=6) as tf:
        for p in sorted(DEV_DIR.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(DEV_DIR)
            if _should_skip_source(rel.parts, p):
                continue
            data = p.read_bytes()
            is_exec = (
                p.suffix == ".sh"
                or rel.parts[0] in ("system", "tests")
                or p.name in (
                    "thinksmart-kiosk.py", "thinksmart-kiosk-app", "thinksmart-kiosk-session",
                    "kiosk-mode", "idle-blanker.py",
                )
                or (len(rel.parts) == 2 and p.name.startswith("thinksmart-") and p.suffix == ".py")
            )
            mode = 0o755 if is_exec else 0o644
            _add_tar_bytes(tf, rel.as_posix(), data, mode, now_ts, manifest)

        _add_tar_bytes(tf, "runtime/thinksmart-addons-venv.tar.gz", addons_venv_tgz, 0o644, now_ts, manifest)
        _add_tar_bytes(tf, "runtime/kws-zh.tar.gz", kws_zh_tgz, 0o644, now_ts, manifest)
        if hub_venv_tgz is not None:
            _add_tar_bytes(tf, "runtime/xiaozhi-hub-venv.tar.gz", hub_venv_tgz, 0o644, now_ts, manifest)
        if hub_models_tgz is not None:
            _add_tar_bytes(tf, "runtime/xiaozhi-hub-models.tar.gz", hub_models_tgz, 0o644, now_ts, manifest)
        for arc, (data, mode) in hw_bins.items():
            _add_tar_bytes(tf, arc, data, mode, now_ts, manifest)
        for arc, data, mode in mihomo_assets:
            _add_tar_bytes(tf, arc, data, mode, now_ts, manifest)

        meta = {
            "package_name": "thinksmart-view-baremetal",
            "created_at": now_ts,
            "default_wake_word": "你好小智",
            "file_count": len(manifest),
            "files": manifest,
        }
        meta_bytes = (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ti = tarfile.TarInfo(name="PACKAGE_MANIFEST.json")
        ti.size = len(meta_bytes)
        ti.mtime = now_ts
        ti.mode = 0o644
        ti.uid = 0
        ti.gid = 0
        tf.addfile(ti, io.BytesIO(meta_bytes))

    os.replace(tmp_path, archive_path)
    pkg_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sha_path.write_text(f"{pkg_sha}  {archive_name}\n", encoding="utf-8")
    guide_path.write_text(GUIDE_MD, encoding="utf-8")
    print(f"[4/4] DONE: {archive_path} ({archive_path.stat().st_size / (1024 * 1024):.1f} MiB, files={len(manifest)}) sha256={pkg_sha}")
    return archive_path, sha_path, guide_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Build ThinkSmart View offline bare-metal installer package")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Target directory outside shared repo")
    ap.add_argument("--host", default=DEFAULT_HOST, help="Live device SSH target for runtime assets")
    ap.add_argument("--key", default=DEFAULT_KEY, help="SSH private key path")
    ap.add_argument("--tag", default="20261125", help="Package version tag")
    ap.add_argument("--skip-hub-runtime", action="store_true", help="Omit xiaozhi-hub .venv and models/ archives")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out_dir).resolve()
    if REPO_ROOT in out_dir.parents or out_dir == REPO_ROOT:
        raise SystemExit(f"Refusing to write large installer bundle inside shared repo: {out_dir}")
    build_package(out_dir, args.host, args.key, args.tag, include_hub_runtime=(not args.skip_hub_runtime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
