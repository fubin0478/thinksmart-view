import os
import time
import json
import uuid
import random
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.utils.util import audio_to_data
from core.providers.tts.dto.dto import SentenceType
from core.utils.wakeup_word import WakeupWordsConfig
from core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
from core.utils.util import remove_punctuation_and_length, opus_datas_to_wav_bytes
from core.providers.tools.device_mcp import MCPClient, send_mcp_initialize_message

TAG = __name__

WAKEUP_CONFIG = {
    "refresh_time": 300,
    "responses": [
        "我一直都在呢，您请说。",
        "在的呢，请随时吩咐我。",
        "来啦来啦，请告诉我吧。",
        "您请说，我正听着。",
        "请您讲话，我准备好了。",
        "请您说出指令吧。",
        "我认真听着呢，请讲。",
        "请问您需要什么帮助？",
        "我在这里，等候您的指令。",
    ],
}

# 创建全局的唤醒词配置管理器
wakeup_words_config = WakeupWordsConfig()

# 用于防止并发调用wakeupWordsResponse的锁
_wakeup_response_lock = asyncio.Lock()


async def handleHelloMessage(conn: "ConnectionHandler", msg_json):
    """处理hello消息"""
    audio_params = msg_json.get("audio_params")
    if isinstance(audio_params, dict) and audio_params:
        format = audio_params.get("format")
        conn.logger.bind(tag=TAG).debug(f"客户端音频格式: {format}")
        conn.audio_format = format
        srv_ap = dict(conn.welcome_msg.get("audio_params") or {})
        if format in ("opus", "pcm"):
            srv_ap["format"] = format
        conn.welcome_msg = {**conn.welcome_msg, "audio_params": srv_ap}
    features = msg_json.get("features")
    if isinstance(features, dict) and features:
        conn.logger.bind(tag=TAG).debug(f"客户端特性: {features}")
        conn.features = features
        if features.get("mcp"):
            conn.logger.bind(tag=TAG).debug("客户端支持MCP")
            conn.mcp_client = MCPClient()
        if features.get("aec"):
            conn.logger.bind(tag=TAG).debug("客户端启用了服务端AEC")
            conn.client_aec = True

    welcome = conn.welcome_msg
    if isinstance(features, dict) and features.get("vad_end"):
        # [xiaozhi-hub] 告诉客户端这个网关会发 VAD 事件（含"什么也没识别出来"），它不必干等兜底超时
        welcome = {**welcome, "features": {**(welcome.get("features") or {}), "vad_end": True}}
    await conn.websocket.send(json.dumps(welcome))

    # The device waits for the server hello before processing MCP messages.
    if isinstance(features, dict) and features.get("mcp"):
        asyncio.create_task(send_mcp_initialize_message(conn))


async def checkWakeupWords(conn: "ConnectionHandler", text):
    from core.utils import turn_gen
    gen = turn_gen.current(conn)
    enable_wakeup_words_response_cache = (conn.config or {}).get(
        "enable_wakeup_words_response_cache", False
    )

    # 等待tts初始化，最多等待3秒
    start_time = time.time()
    while time.time() - start_time < 3:
        if conn.tts:
            break
        await asyncio.sleep(0.01)
    else:
        return False

    if not enable_wakeup_words_response_cache:
        return False

    _, filtered_text = remove_punctuation_and_length(text)
    wakeup_words = conn.config.get("wakeup_words") or ()
    if not isinstance(wakeup_words, (list, tuple, set)) or filtered_text not in wakeup_words:
        return False

    # 获取当前音色
    voice = getattr(conn.tts, "voice", "default")
    if not voice:
        voice = "default"

    # 获取唤醒词回复配置
    response = wakeup_words_config.get_wakeup_response(voice)
    default_resp = {
        "voice": "default",
        "file_path": "config/assets/wakeup_words_short.wav",
        "time": 0,
        "text": "我在这里哦！",
    }
    if not response or not response.get("file_path"):
        response = default_resp

    # 先解码音频数据再发 tts start，避免文件竞争失败时设备卡在 speaking
    is_opus = getattr(conn, "audio_format", "opus") != "pcm"
    out_sr = int(getattr(conn, "sample_rate", 24000) or 24000)
    try:
        opus_packets = await audio_to_data(
            response.get("file_path"),
            is_opus=is_opus,
            use_cache=True,
            sample_rate=out_sr,
        )
    except Exception:
        response = default_resp
        opus_packets = await audio_to_data(response.get("file_path"), is_opus=is_opus, use_cache=True, sample_rate=out_sr)

    if turn_gen.stale(conn, gen) or (hasattr(conn, "_is_closing") and conn._is_closing()):
        return False

    # 和 receiveAudioHandle.mark_just_woken_up 一样，这里直接写避免循环导入
    conn.just_woken_up = True
    conn.just_woken_up_at = time.monotonic()
    sid = str(uuid.uuid4().hex)
    conn.sentence_id = sid
    conn.client_abort = False
    conn.client_is_speaking = True
    await send_tts_message(conn, "start")

    conn.logger.bind(tag=TAG).info(f"播放唤醒词回复: {response.get('text')}")
    await sendAudioMessage(conn, SentenceType.FIRST, opus_packets, response.get("text"), sentence_id=sid)
    await sendAudioMessage(conn, SentenceType.LAST, [], None, sentence_id=sid)

    # 补充配对对话（被打断或已被新轮次取代则不写入）
    if not conn.client_abort and conn.sentence_id == sid:
        conn.dialogue.put(Message(role="user", content=text))
        conn.dialogue.put(Message(role="assistant", content=response.get("text")))

    # 检查是否需要更新唤醒词回复
    if time.time() - response.get("time", 0) > WAKEUP_CONFIG["refresh_time"]:
        if not _wakeup_response_lock.locked():
            asyncio.create_task(wakeupWordsResponse(conn))
    return True


async def wakeupWordsResponse(conn: "ConnectionHandler"):
    if not conn.tts or _wakeup_response_lock.locked():
        return

    try:
        async with _wakeup_response_lock:
            # 从预定义回复列表中随机选择一个回复
            result = random.choice(WAKEUP_CONFIG["responses"])
            if not result or len(result) == 0:
                return

            # 生成TTS音频
            tts_result = await asyncio.to_thread(conn.tts.to_tts, result)
            if isinstance(tts_result, str):
                tmp_tts_file = tts_result
                try:
                    tts_result = await audio_to_data(
                        tmp_tts_file,
                        is_opus=True,
                        use_cache=False,
                        sample_rate=int(getattr(conn, "sample_rate", 24000) or 24000),
                    )
                finally:
                    try:
                        os.remove(tmp_tts_file)
                    except OSError:
                        pass
            if not tts_result:
                return

            # 获取当前音色
            voice = getattr(conn.tts, "voice", "default")

            # 使用链接的sample_rate
            wav_bytes = opus_datas_to_wav_bytes(tts_result, sample_rate=conn.sample_rate)
            file_path = wakeup_words_config.generate_file_path(voice)
            tmp_path = f"{file_path}.{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp_path, "wb") as f:
                    f.write(wav_bytes)
                os.replace(tmp_path, file_path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
            # 更新配置
            wakeup_words_config.update_wakeup_response(voice, file_path, result)
    except Exception as e:
        if getattr(conn, "logger", None):
            conn.logger.bind(tag=TAG).warning(f"生成唤醒回复音频失败: {e}")
