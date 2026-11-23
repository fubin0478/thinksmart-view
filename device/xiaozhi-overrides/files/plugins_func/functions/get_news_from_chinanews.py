import asyncio
import ipaddress
import random
import re
import socket
import urllib.parse
import xml.etree.ElementTree as ET
import httpx
from bs4 import BeautifulSoup
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


TAG = __name__
logger = setup_logging()
MAX_RSS_BYTES = 1024 * 1024
MAX_DETAIL_BYTES = 1024 * 1024
MAX_DETAIL_CHARS = 2000
_XML_UNSAFE_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

GET_NEWS_FROM_CHINANEWS_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_news_from_chinanews",
        "description": (
            "当用户要求查看或收听新闻时调用（如'来条新闻''今天有什么新闻'）。"
            "用户可以指定新闻类型，如社会新闻、科技新闻、国际新闻等。"
            "如果没有指定，默认播报社会新闻。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "新闻类别，例如社会、科技、国际。可选参数，如果不提供则使用默认类别",
                },
                "detail": {
                    "type": "boolean",
                    "description": "是否获取详细内容，默认为false。如果为true，则获取上一条新闻的详细内容",
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


_BLOCKED_HOST_SUFFIXES = (
    ".local", ".localhost", ".internal", ".lan", ".home.arpa",
    ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me",
)
_ORIG_ASYNC_CLIENT = httpx.AsyncClient


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


async def _fetch_public_bytes(url: str, timeout_sec: float, max_bytes: int, truncate: bool = False) -> bytes | None:
    if not _safe_public_http_url(url):
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    buf = bytearray()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_sec, connect=3.0),
        follow_redirects=False,
    ) as client:
        cur_url = url
        for _ in range(6):
            if not _safe_public_http_url(cur_url):
                return None
            cur_parts = urllib.parse.urlsplit(cur_url)
            if httpx.AsyncClient is _ORIG_ASYNC_CLIENT:
                if not await asyncio.to_thread(_has_only_global_ips, cur_parts.hostname or ""):
                    return None
            async with client.stream("GET", cur_url, headers=headers) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    loc = response.headers.get("location") or ""
                    if not loc:
                        return None
                    nxt_url = str(response.url.join(loc))
                    if cur_parts.scheme == "https" and not nxt_url.startswith("https://"):
                        return None
                    cur_url = nxt_url
                    continue
                if response.status_code != 200 or not _safe_public_http_url(str(response.url)):
                    return None
                ctype = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if ctype.startswith(("audio/", "video/", "image/", "font/", "multipart/")) or any(
                    tok in ctype for tok in ("octet-stream", "pdf", "zip", "gzip")
                ):
                    return None
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if len(buf) + len(chunk) > max_bytes:
                        if truncate:
                            buf.extend(chunk[: max_bytes - len(buf)])
                            break
                        return None
                    buf.extend(chunk)
                return bytes(buf)
    return None


def _safe_parse_rss_xml(xml_bytes: bytes) -> ET.Element:
    if _XML_UNSAFE_RE.search(xml_bytes):
        raise ValueError("Refusing XML with DOCTYPE/ENTITY declarations")
    return ET.fromstring(xml_bytes)


def _extract_article_text(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, "html.parser")
    content_div = soup.select_one(".content_desc, .content, article, .article-content")
    paragraphs = content_div.find_all("p") if content_div else soup.find_all("p")
    content = "\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
    return content[:MAX_DETAIL_CHARS]


async def fetch_news_from_rss(rss_url):
    """从RSS源获取新闻列表"""
    try:
        xml_bytes = await _fetch_public_bytes(rss_url, timeout_sec=5.0, max_bytes=MAX_RSS_BYTES, truncate=False)
        if not xml_bytes:
            return []
        root = _safe_parse_rss_xml(xml_bytes)

        news_items = []
        for item in root.findall(".//item")[:50]:
            title = (
                (item.find("title").text or "").strip()[:300]
                if item.find("title") is not None and item.find("title").text
                else "无标题"
            )
            raw_link = (
                (item.find("link").text or "").strip()
                if item.find("link") is not None and item.find("link").text
                else "#"
            )
            link = raw_link if _safe_public_http_url(raw_link) else "#"
            description = (
                (item.find("description").text or "").strip()[:1000]
                if item.find("description") is not None and item.find("description").text
                else "无描述"
            )
            pubDate = (
                (item.find("pubDate").text or "").strip()[:100]
                if item.find("pubDate") is not None and item.find("pubDate").text
                else "未知时间"
            )

            news_items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "pubDate": pubDate,
                }
            )

        return news_items
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取RSS新闻失败: {e}")
        return []


async def fetch_news_detail(url):
    """获取新闻详情页内容并总结"""
    try:
        html_bytes = await _fetch_public_bytes(url, timeout_sec=10.0, max_bytes=MAX_DETAIL_BYTES, truncate=True)
        if not html_bytes:
            return "无法获取详细内容"
        content = await asyncio.to_thread(_extract_article_text, html_bytes)
        return content if content else "无法获取详细内容"
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻详情失败: {e}")
        return "无法获取详细内容"


def map_category(category_text):
    """将用户输入的中文类别映射到配置文件中的类别键"""
    if not category_text:
        return None

    category_map = {
        "社会": "society_rss_url",
        "社会新闻": "society_rss_url",
        "国际": "world_rss_url",
        "国际新闻": "world_rss_url",
        "财经": "finance_rss_url",
        "财经新闻": "finance_rss_url",
        "金融": "finance_rss_url",
        "经济": "finance_rss_url",
    }

    normalized_category = str(category_text).lower().strip()
    return category_map.get(normalized_category, normalized_category)


@register_function(
    "get_news_from_chinanews",
    GET_NEWS_FROM_CHINANEWS_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_news_from_chinanews(
    conn: "ConnectionHandler",
    category: str = None,
    detail: bool = False,
    lang: str = "zh_CN",
):
    """获取新闻并随机选择一条进行播报，或获取上一条新闻的详细内容"""
    try:
        if detail:
            if (
                not hasattr(conn, "last_news_link")
                or not conn.last_news_link
                or "link" not in conn.last_news_link
            ):
                return ActionResponse(
                    Action.REQLLM,
                    "抱歉，没有找到最近查询的新闻，请先获取一条新闻。",
                    None,
                )

            link = conn.last_news_link.get("link")
            title = conn.last_news_link.get("title", "未知标题")

            if link == "#" or not _safe_public_http_url(link):
                return ActionResponse(
                    Action.REQLLM, "抱歉，该新闻没有可用的链接获取详细内容。", None
                )

            logger.bind(tag=TAG).debug(f"获取新闻详情: {title}, URL={link}")

            detail_content = await fetch_news_detail(link)

            if not detail_content or detail_content == "无法获取详细内容":
                return ActionResponse(
                    Action.REQLLM,
                    f"抱歉，无法获取《{title}》的详细内容，可能是链接已失效或网站结构发生变化。",
                    None,
                )

            detail_report = (
                f"根据下列数据，用{lang}回应用户的新闻详情查询请求：\n\n"
                f"新闻标题: {title}\n"
                f"详细内容: {detail_content}\n\n"
                f"(请对上述新闻内容进行总结，提取关键信息，以自然、流畅的方式向用户播报，"
                f"不要提及这是总结，就像是在讲述一个完整的新闻故事)"
            )

            return ActionResponse(Action.REQLLM, detail_report, None)

        plugins_cfg = (conn.config or {}).get("plugins") or {}
        rss_config = plugins_cfg.get("get_news_from_chinanews") or {}
        if not isinstance(rss_config, dict):
            rss_config = {}
        default_rss_url = rss_config.get(
            "default_rss_url", "https://www.chinanews.com.cn/rss/society.xml"
        )

        mapped_category = map_category(category)

        rss_url = default_rss_url
        if (
            isinstance(mapped_category, str)
            and mapped_category.endswith("_rss_url")
            and isinstance(rss_config.get(mapped_category), str)
        ):
            rss_url = rss_config[mapped_category]

        logger.bind(tag=TAG).info(
            f"获取新闻: 原始类别={category}, 映射类别={mapped_category}, URL={rss_url}"
        )

        news_items = await fetch_news_from_rss(rss_url)

        if not news_items:
            return ActionResponse(
                Action.REQLLM, "抱歉，未能获取到新闻信息，请稍后再试。", None
            )

        selected_news = random.choice(news_items)

        conn.last_news_link = {
            "link": selected_news.get("link", "#"),
            "title": selected_news.get("title", "未知标题"),
        }

        news_report = (
            f"根据下列数据，用{lang}回应用户的新闻查询请求：\n\n"
            f"新闻标题: {selected_news['title']}\n"
            f"发布时间: {selected_news['pubDate']}\n"
            f"新闻内容: {selected_news['description']}\n"
            f"(请以自然、流畅的方式向用户播报这条新闻，可以适当总结内容，"
            f"直接读出新闻即可，不需要额外多余的内容。"
            f"如果用户询问更多详情，告知用户可以说'请详细介绍这条新闻'获取更多内容)"
        )

        return ActionResponse(Action.REQLLM, news_report, None)

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻出错: {e}")
        return ActionResponse(
            Action.REQLLM, "抱歉，获取新闻时发生错误，请稍后再试。", None
        )
