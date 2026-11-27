import asyncio
import ipaddress
from urllib.parse import urlsplit

from aiohttp import web
from config.logger import setup_logging

TAG = __name__
CLIENT_MAX_SIZE = 6 * 1024 * 1024
_FORBIDDEN_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _is_trusted_host(host_hdr) -> bool:
    if not host_hdr or not isinstance(host_hdr, str):
        return False
    raw = host_hdr.strip()
    if not raw or any(c in raw for c in ("/", "\\", "?", "#", "@", " ", "\t", "\r", "\n")):
        return False
    try:
        sp = urlsplit(f"//{raw}")
        _ = sp.port
        hn = sp.hostname
    except ValueError:
        return False
    if (
        not hn
        or sp.username is not None
        or sp.password is not None
        or sp.path
        or sp.query
        or sp.fragment
    ):
        return False
    hn_ip = hn.split("%", 1)[0] if "%" in hn else hn
    try:
        ip = ipaddress.ip_address(hn_ip)
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        return not (ip.is_global or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        pass
    if "%" in hn:
        return False
    hn_clean = hn[:-1].lower() if hn.endswith(".") else hn.lower()
    if not hn_clean or ".." in hn_clean:
        return False
    if hn_clean == "localhost" or "." not in hn_clean:
        return True
    return hn_clean.endswith((".local", ".lan", ".home", ".internal", ".home.arpa"))


def _is_loopback_origin(orig) -> bool:
    if orig is None or not isinstance(orig, str):
        return False
    raw = orig.strip()
    if not raw:
        return False
    try:
        po = urlsplit(raw)
        _ = po.port
        hn = po.hostname
    except ValueError:
        return False
    if (
        po.scheme not in ("http", "https")
        or not hn
        or po.username is not None
        or po.password is not None
        or po.path
        or po.query
        or po.fragment
    ):
        return False
    return hn.lower() in ("127.0.0.1", "localhost", "::1")


def _is_same_host_origin(orig, req_scheme: str, host_hdr: str) -> bool:
    if orig is None or not isinstance(orig, str) or not host_hdr:
        return False
    raw = orig.strip()
    if not raw:
        return False
    try:
        po = urlsplit(raw)
        po_port = po.port
        po_hn = po.hostname
        hs = urlsplit(f"//{host_hdr.strip()}")
        hs_port = hs.port
        hs_hn = hs.hostname
    except ValueError:
        return False
    if (
        po.scheme not in ("http", "https")
        or po.scheme != (req_scheme or "http").lower()
        or not po_hn
        or not hs_hn
        or po.username is not None
        or po.password is not None
        or po.path
        or po.query
        or po.fragment
    ):
        return False
    default_port = 443 if po.scheme == "https" else 80
    return (
        po_hn.lower() == hs_hn.lower()
        and (po_port or default_port) == (hs_port or default_port)
    )


def _is_loopback_ip(raw) -> bool:
    if not raw:
        return False
    hn = str(raw).split("%", 1)[0].strip()
    try:
        ip = ipaddress.ip_address(hn)
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        return bool(ip.is_loopback)
    except ValueError:
        return False


def _is_local_or_lan_ip(raw) -> bool:
    if not raw:
        return False
    hn = str(raw).split("%", 1)[0].strip()
    try:
        ip = ipaddress.ip_address(hn)
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        return not (ip.is_global or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False


@web.middleware
async def _hub_security_middleware(request: web.Request, handler):
    peer = request.remote
    path = request.path or ""
    if peer:
        if path == "/kiosk" or path.startswith("/kiosk/") or path == "/admin" or path.startswith("/admin/"):
            if not _is_loopback_ip(peer):
                return web.json_response(
                    {"ok": False, "error": "Loopback required"},
                    status=403,
                    headers=_FORBIDDEN_HEADERS,
                )
        elif not _is_local_or_lan_ip(peer):
            return web.json_response(
                {"ok": False, "error": "Forbidden peer"},
                status=403,
                headers=_FORBIDDEN_HEADERS,
            )

    host_hdr = request.headers.get("Host")
    if not _is_trusted_host(host_hdr):
        return web.json_response(
            {"ok": False, "error": "Forbidden host"},
            status=403,
            headers=_FORBIDDEN_HEADERS,
        )

    orig = request.headers.get("Origin")
    if path == "/kiosk" or path.startswith("/kiosk/"):
        sfs = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs == "cross-site" or (orig is not None and not _is_loopback_origin(orig)):
            return web.json_response(
                {"ok": False, "error": "Cross-origin forbidden"},
                status=403,
                headers=_FORBIDDEN_HEADERS,
            )
    elif path == "/admin" or path.startswith("/admin/"):
        sfs = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if (orig is not None and not _is_same_host_origin(orig, request.scheme, host_hdr)) or (
            sfs == "cross-site" and request.method not in ("GET", "HEAD", "OPTIONS")
        ):
            return web.json_response(
                {"ok": False, "error": "Cross-origin forbidden"},
                status=403,
                headers=_FORBIDDEN_HEADERS,
            )

    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        exc.headers.setdefault("X-Content-Type-Options", "nosniff")
        raise
    if resp is not None and hasattr(resp, "headers"):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()

    def create_app(self) -> web.Application:
        app = web.Application(
            client_max_size=CLIENT_MAX_SIZE,
            middlewares=[_hub_security_middleware],
        )
        from core.admin import register_admin_routes

        register_admin_routes(app, self.config, getattr(self, "ws_server", None))
        return app

    async def start(self):
        try:
            server_config = self.config["server"]
            host = server_config.get("ip", "127.0.0.1") or "127.0.0.1"
            port = int(server_config.get("http_port", 8003))

            if port:
                app = self.create_app()
                runner = web.AppRunner(app)
                await runner.setup()
                try:
                    site = web.TCPSite(runner, host, port)
                    await site.start()

                    while True:
                        await asyncio.sleep(3600)
                finally:
                    await runner.cleanup()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise
