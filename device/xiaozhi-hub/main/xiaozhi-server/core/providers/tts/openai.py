import os
import threading
import requests
from requests.adapters import HTTPAdapter
from core.admin import config_store
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()
_ORIG_REQ_POST = requests.post
_TTS_SESS = None
_TTS_SESS_LOCK = threading.Lock()


def _get_tts_session() -> requests.Session:
    global _TTS_SESS
    if _TTS_SESS is None:
        with _TTS_SESS_LOCK:
            if _TTS_SESS is None:
                s = requests.Session()
                ad = HTTPAdapter(pool_connections=4, pool_maxsize=8)
                s.mount("https://", ad)
                s.mount("http://", ad)
                _TTS_SESS = s
    return _TTS_SESS


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsRate", "speed", 0.25, 4, 1, lambda v: round(float(v), 2)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("api_key")
        self.api_url = config.get("api_url", "https://api.openai.com/v1/audio/speech")
        self.model = config.get("model", "tts-1")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice", "alloy")
        self.audio_file_type = config.get("format", "wav")

        # 处理空字符串的情况
        speed = config.get("speed", "1.0")
        self.speed = float(speed) if speed else 1.0

        # 应用百分比调整（如果存在），否则使用公有化配置
        self._apply_percentage_params(config)

        self.output_file = config.get("output_dir", "tmp/")
        model_key_msg = check_model_key("TTS", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)

    async def text_to_speak(self, text, output_file):
        headers = {"Content-Type": "application/json"}
        ak = str(self.api_key or "").strip()
        if ak and not config_store.is_placeholder(ak):
            try:
                ak.encode("latin-1")
                headers["Authorization"] = f"Bearer {ak}"
            except UnicodeEncodeError:
                pass
        data = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": self.audio_file_type,
            "speed": self.speed,
        }
        post_fn = _get_tts_session().post if requests.post is _ORIG_REQ_POST else requests.post
        response = post_fn(
            self.api_url, json=data, headers=headers, timeout=self.tts_timeout
        )
        if response.status_code == 200:
            if output_file:
                os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
                with open(output_file, "wb") as audio_file:
                    audio_file.write(response.content)
            else:
                return response.content
        else:
            raise Exception(
                f"OpenAI TTS请求失败: {response.status_code} - {response.text}"
            )
