import os
import time
import queue
import threading
import aiohttp
import asyncio
import requests
import traceback
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.providers.tts.base import TTSProviderBase
from core.utils import opus_encoder_utils, textUtils
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()

# [ThinkSmart] 连接复用：以前每句话都 asyncio.run（新事件循环）+ 新 ClientSession，每句都重新 TCP 握手，
# 偶发连不上就卡 2 秒。现在每个 TTS 线程固定一个事件循环和一个连接池，一轮回答里的句子复用同一条连接。
# 网关是 uvicorn，空闲 5 秒就关连接，这里 4 秒就主动放掉，避免拿到对方刚关掉的旧连接。
KEEPALIVE_SECONDS = 4.0
_local = threading.local()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        cfg = config if isinstance(config, dict) else {}
        super().__init__(cfg, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        if cfg.get("private_voice"):
            self.voice = cfg.get("private_voice")
        else:
            self.voice = cfg.get("voice", "xiaohe")
        self.api_url = cfg.get("api_url", "http://127.0.0.1:8010/tts")
        self.audio_format = "pcm"
        self.before_stop_play_files = []

        # 创建Opus编码器 需注意接口返回的采样率为24000
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=24000, channels=1, frame_size_ms=60
        )

        # PCM缓冲区
        self.pcm_buffer = bytearray()

    def tts_text_priority_thread(self):
        """流式文本处理线程"""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if self.conn.client_abort:
                    logger.bind(tag=TAG).info("收到打断信息，终止TTS文本处理线程")
                    continue
                if message.sentence_id != self.conn.sentence_id:
                    continue

                if message.sentence_type == SentenceType.FIRST:
                    # 初始化参数
                    self.current_sentence_id = message.sentence_id
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.is_first_sentence = True
                    self.tts_audio_first_sentence = True
                    self.before_stop_play_files.clear()
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text)

                elif ContentType.FILE == message.content_type:
                    logger.bind(tag=TAG).info(
                        f"添加到音频文件列表: {message.content_file}"
                    )
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio_data: self.handle_audio_file(audio_data, message.content_detail)
                        )

                if message.sentence_type == SentenceType.LAST:
                    # 处理剩余文本
                    self._process_remaining_text_stream(True)

            except queue.Empty:
                continue
            except Exception as e:
                tb_ln = e.__traceback__.tb_lineno if getattr(e, "__traceback__", None) is not None else 0
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败, 类型: {type(e).__name__}, 行号: {tb_ln}"
                )
        self._close_thread_loop()  # 连接结束：关掉这个线程的连接池和事件循环

    def _thread_loop(self):
        holder = globals().get("_local") or self
        loop = getattr(holder, "loop", None)
        if loop is None or loop.is_closed():
            loop = holder.loop = asyncio.new_event_loop()
            holder.session = None
            holder.session_loop = None
        return loop

    async def _thread_session(self):
        running_loop = asyncio.get_running_loop()
        holder = globals().get("_local") or self
        session = getattr(holder, "session", None)
        sess_loop = getattr(holder, "session_loop", None)
        if session is None or session.closed or sess_loop is not running_loop:
            if session is not None and not session.closed and sess_loop is not None and not sess_loop.is_closed():
                try:
                    await session.close()
                except Exception:
                    pass
            ka = globals().get("KEEPALIVE_SECONDS", 4.0)
            session = aiohttp.ClientSession(
                trust_env=False, connector=aiohttp.TCPConnector(limit=4, keepalive_timeout=ka))
            holder.session = session
            holder.session_loop = running_loop
        return session

    def _close_thread_loop(self):
        holder = globals().get("_local") or self
        loop = getattr(holder, "loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            session = getattr(holder, "session", None)
            if session is not None and not session.closed:
                loop.run_until_complete(session.close())
        except Exception:
            pass
        finally:
            loop.close()
            holder.loop = holder.session = holder.session_loop = None

    def _process_remaining_text_stream(self, is_last=False):
        """处理剩余的文本并生成语音
        Returns:
            bool: 是否成功处理了文本
        """
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                self.processed_chars = len(full_text)
            else:
                self._process_before_stop_play_files()
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        original_text = text
        try:
            clean_text = MarkdownCleaner.clean_markdown(text)
            speak_text = clean_text
            if self._correct_words_pattern:
                speak_text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], clean_text)
            self._display_text = clean_text
            try:
                success = self._thread_loop().run_until_complete(self.text_to_speak(speak_text, is_last))
            finally:
                self._display_text = None
            if success:
                logger.bind(tag=TAG).info(f"语音生成成功: {original_text}")
            elif speak_text and speak_text.strip() and not (self.conn and self.conn.client_abort):
                logger.bind(tag=TAG).warning("本段语音未完成，不记录为生成成功")
        except Exception as exc:
            logger.bind(tag=TAG).error(f"语音生成失败，类型: {type(exc).__name__}")
            if is_last and not (self.conn and self.conn.client_abort):
                try:
                    self._process_before_stop_play_files()
                except Exception:
                    pass
        return None

    async def text_to_speak(self, text, is_last):
        """Retry a transient failure once, only before any audio has been emitted."""
        _dt = getattr(self, "_display_text", None)
        display_text = _dt if isinstance(_dt, str) and _dt else text
        payload = {"text": text, "character": self.voice}
        sid = getattr(self, "current_sentence_id", None)
        frame_bytes = int(self.opus_encoder.sample_rate * self.opus_encoder.channels
                          * self.opus_encoder.frame_size_ms / 1000 * 2)
        audio_started = False

        def stopped():
            stop_ev = getattr(self.conn, "stop_event", None) if self.conn else None
            ev_set = callable(getattr(stop_ev, "is_set", None)) and stop_ev.is_set() is True
            return bool(self.conn and (self.conn.client_abort or ev_set or
                (sid is not None and getattr(self.conn, "sentence_id", sid) != sid)))

        def emit(frame, end_of_stream=False):
            nonlocal audio_started
            if not audio_started:
                self.tts_audio_queue.put((SentenceType.FIRST, [], display_text, sid))
                audio_started = True
            self.opus_encoder.encode_pcm_to_opus_stream(
                frame, end_of_stream=end_of_stream, callback=self.handle_opus)

        if stopped():
            return False
        if not text or not text.strip():
            if is_last:
                self._process_before_stop_play_files()
            return False
        for attempt in range(2):
            if stopped():
                return False
            start, phase, received, retryable = time.monotonic(), "headers", 0, False
            try:
                timeout = aiohttp.ClientTimeout(total=self.tts_timeout,
                    connect=min(5.0, self.tts_timeout), sock_connect=min(2.0, self.tts_timeout),
                    sock_read=self.tts_timeout)  # TCP 握手正常约 0.1-0.2 秒；偶发连不上时 2 秒就重试，不再干等 5 秒
                session = await self._thread_session()
                async with session.post(self.api_url, json=payload, timeout=timeout) as resp:
                    if resp.status != 200:
                        retryable = resp.status == 429 or resp.status >= 500
                        raise RuntimeError(f"HTTP {resp.status}")
                    phase = "audio"
                    self.pcm_buffer.clear()
                    if hasattr(self.opus_encoder, "reset_state"):
                        self.opus_encoder.reset_state()
                    prebuffer_bytes, prebuffered = frame_bytes * 5, False
                    async for chunk in resp.content.iter_any():
                        if stopped():
                            self.pcm_buffer.clear()
                            return False
                        data = chunk[0] if isinstance(chunk, (list, tuple)) else chunk
                        if not data:
                            continue
                        received += len(data)
                        self.pcm_buffer.extend(data)
                        if not prebuffered and len(self.pcm_buffer) < prebuffer_bytes:
                            continue
                        prebuffered = True
                        while len(self.pcm_buffer) >= frame_bytes:
                            frame = bytes(self.pcm_buffer[:frame_bytes])
                            del self.pcm_buffer[:frame_bytes]
                            emit(frame)
                    if stopped():
                        self.pcm_buffer.clear()
                        return False
                    # Short utterances still flush without waiting for the five-frame buffer.
                    while len(self.pcm_buffer) >= frame_bytes:
                        frame = bytes(self.pcm_buffer[:frame_bytes])
                        del self.pcm_buffer[:frame_bytes]
                        emit(frame)
                    even_len = len(self.pcm_buffer) & ~1
                    if even_len:
                        emit(bytes(self.pcm_buffer[:even_len]), end_of_stream=True)
                    elif len(getattr(self.opus_encoder, "buffer", ())) > 0:
                        emit(b"", end_of_stream=True)
                    self.pcm_buffer.clear()
                    if not audio_started:
                        retryable = True
                        raise RuntimeError("empty_audio")
                    if is_last:
                        self._process_before_stop_play_files()
                    return True
            except Exception as exc:
                self.pcm_buffer.clear()
                if hasattr(self.opus_encoder, "reset_state"):
                    self.opus_encoder.reset_state()
                transient = retryable or isinstance(exc, (
                    asyncio.TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError))
                logger.bind(tag=TAG).error(
                    f"TTS失败: {type(exc).__name__}, 阶段={phase}, 耗时={time.monotonic()-start:.2f}s, "
                    f"收到PCM={received}字节, 已开始音频={audio_started}, 尝试={attempt+1}")
                if attempt == 0 and transient and not audio_started and not stopped():
                    logger.bind(tag=TAG).warning("TTS瞬时故障且尚未发送音频，重试一次")
                    await asyncio.sleep(0.25)
                    continue
                # A failed non-final segment must not close the whole conversation.
                if is_last and not stopped():
                    self._process_before_stop_play_files()
                return False
        return False


    def audio_to_pcm_data_stream(
        self, audio_file_path, callback=None
    ):
        """音频文件转换为PCM编码，使用24kHz采样率"""
        from core.utils.util import audio_to_data_stream
        return audio_to_data_stream(audio_file_path, is_opus=False, callback=callback, sample_rate=24000, opus_encoder=None)

    def audio_to_opus_data_stream(
        self, audio_file_path, callback=None
    ):
        """音频文件转换为Opus编码，使用24kHz采样率和独立编码器避免并发冲突"""
        from core.utils.util import audio_to_data_stream
        return audio_to_data_stream(audio_file_path, is_opus=True, callback=callback, sample_rate=24000, opus_encoder=None)

    async def close(self):
        """资源清理"""
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()

    def to_tts(self, text: str) -> list:
        """非流式TTS处理，用于测试、预热缓存及保存音频文件的场景（使用独立编码器保证线程安全）"""
        start_time = time.time()
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], text)
        if not text or not text.strip():
            return []

        payload = {"text": text, "character": self.voice}

        try:
            req_timeout = (min(2.0, self.tts_timeout), self.tts_timeout)
            with requests.post(
                self.api_url, json=payload, timeout=req_timeout, stream=True, proxies={"http": None, "https": None}
            ) as response:
                if response.status_code != 200:
                    err_preview = ""
                    if hasattr(response, "iter_content"):
                        err_preview = next(response.iter_content(chunk_size=512), b"").decode("utf-8", errors="replace")[:200]
                    else:
                        err_preview = (response.text or "")[:200]
                    logger.bind(tag=TAG).error(f"TTS请求失败: {response.status_code}, {err_preview}")
                    return []

                if hasattr(response, "iter_content"):
                    pcm_buf = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            pcm_buf.extend(chunk)
                            if len(pcm_buf) > 16 * 1024 * 1024:
                                raise ValueError("TTS PCM response exceeds 16 MiB")
                    raw_pcm = bytes(pcm_buf)
                else:
                    raw_pcm = response.content or b""
                logger.bind(tag=TAG).info(f"TTS请求成功: {text}, 耗时: {time.time() - start_time:.3f}秒")

                # 使用独立临时opus编码器处理PCM数据，避免与流式线程并发访问self.opus_encoder
                opus_datas = []
                pcm_data = raw_pcm[:len(raw_pcm) & ~1]
                temp_encoder = opus_encoder_utils.OpusEncoderUtils(
                    sample_rate=24000, channels=1, frame_size_ms=60
                )
                try:
                    frame_bytes = int(
                        temp_encoder.sample_rate
                        * temp_encoder.channels
                        * temp_encoder.frame_size_ms
                        / 1000
                        * 2
                    )
                    for i in range(0, len(pcm_data), frame_bytes):
                        frame = pcm_data[i : i + frame_bytes]
                        if len(frame) < frame_bytes:
                            frame = frame + b"\x00" * (frame_bytes - len(frame))
                        temp_encoder.encode_pcm_to_opus_stream(
                            frame,
                            end_of_stream=(i + frame_bytes >= len(pcm_data)),
                            callback=lambda opus: opus_datas.append(opus)
                        )
                finally:
                    temp_encoder.close()

                return opus_datas

        except Exception as e:
            logger.bind(tag=TAG).error(f"TTS请求异常: {type(e).__name__}")
            return []
