"""提示词预览拆解（给统一管理后台 preview_prompt 使用，只读不改配置）。"""

import os
from datetime import datetime

from jinja2 import Template

from config.config_loader import get_project_dir
from core.admin import catalog, connections

MARK_START, MARK_SEP, MARK_END = "\x01", "\x02", "\x03"

VAR_LABELS = {
    "base_prompt": "人设",
    "current_time": "当前时间",
    "today_date": "日期",
    "today_weekday": "星期",
    "lunar_date": "农历",
    "emojiList": "表情列表",
    "dynamic_context": "动态上下文",
    "language": "回复语言",
}


def _fewshot_text() -> str:
    from core.utils.dialogue import Message, render_fewshot

    def call(name, args):
        return Message(role="assistant", tool_calls=[{"function": {"name": name, "arguments": args}}])

    return render_fewshot([
        Message(role="user", content="给我讲个故事吧"),
        call("direct_answer", '{"response": "好呀，你想听什么类型的呀？童话、冒险还是搞笑的？选一个我给你开讲~"}'),
        Message(role="tool", content="已直接回复"),
        Message(role="user", content="拜拜"),
        call("handle_exit_intent", '{"say_goodbye": "再见，下次再聊~"}'),
        Message(role="tool", content="退出意图已处理"),
        Message(role="assistant", content="再见，下次再聊~"),
    ])


def _mark(name: str, value) -> str:
    return f"{MARK_START}{name}{MARK_SEP}{value}{MARK_END}"


def _entry(merged: dict, section: str) -> tuple:
    name = (merged.get("selected_module") or {}).get(section)
    entry = (merged.get(section) or {}).get(name) or {}
    return name, entry if isinstance(entry, dict) else {}


def _online_conn():
    for item in connections.snapshot():
        conn = connections.get(item["session_id"])
        if conn is not None:
            return conn
    return None


def _system_prompt_of(conn) -> str:
    dialogue = getattr(getattr(conn, "dialogue", None), "dialogue", None) or []
    for msg in dialogue:
        if getattr(msg, "role", None) == "system":
            return str(getattr(msg, "content", "") or "")
    return ""


def _llm_label(name: str) -> str:
    vendor = catalog.VENDOR_BY_ENTRY.get(name)
    return vendor["label"] if vendor else name


def _template_name(merged: dict) -> str:
    return str(merged.get("prompt_template") or "hub-base-prompt.txt")


def _template_text(merged: dict) -> str:
    path = os.path.join(get_project_dir(), _template_name(merged))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _context_values(merged: dict) -> dict:
    from core.utils.current_time import get_current_date, get_current_lunar_date, get_current_weekday
    from core.utils.prompt_manager import EMOJI_List

    _, tts = _entry(merged, "TTS")
    return {
        "current_time": datetime.now().strftime("%H:%M"),
        "today_date": get_current_date(),
        "today_weekday": get_current_weekday(),
        "lunar_date": get_current_lunar_date(),
        "emojiList": EMOJI_List,
        "dynamic_context": "",
        "language": tts.get("language") or "中文",
    }


def render_base_prompt(merged: dict) -> str:
    template = _template_text(merged)
    persona = str(merged.get("prompt") or "")
    if not template:
        return _mark("base_prompt", persona)
    values = _context_values(merged)
    marked = {k: _mark(k, v) for k, v in values.items()}
    return Template(template).render(
        base_prompt=_mark("base_prompt", persona), emoji_enabled=True,
        device_id="", client_ip="", **marked,
    )


def describe_prompt(merged: dict) -> dict:
    llm_name, _ = _entry(merged, "LLM")
    _, intent = _entry(merged, "Intent")
    conn = _online_conn()
    base = render_base_prompt(merged)

    segments = [{
        "id": "base", "title": "基础模板（含人设）", "source": "服务端",
        "when": "设备连接时生成；当前时间每轮更新",
        "text": base, "collapsed": False,
        "note": f"来自 {_template_name(merged)}。高亮的部分是填进去的值：人设来自配置里的 prompt，日期、农历在设备连接时生成，当前时间每轮发送前更新。",
    }]
    if intent.get("type") == "function_call":
        segments.append({
            "id": "fewshot", "title": "工具调用示例", "source": "服务端", "when": "每轮",
            "text": _fewshot_text(), "collapsed": True,
            "note": "追加在系统提示词末尾的一段示例，教模型什么时候直接回答、什么时候调用工具。不会显示给用户。",
        })
        plugins = [p["label"] for p in catalog.PLUGINS if p["name"] in (intent.get("functions") or [])]
        segments.append({
            "id": "tools", "title": "工具清单", "source": "服务端", "when": "每轮",
            "text": "、".join(["直接回答（direct_answer）", *[p["label"] for p in catalog.ALWAYS_ON_PLUGINS], *plugins]),
            "collapsed": False,
            "note": "随请求一起发给模型的工具（OpenAI 协议的 tools 参数），每个工具带名字、用途和参数说明。",
        })
    segments.append({
        "id": "history", "title": "历史对话 + 这一轮的话", "source": "服务端", "when": "每轮",
        "text": "[user] 上一轮说的话\n[assistant] 上一轮的回复\n……\n[user] 这一轮识别出的文字", "collapsed": False,
        "note": "每轮都把这次连接以来的全部对话一起发送；由 chat_history 按窗口截断与总结。",
    })

    actual = _system_prompt_of(conn) if conn is not None else ""
    return {
        "llm": _llm_label(llm_name),
        "segments": segments,
        "warnings": [],
        "actual": {"device_id": getattr(conn, "device_id", None), "text": actual} if actual else None,
        "marks": {"start": MARK_START, "sep": MARK_SEP, "end": MARK_END},
        "var_labels": VAR_LABELS,
    }
