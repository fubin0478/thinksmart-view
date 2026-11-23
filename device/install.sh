#!/usr/bin/env bash
# ThinkSmart View (CD-18781Y) 设备端全栈套件一键二次安装与升级脚本
# 前置底座要求：justhalfbit/thinksmart-ubuntu-image (Ubuntu 24.04 ARM64，已内置小智语音客户端 xiaozhi-thinksmart-client)
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "请使用 sudo 运行：sudo bash device/install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEV_DIR="$SCRIPT_DIR"
PREBUILT_DIR="$DEV_DIR/prebuilt/arm64"
NEED_REBOOT_FOR_CAM=0

# 系统级与用户态服务统一运行在设备默认用户 ubuntu (UID 1000) 下；优先锁定 ubuntu，避免用其他账号 sudo 时装错家目录
if [ "${ALLOW_CUSTOM_USER:-0}" = "1" ] && [ -n "${THINKSMART_USER:-}" ] && id "$THINKSMART_USER" >/dev/null 2>&1; then
  TARGET_USER="$THINKSMART_USER"
elif id ubuntu >/dev/null 2>&1 && [ "$(id -u ubuntu)" -eq 1000 ]; then
  TARGET_USER="ubuntu"
else
  echo "错误：未找到 UID 为 1000 的设备默认用户 ubuntu（系统服务均绑定 User=ubuntu / /run/user/1000）。" >&2
  exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"
ARCH="$(uname -m)"
NEED_REBOOT_FOR_GROUPS=0

echo "==> 目标仓库目录: $REPO_ROOT"
echo "==> 目标设备用户: $TARGET_USER ($TARGET_HOME, uid=$TARGET_UID)"
echo "==> 当前系统架构: $ARCH"

# 0. 架构与小智前置底座预检
if [ "$ARCH" != "aarch64" ] && [ "${ALLOW_NON_ARM64:-0}" != "1" ]; then
  echo "错误：当前架构为 $ARCH，本安装脚本仅适用于 Lenovo ThinkSmart View (aarch64 Ubuntu 24.04)。" >&2
  exit 1
fi

HAS_XIAOZHI=0
if [ -d /opt/thinksmart/client ] || systemctl cat thinksmart-client.service >/dev/null 2>&1; then
  HAS_XIAOZHI=1
  echo "==> [预检] 已检测到小智语音底座 (thinksmart-client.service)"
else
  if [ "${SKIP_XIAOZHI_CHECK:-0}" != "1" ]; then
    cat >&2 <<'EOF'
错误：未检测到小智语音客户端底座 (/opt/thinksmart/client 或 thinksmart-client.service)。

说明：
  本套件是基于「小智语音底座」构建的零侵入二次安装扩展包（小智提供底层 :8090 宿主页、
  常驻麦克风唤醒、WebRTC 回声消除及系统设置/Wi-Fi 面板）。
  请先刷入已内置小智的社区固件镜像：
    https://github.com/justhalfbit/thinksmart-ubuntu-image
  刷机开机后再执行 sudo bash device/install.sh 即可一键完成全部扩展安装。
  （如仅需在无小智环境强制预装文件，可加 SKIP_XIAOZHI_CHECK=1 运行）
EOF
    exit 1
  fi
  echo "警告：未检测到 thinksmart-client.service，已按 SKIP_XIAOZHI_CHECK=1 继续安装。" >&2
fi

# 1. 检查并补齐系统 apt 运行时依赖、用户硬件组与目录
echo "==> [1/9] 检查系统 apt 依赖包与硬件权限组..."
REQUIRED_PKGS=(
  python3-venv
  python3-pip
  python3-aiohttp
  python3-numpy
  python3-gi
  python3-dbus
  python3-smbus2
  python3-yaml
  python3-ruamel.yaml
  python3-pil
  v4l-utils
  qrencode
  i2c-tools
  wireplumber
  libspa-0.2-bluetooth
  gir1.2-webkit2-4.1
  weston
  ffmpeg
  pulseaudio-utils
  libfdk-aac2
  avahi-daemon
  uxplay
  gstreamer1.0-gl
  gstreamer1.0-plugins-bad
  gstreamer1.0-plugins-good
  gstreamer1.0-libav
  gstreamer1.0-alsa
  exfatprogs
  ntfs-3g
  device-tree-compiler
)
MISSING_PKGS=()
for pkg in "${REQUIRED_PKGS[@]}"; do
  if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q '^install ok installed$'; then
    MISSING_PKGS+=("$pkg")
  fi
done
if [ "${#MISSING_PKGS[@]}" -gt 0 ]; then
  echo "    正在自动补齐缺失的系统软件包: ${MISSING_PKGS[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}"
fi

CUR_GROUPS=" $(id -nG "$TARGET_USER" 2>/dev/null || true) "
for chk_grp in video i2c input; do
  if getent group "$chk_grp" >/dev/null 2>&1 && [[ "$CUR_GROUPS" != *" $chk_grp "* ]]; then
    NEED_REBOOT_FOR_GROUPS=1
  fi
done
for grp in audio video input plugdev netdev i2c bluetooth; do
  if getent group "$grp" >/dev/null 2>&1; then
    usermod -aG "$grp" "$TARGET_USER" 2>/dev/null || true
  fi
done

modprobe i2c-dev 2>/dev/null || true
install -d -m 0755 /etc/modules-load.d
if [ ! -f /etc/modules-load.d/thinksmart-i2c.conf ]; then
  printf 'i2c-dev\n' > /etc/modules-load.d/thinksmart-i2c.conf
  chmod 0644 /etc/modules-load.d/thinksmart-i2c.conf
fi

mkdir -p /mnt/usb "$TARGET_HOME/Music" "$TARGET_HOME/.config/thinksmart-qqmusic"
if [ ! -e "$TARGET_HOME/Music/usb" ] && [ ! -L "$TARGET_HOME/Music/usb" ]; then
  ln -s /mnt/usb "$TARGET_HOME/Music/usb"
fi
chmod 0700 "$TARGET_HOME/.config/thinksmart-qqmusic"
chown "$TARGET_UID:$TARGET_GID" "$TARGET_HOME/Music" "$TARGET_HOME/.config/thinksmart-qqmusic"
chown -h "$TARGET_UID:$TARGET_GID" "$TARGET_HOME/Music/usb" 2>/dev/null || true

# 2. 创建独立 Python 运行环境与测试套件（开启 system-site-packages 复用系统 PIL/yaml/gi/dbus/aiohttp/numpy）
if [ ! -x /opt/thinksmart-addons/venv/bin/python ]; then
  echo "==> [2/9] 创建 /opt/thinksmart-addons/venv 独立虚拟环境 (--system-site-packages)..."
  mkdir -p /opt/thinksmart-addons
  python3 -m venv --system-site-packages /opt/thinksmart-addons/venv
elif grep -q '^include-system-site-packages = false' /opt/thinksmart-addons/venv/pyvenv.cfg 2>/dev/null; then
  sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' /opt/thinksmart-addons/venv/pyvenv.cfg
fi
if [ ! -x /opt/thinksmart-addons/venv/bin/pip ]; then
  /opt/thinksmart-addons/venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || python3 -m venv --system-site-packages --upgrade /opt/thinksmart-addons/venv || true
fi
echo "==> [2/9] 校验 Python 依赖 (aiohttp, numpy, lunar_python)..."
if [ -x /opt/thinksmart-addons/venv/bin/pip ]; then
  /opt/thinksmart-addons/venv/bin/pip install -q aiohttp numpy lunar_python \
    || /opt/thinksmart-addons/venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple aiohttp numpy lunar_python
elif command -v pip3 >/dev/null 2>&1; then
  pip3 install --python /opt/thinksmart-addons/venv/bin/python --break-system-packages -q aiohttp numpy lunar_python \
    || pip3 install --python /opt/thinksmart-addons/venv/bin/python --break-system-packages -q -i https://pypi.tuna.tsinghua.edu.cn/simple aiohttp numpy lunar_python
fi

VENV_SITE="$(/opt/thinksmart-addons/venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"
if [ -d "$VENV_SITE/lunar_python" ]; then
  mkdir -p /opt/thinksmart-addons/vendor
  ln -sfn "$VENV_SITE/lunar_python" /opt/thinksmart-addons/vendor/lunar_python
fi
if [ -d "$DEV_DIR/tests" ]; then
  for tf in "$DEV_DIR/tests"/test_*.py; do
    [ -f "$tf" ] && install -m 0755 "$tf" "/opt/thinksmart-addons/$(basename "$tf")"
  done
fi

# 3. 安装 /usr/local/bin、/usr/local/sbin 与 ARM64 硬件预编译组件
echo "==> [3/9] 安装可执行程序与硬件加速/摄像头组件..."
BIN_FILES=(
  "$DEV_DIR/home/thinksmart-home.py"
  "$DEV_DIR/qqmusic/thinksmart-qqmusic.py"
  "$DEV_DIR/monitor/thinksmart-monitor.py"
  "$DEV_DIR/monitor/thinksmart_display.py"
  "$DEV_DIR/intercom/thinksmart-intercom.py"
  "$DEV_DIR/intercom/thinksmart-intercom-ui.py"
  "$DEV_DIR/intercom/thinksmart-intercom-tunnel.sh"
  "$DEV_DIR/study/thinksmart-study.py"
  "$DEV_DIR/tutor/thinksmart-tutor.py"
  "$DEV_DIR/admin/thinksmart-admin.py"
  "$DEV_DIR/system/idle-blanker.py"
  "$DEV_DIR/system/screen-wake-daemon.py"
  "$DEV_DIR/system/thinksmart-airplay.py"
  "$DEV_DIR/system/thinksmart-kiosk.py"
  "$DEV_DIR/system/thinksmart-kiosk-session"
  "$DEV_DIR/system/thinksmart-kiosk-app"
  "$DEV_DIR/system/kiosk-mode"
  "$DEV_DIR/system/thinksmart-mic"
  "$DEV_DIR/system/vcnl4200-proximity-daemon.py"
  "$DEV_DIR/system/gpu-watchdog.py"
  "$DEV_DIR/system/usb-automount.sh"
  "$DEV_DIR/system/usb-mode"
)
for f in "${BIN_FILES[@]}"; do
  install -D -m 0755 "$f" "/usr/local/bin/$(basename "$f")"
done

install -D -m 0755 "$DEV_DIR/system/thinksmart-review" /usr/local/sbin/thinksmart-review
install -D -m 0755 "$DEV_DIR/system/thinksmart-usb-watchdog" /usr/local/sbin/thinksmart-usb-watchdog
install -D -m 0755 "$DEV_DIR/system/thinksmart-airplay-tune" /usr/local/sbin/thinksmart-airplay-tune
install -D -m 0755 "$DEV_DIR/admin/thinksmart-set-admin-password" /usr/local/sbin/thinksmart-set-admin-password
install -D -m 0700 "$DEV_DIR/system/thinksmart-set-search-keys" /usr/local/sbin/thinksmart-set-search-keys
if [ -f "$DEV_DIR/configs/sudoers/thinksmart-addons" ]; then
  if visudo -cf "$DEV_DIR/configs/sudoers/thinksmart-addons" >/dev/null 2>&1; then
    install -D -m 0440 "$DEV_DIR/configs/sudoers/thinksmart-addons" /etc/sudoers.d/thinksmart-addons
  else
    echo "警告：sudoers 模板校验未通过，跳过安装 /etc/sudoers.d/thinksmart-addons" >&2
  fi
fi

if [ "$ARCH" = "aarch64" ] && [ -f "$PREBUILT_DIR/SHA256SUMS" ]; then
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
      if [ "$CUR_DTB_SHA" = "$CAM_DTB_SHA" ]; then
        echo "    [DTB] 前置摄像头 S5KC505A 设备树已处于启用状态。"
      elif [ "$CUR_DTB_SHA" = "$STOCK_DTB_SHA" ] || [ "${FORCE_CAMERA_DTB:-0}" = "1" ]; then
        [ -f "${BOOT_DTB}.bak" ] || cp -p "$BOOT_DTB" "${BOOT_DTB}.bak"
        install -m 0644 "$PREBUILT_DIR/apq8053-lenovo-cd-18781y.dtb" "$BOOT_DTB"
        NEED_REBOOT_FOR_CAM=1
        echo "    [DTB] 已自动备份 ${BOOT_DTB}.bak 并写入摄像头设备树补丁（重启后 /dev/video0 生效）。"
      else
        echo "    [DTB] 当前 $BOOT_DTB 哈希 ($CUR_DTB_SHA) 非 20261101 原厂底包，为保护启动未覆盖；可参考 $PREBUILT_DIR/apq8053-camera.dts.patch 手动合入。"
      fi
    fi
  else
    echo "警告：$PREBUILT_DIR/SHA256SUMS 校验未通过，跳过预编译二进制部署。" >&2
  fi
fi

# 4. 安装 WebRTC 回声消除 & 语音 MCP 桥接钩子（文件名必须为 sitecustomize.py）
echo "==> [4/9] 安装 thinksmart-client AEC 与语音工具桥接钩子..."
install -D -m 0644 "$DEV_DIR/qqmusic/aec_bridge_sitecustomize.py" /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py
install -D -m 0644 "$DEV_DIR/configs/systemd/system/thinksmart-client.service.d/aec-bridge.conf" \
  /etc/systemd/system/thinksmart-client.service.d/aec-bridge.conf

# 5. 安装静态网页、Kiosk 页面与注入脚本
echo "==> [5/9] 安装静态资源到 /usr/local/share/thinksmart-*..."
install -D -m 0644 "$DEV_DIR/home/index.html" /usr/local/share/thinksmart-home/index.html
install -D -m 0644 "$DEV_DIR/home/apps.json" /usr/local/share/thinksmart-home/apps.json

install -D -m 0644 "$DEV_DIR/qqmusic/index.html" /usr/local/share/thinksmart-qqmusic/index.html
install -D -m 0644 "$DEV_DIR/qqmusic/kiosk_overlay.js" /usr/local/share/thinksmart-qqmusic/kiosk_overlay.js

install -D -m 0644 "$DEV_DIR/monitor/index.html" /usr/local/share/thinksmart-monitor/index.html
install -D -m 0644 "$DEV_DIR/monitor/display.html" /usr/local/share/thinksmart-monitor/display.html
install -D -m 0644 "$DEV_DIR/monitor/home_overlay.js" /usr/local/share/thinksmart-monitor/home_overlay.js
install -D -m 0644 "$DEV_DIR/monitor/servers.example.json" /usr/local/share/thinksmart-monitor/servers.example.json
install -D -m 0644 "$DEV_DIR/monitor/traffic.example.json" /usr/local/share/thinksmart-monitor/traffic.example.json
if [ ! -f /usr/local/share/thinksmart-monitor/servers.json ]; then
  install -m 0644 "$DEV_DIR/monitor/servers.example.json" /usr/local/share/thinksmart-monitor/servers.json
fi
if [ ! -f /usr/local/share/thinksmart-monitor/traffic.json ]; then
  install -m 0644 "$DEV_DIR/monitor/traffic.example.json" /usr/local/share/thinksmart-monitor/traffic.json
fi

install -D -m 0644 "$DEV_DIR/intercom/apple-touch-icon.png" /usr/local/share/thinksmart-intercom/apple-touch-icon.png
install -D -m 0644 "$DEV_DIR/intercom/favicon.svg" /usr/local/share/thinksmart-intercom/favicon.svg

install -D -m 0644 "$DEV_DIR/study/parent.html" /usr/local/share/thinksmart-study/parent.html
install -D -m 0644 "$DEV_DIR/study/apple-touch-icon.png" /usr/local/share/thinksmart-study/apple-touch-icon.png
install -D -m 0644 "$DEV_DIR/study/favicon.svg" /usr/local/share/thinksmart-study/favicon.svg

install -D -m 0644 "$DEV_DIR/tutor/index.html" /usr/local/share/thinksmart-tutor/index.html
install -D -m 0644 "$DEV_DIR/tutor/mobile.html" /usr/local/share/thinksmart-tutor/mobile.html
install -D -m 0644 "$DEV_DIR/tutor/manifest.json" /usr/local/share/thinksmart-tutor/manifest.json
install -D -m 0644 "$DEV_DIR/tutor/favicon.svg" /usr/local/share/thinksmart-tutor/favicon.svg
install -D -m 0644 "$DEV_DIR/tutor/apple-touch-icon.png" /usr/local/share/thinksmart-tutor/apple-touch-icon.png

install -D -m 0644 "$DEV_DIR/admin/index.html" /usr/local/share/thinksmart-admin/index.html
install -D -m 0644 "$DEV_DIR/admin/favicon.svg" /usr/local/share/thinksmart-admin/favicon.svg
install -D -m 0644 "$DEV_DIR/admin/icon.png" /usr/local/share/thinksmart-admin/icon.png

for f in "$DEV_DIR/kiosk/"*; do
  install -D -m 0644 "$f" "/usr/local/share/thinksmart-kiosk/$(basename "$f")"
done

# 6. 确保 PAM 会话文件与 /etc/thinksmart-kiosk.conf 顶层 URL 指向 :8090（绝不能设为 :8095）
echo "==> [6/9] 检查 PAM 登录配置与 /etc/thinksmart-kiosk.conf 宿主页..."
install -D -m 0644 "$DEV_DIR/configs/pam.d/thinksmart-kiosk" /etc/pam.d/thinksmart-kiosk
if [ ! -f /etc/thinksmart-kiosk.conf ]; then
  install -D -m 0644 "$DEV_DIR/configs/kiosk/thinksmart-kiosk.conf.example" /etc/thinksmart-kiosk.conf
else
  CUR_URL="$(sed -n 's/^URL=\(.*\)/\1/p' /etc/thinksmart-kiosk.conf | tail -n 1 | tr -d '"'\''[:space:]')"
  case "$CUR_URL" in
    http://127.0.0.1:8095*|http://localhost:8095*)
      echo "    检测到 URL 指向了主页 :8095（会导致无法唤醒小智），正在自动改回 http://127.0.0.1:8090/ ..."
      /usr/local/bin/kiosk-mode url http://127.0.0.1:8090/
      ;;
    "")
      /usr/local/bin/kiosk-mode url http://127.0.0.1:8090/
      ;;
  esac
fi

# 7. 安装系统级 journald / systemd / udev / sysctl / WirePlumber / NetworkManager / Bluetooth 配置
echo "==> [7/9] 安装系统级音频、蓝牙、Wi-Fi 省电保护与 systemd 配置..."
install -D -m 0644 "$DEV_DIR/configs/journald/thinksmart.conf" \
  /etc/systemd/journald.conf.d/thinksmart.conf

# 若用户此前直接修改过主 unit 内的环境变量（如 NEZHA_HOST 或 WEATHER_*），自动迁移至 local.conf 防止升级丢失
for svc_preserve in thinksmart-monitor thinksmart-home; do
  unit_path="/etc/systemd/system/${svc_preserve}.service"
  local_drop="/etc/systemd/system/${svc_preserve}.service.d/local.conf"
  if [ -f "$unit_path" ] && [ ! -f "$local_drop" ]; then
    if ! cmp -s "$DEV_DIR/configs/systemd/system/${svc_preserve}.service" "$unit_path"; then
      ENV_LINES="$(grep '^Environment=' "$unit_path" | grep -v 'NEZHA_HOST=nezha\.example\.com' || true)"
      if [ -n "$ENV_LINES" ] && [ "$svc_preserve" = "thinksmart-monitor" ]; then
        mkdir -p "/etc/systemd/system/${svc_preserve}.service.d"
        printf '[Service]\n%s\n' "$ENV_LINES" > "$local_drop"
        chmod 0644 "$local_drop"
        echo "    已自动将 ${svc_preserve}.service 中的自定义环境变量迁移至 $local_drop"
      elif [ "$svc_preserve" = "thinksmart-home" ] && grep -Eq 'WEATHER_|QWEATHER_' "$unit_path"; then
        mkdir -p "/etc/systemd/system/${svc_preserve}.service.d"
        grep '^Environment=' "$unit_path" | sed '1i[Service]' > "$local_drop"
        chmod 0644 "$local_drop"
        echo "    已自动将 ${svc_preserve}.service 中的自定义天气配置迁移至 $local_drop"
      fi
    fi
  fi
done

for f in "$DEV_DIR/configs/systemd/system/"*.service "$DEV_DIR/configs/systemd/system/"*.socket; do
  [ -f "$f" ] && install -D -m 0644 "$f" "/etc/systemd/system/$(basename "$f")"
done
for dropin_rel in \
  NetworkManager-wait-online.service.d/thinksmart.conf \
  bluetooth.service.d/no-sap.conf \
  cron.service.d/override.conf \
  screen-wake.service.d/override.conf \
  thinksmart-gpu-watchdog.service.d/override.conf \
  thinksmart-intercom.service.d/hardening.conf \
  thinksmart-intercom-tunnel.service.d/hardening.conf \
  thinksmart-qqmusic.service.d/limits.conf \
  thinksmart-kiosk.service.d/override.conf; do
  install -D -m 0644 "$DEV_DIR/configs/systemd/system/$dropin_rel" \
    "/etc/systemd/system/$dropin_rel"
done

# 确保 LoadCredential=mihomo.yaml:/etc/mihomo/config.yaml 与管理后台/服务可写目录就绪
install -d -m 0700 /etc/mihomo
if [ ! -f /etc/mihomo/config.yaml ]; then
  printf '{}\n' > /etc/mihomo/config.yaml
  chmod 0600 /etc/mihomo/config.yaml
fi
[ -d /etc/mihomo/ui ] && chmod -R a+rX /etc/mihomo/ui 2>/dev/null || true
install -d -m 0700 -o "$TARGET_UID" -g "$TARGET_GID" \
  /var/lib/thinksmart-admin /var/lib/thinksmart-tutor /var/lib/thinksmart-home /var/lib/thinksmart-study /var/lib/thinksmart-monitor
install -d -m 0755 -o "$TARGET_UID" -g "$TARGET_GID" "$TARGET_HOME/.config/thinksmart-client"
if [ -d "$TARGET_HOME/xiaozhi-hub-server/main/xiaozhi-server" ]; then
  install -d -m 0700 -o "$TARGET_UID" -g "$TARGET_GID" "$TARGET_HOME/xiaozhi-hub-server/main/xiaozhi-server/data"
  install -d -m 0755 -o "$TARGET_UID" -g "$TARGET_GID" "$TARGET_HOME/xiaozhi-hub-server/main/xiaozhi-server/music"
  install -d -m 0755 -o "$TARGET_UID" -g "$TARGET_GID" "$TARGET_HOME/xiaozhi-hub-server/main/xiaozhi-server/config/assets/wakeup_words"
fi
if [ ! -f /var/lib/thinksmart-admin/auth.json ]; then
  printf 'admin:admin\n' | setpriv --reuid="$TARGET_UID" --regid="$TARGET_GID" --init-groups \
    env ADMIN_STATE=/var/lib/thinksmart-admin PYTHONDONTWRITEBYTECODE=1 \
    /opt/thinksmart-addons/venv/bin/python /usr/local/bin/thinksmart-admin.py --set-password >/dev/null
  echo "    已初始化管理后台默认账号/密码: admin / admin（首次内网登录后建议修改）"
fi
if [ ! -f /var/lib/thinksmart-admin/wan_access.json ]; then
  printf '{"enabled": false, "updated_at": 0}\n' > /var/lib/thinksmart-admin/wan_access.json
  chown "$TARGET_UID:$TARGET_GID" /var/lib/thinksmart-admin/wan_access.json
  chmod 0600 /var/lib/thinksmart-admin/wan_access.json
fi

for f in "$DEV_DIR/configs/udev/"*.rules; do
  install -D -m 0644 "$f" "/etc/udev/rules.d/$(basename "$f")"
done
for f in "$DEV_DIR/configs/sysctl/"*.conf; do
  install -D -m 0644 "$f" "/etc/sysctl.d/$(basename "$f")"
done

install -D -m 0644 "$DEV_DIR/configs/wireplumber-system/scripts/suspend-node.lua" \
  /etc/wireplumber/scripts/suspend-node.lua
install -D -m 0644 "$DEV_DIR/configs/wireplumber-system/bluetooth.lua.d/60-thinksmart-speaker.lua" \
  /etc/wireplumber/bluetooth.lua.d/60-thinksmart-speaker.lua
install -D -m 0644 "$DEV_DIR/configs/wireplumber-system/main.lua.d/99-cd18781y-alsa.lua" \
  /etc/wireplumber/main.lua.d/99-cd18781y-alsa.lua

install -D -m 0644 "$DEV_DIR/configs/NetworkManager/zz-thinksmart-wifi-powersave-off.conf" \
  /etc/NetworkManager/conf.d/zz-thinksmart-wifi-powersave-off.conf

if [ -d /etc/bluetooth ]; then
  chmod u+w /etc/bluetooth 2>/dev/null || true
  if [ -f /etc/bluetooth/main.conf ] && [ ! -f /etc/bluetooth/main.conf.bak ]; then
    cp -p /etc/bluetooth/main.conf /etc/bluetooth/main.conf.bak
  fi
  install -m 0644 "$DEV_DIR/configs/bluetooth/main.conf" /etc/bluetooth/main.conf
fi

# 8. 安装用户级 PipeWire / WirePlumber / systemd-user 配置与 xiaozhi-hub overrides
echo "==> [8/9] 安装用户级 PipeWire / WirePlumber / systemd 配置到 $TARGET_HOME/.config/..."
for sub in client.conf.d pipewire-pulse.conf.d pipewire.conf.d; do
  mkdir -p "$TARGET_HOME/.config/pipewire/$sub"
  for f in "$DEV_DIR/configs/pipewire/$sub/"*.conf; do
    install -m 0644 "$f" "$TARGET_HOME/.config/pipewire/$sub/$(basename "$f")"
  done
done

for sub in bluetooth.lua.d main.lua.d; do
  mkdir -p "$TARGET_HOME/.config/wireplumber/$sub"
  for f in "$DEV_DIR/configs/wireplumber/$sub/"*.lua; do
    install -m 0644 "$f" "$TARGET_HOME/.config/wireplumber/$sub/$(basename "$f")"
  done
done

mkdir -p \
  /etc/systemd/user \
  "$TARGET_HOME/.config/systemd/user/thinksmart-mic.service.d" \
  "$TARGET_HOME/.config/systemd/user/xiaozhi-hub.service.d"
# idle-blanker 与 vcnl4200-proximity-daemon 统一安装至 /etc/systemd/user，避免 ~/.config 副本遮蔽
rm -rf \
  "$TARGET_HOME/.config/systemd/user/idle-blanker.service" \
  "$TARGET_HOME/.config/systemd/user/idle-blanker.service.d" \
  "$TARGET_HOME/.config/systemd/user/vcnl4200-proximity-daemon.service"
for sys_u in idle-blanker.service vcnl4200-proximity-daemon.service; do
  if [ -f "$DEV_DIR/configs/systemd/user/$sys_u" ]; then
    install -D -m 0644 "$DEV_DIR/configs/systemd/user/$sys_u" "/etc/systemd/user/$sys_u"
  fi
done
for usr_u in tas5782m-preinit.service thinksmart-airplay.service thinksmart-mic.service; do
  if [ -f "$DEV_DIR/configs/systemd/user/$usr_u" ]; then
    install -m 0644 "$DEV_DIR/configs/systemd/user/$usr_u" "$TARGET_HOME/.config/systemd/user/$usr_u"
  fi
done
install -m 0644 "$DEV_DIR/configs/systemd/user/thinksmart-mic.service.d/override.conf" \
  "$TARGET_HOME/.config/systemd/user/thinksmart-mic.service.d/override.conf"
# 仅安装正式 drop-in，跳过 proxy.conf.example，避免无本地代理时阻断 xiaozhi-hub
install -m 0644 "$DEV_DIR/configs/systemd/user/xiaozhi-hub.service.d/thinksmart-overrides.conf" \
  "$TARGET_HOME/.config/systemd/user/xiaozhi-hub.service.d/thinksmart-overrides.conf"

chown -R "$TARGET_UID:$TARGET_GID" \
  "$TARGET_HOME/.config/pipewire" \
  "$TARGET_HOME/.config/wireplumber" \
  "$TARGET_HOME/.config/systemd"

install -D -m 0644 "$DEV_DIR/xiaozhi-overrides/sitecustomize.py" \
  /opt/thinksmart-addons/xiaozhi-hub-overrides/sitecustomize.py
if [ -d "$DEV_DIR/xiaozhi-overrides/files" ]; then
  rm -rf /opt/thinksmart-addons/xiaozhi-hub-overrides/files
  cp -R "$DEV_DIR/xiaozhi-overrides/files" /opt/thinksmart-addons/xiaozhi-hub-overrides/files
  chown -R root:root /opt/thinksmart-addons/xiaozhi-hub-overrides
  chmod -R u=rwX,go=rX /opt/thinksmart-addons/xiaozhi-hub-overrides
fi

# 9. 屏蔽冲突服务并重载所有系统级/用户级服务
echo "==> [9/9] 屏蔽冲突守护进程并启动全部服务..."
for conflict_unit in shairport-sync.service iio-sensor-proxy.service udisks2.service; do
  systemctl stop "$conflict_unit" >/dev/null 2>&1 || true
  systemctl mask "$conflict_unit" >/dev/null 2>&1 || true
done

udevadm control --reload-rules >/dev/null 2>&1 || true
udevadm trigger --subsystem-match=backlight --subsystem-match=input --subsystem-match=i2c-dev --subsystem-match=block >/dev/null 2>&1 || true
sysctl --system >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl restart systemd-journald >/dev/null 2>&1 || true

for infra_unit in avahi-daemon.service bluetooth.service; do
  systemctl enable "$infra_unit" >/dev/null 2>&1 || true
  systemctl restart "$infra_unit" >/dev/null 2>&1 || true
done

SYSTEM_UNITS=(
  display-guard.service
  screen-wake.service
  thinksmart-gpu-watchdog.service
  thinksmart-usb-host.service
  thinksmart-usb-watchdog.service
  thinksmart-home.service
  thinksmart-qqmusic.service
  thinksmart-monitor.service
  thinksmart-study.service
  thinksmart-intercom.service
  thinksmart-kiosk.service
)
FAILED_INSTALL_UNITS=()
for u in "${SYSTEM_UNITS[@]}"; do
  systemctl enable "$u" >/dev/null 2>&1 || true
  systemctl restart "$u" >/dev/null 2>&1 || true
  if ! systemctl is-active --quiet "$u"; then
    FAILED_INSTALL_UNITS+=("$u")
  fi
done

# 管理后台 (:8097) 与 AI 动态黑板讲题 (:8096) 采用 systemd socket 按需秒级拉起
for sock in thinksmart-admin.socket thinksmart-tutor.socket; do
  systemctl enable "$sock" >/dev/null 2>&1 || true
  systemctl start "$sock" >/dev/null 2>&1 || true
  if ! systemctl is-active --quiet "$sock"; then
    FAILED_INSTALL_UNITS+=("$sock")
  fi
done
for lazy_svc in thinksmart-admin.service thinksmart-tutor.service; do
  systemctl disable "$lazy_svc" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$lazy_svc"; then
    systemctl restart "$lazy_svc" >/dev/null 2>&1 || true
  fi
done

if [ "${#FAILED_INSTALL_UNITS[@]}" -gt 0 ]; then
  echo "    [WARN] 以下服务/Socket 在本次启动后尚未进入 active 状态，请检查 journalctl：${FAILED_INSTALL_UNITS[*]}" >&2
fi

if [ "$HAS_XIAOZHI" -eq 1 ]; then
  systemctl restart thinksmart-client.service >/dev/null 2>&1 || true
fi

USER_ENV="XDG_RUNTIME_DIR=/run/user/$TARGET_UID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$TARGET_UID/bus"
if su - "$TARGET_USER" -c "$USER_ENV systemctl --user status" >/dev/null 2>&1; then
  su - "$TARGET_USER" -c "$USER_ENV systemctl --user daemon-reload" || true
  for u in thinksmart-mic.service tas5782m-preinit.service idle-blanker.service vcnl4200-proximity-daemon.service thinksmart-airplay.service; do
    su - "$TARGET_USER" -c "$USER_ENV systemctl --user enable '$u'" >/dev/null 2>&1 || true
  done
  su - "$TARGET_USER" -c "$USER_ENV systemctl --user restart pipewire pipewire-pulse wireplumber thinksmart-mic.service idle-blanker.service vcnl4200-proximity-daemon.service thinksmart-airplay.service" >/dev/null 2>&1 || true
  su - "$TARGET_USER" -c "$USER_ENV systemctl --user start tas5782m-preinit.service" >/dev/null 2>&1 || true
  if su - "$TARGET_USER" -c "$USER_ENV systemctl --user list-unit-files xiaozhi-hub.service" 2>/dev/null | grep -q 'xiaozhi-hub.service'; then
    su - "$TARGET_USER" -c "$USER_ENV systemctl --user restart xiaozhi-hub.service" >/dev/null 2>&1 || true
  fi
else
  echo "提示：用户态 systemd 会话当前未运行，请重启设备后自动拉起用户态服务。"
fi

echo ""
if [ "$NEED_REBOOT_FOR_CAM" -eq 1 ]; then
  echo "重要提示：本次已更新前置摄像头设备树 (/boot/apq8053-lenovo-cd-18781y.dtb)，请执行一次 sudo reboot 激活摄像头！"
elif [ "$NEED_REBOOT_FOR_GROUPS" -eq 1 ]; then
  echo "重要提示：本次已将 $TARGET_USER 新加入硬件权限组 (video/i2c/input)，请执行一次 sudo reboot 使运行中的用户会话继承新权限！"
fi
echo "管理后台提示："
echo "  · 仅限局域网内网登录：http://<设备局域网IP>:18091/admin/"
echo "  · 初始用户名与密码均为 admin / admin（外网访问默认关闭，可在后台右上角按需手动开启）"
echo "  · 如接入 USB 高拍仪 / U 盘进行 AI 讲题，请配合带独立供电的 USB Hub 并推荐竖版立式使用"
echo ""
echo "安装完成！建议运行以下命令自检整机服务与跨界面唤醒链路："
echo "  bash $DEV_DIR/diagnose-wake.sh"
