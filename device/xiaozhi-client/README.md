# ThinkSmart View 语音客户端（自主维护版 `1.0.0-self`）

本目录为集成至 `thinksmart-view` 主仓库后的小智语音客户端原生源码（保留原 MIT 许可证声明），已将原先通过外挂 `sitecustomize.py` 注入的以下能力直接合入 `thinksmart_client/bridge.py`：

- QQ 音乐 / 白噪音 / 伴学音频的 48kHz 立体声 PCM 实时混音与 WebRTC 线性回声消除参考流馈入；
- 11 项整机原生语音 MCP 工具（音乐搜索与播控、电台、睡眠定时、桌面倒数日、服务器监控查询、AI 拍题讲解/专注共写/自助听写）；
- 闹钟响铃期间的硬件扬声器音量独占协同与启动后 20 秒 `malloc_trim` 堆内存回收；
- 永久移除第三方在线检查与自动更新链路（`update.py` 锁定为本地自主维护态）。

## 设备端运行目录与模型说明

- 运行路径：`/opt/thinksmart/client/1.0.0-self`（`/opt/thinksmart/client/current` 软链指向该目录）
- Python 环境：复用 `/opt/thinksmart/envs/` 既有虚拟环境软链 `.venv`
- 本地唤醒词模型：`models/kws-zh/` 下的 ONNX 权重文件（约 13 MB）保留在设备本地磁盘，不纳入同步源码库。
