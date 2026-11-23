import asyncio
import json
import urllib.error
import urllib.request
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, Action, ActionResponse

TAG = __name__
logger = setup_logging()

# 2026-11-12 (Claude): bedtime. "我要睡觉了" starts a sleep session (the last sound, its own quiet volume, a long fade);
# "放海浪声，30 分钟后关" / "放到 23:30" / "放一整夜" / "再放半小时" go to the music service's sleep timer instead of an alarm.
play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": (
            "播放音乐、电台广播、少儿故事英语、相声评书，或控制播放（默认使用QQ音乐无损曲库，同时支持高音质网络电台、听书断点续播与U盘本地曲库），也负责睡前助眠和定时关闭。"
            "当用户要求播放歌曲、歌手、'我喜欢的音乐'、'每日推荐'、'我的歌单'、U盘音乐，"
            "或要求播放广播电台/音乐电台（如中国之声、音乐之声、亚洲音乐台、怀旧金曲广播、爵士轻音乐台、天津相声广播、交通广播、香港电台等）、"
            "少儿故事与英语名著（米小圈上学记、牛津树英语、新概念英语、唐诗三百首、凯叔讲故事、成语故事、睡前故事等）、"
            "相声评书（郭德纲相声、单田芳评书、三国演义评书、水浒传评书、刘兰芳岳飞传等），"
            "或者要求打开音乐/电台界面、暂停、继续播放、停止播放、下一首/下一台/下一集、上一首/上一台/上一集时调用本工具。"
            "当用户说要睡觉了、要助眠的声音（雨声、海浪、白噪音、褐噪音、助眠钢琴、摇篮曲等），"
            "或者要求'N分钟后关'、'几点关'、'放一整夜'、'再放半小时'时，也调用本工具（不要设闹钟或倒计时）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": (
                        "要播放的歌名、歌手名、电台/故事/评书名称（如'中国之声'、'亚洲音乐台'、'米小圈上学记'、'三国演义评书'）、'我喜欢的音乐'、'每日推荐'或控制指令。"
                        "控制指令可传：'pause'(暂停)、'resume'(继续)、'stop'(停止)、'next'(下一首/下一台)、'prev'(上一首/上一台)、'open_ui'(打开音乐界面)、'open_radio'(打开电台界面)。"
                        "若用户未指定具体歌曲（如'随便放首歌'），传 'random'。"
                        "用户说要睡觉了（没指定声音）传 'sleep'；说不睡了、关闭睡眠模式或关掉助眠传 'stop_sleep'；"
                        "只给正在放的声音加定时传 'timer'；延长定时传 'extend'；"
                        "要助眠声音时传声音名，如 '雨声'、'海浪'、'白噪音'、'褐噪音'、'助眠钢琴'、'摇篮曲'。"
                    ),
                },
                "sleep_minutes": {
                    "type": "integer",
                    "description": "多少分钟后慢慢变小并停止（定时关闭）；song_name 为 'extend' 时是要延长的分钟数。",
                },
                "sleep_until": {
                    "type": "string",
                    "description": "几点停止，24 小时制 HH:MM，例如 '23:30'。",
                },
                "all_night": {
                    "type": "boolean",
                    "description": "放一整夜：半小时后自动调小，早上 6:30 停止。",
                },
            },
            "required": ["song_name"],
        },
    },
}

SLEEP_START_WORDS = {'sleep', '睡觉', '睡眠', '睡眠模式', '我要睡觉了', '要睡觉了', '睡觉了', '晚安', '哄睡', '入睡', '助眠'}
SLEEP_STOP_WORDS = {
    'stop_sleep', '关闭睡眠', '关闭睡眠模式', '退出睡眠', '退出睡眠模式', '结束睡眠', '结束睡眠模式',
    '取消睡眠', '取消睡眠模式', '关闭助眠', '停止助眠', '关掉助眠', '不睡了', '起床了', '我醒了', '醒了',
}
SLEEP_SOUND_WORDS = {'助眠音乐', '睡眠音乐', '催眠曲', '摇篮曲', '助眠钢琴', '睡前轻音乐', '钢琴摇篮曲', '睡前音乐'}
SLEEP_COMBO_WORDS = {'雨夜壁炉', '深度入眠', '林间溪涧', '溪边琴音', '海边夏夜', '炉边听雨', '白噪细雨', '粉噪海浪', '褐噪细雨', '溪畔冥想', '围炉轻琴'}
TIMER_WORDS = {'timer', '定时', '定时关闭', '定时关', 'current', '当前', '正在放的'}
EXTEND_WORDS = {'extend', '延长', '再放', '再放一会', '多放一会', '再放一会儿'}

MUSIC_CACHE = {"music_files": [], "music_file_names": [], "scan_time": 0.0}
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def initialize_music_handler(conn=None):
    return dict(MUSIC_CACHE)


def _api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:8091{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=25) as r:
            data = json.loads(r.read(65536).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:  # the music service answers 400 with {"ok": false, "error": ...}
        try:
            data = json.loads(e.read(65536).decode("utf-8", errors="replace"))
        except Exception:
            data = {"ok": False}
    return data if isinstance(data, dict) else {}


def _sleep_timer_args(sleep_minutes=None, sleep_until=None, all_night=None) -> dict:
    out = {}
    if all_night is True:
        out["night"] = True
        return out
    if isinstance(sleep_until, str) and sleep_until.strip():
        out["until"] = sleep_until.strip()[:8]
        return out
    if sleep_minutes is not None and not isinstance(sleep_minutes, bool):
        try:
            m = int(float(sleep_minutes))
        except (TypeError, ValueError, OverflowError):
            m = 0
        if 1 <= m <= 240:
            out["minutes"] = m
    return out


def _say(msg: str):
    return ActionResponse(action=Action.RESPONSE, result=msg, response=msg)


@register_function("play_music", play_music_function_desc, ToolType.SYSTEM_CTL)
async def play_music(conn, song_name: str = "random", sleep_minutes=None, sleep_until=None, all_night=None):
    try:
        q = ("" if isinstance(song_name, bool) else (song_name if isinstance(song_name, str) else str(song_name or ""))).strip() or "random"
        q_low = q.lower()
        timer = _sleep_timer_args(sleep_minutes, sleep_until, all_night)

        # ---- bedtime (2026-11-12) ----
        q_clean = q_low.strip(' ，。！？,.!?《》"\'“”')
        if q_clean in SLEEP_STOP_WORDS:
            res = await asyncio.to_thread(_api_post, "/api/music/sleep_session", {"action": "stop"})
            return _say(res.get("message") or "好的，已停止")
        for sp in ("关闭", "关掉", "停止", "取消", "停掉", "别放"):
            if q_clean.startswith(sp):
                tail = q_clean[len(sp):].strip()
                if tail in ("睡眠", "睡眠模式", "助眠", "助眠模式") or tail in SLEEP_SOUND_WORDS or tail in SLEEP_COMBO_WORDS:
                    res = await asyncio.to_thread(_api_post, "/api/music/sleep_session", {"action": "stop"})
                    return _say(res.get("message") or "好的，已停止")
        if q_low in EXTEND_WORDS:
            res = await asyncio.to_thread(_api_post, "/api/music/sleep_session",
                                          {"action": "extend", "minutes": timer.get("minutes", 30)})
            return _say(res.get("message") or res.get("error") or "没能延长定时")
        if q_low in SLEEP_START_WORDS or q_low in SLEEP_SOUND_WORDS or q_low in SLEEP_COMBO_WORDS:
            res = await asyncio.to_thread(_api_post, "/api/music/sleep_session",
                                          dict({"action": "start", "sound": "last" if q_low in SLEEP_START_WORDS else q}, **timer))
            return _say(res.get("message") or res.get("error") or "睡眠模式没能开始")
        if timer and (q_low in TIMER_WORDS or q_low in ("random", "resume", "继续", "继续播放")):
            res = await asyncio.to_thread(_api_post, "/api/music/sleep", dict(timer, fade=300))
            return _say(res.get("message") or res.get("error") or "没能设定定时")
        if timer:
            # a named sound with a timer: a sleep session when it is a bedtime sound, else play it and set the timer
            res = await asyncio.to_thread(_api_post, "/api/music/sleep_session", dict({"action": "start", "sound": q}, **timer))
            if res.get("ok"):
                return _say(res.get("message") or "好的，晚安")
            played = await asyncio.to_thread(_api_post, "/api/music/play", {"query": q, "source": "auto"})
            if not played.get("ok"):
                return _say(played.get("error") or f"抱歉，没有找到《{q}》的可播放音源")
            tres = await asyncio.to_thread(_api_post, "/api/music/sleep", dict(timer, fade=300))
            title = played.get("title", q)
            return _say(f"正在播放《{title}》。" + (tres.get("message") or ""))

        ctrl_map = {
            "pause": "pause", "暂停": "pause", "暂停播放": "pause",
            "resume": "resume", "继续": "resume", "继续播放": "resume",
            "stop": "stop", "停止": "stop", "停止播放": "stop", "关闭音乐": "stop", "关掉音乐": "stop", "别唱了": "stop", "不听了": "stop", "别讲了": "stop",
            "关闭电台": "stop", "关掉电台": "stop", "停止电台": "stop", "关闭收音机": "stop", "关掉收音机": "stop", "关闭广播": "stop", "关掉广播": "stop",
            "next": "next", "下一首": "next", "换一首": "next", "切歌": "next", "下一台": "next", "换台": "next", "换个台": "next", "下一个台": "next", "下一集": "next",
            "prev": "prev", "上一首": "prev", "上一台": "prev", "上一个台": "prev", "上一集": "prev",
            "open": "open_ui", "open_ui": "open_ui", "打开音乐": "open_ui", "打开音乐界面": "open_ui", "打开qq音乐": "open_ui",
            "open_radio": "open_radio", "打开电台界面": "open_radio", "打开收音机界面": "open_radio", "电台界面": "open_radio", "收音机界面": "open_radio",
            "mode": "toggle_mode", "toggle_mode": "toggle_mode", "切换模式": "toggle_mode", "切换播放模式": "toggle_mode",
        }
        if q_low in ctrl_map:
            act = ctrl_map[q_low]
            res = await asyncio.to_thread(_api_post, "/api/music/control", {"action": act})
            if res.get("ok"):
                t = res.get("track")
                if isinstance(t, dict) and act in ("next", "prev", "resume") and t.get("title"):
                    artist = f"{t.get('artist', '')}的" if t.get("artist") else ""
                    msg = f"好的，正在为您播放{artist}《{t.get('title', '')}》"
                    return ActionResponse(action=Action.RESPONSE, result=msg, response=msg)
                msg = res.get("message", "好的")
                return ActionResponse(action=Action.RESPONSE, result=msg, response=msg)
            return ActionResponse(action=Action.RESPONSE, result="失败", response=res.get("error", "操作失败"))

        res = await asyncio.to_thread(_api_post, "/api/music/play", {"query": q, "source": "auto"})
        if res.get("ok"):
            if res.get("mode_only") and res.get("message"):
                msg = str(res["message"])
                return ActionResponse(action=Action.RESPONSE, result=msg, response=msg)
            title = res.get("title", q)
            artist = res.get("artist", "")
            source = res.get("source", "QQ音乐")
            artist_str = f"{artist}的" if artist else ""
            return ActionResponse(
                action=Action.RESPONSE,
                result="正在播放",
                response=f"正在通过{source}为您播放{artist_str}《{title}》",
            )
        return ActionResponse(
            action=Action.RESPONSE,
            result="未找到歌曲",
            response=res.get("error", f"抱歉，没有找到《{q}》的可播放音源"),
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"play_music failed: {e}")
        return ActionResponse(action=Action.RESPONSE, result=str(e), response="播放音乐时出错了，请稍后再试")
