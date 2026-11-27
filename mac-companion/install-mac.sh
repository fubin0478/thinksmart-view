#!/usr/bin/env bash
# Apple Silicon Mac 本地算力配套端环境初始化脚本
# 自动创建虚拟环境、安装依赖、初始化网关 Token / 学习陪伴密钥，并生成 LaunchAgent 配置（不自动覆盖已有密钥）
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "错误：本脚本仅适用于 macOS (Apple Silicon)。" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VOICE_DIR="$SCRIPT_DIR/voice-server"
VISION_DIR="$SCRIPT_DIR/study-vision"

PY_BIN=""
for cand in python3.12 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY_BIN="$(command -v "$cand")"
      break
    fi
  fi
done
if [ -z "$PY_BIN" ]; then
  echo "错误：未找到 Python >= 3.10（建议 brew install python@3.12）。" >&2
  exit 1
fi

echo "==> [1/4] 使用 $PY_BIN 初始化虚拟环境: $VOICE_DIR/.venv"
if [ ! -x "$VOICE_DIR/.venv/bin/python" ]; then
  "$PY_BIN" -m venv "$VOICE_DIR/.venv"
fi
"$VOICE_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$VOICE_DIR/.venv/bin/pip" install -r "$VOICE_DIR/requirements.txt"

echo "==> [2/4] 初始化本地配置文件与密钥（已存在则跳过）..."
mkdir -p "$HOME/.config/thinksmart-voice" "$HOME/.config/study-vision"
chmod 700 "$HOME/.config/thinksmart-voice" "$HOME/.config/study-vision"

if [ ! -f "$HOME/.config/thinksmart-voice/public_gateway.json" ]; then
  GW_TOKEN="$(openssl rand -hex 16)"
  umask 077
  cat > "$HOME/.config/thinksmart-voice/public_gateway.json" <<EOF
{
  "port": 8080,
  "token": "$GW_TOKEN"
}
EOF
  echo "    已生成网关配置: ~/.config/thinksmart-voice/public_gateway.json"
fi

if [ ! -f "$HOME/.config/study-vision/secret" ]; then
  umask 077
  openssl rand -hex 32 > "$HOME/.config/study-vision/secret"
  chmod 600 "$HOME/.config/study-vision/secret"
  echo "    已生成学习陪伴共享密钥: ~/.config/study-vision/secret（请同步复制到设备 /var/lib/thinksmart-study/secret）"
fi

[ -f "$VOICE_DIR/asr_context.txt" ] || cp "$VOICE_DIR/asr_context.example.txt" "$VOICE_DIR/asr_context.txt"
if [ ! -f "$HOME/.config/thinksmart-voice/homophones.json" ]; then
  cp "$VOICE_DIR/homophones.example.json" "$HOME/.config/thinksmart-voice/homophones.json"
  chmod 600 "$HOME/.config/thinksmart-voice/homophones.json"
fi
if [ ! -f "$VOICE_DIR/voices/voices.json" ] && [ -f "$VOICE_DIR/voices/voices.example.json" ]; then
  cp "$VOICE_DIR/voices/voices.example.json" "$VOICE_DIR/voices/voices.json"
fi
mkdir -p "$VOICE_DIR/models/hy-mt2" "$VISION_DIR/models" "$HOME/Library/Logs/xiaozhi-voice" "$HOME/Library/Logs/study-vision" "$HOME/Library/LaunchAgents"

echo "==> [3/4] 渲染 LaunchAgent 模板到 ~/Library/LaunchAgents/（仅当目标不存在或传入 --force-plist 时写入）..."
FORCE_PLIST=0
[ "${1:-}" = "--force-plist" ] && FORCE_PLIST=1

for tpl in \
  "$VOICE_DIR/launchd/"*.plist.example \
  "$VISION_DIR/launchd/"*.plist.example; do
  base="$(basename "$tpl" .example)"
  dst="$HOME/Library/LaunchAgents/$base"
  if [ ! -f "$dst" ] || [ "$FORCE_PLIST" -eq 1 ]; then
    "$PY_BIN" - "$tpl" "$dst" "$REPO_ROOT" "$HOME" <<'PY'
import pathlib, plistlib, sys
tpl, dst, repo_root, home_dir = sys.argv[1:5]
def sub(v):
    if isinstance(v, str):
        return v.replace("/Users/YOUR_USERNAME/thinksmart-view", repo_root).replace("/Users/YOUR_USERNAME", home_dir)
    if isinstance(v, list):
        return [sub(x) for x in v]
    if isinstance(v, dict):
        return {k: sub(val) for k, val in v.items()}
    return v
data = plistlib.loads(pathlib.Path(tpl).read_bytes())
with open(dst, "wb") as f:
    plistlib.dump(sub(data), f, sort_keys=True)
PY
    echo "    已生成: $dst"
  else
    echo "    保留已有: $dst（如需重生成请传 --force-plist）"
  fi
done

echo "==> [4/4] 完成！后续步骤："
echo "  1. 将语音模型放入 $VOICE_DIR/models/："
echo "     - Qwen3-ASR-1.7B-8bit/"
echo "     - Qwen3-TTS-12Hz-1.7B-Base-8bit/"
echo "     （可选本地同传翻译：$VOICE_DIR/models/hy-mt2/Hy-MT2-1.8B-Q8_0.gguf）"
echo "     并在 $VOICE_DIR/voices/ 放置 voices.json 及对应参考音色 WAV（示例见 voices.example.json）"
echo "  2. 如需启用学习陪伴本地视觉推理（可选）：brew install llama.cpp，并将以下模型放入 $VISION_DIR/models/："
echo "     - Qwen3.5-9B-Q8_0.gguf（16GB 机型可用 Qwen3.5-9B-Q4_K_M.gguf）"
echo "     - mmproj-F16.gguf"
echo "  3. 使用管理脚本启停服务: bash $VOICE_DIR/manage.sh start all && bash $VOICE_DIR/manage.sh status"
