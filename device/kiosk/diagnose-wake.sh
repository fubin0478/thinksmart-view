#!/usr/bin/env bash
# 只读自检脚本：全面排查「小智底座、跨界面语音唤醒链路、自研服务与硬件组件」就绪状态
set -u

FAIL=0
WARN=0

pass() { printf '[\033[32mPASS\033[0m] %s\n' "$1"; }
warn() { printf '[\033[33mWARN\033[0m] %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf '[\033[31mFAIL\033[0m] %s\n' "$1"; FAIL=$((FAIL + 1)); }

echo "=== ThinkSmart View 整机部署与跨界面唤醒自检 ==="

# 1. 基础语音客户端是否安装与运行
if [ -d /opt/thinksmart/client/current ] && systemctl is-active --quiet thinksmart-client.service; then
  pass "thinksmart-client.service 运行中（后台常驻麦克风与 sherpa-onnx 唤醒）"
else
  fail "未检测到运行中的 thinksmart-client.service（/opt/thinksmart/client/current）。请检查自主维护版小智客户端服务状态。"
fi

# 2. Kiosk 顶层 URL 必须指向小智 :8090 + PAM 会话文件就位
if [ -f /etc/pam.d/thinksmart-kiosk ]; then
  pass "存在 PAM 会话配置 /etc/pam.d/thinksmart-kiosk"
else
  fail "缺少 /etc/pam.d/thinksmart-kiosk（Weston Kiosk 自动登录将失败，请重新运行 sudo bash device/install.sh）"
fi

if [ -f /etc/thinksmart-kiosk.conf ]; then
  KIOSK_URL="$(sed -n 's/^URL=\(.*\)/\1/p' /etc/thinksmart-kiosk.conf | tail -n 1 | tr -d '"'\''[:space:]')"
  case "$KIOSK_URL" in
    http://127.0.0.1:8090*|http://localhost:8090*)
      pass "/etc/thinksmart-kiosk.conf URL=$KIOSK_URL（小智作为顶层宿主）"
      ;;
    *)
      fail "/etc/thinksmart-kiosk.conf 当前 URL=$KIOSK_URL，必须保持 http://127.0.0.1:8090/！若设成 :8095 主页，唤醒后不会切回小智界面。修复：sudo kiosk-mode url http://127.0.0.1:8090/ && sudo kiosk-mode restart"
      ;;
  esac
else
  fail "缺少 /etc/thinksmart-kiosk.conf（请运行 sudo bash device/install.sh）"
fi

# 3. Kiosk 注入脚本与叠加层是否存在
if [ -f /usr/local/bin/thinksmart-kiosk.py ] && grep -q 'home_overlay.js' /usr/local/bin/thinksmart-kiosk.py; then
  pass "/usr/local/bin/thinksmart-kiosk.py 已包含 overlay 注入逻辑"
else
  fail "/usr/local/bin/thinksmart-kiosk.py 未更新（仍是旧版），无法向 :8090 注入桌面主页与全屏子页面"
fi

for asset in \
  /usr/local/share/thinksmart-monitor/home_overlay.js \
  /usr/local/share/thinksmart-qqmusic/kiosk_overlay.js \
  /usr/local/share/thinksmart-home/index.html \
  /usr/local/share/thinksmart-qqmusic/index.html \
  /usr/local/share/thinksmart-tutor/index.html \
  /usr/local/share/thinksmart-tutor/mobile.html \
  /usr/local/share/thinksmart-admin/index.html; do
  if [ -f "$asset" ]; then
    pass "存在界面资源: $asset"
  else
    fail "缺少界面资源: $asset"
  fi
done

# 4. 原生 WebRTC AEC 与语音工具桥接 + 第三方更新熔断检查
if [ -f /opt/thinksmart/client/current/thinksmart_client/bridge.py ]; then
  pass "已合入原生 AEC 与语音工具模块 /opt/thinksmart/client/current/thinksmart_client/bridge.py"
else
  fail "缺少原生桥接模块 /opt/thinksmart/client/current/thinksmart_client/bridge.py"
fi

if systemctl is-active --quiet thinksmart-update-check.timer 2>/dev/null || systemctl is-active --quiet thinksmart-update-request.path 2>/dev/null; then
  warn "检测到第三方在线更新定时器/路径监听仍在运行（应停用 thinksmart-update-check.timer 与 thinksmart-update-request.path）"
else
  pass "已停用第三方自动更新监听（本地自主维护模式）"
fi

if [ -n "${THINKSMART_USER:-}" ] && id "$THINKSMART_USER" >/dev/null 2>&1; then
  TARGET_USER="$THINKSMART_USER"
elif id ubuntu >/dev/null 2>&1; then
  TARGET_USER="ubuntu"
else
  TARGET_USER=""
fi
U_HOME=""
U_UID="1000"
if [ -n "$TARGET_USER" ]; then
  U_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
  U_UID="$(id -u "$TARGET_USER")"
fi

if [ -f "/run/user/$U_UID/qqmusic-bridge.json" ]; then
  if grep -q '"audio_hook": true' "/run/user/$U_UID/qqmusic-bridge.json"; then
    pass "运行时桥接已激活 (/run/user/$U_UID/qqmusic-bridge.json audio_hook=true)"
  else
    warn "qqmusic-bridge.json 存在但 audio_hook 未激活：$(cat "/run/user/$U_UID/qqmusic-bridge.json" 2>/dev/null)"
  fi
else
  warn "未找到 /run/user/$U_UID/qqmusic-bridge.json（若刚部署请重启 thinksmart-client.service）"
fi

# 5. 核心系统服务与硬件增强组件检查
for svc in display-guard.service screen-wake.service thinksmart-gpu-watchdog.service thinksmart-home.service thinksmart-qqmusic.service thinksmart-monitor.service thinksmart-intercom.service thinksmart-study.service thinksmart-kiosk.service thinksmart-usb-host.service thinksmart-usb-watchdog.service thinksmart-admin.socket thinksmart-tutor.socket; do
  if systemctl is-active --quiet "$svc"; then
    pass "系统服务/Socket 运行中: $svc"
  else
    fail "系统服务/Socket 未运行: $svc（请检查 journalctl -u $svc -n 30）"
  fi
done

for hw_bin in /usr/local/bin/uxplay /usr/local/lib/libuxplay_autorotate.so /usr/local/bin/s5kc505a_cam_helper; do
  if [ -x "$hw_bin" ]; then
    pass "存在硬件组件: $hw_bin"
  else
    warn "缺少硬件组件: $hw_bin（AirPlay 投屏或前置摄像头将不可用）"
  fi
done

if [ -e /dev/video0 ]; then
  pass "前置摄像头设备节点已就绪 (/dev/video0)"
else
  warn "未检测到 /dev/video0（如刚通过 install.sh 更新摄像头 DTB，执行 sudo reboot 重启一次即可点亮）"
fi

# 6. 麦克风与用户态 PipeWire / WirePlumber（固定检查 ubuntu 用户，兼容普通用户与 root / sudo 调用）
if [ -n "$TARGET_USER" ]; then
  if [ -f "$U_HOME/.config/wireplumber/main.lua.d/51-cd18781y-alsa.lua" ]; then
    pass "用户级 WirePlumber ALSA 配置存在于 $U_HOME/.config/wireplumber/main.lua.d/"
  else
    warn "缺少 $U_HOME/.config/wireplumber/main.lua.d/51-cd18781y-alsa.lua"
  fi
  for usvc in thinksmart-mic.service idle-blanker.service vcnl4200-proximity-daemon.service thinksmart-airplay.service; do
    if [ "$(id -un)" = "$TARGET_USER" ]; then
      XDG_RUNTIME_DIR="/run/user/$U_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$U_UID/bus" systemctl --user is-active --quiet "$usvc" 2>/dev/null
      rc=$?
    elif [ "$(id -u)" -eq 0 ]; then
      runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$U_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$U_UID/bus" systemctl --user is-active --quiet "$usvc" 2>/dev/null
      rc=$?
    else
      warn "无法以当前用户 $(id -un) 无密查询 $TARGET_USER 的用户态服务 $usvc（请以 $TARGET_USER 或 sudo 运行）"
      continue
    fi
    if [ "$rc" -eq 0 ]; then
      pass "用户态服务运行中: $usvc"
    elif [ "$usvc" = "thinksmart-airplay.service" ]; then
      if [ "$(id -un)" = "$TARGET_USER" ]; then
        u_en="$(XDG_RUNTIME_DIR="/run/user/$U_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$U_UID/bus" systemctl --user is-enabled "$usvc" 2>/dev/null || true)"
      else
        u_en="$(runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$U_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$U_UID/bus" systemctl --user is-enabled "$usvc" 2>/dev/null || true)"
      fi
      case "$u_en" in
        disabled|masked)
          pass "用户态服务已按设置关闭: $usvc"
          ;;
        *)
          fail "用户态服务未运行: $usvc"
          ;;
      esac
    else
      fail "用户态服务未运行: $usvc"
    fi
  done
fi

if [ -f /etc/wireplumber/scripts/suspend-node.lua ] && [ -f /etc/wireplumber/main.lua.d/99-cd18781y-alsa.lua ]; then
  pass "已安装系统级 WirePlumber ALSA 保护与防功放休眠脚本"
else
  warn "缺少 /etc/wireplumber/scripts/suspend-node.lua 或 99-cd18781y-alsa.lua"
fi

if [ -f /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/plugins_func/functions/play_music.py ] && grep -q '8091' /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/plugins_func/functions/play_music.py; then
  pass "小智网关已原生合入本地音乐播控与 64kbps 语音增强模块"
else
  warn "小智网关缺少原生本地音乐播控与增强模块"
fi

echo ""
echo "诊断结果：$FAIL 个错误，$WARN 个警告。"
[ "$FAIL" -eq 0 ]
