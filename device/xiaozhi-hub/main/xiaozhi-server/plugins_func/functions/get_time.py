from datetime import datetime
import json
import time
import urllib.request
import cnlunar
from plugins_func.register import register_function, ToolType, ActionResponse, Action

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ORIG_NO_PROXY_OPEN = _NO_PROXY_OPENER.open
_CAL_MONTH_CACHE = {}


def _fetch_home_holiday_note(target_dt: datetime) -> str:
    """Query ThinkSmart Home :8095/api/home/calendar for authoritative State Council statutory holiday & makeup-workday schedule."""
    ym = target_dt.strftime("%Y-%m")
    target_iso = target_dt.strftime("%Y-%m-%d")
    use_memo = (_NO_PROXY_OPENER.open is _ORIG_NO_PROXY_OPEN)
    now_m = time.monotonic()
    hit = _CAL_MONTH_CACHE.get(ym) if use_memo else None
    if hit and (now_m - hit[0]) < 60.0:
        data = hit[1]
    else:
        req = urllib.request.Request(
            f"http://127.0.0.1:8095/api/home/calendar?ym={ym}",
            headers={"Host": "127.0.0.1:8095", "Accept": "application/json"},
            method="GET",
        )
        try:
            with _NO_PROXY_OPENER.open(req, timeout=2.0) as resp:
                data = json.loads(resp.read(131072).decode("utf-8", errors="replace"))
        except Exception:
            return ""
        if isinstance(data, dict) and use_memo:
            if len(_CAL_MONTH_CACHE) >= 8:
                _CAL_MONTH_CACHE.clear()
            _CAL_MONTH_CACHE[ym] = (now_m, data)
    if not isinstance(data, dict):
        return ""
    days = ((data.get("month") or {}).get("days")) or []
    notes = []
    for cell in days:
        if isinstance(cell, dict) and cell.get("date") == target_iso:
            hol = cell.get("hol")
            hol_name = str(cell.get("hol_name") or "").strip()
            if hol == "off":
                notes.append(f"国家法定节假日安排：{hol_name or '法定假日'}放假休息")
            elif hol == "work":
                notes.append(f"国家法定节假日安排：{hol_name or '节假日'}调休补班（正常上班/上学）")
            else:
                notes.append(
                    "国家法定节假日安排：周末休息" if cell.get("weekend") else "国家法定节假日安排：普通工作日"
                )
            cd = cell.get("cd")
            if isinstance(cd, list) and cd:
                titles = [str(x.get("title") or "") if isinstance(x, dict) else str(x) for x in cd if x]
                titles = [t.strip() for t in titles if t.strip()]
                if titles:
                    notes.append("家庭日程/纪念日：" + "、".join(titles[:4]))
            elif isinstance(cd, str) and cd.strip():
                notes.append("家庭日程/纪念日：" + cd.strip())
            break
    upcoming = [
        f"{c.get('date')}({c.get('hol_name')}{'调休上班' if c.get('hol') == 'work' else '放假'})"
        for c in days
        if isinstance(c, dict) and str(c.get("date") or "") > target_iso and c.get("hol") in ("off", "work")
    ]
    if upcoming:
        notes.append("当月后续法定放假/调休：" + "，".join(upcoming[:6]))
    return "\n".join(notes)


get_lunar_function_desc = {
    "type": "function",
    "function": {
        "name": "get_lunar",
        "description": (
            "用于查询具体日期的阴历/农历、国务院法定节假日放假与调休安排、节气、天干地支、生肖、星座、八字、宜忌及家庭纪念日信息。"
            "用户可以指定查询内容，如：今天/某天放假吗、调休安排、阴历日期、节日、节气、生肖、宜忌等。"
            "如果没有指定查询内容，则默认查询干支年、农历日期与法定放假调休状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "要查询的日期，格式为YYYY-MM-DD，例如2026-10-01。如果不提供，则使用当前日期",
                },
                "query": {
                    "type": "string",
                    "description": "要查询的内容，例如放假调休、阴历日期、天干地支、节日、节气、生肖、星座、八字、宜忌等",
                },
            },
            "required": [],
        },
    },
}


@register_function("get_lunar", get_lunar_function_desc, ToolType.WAIT)
def get_lunar(date=None, query=None):
    """
    用于获取当前的阴历/农历，和天干地支、节气、生肖、星座、八字、宜忌等黄历信息
    """
    from core.utils.cache.manager import cache_manager, CacheType

    # 如果提供了日期参数，则使用指定日期；否则使用当前日期
    if date:
        try:
            now = datetime.strptime(date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return ActionResponse(
                Action.REQLLM,
                f"日期格式错误，请使用YYYY-MM-DD格式，例如：2024-01-01",
                None,
            )
    else:
        now = datetime.now()

    current_date = now.strftime("%Y-%m-%d")

    # 如果 query 为 None，则使用默认文本
    if query is None:
        query = "默认查询干支年和农历日期"

    home_holiday_note = _fetch_home_holiday_note(now)
    lunar_cache_key = f"lunar_base_{current_date}"
    lunar_body = cache_manager.get(CacheType.LUNAR, lunar_cache_key)
    if not lunar_body:
        lunar = cnlunar.Lunar(now, godType="8char")
        lunar_body = (
            "农历信息：\n"
            "%s年%s%s\n" % (lunar.lunarYearCn, lunar.lunarMonthCn[:-1], lunar.lunarDayCn)
            + "干支: %s年 %s月 %s日\n" % (lunar.year8Char, lunar.month8Char, lunar.day8Char)
            + "生肖: 属%s\n" % (lunar.chineseYearZodiac)
            + "八字: %s\n"
            % (
                " ".join(
                    [lunar.year8Char, lunar.month8Char, lunar.day8Char, lunar.twohour8Char]
                )
            )
            + "今日节日: %s\n"
            % (
                ",".join(
                    filter(
                        None,
                        (
                            lunar.get_legalHolidays(),
                            lunar.get_otherHolidays(),
                            lunar.get_otherLunarHolidays(),
                        ),
                    )
                )
            )
            + "今日节气: %s\n" % (lunar.todaySolarTerms)
            + "下一节气: %s %s年%s月%s日\n"
            % (
                lunar.nextSolarTerm,
                lunar.nextSolarTermYear,
                lunar.nextSolarTermDate[0],
                lunar.nextSolarTermDate[1],
            )
            + "今年节气表: %s\n"
            % (
                ", ".join(
                    [
                        f"{term}({date[0]}月{date[1]}日)"
                        for term, date in lunar.thisYearSolarTermsDic.items()
                    ]
                )
            )
            + "生肖冲煞: %s\n" % (lunar.chineseZodiacClash)
            + "星座: %s\n" % (lunar.starZodiac)
            + "纳音: %s\n" % lunar.get_nayin()
            + "彭祖百忌: %s\n" % (lunar.get_pengTaboo(delimit=", "))
            + "值日: %s执位\n" % lunar.get_today12DayOfficer()[0]
            + "值神: %s(%s)\n"
            % (lunar.get_today12DayOfficer()[1], lunar.get_today12DayOfficer()[2])
            + "廿八宿: %s\n" % lunar.get_the28Stars()
            + "吉神方位: %s\n" % " ".join(lunar.get_luckyGodsDirection())
            + "今日胎神: %s\n" % lunar.get_fetalGod()
            + "宜: %s\n" % "、".join(lunar.goodThing[:10])
            + "忌: %s\n" % "、".join(lunar.badThing[:10])
            + "(默认返回干支年和农历日期；仅在要求查询宜忌信息时才返回本日宜忌)"
        )
        cache_manager.set(CacheType.LUNAR, lunar_cache_key, lunar_body)

    response_text = f"根据以下信息回应用户的查询请求，并提供与{query}相关的信息：\n"
    if home_holiday_note:
        response_text += home_holiday_note + "\n"
    response_text += lunar_body
    return ActionResponse(Action.REQLLM, response_text, None)
