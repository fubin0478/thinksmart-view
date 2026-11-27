# ThinkSmart View — Apple Silicon Mac 本地算力伴侣 (`mac-companion`)

本目录包含运行在 Apple Silicon Mac（M 系列芯片）上的本地语音与多模态视觉推理伴侣服务，全部为**纯源码与脱敏模板**（不含 `.venv`、GGUF/MLX 模型权重、个人克隆录音或任何密钥）。

## 组件与默认端口

| 服务 | LaunchAgent 模板 | 默认监听 | 说明 |
|---|---|---|---|
| 主语音引擎 (`voice-server/server.py`) | `com.thinksmart.voice` | `0.0.0.0:8010` | MLX 本地 `Qwen3-ASR-1.7B-8bit` 语音识别 + `Qwen3-TTS-12Hz-1.7B-Base-8bit` 多风格克隆流式合成（含编译加速、尾标点恢复、Metal 权重常驻保鲜与本地 `study-vision` GPU 抢占协同） |
| 溢出副 TTS (`voice-server/server.py`) | `com.thinksmart.voice-tts2` | `127.0.0.1:8011` | `ASR_DISABLED=1` 纯 TTS 副进程；当 `:8010` 正在合成时自动分流并发请求，默认空闲 `600s` 自动休眠释放显存（大内存机型可设 `TTS_IDLE_SLEEP_SECONDS=0` 常驻） |
| 令牌网关 (`voice-server/public_gateway.py`) | `com.thinksmart.gateway` | `0.0.0.0:8080` | 仅暴露路径令牌保护下的模型接口（`/<token>/tts`、`/v1/audio/*`、`/translation/v1/*`、`/study/v1`），绝不暴露管理或通用代理路由 |
| 本地同传模型（可选） | `com.thinksmart.hy-mt2` | `127.0.0.1:8082` | `llama-server` 运行 `Hy-MT2-1.8B-Q8_0.gguf`，提供超低延迟本地中英/多语种同传翻译，空闲 `600s` 自动休眠 |
| 伴学视觉模型（可选） | `com.thinksmart.study-vision-llm` | `127.0.0.1:8083` | `llama-server` 运行 `Qwen3.5-9B-Q8_0.gguf` + `mmproj-F16.gguf`（32GB 内存推荐；16GB 机型可用 `Q4_K_M`），开启 `-fa on` 与前缀 KV 缓存 |
| 伴学加密桥接 (`study-vision/vision_bridge.py`) | `com.thinksmart.study-vision-bridge` | `127.0.0.1:8084` | 与设备端 `thinksmart-study.py` 共享 `HMAC-SHA256 + SHAKE256` 端到端密封协议（防重放、内存即焚、重力感应 `sips` 批量扶正） |

## 单机部署 vs 双 Mac 分离部署

1. **单台 Apple Silicon Mac 一体化运行**：
   - 执行 `bash mac-companion/install-mac.sh` 自动创建虚拟环境、生成随机网关 Token（`~/.config/thinksmart-voice/public_gateway.json`）与伴学共享密钥（`~/.config/study-vision/secret`，需同步至设备 `/var/lib/thinksmart-study/secret`），并渲染 `~/Library/LaunchAgents/com.thinksmart.*.plist`。
   - 单机同跑语音与伴学视觉时，`server.py` 与 `vision_bridge.py` 会通过本地文件锁与 GPU 租约自动礼让/挂起背景视觉推理，确保实时语音零卡顿。
2. **双 Mac 局域网算力分离（如 14 寸跑实时语音 + 16 寸 32GB 专跑 `Qwen3.5-9B` 视觉）**：
   - 在专跑视觉的副机启动 `com.thinksmart.study-vision-llm`（监听局域网或开启 `--api-key`）；
   - 在主伴侣机的 `com.thinksmart.study-vision-bridge.plist` 中将 `STUDY_VISION_LLM` 指向副机 `http://<VISION_MAC_LAN_IP>:8083`（可配 `STUDY_VISION_LLM_FALLBACK=http://<VISION_MAC_HOSTNAME>.local:8083` 与 `STUDY_VISION_VOICE_WAIT_MAX=0`），主伴侣机的 `com.thinksmart.voice.plist` 设 `STUDY_VISION_PREEMPT=0`，两机 Metal GPU 完全独立零争抢。

## 日常运维

```bash
# 查看所有本地伴侣服务健康状态
bash mac-companion/voice-server/manage.sh status

# 启动 / 重启 / 停止指定或全部服务
bash mac-companion/voice-server/manage.sh restart all
bash mac-companion/voice-server/manage.sh logs vision
```
