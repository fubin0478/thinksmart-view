"""管理后台与本机 /kiosk 接口使用的配置摘要与模型校验（已精简旧独立后台写接口）。"""

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

from config.config_loader import get_project_dir
from core.admin import catalog, config_store
from core.agent import asr_text

URL = re.compile(r"^https?://\S+$")
VAD_SILENCE_FIELD = "min_silence_duration_ms"


def _check_vendor_url(base_url: str) -> None:
    if not URL.match(base_url):
        raise ValueError("接口地址需要以 http:// 或 https:// 开头")
    try:
        sp = urlsplit(base_url)
    except ValueError:
        raise ValueError("接口地址格式不对") from None
    if sp.scheme.lower() not in ("http", "https") or not sp.hostname or sp.username or sp.password:
        raise ValueError("接口地址格式不对")
    host = sp.hostname.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        if ip.is_unspecified or ip.is_multicast or ip.is_link_local:
            raise ValueError("接口地址不能指向保留或链路本地地址")
    except ValueError as e:
        if "保留或链路本地地址" in str(e):
            raise


def _selected(merged: dict, section: str):
    return (merged.get("selected_module") or {}).get(section)


def _entry(merged: dict, section: str, name: str) -> dict:
    entry = (merged.get(section) or {}).get(name)
    return entry if isinstance(entry, dict) else {}


def _text(value) -> str:
    return "" if config_store.is_placeholder(value) else str(value).strip()


def _url_field(entry: dict) -> str:
    return "url" if "url" in entry and "base_url" not in entry else "base_url"


def describe_vendor(vendor: dict, merged: dict) -> dict:
    entry = _entry(merged, "LLM", vendor["entry"])
    key = entry.get("api_key")
    base_url = _text(entry.get(_url_field(entry))) or vendor["base_url"]
    model = _text(entry.get("model_name")) or (vendor["models"][0] if vendor["models"] else "")
    key_set = not config_store.is_placeholder(key)
    exists = bool(entry) and entry.get("type") == "openai"
    return {
        "id": vendor["id"],
        "label": vendor["label"],
        "entry": vendor["entry"],
        "base_url": base_url,
        "model": model,
        "models": vendor["models"],
        "hint": vendor.get("hint", ""),
        "key_set": key_set,
        "key_hint": config_store.mask_key(key),
        "configured": exists and bool(base_url and model) and (key_set or vendor["id"] == "custom"),
        "conflict": bool(entry) and entry.get("type") != "openai",
    }


def llm_kind(merged: dict, name: str = None) -> str:
    name = name or _selected(merged, "LLM")
    if name in catalog.VENDOR_BY_ENTRY and config_store.entry_type(merged, "LLM", name) == "openai":
        return "api"
    return "other"


def describe_llm(merged: dict) -> dict:
    selected = _selected(merged, "LLM")
    kind = llm_kind(merged, selected)
    vendor = catalog.VENDOR_BY_ENTRY.get(selected) if kind == "api" else None
    return {
        "selected": selected,
        "kind": kind,
        "vendor": vendor["id"] if vendor else None,
        "vendors": [describe_vendor(v, merged) for v in catalog.LLM_VENDORS],
    }


def desired_intent(merged: dict, llm_name: str, path: Path = None):
    pref = (config_store.load_custom(path).get("admin") or {}).get("api_intent")
    if pref in catalog.INTENT_MODES:
        return pref
    return "function_call"


SHERPA_MODEL_FILES = {
    "sense_voice": ["model.int8.onnx", "tokens.txt"],
    "paraformer": ["model.int8.onnx", "tokens.txt"],
    "fire_red_asr": ["encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"],
    "fire_red_asr_ctc": ["model.int8.onnx", "tokens.txt"],
    "funasr_nano": ["llm.int8.onnx", "encoder_adaptor.int8.onnx", "embedding.int8.onnx", "Qwen3-0.6B"],
    "qwen3_asr": ["decoder.int8.onnx", "encoder.int8.onnx", "conv_frontend.onnx", "tokenizer"],
    "transducer": ["encoder*.onnx", "decoder*.onnx", "joiner*.onnx", "tokens.txt"],
}


def _find_model_file(model_dir: Path, name: str):
    if "*" not in name:
        path = model_dir / name
        return path if path.exists() else None
    matches = sorted(model_dir.glob(name), key=lambda p: (".int8." not in p.name, p.name))
    return matches[0] if matches else None


def asr_model_check(asr_entry: dict):
    if asr_entry.get("type") != "sherpa_onnx_local":
        return None
    files = SHERPA_MODEL_FILES.get(asr_entry.get("model_type") or "sense_voice", SHERPA_MODEL_FILES["sense_voice"])
    model_dir = Path(get_project_dir()) / (asr_entry.get("model_dir") or "")
    model = _find_model_file(model_dir, files[0]) or model_dir / files[0]
    size = model.stat().st_size if model.exists() else 0
    absent = [f for f in files if _find_model_file(model_dir, f) is None]
    ok = size > 100 * 1024 * 1024 and not absent
    if size and not absent:
        detail = f"{model.name} {size // (1024 * 1024)}MB"
    elif absent:
        detail = f"缺少 {model_dir / absent[0]}"
    else:
        detail = f"{model} 文件不完整"
    return {"ok": ok, "detail": detail}


hotwords_supported = asr_text.hotwords_supported


def describe_hotwords(merged: dict) -> dict:
    entry = _entry(merged, "ASR", _selected(merged, "ASR"))
    configured = asr_text.hotwords_of(merged)
    seen = {w.lower() for w in configured}
    result = {
        "supported": hotwords_supported(entry),
        "words": configured,
        "auto": [w for w in asr_text.call_name_hotwords(merged) if w.lower() not in seen],
        "dropped": [],
    }
    if entry.get("type") == "doubao_stream":
        multilingual = str(entry.get("enable_multilingual", False)).lower() != "false"
        _, result["dropped"] = asr_text.doubao_hotwords(asr_text.effective_hotwords(entry, merged), multilingual)
    return result
