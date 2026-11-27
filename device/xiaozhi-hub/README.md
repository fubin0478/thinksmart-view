# ThinkSmart View 小智语音网关服务（自主维护版）

本目录为集成至 `thinksmart-view` 主仓库后的本地小智语音网关（`xiaozhi-hub`）原生源码（保留原 MIT 许可证声明）。

## 已原生合入的 23 项增强与安全模块

原先通过 `/opt/thinksmart-addons/xiaozhi-hub-overrides` 外挂注入的全部 23 个模块与启动后堆内存回收现已直接合入 `main/xiaozhi-server/` 原生源码：

1. **音频与交互体验**：64kbps Opus 高清语音编码、`IndexStreamTTS` 首包防断音缓冲与打断同步、喇叭停后回声误判修复、OpenAI 兼容本地算力 ASR 重试与免代理直连、呼名回复音频缓存原子写入；
2. **整机生态插件**：`play_music` 直连本机 `:8091` QQ 音乐服务、DeepSeek + 秘塔/高德/实时行情联网搜索、新闻编号精读与防重 + DNS 重绑定 SSRF 防护、RAGFlow TLS 强校验；
3. **安全与全栈瘦身**：HTTP/WebSocket 防 DNS 重绑定与防 CSWSH 跨站劫持、OTA/视觉接口鉴权与流式限流、管理后台登录按源限流与密码长度上限、`chat.db` WAL 自动检查点与旧对话安全裁剪、日志 `2 MB x 2` 压缩轮转、会话结束及启动 18 秒 `malloc_trim` 堆回收；
4. **彻底切断第三方更新**：`core/admin/updater.py` 与 `scripts/update.sh` 已锁定为本地自主维护模式，永不连接外部 GitHub 拉取或重置代码。

## 运行时数据与模型约定（不纳入源码库）

- 私有运行配置：`main/xiaozhi-server/data/.config.yaml`（请参考同目录 `.config.example.yaml` 模板配置，严禁将真实密钥提交入库）
- 本地语音/VAD 模型：`main/xiaozhi-server/models/`（约 175 MB，留存设备端本地磁盘）
- Python 虚拟环境：`main/xiaozhi-server/.venv/`（留存设备端本地磁盘）
