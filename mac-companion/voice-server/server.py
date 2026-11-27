#!/usr/bin/env python3
"""
Local Apple Silicon (MLX) Speech Recognition (ASR) & Multi-Style Voice-Cloning TTS Server
Designed for ThinkSmart View / Xiaozhi Server & OpenAI-compatible clients.

Optimizations included:
1. Multi-Style Reference Pool & Anti-Monotony Rotation:
   Rotates across calm/flow/warm/question reference clips per sentence (with expressive
   exclamation selection) so consecutive sentences never repeat the same prosody contour.
2. Punctuation Restoration:
   Restores trailing sentence punctuation ('？' or '。') stripped by Xiaozhi's IndexStreamTTS
   last-segment cleaner so the model produces natural sentence-final intonation.
3. SenseVoice / Qwen3-ASR Noise Filtering & Metal GPU Coordination:
   Prevents short breaths/ambient noise from hallucinating foreign kana, keeps MLX weight pages
   warm in unified memory, and coordinates Metal GPU leases with local study-vision inference.
"""

import asyncio
import atexit
import fcntl
import gc
import io
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import httpx
import mlx.core as mx
from mlx.utils import tree_flatten
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

from mlx_audio.stt import load as load_stt_model
from mlx_audio.tts.utils import load_model as load_tts_model
from mlx_audio.utils import load_audio
from speech_text import interpretation_speech_text
import tts_speedups

# ---------------------------------------------------------------------------
# Logging & Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("xiaozhi-voice")


class _HealthCheckAccessFilter(logging.Filter):
    """Suppress high-frequency 200 OK access logs for GET /health probes while keeping errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if '"GET /health ' in msg and " 200" in msg:
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_HealthCheckAccessFilter())

BASE_DIR = Path(__file__).resolve().parent
ASR_MODEL_DIR = Path(
    os.environ.get("ASR_MODEL_DIR", str(BASE_DIR / "models" / "SenseVoiceSmall"))
)
# ASR_SENSEVOICE=0: SenseVoice is not loaded at all; every request for SenseVoiceSmall
# is served by Qwen3-ASR (same normal-mode filtering), freeing SenseVoice's memory.
SENSEVOICE_ENABLED = os.environ.get("ASR_SENSEVOICE", "1") != "0"
# ASR_DISABLED=1 starts a TTS-only overflow worker (no ASR models loaded, e.g. on 127.0.0.1:8011).
# On the primary service, TTS_OVERFLOW_URL=<worker> forwards /tts requests there while local TTS is busy.
ASR_DISABLED = os.environ.get("ASR_DISABLED", "0") == "1"
# Bound MLX's freed Metal buffer cache so concurrent synthesis never grows unboundedly.
MLX_CACHE_LIMIT_MB = int(os.environ.get("MLX_CACHE_LIMIT_MB", "1024"))
if MLX_CACHE_LIMIT_MB >= 0:
    mx.set_cache_limit(MLX_CACHE_LIMIT_MB * 1024 * 1024)
TTS_OVERFLOW_URL = os.environ.get("TTS_OVERFLOW_URL", "").rstrip("/")
# Secondary TTS worker (ASR_DISABLED=1) unloads MLX weights after idle timeout so it sits <120 MB
# until an overflow burst wakes it; primary (:8010) defaults to 0 (always resident).
TTS_IDLE_SLEEP_SECONDS = int(os.environ.get("TTS_IDLE_SLEEP_SECONDS", "600" if ASR_DISABLED else "0"))
MAX_ASR_UPLOAD_BYTES = int(os.environ.get("MAX_ASR_UPLOAD_BYTES", str(25 * 1024 * 1024)))
ASR_CONTEXT_FILE = Path(os.environ.get("ASR_CONTEXT_FILE", str(BASE_DIR / "asr_context.txt")))
_asr_context_cache = {"mtime": None, "text": ""}


def _default_asr_context() -> str:
    try:
        mtime = ASR_CONTEXT_FILE.stat().st_mtime
        if mtime != _asr_context_cache["mtime"]:
            _asr_context_cache.update(mtime=mtime, text=ASR_CONTEXT_FILE.read_text(encoding="utf-8").strip()[:600])
    except OSError:
        _asr_context_cache.update(mtime=None, text="")
    return _asr_context_cache["text"]


TRANSLATION_ASR_MODEL_DIR = Path(os.environ.get(
    "TRANSLATION_ASR_MODEL_DIR", str(BASE_DIR / "models" / "Qwen3-ASR-1.7B-8bit")
))
_tts_8bit = BASE_DIR / "models" / "Qwen3-TTS-12Hz-1.7B-Base-8bit"
_tts_bf16 = BASE_DIR / "models" / "Qwen3-TTS-12Hz-1.7B-Base-bf16"
TTS_MODEL_DIR = Path(
    os.environ.get(
        "TTS_MODEL_DIR",
        str(_tts_8bit if _tts_8bit.exists() else _tts_bf16),
    )
)
VOICES_DIR = Path(os.environ.get("VOICES_DIR", str(BASE_DIR / "voices")))
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "xiaohe")
STREAMING_INTERVAL = float(os.environ.get("STREAMING_INTERVAL", "0.24"))
TRANSLATION_STREAMING_INTERVAL = float(os.environ.get("TRANSLATION_STREAMING_INTERVAL", "0.16"))
ASR_LANGUAGES = {"auto", "zh", "en", "yue", "ja", "ko"}
TRANSLATION_ASR_LANGUAGES = ASR_LANGUAGES | {"de", "fr", "ru", "es", "pt", "it"}
LANGUAGE_NAMES = {
    "zh": "chinese", "en": "english", "ja": "japanese",
    "ko": "korean", "de": "german", "fr": "french",
    "ru": "russian", "es": "spanish", "pt": "portuguese", "it": "italian",
}

# Xiaozhi IndexStreamTTS Opus encoder uses 24000Hz * 1ch * 60ms * 2 bytes = 2880 bytes/frame
OPUS_FRAME_BYTES_24K_60MS = 2880
TTS_SAMPLE_RATE = 24000

VOICE_ALIASES = {
    "湾湾小何": "xiaohe",
    "小何": "xiaohe",
    "wanwanxiaohe": "xiaohe",
    "zh_female_xiaohe_uranus_bigtts": "xiaohe",
    "zh_female_wanwanxiaohe_moon_bigtts": "xiaohe",
    "zh_female_vv_uranus_bigtts": "vivi",
    "vv": "vivi",
    "甜美桃子": "taozi",
    "奶气萌娃": "mengwa",
    "serena": "xiaohe",
    "vivian": "vivi",
    "alloy": "xiaohe",
    "nova": "vivi",
    "shimmer": "xiaohe",
    "onyx": "vivi",
}

_gpu_lock = threading.Lock()
_tts_session_lock = threading.Lock()
_tts_lifecycle_lock = threading.Lock()
_active_asr_lock = threading.Lock()
_active_translation_lock = threading.Lock()
_gpu_lease_lock = threading.Lock()

_asr_model = None
_translation_asr_model = None
_tts_model = None
_tts_sleeping: bool = False
_voice_bank: Dict[str, dict] = {}
_cached_supported_languages: List[str] = []
_cached_sample_rate: int = TTS_SAMPLE_RATE
_cached_speakers_meta: Dict[str, dict] = {}
_voice_rotation_state: Dict[str, int] = {"xiaohe": 1, "vivi": 1} if ASR_DISABLED else {}
_last_variant_used: Dict[str, str] = {}
_spk_embed_cache: Dict[Tuple[int, float], mx.array] = {}
_startup_time: float = 0.0
_last_inference_time: float = 0.0
_last_voice_activity_time: float = 0.0
_last_asr_activity_time: float = 0.0
_active_asr_count: int = 0
_active_translation_count: int = 0
_last_keepwarm_time: float = 0.0
_keepwarm_count: int = 0
_keepwarm_arrays: List[mx.array] = []

_STUDY_VISION_PORT = int(os.environ.get("STUDY_VISION_LLM_PORT", "8083"))
_STUDY_VISION_PREEMPT = os.environ.get("STUDY_VISION_PREEMPT", "1") != "0"
_GPU_PAUSE_GRACE_SEC = float(os.environ.get("STUDY_VISION_PAUSE_GRACE_SEC", "0.50"))
_STUDY_VISION_MARKER = Path(os.path.expanduser("~/Library/Caches/study-vision/inferring.json"))
_GPU_HOLD_DIR = Path(os.path.expanduser("~/Library/Caches/xiaozhi-voice/gpu-holds"))
_gpu_lease_seq: int = 0
_verified_llama_pids: Dict[int, float] = {}


def _touch_voice_activity(is_asr: bool = False) -> None:
    global _last_voice_activity_time, _last_asr_activity_time
    now = time.time()
    _last_voice_activity_time = now
    if is_asr:
        _last_asr_activity_time = now


def _is_pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_study_vision_llama_pid(pid: int) -> bool:
    if pid <= 1 or pid == os.getpid() or not _is_pid_alive(pid):
        _verified_llama_pids.pop(pid, None)
        return False
    now = time.monotonic()
    cached = _verified_llama_pids.get(pid)
    if cached is not None and (now - cached) < 600.0:
        return True
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        cmd = (res.stdout or "").strip()
    except Exception:
        return False
    ok = "llama-server" in cmd and bool(re.search(rf"(?:^|\s|=){_STUDY_VISION_PORT}(?:\s|$)", cmd))
    if ok:
        _verified_llama_pids[pid] = now
    else:
        _verified_llama_pids.pop(pid, None)
    return ok


def _find_listening_study_vision_pid() -> int:
    try:
        res = subprocess.run(
            ["lsof", "-nPt", f"-iTCP:{_STUDY_VISION_PORT}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if _is_study_vision_llama_pid(pid):
                    return pid
    except Exception:
        pass
    return 0


def _read_active_study_vision_marker() -> Optional[dict]:
    try:
        data = json.loads(_STUDY_VISION_MARKER.read_text(encoding="utf-8"))
        ts = float(data.get("ts") or 0.0)
        if (time.time() - ts) > 95.0:
            return None
        pid = int(data.get("pid") or 0)
        if pid <= 1:
            return None
        return data
    except Exception:
        return None


@contextmanager
def _gpu_hold_flock():
    _GPU_HOLD_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(_GPU_HOLD_DIR / ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _prune_leases_locked(now: float, remove_owner_pid: int = 0) -> Tuple[Dict[int, int], set]:
    live_counts: Dict[int, int] = {}
    removed_targets: set = set()
    for p in _GPU_HOLD_DIR.glob("*.lease"):
        remove = False
        target_pid = 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            owner_pid = int(data.get("owner_pid") or 0)
            target_pid = int(data.get("target_pid") or 0)
            exp = float(data.get("exp") or 0.0)
            if (
                exp <= now
                or not _is_pid_alive(owner_pid)
                or (remove_owner_pid > 0 and owner_pid == remove_owner_pid)
            ):
                remove = True
            elif target_pid > 1:
                live_counts[target_pid] = live_counts.get(target_pid, 0) + 1
        except Exception:
            remove = True
        if remove:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            if target_pid > 1:
                removed_targets.add(target_pid)
    return live_counts, removed_targets


def _reap_expired_gpu_holds(resume_pid_if_idle: int = 0, remove_own_leases: bool = False) -> None:
    if not _GPU_HOLD_DIR.exists() and resume_pid_if_idle <= 1:
        return
    try:
        resumed_pids: List[int] = []
        with _gpu_lease_lock:
            with _gpu_hold_flock():
                live_counts, removed_targets = _prune_leases_locked(
                    time.time(),
                    remove_owner_pid=os.getpid() if remove_own_leases else 0,
                )
                candidates = set(removed_targets)
                if resume_pid_if_idle > 1:
                    candidates.add(resume_pid_if_idle)
                for pid in candidates:
                    if live_counts.get(pid, 0) == 0 and _is_study_vision_llama_pid(pid):
                        try:
                            os.kill(pid, signal.SIGCONT)
                            if pid in removed_targets or resume_pid_if_idle == pid:
                                resumed_pids.append(pid)
                        except ProcessLookupError:
                            pass
                        except OSError:
                            pass
        if resumed_pids:
            logger.info("Reaped GPU hold leases; ensured SIGCONT on study-vision pids=%s", resumed_pids)
    except Exception as exc:
        logger.warning("Failed to reap GPU hold leases: %s", exc)


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _acquire_study_vision_pause(reason: str, max_hold_sec: float = 20.0) -> Optional[str]:
    global _gpu_lease_seq
    if not _STUDY_VISION_PREEMPT or not _STUDY_VISION_MARKER.exists():
        return None
    marker = _read_active_study_vision_marker()
    if marker is None:
        return None
    target_pid = int(marker.get("pid") or 0)
    if not _is_study_vision_llama_pid(target_pid):
        return None
    lease_path: Optional[Path] = None
    stopped_now = False
    try:
        with _gpu_lease_lock:
            with _gpu_hold_flock():
                marker2 = _read_active_study_vision_marker()
                if marker2 is None:
                    return None
                target_pid = int(marker2.get("pid") or 0)
                if not _is_study_vision_llama_pid(target_pid):
                    return None
                now = time.time()
                live_counts, _ = _prune_leases_locked(now)
                preexisting = live_counts.get(target_pid, 0)
                _gpu_lease_seq += 1
                lease_path = _GPU_HOLD_DIR / f"{os.getpid()}_{threading.get_ident()}_{_gpu_lease_seq}.lease"
                payload = {
                    "owner_pid": os.getpid(),
                    "target_pid": target_pid,
                    "exp": now + max_hold_sec,
                    "reason": reason,
                    "grace": False,
                }
                _write_json_atomic(lease_path, payload)
                if preexisting == 0:
                    try:
                        os.kill(target_pid, signal.SIGSTOP)
                        stopped_now = True
                    except ProcessLookupError:
                        lease_path.unlink(missing_ok=True)
                        return None
                    except OSError as exc:
                        logger.warning("Failed to pause study-vision pid=%d: %s", target_pid, exc)
                        lease_path.unlink(missing_ok=True)
                        return None
        if stopped_now:
            logger.info("Paused study-vision llama-server pid=%d for %s", target_pid, reason)
        timer = threading.Timer(max_hold_sec + 0.5, _reap_expired_gpu_holds)
        timer.daemon = True
        timer.start()
        return str(lease_path)
    except Exception as exc:
        logger.warning("Acquire study-vision pause error: %s", exc)
        if lease_path is not None:
            try:
                lease_path.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def _release_study_vision_pause(token: Optional[str], grace_sec: float = _GPU_PAUSE_GRACE_SEC) -> None:
    if not token:
        return
    lease_path = Path(token)
    resumed_pids: List[int] = []
    deferred_grace = False
    try:
        with _gpu_lease_lock:
            with _gpu_hold_flock():
                now = time.time()
                data: Optional[dict] = None
                target_pid = 0
                try:
                    data = json.loads(lease_path.read_text(encoding="utf-8"))
                    target_pid = int(data.get("target_pid") or 0)
                except Exception:
                    target_pid = 0
                if (
                    grace_sec > 0
                    and data is not None
                    and target_pid > 1
                    and lease_path.exists()
                    and _read_active_study_vision_marker() is not None
                ):
                    data["exp"] = min(float(data.get("exp") or (now + grace_sec)), now + grace_sec)
                    data["grace"] = True
                    _write_json_atomic(lease_path, data)
                    deferred_grace = True
                else:
                    try:
                        lease_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    live_counts, removed_targets = _prune_leases_locked(now)
                    candidates = set(removed_targets)
                    if target_pid > 1:
                        candidates.add(target_pid)
                    for pid in candidates:
                        if live_counts.get(pid, 0) == 0 and _is_study_vision_llama_pid(pid):
                            try:
                                os.kill(pid, signal.SIGCONT)
                                resumed_pids.append(pid)
                            except ProcessLookupError:
                                pass
                            except OSError as exc:
                                logger.warning("Failed to resume study-vision pid=%d: %s", pid, exc)
        if deferred_grace:
            timer = threading.Timer(grace_sec + 0.03, _reap_expired_gpu_holds)
            timer.daemon = True
            timer.start()
        elif resumed_pids:
            logger.info("Resumed study-vision llama-server pid=%s", resumed_pids)
    except Exception as exc:
        logger.warning("Release study-vision pause error: %s", exc)


TAG_CLEAN_RE = re.compile(r"<\|[^|>]+?\|>")
PRONOUNCEABLE_RE = re.compile(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", re.UNICODE)
END_PUNCT_RE = re.compile(r"[。！？!?…~～.；;]['\"”’」』）》】\]\)]*$")
QUESTION_CUE_RE = re.compile(
    r"(吗|呢|么|啥|什么|怎么|咋样|怎么样|好不好|行不行|对不对|是不是|要不要|可不可以|能不能|哪只|哪个|哪里|哪儿|几|多少|为什么)[呀啊哦呢吗吧啦]*$"
)
QUESTION_MID_RE = re.compile(
    r"(哪种|哪类|哪款|哪部|哪首|哪位|哪些|哪个|哪里|哪儿|怎么选|选哪)[^，。！？!?；;]{0,12}$"
)
NON_QUESTION_PREFIX_RE = re.compile(
    r"(不管|无论|不论|哪怕).{0,4}(哪种|哪类|哪款|哪部|哪首|哪位|哪些|哪个|哪里|哪儿)"
)

# Generic ASR homophone corrections for voice commands & common domain terms.
# Additional custom landmark/household rules can be placed in ~/.config/thinksmart-voice/homophones.json
ASR_HOMOPHONE_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"记时"), "计时"),
    (re.compile(r"代班(事项|事情|任务|清单|日程)"), r"待办\1"),
    (re.compile(r"开启(?:重新权利|同声权利|同生传译|同声传意)"), "开启同声传译"),
    (re.compile(r"(?:筒声|铜声|童声|通声|痛声|同生|童生|铜生|通生|同升|同身)(?=翻译|传译)"), "同声"),
    (re.compile(r"(?:(?<=开启|打开|进入|开始|启动|退出|关闭|停止|结束)|^)"
                r"[同筒铜童通痛桶统彤瞳][声城时生升身胜盛圣省成程乘诚市事师诗]"
                r"(?=(?:翻译|传译)(?:模式)?[吧呀啦哦。！？!?，,\s]*$)"), "同声"),
    (re.compile(r"(?<=开启|打开|进入|开始|启动|退出|关闭|停止|结束)(?:同船|铜船|童船|通传|铜传|童传|痛传)"), "同传"),
    (re.compile(r"(播放|播报)一行闻"), r"\1一下新闻"),
    (re.compile(r"一行闻"), "一下新闻"),
    (re.compile(r"(?:呼鲁特|呼伦特|布鲁特|普伦特)原油"), "布伦特原油"),
    (re.compile(r"(?:宝金|保金)(?:现代|现在)"), "黄金现在"),
    (re.compile(r"(?:宝金|保金)(价格|多少钱|行情|现价)"), r"黄金\1"),
    (re.compile(r"你是中[王亡]，?他就说赢王王?"), "你是中文，他就说英文"),
    (re.compile(r"说中[王亡](?=.*(?:英|赢))"), "说中文"),
    (re.compile(r"说赢王王?"), "说英文"),
    (re.compile(r"鹦鹉应该怎么(?:办|翻)"), "英文应该怎么翻"),
    (re.compile(r"(左|右)侧(?:建昌|剑苍|健康|见仓|检仓)"), r"\1侧建仓"),
    (re.compile(r"调仓换骨"), "调仓换股"),
    (re.compile(r"市赢率"), "市盈率"),
    (re.compile(r"缩量回采"), "缩量回踩"),
    (re.compile(r"^(?:吃下|催下|推下|吹下|退夏|脆下|坠下)([吧啦呀哦]?[，,。！？!?\s]*)$"), r"退下\1"),
    (re.compile(r"^(小智|小麦)(?:吃下|催下|推下|吹下|退夏|脆下|坠下)([吧啦呀哦]?[，,。！？!?\s]*)$"), r"\1退下\2"),
]

CUSTOM_HOMOPHONES_FILE = Path(
    os.environ.get("ASR_HOMOPHONES_FILE", str(Path.home() / ".config" / "thinksmart-voice" / "homophones.json"))
)
if CUSTOM_HOMOPHONES_FILE.is_file():
    try:
        for item in json.loads(CUSTOM_HOMOPHONES_FILE.read_text(encoding="utf-8")):
            ASR_HOMOPHONE_RULES.append((re.compile(item["pattern"]), item["replacement"]))
    except Exception as _exc:
        logger.warning("Failed to load custom homophones from %s: %s", CUSTOM_HOMOPHONES_FILE, _exc)


def _correct_asr_homophones(text: str) -> str:
    """Correct common ASR homophone errors for voice commands and custom household terms."""
    if not text:
        return text
    corrected = text
    for pattern, replacement in ASR_HOMOPHONE_RULES:
        corrected = pattern.sub(replacement, corrected)
    if corrected != text:
        logger.info("ASR homophone corrected: %r -> %r", text, corrected)
    return corrected


def _normalize_tts_text(raw_text: str, language: str = "auto") -> str:
    """Restore trailing sentence punctuation stripped by Xiaozhi's segment cleaner."""
    text = (raw_text or "").strip()
    if not text:
        return ""
    if not END_PUNCT_RE.search(text):
        is_mid_q = bool(QUESTION_MID_RE.search(text)) and not bool(NON_QUESTION_PREFIX_RE.search(text))
        if "？" in text or "?" in text or QUESTION_CUE_RE.search(text) or is_mid_q:
            text = text + "？"
        else:
            text = text + ("。" if re.search(r"[\u3400-\u9fff\u3040-\u30ff]", text) else ".")
    return text


def _infer_tts_language(text: str, language: str = "auto") -> str:
    """Resolve 'auto' to an explicit Qwen3-TTS language token so ICL prefill uses
    codec_think_id + language_id instead of codec_nothink_id (improves short-utterance alignment).
    """
    lang = LANGUAGE_NAMES.get((language or "auto").lower(), (language or "auto").lower())
    if lang != "auto":
        return lang
    if re.search(r"[\u3040-\u30ff]", text):
        cand = "japanese"
    elif re.search(r"[\uac00-\ud7af]", text):
        cand = "korean"
    elif re.search(r"[\u3400-\u9fff]", text):
        cand = "chinese"
    elif re.search(r"[\u0400-\u04ff]", text):
        cand = "russian"
    elif re.search(r"[A-Za-z]", text) and not re.search(r"[\u00c0-\u024f]", text):
        cand = "english"
    else:
        return "auto"
    supported = getattr(_tts_model, "supported_languages", None) or _cached_supported_languages or ()
    return cand if (not supported or cand in supported) else "auto"


def _estimate_tts_token_budget(text: str) -> Tuple[int, int]:
    """Compute syllable-aware (soft_max_tokens, hard_max_tokens) at 12.5Hz (80ms/token)."""
    cjk_kana_ko = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    digits = len(re.findall(r"\d", text))
    symbols = len(re.findall(r"[%％°℃℉+=×÷/@&$¥€£]", text))
    mid_connectors = len(re.findall(r"(?<=\w)[.\-:](?=\w)", text))
    words = re.findall(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff]+", text)
    word_units = sum(max(1.3, len(w) * 0.42) for w in words)
    pauses = len(re.findall(r"[,，、;；:：—…~～.。!！?？]", text))

    units = (
        cjk_kana_ko * 1.0
        + digits * 1.85
        + symbols * 2.5
        + mid_connectors * 1.2
        + word_units
        + pauses * 0.9
    )
    soft_tokens = min(650, max(24, int(round(units * 4.0 + 14))))
    hard_tokens = min(800, max(32, int(round(units * 5.0 + 22))))
    return soft_tokens, hard_tokens


def _select_voice_variant(voice_key: str, text: str, translation: bool = False) -> Tuple[str, dict]:
    """Select the best prosody reference variant and rotate across consecutive sentences
    so the model never repeats the exact same intonation contour back-to-back.
    """
    lower = (voice_key or DEFAULT_VOICE).strip().split("|", 1)[0].strip().lower()
    if lower in VOICE_ALIASES:
        lower = VOICE_ALIASES[lower]
    if lower not in _voice_bank:
        lower = DEFAULT_VOICE.lower() if DEFAULT_VOICE.lower() in _voice_bank else next(iter(_voice_bank))

    entry = _voice_bank[lower]
    variants: List[dict] = entry["variants"]
    if len(variants) == 1:
        return lower, variants[0]
    if translation:
        return lower, next((v for v in variants if v["style"] == "flow"), variants[0])

    last_style = _last_variant_used.get(lower)
    is_question = bool(re.search(r"[？?]['\"”’」』）》】\]\)]*$", text))
    is_exclamation = bool(re.search(r"[！!]['\"”’」』）》】\]\)]*$", text))

    if is_question:
        q_candidates = [
            v
            for style in ("question", "warm")
            for v in variants
            if v["style"] == style and v["style"] != last_style
        ]
        if not q_candidates:
            q_candidates = [v for v in variants if v["style"] != last_style] or variants
        chosen = q_candidates[0]
    elif is_exclamation:
        ex_candidates = [
            v
            for style in ("warm", "question", "flow")
            for v in variants
            if v["style"] == style and v["style"] != last_style
        ]
        if not ex_candidates:
            ex_candidates = [v for v in variants if v["style"] != last_style] or variants
        chosen = ex_candidates[0]
    else:
        narrative_pool = [v for v in variants if v["style"] in ("calm", "flow", "warm")]
        if not narrative_pool:
            narrative_pool = variants
        idx = _voice_rotation_state.get(lower, 0) % len(narrative_pool)
        chosen = narrative_pool[idx]
        if chosen["style"] == last_style and len(narrative_pool) > 1:
            idx = (idx + 1) % len(narrative_pool)
            chosen = narrative_pool[idx]
        _voice_rotation_state[lower] = idx + 1

    _last_variant_used[lower] = chosen["style"]
    return lower, chosen


def _trim_reference_audio(audio_mx: mx.array, sample_rate: int = TTS_SAMPLE_RATE) -> mx.array:
    """Trim leading (>15ms) noise/silence and append an exact 200ms clean silence boundary
    after active speech so ref_text never bleeds into target_text.
    """
    arr = np.asarray(audio_mx, dtype=np.float32).reshape(-1).copy()
    if arr.size == 0:
        return audio_mx
    active = np.where(np.abs(arr) > 0.008)[0]
    if active.size == 0:
        return audio_mx
    lead_keep = int(sample_rate * 0.015)
    decay_keep = int(sample_rate * 0.020)
    silence_pad = int(sample_rate * 0.200)
    start = max(0, int(active[0]) - lead_keep)
    end = min(arr.size, int(active[-1]) + 1 + decay_keep)
    core = arr[start:end].copy()
    fade_in = min(int(sample_rate * 0.008), core.size // 4)
    fade_out = min(int(sample_rate * 0.012), core.size // 4)
    if fade_in > 1:
        core[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    if fade_out > 1:
        core[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
    padded = np.concatenate([core, np.zeros(silence_pad, dtype=np.float32)], axis=0)
    return mx.array(padded)


def _float_audio_to_pcm16_bytes(audio_arr) -> bytes:
    np_audio = np.asarray(audio_arr, dtype=np.float32).reshape(-1)
    if np_audio.size == 0:
        return b""
    np.clip(np_audio, -1.0, 1.0, out=np_audio)
    pcm16 = (np_audio * 32767.0).astype("<i2")
    return pcm16.tobytes()


def _trim_leading_pcm16_silence(
    pcm_bytes: bytes,
    max_lead_samples: int = 480,  # 20ms at 24kHz
    threshold: int = 180,
) -> Tuple[bytes, bool]:
    """Strip dead leading silence before first speech onset while keeping a 20ms natural attack."""
    samples = np.frombuffer(pcm_bytes, dtype="<i2")
    if samples.size == 0:
        return b"", False
    active = np.where(np.abs(samples) >= threshold)[0]
    if active.size == 0:
        return b"", False
    first_idx = int(active[0])
    if first_idx <= max_lead_samples:
        return pcm_bytes, True
    keep_start = first_idx - max_lead_samples
    trimmed = samples[keep_start:].copy()
    ramp = min(120, max_lead_samples)
    if ramp > 1 and trimmed.size >= ramp:
        trimmed[:ramp] = (trimmed[:ramp].astype(np.float32) * np.linspace(0.0, 1.0, ramp)).astype("<i2")
    return trimmed.tobytes(), True


def _pcm16_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = TTS_SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _stream_tts_pcm_chunks(
    text: str,
    voice: Optional[str] = None,
    pad_to_opus_frame: bool = True,
    language: str = "auto",
    translation: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> Iterator[bytes]:
    """Synchronous generator yielding 24kHz 16-bit mono PCM bytes incrementally."""
    spoken_text = interpretation_speech_text(text, language) if translation else text
    clean_text = _normalize_tts_text(spoken_text, language)
    if not clean_text or not PRONOUNCEABLE_RE.search(clean_text):
        yield b"\x00" * OPUS_FRAME_BYTES_24K_60MS
        return

    t0 = time.perf_counter()
    global _last_inference_time
    _last_inference_time = time.time()
    first_chunk_time: Optional[float] = None
    total_bytes = 0
    leading_trimmed = False
    last_sample_val: int = 0
    completed_normally = False
    voice_id = DEFAULT_VOICE
    variant: dict = {"style": "default"}
    resolved_lang = _infer_tts_language(clean_text, language)
    soft_tokens, hard_max_tokens = _estimate_tts_token_budget(clean_text)

    _touch_voice_activity()
    while not _tts_session_lock.acquire(timeout=0.1):
        if cancel_event is not None and cancel_event.is_set():
            return
    pause_token: Optional[str] = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            return
        _touch_voice_activity()
        pause_token = _acquire_study_vision_pause("tts", max_hold_sec=20.0)
        if _tts_sleeping or _tts_model is None:
            _ensure_tts_awake()
        voice_id, variant = _select_voice_variant(voice or DEFAULT_VOICE, clean_text, translation)
        with _gpu_lock:
            _tts_model._current_soft_max_tokens = soft_tokens
            gen = _tts_model.generate(
                text=clean_text,
                ref_audio=variant["audio_mx"],
                ref_text=variant["text"],
                lang_code=resolved_lang,
                temperature=0.65 if translation else 0.78,
                top_p=0.92,
                top_k=40,
                max_tokens=hard_max_tokens,
                stream=True,
                streaming_interval=TRANSLATION_STREAMING_INTERVAL if translation else STREAMING_INTERVAL,
                verbose=False,
            )

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return
                if pause_token is None and _STUDY_VISION_MARKER.exists():
                    pause_token = _acquire_study_vision_pause("tts", max_hold_sec=20.0)
                with _gpu_lock:
                    try:
                        result = next(gen)
                    except StopIteration:
                        break
                    pcm_bytes = _float_audio_to_pcm16_bytes(result.audio)

                if pcm_bytes and not leading_trimmed:
                    pcm_bytes, leading_trimmed = _trim_leading_pcm16_silence(pcm_bytes)

                if pcm_bytes:
                    if first_chunk_time is None:
                        first_chunk_time = time.perf_counter() - t0
                    last_sample_val = int(np.frombuffer(pcm_bytes[-2:], dtype="<i2")[0])
                    total_bytes += len(pcm_bytes)
                    yield pcm_bytes
            completed_normally = True
        finally:
            try:
                with _gpu_lock:
                    gen.close()
            except Exception:
                pass
            if not completed_normally and total_bytes > 0:
                elapsed_abort = time.perf_counter() - t0
                audio_abort = total_bytes / (TTS_SAMPLE_RATE * 2)
                logger.warning(
                    "TTS stream cancelled mid-utterance | voice=%s(%s) | lang=%s | chars=%d | streamed=%.2fs | elapsed=%.2fs",
                    voice_id,
                    variant.get("style", "?"),
                    resolved_lang,
                    len(clean_text),
                    audio_abort,
                    elapsed_abort,
                )
    finally:
        try:
            _release_study_vision_pause(pause_token)
        finally:
            _last_inference_time = time.time()
            _touch_voice_activity()
            _tts_session_lock.release()

    if total_bytes == 0:
        yield b"\x00" * OPUS_FRAME_BYTES_24K_60MS
        total_bytes = OPUS_FRAME_BYTES_24K_60MS
    elif pad_to_opus_frame:
        remainder = total_bytes % OPUS_FRAME_BYTES_24K_60MS
        if remainder != 0:
            pad_len = OPUS_FRAME_BYTES_24K_60MS - remainder
            pad_samples = pad_len // 2
            if abs(last_sample_val) > 8 and pad_samples > 0:
                ramp_len = min(pad_samples, 120)
                pad_arr = np.zeros(pad_samples, dtype="<i2")
                pad_arr[:ramp_len] = np.linspace(last_sample_val, 0.0, ramp_len).astype("<i2")
                yield pad_arr.tobytes()
            else:
                yield b"\x00" * pad_len
            total_bytes += pad_len

    elapsed = time.perf_counter() - t0
    audio_sec = total_bytes / (TTS_SAMPLE_RATE * 2)
    ttfb_ms = (first_chunk_time or elapsed) * 1000.0
    logger.info(
        "TTS clone done | voice=%s(%s) | chars=%d | audio=%.2fs | TTFB=%.0fms | total=%.2fs | RTF=%.2fx",
        voice_id,
        variant["style"],
        len(clean_text),
        audio_sec,
        ttfb_ms,
        elapsed,
        elapsed / max(audio_sec, 0.01),
    )


async def _async_tts_pcm_chunks(**kwargs):
    """A worker owns and closes the MLX iterator even when HTTP is cancelled."""
    pending = queue.Queue(maxsize=8)
    cancelled = threading.Event()

    def offer(item):
        while not cancelled.is_set():
            try:
                pending.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def produce():
        iterator = _stream_tts_pcm_chunks(cancel_event=cancelled, **kwargs)
        try:
            for chunk in iterator:
                if cancelled.is_set():
                    break
                offer(chunk)
        except Exception as exc:
            logger.exception("TTS worker failed")
            offer(exc)
        finally:
            iterator.close()
            offer(None)

    worker = threading.Thread(target=produce, name="tts-stream", daemon=True)
    worker.start()
    try:
        while True:
            try:
                item = await asyncio.to_thread(pending.get, True, 0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        cancelled.set()


def _transcribe_file(audio_path: str, language: str = "auto", use_itn: bool = False,
                     model: str = "SenseVoiceSmall", context: Optional[str] = None) -> dict:
    global _last_inference_time, _active_asr_count
    _last_inference_time = time.time()
    _touch_voice_activity(is_asr=True)
    t0 = time.perf_counter()
    lang_arg = (language or "auto").lower()
    if _translation_asr_model is None:
        raise RuntimeError("ASR is disabled on this TTS-only worker")
    normal_mode = model != "Qwen3-ASR-1.7B"
    if _asr_model is None:
        model = "Qwen3-ASR-1.7B"
    with _active_asr_lock:
        _active_asr_count += 1
    pause_token: Optional[str] = None
    try:
        pause_token = _acquire_study_vision_pause("asr", max_hold_sec=20.0)
        with _gpu_lock:
            audio = load_audio(audio_path, sample_rate=16000)
            waveform = np.asarray(audio, dtype=np.float32).reshape(-1)
            if waveform.size < 1920 or float(np.max(np.abs(waveform))) < 0.001:
                return {"text": "", "language": "nospeech", "emotion": None,
                        "event": "nospeech", "model": model,
                        "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}
            if model == "Qwen3-ASR-1.7B":
                qwen_language = None if lang_arg == "auto" else LANGUAGE_NAMES.get(
                    lang_arg, "cantonese" if lang_arg == "yue" else lang_arg
                )
                result = _translation_asr_model.generate(
                    audio, language=qwen_language, max_tokens=1024,
                    temperature=0, verbose=False, system_prompt=context or None,
                )
            else:
                result = _asr_model.generate(
                    audio, language=lang_arg, use_itn=use_itn, verbose=False,
                )
    finally:
        try:
            _release_study_vision_pause(pause_token)
        finally:
            _last_inference_time = time.time()
            _touch_voice_activity(is_asr=True)
            with _active_asr_lock:
                _active_asr_count = max(0, _active_asr_count - 1)

    raw_text = (result.text or "").strip()
    clean_text = TAG_CLEAN_RE.sub("", raw_text).strip()
    if not PRONOUNCEABLE_RE.search(clean_text):
        clean_text = ""
    elif normal_mode and re.fullmatch(
        r"[\uac00-\ud7af\u3040-\u30ff\s.,!?，。！？、~～]+", clean_text
    ):
        logger.info("Filtered foreign-script hallucination in normal mode: %r", clean_text)
        clean_text = ""
    elif re.search(r"[\u4e00-\u9fff]", clean_text):
        clean_text = _correct_asr_homophones(clean_text)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    seg = result.segments[0] if getattr(result, "segments", None) else {}
    detected_language = result.language
    if isinstance(detected_language, (list, tuple)):
        detected_language = next((item for item in detected_language if item), "auto")
    if detected_language == "nospeech" or not clean_text:
        clean_text = ""
        detected_language = "nospeech"
    logger.info(
        "ASR done | %.0fms | model=%s | lang=%s | emo=%s | chars=%d | ctx=%s",
        elapsed_ms,
        model,
        detected_language,
        seg.get("emotion", "?"),
        len(clean_text),
        "yes" if (context and model == "Qwen3-ASR-1.7B") else "no",
    )
    return {
        "text": clean_text,
        "language": detected_language,
        "emotion": seg.get("emotion"),
        "event": seg.get("event"),
        "duration_ms": round(elapsed_ms, 1),
        "model": model,
    }


def _is_on_ac_power() -> bool:
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=1.5)
        return "AC Power" in out
    except Exception:
        return True


def _collect_keepwarm_arrays() -> List[mx.array]:
    arrays: List[mx.array] = []
    seen_ids = set()
    for obj in (_asr_model, _translation_asr_model, _tts_model):
        if obj is None or not hasattr(obj, "parameters"):
            continue
        try:
            for _, arr in tree_flatten(obj.parameters()):
                if isinstance(arr, mx.array) and arr.size > 0 and id(arr) not in seen_ids:
                    seen_ids.add(id(arr))
                    arrays.append(arr)
        except Exception:
            pass
    for emb in _spk_embed_cache.values():
        if isinstance(emb, mx.array) and emb.size > 0 and id(emb) not in seen_ids:
            seen_ids.add(id(emb))
            arrays.append(emb)
    for vinfo in _voice_bank.values():
        for var in vinfo.get("variants", []):
            amx = var.get("audio_mx")
            if isinstance(amx, mx.array) and amx.size > 0 and id(amx) not in seen_ids:
                seen_ids.add(id(amx))
                arrays.append(amx)
    return arrays


def _touch_mlx_weights_once() -> float:
    """Stride-touch every 16KB macOS VM page of ASR+TTS weights on Metal so unified memory
    pages stay marked active and never suffer swap-in stalls after idle periods.
    """
    global _last_keepwarm_time, _keepwarm_count
    if _tts_sleeping or _tts_session_lock.locked():
        return -1.0
    t0 = time.perf_counter()
    arrays = _keepwarm_arrays or _collect_keepwarm_arrays()
    batch_size = 32
    for i in range(0, len(arrays), batch_size):
        if _tts_session_lock.locked():
            return -1.0
        if not _gpu_lock.acquire(timeout=0.05):
            return -1.0
        try:
            batch = arrays[i : i + batch_size]
            probes = []
            for arr in batch:
                flat = arr.reshape(-1)
                step = 4096 if flat.size > 4096 else max(1, flat.size)
                probes.append(flat[::step].astype(mx.float32).sum())
            if probes:
                mx.eval(*probes)
        finally:
            _gpu_lock.release()
    if not _tts_session_lock.locked() and _gpu_lock.acquire(timeout=0.05):
        try:
            mx.clear_cache()
        finally:
            _gpu_lock.release()
    _last_keepwarm_time = time.time()
    _keepwarm_count += 1
    return (time.perf_counter() - t0) * 1000.0


_keepwarm_clip: Optional[mx.array] = None


def _warm_asr_once() -> float:
    """Run active ASR models on a short real speech clip to keep Metal kernels warm."""
    if ASR_DISABLED or _keepwarm_clip is None or _tts_session_lock.locked():
        return -1.0
    if not _gpu_lock.acquire(timeout=0.05):
        return -1.0
    t0 = time.perf_counter()
    try:
        if _asr_model is not None:
            _asr_model.generate(_keepwarm_clip, language="zh", use_itn=False, verbose=False)
        if _translation_asr_model is None:
            return -1.0
        _translation_asr_model.generate(_keepwarm_clip, language=None, max_tokens=16, temperature=0, verbose=False)
        mx.clear_cache()
        return (time.perf_counter() - t0) * 1000.0
    finally:
        _gpu_lock.release()


LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "xiaozhi-voice"
STUDY_VISION_LOG_DIR = Path.home() / "Library" / "Logs" / "study-vision"
LOG_ROTATE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per log file


def _rotate_launchd_logs_if_needed(max_bytes: int = LOG_ROTATE_MAX_BYTES) -> None:
    """Copy-and-truncate launchd stdout/stderr logs in place when exceeding max_bytes."""
    log_targets = (
        (
            LAUNCHD_LOG_DIR,
            (
                "server.out.log",
                "server.err.log",
                "hy-mt2.out.log",
                "hy-mt2.err.log",
                "translation.out.log",
                "translation.err.log",
                "tts2.out.log",
                "tts2.err.log",
                "gateway.out.log",
                "gateway.err.log",
            ),
        ),
        (
            STUDY_VISION_LOG_DIR,
            (
                "llm.out.log",
                "llm.err.log",
                "bridge.out.log",
                "bridge.err.log",
            ),
        ),
    )
    for log_dir, names in log_targets:
        if not log_dir.is_dir():
            continue
        for name in names:
            log_path = log_dir / name
            try:
                if log_path.is_file() and log_path.stat().st_size > max_bytes:
                    bak_path = log_dir / f"{name}.1"
                    shutil.copyfile(log_path, bak_path)
                    os.truncate(log_path, 0)
                    logger.info(
                        "Rotated launchd log %s/%s (> %d bytes) -> %s",
                        log_dir.name, name, max_bytes, bak_path.name,
                    )
            except Exception as exc:
                logger.warning("Log rotation skipped for %s/%s: %s", log_dir.name, name, exc)


def _load_tts_and_voices_locked(run_decode_warmup: bool = False) -> int:
    """Load Qwen3-TTS weights, voice bank, and pre-compile ICL primers under _tts_lifecycle_lock."""
    global _tts_model, _voice_bank, _keepwarm_arrays, _tts_sleeping
    global _cached_supported_languages, _cached_sample_rate, _cached_speakers_meta
    logger.info("Loading Voice-Cloning TTS model from %s ...", TTS_MODEL_DIR)
    model = load_tts_model(str(TTS_MODEL_DIR))

    orig_extract = model.extract_speaker_embedding

    def cached_extract_speaker_embedding(audio: mx.array) -> mx.array:
        key = (audio.size, float(audio.sum()))
        if key not in _spk_embed_cache:
            emb = orig_extract(audio)
            mx.eval(emb)
            _spk_embed_cache[key] = emb
        return _spk_embed_cache[key]

    model.extract_speaker_embedding = cached_extract_speaker_embedding

    tts_speedups_on = os.environ.get("TTS_SPEEDUPS", "1") == "1" and tts_speedups.install(model)

    voices_json_path = VOICES_DIR / "voices.json"
    with open(voices_json_path, "r", encoding="utf-8") as f:
        voices_cfg = json.load(f)

    new_voice_bank: Dict[str, dict] = {}
    total_variants = 0
    for vid, vinfo in voices_cfg.items():
        raw_variants = vinfo.get("variants") or [
            {"style": "default", "audio": vinfo["audio"], "text": vinfo["text"]}
        ]
        loaded_variants = []
        for var in raw_variants:
            wav_path = VOICES_DIR / var["audio"]
            raw_audio_mx = load_audio(str(wav_path), sample_rate=model.sample_rate)
            audio_mx = _trim_reference_audio(raw_audio_mx, sample_rate=model.sample_rate)
            mx.eval(audio_mx)
            loaded_variants.append(
                {
                    "style": var.get("style", "default"),
                    "audio_path": str(wav_path),
                    "audio_mx": audio_mx,
                    "text": var["text"],
                }
            )
            total_variants += 1

        new_voice_bank[vid.lower()] = {
            "name": vinfo.get("name", vid),
            "variants": loaded_variants,
        }

    logger.info(
        "Pre-caching %d ICL style variants across voices %s ...",
        total_variants,
        list(new_voice_bank.keys()),
    )
    for vid, vdata in new_voice_bank.items():
        for var in vdata["variants"]:
            with _gpu_lock:
                model._prepare_icl_generation_inputs(
                    text="你好。",
                    ref_audio=var["audio_mx"],
                    ref_text=var["text"],
                    language="chinese",
                )
                if tts_speedups_on:
                    tts_speedups.warm_primer(model, var["audio_mx"], var["text"], language="chinese")

    _tts_model = model
    _voice_bank = new_voice_bank
    _cached_supported_languages = list(getattr(model, "supported_languages", []) or [])
    _cached_sample_rate = int(getattr(model, "sample_rate", TTS_SAMPLE_RATE))
    _cached_speakers_meta = {
        k: {
            "name": v["name"],
            "styles": [x["style"] for x in v["variants"]],
        }
        for k, v in new_voice_bank.items()
    }
    _tts_sleeping = False
    _keepwarm_arrays = _collect_keepwarm_arrays()

    if run_decode_warmup:
        for _ in _stream_tts_pcm_chunks("你好。", voice=DEFAULT_VOICE):
            pass
        if tts_speedups_on:
            for _ in _stream_tts_pcm_chunks("Hello.", voice=DEFAULT_VOICE, language="english", translation=True):
                pass
        _keepwarm_arrays = _collect_keepwarm_arrays()
    return total_variants


def _ensure_tts_awake() -> None:
    """Wake sleeping secondary TTS worker on demand before serving a synthesis request."""
    global _last_inference_time
    if _tts_model is not None and not _tts_sleeping:
        return
    with _tts_lifecycle_lock:
        if _tts_model is not None and not _tts_sleeping:
            return
        t0 = time.perf_counter()
        _spk_embed_cache.clear()
        total_variants = _load_tts_and_voices_locked(run_decode_warmup=False)
        _last_inference_time = time.time()
        logger.info(
            "TTS worker woke from idle sleep in %.2fs (%d variants, MLX active=%.1f MB)",
            time.perf_counter() - t0,
            total_variants,
            mx.get_active_memory() / 2**20,
        )


def _maybe_sleep_tts_worker(idle_sec: float) -> bool:
    """Unload TTS weights and Metal cache on TTS-only worker after idle timeout."""
    global _tts_model, _voice_bank, _keepwarm_arrays, _tts_sleeping
    if TTS_IDLE_SLEEP_SECONDS <= 0 or not ASR_DISABLED:
        return False
    with _tts_lifecycle_lock:
        if _tts_sleeping or _tts_model is None:
            return False
        if not _tts_session_lock.acquire(blocking=False):
            return False
        try:
            if not _gpu_lock.acquire(timeout=0.1):
                return False
            try:
                cur_idle = time.time() - max(_last_inference_time, _startup_time)
                if cur_idle < TTS_IDLE_SLEEP_SECONDS:
                    return False
                _keepwarm_arrays = []
                _spk_embed_cache.clear()
                _voice_bank = {}
                _tts_model = None
                _tts_sleeping = True
                gc.collect()
                mx.clear_cache()
            finally:
                _gpu_lock.release()
        finally:
            _tts_session_lock.release()
    logger.info(
        "TTS worker entered idle sleep after %.0fs idle (MLX active=%.1f MB, cache=%.1f MB)",
        idle_sec,
        mx.get_active_memory() / 2**20,
        mx.get_cache_memory() / 2**20,
    )
    return True


async def _mlx_memory_keepwarm_loop():
    """Run lightweight Metal page keep-warm every 8 minutes, or sleep TTS-only worker after idle timeout."""
    idle_sleep_mode = ASR_DISABLED and TTS_IDLE_SLEEP_SECONDS > 0
    tick_sec = max(1, min(60, TTS_IDLE_SLEEP_SECONDS // 2)) if idle_sleep_mode else 480
    last_log_rotate = time.time()
    if ASR_DISABLED and not idle_sleep_mode:
        await asyncio.sleep(240)
    while True:
        try:
            await asyncio.sleep(tick_sec)
            now = time.time()
            if _GPU_HOLD_DIR.exists():
                await asyncio.to_thread(_reap_expired_gpu_holds)
            if not idle_sleep_mode or (now - last_log_rotate) >= 480:
                await asyncio.to_thread(_rotate_launchd_logs_if_needed)
                last_log_rotate = now
            if idle_sleep_mode:
                tts_idle_sec = now - max(_last_inference_time, _startup_time)
                if (
                    not _tts_sleeping
                    and _tts_model is not None
                    and tts_idle_sec >= TTS_IDLE_SLEEP_SECONDS
                    and not _tts_session_lock.locked()
                ):
                    await asyncio.to_thread(_maybe_sleep_tts_worker, tts_idle_sec)
                continue
            idle_sec = now - max(_last_inference_time, _last_keepwarm_time, _startup_time)
            if idle_sec < 280 or _tts_session_lock.locked() or not _is_on_ac_power():
                continue
            dt_ms = await asyncio.to_thread(_touch_mlx_weights_once)
            if dt_ms >= 0:
                logger.info("MLX memory keep-warm touched %d weight tensors in %.1fms", len(_keepwarm_arrays), dt_ms)
            if not ASR_DISABLED:
                asr_ms = await asyncio.to_thread(_warm_asr_once)
                if asr_ms >= 0:
                    logger.info(
                        "ASR keep-warm decode (%s) in %.0fms",
                        "SenseVoice + Qwen3-ASR" if _asr_model is not None else "Qwen3-ASR",
                        asr_ms,
                    )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("MLX keep-warm tick skipped: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI Lifespan & Voice Cloning Cache Warmup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _asr_model, _translation_asr_model, _tts_model, _voice_bank, _startup_time, _keepwarm_arrays, _last_inference_time, _keepwarm_clip
    t_start = time.perf_counter()
    _rotate_launchd_logs_if_needed()

    if SENSEVOICE_ENABLED and not ASR_DISABLED:
        logger.info("Loading ASR model from %s ...", ASR_MODEL_DIR)
        _asr_model = load_stt_model(str(ASR_MODEL_DIR))
    else:
        logger.info("SenseVoice disabled (ASR_SENSEVOICE=0): Qwen3-ASR serves all recognition")
    if ASR_DISABLED:
        _asr_model = None
        logger.info("ASR disabled (ASR_DISABLED=1): TTS-only worker")
    else:
        logger.info("Loading interpretation ASR model from %s ...", TRANSLATION_ASR_MODEL_DIR)
        _translation_asr_model = load_stt_model(str(TRANSLATION_ASR_MODEL_DIR))

    if _asr_model is not None or _translation_asr_model is not None:
        dummy_audio = np.zeros(8000, dtype=np.float32)
        with _gpu_lock:
            if _asr_model is not None:
                _asr_model.generate(dummy_audio, language="zh", use_itn=True)
            if _translation_asr_model is not None:
                _translation_asr_model.generate(dummy_audio, language=None, max_tokens=8, temperature=0)

    with _tts_lifecycle_lock:
        total_variants = _load_tts_and_voices_locked(run_decode_warmup=True)

    _startup_time = time.time()
    _last_inference_time = _startup_time
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckAccessFilter())
    if not ASR_DISABLED:
        clip_path = next(
            (v["audio_path"] for vinfo in _voice_bank.values() for v in vinfo.get("variants", ()) if Path(v.get("audio_path", "")).is_file()),
            None,
        )
        if clip_path:
            try:
                _keepwarm_clip = load_audio(str(clip_path), sample_rate=16000)[:24000]
            except Exception as exc:
                logger.warning("Failed to load keep-warm clip %s (%s), using silence fallback", clip_path, exc)
                _keepwarm_clip = mx.zeros((24000,), dtype=mx.float32)
        else:
            logger.warning("No voice reference WAV found in %s; using silence keep-warm clip", VOICES_DIR)
            _keepwarm_clip = mx.zeros((24000,), dtype=mx.float32)
        mx.eval(_keepwarm_clip)
        logger.info("ASR keep-warm decode at startup in %.0fms", _warm_asr_once())
    kw_init_ms = _touch_mlx_weights_once() if not (ASR_DISABLED and TTS_IDLE_SLEEP_SECONDS > 0) else 0.0
    _reap_expired_gpu_holds(resume_pid_if_idle=_find_listening_study_vision_pid(), remove_own_leases=True)
    keepwarm_task = asyncio.create_task(_mlx_memory_keepwarm_loop())
    logger.info(
        "Voice server ready in %.2fs! (ASR=%s, TTS=%s, Voices=%s, Variants=%d, KeepWarmTensors=%d in %.1fms, IdleSleep=%ds)",
        time.perf_counter() - t_start,
        "none (TTS-only worker)" if ASR_DISABLED else "SenseVoiceSmall" if _asr_model is not None else "Qwen3-ASR-1.7B only",
        TTS_MODEL_DIR.name,
        list(_voice_bank.keys()),
        total_variants,
        len(_keepwarm_arrays),
        kw_init_ms,
        TTS_IDLE_SLEEP_SECONDS if ASR_DISABLED else 0,
    )
    try:
        yield
    finally:
        keepwarm_task.cancel()
        _reap_expired_gpu_holds(resume_pid_if_idle=_find_listening_study_vision_pid(), remove_own_leases=True)
        if _overflow_client is not None:
            try:
                await _overflow_client.aclose()
            except Exception:
                pass
        if _relay_client is not None:
            try:
                await _relay_client.aclose()
            except Exception:
                pass


atexit.register(lambda: _reap_expired_gpu_holds(resume_pid_if_idle=_find_listening_study_vision_pid(), remove_own_leases=True))


app = FastAPI(
    title="ThinkSmart Local MLX Voice Server (Multi-Style Prosody Edition)",
    version="2.4.0",
    lifespan=lifespan,
)


class IndexStreamTTSRequest(BaseModel):
    text: str
    character: Optional[str] = None
    voice: Optional[str] = None
    language: str = "auto"
    mode: str = "chat"


class OpenAISpeechRequest(BaseModel):
    model: Optional[str] = "qwen3-tts-1.7b-base"
    input: str
    voice: Optional[str] = None
    response_format: Optional[str] = "wav"
    speed: Optional[float] = 1.0
    language: str = "auto"
    mode: str = "chat"


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _startup_time, 1) if _startup_time else 0,
        "asr_model": ("none (TTS-only worker)" if ASR_DISABLED else
                      ASR_MODEL_DIR.name if _asr_model is not None else "Qwen3-ASR-1.7B (SenseVoice disabled)"),
        "tts_overflow": TTS_OVERFLOW_URL or None,
        "mlx_memory_mb": {
            "active": round(mx.get_active_memory() / 2**20),
            "cache": round(mx.get_cache_memory() / 2**20),
            "peak": round(mx.get_peak_memory() / 2**20),
            "cache_limit": MLX_CACHE_LIMIT_MB,
        },
        "translation_asr_model": "Qwen3-ASR-1.7B",
        "translation_asr_languages": sorted(TRANSLATION_ASR_LANGUAGES),
        "tts_model": TTS_MODEL_DIR.name,
        "sample_rate": getattr(_tts_model, "sample_rate", _cached_sample_rate),
        "default_voice": DEFAULT_VOICE,
        "speakers": (
            {
                k: {
                    "name": v["name"],
                    "styles": [x["style"] for x in v["variants"]],
                }
                for k, v in _voice_bank.items()
            }
            if _voice_bank
            else _cached_speakers_meta
        ),
        "active_memory_gb": round(mx.get_active_memory() / 1e9, 2),
        "asr_languages": sorted(ASR_LANGUAGES),
        "tts_languages": list(getattr(_tts_model, "supported_languages", _cached_supported_languages)),
        "translation_streaming_interval": TRANSLATION_STREAMING_INTERVAL,
        "tts_busy": _tts_session_lock.locked(),
        "asr_busy": _active_asr_count > 0,
        "translation_busy": _active_translation_count > 0,
        "voice_active": _tts_session_lock.locked() or _active_asr_count > 0 or _active_translation_count > 0,
        "last_voice_age_s": round(max(0.0, time.time() - _last_voice_activity_time), 3) if _last_voice_activity_time > 0 else None,
        "last_asr_age_s": round(max(0.0, time.time() - _last_asr_activity_time), 3) if _last_asr_activity_time > 0 else None,
        "tts_sleeping": _tts_sleeping,
        "tts_idle_sleep_seconds": TTS_IDLE_SLEEP_SECONDS if ASR_DISABLED else 0,
        "keepwarm_tensors": len(_keepwarm_arrays),
        "keepwarm_count": _keepwarm_count,
        "last_keepwarm_age_s": round(time.time() - _last_keepwarm_time, 1) if _last_keepwarm_time else None,
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "SenseVoiceSmall", "object": "model", "owned_by": "local-mlx"},
            {"id": "Qwen3-ASR-1.7B", "object": "model", "owned_by": "local-mlx"},
            {
                "id": "Qwen3-TTS-12Hz-1.7B-Base-bf16",
                "object": "model",
                "owned_by": "local-mlx",
            },
        ],
    }


@app.post("/v1/audio/transcriptions")
async def create_transcription(
    file: UploadFile = File(...),
    model: Optional[str] = Form("SenseVoiceSmall"),
    language: Optional[str] = Form("auto"),
    response_format: Optional[str] = Form("json"),
    use_itn: bool = Form(False),
    prompt: Optional[str] = Form(None),
):
    language = (language or "auto").strip().lower().split("-", 1)[0]
    model = model or "SenseVoiceSmall"
    if model not in {"SenseVoiceSmall", "Qwen3-ASR-1.7B"}:
        raise HTTPException(status_code=400, detail="Unsupported ASR model")
    supported_languages = TRANSLATION_ASR_LANGUAGES if model == "Qwen3-ASR-1.7B" else ASR_LANGUAGES
    if language not in supported_languages:
        raise HTTPException(status_code=400, detail="Unsupported ASR language")
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    chunks: List[bytes] = []
    total_read = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > MAX_ASR_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Audio upload exceeds {MAX_ASR_UPLOAD_BYTES} bytes",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        if response_format == "text":
            return PlainTextResponse("")
        return {"text": ""}

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        prompt = (prompt or "").strip()
        if prompt == "@default" or (not prompt and model == "SenseVoiceSmall"):
            context = _default_asr_context()
        else:
            context = prompt[:600] or None
        res = await asyncio.to_thread(_transcribe_file, tmp_path, language, use_itn, model, context)
    except Exception as e:
        logger.exception("ASR transcription failed: %s", e)
        raise HTTPException(status_code=500, detail=f"ASR failed: {e}") from e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if response_format == "text":
        return PlainTextResponse(res["text"])
    if response_format == "verbose_json":
        return res
    return {"text": res["text"]}


_overflow_client: Optional[httpx.AsyncClient] = None
_relay_client: Optional[httpx.AsyncClient] = None


def _get_relay_client() -> httpx.AsyncClient:
    global _relay_client
    if _relay_client is None:
        _relay_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=16, max_connections=32, keepalive_expiry=300.0),
        )
    return _relay_client


async def _try_overflow_tts(req: "IndexStreamTTSRequest"):
    """Local TTS is busy: stream this request from the second worker if it is up and free."""
    global _overflow_client
    try:
        from starlette.background import BackgroundTask
        if _overflow_client is None:
            _overflow_client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=0.5),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16, keepalive_expiry=300.0),
            )
        health_resp = await _overflow_client.get(TTS_OVERFLOW_URL + "/health", timeout=0.5)
        if health_resp.status_code != 200 or health_resp.json().get("tts_busy"):
            return None
        upstream = await _overflow_client.send(
            _overflow_client.build_request("POST", TTS_OVERFLOW_URL + "/tts", json=req.model_dump()),
            stream=True,
        )
        if upstream.status_code != 200:
            await upstream.aclose()
            return None
        logger.info("TTS overflow -> %s (local TTS busy)", TTS_OVERFLOW_URL)
        return StreamingResponse(
            upstream.aiter_raw(),
            media_type="application/octet-stream",
            background=BackgroundTask(upstream.aclose),
        )
    except Exception as exc:
        logger.warning("TTS overflow worker unavailable (%s), queueing locally", type(exc).__name__)
        return None


@app.post("/tts")
async def xiaozhi_index_stream_tts(req: IndexStreamTTSRequest):
    """Streaming 24kHz 16-bit mono PCM endpoint for Xiaozhi IndexStreamTTS."""
    if TTS_OVERFLOW_URL and _tts_session_lock.locked():
        overflow = await _try_overflow_tts(req)
        if overflow is not None:
            return overflow
    voice = req.character or req.voice or DEFAULT_VOICE
    lang_key = (req.language or "auto").strip().lower().replace("_", "-").split("-", 1)[0]
    language = LANGUAGE_NAMES.get(lang_key, lang_key)
    supported_langs = getattr(_tts_model, "supported_languages", None) or _cached_supported_languages
    if supported_langs and language not in supported_langs:
        raise HTTPException(status_code=400, detail="Unsupported TTS language")
    if _tts_sleeping or _tts_model is None:
        await asyncio.to_thread(_ensure_tts_awake)
    return StreamingResponse(
        _async_tts_pcm_chunks(
            text=req.text,
            voice=voice,
            pad_to_opus_frame=True,
            language=language,
            translation=req.mode == "translation",
        ),
        media_type="application/octet-stream",
    )


@app.post("/v1/audio/speech")
async def openai_speech(req: OpenAISpeechRequest):
    """OpenAI-compatible TTS endpoint supporting 'pcm' (streaming) and 'wav'."""
    fmt = (req.response_format or "wav").lower()
    if fmt not in {"pcm", "wav"}:
        raise HTTPException(status_code=400, detail="Supported audio formats: pcm, wav")
    lang_key = (req.language or "auto").strip().lower().replace("_", "-").split("-", 1)[0]
    language = LANGUAGE_NAMES.get(lang_key, lang_key)
    supported_langs = getattr(_tts_model, "supported_languages", None) or _cached_supported_languages
    if supported_langs and language not in supported_langs:
        raise HTTPException(status_code=400, detail="Unsupported TTS language")
    if _tts_sleeping or _tts_model is None:
        await asyncio.to_thread(_ensure_tts_awake)
    if fmt == "pcm":
        return StreamingResponse(
            _async_tts_pcm_chunks(
                text=req.input,
                voice=req.voice,
                pad_to_opus_frame=False,
                language=language,
                translation=req.mode == "translation",
            ),
            media_type="audio/pcm",
        )

    pcm_parts = [part async for part in _async_tts_pcm_chunks(
        text=req.input, voice=req.voice, pad_to_opus_frame=False,
        language=language, translation=req.mode == "translation",
    )]
    wav_bytes = _pcm16_to_wav_bytes(b"".join(pcm_parts), TTS_SAMPLE_RATE)
    return Response(content=wav_bytes, media_type="audio/wav")


async def _relay_openai_v1(target_base_url: str, subpath: str, request: Request):
    target_url = f"{target_base_url.rstrip('/')}/{subpath}"
    body = await request.body()
    auth = request.headers.get("authorization", "")
    headers = {
        "Content-Type": request.headers.get("content-type", "application/json"),
        "User-Agent": "OpenAI/Python 2.8.1",
    }
    if auth:
        headers["Authorization"] = auth

    is_stream = False
    if body:
        try:
            payload = json.loads(body)
            is_stream = bool(payload.get("stream"))
        except Exception:
            pass

    rclient = _get_relay_client()
    if is_stream:
        from starlette.background import BackgroundTask

        up_req = rclient.build_request(
            request.method, target_url, content=body if body else None, headers=headers
        )
        try:
            upstream_resp = await rclient.send(up_req, stream=True)
        except httpx.HTTPError as e:
            return Response(
                content=json.dumps({"error": {"message": f"Upstream relay error: {e}", "type": "bad_gateway"}}),
                status_code=502,
                media_type="application/json",
            )

        if upstream_resp.status_code >= 400:
            try:
                err_body = await upstream_resp.aread()
            finally:
                await upstream_resp.aclose()
            return Response(
                content=err_body,
                status_code=upstream_resp.status_code,
                media_type=upstream_resp.headers.get("content-type", "application/json"),
            )

        closed = False

        async def _close_upstream():
            nonlocal closed
            if not closed:
                closed = True
                await upstream_resp.aclose()

        async def _stream_chunks():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await _close_upstream()

        return StreamingResponse(
            _stream_chunks(),
            status_code=upstream_resp.status_code,
            media_type="text/event-stream",
            background=BackgroundTask(_close_upstream),
        )

    try:
        up_resp = await rclient.request(
            request.method, target_url, content=body if body else None, headers=headers
        )
        return Response(
            content=up_resp.content,
            status_code=up_resp.status_code,
            media_type=up_resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as e:
        return Response(
            content=json.dumps({"error": {"message": f"Upstream relay error: {e}", "type": "bad_gateway"}}),
            status_code=502,
            media_type="application/json",
        )


UPSTREAM_OPENAI_URL = os.environ.get("UPSTREAM_OPENAI_URL", "https://api.openai.com/v1")
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8081/v1")
TRANSLATION_LLM_URL = os.environ.get("TRANSLATION_LLM_URL", "http://127.0.0.1:8082/v1")
LOCAL_SLM_MODEL = os.environ.get("LOCAL_SLM_MODEL", "local-slm-fast")


@app.api_route("/upstream/v1/{subpath:path}", methods=["GET", "POST"])
async def upstream_openai_proxy(subpath: str, request: Request):
    """Optional LAN relay to an upstream OpenAI-compatible endpoint."""
    return await _relay_openai_v1(UPSTREAM_OPENAI_URL, subpath, request)


@app.api_route("/local-llm/v1/{subpath:path}", methods=["GET", "POST"])
async def local_llm_proxy(subpath: str, request: Request):
    """Optional LAN bridge to a loopback-bound local OpenAI-compatible server."""
    return await _relay_openai_v1(LOCAL_LLM_URL, subpath, request)


@app.api_route("/translation/v1/{subpath:path}", methods=["GET", "POST"])
async def local_translation_proxy(subpath: str, request: Request):
    """Expose the loopback-only interpretation engine (e.g. Hy-MT2) over the LAN,
    coordinating Metal GPU priority against background study-vision."""
    global _active_translation_count
    if request.method != "POST":
        return await _relay_openai_v1(TRANSLATION_LLM_URL, subpath, request)
    _touch_voice_activity()
    with _active_translation_lock:
        _active_translation_count += 1
    pause_token = await asyncio.to_thread(_acquire_study_vision_pause, "translation", 20.0)
    released = False

    def _finish_translation() -> None:
        nonlocal released
        global _active_translation_count
        if released:
            return
        released = True
        try:
            _release_study_vision_pause(pause_token)
        finally:
            _touch_voice_activity()
            with _active_translation_lock:
                _active_translation_count = max(0, _active_translation_count - 1)

    try:
        resp = await _relay_openai_v1(TRANSLATION_LLM_URL, subpath, request)
        if isinstance(resp, StreamingResponse):
            orig_iter = resp.body_iterator

            async def _wrapped_stream():
                try:
                    async for chunk in orig_iter:
                        yield chunk
                finally:
                    await asyncio.to_thread(_finish_translation)

            resp.body_iterator = _wrapped_stream()
            return resp
        await asyncio.to_thread(_finish_translation)
        return resp
    except Exception:
        await asyncio.to_thread(_finish_translation)
        raise


SLM_ALLOWED_IPS = {
    ip.strip() for ip in os.environ.get("SLM_ALLOWED_IPS", "127.0.0.1,::1").split(",") if ip.strip()
}
_SLM_LOCAL_MODELS_CACHE = {"ts": 0.0, "ok": False, "models": set(), "unserved_logged": set()}
_SLM_LOCAL_MODELS_LOCK = threading.Lock()


def _is_local_llm_model_served(model_name: str, api_key: str) -> bool:
    """Check whether LOCAL_LLM_URL currently serves `model_name` (30s TTL, logs state changes once)."""
    m = (model_name or "").strip()
    if not m or not api_key:
        return False
    now = time.monotonic()
    ttl = 30.0 if _SLM_LOCAL_MODELS_CACHE["ok"] else 10.0
    if _SLM_LOCAL_MODELS_CACHE["ts"] == 0.0 or (now - _SLM_LOCAL_MODELS_CACHE["ts"]) >= ttl:
        with _SLM_LOCAL_MODELS_LOCK:
            now = time.monotonic()
            ttl = 30.0 if _SLM_LOCAL_MODELS_CACHE["ok"] else 10.0
            if _SLM_LOCAL_MODELS_CACHE["ts"] == 0.0 or (now - _SLM_LOCAL_MODELS_CACHE["ts"]) >= ttl:
                try:
                    req_m = urllib.request.Request(
                        f"{LOCAL_LLM_URL.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {api_key}"},
                        method="GET",
                    )
                    with urllib.request.urlopen(req_m, timeout=1.0) as r_m:
                        m_obj = json.loads(r_m.read().decode("utf-8"))
                        served = set()
                        for item in (m_obj.get("data") or []):
                            if isinstance(item, dict):
                                mid = str(item.get("id") or "").strip()
                                if mid:
                                    served.add(mid)
                                    if "/" in mid:
                                        served.add(mid.split("/", 1)[1].strip())
                        _SLM_LOCAL_MODELS_CACHE.update({"ts": now, "ok": True, "models": served})
                except Exception:
                    _SLM_LOCAL_MODELS_CACHE.update({"ts": now, "ok": False})
                    return False
    if not _SLM_LOCAL_MODELS_CACHE["ok"]:
        return False
    if m in _SLM_LOCAL_MODELS_CACHE["models"]:
        if m in _SLM_LOCAL_MODELS_CACHE["unserved_logged"]:
            _SLM_LOCAL_MODELS_CACHE["unserved_logged"].discard(m)
            logger.info("SLM local model %s is back online, resuming primary route", m)
        return True
    if m not in _SLM_LOCAL_MODELS_CACHE["unserved_logged"]:
        _SLM_LOCAL_MODELS_CACHE["unserved_logged"].add(m)
        logger.info("SLM local model %s not served, routing directly to fallback", m)
    return False


def _read_local_secret(filename: str) -> str:
    try:
        p = Path.home() / ".config" / "thinksmart-voice" / filename
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _preserve_permanent_memory_blocks(history_memory: str, candidate_summary: str) -> str:
    """Ensure any '永久保留区' blocks in history_memory are never dropped or overwritten by SLM errors."""
    hist = (history_memory or "").strip()
    cand = (candidate_summary or "").strip()
    if not hist or hist == "无历史记忆":
        return cand
    bad_markers = (
        "生成总结失败",
        "请稍后重试",
        "LLM服务不可用",
        "无法生成",
        "本次对话内容较少",
        "没有需要总结的重要信息",
        "服务暂不可用",
        "总结生成失败",
    )
    if not cand or len(cand) < 50 or any(m in cand for m in bad_markers):
        return hist
    missing_blocks = []
    for block in re.split(r"\n{2,}(?=【)", hist):
        block = block.strip()
        if not block.startswith("【"):
            continue
        header_end = block.find("】")
        if header_end <= 0:
            continue
        header = block[: header_end + 1]
        short_header = header.split("（", 1)[0].rstrip("】")
        if "永久保留" in header and short_header not in cand:
            missing_blocks.append(block)
    if missing_blocks:
        cand = "\n\n".join(missing_blocks) + "\n\n" + cand
    return cand


def _extract_fallback_title(prompt_text: str) -> str:
    for line in reversed((prompt_text or "").splitlines()):
        line = line.strip()
        if line.startswith(("用户:", "User:", "用户：")):
            raw = re.sub(r"^(?:用户|User)[：:]\s*", "", line)
            cleaned = re.sub(r"[，。！？、：；''\"\"【】（）\s,.!?;:]+", "", raw)
            if cleaned:
                return cleaned[:15]
    return "日常语音对话"


@app.api_route("/slm/v1/{subpath:path}", methods=["GET", "POST"])
async def slm_high_availability_proxy(subpath: str, request: Request):
    """High-availability SLM gateway for session title & memory summary generation:
    Primary: Local OpenAI-compatible SLM endpoint (LOCAL_LLM_URL)
    Fallback: Official DeepSeek API (deepseek-flash, thinking=disabled)
    Built-in Permanent-Memory Guardian & Zero-Error Title Fallback.
    """
    client_ip = request.client.host if request.client else ""
    if client_ip not in SLM_ALLOWED_IPS:
        logger.warning("SLM rejected caller %s", client_ip)
        return Response(
            content=json.dumps({"error": {"message": "forbidden"}}),
            status_code=403,
            media_type="application/json",
        )

    if subpath != "chat/completions" or request.method != "POST":
        return await _relay_openai_v1("https://api.deepseek.com/v1", subpath, request)

    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        return await _relay_openai_v1("https://api.deepseek.com/v1", subpath, request)

    if payload.get("stream"):
        return await _relay_openai_v1("https://api.deepseek.com/v1", subpath, request)

    messages = payload.get("messages") or []
    prompt_text = "\n".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
    is_summary = "你是一个经验丰富的记忆总结者" in prompt_text and "历史记忆：\n" in prompt_text
    is_title = "生成一个简洁的会话标题" in prompt_text
    history_memory = ""
    if is_summary:
        m = re.search(r"历史记忆：\n(.*?)\n\n新对话内容：\n", prompt_text, re.S)
        if m:
            history_memory = m.group(1).strip()

    local_llm_key = _read_local_secret("local_llm.key")
    incoming_auth = request.headers.get("authorization", "").strip()
    deepseek_key = _read_local_secret("deepseek.key")
    ds_auth = incoming_auth if incoming_auth else (f"Bearer {deepseek_key}" if deepseek_key else "")

    def _try_upstreams() -> Tuple[Optional[str], str]:
        if local_llm_key and _is_local_llm_model_served(LOCAL_SLM_MODEL, local_llm_key):
            try:
                local_req_body = {
                    "model": LOCAL_SLM_MODEL,
                    "messages": messages,
                    "reasoning_effort": "none",
                    "max_tokens": max(int(payload.get("max_tokens") or 1024), 256),
                    "temperature": float(payload.get("temperature") or 0.3),
                    "stream": False,
                }
                req_local = urllib.request.Request(
                    f"{LOCAL_LLM_URL.rstrip('/')}/chat/completions",
                    data=json.dumps(local_req_body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {local_llm_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req_local, timeout=15) as resp:
                    resp_obj = json.loads(resp.read().decode("utf-8"))
                    content = (
                        ((resp_obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    ).strip()
                    if content:
                        return content, LOCAL_SLM_MODEL
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    with _SLM_LOCAL_MODELS_LOCK:
                        _SLM_LOCAL_MODELS_CACHE["models"].discard(LOCAL_SLM_MODEL)
                        _SLM_LOCAL_MODELS_CACHE["ts"] = time.monotonic()
                        _SLM_LOCAL_MODELS_CACHE["unserved_logged"].add(LOCAL_SLM_MODEL)
                logger.warning("SLM local upstream failed (%s), falling back to DeepSeek", e)
            except Exception as e:
                logger.warning("SLM local upstream failed (%s), falling back to DeepSeek", e)

        if ds_auth:
            try:
                ds_body = {
                    "model": "deepseek-flash",
                    "messages": messages,
                    "max_tokens": int(payload.get("max_tokens") or 2000),
                    "temperature": float(payload.get("temperature") or 0.3),
                    "thinking": {"type": "disabled"},
                    "stream": False,
                }
                req_ds = urllib.request.Request(
                    "https://api.deepseek.com/v1/chat/completions",
                    data=json.dumps(ds_body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": ds_auth,
                        "User-Agent": "OpenAI/Python 2.8.1",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req_ds, timeout=25) as resp:
                    resp_obj = json.loads(resp.read().decode("utf-8"))
                    content = (
                        ((resp_obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    ).strip()
                    if content:
                        return content, "deepseek-flash"
            except Exception as e:
                logger.warning("SLM DeepSeek fallback failed (%s)", e)
        return None, "fallback-local"

    content, used_model = await asyncio.to_thread(_try_upstreams)
    if is_summary:
        content = _preserve_permanent_memory_blocks(history_memory, content or "")
    elif is_title and not content:
        content = _extract_fallback_title(prompt_text)

    if not content:
        return Response(
            content=json.dumps({"error": {"message": "SLM unavailable"}}),
            status_code=503,
            media_type="application/json",
        )

    out_obj = {
        "id": f"chatcmpl-slm-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": used_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": len(content), "total_tokens": len(content)},
    }
    return Response(content=json.dumps(out_obj, ensure_ascii=False), status_code=200, media_type="application/json")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8010"))
    uvicorn.run(app, host=host, port=port, log_level="info")
