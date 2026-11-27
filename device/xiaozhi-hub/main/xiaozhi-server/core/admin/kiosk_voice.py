"""大屏「设置 · 小麦」里由网关管的语音参数：听写引擎、音色、说完停顿、收音门槛、多久算聊完、对话记忆、呼名切换。

只读写下面白名单里的字段，带范围校验；密钥、接口地址、人设提示词都不在这里（在统一管理后台里改）。
读的是磁盘上合并后的配置，写 data/.config.yaml（config_store 往返保留注释、格式和 0600 权限）。
只放对这台设备真的起作用的：它在本机唤醒、自己响「在」，网关的唤醒应答和结束语用不到，所以不在这里。
接口只给本机的语音客户端用（admin_handler 里的 /kiosk/internal/voice，守卫同 /kiosk/internal/reload）。
2026-11-25
"""

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from core.admin import catalog, config_store, pages

# 装在本机、能直接选的语音合成；其余上游适配器已经删掉
ENGINES = (
    ("IndexStreamTTS", "家庭音色", "家里 Mac 实时合成，最自然"),
    ("EdgeTTS", "微软在线", "在线合成，音色多，可调语速音调"),
    ("OpenAITTS", "OpenAI 接口", "按管理后台里填的接口合成"),
)
# 听写：家里 Mac 上的服务，或装在本机的模型；别的识别服务在管理后台里选
ASR_ENGINES = (
    ("MacASR", "家里 Mac", "更准，Mac 关机时用不了"),
    ("SherpaXASR", "本机", "断网也能用，多占些内存"),
)
MAX_WAKE_LABELS = 12


def _entry(merged: dict, section: str, name: str) -> dict:
    e = ((merged.get(section) or {}).get(name)) or {}
    return e if isinstance(e, dict) else {}


def _int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) != float("inf") else default


def _vad_name(merged: dict) -> str:
    return str((merged.get("selected_module") or {}).get("VAD") or "SileroVAD")


def engine_ready(merged: dict, name: str) -> bool:
    e = _entry(merged, "TTS", name)
    if not e:
        return False
    if name == "EdgeTTS":
        return True
    url = str(e.get("api_url") or "").strip()
    if config_store.is_placeholder(url) or not url.startswith(("http://", "https://")):
        return False
    if name == "OpenAITTS" and "api.openai.com" in url.lower():
        return not config_store.is_placeholder(e.get("api_key"))
    return True


def asr_ready(merged: dict, name: str) -> bool:
    e = _entry(merged, "ASR", name)
    if name == "SherpaXASR":
        return bool((pages.asr_model_check(e) or {}).get("ok"))
    url = str(e.get("base_url") or "").strip()
    return e.get("type") == "openai" and not config_store.is_placeholder(url) and url.startswith(("http://", "https://"))


def describe(merged: dict) -> dict:
    selected = str((merged.get("selected_module") or {}).get("TTS") or "")
    edge = _entry(merged, "TTS", "EdgeTTS")
    asr = str((merged.get("selected_module") or {}).get("ASR") or "")
    vad = _entry(merged, "VAD", _vad_name(merged))
    history = merged.get("chat_history") if isinstance(merged.get("chat_history"), dict) else {}
    names = merged.get("call_names") if isinstance(merged.get("call_names"), dict) else {}
    return {
        "tts": {
            "selected": selected,
            "engines": [{"id": i, "label": label, "note": note} for i, label, note in ENGINES if engine_ready(merged, i)],
            "voice": str(_entry(merged, "TTS", selected).get("voice") or ""),
            "edge": {
                "voice": str(edge.get("voice") or "zh-CN-XiaoxiaoNeural"),
                "rate": max(-50, min(50, _int(edge.get("rate"), 0))),
                "pitch": max(-50, min(50, _int(edge.get("pitch"), 0))),
                "volume": max(-50, min(100, _int(edge.get("volume"), 50))),
            },
            "edge_voices": catalog.EDGE_VOICES,
            "openai": {"speed": round(max(0.5, min(2.0, _float(_entry(merged, "TTS", "OpenAITTS").get("speed"), 1.0))), 1)},
        },
        "weather_city": str(_entry(merged, "plugins", "get_weather").get("location") or ""),
        "asr": {
            "selected": asr,
            "engines": [{"id": i, "label": label, "note": note} for i, label, note in ASR_ENGINES if asr_ready(merged, i)],
        },
        "vad": {
            "silence_ms": max(200, min(5000, _int(vad.get("min_silence_duration_ms"), 650))),
            "threshold": round(max(0.1, min(0.9, _float(vad.get("threshold"), 0.5))), 2),
        },
        "session": {"no_voice_close_sec": max(30, min(600, _int(merged.get("close_connection_no_voice_time"), 120)))},
        "history": {
            "enabled": bool(history.get("enabled", True)),
            "max_messages": max(2, min(100, _int(history.get("max_messages"), 30))),
            "summary": bool(history.get("summary", True)),
        },
        "call_names": bool(names.get("enabled", True)),
    }


def _want_bool(name, v) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{name}要是开或关")
    return v


def _want_int(name, v, lo, hi) -> int:
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or int(v) != v:
        raise ValueError(f"{name}要是整数")
    if not lo <= v <= hi:
        raise ValueError(f"{name}要在 {lo}～{hi} 之间")
    return int(v)


def _want_float(name, v, lo, hi) -> float:
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{name}要是数字")
    if not lo <= v <= hi:
        raise ValueError(f"{name}要在 {lo}～{hi} 之间")
    return round(float(v), 2)


def plan(values: dict, merged: dict) -> list:
    """校验，返回要写的 [(路径, 值, 要热加载的段)]；有一个不对就整批不写（ValueError）。"""
    if not isinstance(values, dict) or not values:
        raise ValueError("没有要保存的内容")
    vad = _vad_name(merged)
    out = []
    for key, v in values.items():
        if key == "tts_engine":
            if v not in [i for i, _, _ in ENGINES] or not engine_ready(merged, v):
                raise ValueError("这个音色来源还没配置好，请先在管理后台填写")
            out.append((("selected_module", "TTS"), v, "TTS"))
        elif key == "edge_voice":
            if v not in catalog.EDGE_VOICE_VALUES:
                raise ValueError("不支持这个音色")
            out.append((("TTS", "EdgeTTS", "voice"), v, "TTS"))
        elif key == "edge_rate":
            out.append((("TTS", "EdgeTTS", "rate"), _want_int("语速", v, -50, 50), "TTS"))
        elif key == "edge_pitch":
            out.append((("TTS", "EdgeTTS", "pitch"), _want_int("音调", v, -50, 50), "TTS"))
        elif key == "edge_volume":
            out.append((("TTS", "EdgeTTS", "volume"), f"{_want_int('说话音量', v, -50, 100):+d}", "TTS"))
        elif key == "openai_speed":
            out.append((("TTS", "OpenAITTS", "speed"), _want_float("语速", v, 0.5, 2.0), "TTS"))
        elif key == "asr_engine":
            if v not in [i for i, _, _ in ASR_ENGINES] or not asr_ready(merged, v):
                raise ValueError("这个听写引擎还没准备好")
            out.append((("selected_module", "ASR"), v, "ASR"))
        elif key == "silence_ms":
            out.append((("VAD", vad, "min_silence_duration_ms"), _want_int("说完停顿", v, 200, 5000), "VAD"))
        elif key == "vad_threshold":
            out.append((("VAD", vad, "threshold"), _want_float("收音门槛", v, 0.1, 0.9), "VAD"))
        elif key == "no_voice_close_sec":
            out.append((("close_connection_no_voice_time",), _want_int("多久算聊完", v, 30, 600), ""))
        elif key == "history":
            out.append((("chat_history", "enabled"), _want_bool("聊天记录", v), ""))
        elif key == "max_messages":
            out.append((("chat_history", "max_messages"), _want_int("带上最近几句", v, 2, 100), ""))
        elif key == "summary":
            out.append((("chat_history", "summary"), _want_bool("长期记忆", v), ""))
        elif key == "call_names":
            out.append((("call_names", "enabled"), _want_bool("叫名字换模型", v), "call_names"))
        elif key == "wakeup_words":
            if not isinstance(v, list) or not 1 <= len(v) <= MAX_WAKE_LABELS or not all(isinstance(x, str) and 0 < len(x.strip()) <= 12 for x in v):
                raise ValueError("唤醒词列表格式不对")
            out.append((("wakeup_words",), [x.strip() for x in v], ""))
        elif key == "weather_city":
            if not isinstance(v, str) or not (1 <= len(v.strip()) <= 30):
                raise ValueError("城市名要在 1～30 个字之间")
            out.append((("plugins", "get_weather", "location"), v.strip(), "Intent"))
        else:
            raise ValueError(f"不支持的设置项：{key}")
    return out


def save(values: dict, merged: dict, path=None) -> dict:
    """写入有变化的字段。返回 {"changed": [路径...], "sections": 要热加载的段, "tts": 音色变了没有}。"""
    todo = plan(values, merged)
    changed, sections = [], set()
    with config_store._WRITE_LOCK:
        fresh = config_store.load_merged(custom_path=path) if path else config_store.load_merged()
        pending = []
        for p, v, section in todo:
            cur = fresh
            for seg in p:
                cur = cur.get(seg) if isinstance(cur, dict) else None
            if cur == v and type(cur) is type(v):
                continue
            pending.append((p, v, section))
        if pending:
            with config_store.edit_custom(path) as data:
                for p, v, section in pending:
                    target = data
                    for seg in p[:-1]:
                        nxt = target.get(seg)
                        if not isinstance(nxt, dict):
                            nxt = CommentedMap()
                            target[seg] = nxt
                        target = nxt
                    target[p[-1]] = CommentedSeq(v) if isinstance(v, list) else v
                    changed.append(".".join(p))
                    if section:
                        sections.add(section)
    return {"changed": changed, "sections": sorted(sections), "tts": "TTS" in sections}
