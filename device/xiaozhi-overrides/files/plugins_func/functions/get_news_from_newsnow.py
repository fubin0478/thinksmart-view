"""[ThinkSmart 本地改动，独立覆盖，官方文件不动] 新闻一次列出好几条（默认 3 条，带编号），
用户说第几条就讲那条的详情（pick），说下一批就接着往下列（more），同一来源不重复；详情正文截到
detail_chars（默认 2000 字）。2026-11-05 起优化：一次给模型一池候选（pool_size 默认 18），由模型按财经内参挑 3 条主线播报。
其余（新闻源映射、抓取、MarkItDown 清洗）与官方一致。"""
import asyncio
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
import urllib.parse
import weakref
import httpx
from io import BytesIO
from markitdown import MarkItDown, StreamInfo
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


TAG = __name__
logger = setup_logging()

CHANNEL_MAP = {
    "V2EX": "v2ex-share",
    "知乎": "zhihu",
    "微博": "weibo",
    "联合早报": "zaobao",
    "酷安": "coolapk",
    "MKTNews": "mktnews-flash",
    "华尔街见闻": "wallstreetcn-quick",
    "36氪": "36kr-quick",
    "抖音": "douyin",
    "虎扑": "hupu",
    "百度贴吧": "tieba",
    "今日头条": "toutiao",
    "IT之家": "ithome",
    "澎湃新闻": "thepaper",
    "卫星通讯社": "sputniknewscn",
    "参考消息": "cankaoxiaoxi",
    "远景论坛": "pcbeta-windows11",
    "财联社": "cls-depth",
    "雪球": "xueqiu-hotstock",
    "格隆汇": "gelonghui",
    "法布财经": "fastbull-express",
    "Solidot": "solidot",
    "Hacker News": "hackernews",
    "Product Hunt": "producthunt",
    "Github": "github-trending-today",
    "哔哩哔哩": "bilibili-hot-search",
    "快手": "kuaishou",
    "靠谱新闻": "kaopu",
    "金十数据": "jin10",
    "百度热搜": "baidu",
    "牛客": "nowcoder",
    "少数派": "sspai",
    "稀土掘金": "juejin",
    "凤凰网": "ifeng",
    "虫部落": "chongbuluo-latest",
}

# 默认新闻来源字典，当配置中没有指定时使用
DEFAULT_NEWS_SOURCES = "澎湃新闻;百度热搜;财联社"

def _get_newsnow_config(conn):
    # 从连接配置获取
    plugins = (conn.config or {}).get("plugins") or {}
    newsnow = plugins.get("get_news_from_newsnow") or {}
    sources = newsnow.get("news_sources", "")
    if isinstance(sources, str) and sources.strip():
        return sources

    return ""

def get_news_sources_from_config(conn):
    """从配置中获取新闻源字符串"""
    try:
        result = _get_newsnow_config(conn)
        if result:
            logger.bind(tag=TAG).debug(f"使用配置的新闻源: {result}")
            return result

        logger.bind(tag=TAG).debug("未找到新闻源配置，使用默认配置")
        return DEFAULT_NEWS_SOURCES

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻源配置失败: {e}，使用默认配置")
        return DEFAULT_NEWS_SOURCES


# 从默认配置获取可用的新闻源名称（运行时由get_news_sources_from_config动态获取）
example_sources_str = DEFAULT_NEWS_SOURCES.replace(";","、")

GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_news_from_newsnow",
        "description": "查看或收听新闻（如'来点新闻''今天有什么财经新闻'）。返回一批带候选号（#1、#2…）的最新快讯，"
                       "由你挑出最重要的3条主线来播报。用户要详细听某一条时用 detail=true、pick=那条主线里最主要的那则新闻的候选号；"
                       "用户说'下一批''再来几条'时用 more=true。",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": f"新闻源的标准中文名称，例如{example_sources_str}等。可选；more=true 时不填就沿用上一批的来源",
                },
                "detail": {
                    "type": "boolean",
                    "description": "是否获取某一条的详细内容，默认 false。为 true 时用 pick 指定上一批里的第几条",
                },
                "pick": {
                    "type": "integer",
                    "description": "detail=true 时要讲的那则新闻的候选号（#后面的数字）",
                },
                "more": {
                    "type": "boolean",
                    "description": "是否接着上一批往下列下一批（不重复），默认 false",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}


# ---- 财联社用 7x24「电报」，财经多源合并，聚合站不通时直连华尔街见闻 / 新浪财经快讯 ----
CHANNEL_MAP["财联社"] = "cls-telegraph"
CHANNEL_MAP["金十数据"] = "jin10"
FINANCE_GROUP = [("财联社", "cls-telegraph"), ("华尔街见闻", "wallstreetcn-quick"), ("格隆汇", "gelonghui"), ("金十数据", "jin10")]
_HTTP = weakref.WeakKeyDictionary()
# 用户不要国内社会类新闻（案件、事故、民生八卦）：标题里有这些词的直接跳过（2026-11-05）
SOCIAL_RE = re.compile(r"(宣判|一审|二审|庭审|藏尸|命案|凶手|嫌疑人|刑拘|批捕|警方通报|车祸|溺亡|坠楼|家暴|猥亵|诈骗案|失踪|"
                       r"网红|明星|八卦|热搜|塌方|火灾|事故|遇难|伤亡|医院|学生|校园|幼儿园|小区|物业|城管|村民|彩礼)")


_BLOCKED_HOST_SUFFIXES = (
    ".local", ".localhost", ".internal", ".lan", ".home.arpa",
    ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me",
)
_ORIG_ASYNC_CLIENT = httpx.AsyncClient
MAX_API_JSON_BYTES = 1024 * 1024


def _is_global_ip(ip_obj) -> bool:
    if getattr(ip_obj, "ipv4_mapped", None) is not None:
        ip_obj = ip_obj.ipv4_mapped
    return not (
        not ip_obj.is_global
        or ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or ip_obj.is_reserved
    )


def _has_only_global_ips(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h or h in ("localhost", "ip6-localhost", "ip6-loopback") or h.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        return _is_global_ip(ipaddress.ip_address(h))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = (info[4][0] or "").split("%", 1)[0]
        try:
            if not _is_global_ip(ipaddress.ip_address(addr)):
                return False
        except ValueError:
            return False
    return True


def _client():
    loop = asyncio.get_running_loop()
    c = _HTTP.get(loop)
    if c is None or c.is_closed:
        c = _HTTP[loop] = httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0), follow_redirects=False,
                                            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8))
    return c


async def _get_json_bounded(url: str, timeout_sec: float, max_bytes: int = MAX_API_JSON_BYTES):
    cur_url = url
    for _ in range(4):
        if not _safe_public_http_url(cur_url):
            return 0, None
        cur_parts = urllib.parse.urlsplit(cur_url)
        if httpx.AsyncClient is _ORIG_ASYNC_CLIENT:
            if not await asyncio.to_thread(_has_only_global_ips, cur_parts.hostname or ""):
                return 0, None
        async with _client().stream(
            "GET", cur_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=httpx.Timeout(timeout_sec, connect=2.0)
        ) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location") or ""
                if not loc:
                    return r.status_code, None
                nxt_url = str(r.url.join(loc))
                if cur_parts.scheme == "https" and not nxt_url.startswith("https://"):
                    return 0, None
                cur_url = nxt_url
                continue
            if r.status_code != 200:
                return r.status_code, None
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                if not chunk:
                    continue
                if len(buf) + len(chunk) > max_bytes:
                    raise ValueError("API JSON exceeds maximum size")
                buf.extend(chunk)
            return 200, json.loads(buf.decode("utf-8", errors="replace"))
    return 0, None


async def _direct_wscn(limit=20):
    code, payload = await _get_json_bounded(
        f"https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit={limit}", 3.5
    )
    if code != 200 or not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        text = re.sub(r"<[^>]+>", "", str(it.get("content_text") or it.get("content") or "")).strip()
        title = str(it.get("title") or "").strip() or text[:180]
        if title:
            out.append({"title": title, "url": str(it.get("uri") or "")})
    return out


async def _direct_sina(limit=20):
    code, payload = await _get_json_bounded(
        f"https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={limit}&zhibo_id=152", 3.5
    )
    if code != 200 or not isinstance(payload, dict):
        return []
    res = payload.get("result") if isinstance(payload, dict) else None
    data = res.get("data") if isinstance(res, dict) else None
    feed = data.get("feed") if isinstance(data, dict) else None
    items = feed.get("list") if isinstance(feed, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(it.get("rich_text") or ""))).strip()
        if text:
            u = str(it.get("docurl") or "")
            out.append({"title": text[:180], "url": u if u.startswith("http") else ""})
    return out


async def fetch_news_from_api(conn: "ConnectionHandler", source="thepaper"):
    """newsnow 聚合接口，失败重试一次；财经快讯类再改走官方直连兜底。"""
    import asyncio
    api_url = f"https://newsnow.busiyi.world/api/s?id={source}"
    news_config = (((conn.config or {}).get("plugins") or {}).get("get_news_from_newsnow") or {})
    custom_url = news_config.get("url")
    if isinstance(custom_url, str) and custom_url.strip().startswith(("https://", "http://")):
        cand_url = custom_url.strip() + source
        if _safe_public_http_url(cand_url):
            api_url = cand_url
    last_err = None
    for attempt, t_out in enumerate((3.0, 4.0), 1):
        try:
            code, payload = await _get_json_bounded(api_url, t_out)
            if code == 200 and isinstance(payload, dict):
                items = payload.get("items")
                if isinstance(items, list):
                    valid = [x for x in items if isinstance(x, dict)]
                    if valid:
                        return valid
            last_err = f"HTTP {code}"
        except Exception as e:
            last_err = type(e).__name__
        if attempt == 1:
            await asyncio.sleep(0.15)
    logger.bind(tag=TAG).warning(f"新闻聚合源 {source} 不可用（{last_err}），尝试直连兜底")
    try:
        if source in ("wallstreetcn-quick", "gelonghui"):
            return await _direct_wscn()
        if source in ("cls-telegraph", "jin10"):
            return await _direct_sina()
    except Exception as e:
        logger.bind(tag=TAG).error(f"直连兜底源异常: {type(e).__name__}")
    return []


DETAIL_MAX_BYTES = 2 * 1024 * 1024


def _safe_public_http_url(url) -> bool:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if (
        not host
        or host in ("localhost", "ip6-localhost", "ip6-loopback")
        or host.endswith(_BLOCKED_HOST_SUFFIXES)
    ):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host or "." not in host:
            return False
        tld = host.rsplit(".", 1)[-1]
        if not tld or tld[0].isdigit():
            return False
        return True
    return _is_global_ip(ip)


async def fetch_news_detail(url):
    """获取新闻详情页内容并使用MarkItDown清理HTML（流式限长防2GB内存溢出）"""
    if not _safe_public_http_url(url):
        return "无法获取详细内容"
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        chunks = bytearray()
        charset = "utf-8"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False) as client:
            cur_url = url
            for _ in range(6):
                if not _safe_public_http_url(cur_url):
                    return "无法获取详细内容"
                cur_parts = urllib.parse.urlsplit(cur_url)
                if httpx.AsyncClient is _ORIG_ASYNC_CLIENT:
                    if not await asyncio.to_thread(_has_only_global_ips, cur_parts.hostname or ""):
                        return "无法获取详细内容"
                async with client.stream("GET", cur_url, headers=headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        loc = response.headers.get("location") or ""
                        if not loc:
                            return "无法获取详细内容"
                        nxt_url = str(response.url.join(loc))
                        if cur_parts.scheme == "https" and not nxt_url.startswith("https://"):
                            return "无法获取详细内容"
                        cur_url = nxt_url
                        continue
                    if response.status_code != 200 or not _safe_public_http_url(str(response.url)):
                        logger.bind(tag=TAG).warning(f"获取新闻详情HTTP {response.status_code}: {url}")
                        return "无法获取详细内容"
                    ctype = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if ctype.startswith(("audio/", "video/", "image/", "font/", "multipart/")) or any(
                        tok in ctype for tok in ("octet-stream", "pdf", "zip", "gzip", "msword", "officedocument", "vnd.")
                    ):
                        return "无法获取详细内容"
                    charset = response.charset_encoding or "utf-8"
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        remain = DETAIL_MAX_BYTES - len(chunks)
                        if remain <= 0:
                            break
                        chunks.extend(chunk[:remain])
                        if len(chunks) >= DETAIL_MAX_BYTES:
                            break
                    break
            else:
                return "无法获取详细内容"
        if not chunks:
            return "无法获取详细内容"
        content_bytes = bytes(chunks)

        def _to_markdown():
            md = MarkItDown(enable_plugins=False)
            return md.convert_stream(
                BytesIO(content_bytes),
                stream_info=StreamInfo(
                    mimetype="text/html",
                    extension=".html",
                    charset=charset,
                ),
            )

        # 在线程池中执行MarkItDown清洗，避免阻塞事件循环上的TTS流式发包
        result = await asyncio.to_thread(_to_markdown)

        # 获取清理后的文本内容
        clean_text = result.text_content

        # 如果清理后的内容为空，返回提示信息
        if not clean_text or len(clean_text.strip()) == 0:
            logger.bind(tag=TAG).warning(f"清理后的新闻内容为空: {url}")
            return "无法解析新闻详情内容，可能是网站结构特殊或内容受限。"

        return clean_text
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻详情失败: {e}")
        return "无法获取详细内容"


# 讲过的新闻按设备记下来（跨对话、hub 重启也记得），默认 3 天内不再报第二次。存在 hub 的 data/ 下（git 忽略）。
SEEN_PATH = "/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/newsnow_seen.json"
_seen_lock = threading.Lock()
_list_cache = {}
# 默认优先关注的方向（私有配置 plugins.get_news_from_newsnow.focus_keywords 可改）：命中的快讯优先放进候选并标出来
DEFAULT_FOCUS = ["上证", "沪指", "深成指", "创业板", "A股", "大盘", "港股", "恒生", "美股", "纳指", "道指", "标普",
                 "半导体", "芯片", "算力", "人工智能", "AI", "英伟达", "台积电", "苹果", "特斯拉",
                 "黄金", "金价", "白银", "原油", "油价", "布伦特", "铜", "美联储", "降息", "加息", "央行", "利率",
                 "国债", "人民币", "汇率", "美元", "关税", "贸易", "中美", "伊朗", "以色列", "美伊", "俄乌", "乌克兰",
                 "俄罗斯", "特朗普", "欧盟", "日本", "证监会", "国务院", "财政部", "回购", "IPO", "并购", "财报", "业绩"]  # source_id -> (时间, 列表)：10 分钟内的「下一批」不重新抓


def _news_key(item):
    title = re.sub(r"[\W_]+", "", str(item.get("title") or ""))
    return title[:18] or str(item.get("url") or "")


def _load_seen():
    import stat
    fd = -1
    try:
        if os.path.islink(SEEN_PATH):
            return {}
        fd = os.open(SEEN_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 512 * 1024:
            return {}
        chunks, rem = [], 512 * 1024 + 1
        while rem > 0:
            b = os.read(fd, min(65536, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b"".join(chunks)
        if len(raw) > 512 * 1024:
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _save_seen(data):
    tmp = f"{SEEN_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
    created = False
    try:
        os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
        if os.path.islink(SEEN_PATH):
            raise OSError("Refusing symlink seen path")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.path.islink(SEEN_PATH):
            raise OSError("Refusing symlink seen path")
        os.replace(tmp, SEEN_PATH)
        created = False
    except Exception as e:
        logger.bind(tag=TAG).warning(f"保存已读新闻失败: {type(e).__name__}")
    finally:
        if created:
            try:
                os.unlink(tmp)
            except OSError:
                pass


QUERYING_REPLY = "正在为您查询。"  # 查询要几秒：先播这一句（句号不能省，合成按标点切句）


def _say_querying(conn) -> None:
    """先播一句「正在为您查询」，接在这一轮回答的前面（同一个 sentence_id）。用户已经打断时不说。"""
    tts, sentence_id = getattr(conn, "tts", None), getattr(conn, "sentence_id", None)
    if tts is None or not sentence_id or getattr(conn, "client_abort", False):
        return
    if getattr(conn, "_query_prompted_sid", None) == sentence_id:
        return
    try:
        from core.providers.tts.dto.dto import ContentType
        conn._query_prompted_sid = sentence_id
        tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=QUERYING_REPLY, sentence_id=sentence_id)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"播查询提示失败: {type(e).__name__}")


def _cfg_int(conn, key, default, lo, hi):
    raw = (((conn.config or {}).get("plugins") or {}).get("get_news_from_newsnow") or {}).get(key)
    if raw is None or isinstance(raw, bool):
        v = default
    else:
        try:
            v = int(raw)
        except (TypeError, ValueError, OverflowError):
            v = default
    return max(lo, min(hi, v))


# 用户要求（2026-11-05）：快的查询不用提示；整轮等待预计超过 6 秒才先说「正在为您查询」。
# 工具开始前大模型已用掉约 1.5 秒，拿到结果后再说话还要约 1.5 秒，所以工具本身超过 QUERYING_AFTER 秒还没好才播。
QUERYING_AFTER = 0.0  # 2026-11-05 用户改主意：新闻一律先说「正在为您查询」


async def _query_with_notice(conn, coro):
    import asyncio

    async def later():
        await asyncio.sleep(QUERYING_AFTER)
        _say_querying(conn)
    timer = asyncio.create_task(later())
    try:
        return await coro
    finally:
        if not timer.done():
            timer.cancel()


@register_function(
    "get_news_from_newsnow",
    GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_news_from_newsnow(
    conn: "ConnectionHandler",
    source: str = None,
    detail: bool = False,
    pick: int = None,
    more: bool = False,
    lang: str = "zh_CN",
):
    return await _query_with_notice(conn, _get_news_impl(conn, source, detail, pick, more, lang))


async def _get_news_impl(conn, source=None, detail=False, pick=None, more=False, lang="zh_CN"):
    """列出一批带编号的新闻标题，或讲上一批里某一条的详细内容。"""
    try:
        batch_size = _cfg_int(conn, "batch_size", 3, 1, 8)
        detail_chars = _cfg_int(conn, "detail_chars", 2000, 300, 8000)
        last = getattr(conn, "newsnow_last_batch", None) or []

        if str(detail).lower() == "true":
            if not last:
                return ActionResponse(Action.REQLLM, "还没有列过新闻，请先问用户想听哪类新闻，并先列出一批。", None)
            if isinstance(pick, bool):
                n = 0
            else:
                try:
                    n = int(pick) if pick is not None else (1 if len(last) == 1 else 0)
                except (TypeError, ValueError, OverflowError):
                    n = 0
            if not 1 <= n <= len(last):
                return ActionResponse(Action.REQLLM, "没有给出有效的候选号。请回看上一次新闻工具返回的候选（#号），"
                                                     "找到用户要听的那条主线所依据的候选号再调用；实在对不上就简短地问用户要听哪一条。", None)
            item = last[n - 1]
            url, title = item.get("url") or "", item.get("title") or "未知标题"
            logger.bind(tag=TAG).info(f"新闻详情: 候选#{n} {title}")
            if not url or url == "#":
                return ActionResponse(Action.REQLLM, f"《{title}》这条是快讯，没有更多详细内容。请根据标题本身简单说明，并问用户要不要听别的。", None)
            detail_content = await fetch_news_detail(url)
            if not detail_content or detail_content in ("无法获取详细内容", "无法解析新闻详情内容，可能是网站结构特殊或内容受限。"):
                # 有的来源（如联合早报）设备上打不开原文：让模型用联网搜索按标题查
                return ActionResponse(Action.REQLLM, f"《{title}》的原文打不开。请马上调用联网搜索 web_search，用这条新闻的标题去搜，"
                                                     f"再把搜到的内容讲给用户；不要编造。", None)
            detail_content = " ".join(detail_content.split())
            if len(detail_content) < 150:   # 抓到的只是一两句（页面靠脚本加载），当作没抓到
                return ActionResponse(Action.REQLLM, f"《{title}》的原文没抓到正文。请马上调用联网搜索 web_search，用这条新闻的标题去搜，"
                                                     f"再把搜到的内容讲给用户；不要编造。", None)
            detail_content = detail_content[:detail_chars]
            return ActionResponse(Action.REQLLM,
                f"根据下列数据，用{lang}回应用户的新闻详情查询请求：\n\n新闻标题: {title}\n详细内容: {detail_content}\n\n"
                f"(请提取关键信息，用自然、流畅的口语讲给用户听，像在讲一个完整的新闻，不要提这是总结；"
                f"讲完问用户要听这一批里的其他条，还是换下一批。)", None)

        news_sources = get_news_sources_from_config(conn)
        sources_list = [x.strip() for x in news_sources.split(";") if x.strip()]
        state = getattr(conn, "newsnow_state", None)
        if state is None:
            state = conn.newsnow_state = {}
        if str(more).lower() == "true" and not source and last:
            source = last[0].get("source_name")
            if source in {n for n, _ in FINANCE_GROUP}:
                source = "财联社"
        source_id = CHANNEL_MAP.get(source) if source in sources_list else None
        if not source_id:   # 没指定或不在列表里：默认财经快讯（用户以财经、股票、国际为主）
            if source:
                logger.bind(tag=TAG).warning(f"无效的新闻源: {source}，使用默认财经快讯")
            source_id, source = "cls-telegraph", "财联社"

        import asyncio
        keep_days = _cfg_int(conn, "seen_days", 3, 1, 30)
        device = str(getattr(conn, "device_id", "") or "default")
        now = time.time()

        def _read_dev_seen():
            with _seen_lock:
                raw = _load_seen().get(device)
                return dict(raw) if isinstance(raw, dict) else {}

        dev_seen = await asyncio.to_thread(_read_dev_seen)
        seen = {
            str(k): float(t)
            for k, t in dev_seen.items()
            if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and now - t < keep_days * 86400
        }
        group = FINANCE_GROUP if source in {n for n, _ in FINANCE_GROUP} else [(source, source_id)]
        cache_key = "finance" if len(group) > 1 else source_id
        cached = _list_cache.get(cache_key)
        if not cached or now - cached[0] > 600 or str(more).lower() != "true":
            import asyncio
            lists = await asyncio.gather(*[fetch_news_from_api(conn, sid) for _, sid in group], return_exceptions=True)
            merged, rounds = [], [l if isinstance(l, list) else [] for l in lists]
            for k in range(max((len(l) for l in rounds), default=0)):      # 轮流各取一条，财经多源交错
                for (name, sid), l in zip(group, rounds):
                    if k < len(l):
                        merged.append(dict(l[k], _src=name, _sid=sid))
            if not merged:
                return ActionResponse(Action.REQLLM, f"抱歉，未能从{source}获取到新闻，请稍后再试或换一个新闻源。", None)
            for old_k in [k for k, v in list(_list_cache.items()) if now - v[0] > 600]:
                _list_cache.pop(old_k, None)
            while len(_list_cache) >= 8 and cache_key not in _list_cache:
                _list_cache.pop(next(iter(_list_cache)), None)
            _list_cache[cache_key] = (now, merged)
            items = merged
        else:
            items = cached[1]
        if len(group) > 1:
            source = "财经快讯（" + "、".join(n for n, _ in group) + "）"
        fresh, keys = [], set()
        for x in items:
            k = _news_key(x)
            if k in seen or k in keys or SOCIAL_RE.search(str(x.get("title") or "")):
                continue
            keys.add(k); fresh.append(x)
        if not fresh:
            return ActionResponse(Action.REQLLM, f"{source}最近的新闻都已经给用户讲过了。请告诉用户，并问要不要换个类别或来源。", None)
        pool_size = _cfg_int(conn, "pool_size", 20, 6, 40)
        raw_focus = (((conn.config or {}).get("plugins") or {}).get("get_news_from_newsnow") or {}).get("focus_keywords")
        if isinstance(raw_focus, str):
            focus = [w.strip() for w in re.split(r"[;,；，、]", raw_focus) if w.strip()] or DEFAULT_FOCUS
        elif isinstance(raw_focus, (list, tuple, set)):
            focus = [str(w).strip() for w in raw_focus if str(w).strip()] or DEFAULT_FOCUS
        else:
            focus = DEFAULT_FOCUS
        def hits(x):
            t = str(x.get("title") or "")
            return [w for w in focus if w and w in t]
        # 先按重点关注点命中多少排（同分保持最新在前），候选里关注的占多数，再用最新的补满
        ranked = sorted(range(len(fresh)), key=lambda k: (-min(len(hits(fresh[k])), 3), k))
        pick_idx = [k for k in ranked if hits(fresh[k])][: pool_size * 2 // 3]
        pick_idx += [k for k in range(len(fresh)) if k not in pick_idx][: pool_size - len(pick_idx)]
        pool = [fresh[k] for k in sorted(pick_idx)]
        new_keys = [_news_key(x) for x in pool]

        def _merge_save_seen():
            with _seen_lock:
                seen_all = _load_seen()
                cur_dev = seen_all.get(device)
                merged = {
                    str(k): float(t)
                    for k, t in (cur_dev.items() if isinstance(cur_dev, dict) else ())
                    if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and now - t < keep_days * 86400
                }
                for nk in new_keys:  # 这一批交给模型挑选的都算讲过：下一批是新的一池
                    merged[nk] = now
                seen_all[device] = merged
                _save_seen(seen_all)

        await asyncio.to_thread(_merge_save_seen)
        conn.newsnow_last_batch = [{"title": x.get("title", "未知标题"), "url": x.get("url", "#"),
                                    "source_id": x.get("_sid", source_id), "source_name": x.get("_src", source)} for x in pool]
        conn.last_newsnow_link = {"url": pool[0].get("url", "#"), "title": pool[0].get("title", ""), "source_id": source_id}
        # 借鉴小白：顺带抓前几则的正文片段，让模型能讲背景和影响
        heads = [(k, x) for k, x in enumerate(pool) if _safe_public_http_url(x.get("url"))][:3]
        bodies = await asyncio.gather(*[asyncio.wait_for(fetch_news_detail(x["url"]), 4.0) for _, x in heads], return_exceptions=True)
        snippet = {k: " ".join(b.split())[:500] for (k, _), b in zip(heads, bodies)
                   if isinstance(b, str) and len(b.strip()) > 80 and not b.startswith("无法")}
        logger.bind(tag=TAG).info(f"获取新闻: {source} 候选 {len(pool)} 条（列表 {len(items)} 条，已讲过 {len(items) - len(fresh)} 条），正文片段 {len(snippet)} 则")
        lines = []
        for k, x in enumerate(pool):
            tag = "、".join(hits(x)[:3])
            line = f"#{k + 1} [{x.get('_src', source)}]{'【重点关注：' + tag + '】' if tag else ''} {' '.join(str(x.get('title') or '').split())[:200]}"
            if k in snippet:
                line += f"\n   正文片段：{snippet[k]}"
            lines.append(line)
        return ActionResponse(Action.REQLLM,
            f"以下是{source}最新的、之前没给用户讲过的新闻候选（#号是候选号）：\n" + "\n".join(lines) +
            "\n\n(请像一份简短财经早报那样播报：从候选里挑出最核心的3条主线——先看标了【重点关注】的，"
            "再按对市场的重要性取舍；相关的几则合成一条主线、做个总结，而不是逐条念；"
            "优先大盘和宏观政策、央行和利率、大宗商品和汇率、龙头公司、牵动市场的国际局势，不要国内社会类新闻。"
            "每条只用阿拉伯数字开头，格式「1，……。2，……。3，……。」，不要说「第一条」「第一是」；"
            "每条先一句话说清发生了什么，再用一两句说背后的原因和对市场或股票的影响，不要干巴巴念标题，也不要编造候选里没有的数字；"
            "三条加起来控制在四百字以内。最后用一句很短的话问想详细听哪一条，还是换下一批。"
            "之后用户说要详细听第几条，就用 detail=true、pick=你那条主线所依据的最主要那则新闻的候选号。)", None)
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻出错: {type(e).__name__}: {e}")
        return ActionResponse(Action.REQLLM, "抱歉，获取新闻时发生错误，请稍后再试。", None)
