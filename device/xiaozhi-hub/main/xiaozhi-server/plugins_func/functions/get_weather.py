"""查天气：天气和空气质量用 Open-Meteo，地名用 OpenStreetMap 的 Nominatim 换成经纬度（查不到再试 Open-Meteo 自带的地名查询）。
都免费、不用 Key。原来用和风天气：项目共用的 Key 已经失效（2026-11 起返回 401），天气数据还是抓网页解析的，已舍弃。

没说地点时查「本地」：后台设置的所在地 > 宽带公网 IP 定位的城市 > 配置里的 default_location。
"""

import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OM_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
# Nominatim 的使用规定：要带能认出应用的 User-Agent，每秒最多 1 次，结果要缓存
USER_AGENT = "thinksmart-view weather plugin/1.0"
TIMEOUT = httpx.Timeout(8.0, connect=4.0)  # 设备上实测每个接口 1.5 秒左右
NOMINATIM_INTERVAL = 1.1
DEFAULT_LOCATION = "广州"
FORECAST_DAYS = 7
REPORT_TTL = 3600  # 天气缓存 1 小时
PLACE_MISS_TTL = 600  # 查不到的地名 10 分钟内不再查
WEEKDAYS = "一二三四五六日"

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查天气：现在的天气、未来 7 天预报、接下来 12 小时会不会下雨下雪、空气质量。用户没说地点时不传 location，查本地。",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "地点，城市或区县，例如 杭州、深圳南山区、东京；用户说的是省份就传省会城市（广东→广州），景点请换成它所在的区县或城市。查本地天气时不传",
                },
            },
            "required": [],
        },
    },
}

# WMO 天气代码 https://open-meteo.com/en/docs
WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴有冰雹", 99: "雷阵雨伴有冰雹",
}
RAIN_CODES = set(range(51, 68)) | {80, 81, 82}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
WIND_DIRS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
# 风速（km/h）上限 → 风力等级
BEAUFORT = [1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118]
# PM2.5（μg/m³，24 小时）分级，HJ 633
PM25_LEVELS = [(35, "优"), (75, "良"), (115, "轻度污染"), (150, "中度污染"), (250, "重度污染")]


class WeatherError(Exception):
    """查天气失败，message 可以直接告诉用户。"""


@dataclass(frozen=True)
class Place:
    name: str  # 念给用户听的名字，例如 天河区（广州市）
    lat: float
    lon: float


# ---------- 地名 ----------

import weakref

_places = {}  # 地名 -> (时间, Place 或 None)
_nominatim_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_nominatim_last = 0.0


def _get_nominatim_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lk = _nominatim_locks.get(loop)
    if lk is None:
        lk = asyncio.Lock()
        _nominatim_locks[loop] = lk
    return lk


def _client():
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})


async def _nominatim(name: str):
    global _nominatim_last
    async with _get_nominatim_lock():
        wait = _nominatim_last + NOMINATIM_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with _client() as client:
                # featureType=settlement：只在城市、区县、乡镇这些居民点和行政区里找。不加的话模糊匹配太宽，
                # 「杭州西湖区」会查成一所大学、乱说的地名会查成某家餐馆
                resp = await client.get(NOMINATIM_URL, params={
                    "q": name, "format": "jsonv2", "limit": 1, "accept-language": "zh-CN",
                    "featureType": "settlement"})
        finally:
            _nominatim_last = time.monotonic()
    resp.raise_for_status()
    items = resp.json()
    if not items:
        return None
    item = items[0]
    # display_name 例如「天河区, 广州市, 广东省, 中国」；国外的会带几种写法，例如「纽约;紐約, 纽约州;紐約州, 美国;美國」「东京都/東京都, 日本」
    parts = [p.split(";")[0].split("/")[0].strip() for p in item.get("display_name", name).split(",")]
    return Place(_label(parts), float(item["lat"]), float(item["lon"]))


def _label(parts) -> str:
    """念给用户听的地名：本身 +（省市区），例如「南山街道（广东省深圳市南山区）」。只写上一级会分不清同名的
    （深圳和鹤岗都有南山区）。去掉邮编和最后的国家名。"""
    parts = [p for p in parts if p and not p.isdigit()]
    if not parts:
        return ""
    upper = list(reversed(parts[1:-1]))  # 中间是上级行政区，最后一个是国家
    return parts[0] + (f"（{''.join(upper)}）" if upper else "")


async def _open_meteo_geo(name: str):
    """Open-Meteo 的地名查询只认得大城市，而且不认「市」「区」后缀；按人口挑最大的那个。"""
    async with _client() as client:
        for q in dict.fromkeys([name, name.rstrip("市")]):
            resp = await client.get(OM_GEO_URL, params={"name": q, "count": 10, "language": "zh", "format": "json"})
            resp.raise_for_status()
            results = [r for r in resp.json().get("results") or [] if isinstance(r, dict) and r.get("name") and r.get("latitude") is not None and r.get("longitude") is not None]
            if results:
                r = max(results, key=lambda r: r.get("population") or 0)
                admin = r.get("admin1")
                label = r["name"] + (f"（{admin}）" if admin and admin != r["name"] else "")
                return Place(label, float(r["latitude"]), float(r["longitude"]))
    return None


async def find_place(name: str) -> Place:
    name = name.strip()
    cached = _places.get(name)
    if cached and (cached[1] is not None or time.time() - cached[0] < PLACE_MISS_TTL):
        if cached[1] is None:
            raise WeatherError(f"没找到「{name}」这个地方")
        return cached[1]
    place, failed, any_ok = None, None, False
    for lookup in (_nominatim, _open_meteo_geo):
        try:
            place = await lookup(name)
            any_ok = True
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
            failed = e
            logger.bind(tag=TAG).warning(f"查地名「{name}」失败（{lookup.__name__}）: {e!r}")
            continue
        if place:
            break
    if place is None and failed is not None and not any_ok:
        raise WeatherError("地名查询服务连不上")
    if len(_places) >= 256:
        now_ts = time.time()
        for k, v in list(_places.items()):
            if v[1] is None and now_ts - v[0] >= PLACE_MISS_TTL:
                _places.pop(k, None)
        while len(_places) >= 256:
            _places.pop(next(iter(_places)), None)
    _places[name] = (time.time(), place)
    if place is None:
        raise WeatherError(f"没找到「{name}」这个地方")
    return place


# ---------- 天气 ----------

_reports = {}  # 经纬度 -> (时间, 报告)


def _num(value, digits=0):
    if value is None:
        return "?"
    return f"{value:.{digits}f}" if digits else f"{round(value)}"


def _wind(speed_kmh, degrees) -> str:
    if speed_kmh is None:
        return ""
    level = next((i for i, top in enumerate(BEAUFORT) if speed_kmh < top), 12)
    if level == 0:
        return "无风"
    direction = WIND_DIRS[round((degrees or 0) / 45) % 8] + "风" if degrees is not None else "风"
    return f"{direction} {level} 级"


def _day_weather(code, rain_mm) -> str:
    """一天的天气：Open-Meteo 的日代码取一天里最严重的天气，零点几毫米的毛毛雨也会报成雨（和和风对比过），
    雨量不到 1 毫米说成「多云，可能有零星小雨」。"""
    text = WMO.get(code, "未知")
    if code in RAIN_CODES and (rain_mm or 0) < 1.0:
        return "多云，可能有零星小雨"
    if code in (51, 53, 55):
        return "小雨"
    return text


def _pm25_level(pm25) -> str:
    return next((name for top, name in PM25_LEVELS if pm25 <= top), "严重污染")


def _day_label(day: date, today: date) -> str:
    names = {0: "今天", 1: "明天", 2: "后天"}
    prefix = names.get((day - today).days, "")
    return f"{prefix}{day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}"


def _rain_next_hours(hourly: dict, now: datetime, hours=12) -> str:
    """接下来 12 小时哪几个钟头可能下雨或下雪（概率 ≥ 50% 或降水 ≥ 0.3 毫米）。"""
    wet = []
    for t, prob, mm, code in zip(hourly.get("time", []), hourly.get("precipitation_probability", []),
                                 hourly.get("precipitation", []), hourly.get("weather_code", [])):
        start = datetime.fromisoformat(t)
        if start + timedelta(hours=1) <= now or start > now + timedelta(hours=hours):
            continue
        if (prob or 0) >= 50 or (mm or 0) >= 0.3:
            wet.append((start, prob or 0, code))
    if not wet:
        return f"接下来 {hours} 小时：基本不会下雨"
    what = "下雪" if any(code in SNOW_CODES for _, _, code in wet) else "下雨"

    def clock(t):
        return ("" if t.date() == now.date() else "明天") + f"{t.hour} 点"

    first, last = wet[0][0], wet[-1][0] + timedelta(hours=1)
    top = max(p for _, p, _ in wet)
    span = clock(first) if len(wet) == 1 else f"{clock(first)}到{clock(last)}"
    return f"接下来 {hours} 小时：{span}可能{what}，降水概率最高 {top}%"


def format_report(place: Place, forecast: dict, air: dict = None) -> str:
    cur = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    now = datetime.fromisoformat(cur["time"]) if cur.get("time") else datetime.now()
    lines = [f"地点：{place.name}"]
    wind = _wind(cur.get("wind_speed_10m"), cur.get("wind_direction_10m"))
    lines.append(
        f"现在（{now:%H:%M}）：{WMO.get(cur.get('weather_code'), '未知')}，{_num(cur.get('temperature_2m'))}°C，"
        f"体感 {_num(cur.get('apparent_temperature'))}°C，湿度 {_num(cur.get('relative_humidity_2m'))}%"
        + (f"，{wind}" if wind else "")
    )
    pm25 = ((air or {}).get("current") or {}).get("pm2_5")
    if pm25 is not None:
        lines.append(f"空气质量：{_pm25_level(pm25)}（PM2.5 {_num(pm25)}）")
    lines.append(_rain_next_hours(forecast.get("hourly") or {}, now))
    today = now.date()

    def column(name, i):
        values = daily.get(name) or []
        return values[i] if i < len(values) else None

    for i, day in enumerate(daily.get("time", [])):
        d = date.fromisoformat(day)
        weather = _day_weather(column("weather_code", i), column("precipitation_sum", i))
        prob = column("precipitation_probability_max", i)
        lines.append(
            f"{_day_label(d, today)}：{weather}，{_num(column('temperature_2m_min', i))}～"
            f"{_num(column('temperature_2m_max', i))}°C" + (f"，降水概率 {prob}%" if prob is not None else "")
        )
    return "\n".join(lines)


async def fetch_report(place: Place) -> str:
    key = f"{place.lat:.2f},{place.lon:.2f}"
    cached = _reports.get(key)
    if cached and time.time() - cached[0] < REPORT_TTL:
        return cached[1]
    common = {"latitude": place.lat, "longitude": place.lon, "timezone": "auto"}
    try:
        async with _client() as client:
            forecast_req = client.get(FORECAST_URL, params={
                **common, "forecast_days": FORECAST_DAYS,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
                           "wind_speed_10m,wind_direction_10m",
                "hourly": "precipitation_probability,precipitation,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                         "precipitation_probability_max",
            })
            air_req = client.get(AIR_URL, params={**common, "current": "pm2_5"})
            forecast_resp, air_resp = await asyncio.gather(forecast_req, air_req, return_exceptions=True)
        if isinstance(forecast_resp, Exception):
            raise forecast_resp
        forecast_resp.raise_for_status()
        forecast = forecast_resp.json()
        air = None
        if isinstance(air_resp, httpx.Response) and air_resp.status_code == 200:
            air = air_resp.json()
        else:
            logger.bind(tag=TAG).warning(f"空气质量没查到: {air_resp!r}")
        report = format_report(place, forecast, air)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.bind(tag=TAG).error(f"查天气失败（{place.name}）: {e!r}")
        raise WeatherError("天气服务暂时连不上")
    if len(_reports) >= 256:
        now_ts = time.time()
        for k, v in list(_reports.items()):
            if now_ts - v[0] >= REPORT_TTL:
                _reports.pop(k, None)
        while len(_reports) >= 256:
            _reports.pop(next(iter(_reports)), None)
    _reports[key] = (time.time(), report)
    return report


HOME_WEATHER_BASE = "http://127.0.0.1:8095"
_HOME_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_home_json_sync(base_url: str, path: str, timeout_s: float = 1.6):
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Host": "127.0.0.1:8095", "Accept": "application/json"},
        method="GET",
    )
    try:
        with _HOME_NO_PROXY_OPENER.open(req, timeout=timeout_s) as r:
            if r.status == 200:
                data = json.loads(r.read(262144).decode("utf-8", errors="replace"))
                return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def _format_home_weather_report(data: dict) -> str:
    """将 ThinkSmart 主页同源天气数据格式化为语音播报文本，确保大屏显示与语音播报 100% 一致。"""
    city = str(data.get("city") or "本地").strip()
    now_w = data.get("now") or {}
    air = data.get("air") or {}
    daily = data.get("daily") if isinstance(data.get("daily"), list) else []
    hourly = data.get("hourly") if isinstance(data.get("hourly"), list) else []
    try:
        now_dt = datetime.fromtimestamp(float(data["updated"])) if data.get("updated") else datetime.now()
    except (TypeError, ValueError, OSError):
        now_dt = datetime.now()

    lines = [f"地点：{city}（主页同源实况）"]
    cond = str(now_w.get("text") or "未知").strip()
    temp = now_w.get("temp", "?")
    feels = now_w.get("feels") if now_w.get("feels") not in (None, "") else now_w.get("feels_like")
    hum = now_w.get("humidity")
    w_dir = str(now_w.get("wind_dir") or "").strip()
    w_sc = str(now_w.get("wind_scale") or "").strip()
    wind_s = f"{w_dir} {w_sc} 级".strip() if (w_dir and w_sc) else w_dir

    cur_parts = [f"现在（{now_dt:%H:%M}）：{cond}，{temp}°C"]
    if feels not in (None, ""):
        cur_parts.append(f"体感 {feels}°C")
    if hum not in (None, ""):
        cur_parts.append(f"湿度 {hum}%")
    if wind_s:
        cur_parts.append(wind_s)
    lines.append("，".join(cur_parts))

    if isinstance(air, dict) and (air.get("category") or air.get("aqi")):
        cat = str(air.get("category") or "").strip()
        aqi_v = str(air.get("aqi") or "").strip()
        pm25_v = ""
        for p in (air.get("pollutants") or []):
            if isinstance(p, dict) and str(p.get("name") or "").upper() in ("PM2.5", "PM2_5") and p.get("value") not in (None, "", "--"):
                pm25_v = str(p["value"])
                break
        extra = "，".join(x for x in (f"AQI {aqi_v}" if aqi_v else "", f"PM2.5 {pm25_v}" if pm25_v else "") if x)
        lines.append(f"空气质量：{cat or '实时'}" + (f"（{extra}）" if extra else ""))

    if daily and isinstance(daily[0], dict) and daily[0].get("sunrise") and daily[0].get("sunset"):
        lines.append(f"日出 {daily[0]['sunrise']}，日落 {daily[0]['sunset']}")

    if hourly:
        wet = []
        for h in hourly[:12]:
            if not isinstance(h, dict):
                continue
            try:
                pop = int(float(h.get("pop") or 0))
            except (TypeError, ValueError):
                pop = 0
            htxt = str(h.get("text") or "")
            if pop >= 45 or any(k in htxt for k in ("雨", "雪", "雹")):
                wet.append((str(h.get("time") or ""), pop, htxt))
        if not wet:
            lines.append("接下来 12 小时：基本不会下雨")
        else:
            what = "下雪" if any("雪" in x[2] for x in wet) else "下雨"
            top_pop = max(x[1] for x in wet)
            span = wet[0][0] if len(wet) == 1 else f"{wet[0][0]}到{wet[-1][0]}"
            lines.append(f"接下来 12 小时：{span}可能{what}" + (f"，降水概率最高 {top_pop}%" if top_pop > 0 else ""))

    today = date.today()
    for d in daily[:FORECAST_DAYS]:
        if not isinstance(d, dict) or not d.get("date"):
            continue
        try:
            dd = date.fromisoformat(str(d["date"]))
            lbl = _day_label(dd, today)
        except ValueError:
            lbl = str(d["date"])
        d_txt = str(d.get("text") or "未知")
        n_txt = str(d.get("text_night") or "")
        wx = f"{d_txt}转{n_txt}" if (n_txt and n_txt != d_txt) else d_txt
        lines.append(f"{lbl}：{wx}，{d.get('min', '?')}～{d.get('max', '?')}°C")
    return "\n".join(lines)


def _query_home_weather_sync(target_city: str, base_url: str) -> str | None:
    clean = str(target_city or "").strip()
    base_w = _fetch_home_json_sync(base_url, "/api/home/weather", timeout_s=1.4)
    if not (isinstance(base_w, dict) and base_w.get("ok")):
        return None
    home_city = str(base_w.get("city") or "").strip()
    allowed = [str(c).strip() for c in (base_w.get("cities") or []) if str(c).strip()]
    if home_city and home_city not in allowed:
        allowed.insert(0, home_city)

    def _norm(c: str) -> str:
        return c[:-1] if len(c) > 2 and c.endswith(("市", "区", "县")) else c

    matched = ""
    if not clean:
        matched = home_city or (allowed[0] if allowed else "")
    else:
        cn = _norm(clean)
        for cand in allowed:
            if clean == cand or cn == _norm(cand):
                matched = cand
                break
        if not matched:
            return None

    if matched:
        det = _fetch_home_json_sync(
            base_url,
            "/api/home/weather/detail?" + urllib.parse.urlencode({"city": matched}),
            timeout_s=1.8,
        )
        if isinstance(det, dict) and det.get("ok"):
            return _format_home_weather_report(det)
    if not clean or (home_city and _norm(clean) == _norm(home_city)):
        return _format_home_weather_report(base_w)
    return None


async def home_weather_report(conn, location: str = None) -> str | None:
    """优先向本机 ThinkSmart 主页(:8095)读取同源气象；未命中或单元测试隔离态返回 None 交由 Open-Meteo 兜底。"""
    force_base = os.environ.get("THINKSMART_HOME_WEATHER_URL", "").strip()
    if os.environ.get("PYTEST_CURRENT_TEST") and not force_base:
        return None
    base_url = force_base or HOME_WEATHER_BASE
    explicit_loc = str(location or "").strip()
    # 没传 location 时直接读主页(:8095)当前主城市实况，确保在大屏「设置 → 天气音乐」改完主城市后语音与主页 100% 同步；
    # 仅在主页未就绪且需回退 Open-Meteo 时，local_query 才会读取 plugins.get_weather.location。
    return await asyncio.to_thread(_query_home_weather_sync, explicit_loc, base_url)


# ---------- 本地 ----------

def _ip_city(conn) -> str:
    """宽带公网 IP 所在的城市（按设备地址缓存）。"""
    from core.utils.cache.manager import cache_manager, CacheType
    from core.utils.util import get_ip_info

    ip = getattr(conn, "client_ip", None)
    if not ip:
        return ""
    info = cache_manager.get(CacheType.IP_INFO, ip)
    if info is None:
        info = get_ip_info(ip, logger)
        if info:
            cache_manager.set(CacheType.IP_INFO, ip, info)
    return ((info or {}).get("city") or "").strip()


def local_query(conn) -> str:
    """本地查哪里：后台设置的所在地 > IP 定位的城市 > default_location。"""
    cfg = (conn.config.get("plugins") or {}).get("get_weather") or {}
    location = str(cfg.get("location") or "").strip()
    if location:
        return location
    return _ip_city(conn) or str(cfg.get("default_location") or "").strip() or DEFAULT_LOCATION


async def local_report(conn) -> str:
    home_rep = await home_weather_report(conn, None)
    if home_rep:
        return home_rep
    query = await asyncio.to_thread(local_query, conn)  # IP 定位是同步请求
    return await fetch_report(await find_place(query))


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(conn: "ConnectionHandler", location: str = None, lang: str = "zh_CN"):
    try:
        if location and location.strip():
            home_rep = await home_weather_report(conn, location)
            report = home_rep if home_rep else await fetch_report(await find_place(location))
        else:
            report = await local_report(conn)
    except WeatherError as e:
        return ActionResponse(Action.REQLLM, f"没查到天气：{e}。请简短告诉用户这次没查到，稍后再试。", None)
    return ActionResponse(Action.REQLLM, report, None)
