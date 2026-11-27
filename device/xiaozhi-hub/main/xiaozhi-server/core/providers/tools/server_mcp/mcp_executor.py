"""服务端MCP工具执行器（data/.mcp_server_settings.json 为空时延迟跳过 mcp SDK 导入，节省内存与首连耗时）"""

import json
import os
from typing import Dict, Any, Optional
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import Action, ActionResponse
from config.config_loader import get_project_dir


class ServerMCPExecutor(ToolExecutor):
    """服务端MCP工具执行器"""

    def __init__(self, conn):
        self.conn = conn
        self.mcp_manager: Optional[Any] = None
        self._initialized = False

    @staticmethod
    def _has_configured_servers() -> bool:
        cfg_path = os.path.join(get_project_dir(), "data", ".mcp_server_settings.json")
        if not os.path.isfile(cfg_path):
            return False
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            if not isinstance(servers, dict) or not servers:
                return False
            return any(isinstance(v, dict) and (v.get("command") or v.get("url")) for v in servers.values())
        except Exception:
            return False

    async def initialize(self):
        """仅在配置了至少一个有效外部 MCP 服务时才导入 ServerMCPManager（避免无谓加载 mcp/pydantic/starlette）。"""
        if not self._initialized:
            self._initialized = True
            if not self._has_configured_servers():
                return
            from .mcp_manager import ServerMCPManager

            self.mcp_manager = ServerMCPManager(self.conn)
            await self.mcp_manager.initialize_servers()

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行服务端MCP工具"""
        if not self._initialized or not self.mcp_manager:
            return ActionResponse(
                action=Action.ERROR,
                response="MCP管理器未初始化",
            )

        try:
            actual_tool_name = (
                tool_name
                if self.mcp_manager.is_mcp_tool(tool_name)
                else (tool_name[4:] if tool_name.startswith("mcp_") else tool_name)
            )
            result = await self.mcp_manager.execute_tool(actual_tool_name, arguments)
            return ActionResponse(action=Action.REQLLM, result=str(result))
        except ValueError as e:
            return ActionResponse(action=Action.NOTFOUND, response=str(e))
        except Exception as e:
            return ActionResponse(action=Action.ERROR, response=str(e))

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有服务端MCP工具"""
        if not self._initialized or not self.mcp_manager:
            return {}

        tools = {}
        for tool in self.mcp_manager.get_all_tools():
            func_def = tool.get("function", {})
            tool_name = func_def.get("name", "")
            if tool_name:
                tools[tool_name] = ToolDefinition(
                    name=tool_name, description=tool, tool_type=ToolType.SERVER_MCP
                )
        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的服务端MCP工具"""
        if not self._initialized or not self.mcp_manager:
            return False
        if self.mcp_manager.is_mcp_tool(tool_name):
            return True
        actual_tool_name = tool_name[4:] if tool_name.startswith("mcp_") else tool_name
        return self.mcp_manager.is_mcp_tool(actual_tool_name)

    async def cleanup(self):
        """清理MCP连接"""
        if self.mcp_manager:
            await self.mcp_manager.cleanup_all()
