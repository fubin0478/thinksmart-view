import json
import os
import threading
import time

NATIVE_MERGED_MODULES = {
  "core.utils.opus_encoder_utils": "下发语音 Opus 码率 24k -> 64k",
  "core.utils.util": "audio_to_data 编码器码率 64k + pydub 延迟加载",
  "core.providers.tts.index_stream": "IndexStreamTTS：打断、sentence_id、首包缓冲 5 帧防断音、不走代理、音色名 xiaohe",
  "plugins_func.functions.play_music": "play_music 改为调用本机 QQ 音乐服务 127.0.0.1:8091",
  "core.utils.reply_echo": "回声过滤：喇叭停了之后才说的话不当回声（新闻后回答「详细内容」第一次没反应）",
  "core.providers.asr.openai": "OpenAI 兼容识别：交给本地算力端（LocalASR），不阻塞、有超时、连不上重试一次、不走代理",
  "plugins_func.functions.web_search": "联网搜索：DeepSeek 自带搜索 + 内网搜索服务的秘塔/高德/实时行情（metaso_key、amap_key 在私有配置）",
  "plugins_func.functions.get_news_from_newsnow": "新闻一次列 3 条带编号，第几条讲详情、下一批不重复，详情截 2000 字，原文打不开改用联网搜索（MarkItDown 延迟加载）",
  "core.http_server": "HTTP 服务：Host 防 DNS 重绑定、/kiosk 与 /admin 跨站 Origin 拦截及 6 MiB 请求体上限",
  "core.websocket_server": "WebSocket 服务：握手阶段拦截外部浏览器跨站劫持（CSWSH）与不可信 Host",
  "core.admin.admin_handler": "后台与 Kiosk 接口：精简停用旧独立管理路由，保留本机 /kiosk/* 控制通道",
  "core.connection": "设备连接：仅信任本机反向代理转发头、日志脱敏鉴权请求头、清洗并限制 device-id 长度",
  "core.handle.textHandler.listenMessageHandler": "Listen 消息：容错缺失字段与空唤醒词列表、限制文本长度上限",
  "core.agent.call_names": "呼名回复音频缓存：0700 目录与 0600 唯一临时文件原子落盘及读取体积校验",
  "config.logger": "日志瘦身与脱敏：server.log 轮转收紧至 2MB×2(.gz) 防 eMMC 膨胀，关闭 diagnose 防异常泄露密钥",
  "core.agent.chat_history": "聊天库封顶：WAL 自动检查点收紧至 256 页并自动裁剪已总结超 300 条的旧记录防 eMMC 膨胀",
}
_HUB_STATUS_PATH = "/run/user/1000/xiaozhi-hub-overrides.json"
_LAST_HUB_TRIM_MONO = 0.0


def _trim_hub_heap() -> bool:
    global _LAST_HUB_TRIM_MONO
    now_m = time.monotonic()
    if now_m - _LAST_HUB_TRIM_MONO < 30.0:
        return False
    _LAST_HUB_TRIM_MONO = now_m
    try:
        import ctypes
        import gc
        gc.collect()
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        return True
    except Exception:
        return False


def _init_native_hub_runtime() -> None:
    try:
        data = {
            "mode": "native_merged",
            "pid": os.getpid(),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "modules": {
                k: {"state": "applied", "native": True, "change": desc}
                for k, desc in NATIVE_MERGED_MODULES.items()
            },
        }
        tmp = f"{_HUB_STATUS_PATH}.{os.getpid()}.tmp"
        if not os.path.islink(_HUB_STATUS_PATH):
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    os.fchmod(f.fileno(), 0o644)
                    json.dump(data, f, ensure_ascii=False, indent=1)
                if not os.path.islink(_HUB_STATUS_PATH):
                    os.replace(tmp, _HUB_STATUS_PATH)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        pass
    try:
        _t = threading.Timer(18.0, _trim_hub_heap)
        _t.daemon = True
        _t.name = "hub-postboot-trim"
        _t.start()
    except Exception:
        pass


import sys
import uuid
import signal
import asyncio
from config.config_loader import load_config
from config.logger import setup_logging
from core.utils.util import get_local_ip, check_ffmpeg_installed, is_placeholder_secret
from core.http_server import SimpleHttpServer
from core.websocket_server import WebSocketServer
from core.utils.gc_manager import get_gc_manager

TAG = __name__
logger = setup_logging()


async def wait_for_exit() -> None:
    """阻塞直到收到 Ctrl-C / SIGTERM。"""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            pass


async def main():
    _init_native_hub_runtime()
    if os.environ.get("SKIP_DOCTOR") != "1":
        check_ffmpeg_installed()
    config = await load_config()

    auth_key = config["server"].get("auth_key", "")
    if is_placeholder_secret(auth_key):
        auth_key = str(uuid.uuid4().hex)
    config["server"]["auth_key"] = auth_key

    gc_manager = get_gc_manager(interval_seconds=300)
    await gc_manager.start()

    ws_server = WebSocketServer(config)
    ws_task = asyncio.create_task(ws_server.start())
    http_server = SimpleHttpServer(config)
    http_server.ws_server = ws_server
    http_task = asyncio.create_task(http_server.start())

    port = int(config["server"].get("http_port", 8003))
    logger.bind(tag=TAG).info("管理后台是\t\thttp://{}:{}/admin", get_local_ip(), port)

    server_config = config.get("server", {})
    websocket_port = int(server_config.get("port", 8000)) if isinstance(server_config, dict) else 8000
    logger.bind(tag=TAG).debug("Websocket地址是\tws://{}:{}/xiaozhi/v1/", get_local_ip(), websocket_port)

    exit_task = asyncio.create_task(wait_for_exit())
    fatal_exc = None
    try:
        done, _ = await asyncio.wait(
            [exit_task, ws_task, http_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not exit_task.done():
            for srv_task in (ws_task, http_task):
                if srv_task in done:
                    fatal_exc = srv_task.exception() or RuntimeError("服务监听任务意外退出")
                    logger.bind(tag=TAG).error(f"核心监听任务异常终止: {fatal_exc}")
                    break
    except asyncio.CancelledError:
        print("任务被取消，清理资源中...")
    finally:
        await gc_manager.stop()
        for t in (exit_task, ws_task, http_task):
            if t and not t.done():
                t.cancel()
        await asyncio.wait(
            [t for t in (exit_task, ws_task, http_task) if t],
            timeout=3.0,
            return_when=asyncio.ALL_COMPLETED,
        )
        print("服务器已关闭，程序退出。")
    if fatal_exc is not None:
        raise fatal_exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("手动中断，程序终止。")
