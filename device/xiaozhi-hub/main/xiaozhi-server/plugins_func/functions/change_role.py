from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

prompts = {
    "家庭管家": """我是家里智能屏幕上的全能助手{{assistant_name}}，温和贴心、回答精炼实用。
我熟悉家里的音乐电台、天气日历、番茄专注、留言提醒、服务器状态以及学习黑板功能。
日常回复自然口语化、言简意赅，需要操作屏幕或设备功能时优先调用对应工具。""",
    "伴学导师": """我是专门陪伴萌萌和朵朵学习的启发式导师{{assistant_name}}，耐心温和、循循善诱。
面对学科问题时，我坚持苏格拉底式提问引导，先帮孩子拆解已知条件和思路，绝不直接报出最终算式或答案；
遇到具体题目讲解、错题复测或听写批改时，主动调用屏幕上的 AI 动态黑板讲题工具。""",
    "英语老师": """我是一个叫{{assistant_name}}(Lily)的双语口语陪练老师，发音标准、鼓励式教学。
我会使用自然地道的英语搭配简短中文提示与你对话，如果你愿意，我可以全英文交流。
每次回复保持简短轻松，多用互动提问引导你开口多说多练，温和纠正表达中的小问题。""",
    "故事姐姐": """我是温柔生动的睡前故事与科普姐姐{{assistant_name}}，语气温柔舒缓、画面感强。
我擅长给孩子讲述少儿故事、历史典故、自然科学奥秘与诗词赏析，语言浅显优美、富有童趣。""",
}
_ROLE_ALIASES = {
    "管家": "家庭管家",
    "默认": "家庭管家",
    "默认模式": "家庭管家",
    "正常模式": "家庭管家",
    "小麦": "家庭管家",
    "伴学": "伴学导师",
    "伴学模式": "伴学导师",
    "学习导师": "伴学导师",
    "辅导老师": "伴学导师",
    "英语陪练": "英语老师",
    "口语老师": "英语老师",
    "讲故事": "故事姐姐",
    "故事模式": "故事姐姐",
}
change_role_function_desc = {
    "type": "function",
    "function": {
        "name": "change_role",
        "description": "切换语音助手的场景模式与对话风格，可选角色：[家庭管家, 伴学导师, 英语老师, 故事姐姐]",
        "parameters": {
            "type": "object",
            "properties": {
                "role_name": {"type": "string", "description": "助手称呼，默认填 小麦"},
                "role": {"type": "string", "description": "目标场景角色：家庭管家 / 伴学导师 / 英语老师 / 故事姐姐"},
            },
            "required": ["role", "role_name"],
        },
    },
}


@register_function("change_role", change_role_function_desc, ToolType.CHANGE_SYS_PROMPT)
def change_role(conn: "ConnectionHandler", role: str, role_name: str = "小麦"):
    """切换家庭助手场景角色"""
    norm_role = _ROLE_ALIASES.get(str(role or "").strip(), str(role or "").strip())
    if norm_role not in prompts:
        return ActionResponse(
            action=Action.RESPONSE,
            result="切换角色失败",
            response="目前支持切换为：家庭管家、伴学导师、英语老师或故事姐姐。",
        )
    safe_name = str(role_name or "小麦").strip()[:16] or "小麦"
    new_prompt = prompts[norm_role].replace("{{assistant_name}}", safe_name)
    if isinstance(getattr(conn, "config", None), dict):
        conn.config["prompt"] = new_prompt
    pm = getattr(conn, "prompt_manager", None)
    if pm is not None and callable(getattr(pm, "build_enhanced_prompt", None)):
        extra_kw = {"language": "中英双语（如用户愿意可全英文）"} if norm_role == "英语老师" else {}
        enhanced = pm.build_enhanced_prompt(
            new_prompt,
            getattr(conn, "device_id", None),
            getattr(conn, "client_ip", None),
            emoji_enabled=(getattr(conn, "features", None) or {}).get("emoji", True),
            **extra_kw,
        )
        if enhanced:
            new_prompt = enhanced
    conn.change_system_prompt(new_prompt)
    logger.bind(tag=TAG).info(f"切换助手场景角色:{norm_role}, 称呼:{safe_name}")
    res = f"好的，已切换到{norm_role}模式，我是{safe_name}。"
    return ActionResponse(action=Action.RESPONSE, result="切换角色已处理", response=res)
