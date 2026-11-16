import base64
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import time
from typing import Dict, List, Tuple
from urllib.parse import urlsplit

from aiohttp import web
from core.api.base_handler import BaseHandler
from core.auth import AuthManager
from core.utils.util import get_local_ip, get_vision_url

TAG = __name__
MAX_OTA_BODY_BYTES = 64 * 1024


def _safe_basename(filename: str) -> str:
    # Prevent directory traversal
    return os.path.basename(filename)


def _parse_version(ver: str) -> Tuple[int, ...]:
    # conservative parser: split by non-digit, keep numeric parts
    parts = re.findall(r"\d+", ver)
    return tuple(int(p) for p in parts) if parts else (0,)


def _is_higher_version(a: str, b: str) -> bool:
    """Return True if version string a > b (semver-like numeric compare)."""
    ta = _parse_version(a)
    tb = _parse_version(b)
    maxlen = max(len(ta), len(tb))
    for i in range(maxlen):
        ai = ta[i] if i < len(ta) else 0
        bi = tb[i] if i < len(tb) else 0
        if ai > bi:
            return True
        if ai < bi:
            return False
    return False


def _is_trusted_local_origin(orig: str, host_hdr: str) -> bool:
    if not orig or not isinstance(orig, str):
        return False
    try:
        po = urlsplit(orig.strip())
        _ = po.port
        po_hn = po.hostname
    except ValueError:
        return False
    if (
        po.scheme not in ("http", "https")
        or not po_hn
        or po.username is not None
        or po.password is not None
        or po.path
        or po.query
        or po.fragment
    ):
        return False
    hn_low = po_hn.lower()
    if hn_low in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        hs = urlsplit(f"//{(host_hdr or '').strip()}")
        _ = hs.port
        hs_hn = (hs.hostname or "").lower()
    except ValueError:
        return False
    if not hs_hn or hn_low != hs_hn:
        return False
    try:
        ip = ipaddress.ip_address(hn_low.split("%", 1)[0])
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        return not (ip.is_global or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        if "%" in hn_low:
            return False
        clean = hn_low[:-1] if hn_low.endswith(".") else hn_low
        return ("." not in clean) or clean.endswith(
            (".local", ".lan", ".home", ".internal", ".home.arpa")
        )


def _valid_header_id(val: str) -> bool:
    return bool(val) and len(val) <= 128 and not any(c in val for c in ("\r", "\n", "\x00"))


class OTAHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)
        auth_config = config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # 设备白名单
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds")
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

        # firmware storage
        self.bin_dir = os.path.join(os.getcwd(), "data", "bin")
        self._bin_cache: Dict = {
            "updated_at": 0,
            "ttl": config.get("firmware_cache_ttl", 30),
            "files_by_model": {},
        }

    def _refresh_bin_cache_if_needed(self):
        now = int(time.time())
        ttl = int(self._bin_cache.get("ttl", 30))
        if now - int(
            self._bin_cache.get("updated_at", 0)
        ) < ttl and self._bin_cache.get("files_by_model"):
            return

        files_by_model: Dict[str, List[Tuple[str, str]]] = {}
        try:
            if not os.path.isdir(self.bin_dir):
                os.makedirs(self.bin_dir, exist_ok=True)

            pattern = os.path.join(self.bin_dir, "*.bin")
            for path in glob.glob(pattern):
                try:
                    st = os.lstat(path)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                except OSError:
                    continue
                fname = os.path.basename(path)
                m = re.match(r"^(.+?)_([0-9][A-Za-z0-9\.\-_]*)\.bin$", fname)
                if not m:
                    continue
                model = m.group(1)
                version = m.group(2)
                files_by_model.setdefault(model, []).append((version, fname))

            for model, items in files_by_model.items():
                items.sort(key=lambda it: _parse_version(it[0]), reverse=True)

            self._bin_cache["files_by_model"] = files_by_model
            self._bin_cache["updated_at"] = now
            self.logger.bind(tag=TAG).info(
                f"Firmware cache refreshed: {len(files_by_model)} models"
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"刷新固件缓存失败: {e}")

    def generate_password_signature(self, content: str, secret_key: str) -> str:
        """生成MQTT密码签名"""
        try:
            hmac_obj = hmac.new(
                secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
            )
            signature = hmac_obj.digest()
            return base64.b64encode(signature).decode("utf-8")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"生成MQTT密码签名失败: {e}")
            return ""

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址"""
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket", "")

        if "你的" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def handle_post(self, request):
        """处理 OTA POST 请求"""
        response = None
        try:
            orig = request.headers.get("Origin")
            if orig is not None and not _is_trusted_local_origin(
                orig, request.headers.get("Host", "")
            ):
                response = web.Response(
                    text=json.dumps(
                        {"success": False, "message": "cross-origin forbidden"},
                        separators=(",", ":"),
                    ),
                    content_type="application/json",
                    status=403,
                )
                return response

            if getattr(request, "content", None) is not None:
                raw_bytes = await request.content.read(MAX_OTA_BODY_BYTES + 1)
                if len(raw_bytes) > MAX_OTA_BODY_BYTES:
                    response = web.Response(
                        text=json.dumps(
                            {"success": False, "message": "payload too large"},
                            separators=(",", ":"),
                        ),
                        content_type="application/json",
                        status=413,
                    )
                    return response
                data = raw_bytes.decode("utf-8", errors="replace")
            else:
                data = await request.text()
                if len(data.encode("utf-8")) > MAX_OTA_BODY_BYTES:
                    response = web.Response(
                        text=json.dumps(
                            {"success": False, "message": "payload too large"},
                            separators=(",", ":"),
                        ),
                        content_type="application/json",
                        status=413,
                    )
                    return response

            device_id = request.headers.get("device-id", "").strip()
            client_id = request.headers.get("client-id", "").strip()
            if not _valid_header_id(device_id) or not _valid_header_id(client_id):
                response = web.Response(
                    text=json.dumps(
                        {"success": False, "message": "missing or invalid device headers"},
                        separators=(",", ":"),
                    ),
                    content_type="application/json",
                    status=400,
                )
                return response

            self.logger.bind(tag=TAG).info(
                f"OTA请求设备ID: {device_id}, ClientID: {client_id}"
            )

            data_json = {}
            try:
                parsed = json.loads(data) if data else {}
                if isinstance(parsed, dict):
                    data_json = parsed
            except Exception:
                data_json = {}

            server_config = self.config["server"]
            websocket_port = int(server_config.get("port", 8000))
            local_ip = get_local_ip()

            device_model = ""
            for h in ("device-model", "device_model", "model"):
                if h in request.headers:
                    device_model = request.headers.get(h, "").strip()
                    break
            if not device_model:
                try:
                    if "board" in data_json and isinstance(data_json["board"], dict):
                        device_model = str(data_json["board"].get("type", "") or "")
                    elif "model" in data_json:
                        device_model = str(data_json.get("model", "") or "")
                except Exception:
                    device_model = ""
            if not device_model or len(device_model) > 128:
                device_model = "default"

            device_version = ""
            for h in (
                "device-version",
                "device_version",
                "firmware-version",
                "app-version",
                "application-version",
            ):
                if h in request.headers:
                    device_version = request.headers.get(h, "").strip()
                    break
            if not device_version:
                try:
                    app_info = data_json.get("application", {})
                    if isinstance(app_info, dict):
                        device_version = str(app_info.get("version", "") or "")
                except Exception:
                    device_version = ""
            if not device_version or len(device_version) > 64:
                device_version = "0.0.0"

            tz_cfg = server_config.get("timezone_offset")
            if tz_cfg is None or str(tz_cfg).strip().lower() in ("", "auto"):
                try:
                    import datetime as _dt
                    utc_off = _dt.datetime.now().astimezone().utcoffset()
                    tz_offset_min = int(utc_off.total_seconds() // 60) if utc_off is not None else 8 * 60
                except Exception:
                    tz_offset_min = 8 * 60
            else:
                try:
                    tz_offset_min = int(float(tz_cfg) * 60)
                except Exception:
                    tz_offset_min = 8 * 60

            return_json = {
                "server_time": {
                    "timestamp": int(round(time.time() * 1000)),
                    "timezone_offset": tz_offset_min,
                },
                "firmware": {
                    "version": device_version,
                    "url": "",
                },
            }

            mqtt_gateway_endpoint = server_config.get("mqtt_gateway")

            if mqtt_gateway_endpoint:
                try:
                    group_id = f"GID_{device_model}".replace(":", "_").replace(" ", "_")
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"获取设备型号失败: {e}")
                    group_id = "GID_default"

                mac_address_safe = device_id.replace(":", "_")
                mqtt_client_id = f"{group_id}@@@{mac_address_safe}@@@{mac_address_safe}"

                user_data = {"ip": "unknown"}
                try:
                    user_data_json = json.dumps(user_data)
                    username = base64.b64encode(user_data_json.encode("utf-8")).decode(
                        "utf-8"
                    )
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"生成用户名失败: {e}")
                    username = ""

                password = ""
                signature_key = server_config.get("mqtt_signature_key", "")
                if signature_key:
                    password = self.generate_password_signature(
                        mqtt_client_id + "|" + username, signature_key
                    )
                    if not password:
                        password = ""
                else:
                    self.logger.bind(tag=TAG).warning("缺少MQTT签名密钥，密码留空")

                return_json["mqtt"] = {
                    "endpoint": mqtt_gateway_endpoint,
                    "client_id": mqtt_client_id,
                    "username": username,
                    "password": password,
                    "publish_topic": "device-server",
                    "subscribe_topic": f"devices/p2p/{mac_address_safe}",
                }
                self.logger.bind(tag=TAG).info(f"为设备 {device_id} 下发MQTT网关配置")

            else:
                token = ""
                if self.auth_enable:
                    if self.allowed_devices:
                        if device_id not in self.allowed_devices:
                            token = self.auth.generate_token(client_id, device_id)
                    else:
                        token = self.auth.generate_token(client_id, device_id)
                return_json["websocket"] = {
                    "url": self._get_websocket_url(local_ip, websocket_port),
                    "token": token,
                }
                self.logger.bind(tag=TAG).info(
                    f"未配置MQTT网关，为设备 {device_id} 下发WebSocket配置"
                )

            try:
                self._refresh_bin_cache_if_needed()
                files_by_model = self._bin_cache.get("files_by_model", {})
                candidates = files_by_model.get(device_model, [])

                chosen_url = ""
                chosen_version = device_version

                for ver, fname in candidates:
                    if _is_higher_version(ver, device_version):
                        chosen_version = ver
                        vision_url = get_vision_url(self.config)
                        chosen_url = vision_url.replace(
                            "/mcp/vision/explain", f"/xiaozhi/ota/download/{fname}"
                        )
                        break

                if chosen_url:
                    return_json["firmware"]["version"] = chosen_version
                    return_json["firmware"]["url"] = chosen_url
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"检查固件版本时出错: {e}")

            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"OTA POST处理异常: {type(e).__name__}")
            return_json = {"success": False, "message": "request error."}
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
                status=500,
            )
        finally:
            if response is not None:
                self._add_cors_headers(response)
            return response

    async def handle_get(self, request):
        """处理 OTA GET 请求"""
        response = None
        try:
            server_config = self.config["server"]
            local_ip = get_local_ip()
            websocket_port = int(server_config.get("port", 8000))
            websocket_url = self._get_websocket_url(local_ip, websocket_port)
            message = f"OTA接口运行正常，向设备发送的websocket地址是：{websocket_url}"
            response = web.Response(text=message, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"OTA GET请求异常: {type(e).__name__}")
            response = web.Response(
                text="OTA接口异常", content_type="text/plain", status=500
            )
        finally:
            if response is not None:
                self._add_cors_headers(response)
            return response

    async def handle_download(self, request):
        """
        下载固件接口
        URL: /xiaozhi/ota/download/{filename}
        - 只允许下载 data/bin 目录下的非符号链接普通 .bin 文件
        """
        resp = None
        try:
            fname = request.match_info.get("filename", "")
            if not fname:
                raise web.HTTPBadRequest(text="filename required")

            fname = _safe_basename(fname)
            if not re.match(r"^[A-Za-z0-9\.\-_]+\.bin$", fname):
                raise web.HTTPBadRequest(text="invalid filename")

            file_path = os.path.join(self.bin_dir, fname)
            if os.path.islink(file_path):
                raise web.HTTPForbidden(text="forbidden")
            file_real = os.path.realpath(file_path)
            bin_dir_real = os.path.realpath(self.bin_dir)
            if (
                not file_real.startswith(bin_dir_real + os.sep)
                and file_real != bin_dir_real
            ):
                raise web.HTTPForbidden(text="forbidden")

            try:
                st = os.lstat(file_real)
            except OSError:
                raise web.HTTPNotFound(text="file not found")
            if not stat.S_ISREG(st.st_mode):
                raise web.HTTPForbidden(text="forbidden")

            resp = web.FileResponse(path=file_real)
        except web.HTTPError as e:
            resp = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"固件下载异常: {type(e).__name__}")
            resp = web.Response(text="download error", status=500)
        finally:
            try:
                if resp is not None:
                    self._add_cors_headers(resp)
            except Exception:
                pass
            return resp
