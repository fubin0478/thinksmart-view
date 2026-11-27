import asyncio
import ipaddress
import logging
from urllib.parse import urlsplit

import websockets
from config.logger import setup_logging


class SuppressInvalidHandshakeFilter(logging.Filter):
    """过滤掉无效握手错误日志（如HTTPS访问WS端口）"""

    def filter(self, record):
        msg = record.getMessage()
        suppress_keywords = [
            "opening handshake failed",
            "did not receive a valid HTTP request",
            "connection closed while reading HTTP request",
            "line without CRLF",
        ]
        return not any(keyword in msg for keyword in suppress_keywords)


def _setup_websockets_logger():
    """配置 websockets 相关的所有 logger，过滤无效握手错误"""
    filter_instance = SuppressInvalidHandshakeFilter()
    for logger_name in ["websockets", "websockets.server", "websockets.client"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(filter_instance)


_setup_websockets_logger()


from core.auth import AuthenticationError, AuthManager
from core.connection import ConnectionHandler
from core.utils.modules_initialize import initialize_modules

TAG = __name__


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


def _is_trusted_ws_origin(origin, host_hdr) -> bool:
    if origin is None or not isinstance(origin, str):
        return False
    raw = origin.strip()
    if not raw or raw.lower() == "null":
        return False
    try:
        po = urlsplit(raw)
        _ = po.port
        po_hn = po.hostname
        hs = urlsplit(f"//{(host_hdr or '').strip()}")
        _ = hs.port
        hs_hn = hs.hostname
    except ValueError:
        return False
    if (
        po.scheme not in ("http", "https")
        or not po_hn
        or not hs_hn
        or po.username is not None
        or po.password is not None
        or po.path
        or po.query
        or po.fragment
    ):
        return False
    if not _is_trusted_host(po.netloc):
        return False
    po_low = po_hn.lower()
    if po_low in ("127.0.0.1", "localhost", "::1"):
        return True
    return po_low == hs_hn.lower()


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging(config)
        self.config_lock = asyncio.Lock()
        modules = initialize_modules(
            self.logger,
            self.config,
            "VAD" in self.config["selected_module"],
            "ASR" in self.config["selected_module"],
            "LLM" in self.config["selected_module"],
            False,
            "Memory" in self.config["selected_module"],
            "Intent" in self.config["selected_module"],
        )
        self._vad = modules["vad"] if "vad" in modules else None
        self._asr = modules["asr"] if "asr" in modules else None
        self._llm = modules["llm"] if "llm" in modules else None
        self._intent = modules["intent"] if "intent" in modules else None
        self._memory = modules["memory"] if "memory" in modules else None

        auth_config = self.config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = self.config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds", None)
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "127.0.0.1") or "127.0.0.1"
        port = int(server_config.get("port", 8000))

        async with websockets.serve(
            self._handle_connection,
            host,
            port,
            process_request=self._http_response,
            ping_interval=None,
            max_size=1048576,
        ):
            await asyncio.Future()

    async def _handle_connection(self, websocket: websockets.ServerConnection):
        headers = {str(k).lower(): v for k, v in websocket.request.headers.items()}
        if headers.get("device-id", None) is None:
            from urllib.parse import parse_qs, urlparse

            request_path = websocket.request.path
            if not request_path:
                self.logger.bind(tag=TAG).error("无法获取请求路径")
                await websocket.close()
                return
            parsed_url = urlparse(request_path)
            query_params = parse_qs(parsed_url.query)
            def _clean_qhdr(val: str, max_len: int) -> str:
                return "".join(ch for ch in (val or "") if ch not in ("\r", "\n", "\x00")).strip()[:max_len]

            dev_q = _clean_qhdr(query_params.get("device-id", [""])[0], 128)
            if not dev_q:
                try:
                    await websocket.send("端口正常，如需测试连接，请启动digital-human测试")
                    await websocket.close()
                except websockets.exceptions.ConnectionClosed:
                    pass
                return
            websocket.request.headers["device-id"] = dev_q
            if "client-id" in query_params:
                cli_q = _clean_qhdr(query_params["client-id"][0], 128)
                if cli_q:
                    websocket.request.headers["client-id"] = cli_q
            if "authorization" in query_params:
                auth_q = _clean_qhdr(query_params["authorization"][0], 4096)
                if auth_q:
                    websocket.request.headers["authorization"] = auth_q

        try:
            await self._handle_auth(websocket)
        except AuthenticationError:
            try:
                await websocket.send("认证失败")
                await websocket.close()
            except websockets.exceptions.ConnectionClosed:
                pass
            return

        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,
        )
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理连接时出错: {e}")
        finally:
            try:
                if hasattr(websocket, "closed"):
                    if not websocket.closed:
                        await websocket.close()
                elif hasattr(websocket, "state"):
                    if getattr(websocket.state, "name", None) != "CLOSED":
                        await websocket.close()
                else:
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"服务器端强制关闭连接时出错: {close_error}"
                )

    async def _http_response(self, websocket, request_headers):
        remote_addr = getattr(websocket, "remote_address", None)
        if remote_addr is not None:
            peer_ip = remote_addr[0] if isinstance(remote_addr, (tuple, list)) and remote_addr else str(remote_addr)
            hn_ip = str(peer_ip or "").split("%", 1)[0].strip()
            try:
                ip = ipaddress.ip_address(hn_ip)
                if getattr(ip, "ipv4_mapped", None) is not None:
                    ip = ip.ipv4_mapped
                bind_ip = str(((getattr(self, "config", None) or {}).get("server") or {}).get("ip") or "127.0.0.1").strip()
                need_loopback = bind_ip in ("127.0.0.1", "localhost", "::1")
                peer_ok = bool(ip.is_loopback) if need_loopback else not (ip.is_global or ip.is_multicast or ip.is_unspecified)
            except ValueError:
                peer_ok = False
            if not peer_ok:
                self.logger.bind(tag=TAG).warning("拒绝非受信任来源 IP 的 WebSocket/HTTP 请求")
                return websocket.respond(403, "Forbidden\n")

        hdrs_l = {k.lower(): v for k, v in request_headers.headers.items()}
        host_hdr = hdrs_l.get("host", "")
        if not _is_trusted_host(host_hdr):
            self.logger.bind(tag=TAG).warning("拒绝不可信 Host 的 WebSocket/HTTP 请求")
            return websocket.respond(403, "Forbidden\n")

        origin = hdrs_l.get("origin")
        if origin is not None and not _is_trusted_ws_origin(origin, host_hdr):
            self.logger.bind(tag=TAG).warning("拒绝外部跨站 Origin 的 WebSocket/HTTP 请求")
            return websocket.respond(403, "Forbidden\n")

        conn_tokens = [
            t.strip() for t in (hdrs_l.get("connection", "") or "").lower().split(",")
        ]
        if "upgrade" in conn_tokens:
            return None
        return websocket.respond(200, "Server is running\n")

    async def _handle_auth(self, websocket: websockets.ServerConnection):
        if self.auth_enable:
            headers = {str(k).lower(): v for k, v in websocket.request.headers.items()}
            device_id = headers.get("device-id", None)
            client_id = headers.get("client-id", None)
            if self.allowed_devices and device_id in self.allowed_devices:
                return
            token = headers.get("authorization", "")
            if token[:7].lower() == "bearer ":
                token = token[7:].strip()
            else:
                raise AuthenticationError("Missing or invalid Authorization header")
            auth_success = self.auth.verify_token(
                token, client_id=client_id, username=device_id
            )
            if not auth_success:
                raise AuthenticationError("Invalid token")
