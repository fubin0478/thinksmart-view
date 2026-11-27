"""[ThinkSmart 本地改动，独立覆盖，官方文件不动] 联网搜索 = 官方 DeepSeek 自带搜索 +
秘塔网页搜索、高德地图（本地城市 POI / 驾车路线）、新浪/腾讯实时行情（股票、大盘、黄金原油，免 Key）。
密钥都在私有配置 plugins.web_search：
api_key（DeepSeek，官方含义不变）、metaso_key（秘塔）、amap_key（高德）；没填的那一路自动跳过。
后台的「测试」按钮仍只测 DeepSeek（settings_of / search / format_for_llm 保持官方接口）。

以下为官方说明：联网搜索：向 Anthropic 协议的接口发一次请求，带上服务端自带的搜索工具 web_search_20250305，
让那个模型去搜，拿回总结和搜索结果（标题、日期），再交给聊天模型回答。

和 DeepSeek Harness 的做法一致：不接专门的搜索服务，用支持自带搜索的 Anthropic 协议接口
（DeepSeek 的 https://api.deepseek.com/anthropic/v1、Anthropic 官方等）。聊天模型用哪家都可以，和这里无关。
"""

import asyncio
import math
import os
import stat
import re
import time
import weakref
from dataclasses import dataclass, field

import httpx
from config.logger import setup_logging
from core.providers.tts.dto.dto import ContentType
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
_ORIG_ASYNC_CLIENT = httpx.AsyncClient
_SHARED_SSL_CTX = None


def _get_ssl_ctx():
    global _SHARED_SSL_CTX
    if _SHARED_SSL_CTX is None:
        import ssl
        _SHARED_SSL_CTX = ssl.create_default_context()
    return _SHARED_SSL_CTX
logger = setup_logging()

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic/v1"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_USES = 5
MAX_USES_LIMIT = 10
# 语音场景用户在干等：连不上 5 秒就放弃，整次搜索（可能搜好几轮）最多 20 秒（实测一般 3 到 5 秒）。
# httpx 的超时只管每一段等待，整次的上限另用 asyncio.wait_for
TOTAL_SECONDS = 20.0
TIMEOUT = httpx.Timeout(TOTAL_SECONDS, connect=5.0)
WEEKDAYS = "一二三四五六日"
# 模型决定搜索时先播的一句，免得搜索那几秒没有声音
SEARCHING_REPLY = "正在为您查询。"  # 2026-11-05 用户要求统一说这句  # 句号不能省：合成按标点切句，没有就会和后面的回答连成一句
RESULT_LIMIT = 5  # 交给聊天模型的来源条数上限
SNIPPET_LIMIT = 150  # 每条来源的原文片段最多多少字
ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_DESCRIPTION = (
    "全网实时搜索、高德地图POI/路况、A股/港股/美股实时行情四合一查询工具。当用户查询本地商场新店、楼层餐厅美食（如朝阳大悦城韩餐）、"
    "周边停车场及出入口、两地驾车距离与路线、实时股票价格/涨跌幅/市值、大盘指数、天气预报、公司财报、最新政策、新闻事件时，必须调用此工具。"
)

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题，写完整（若涉及本地商场/餐厅/停车场/路线且未提城市，请带上默认城市如“北京”；查股票请直接写股票名称或代码）",
                }
            },
            "required": ["query"],
        },
    },
}

_SEARCH_SYSTEM = (
    "你是联网搜索助手。必须先用 web_search 工具搜索，再根据搜索结果用中文总结要点，"
    "300 字以内，只写和问题直接相关的内容，保留关键的数字、日期和来源名称；"
    "用纯文本，不要用 Markdown 标题、列表符号或加粗（结果会交给语音助手念出来）；"
    "不要编造搜索结果里没有的内容。"
    "今天是 {today}。"
)

# 失败分两类：config 要用户去后台处理（设备要说出原因），temporary 是临时问题（只说这次没搜到）
CONFIG, TEMPORARY = "config", "temporary"


class SearchError(Exception):
    """搜索失败。message 是详细原因（后台测试按钮直接显示）；spoken 是设备上能说给用户听的简短原因。"""

    def __init__(self, message: str, kind: str = TEMPORARY, spoken: str = ""):
        super().__init__(message)
        self.kind = kind
        self.spoken = spoken or message


def today_text() -> str:
    return time.strftime("%Y-%m-%d") + " 星期" + WEEKDAYS[time.localtime().tm_wday]


@dataclass
class SearchResult:
    summary: str = ""
    results: list = field(default_factory=list)  # [{"title", "url", "page_age", "snippet"}]
    queries: list = field(default_factory=list)  # 模型实际搜的词
    seconds: float = 0.0


def messages_url(base_url: str) -> str:
    """接口地址补成 .../v1/messages：/anthropic、/anthropic/v1、.../v1/messages 三种写法都认。"""
    url = str(base_url or "").strip().rstrip("/")
    if url.endswith("/messages"):
        return url
    if url.endswith("/v1"):
        return url + "/messages"
    return url + "/v1/messages"


def settings_of(config: dict) -> dict:
    """从 plugins.web_search 取出整理好的四项设置（缺的用默认值）。"""
    cfg = ((config or {}).get("plugins") or {}).get("web_search") or {}
    raw_max = cfg.get("max_uses")
    if raw_max is None or isinstance(raw_max, bool):
        max_uses = DEFAULT_MAX_USES
    else:
        try:
            max_uses = int(raw_max)
        except (TypeError, ValueError, OverflowError):
            max_uses = DEFAULT_MAX_USES
    # 配置里还有旧版的 provider（秘塔、Tavily、Serply）：api_key 是给那些服务的，不能发给新接口，当成没填
    legacy = bool(cfg.get("provider"))
    raw_key = "" if legacy else str(cfg.get("api_key") or "").strip()
    if raw_key and (
        "你" in raw_key
        or raw_key.upper().startswith(("YOUR_", "REPLACE_WITH_"))
        or raw_key.upper() in ("CHANGE_ME", "EXAMPLE", "__REDACTED__")
    ):
        raw_key = ""
    return {
        "base_url": str(cfg.get("base_url") or "").strip() or DEFAULT_BASE_URL,
        "api_key": raw_key,
        "model": str(cfg.get("model") or "").strip() or DEFAULT_MODEL,
        "max_uses": min(max(max_uses, 1), MAX_USES_LIMIT),
    }


def _api_error(response: httpx.Response, api_key: str = "") -> SearchError:
    """把接口返回的错误翻译成能看懂的原因并分类（有的接口会把收到的 Key 写进错误信息，换成 ***）。"""
    try:
        data = response.json()
    except ValueError:
        data = None
    message = ""
    if isinstance(data, dict):
        err = data.get("error")
        message = str(err.get("message") or "") if isinstance(err, dict) else str(err or data.get("message") or "")
    if api_key:
        message = message.replace(api_key, "***")
    status = response.status_code
    detail = f"{status}{'：' + message if message else ''}"
    low = message.lower()
    if status == 402 or "insufficient balance" in low or "余额" in message or "credit balance" in low:
        return SearchError(f"账户余额不足（{detail}）", CONFIG, "联网搜索的账户余额不足，需要去充值")
    if status in (401, 403):
        return SearchError(f"API Key 不对或没有权限（{detail}）", CONFIG, "联网搜索的 API Key 不对")
    if status == 404:
        return SearchError(f"接口地址不对，或者不是 Anthropic 协议的接口（{detail}）", CONFIG,
                           "联网搜索的接口地址不对")
    if status == 429:
        return SearchError(f"请求太频繁，稍后再试（{detail}）", TEMPORARY)
    if data is None:
        return SearchError(f"接口返回的不是 Anthropic 协议的格式（HTTP {status}）",
                           TEMPORARY if status >= 500 else CONFIG, "联网搜索的接口地址不对")
    if status in (400, 422) and ("web_search" in low or "tool" in low or "server" in low):
        return SearchError(f"这个接口或模型不支持自带搜索（{detail}）", CONFIG, "联网搜索设置的接口或模型不支持搜索")
    return SearchError(f"接口返回错误（{detail}）", TEMPORARY)


def _citation_snippets(blocks: list) -> dict:
    """url -> 原文片段：搜索结果本身不带正文，只有模型回答时引用的原文（citations[].cited_text）。
    同一来源被引用多次时拼起来，最多 SNIPPET_LIMIT 字。"""
    snippets = {}
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        for cite in block.get("citations") or []:
            if not isinstance(cite, dict):
                continue
            url, text = str(cite.get("url") or ""), " ".join(str(cite.get("cited_text") or "").split())
            if not url or not text:
                continue
            old = snippets.get(url, "")
            if text in old or len(old) >= SNIPPET_LIMIT:
                continue
            joined = f"{old} {text}".strip()
            snippets[url] = joined if len(joined) <= SNIPPET_LIMIT else joined[:SNIPPET_LIMIT] + "…"
    return snippets


def _parse(data) -> SearchResult:
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise SearchError("接口返回的不是 Anthropic 协议的格式", CONFIG, "联网搜索的接口地址不对")
    result = SearchResult()
    texts, searched, errors, seen = [], False, [], set()
    snippets = _citation_snippets(data["content"])
    for block in data["content"]:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind == "server_tool_use":
            searched = True
            query = (block.get("input") or {}).get("query")
            if query:
                result.queries.append(str(query))
        elif kind == "web_search_tool_result":
            searched = True
            content = block.get("content")
            if isinstance(content, dict):  # web_search_tool_result_error
                errors.append(str(content.get("error_code") or "unknown"))
                continue
            for item in content or []:
                if not isinstance(item, dict) or item.get("type") != "web_search_result":
                    continue
                url = str(item.get("url") or "")
                if url in seen:
                    continue
                seen.add(url)
                result.results.append({"title": str(item.get("title") or "无标题"), "url": url,
                                       "page_age": str(item.get("page_age") or ""),
                                       "snippet": snippets.get(url, "")})
    # 有原文片段的（总结里引用过的）排前面，交给聊天模型时只留前几条
    result.results.sort(key=lambda r: not r["snippet"])
    result.summary = "".join(texts).strip()
    if not searched:
        raise SearchError("接口没有执行搜索，这个接口或模型可能不支持自带搜索", CONFIG,
                          "联网搜索设置的接口或模型不支持搜索")
    if not result.results and errors:
        raise SearchError(f"搜索没有成功（{', '.join(errors)}）")
    return result


async def search(settings: dict, query: str) -> SearchResult:
    """搜一次。失败时抛 SearchError，原因写在 message 里。"""
    if not settings.get("api_key"):
        raise SearchError("还没有填写 API Key", CONFIG, "联网搜索还没开通，需要先填 API Key")
    url = messages_url(settings["base_url"])
    if not url.startswith(("http://", "https://")):
        raise SearchError("接口地址要以 http:// 或 https:// 开头", CONFIG, "联网搜索的接口地址不对")
    payload = {
        "model": settings["model"],
        "max_tokens": 1024,
        "thinking": {"type": "disabled"},
        "system": _SEARCH_SYSTEM.format(today=today_text()),
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": settings["max_uses"]}],
        "messages": [{"role": "user", "content": query}],
    }
    headers = {"x-api-key": settings["api_key"], "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}
    started = time.monotonic()
    try:
        if httpx.AsyncClient is _ORIG_ASYNC_CLIENT:
            client = _get_shared_http_client()
            response = await asyncio.wait_for(
                client.post(url, json=payload, headers=headers, timeout=TIMEOUT),
                TOTAL_SECONDS,
            )
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await asyncio.wait_for(client.post(url, json=payload, headers=headers), TOTAL_SECONDS)
    except (httpx.TimeoutException, asyncio.TimeoutError):
        raise SearchError(f"等了 {time.monotonic() - started:.0f} 秒没有结果，接口太慢或地址不通")
    except httpx.HTTPError as e:
        err_msg = str(e).replace(settings["api_key"], "***") if settings.get("api_key") else str(e)
        raise SearchError(f"连不上接口地址（{type(e).__name__}：{err_msg}）")
    if response.status_code != 200:
        raise _api_error(response, settings["api_key"])
    try:
        data = response.json()
    except ValueError:
        raise SearchError("接口返回的不是 Anthropic 协议的格式", CONFIG, "联网搜索的接口地址不对")
    result = _parse(data)
    result.seconds = round(time.monotonic() - started, 1)
    return result


def format_for_llm(query: str, result: SearchResult) -> str:
    """交给聊天模型：搜索模型的短总结，加前几条来源的标题、日期和原文片段（不给网址，语音用不上）。"""
    lines = [f"【联网搜索结果】搜索：{query}"]
    if result.summary:
        lines.append(f"总结：{result.summary}")
    # 有的接口（如 DeepSeek）回答时不带引用，就只有标题和日期
    has_snippet = any(item["snippet"] for item in result.results[:RESULT_LIMIT])
    if result.results:
        lines.append("来源和原文片段：" if has_snippet else "来源：")
        for i, item in enumerate(result.results[:RESULT_LIMIT], 1):
            age = f"（{item['page_age']}）" if item["page_age"] else ""
            snippet = f"：{item['snippet']}" if item["snippet"] else ""
            lines.append(f"{i}. {item['title']}{age}{snippet}")
    lines.append("回答要求：" + ("总结和原文片段对不上时，以原文片段为准；" if has_snippet else "")
                 + "结果里没有的内容不要编。只用一到两句话、大约 60 个字以内说最重要的结论，"
                 "细节等用户追问再说，不要念网址；内容多的话，最后问一句用户还想听哪方面。")
    return "\n".join(lines)


def _say_searching(conn, expected_sid=None, expected_gen=None) -> None:
    """先播一句"我上网查一下"，接在这一轮回答的前面（同一个 sentence_id）。用户已经打断时不说。"""
    from core.utils import turn_gen

    tts, sentence_id = getattr(conn, "tts", None), getattr(conn, "sentence_id", None)
    if tts is None or not sentence_id or getattr(conn, "client_abort", False):
        return
    if expected_sid is not None and sentence_id != expected_sid:
        return
    if expected_gen is not None and turn_gen.stale(conn, expected_gen):
        return
    if getattr(conn, "_query_prompted_sid", None) == sentence_id:
        return
    try:
        conn._query_prompted_sid = sentence_id
        tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=SEARCHING_REPLY, sentence_id=sentence_id)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"播搜索提示失败: {e}")

# ---- 本地高德地图 POI/路线 + 秘塔网页搜索 + 实时股票/大宗行情并行检索 ----
_HOME_CFG_CACHE = {"mtime": -1.0, "city": ""}


def _default_local_city() -> str:
    env_c = os.environ.get("THINKSMART_DEFAULT_CITY", "").strip()
    if env_c:
        return env_c
    p = "/var/lib/thinksmart-home/config.json"
    try:
        st = os.stat(p)
        if st.st_mtime != _HOME_CFG_CACHE["mtime"]:
            import json as _json
            with open(p, "r", encoding="utf-8") as fh:
                d = _json.load(fh)
            c = str((d or {}).get("city") or "").strip() if isinstance(d, dict) else ""
            _HOME_CFG_CACHE["mtime"] = st.st_mtime
            _HOME_CFG_CACHE["city"] = c
        if _HOME_CFG_CACHE["city"]:
            return _HOME_CFG_CACHE["city"]
    except Exception:
        pass
    return "北京"


LOCAL_CUE_RE = re.compile(
    r"(万达|万象城|大悦城|太古里|SKP|国贸|合生汇|蓝色港湾|吾悦|凯德|王府井|三里屯|中关村|附近|周边|我们这|这里|哪家|新开|停车|餐厅|饭店|烤肉|火锅|韩餐|料理|怎么走|多远|开车)"
)
COMMODITY_CUE_RE = re.compile(
    r"(原油|油价|布伦特|布油|WTI|wti|纽约原油|美油|国际油价|黄金|金价|国际金价|伦敦金|现货黄金|纽约金|COMEX|沪金|国内金价|白银|银价|伦敦银|现货白银|沪银)"
)
STOCK_CUE_RE = re.compile(
    r"(股票|股价|行情|大盘|指数|上证|深证|深成指|创业板|科创50|沪深300|恒生|恒指|港股|美股|纳指|纳斯达克|道琼斯|道指|标普|A股|涨跌|涨停|跌停|市值|市盈率|换手率|成交额|持仓|个股|板块|茅台|腾讯|阿里|英伟达|特斯拉|苹果|微软|谷歌|比亚迪|宁德时代|中芯国际|寒武纪|东方财富|中信证券|\b\d{6}\b)"
)
OTHER_CITY_MALL_RE = re.compile(
    r"(天津|沈阳|烟台|成都|上海|苏州|杭州|深圳|广州|武汉|西安|重庆|大连|长春|厦门|济南|石家庄)"
)
LANDMARK_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(朝阳大悦城|青年路大悦城)"), "朝阳大悦城"),
    (re.compile(r"(西单大悦城)"), "西单大悦城"),
    (re.compile(r"(三里屯太古里|太古里)"), "三里屯太古里"),
    (re.compile(r"(合生汇|朝阳合生汇)"), "朝阳合生汇"),
    (re.compile(r"(蓝色港湾)"), "SOLANA蓝色港湾"),
    (re.compile(r"(万达广场|万达)"), "万达广场"),
    (re.compile(r"(万象城)"), "万象城"),
    (re.compile(r"(凯德广场|凯德)"), "凯德广场"),
]
CATEGORY_SYNONYMS: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"(韩餐|韩国料理|韩式|烤肉)"), "韩国料理|韩餐|韩式烤肉|烤肉", ""),
    (re.compile(r"(停车|车位|地库)"), "停车场", "150900"),
    (re.compile(r"(火锅|涮肉)"), "火锅|铜锅涮肉", ""),
    (re.compile(r"(海鲜|自助)"), "海鲜|自助餐", ""),
    (re.compile(r"(充电桩|充电站)"), "充电站", "011100"),
]
ROUTE_RE = re.compile(
    r"从?(?P<orig>[^到去离]+?)(?:到|去|离)(?P<dest>[^多远怎么走开车多久需要]+?)(?:多远|怎么走|开车|多久|距离|路线)"
)
STOCK_CLEAN_STOPWORDS = re.compile(
    r"(今天|今日|现在|目前|最新|实时|查询|查一下|看看|帮我查|请问|股票|股价|行情|走势|涨跌幅|涨跌|多少钱|多少|怎么样|如何|分析|投研|研判|市值|市盈率|换手率|成交额|收盘|开盘|业务|逻辑|基本面|主营|的|了|吗|呢|呀|啊)"
)
DEEP_WEB_CUE_RE = re.compile(
    r"(为什么|为何|原因|怎么回事|新闻|消息|快讯|利好|利空|财报|业绩|研报|分析|投研|研判|预测|展望|政策|公告|事件|动态|逻辑|业务|主营|基本面|题材|概念|赛道|核心|催化|估值|订单|产能|看点|前景|龙头|份额|壁垒|竞争力|仔细|详细|深度|全面|怎么看|能不能买|建仓|调仓|评价|口碑|推荐|好吃|哪家|排行|攻略|楼层|新开|电话|营业时间)"
)
PURE_INDEX_CUE_RE = re.compile(r"(大盘|指数|上证|深证|深成指|创业板|恒生|恒指)")

_SHARED_HTTP_CLIENTS = weakref.WeakKeyDictionary()


def _get_shared_http_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _SHARED_HTTP_CLIENTS.get(loop)
    if client is None or client.is_closed:
        _ssl_kw = {"verify": _get_ssl_ctx()} if httpx.AsyncClient is _ORIG_ASYNC_CLIENT else {}
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(6.5, connect=2.5),
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
            follow_redirects=True,
            **_ssl_kw,
        )
        _SHARED_HTTP_CLIENTS[loop] = client
    return client



def _enrich_local_query(query: str) -> str:
    q = (query or "").strip()
    city = _default_local_city()
    if LOCAL_CUE_RE.search(q) and not STOCK_CUE_RE.search(q) and not COMMODITY_CUE_RE.search(q) and (city not in q):
        return f"{city} {q}"
    return q


def _extract_last_user_query(conn: Optional["ConnectionHandler"]) -> str:
    try:
        dlg = getattr(conn, "dialogue", None)
        pool = getattr(dlg, "dialogue", None) or []
        for msg in reversed(pool):
            if getattr(msg, "role", None) == "user" and getattr(msg, "content", None):
                return str(msg.content)
    except Exception:
        pass
    return ""


def _build_deep_stock_metaso_query(query: str) -> str:
    q = (query or "").strip()
    if DEEP_WEB_CUE_RE.search(q):
        return q
    clean = re.sub(r"(股票|实时行情|行情|最新价|股价)", "", q).strip()
    return f"{clean} 核心业务 主营逻辑 最新动态 研报"


def _get_amap_key(web_search_config: dict) -> str:
    from core.admin import config_store
    raw_cfg = (web_search_config or {}).get("amap_key")
    raw_env = os.environ.get("AMAP_API_KEY")
    cand = str(raw_cfg if raw_cfg is not None and not isinstance(raw_cfg, bool) else (raw_env or "")).strip()
    key = "" if config_store.is_placeholder(cand) else cand
    from config.config_loader import get_project_dir as _get_hub_project_dir
    for key_path in (
        os.path.join(_get_hub_project_dir(), "data", ".amap_key"),
        "/opt/xiaozhi-esp32-server/data/.amap_key",
    ):
        if key:
            break
        try:
            fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                st = os.fstat(fd)
                if stat.S_ISREG(st.st_mode) and st.st_size <= 4096:
                    fk = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
                    if fk and not config_store.is_placeholder(fk):
                        key = fk
            finally:
                os.close(fd)
        except OSError:
            pass
    return key


async def _search_realtime_commodities(query: str) -> str:
    """通过新浪财经高速接口毫秒级获取黄金（伦敦金/纽约金/沪金）、原油（布伦特/WTI）、白银实时行情"""
    try:
        if not COMMODITY_CUE_RE.search(query):
            return ""
        want_oil = bool(re.search(r"(原油|油价|布伦特|布油|WTI|wti|纽约原油|美油|国际油价)", query))
        want_gold = bool(re.search(r"(黄金|金价|国际金价|伦敦金|现货黄金|纽约金|COMEX|沪金|国内金价)", query))
        want_silver = bool(re.search(r"(白银|银价|伦敦银|现货白银|沪银)", query))
        symbols = []
        if want_oil:
            symbols.extend(["hf_OIL", "hf_CL"])
        if want_gold:
            symbols.extend(["hf_XAU", "hf_GC", "nf_AU0"])
        if want_silver:
            symbols.extend(["hf_XAG", "nf_AG0"])
        if not symbols:
            symbols = ["hf_OIL", "hf_CL", "hf_XAU", "nf_AU0"]

        client = _get_shared_http_client()
        resp = await client.get(
            f"https://hq.sinajs.cn/list={','.join(symbols)}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=httpx.Timeout(3.5, connect=1.5),
        )
        if resp.status_code != 200:
            return ""
        raw_text = resp.content.decode(resp.charset_encoding or "gbk", errors="ignore")
        lines = ["【实时大宗商品/贵金属行情（新浪财经直连）】"]
        item_lines = []
        for line in raw_text.strip().splitlines():
            m = re.search(r'hq_str_([A-Za-z0-9_]+)="([^"]+)"', line)
            if not m:
                continue
            sym, body = m.group(1), m.group(2)
            fields = body.split(",")
            try:
                if sym.startswith("hf_") and len(fields) >= 14 and fields[0]:
                    price = float(fields[0])
                    prev = float(fields[7]) if fields[7] else price
                    if not (math.isfinite(price) and math.isfinite(prev)):
                        continue
                    high = fields[4]
                    low = fields[5]
                    ts = fields[6]
                    open_p = fields[8]
                    dt = fields[12]
                    name = fields[13]
                    chg = price - prev
                    chg_pct = (chg / prev * 100.0) if prev else 0.0
                    unit = "美元/桶" if sym in ("hf_OIL", "hf_CL") else "美元/盎司"
                    item_lines.append(
                        f"- {name}({sym})：最新价:{price:.2f}{unit} | 涨跌幅:{chg_pct:+.2f}%({chg:+.2f}) | 今开:{open_p} | 最高:{high} | 最低:{low} | 昨收:{prev:.2f} | 时间:{dt} {ts}"
                    )
                elif sym.startswith("nf_") and len(fields) >= 18 and fields[8]:
                    price = float(fields[8])
                    prev = float(fields[10]) if fields[10] else price
                    if not (math.isfinite(price) and math.isfinite(prev)):
                        continue
                    name = fields[0]
                    open_p = fields[2]
                    high = fields[3]
                    low = fields[4]
                    dt = fields[17]
                    chg = price - prev
                    chg_pct = (chg / prev * 100.0) if prev else 0.0
                    unit = "元/克" if sym == "nf_AU0" else "元/千克"
                    label = "沪金主连(国内金价)" if sym == "nf_AU0" else f"{name}({sym})"
                    item_lines.append(
                        f"- {label}：最新价:{price:.2f}{unit} | 涨跌幅:{chg_pct:+.2f}%({chg:+.2f}) | 今开:{open_p} | 最高:{high} | 最低:{low} | 昨结算:{prev:.2f} | 日期:{dt}"
                    )
            except (TypeError, ValueError):
                continue
        if not item_lines:
            return ""
        lines.extend(item_lines)
        return "\n".join(lines)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"实时大宗商品行情查询异常: {e}")
        return ""


async def _search_realtime_stock(query: str) -> str:
    """通过腾讯财经实时接口毫秒级获取大盘指数及A股/港股/美股实时盘面数据"""
    try:
        norm_query = (query or "").strip()
        client = _get_shared_http_client()
        req_timeout = httpx.Timeout(4.0, connect=1.8)
        # 1. Extract candidate stock keywords from query
        candidate_kw = STOCK_CLEAN_STOPWORDS.sub(" ", norm_query).strip()
        tokens = [t.strip() for t in re.split(r"[\s,，、/和与及]+", candidate_kw) if len(t.strip()) >= 2]
        if "紫光" in norm_query and "紫光股份" not in tokens and "紫光国微" not in tokens:
            tokens.extend(["紫光股份", "紫光国微"])

        quote_codes = ["sh000001", "sz399001", "sz399006", "hkHSI"]
        for tok in tokens[:5]:
            if tok in ("大盘", "指数", "上证", "上证指数", "深证", "创业板", "恒生", "A股", "港股", "美股"):
                continue
            try:
                sr = await client.get(
                    "https://smartbox.gtimg.cn/s3/",
                    params={"v": "2", "q": tok, "t": "all"},
                    timeout=req_timeout,
                )
                if sr.status_code != 200:
                    continue
                raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), sr.text)
                matches = re.findall(r"(sh|sz|bj|hk|us)~([A-Za-z0-9._]+)~([^~]+)~", raw)
                for mkt, code, _ in matches[:2]:
                    clean_code = code.split(".")[0].upper() if mkt == "us" else code
                    qcode = f"{mkt}{clean_code}"
                    if qcode not in quote_codes:
                        quote_codes.append(qcode)
            except Exception:
                continue

        qr = await client.get(
            f"http://qt.gtimg.cn/q={','.join(quote_codes)}",
            timeout=req_timeout,
        )
        if qr.status_code != 200:
            return ""
        qr_text = qr.content.decode(qr.charset_encoding or "gbk", errors="ignore")
        lines = ["【实时证券行情数据（腾讯财经直连）】"]
        index_lines = []
        stock_lines = []
        for entry in qr_text.strip().split(";"):
            if "~" not in entry:
                continue
            fields = entry.split("~")
            if len(fields) < 46 or not fields[1]:
                continue
            name = fields[1]
            code = fields[2]
            price = fields[3]
            prev_close = fields[4]
            open_p = fields[5]
            chg = fields[31]
            chg_pct = fields[32]
            high = fields[33]
            low = fields[34]
            turnover_rate = fields[38]
            pe = fields[39]
            mkt_cap = fields[45]
            ts = fields[30]
            if code in ("000001", "399001", "399006", "HSI") and name in ("上证指数", "深证成指", "创业板指", "恒生指数"):
                index_lines.append(f"{name}: {price} ({chg_pct}%, 涨跌 {chg})")
            else:
                extra = [
                    f"最新价:{price}",
                    f"涨跌幅:{chg_pct}%({chg})",
                    f"今开:{open_p}",
                    f"最高:{high}",
                    f"最低:{low}",
                    f"昨收:{prev_close}",
                ]
                if turnover_rate and turnover_rate != "0.00":
                    extra.append(f"换手率:{turnover_rate}%")
                if pe:
                    extra.append(f"市盈率PE:{pe}")
                if mkt_cap:
                    extra.append(f"总市值:{mkt_cap}亿")
                if ts:
                    extra.append(f"行情时间:{ts}")
                stock_lines.append(f"- {name}({code})：{' | '.join(extra)}")

        if stock_lines:
            lines.extend(stock_lines)
        if index_lines:
            lines.append("大盘参考：" + " ； ".join(index_lines))
        return "\n".join(lines) if (stock_lines or index_lines) else ""
    except Exception as e:
        logger.bind(tag=TAG).warning(f"实时股票行情查询异常: {e}")
        return ""


def _format_amap_pois(pois: list, limit: int = 10, title: str = "【高德地图实时POI检索】") -> str:
    if not isinstance(pois, list) or not pois:
        return ""
    lines = [title]
    seen = set()
    count = 0
    for p in pois:
        if not isinstance(p, dict):
            continue
        raw_name = p.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        count += 1
        addr = p.get("address", "") if isinstance(p.get("address"), str) else ""
        pname = p.get("adname", "") if isinstance(p.get("adname"), str) else ""
        type_str = p.get("type", "") if isinstance(p.get("type"), str) else ""
        raw_biz = p.get("biz_ext")
        biz = raw_biz if isinstance(raw_biz, dict) else {}
        rating = biz.get("rating") if isinstance(biz.get("rating"), str) else ""
        cost = biz.get("cost") if isinstance(biz.get("cost"), str) else ""
        parking = p.get("parking_type", "") if isinstance(p.get("parking_type"), str) else ""
        tel = p.get("tel", "") if isinstance(p.get("tel"), str) else ""
        dist = p.get("distance", "") if isinstance(p.get("distance"), str) else ""
        extra = []
        if pname or addr:
            extra.append(f"地址:{pname}{addr}")
        if dist:
            extra.append(f"距离:{dist}米")
        if type_str:
            extra.append(f"分类:{type_str}")
        if rating:
            extra.append(f"评分:{rating}")
        if cost:
            extra.append(f"人均:{cost}元")
        if parking:
            extra.append(f"停车类型:{parking}")
        if tel:
            extra.append(f"电话:{tel}")
        detail_str = " | ".join(extra)
        lines.append(f"{count}. {name}（{detail_str}）")
        if count >= limit:
            break
    return "\n".join(lines) if count > 0 else ""


async def _amap_geocode_poi(
    client: httpx.AsyncClient, amap_key: str, place_name: str, city: str = ""
) -> Optional[Tuple[str, str]]:
    city = city or _default_local_city()
    resp = await client.get(
        "https://restapi.amap.com/v3/place/text",
        params={"key": amap_key, "keywords": place_name, "city": city, "citylimit": "true", "offset": 1},
        timeout=httpx.Timeout(6.5, connect=2.5),
    )
    resp.raise_for_status()
    data = resp.json()
    pois = data.get("pois") if isinstance(data, dict) else None
    if isinstance(pois, list) and pois and isinstance(pois[0], dict):
        loc = pois[0].get("location")
        raw_name = pois[0].get("name")
        if isinstance(loc, str) and loc:
            return (raw_name if isinstance(raw_name, str) and raw_name else place_name), loc
    return None


async def _search_amap_poi(amap_key: str, query: str, max_results: int = 10) -> str:
    if not amap_key:
        return ""
    local_city = _default_local_city()
    clean_kw = re.sub(rf"^{re.escape(local_city)}市?\s*", "", query).strip() or query
    sections = []
    try:
        client = _get_shared_http_client()
        req_timeout = httpx.Timeout(6.5, connect=2.5)
        def _amap_num(val, default=0.0):
            try:
                return float(val) if isinstance(val, (int, float, str)) and not isinstance(val, bool) and str(val).strip() else default
            except (TypeError, ValueError):
                return default

        # 1. Check if it is a route/distance query
        route_m = ROUTE_RE.search(clean_kw)
        if route_m:
            try:
                orig_name = route_m.group("orig").strip()
                dest_name = route_m.group("dest").strip()
                if orig_name and dest_name:
                    orig_geo, dest_geo = await asyncio.gather(
                        _amap_geocode_poi(client, amap_key, orig_name),
                        _amap_geocode_poi(client, amap_key, dest_name),
                    )
                else:
                    orig_geo, dest_geo = None, None
                if orig_geo and dest_geo:
                    r_resp = await client.get(
                        "https://restapi.amap.com/v3/direction/driving",
                        params={
                            "key": amap_key,
                            "origin": orig_geo[1],
                            "destination": dest_geo[1],
                            "extensions": "base",
                        },
                        timeout=req_timeout,
                    )
                    r_data = r_resp.json() if r_resp.status_code == 200 else None
                    route_obj = r_data.get("route") if isinstance(r_data, dict) else None
                    paths = route_obj.get("paths") if isinstance(route_obj, dict) else None
                    if isinstance(paths, list) and paths and isinstance(paths[0], dict):
                        p0 = paths[0]
                        dist_km = round(_amap_num(p0.get("distance"), 0.0) / 1000.0, 1)
                        dur_min = max(1, round(_amap_num(p0.get("duration"), 0.0) / 60.0))
                        raw_lights = p0.get("traffic_lights")
                        lights = str(raw_lights).strip() if isinstance(raw_lights, (int, str)) and not isinstance(raw_lights, bool) and str(raw_lights).strip() else "0"
                        roads = []
                        steps = p0.get("steps")
                        for s in steps if isinstance(steps, list) else []:
                            if not isinstance(s, dict):
                                continue
                            rd = s.get("road")
                            if isinstance(rd, str) and rd and (not roads or roads[-1] != rd):
                                roads.append(rd)
                        road_str = " → ".join(roads[:6])
                        sections.append(
                            f"【高德地图实时驾车路线】\n起点：{orig_geo[0]} → 终点：{dest_geo[0]}\n"
                            f"距离：{dist_km}公里 | 预计耗时：约{dur_min}分钟 | 红绿灯：{lights}个 | 主要途经：{road_str}"
                        )
            except Exception as e:
                logger.bind(tag=TAG).warning(f"高德算路子查询异常: {type(e).__name__}")

        # 2. Detect landmark + around search (e.g. 朝阳大悦城 + 韩餐/停车场)
        landmark_target = None
        for pat, std_name in LANDMARK_PATTERNS:
            if pat.search(clean_kw):
                landmark_target = std_name
                break

        around_keywords = ""
        around_types = ""
        for cat_pat, syn_kw, poi_type in CATEGORY_SYNONYMS:
            if cat_pat.search(clean_kw):
                around_keywords = syn_kw
                around_types = poi_type
                break

        if landmark_target and (around_keywords or around_types):
            try:
                lm_geo = await _amap_geocode_poi(client, amap_key, landmark_target, city=local_city)
                if lm_geo:
                    around_params = {
                        "key": amap_key,
                        "location": lm_geo[1],
                        "radius": 600,
                        "offset": 15,
                        "page": 1,
                        "extensions": "all",
                    }
                    if around_types:
                        around_params["types"] = around_types
                    else:
                        around_params["keywords"] = around_keywords
                    ar_resp = await client.get(
                        "https://restapi.amap.com/v3/place/around",
                        params=around_params,
                        timeout=req_timeout,
                    )
                    ar_data = ar_resp.json() if ar_resp.status_code == 200 else None
                    ar_pois = ar_data.get("pois") if isinstance(ar_data, dict) else None
                    formatted = _format_amap_pois(
                        ar_pois if isinstance(ar_pois, list) else [],
                        limit=12,
                        title=f"【高德地图实时周边POI：{lm_geo[0]} 600米内】",
                    )
                    if formatted:
                        sections.append(formatted)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"高德周边POI子查询异常: {type(e).__name__}")

        # 3. Also run direct text search in default local city
        if not sections:
            text_kw = clean_kw
            if landmark_target and around_keywords and "|" not in clean_kw:
                first_syn = around_keywords.split("|")[0]
                text_kw = f"{landmark_target} {first_syn}"
            t_resp = await client.get(
                "https://restapi.amap.com/v3/place/text",
                params={
                    "key": amap_key,
                    "keywords": text_kw,
                    "city": local_city,
                    "citylimit": "true",
                    "offset": max_results,
                    "page": 1,
                    "extensions": "all",
                },
                timeout=req_timeout,
            )
            t_data = t_resp.json() if t_resp.status_code == 200 else None
            t_pois = t_data.get("pois") if isinstance(t_data, dict) else None
            if isinstance(t_pois, list) and t_pois:
                formatted_t = _format_amap_pois(t_pois, limit=max_results, title="【高德地图实时POI检索】")
                if formatted_t:
                    sections.append(formatted_t)

        return "\n\n".join(sections)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"高德POI检索异常: {type(e).__name__}")  # 异常文字带请求地址，里面有高德 Key
        return ""


async def _search_metaso(api_key: str, query: str, max_results: int) -> str:
    """调用秘塔搜索API（修复snippet丢失Bug并过滤异地同名商圈干扰）"""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "size": max(max_results + 3, 8),
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    client = _get_shared_http_client()
    response = await client.post(
        url,
        json=payload,
        headers=headers,
        timeout=httpx.Timeout(5.0, connect=2.0),
    )
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError("metaso response exceeds 2 MiB")
    data = response.json()
    if not isinstance(data, dict):
        return ""
    webpages = data.get("webpages", [])
    if not isinstance(webpages, list) or not webpages:
        return ""

    local_city = _default_local_city()
    is_local_city_query = local_city in query
    lines = ["【秘塔网页搜索结果】"]
    kept = 0
    for item in webpages:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "无标题")
        snippet = str(item.get("summary") or item.get("snippet") or "").strip()
        date = str(item.get("date") or "")
        combined = f"{title} {snippet}"
        if (
            is_local_city_query
            and OTHER_CITY_MALL_RE.search(combined)
            and (local_city not in combined)
        ):
            continue
        kept += 1
        lines.append(f"{kept}. 标题：{title}")
        if date:
            lines.append(f"   日期：{date}")
        if snippet:
            clean_snip = re.sub(r"\s+", " ", snippet)[:260]
            lines.append(f"   摘要：{clean_snip}")
        if kept >= max_results:
            break

    return "\n".join(lines) if kept > 0 else ""


# ---- 合并入口 ----
DEEPSEEK_CAP_WITH_METASO = 6.0  # 秘塔有结果时，DeepSeek 搜索最多再等这么久（它一般 3～4 秒）
ANSWER_RULE = ("回答要求：以上结果里没有的内容不要编。行情、路线这类数字直接照念；只用一到两句话、大约 60 个字以内"
               "说最重要的结论，细节等用户追问再说，不要念网址；内容多的话，最后问一句用户还想听哪方面。")


async def _search_deepseek(conn, query: str, timeout_sec: float) -> str:
    settings = settings_of(conn.config)
    if not settings["api_key"]:
        return ""
    try:
        result = await asyncio.wait_for(search(settings, query), timeout_sec)
    except asyncio.TimeoutError:
        logger.bind(tag=TAG).warning(f"DeepSeek 搜索超过 {timeout_sec:.0f} 秒，先用其他来源")
        return ""
    except SearchError as e:
        logger.bind(tag=TAG).warning(f"DeepSeek 搜索失败（{e.kind}）: {e}")
        return ""
    text = format_for_llm(query, result)
    return text.rsplit("\n回答要求：", 1)[0].replace("【联网搜索结果】", "【DeepSeek 联网搜索】", 1)


# 用户要求（2026-11-05）：快的查询不用提示；整轮等待预计超过 6 秒才先说「正在为您查询」
# （工具前后的大模型各约 1.5 秒，所以搜索本身超过 3 秒还没好才播）。
QUERYING_AFTER = 0.8  # 2026-11-05 用户：联网搜索都说；股价金价快速通道 0.5 秒内就回来，不说


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    q = "" if isinstance(query, bool) else str(query or "").strip()
    if not q:
        return ActionResponse(Action.REQLLM, "请提供搜索关键词。", None)
    from core.utils import turn_gen

    orig_sid = getattr(conn, "sentence_id", None)
    orig_gen = turn_gen.current(conn)

    async def later():
        await asyncio.sleep(QUERYING_AFTER)
        _say_searching(conn, orig_sid, orig_gen)
    timer = asyncio.create_task(later())
    try:
        return await _web_search_impl(conn, q)
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)


async def _web_search_impl(conn: "ConnectionHandler", query: str = None):
    query = "" if isinstance(query, bool) else str(query or "").strip()
    if not query:
        return ActionResponse(Action.REQLLM, "请提供搜索关键词。", None)
    started = time.monotonic()
    effective_query = _enrich_local_query(query)
    cfg = ((conn.config or {}).get("plugins") or {}).get("web_search") or {}
    from core.admin import config_store
    raw_mk = str(cfg.get("metaso_key") or "").strip()
    metaso_key = "" if config_store.is_placeholder(raw_mk) else raw_mk
    if metaso_key:
        try:
            metaso_key.encode("latin-1")
        except UnicodeEncodeError:
            metaso_key = ""
    amap_key = _get_amap_key(cfg)
    has_deepseek = bool(settings_of(conn.config)["api_key"])
    raw_max = cfg.get("max_results")
    try:
        max_results = 5 if raw_max is None or isinstance(raw_max, bool) else int(raw_max)
    except (TypeError, ValueError):
        max_results = 5
    max_results = min(max(max_results, 1), 20)
    logger.bind(tag=TAG).info(
        f"web_search 被调用 | query={query} | effective_query={effective_query} | 秘塔={'有' if metaso_key else '无'} "
        f"| 高德={'有' if amap_key else '无'} | DeepSeek={'有' if has_deepseek else '无'}")

    is_commodity = bool(COMMODITY_CUE_RE.search(query))
    is_stock = bool(STOCK_CUE_RE.search(query))
    last_user_q = _extract_last_user_query(conn)
    needs_deep_web = bool(DEEP_WEB_CUE_RE.search(query)
                          or ((is_stock or is_commodity) and last_user_q and DEEP_WEB_CUE_RE.search(last_user_q)))

    def done(text: str, how: str):
        logger.bind(tag=TAG).info(f"web_search 完成（{how}）| {time.monotonic() - started:.1f} 秒 | {len(text)} 字")
        return ActionResponse(Action.REQLLM, text + "\n" + ANSWER_RULE, None)

    # 快返 1：纯查股价/大盘/黄金原油（免 Key，毫秒级），不等网页搜索，也不播「我上网查一下」
    if (is_stock or is_commodity) and not needs_deep_web:
        fast = await asyncio.gather(*([_search_realtime_commodities(query)] if is_commodity else []),
                                    *([_search_realtime_stock(query)] if is_stock else []), return_exceptions=True)
        joined = "\n\n".join(r.strip() for r in fast if isinstance(r, str) and r.strip())
        if joined and ("最新价:" in joined or (PURE_INDEX_CUE_RE.search(query) and "大盘参考：" in joined)):
            return done(joined, "实时行情快返")

    # 快返 2：同城两地驾车距离/路线，高德算路
    if amap_key and not is_stock and not is_commodity and not needs_deep_web and ROUTE_RE.search(effective_query):
        route = await _search_amap_poi(amap_key, effective_query, max_results=6)
        if route and "【高德地图实时驾车路线】" in route:
            return done(route, "高德路线快返")

    if not (metaso_key or has_deepseek or amap_key or is_stock or is_commodity):
        return ActionResponse(Action.REQLLM, "联网搜索还没配置好（没有可用的搜索服务）。请用一句话告诉用户这次没法搜，不要编造答案。", None)

    tasks = []
    if is_commodity:
        tasks.append(_search_realtime_commodities(query))
    if is_stock:
        tasks.append(_search_realtime_stock(query))
    if amap_key and LOCAL_CUE_RE.search(effective_query) and not is_stock and not is_commodity:
        tasks.append(_search_amap_poi(amap_key, effective_query, max_results=10))

    async def metaso():
        try:
            q = _build_deep_stock_metaso_query(effective_query) if (is_stock and needs_deep_web) else effective_query
            return await _search_metaso(metaso_key, q, max_results)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"秘塔搜索失败: {type(e).__name__}")
            return ""
    if metaso_key:
        tasks.append(metaso())
    if has_deepseek:
        tasks.append(_search_deepseek(conn, effective_query, DEEPSEEK_CAP_WITH_METASO if metaso_key else TOTAL_SECONDS))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    sections = []
    for r in results:
        if isinstance(r, str) and r.strip():
            sections.append(r.strip())
        elif isinstance(r, Exception):
            logger.bind(tag=TAG).warning(f"子搜索任务异常: {type(r).__name__}")
    if not sections:
        return ActionResponse(Action.REQLLM, "联网搜索这次没搜到结果。请用一句话告诉用户这次没搜到，稍后再试；不要编造答案。", None)
    return done(f"【联网搜索】搜索：{query}\n\n" + "\n\n".join(sections), f"{len(sections)} 个来源")
