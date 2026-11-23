import asyncio
import hmac
import json
from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.providers.tools.device_mcp import handle_mcp_message

TAG = __name__


class ServerTextMessageHandler(TextMessageHandler):
    """MCP消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.SERVER

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        # 如果配置是从API读取的，则需要验证secret
        if not conn.read_config_from_api:
            return
        content = msg_json.get("content")
        if not isinstance(content, dict):
            content = {}
        manager_cfg = conn.config.get("manager-api")
        if not isinstance(manager_cfg, dict):
            manager_cfg = {}
        post_secret = content.get("secret", "")
        secret = manager_cfg.get("secret", "")
        # secret 必须非空且使用常量时间比较
        if (
            not isinstance(secret, str)
            or not secret.strip()
            or not isinstance(post_secret, str)
            or not hmac.compare_digest(post_secret.encode("utf-8"), secret.encode("utf-8"))
        ):
            await conn.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "error",
                        "message": "服务器密钥验证失败",
                    }
                )
            )
            return
        action = msg_json.get("action")
        # 动态更新配置
        if action == "update_config":
            try:
                # 更新WebSocketServer的配置
                if not conn.server:
                    await conn.websocket.send(
                        json.dumps(
                            {
                                "type": "server",
                                "status": "error",
                                "message": "无法获取服务器实例",
                                "content": {"action": "update_config"},
                            }
                        )
                    )
                    return

                if not await conn.server.update_config():
                    await conn.websocket.send(
                        json.dumps(
                            {
                                "type": "server",
                                "status": "error",
                                "message": "更新服务器配置失败",
                                "content": {"action": "update_config"},
                            }
                        )
                    )
                    return

                # 发送成功响应
                await conn.websocket.send(
                    json.dumps(
                        {
                            "type": "server",
                            "status": "success",
                            "message": "配置更新成功",
                            "content": {"action": "update_config"},
                        }
                    )
                )
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"更新配置失败: {str(e)}")
                await conn.websocket.send(
                    json.dumps(
                        {
                            "type": "server",
                            "status": "error",
                            "message": "更新服务器配置失败",
                            "content": {"action": "update_config"},
                        }
                    )
                )
        # 重启服务器
        elif action == "restart":
            await conn.handle_restart(msg_json)
