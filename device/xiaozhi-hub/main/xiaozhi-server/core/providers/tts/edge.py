import asyncio
import os
import uuid
import edge_tts
from datetime import datetime
from core.providers.tts.base import TTSProviderBase


def _parse_edge_int(value, default: int) -> int:
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    s = s.rstrip("%Hzhz \t")
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "speech_rate", -100, 100, 0, int),
        ("ttsPitch", "pitch_rate", -100, 100, 0, int),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
        self.audio_file_type = config.get("format", "mp3")

        self.volume = _parse_edge_int(config.get("volume", "50"), 50)
        self.speech_rate = _parse_edge_int(config.get("rate", "0"), 0)
        self.pitch_rate = _parse_edge_int(config.get("pitch", "0"), 0)

        # 应用百分比调整
        self._apply_percentage_params(config)

        self.edge_rate = f"{self.speech_rate:+}%"
        self.edge_volume = f"{self.volume:+}%"
        self.edge_pitch = f"{self.pitch_rate:+}Hz"
        # [xiaozhi-hub] 调大音量不交给 Edge：它把波形整体放大，响的字会被硬削顶（"+50%" 时峰值冲到满幅的 1.15 倍），
        # 设备上听到字中间轻轻"咔"一下。按原音量合成，解码后由网关加增益、平滑压峰（core/utils/pcm_gain.py）
        if self.volume > 0 and delete_audio_file and self.audio_file_type == "mp3":
            self.post_gain = 1 + self.volume / 100
            self.edge_volume = "+0%"

    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        async def _collect():
            communicate = edge_tts.Communicate(
                text,
                voice=self.voice,
                rate=self.edge_rate,
                volume=self.edge_volume,
                pitch=self.edge_pitch,
            )
            if output_file:
                # 确保目录存在并创建空文件
                os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
                with open(output_file, "wb") as f:
                    pass

                # 流式写入音频数据
                with open(output_file, "ab") as f:  # 改为追加模式避免覆盖
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":  # 只处理音频数据块
                            f.write(chunk["data"])
                return None
            # 返回音频二进制数据
            audio_bytes = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_bytes += chunk["data"]
            return audio_bytes

        try:
            return await asyncio.wait_for(_collect(), timeout=30.0)
        except Exception as e:
            if output_file and os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except OSError:
                    pass
            error_msg = f"Edge TTS请求失败: {e}"
            raise Exception(error_msg)  # 抛出异常，让调用方捕获
