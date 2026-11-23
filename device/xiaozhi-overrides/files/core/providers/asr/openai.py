"""OpenAI 兼容的语音识别（/v1/audio/transcriptions）。

[ThinkSmart 本地改动，独立覆盖，官方文件不动] 用来把对话识别交给 MacBook 上的语音服务（SenseVoice /
Qwen3-ASR，经公网网关），比板子上本机识别每句快 0.3～0.9 秒。和官方版本的区别：
- 不阻塞事件循环：请求放到线程池里发（官方版在协程里直接 requests.post，识别期间整个 hub 卡住）；
- 有超时：连接 connect_timeout（默认 1.5 秒）、总 timeout（默认 8 秒），官方版没有超时、可能一直等；
- 连不上（连接错误/连接超时/5xx）且还没拿到结果时立刻重试 retries 次（默认 1 次）；
- 音频在内存里拼成 WAV 直接上传，不写临时文件；
- 默认不走系统代理（hub 环境里有 mihomo 代理变量，家里到 Mac 不需要绕代理），use_proxy: true 可恢复；
- 可选 language 表单字段；没配 api_key 就不带 Authorization 头。
- 可选 prompt 表单字段（OpenAI 接口同名参数）：交给 Qwen3-ASR 当上下文，让家里常说的人名地名更容易认对；
  填 "@default" 表示用 Mac 端 asr_context.txt 里维护的那份常用词（2026-11-05）。
原有配置项（base_url / model_name / api_key / output_dir）含义不变，其他 openai 类型的条目照常能用。
"""
import io
import json
import math
import os
import time
import wave
import asyncio
import threading
from typing import Optional, Tuple, List

import requests

from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from core.providers.asr.base import ASRProviderBase

TAG = __name__
logger = setup_logging()

# hub 每次对话都会新建一个识别实例；按线程复用 Session 连接池，兼顾高并发线程安全与首句零握手延迟
_local = threading.local()


def _session(use_proxy: bool) -> requests.Session:
    sessions = getattr(_local, "sessions", None)
    if sessions is None:
        sessions = _local.sessions = {}
    if use_proxy not in sessions:
        s = requests.Session()
        s.trust_env = use_proxy
        sessions[use_proxy] = s
    return sessions[use_proxy]


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        self.interface_type = InterfaceType.NON_STREAM
        self.api_key = config.get("api_key")
        self.api_url = config.get("base_url")
        self.model = config.get("model_name")
        self.language = config.get("language")
        self.prompt = str(config.get("prompt") or "").strip()
        self.output_dir = config.get("output_dir") or "tmp/"
        self.delete_audio_file = delete_audio_file
        def _pos_float(val, default: float) -> float:
            if val is None or isinstance(val, bool):
                return default
            try:
                f = float(val)
                return f if math.isfinite(f) and f > 0 else default
            except (TypeError, ValueError):
                return default

        self.timeout = _pos_float(config.get("timeout"), 8.0)
        self.connect_timeout = _pos_float(config.get("connect_timeout"), 1.5)
        raw_retries = config.get("retries")
        try:
            self.retries = max(0, 1 if raw_retries is None or isinstance(raw_retries, bool) else int(raw_retries))
        except (TypeError, ValueError):
            self.retries = 1
        self.use_proxy = bool(config.get("use_proxy", False))
        self.session = _session(self.use_proxy)
        os.makedirs(self.output_dir, exist_ok=True)

    def requires_file(self) -> bool:
        return False

    @staticmethod
    def _wav(pcm: bytes) -> bytes:
        pcm = pcm[:len(pcm) & ~1]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        return buf.getvalue()

    def _post(self, wav: bytes) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = {"model": self.model}
        if self.language:
            data["language"] = self.language
        if self.prompt:
            data["prompt"] = self.prompt
        last = None
        for attempt in range(self.retries + 1):
            start = time.monotonic()
            try:
                sess = _session(getattr(self, "use_proxy", False))
                post_kw = dict(files={"file": ("audio.wav", wav, "audio/wav")},
                               data=data, headers=headers,
                               timeout=(self.connect_timeout, self.timeout))
                try:
                    cm = sess.post(self.api_url, stream=True, **post_kw)
                except TypeError:
                    cm = sess.post(self.api_url, **post_kw)
                with cm as resp:
                    if resp.status_code >= 500 and attempt < self.retries:
                        raise requests.ConnectionError(f"HTTP {resp.status_code}")
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    try:
                        if hasattr(resp, "iter_content"):
                            buf = bytearray()
                            for chunk in resp.iter_content(chunk_size=65536):
                                if chunk:
                                    buf.extend(chunk)
                                    if len(buf) > 1048576:
                                        raise RuntimeError("ASR response too large")
                            payload = json.loads(buf.decode("utf-8"))
                        else:
                            payload = resp.json()
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise RuntimeError("invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise RuntimeError("invalid JSON")
                text = str(payload.get("text") or "")
                logger.bind(tag=TAG).info(
                    f"远程识别 {self.model}：{len(wav) / 32000:.1f} 秒音频，用时 {time.monotonic() - start:.2f} 秒"
                    + (f"（第 {attempt + 1} 次）" if attempt else ""))
                return text
            except (requests.ConnectionError, requests.exceptions.ConnectTimeout) as e:
                last = e
                logger.bind(tag=TAG).warning(
                    f"远程识别连接失败（{type(e).__name__}，{time.monotonic() - start:.2f} 秒），"
                    + ("重试" if attempt < self.retries else "放弃"))
        raise last

    def recognize_pcm(self, pcm: bytes) -> str:
        """给 hub 的 /kiosk/asr 用（设备客户端复核唤醒词 wake_verify=hub）：同步识别一段 16kHz 单声道
        16 位 PCM，只返回文字。失败抛异常，接口返回 500，客户端照常唤醒（它对出错是放行的）。
        注意 hub 的「边说边识别」只认 segment_pause_ms，这个类没有该属性，所以不会被拿去分段识别。"""
        try:
            return self._post(self._wav(pcm[:len(pcm) // 2 * 2]))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(type(e).__name__) from None

    async def speech_to_text(self, opus_data: List[bytes], session_id: str, artifacts=None) -> Tuple[Optional[str], Optional[str]]:
        if artifacts is None or not artifacts.pcm_bytes:
            return "", None
        try:
            wav = self._wav(artifacts.pcm_bytes)
            text = await asyncio.get_running_loop().run_in_executor(None, self._post, wav)
            return text, artifacts.file_path
        except Exception as e:
            # 连接错误的文字里带完整 URL（含网关令牌），只记类型；HTTP 状态码可以记
            detail = f": {e}" if isinstance(e, RuntimeError) else ""
            logger.bind(tag=TAG).error(f"语音识别失败: {type(e).__name__}{detail}")
            return "", None
