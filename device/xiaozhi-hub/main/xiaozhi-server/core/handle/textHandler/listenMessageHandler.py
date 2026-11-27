import time
import uuid
import queue
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.utils.dialogue import Message
from core.providers.asr.base import send_vad_event
from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat, mark_just_woken_up
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType


TAG = __name__
MAX_LISTEN_TEXT_CHARS = 4096


class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        mode = msg_json.get("mode")
        if isinstance(mode, str) and 0 < len(mode) <= 32:
            conn.client_listen_mode = mode
            conn.logger.bind(tag=TAG).debug(
                f"客户端拾音模式：{conn.client_listen_mode}"
            )
        state = msg_json.get("state")
        if state == "start":
            # 设备从播放模式切回录音模式,清除刚唤醒丢帧窗口、队列旧帧及所有音频状态
            conn.just_woken_up = False
            while True:
                try:
                    conn.asr_audio_queue.get_nowait()
                except queue.Empty:
                    break
            conn.reset_audio_states()
        elif state == "stop":
            # 收到stop但asr未初始化，跳过处理
            if conn.asr is None:
                return

            conn.client_voice_stop = True
            if conn.asr.interface_type == InterfaceType.STREAM:
                # 流式模式下，发送结束请求
                try:
                    stop_coro = conn.asr._send_stop_request(conn)
                except TypeError:
                    stop_coro = conn.asr._send_stop_request()
                asyncio.create_task(stop_coro)
            else:
                # 非流式模式：排空仍在队列中的尾帧后触发ASR识别或返回空事件
                while True:
                    try:
                        pkt = conn.asr_audio_queue.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(pkt, (bytes, bytearray)) and (
                        conn.client_listen_mode == "manual" or len(conn.asr_audio) > 0
                    ):
                        conn.asr_audio.append(pkt)
                had_voice = bool(getattr(conn, "client_have_voice", False))
                segments = list(getattr(conn, "asr_segments", None) or [])
                seg_start = int(getattr(conn, "asr_segment_start", 0) or 0)
                voice_end = int(getattr(conn, "asr_voice_end", 0) or 0)
                asr_audio_task = conn.asr_audio.copy()
                conn.reset_audio_states()

                if len(asr_audio_task) > 0 and (conn.client_listen_mode == "manual" or had_voice):
                    if segments:
                        total_bytes = sum(len(f) for f in asr_audio_task)
                        done_bytes = (
                            total_bytes
                            if (voice_end and voice_end <= seg_start)
                            else sum(len(f) for f in asr_audio_task[:seg_start])
                        )
                        await conn.asr.handle_voice_stop(
                            conn, asr_audio_task, segments=segments, segmented_bytes=done_bytes
                        )
                    else:
                        await conn.asr.handle_voice_stop(conn, asr_audio_task)
                else:
                    await send_vad_event(conn, "empty")
        elif state == "detect":
            conn.client_have_voice = False
            conn.reset_audio_states()
            original_text = msg_json.get("text")
            if isinstance(original_text, str) and original_text.strip():
                if len(original_text) > MAX_LISTEN_TEXT_CHARS:
                    original_text = original_text[:MAX_LISTEN_TEXT_CHARS]
                conn.last_activity_time = time.time() * 1000
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                # 检查是否是设备呼叫指令 [device_call]
                if original_text.startswith("[device_call]"):
                    # 提取 tag 后的文本
                    call_text = original_text[len("[device_call]"):].strip()
                    conn.logger.bind(tag=TAG).info(f"收到设备呼叫指令: {call_text}")

                    # 标记为来电接听模式
                    conn.incoming_call = True

                    # 等待tts初始化，最多等待3秒
                    start_time = time.time()
                    while time.time() - start_time < 3:
                        if conn.tts:
                            break
                        await asyncio.sleep(0.1)

                    if not conn.tts:
                        return

                    # 准备开始新会话
                    conn.client_abort = False
                    conn.sentence_id = uuid.uuid4().hex
                    await send_stt_message(conn, call_text)

                    conn.tts.store_tts_text(conn.sentence_id, call_text)
                    conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=conn.sentence_id, sentence_type=SentenceType.FIRST, content_type=ContentType.ACTION))
                    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=call_text)
                    conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=conn.sentence_id, sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))

                    # 添加到对话历史，让模型理解上下文
                    m_call = Message(role="assistant", content=call_text)
                    m_call.keep_head = True
                    conn.dialogue.put(m_call)
                    return

                # 识别是否是唤醒词
                wakeup_words = conn.config.get("wakeup_words") or ()
                is_wakeup_words = (
                    filtered_text in wakeup_words
                    if isinstance(wakeup_words, (list, tuple, set))
                    else False
                )
                # 是否开启唤醒词回复
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup_words and not enable_greeting:
                    # 如果是唤醒词，且关闭了唤醒词回复，就不用回答
                    if conn.client_is_speaking:
                        from core.handle.abortHandle import handleAbortMessage
                        await handleAbortMessage(conn)
                    conn.client_abort = False
                    conn.sentence_id = str(uuid.uuid4().hex)
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup_words:
                    mark_just_woken_up(conn)
                    await startToChat(conn, original_text)
                else:
                    mark_just_woken_up(conn)
                    # 否则需要LLM对文字内容进行答复
                    await startToChat(conn, original_text)
