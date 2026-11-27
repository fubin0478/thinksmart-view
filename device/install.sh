#!/usr/bin/env bash
# ThinkSmart View (CD-18781Y) 自主维护一体化安装包部署脚本
# 支持：
#   bash device/install.sh --check                           # 只读检查安装包完整性与语法（非破坏性）
#   sudo bash device/install.sh [--apply]                    # 幂等安装源码、运行时二进制/模型（如存在）、系统配置与状态目录
#   sudo bash device/install.sh --apply --restore-backup <f> # 安装后立即按备份包校验恢复用户配置
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="apply"
RESTORE_ARCHIVE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check)
      MODE="check"
      shift
      ;;
    --apply)
      MODE="apply"
      shift
      ;;
    --restore-backup)
      [ "$#" -ge 2 ] || { echo "缺少备份包路径参数: --restore-backup <file.tar.gz>" >&2; exit 2; }
      RESTORE_ARCHIVE="$2"
      shift 2
      ;;
    -h|--help)
      echo "用法: $0 [--check | --apply [--restore-backup <archive.tar.gz>]]"
      exit 0
      ;;
    *)
      echo "未知参数: $1 (用法: $0 [--check|--apply] [--restore-backup <file.tar.gz>])" >&2
      exit 2
      ;;
  esac
done

REQUIRED_DIRS=(
  "xiaozhi-client/thinksmart_client"
  "xiaozhi-client/web/v2"
  "xiaozhi-hub/main/xiaozhi-server"
  "home/web"
  "qqmusic/web"
  "monitor/web"
  "intercom/web"
  "study/web"
  "tutor/web"
  "admin/web"
  "kiosk/web"
  "system/bin"
  "system/sbin"
  "configs/systemd-system"
  "configs/systemd-user"
  "vendor/lunar_python"
)

for d in "${REQUIRED_DIRS[@]}"; do
  if [ ! -d "$SCRIPT_DIR/$d" ]; then
    echo "[ERROR] 缺少安装包目录: $SCRIPT_DIR/$d" >&2
    exit 1
  fi
done

if [ "$MODE" = "check" ]; then
  PY_BIN=""
  if [ -x /opt/thinksmart-addons/venv/bin/python ]; then
    PY_BIN="/opt/thinksmart-addons/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
  fi
  if [ -n "$PY_BIN" ]; then
    PYTHONDONTWRITEBYTECODE=1 "$PY_BIN" -B - "$SCRIPT_DIR" <<'PY'
import ast, pathlib, tarfile, sys
root = pathlib.Path(sys.argv[1])
py_files = [
    p for p in sorted(root.rglob("*.py"))
    if ".venv" not in p.parts and "__pycache__" not in p.parts
] + [root / "system/sbin/thinksmart-backup", root / "system/sbin/thinksmart-usb-watchdog"]
checked = 0
for p in py_files:
    if p.is_file():
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        checked += 1
rt = root / "runtime"
rt_count = 0
if rt.is_dir():
    for arc_name in ("thinksmart-addons-venv.tar.gz", "kws-zh.tar.gz", "xiaozhi-hub-venv.tar.gz", "xiaozhi-hub-models.tar.gz"):
        ap = rt / arc_name
        if ap.is_file():
            with tarfile.open(ap, "r:gz") as tf:
                assert len(tf.getmembers()) > 0, ap
            rt_count += 1
print(f"CHECK_OK: directories=16 py_ast_ok={checked} runtime_archives={rt_count}")
PY
  fi
  for sh in "$SCRIPT_DIR"/install.sh "$SCRIPT_DIR"/kiosk/diagnose-wake.sh "$SCRIPT_DIR"/intercom/thinksmart-intercom-tunnel*.sh "$SCRIPT_DIR"/system/bin/*.sh "$SCRIPT_DIR"/system/sbin/*.sh; do
    [ -f "$sh" ] && bash -n "$sh"
  done
  echo "[PASS] ThinkSmart View 安装包结构与脚本语法检查通过 ($SCRIPT_DIR)"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo bash $0 安装" >&2
  exit 1
fi

if ! id ubuntu >/dev/null 2>&1; then
  echo "缺少系统用户 ubuntu" >&2
  exit 1
fi

if [ -n "$RESTORE_ARCHIVE" ] && [ ! -f "$RESTORE_ARCHIVE" ]; then
  echo "[ERROR] 找不到备份包: $RESTORE_ARCHIVE" >&2
  exit 1
fi

install -d -m 0755 \
  /opt/thinksmart/client/1.0.0-self \
  /usr/local/lib/thinksmart-aec-bridge \
  /usr/local/share/thinksmart-home \
  /usr/local/share/thinksmart-qqmusic \
  /usr/local/share/thinksmart-monitor \
  /usr/local/share/thinksmart-intercom \
  /usr/local/share/thinksmart-study \
  /usr/local/share/thinksmart-tutor \
  /usr/local/share/thinksmart-admin \
  /usr/local/share/thinksmart-kiosk \
  /opt/thinksmart-addons/vendor/lunar_python

# 0. 可选离线运行时层（仅在完整裸机安装包含 runtime/ 时执行）
RUNTIME_DIR="$SCRIPT_DIR/runtime"
if [ -d "$RUNTIME_DIR" ]; then
  MISSING_APT=()
  for pkg in python3-dbus python3-gi uxplay gstreamer1.0-gl gstreamer1.0-plugins-bad python3-qrcode qrencode; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      MISSING_APT+=("$pkg")
    fi
  done
  if [ "${#MISSING_APT[@]}" -gt 0 ]; then
    echo "[INFO] 检测到缺失系统依赖包: ${MISSING_APT[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${MISSING_APT[@]}" || \
      echo "[WARN] apt 依赖安装未完成，请检查网络连接后补装: ${MISSING_APT[*]}" >&2
  fi

  if [ -d "$RUNTIME_DIR/mihomo" ]; then
    if [ ! -x /usr/bin/mihomo ]; then
      for deb in "$RUNTIME_DIR"/mihomo/mihomo-linux-arm64-*.deb; do
        [ -f "$deb" ] && dpkg -i "$deb" && break
      done
      if [ ! -x /usr/bin/mihomo ] && [ -f "$RUNTIME_DIR/mihomo/mihomo" ]; then
        install -m 0755 "$RUNTIME_DIR/mihomo/mihomo" /usr/bin/mihomo
      fi
    fi
    install -d -m 0700 /etc/mihomo
    install -d -m 0755 /etc/mihomo/ui
    if [ -f "$RUNTIME_DIR/mihomo/compressed-dist.tgz" ]; then
      tar --no-same-owner -xzf "$RUNTIME_DIR/mihomo/compressed-dist.tgz" -C /etc/mihomo/ui
      chown -R root:root /etc/mihomo/ui
      chmod -R a+rX /etc/mihomo/ui
    fi
    [ -f "$RUNTIME_DIR/mihomo/country.mmdb" ] && install -m 0644 "$RUNTIME_DIR/mihomo/country.mmdb" /etc/mihomo/country.mmdb
    [ -f "$RUNTIME_DIR/mihomo/geosite.dat" ] && install -m 0644 "$RUNTIME_DIR/mihomo/geosite.dat" /etc/mihomo/geosite.dat
    [ -f /etc/mihomo/country.mmdb ] && ln -sfn /etc/mihomo/country.mmdb /etc/mihomo/Country.mmdb
    [ -f /etc/mihomo/geosite.dat ] && ln -sfn /etc/mihomo/geosite.dat /etc/mihomo/GeoSite.dat
    if [ ! -f /etc/mihomo/config.yaml ] && [ -f "$RUNTIME_DIR/mihomo/config.example.yaml" ]; then
      install -m 0600 "$RUNTIME_DIR/mihomo/config.example.yaml" /etc/mihomo/config.yaml
    fi
  fi

  if [ -f "$RUNTIME_DIR/thinksmart-addons-venv.tar.gz" ] && [ ! -x /opt/thinksmart-addons/venv/bin/python ]; then
    install -d -m 0755 /opt/thinksmart-addons/venv
    tar --no-same-owner -xzf "$RUNTIME_DIR/thinksmart-addons-venv.tar.gz" -C /opt/thinksmart-addons/venv
    chown -R root:root /opt/thinksmart-addons/venv
  fi

  if [ -f "$RUNTIME_DIR/kws-zh.tar.gz" ] && [ ! -f /opt/thinksmart/client/1.0.0-self/models/kws-zh/encoder.onnx ]; then
    install -d -m 0755 /opt/thinksmart/client/1.0.0-self/models/kws-zh
    tar --no-same-owner -xzf "$RUNTIME_DIR/kws-zh.tar.gz" -C /opt/thinksmart/client/1.0.0-self/models/kws-zh
  fi

  if [ -f "$RUNTIME_DIR/xiaozhi-hub-venv.tar.gz" ] && [ ! -x /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python ]; then
    install -d -o ubuntu -g ubuntu -m 0755 /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv
    tar --no-same-owner -xzf "$RUNTIME_DIR/xiaozhi-hub-venv.tar.gz" -C /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv
    chown -R ubuntu:ubuntu /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv
  fi

  if [ -f "$RUNTIME_DIR/xiaozhi-hub-models.tar.gz" ] && [ ! -d /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/models/snakers4_silero-vad ]; then
    install -d -o ubuntu -g ubuntu -m 0755 /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/models
    tar --no-same-owner -xzf "$RUNTIME_DIR/xiaozhi-hub-models.tar.gz" -C /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/models
    chown -R ubuntu:ubuntu /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/models
  fi

  if [ -d "$RUNTIME_DIR/hw-bins" ]; then
    [ -f "$RUNTIME_DIR/hw-bins/uxplay" ] && install -m 0755 "$RUNTIME_DIR/hw-bins/uxplay" /usr/local/bin/uxplay
    if [ -f "$RUNTIME_DIR/hw-bins/libuxplay_autorotate.so" ]; then
      install -m 0755 "$RUNTIME_DIR/hw-bins/libuxplay_autorotate.so" /usr/local/lib/libuxplay_autorotate.so
      ldconfig || true
    fi
    [ -f "$RUNTIME_DIR/hw-bins/s5kc505a_cam_helper" ] && install -m 0755 "$RUNTIME_DIR/hw-bins/s5kc505a_cam_helper" /usr/local/bin/s5kc505a_cam_helper
  fi
else
  # 在线源码克隆安装模式（无离线 runtime/ 目录时自动补齐 apt 依赖包）
  ONLINE_APT=(
    python3-venv python3-pip python3-aiohttp python3-numpy python3-gi python3-dbus
    python3-smbus2 python3-yaml python3-ruamel.yaml python3-pil python3-qrcode
    v4l-utils qrencode i2c-tools wireplumber libspa-0.2-bluetooth gir1.2-webkit2-4.1
    weston ffmpeg pulseaudio-utils libfdk-aac2 avahi-daemon uxplay
    gstreamer1.0-gl gstreamer1.0-plugins-bad gstreamer1.0-plugins-good
    gstreamer1.0-libav gstreamer1.0-alsa exfatprogs ntfs-3g
  )
  MISSING_ONLINE=()
  for pkg in "${ONLINE_APT[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      MISSING_ONLINE+=("$pkg")
    fi
  done
  if [ "${#MISSING_ONLINE[@]}" -gt 0 ]; then
    echo "[INFO] 正在自动补齐缺失的系统依赖包: ${MISSING_ONLINE[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${MISSING_ONLINE[@]}" || \
      echo "[WARN] 部分 apt 依赖包安装未完成，请检查网络连接后重试: ${MISSING_ONLINE[*]}" >&2
  fi
fi

# 硬件权限组、I2C 模块、独立 Python 虚拟环境兜底与 ARM64 预编译硬件增强层（离线包与在线 git clone 通用）
for grp in audio video input plugdev netdev i2c bluetooth; do
  if getent group "$grp" >/dev/null 2>&1; then
    usermod -aG "$grp" ubuntu 2>/dev/null || true
  fi
done
modprobe i2c-dev 2>/dev/null || true
install -d -m 0755 /etc/modules-load.d
if [ ! -f /etc/modules-load.d/thinksmart-i2c.conf ]; then
  printf 'i2c-dev\n' > /etc/modules-load.d/thinksmart-i2c.conf
  chmod 0644 /etc/modules-load.d/thinksmart-i2c.conf
fi

if [ ! -x /opt/thinksmart-addons/venv/bin/python ]; then
  install -d -m 0755 /opt/thinksmart-addons
  python3 -m venv --system-site-packages /opt/thinksmart-addons/venv
elif grep -q '^include-system-site-packages = false' /opt/thinksmart-addons/venv/pyvenv.cfg 2>/dev/null; then
  sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' /opt/thinksmart-addons/venv/pyvenv.cfg
fi

PREBUILT_DIR="$SCRIPT_DIR/prebuilt/arm64"
if [ "$(uname -m)" = "aarch64" ] && [ -f "$PREBUILT_DIR/SHA256SUMS" ]; then
  if (cd "$PREBUILT_DIR" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then
    install -D -m 0755 "$PREBUILT_DIR/s5kc505a_cam_helper" /usr/local/bin/s5kc505a_cam_helper
    install -D -m 0755 "$PREBUILT_DIR/uxplay" /usr/local/bin/uxplay
    install -D -m 0755 "$PREBUILT_DIR/libuxplay_autorotate.so" /usr/local/lib/libuxplay_autorotate.so
    if [ -d /usr/lib/aarch64-linux-gnu/spa-0.2/bluez5 ]; then
      install -D -m 0644 "$PREBUILT_DIR/libspa-codec-bluez5-aac.so" \
        /usr/lib/aarch64-linux-gnu/spa-0.2/bluez5/libspa-codec-bluez5-aac.so
    fi
    ldconfig 2>/dev/null || true
    BOOT_DTB="/boot/apq8053-lenovo-cd-18781y.dtb"
    STOCK_DTB_SHA="0136683ca0c096cca2287ce4afd4f3a899290c7b902ec884fbbcf882038792f7"
    CAM_DTB_SHA="72ae09417249b3a8995a82067123a51ad5dc5ca1cdbfb8f88124edf3bfb17906"
    if [ -f "$BOOT_DTB" ]; then
      CUR_DTB_SHA="$(sha256sum "$BOOT_DTB" | awk '{print $1}')"
      if [ "$CUR_DTB_SHA" = "$STOCK_DTB_SHA" ] || [ "${FORCE_CAMERA_DTB:-0}" = "1" ]; then
        [ -f "${BOOT_DTB}.bak" ] || cp -p "$BOOT_DTB" "${BOOT_DTB}.bak"
        install -m 0644 "$PREBUILT_DIR/apq8053-lenovo-cd-18781y.dtb" "$BOOT_DTB"
        echo "[DTB] 已自动备份 ${BOOT_DTB}.bak 并写入前置摄像头 S5KC505A 设备树补丁（重启后 /dev/video0 生效）。"
      elif [ "$CUR_DTB_SHA" != "$CAM_DTB_SHA" ]; then
        echo "[DTB] 当前 $BOOT_DTB 哈希非 20261101 原厂底包，未自动覆盖；可参考 $PREBUILT_DIR/apq8053-camera.dts.patch。"
      fi
    fi
  fi
fi

# 确保 mihomo 凭证文件始终存在，防止 thinksmart-admin.service 因 LoadCredential 报 243/CREDENTIALS
install -d -m 0700 /etc/mihomo
install -d -m 0755 /etc/mihomo/ui
if [ ! -f /etc/mihomo/config.yaml ]; then
  printf 'mixed-port: 7890\nmode: rule\n' > /etc/mihomo/config.yaml
  chmod 0600 /etc/mihomo/config.yaml
fi

# 1. 小智客户端与原生桥接（并兼容从基础镜像迁移 kws-zh 模型与 .venv）
cp -a "$SCRIPT_DIR"/xiaozhi-client/. /opt/thinksmart/client/1.0.0-self/
ln -sfn 1.0.0-self /opt/thinksmart/client/current
if [ ! -f /opt/thinksmart/client/1.0.0-self/models/kws-zh/encoder.onnx ]; then
  for old_kws in /opt/thinksmart/client/*/models/kws-zh; do
    if [ -f "$old_kws/encoder.onnx" ]; then
      install -d -m 0755 /opt/thinksmart/client/1.0.0-self/models
      cp -a "$old_kws" /opt/thinksmart/client/1.0.0-self/models/
      break
    fi
  done
fi
if [ ! -e /opt/thinksmart/client/1.0.0-self/.venv ] && [ -d /opt/thinksmart/envs ]; then
  for env_d in /opt/thinksmart/envs/*; do
    if [ -x "$env_d/bin/python" ]; then
      ln -sfn "../../envs/$(basename "$env_d")" /opt/thinksmart/client/1.0.0-self/.venv
      break
    fi
  done
fi
install -m 0644 "$SCRIPT_DIR/xiaozhi-client/thinksmart_client/bridge.py" /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py

# 2. 小智网关（保护 data/.config.yaml 与已有数据不覆盖）
install -d -o ubuntu -g ubuntu -m 0755 \
  /home/ubuntu/xiaozhi-hub-server \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server
cp -a "$SCRIPT_DIR"/xiaozhi-hub/scripts /home/ubuntu/xiaozhi-hub-server/
find "$SCRIPT_DIR/xiaozhi-hub/main/xiaozhi-server" -mindepth 1 -maxdepth 1 \
  ! -name data ! -name models ! -name music ! -name tmp ! -name .venv \
  -exec cp -a {} /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/ \;
rm -rf \
  /home/ubuntu/xiaozhi-hub-server/scripts/update.sh \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/providers/llm/claude_cli \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/agent/claude_session.py \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/agent/agent_session.py \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/api/ota_handler.py \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/api/vision_handler.py \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/api/base_handler.py \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/providers/vllm \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/core/admin/auth.py \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/config/assets/wakeup_words.wav \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/config/assets/max_output_size.wav
install -d -o ubuntu -g ubuntu -m 0700 /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data
install -o ubuntu -g ubuntu -m 0644 \
  "$SCRIPT_DIR/xiaozhi-hub/main/xiaozhi-server/data/.config.example.yaml" \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.config.example.yaml
if [ ! -f /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.config.yaml ]; then
  install -o ubuntu -g ubuntu -m 0600 \
    "$SCRIPT_DIR/xiaozhi-hub/main/xiaozhi-server/data/.config.example.yaml" \
    /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.config.yaml
  _rand_hex="$(tr -d '-' < /proc/sys/kernel/random/uuid 2>/dev/null || od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
  [ -n "$_rand_hex" ] && sed -i "s/REPLACE_WITH_RANDOM_32_CHAR_SECRET/${_rand_hex}/g" /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.config.yaml
fi
if [ ! -f /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.mcp_server_settings.json ]; then
  install -o ubuntu -g ubuntu -m 0600 \
    "$SCRIPT_DIR/xiaozhi-hub/main/xiaozhi-server/mcp_server_settings.json" \
    /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.mcp_server_settings.json
fi
find /home/ubuntu/xiaozhi-hub-server/scripts /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server -maxdepth 6 \( -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' \) -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null || true
chown -R ubuntu:ubuntu /home/ubuntu/xiaozhi-hub-server
if [ ! -x /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/python ] && [ -f /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/requirements.txt ]; then
  echo "[INFO] 正在构建 xiaozhi-hub 虚拟环境..."
  if [ -f /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/requirements-hub-core.txt ]; then
    su -s /bin/bash ubuntu -c 'python3 -m venv /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv && /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/pip install --no-cache-dir -c /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/requirements.txt -r /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/requirements-hub-core.txt' || \
      echo "[WARN] xiaozhi-hub 虚拟环境联网构建未完成，请检查网络后补装" >&2
  else
    su -s /bin/bash ubuntu -c 'python3 -m venv /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv && /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/pip install --no-cache-dir -r /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/requirements.txt' || \
      echo "[WARN] xiaozhi-hub 虚拟环境联网构建未完成，请检查网络后补装" >&2
  fi
fi

# 3. 自主核心服务与前端资源
for svc in home qqmusic monitor study tutor admin; do
  install -m 0755 "$SCRIPT_DIR/$svc/thinksmart-$svc.py" "/usr/local/bin/thinksmart-$svc.py"
  cp -a "$SCRIPT_DIR/$svc/web/." "/usr/local/share/thinksmart-$svc/"
done
[ ! -f /usr/local/share/thinksmart-monitor/servers.json ] && printf '[]\n' > /usr/local/share/thinksmart-monitor/servers.json && chmod 0644 /usr/local/share/thinksmart-monitor/servers.json
rm -f /usr/local/share/thinksmart-monitor/*.example.json /usr/local/share/thinksmart-monitor/display.html
install -m 0755 "$SCRIPT_DIR/intercom/thinksmart-intercom.py" /usr/local/bin/thinksmart-intercom.py
install -m 0755 "$SCRIPT_DIR/intercom/thinksmart-intercom-ui.py" /usr/local/bin/thinksmart-intercom-ui.py
if [ ! -f /usr/local/bin/thinksmart-intercom-tunnel.sh ]; then
  install -m 0755 "$SCRIPT_DIR/intercom/thinksmart-intercom-tunnel.sh" /usr/local/bin/thinksmart-intercom-tunnel.sh
fi
if [ ! -f /usr/local/bin/thinksmart-intercom-tunnel-jp.sh ]; then
  install -m 0755 "$SCRIPT_DIR/intercom/thinksmart-intercom-tunnel-jp.sh" /usr/local/bin/thinksmart-intercom-tunnel-jp.sh
fi
cp -a "$SCRIPT_DIR/intercom/web/." /usr/local/share/thinksmart-intercom/

for fn in thinksmart-kiosk.py thinksmart-kiosk-app thinksmart-kiosk-session kiosk-mode idle-blanker.py diagnose-wake.sh; do
  install -m 0755 "$SCRIPT_DIR/kiosk/$fn" "/usr/local/bin/$fn"
done
install -m 0644 "$SCRIPT_DIR/kiosk/thinksmart_display.py" /usr/local/bin/thinksmart_display.py
cp -a "$SCRIPT_DIR/kiosk/web/." /usr/local/share/thinksmart-kiosk/

for f in "$SCRIPT_DIR"/system/bin/*; do
  [ -f "$f" ] && install -m 0755 "$f" "/usr/local/bin/$(basename "$f")"
done
for f in "$SCRIPT_DIR"/system/sbin/*; do
  [ -f "$f" ] && install -m 0755 "$f" "/usr/local/sbin/$(basename "$f")"
done
cp -a "$SCRIPT_DIR/vendor/lunar_python/." /opt/thinksmart-addons/vendor/lunar_python/
[ -d "$SCRIPT_DIR/tests" ] && cp -a "$SCRIPT_DIR"/tests/test_*.py /opt/thinksmart-addons/
find /opt/thinksmart/client/1.0.0-self /opt/thinksmart-addons/vendor/lunar_python -maxdepth 6 \( -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' \) -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null || true
chown -R root:root \
  /opt/thinksmart/client/1.0.0-self \
  /opt/thinksmart-addons/vendor \
  /usr/local/share/thinksmart-{home,qqmusic,monitor,study,tutor,admin,intercom,kiosk}
compgen -G "/opt/thinksmart-addons/test_*.py" >/dev/null && chown root:root /opt/thinksmart-addons/test_*.py || true

# 4. 系统配置、音频规则、udev 与 systemd 单元
while IFS= read -r -d '' f; do
  rel="${f#"$SCRIPT_DIR/configs/etc/"}"
  [[ "$rel" == *.example* ]] && continue
  if [[ "$rel" == sudoers.d/* ]]; then
    install -D -m 0440 "$f" "/etc/$rel"
  else
    install -D -m 0644 "$f" "/etc/$rel"
  fi
done < <(find "$SCRIPT_DIR/configs/etc" -type f -print0)

while IFS= read -r -d '' f; do
  rel="${f#"$SCRIPT_DIR/configs/systemd-system/"}"
  [[ "$rel" == *.example* ]] && continue
  install -D -m 0644 "$f" "/etc/systemd/system/$rel"
done < <(find "$SCRIPT_DIR/configs/systemd-system" -type f -print0)
chmod 0700 /etc/systemd/system/mihomo.service.d 2>/dev/null || true

install -d -o ubuntu -g ubuntu -m 0755 \
  /home/ubuntu/.config \
  /home/ubuntu/.local \
  /home/ubuntu/.local/share \
  /home/ubuntu/.config/systemd/user/default.target.wants \
  /home/ubuntu/.config/systemd/user/pipewire-pulse.service.wants \
  /home/ubuntu/.config/systemd/user/wireplumber.service.wants \
  /home/ubuntu/.config/wireplumber/main.lua.d \
  /home/ubuntu/.config/wireplumber/bluetooth.lua.d \
  /home/ubuntu/.config/pipewire/pipewire.conf.d \
  /home/ubuntu/.config/pipewire/client.conf.d \
  /home/ubuntu/.config/pipewire/pipewire-pulse.conf.d
chown ubuntu:ubuntu /home/ubuntu/.config /home/ubuntu/.local /home/ubuntu/.local/share

while IFS= read -r -d '' f; do
  rel="${f#"$SCRIPT_DIR/configs/systemd-user/"}"
  [[ "$rel" == *.example* ]] && continue
  install -D -o ubuntu -g ubuntu -m 0644 "$f" "/home/ubuntu/.config/systemd/user/$rel"
done < <(find "$SCRIPT_DIR/configs/systemd-user" -type f -print0)
for u in thinksmart-mic.service tas5782m-preinit.service thinksmart-airplay.service xiaozhi-hub.service; do
  if [ -f "/home/ubuntu/.config/systemd/user/$u" ]; then
    ln -sfn "../$u" "/home/ubuntu/.config/systemd/user/default.target.wants/$u"
  fi
done
if [ -f /home/ubuntu/.config/systemd/user/thinksmart-mic.service ]; then
  ln -sfn /home/ubuntu/.config/systemd/user/thinksmart-mic.service \
    /home/ubuntu/.config/systemd/user/pipewire-pulse.service.wants/thinksmart-mic.service
fi
if [ -f /home/ubuntu/.config/systemd/user/tas5782m-preinit.service ]; then
  ln -sfn /home/ubuntu/.config/systemd/user/tas5782m-preinit.service \
    /home/ubuntu/.config/systemd/user/wireplumber.service.wants/tas5782m-preinit.service
fi
for u in idle-blanker.service vcnl4200-proximity-daemon.service; do
  if [ -f "/etc/systemd/user/$u" ]; then
    ln -sfn "/etc/systemd/user/$u" "/home/ubuntu/.config/systemd/user/default.target.wants/$u"
  fi
done

for f in "$SCRIPT_DIR"/configs/user-wireplumber/*; do
  [ -f "$f" ] && install -o ubuntu -g ubuntu -m 0644 "$f" "/home/ubuntu/.config/wireplumber/main.lua.d/$(basename "$f")"
done
for f in "$SCRIPT_DIR"/configs/user-wireplumber-bt/*; do
  [ -f "$f" ] && install -o ubuntu -g ubuntu -m 0644 "$f" "/home/ubuntu/.config/wireplumber/bluetooth.lua.d/$(basename "$f")"
done
while IFS= read -r -d '' f; do
  rel="${f#"$SCRIPT_DIR/configs/user-pipewire/"}"
  install -D -o ubuntu -g ubuntu -m 0644 "$f" "/home/ubuntu/.config/pipewire/$rel"
done < <(find "$SCRIPT_DIR/configs/user-pipewire" -type f -print0)
chown -R ubuntu:ubuntu /home/ubuntu/.config/systemd /home/ubuntu/.config/wireplumber /home/ubuntu/.config/pipewire

# 5. 状态目录与初始配置/口令（仅首次初始化）
install -d -m 0755 /mnt/usb
install -d -o ubuntu -g ubuntu -m 0700 \
  /var/lib/thinksmart-admin \
  /var/lib/thinksmart-home \
  /var/lib/thinksmart-monitor \
  /var/lib/thinksmart-study \
  /var/lib/thinksmart-tutor \
  /home/ubuntu/.config/thinksmart-qqmusic \
  /home/ubuntu/.local/share/thinksmart-client \
  /home/ubuntu/.local/share/thinksmart-client/alarms
install -d -o ubuntu -g ubuntu -m 0755 \
  /home/ubuntu/.config/thinksmart-client \
  /home/ubuntu/Music \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/music \
  /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/config/assets/wakeup_words
if [ ! -f /home/ubuntu/.config/thinksmart-client/config.json ]; then
  printf '{\n  "wake_words": [\n    "n ǐ h ǎo x iǎo zh ì @你好小智"\n  ]\n}\n' > /home/ubuntu/.config/thinksmart-client/config.json
  chown ubuntu:ubuntu /home/ubuntu/.config/thinksmart-client/config.json
  chmod 0644 /home/ubuntu/.config/thinksmart-client/config.json
fi
# 6. 可选从备份包恢复整机用户配置
if [ -n "$RESTORE_ARCHIVE" ]; then
  /usr/local/sbin/thinksmart-backup restore "$RESTORE_ARCHIVE"
  touch /var/lib/thinksmart-admin/.auth_initialized
  chmod 0600 /var/lib/thinksmart-admin/.auth_initialized
  chown -R ubuntu:ubuntu \
    /var/lib/thinksmart-admin \
    /var/lib/thinksmart-home \
    /var/lib/thinksmart-monitor \
    /var/lib/thinksmart-study \
    /var/lib/thinksmart-tutor \
    /home/ubuntu/.config/thinksmart-qqmusic \
    /home/ubuntu/.config/thinksmart-client \
    /home/ubuntu/.local/share/thinksmart-client \
    /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data
fi
if [ ! -f /var/lib/thinksmart-admin/auth.json ] && [ ! -f /var/lib/thinksmart-admin/.auth_initialized ] && [ -x /usr/local/sbin/thinksmart-set-admin-password ]; then
  printf 'admin:admin\n' | /usr/local/sbin/thinksmart-set-admin-password >/dev/null || true
fi
if [ -d /var/lib/thinksmart-admin ] && [ ! -f /var/lib/thinksmart-admin/.auth_initialized ]; then
  touch /var/lib/thinksmart-admin/.auth_initialized
  chown ubuntu:ubuntu /var/lib/thinksmart-admin/.auth_initialized
  chmod 0600 /var/lib/thinksmart-admin/.auth_initialized
fi

# 7. 永久停用第三方在线更新监听并刷新 systemd
systemctl disable --now thinksmart-update-check.timer thinksmart-update-request.path 2>/dev/null || true
systemctl mask getty@tty1.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable \
  display-guard.service screen-wake.service thinksmart-gpu-watchdog.service \
  thinksmart-home.service thinksmart-qqmusic.service thinksmart-monitor.service \
  thinksmart-intercom.service thinksmart-study.service thinksmart-kiosk.service \
  thinksmart-usb-host.service thinksmart-usb-watchdog.service \
  thinksmart-zram.service thinksmart-ssh-hostkeys.service \
  thinksmart-client.service thinksmart-admin.socket thinksmart-tutor.socket \
  thinksmart-admin.service thinksmart-tutor.service 2>/dev/null || true

echo "[DONE] ThinkSmart View 安装完成。如首次部署底层音频/摄像头配置，请重启一次设备 (sudo reboot)。"
/usr/local/bin/diagnose-wake.sh || true
