import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.providers.tools.device_mcp import handle_mcp_message


class McpTextMessageHandler(TextMessageHandler):
    """MCP消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.MCP

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        payload = msg_json.get("payload") if isinstance(msg_json, dict) else None
        mcp_client = getattr(conn, "mcp_client", None)
        if isinstance(payload, dict) and mcp_client is not None:
            await handle_mcp_message(conn, mcp_client, payload)
