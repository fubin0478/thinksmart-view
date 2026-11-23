import asyncio
import base64
import copy
import hmac
import ipaddress
import json
from typing import Optional, Tuple
from urllib.parse import urlsplit

from aiohttp import web
from config.config_loader import get_private_config_from_api
from config.logger import setup_logging
from core.api.base_handler import BaseHandler
from core.utils.auth import AuthToken
from core.utils.util import get_vision_url, is_valid_image_file
from core.utils.vllm import create_instance
from plugins_func.register import Action

TAG = __name__

# 设置最大文件大小为5MB
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_QUESTION_BYTES = 16 * 1024
MAX_MULTIPART_PARTS = 8


class _VisionRequestError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


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


async def _read_part_bounded(part, max_bytes: int) -> Tuple[bytes, bool]:
    buf = bytearray()
    while True:
        chunk = await part.read_chunk(65536)
        if not chunk:
            return bytes(buf), False
        buf.extend(chunk)
        if len(buf) > max_bytes:
            return b"", True


class VisionHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)
        # 初始化认证工具
        self.auth = AuthToken(config["server"]["auth_key"])

    def _create_error_response(self, message: str) -> dict:
        """创建统一的错误响应格式"""
        return {"success": False, "message": message}

    def _verify_auth_token(self, request) -> Tuple[bool, Optional[str]]:
        """验证认证token（已移除 web_test_client 免认证绕过）"""
        auth_header = request.headers.get("Authorization", "")
        device_id = request.headers.get("Device-Id", "").strip()
        client_id = request.headers.get("Client-Id", "").strip()
        if (
            not device_id
            or len(device_id) > 128
            or any(c in device_id for c in ("\r", "\n"))
            or not client_id
            or len(client_id) > 128
            or any(c in client_id for c in ("\r", "\n"))
        ):
            return False, None
        if not auth_header.startswith("Bearer "):
            return False, None

        token = auth_header[7:].strip()
        if not token or len(token) > 4096:
            return False, None
        is_valid, token_device_id = self.auth.verify_token(token)
        if (
            not is_valid
            or not isinstance(token_device_id, str)
            or not token_device_id
            or not hmac.compare_digest(device_id, token_device_id)
        ):
            return False, None
        return True, token_device_id

    async def handle_post(self, request):
        """处理 MCP Vision POST 请求"""
        response = None
        try:
            orig = request.headers.get("Origin")
            if orig is not None and not _is_trusted_local_origin(
                orig, request.headers.get("Host", "")
            ):
                response = web.Response(
                    text=json.dumps(
                        self._create_error_response("禁止跨站请求"),
                        separators=(",", ":"),
                    ),
                    content_type="application/json",
                    status=403,
                )
                return response

            # 验证token与Device-Id绑定
            is_valid, _ = self._verify_auth_token(request)
            if not is_valid:
                response = web.Response(
                    text=json.dumps(
                        self._create_error_response("无效的认证token或token已过期"),
                        separators=(",", ":"),
                    ),
                    content_type="application/json",
                    status=401,
                )
                return response

            device_id = request.headers.get("Device-Id", "").strip()
            client_id = request.headers.get("Client-Id", "").strip()

            ctype = (request.headers.get("Content-Type") or "").lower()
            if not ctype.startswith("multipart/"):
                raise _VisionRequestError("需要 multipart/form-data 请求", status=415)

            reader = await request.multipart()
            question: Optional[str] = None
            image_data: Optional[bytes] = None
            parts_seen = 0

            while True:
                field = await reader.next()
                if field is None:
                    break
                parts_seen += 1
                if parts_seen > MAX_MULTIPART_PARTS:
                    raise _VisionRequestError("表单字段数量超出限制", status=400)
                fname = (getattr(field, "name", None) or "").strip().lower()
                has_filename = bool(getattr(field, "filename", None))
                if fname == "question" or (
                    question is None and not has_filename and fname not in ("image", "file")
                ):
                    raw_q, too_big = await _read_part_bounded(field, MAX_QUESTION_BYTES)
                    if too_big:
                        raise _VisionRequestError("问题文本超过最大长度限制", status=413)
                    try:
                        question = raw_q.decode("utf-8").strip()
                    except UnicodeDecodeError as exc:
                        raise _VisionRequestError("问题文本编码无效", status=400) from exc
                elif fname in ("image", "file") or image_data is None:
                    raw_img, too_big = await _read_part_bounded(field, MAX_FILE_SIZE)
                    if too_big:
                        raise _VisionRequestError(
                            f"图片大小超过限制，最大允许{MAX_FILE_SIZE // (1024 * 1024)}MB",
                            status=413,
                        )
                    image_data = raw_img
                else:
                    # 丢弃未知小字段并限制体积
                    _, too_big = await _read_part_bounded(field, 4096)
                    if too_big:
                        raise _VisionRequestError("未知表单字段过大", status=413)

            if not question:
                raise _VisionRequestError("缺少问题字段", status=400)
            if not image_data:
                raise _VisionRequestError("缺少图片文件或图片数据为空", status=400)
            if not is_valid_image_file(image_data):
                raise _VisionRequestError(
                    "不支持的文件格式，请上传有效的图片文件（支持JPEG、PNG、GIF、BMP、TIFF、WEBP格式）",
                    status=400,
                )

            image_base64 = base64.b64encode(image_data).decode("utf-8")

            # 如果开启了智控台，则从智控台获取模型配置
            current_config = copy.deepcopy(self.config)
            read_config_from_api = current_config.get("read_config_from_api", False)
            if read_config_from_api:
                current_config = await get_private_config_from_api(
                    current_config,
                    device_id,
                    client_id,
                )

            select_vllm_module = (current_config.get("selected_module") or {}).get("VLLM")
            if not select_vllm_module or select_vllm_module not in (
                current_config.get("VLLM") or {}
            ):
                raise _VisionRequestError("您还未设置默认的视觉分析模块", status=503)

            vllm_cfg = current_config["VLLM"][select_vllm_module]
            vllm_type = (
                select_vllm_module if "type" not in vllm_cfg else vllm_cfg["type"]
            )
            if not vllm_type:
                raise _VisionRequestError("无法找到视觉分析模块供应器", status=503)

            vllm = create_instance(vllm_type, vllm_cfg)
            result = await asyncio.to_thread(vllm.response, question, image_base64)

            return_json = {
                "success": True,
                "action": Action.RESPONSE.name,
                "response": result,
            }
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        except _VisionRequestError as e:
            self.logger.bind(tag=TAG).warning(f"MCP Vision 请求被拒绝({e.status}): {e.message}")
            return_json = self._create_error_response(e.message)
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
                status=e.status,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"MCP Vision POST请求异常: {type(e).__name__}")
            return_json = self._create_error_response("处理请求时发生错误")
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
        """处理 MCP Vision GET 请求"""
        response = None
        try:
            vision_explain = get_vision_url(self.config)
            if vision_explain and len(vision_explain) > 0 and "null" != vision_explain:
                message = (
                    f"MCP Vision 接口运行正常，视觉解释接口地址是：{vision_explain}"
                )
            else:
                message = "MCP Vision 接口未配置视觉解释地址"
            response = web.Response(text=message, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"MCP Vision GET请求异常: {type(e).__name__}")
            return_json = self._create_error_response("服务器内部错误")
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
                status=500,
            )
        finally:
            if response is not None:
                self._add_cors_headers(response)
            return response
