"""设备端MCP客户端支持模块"""

import json
import asyncio
import re
from core.utils.util import sanitize_tool_name
from config.logger import setup_logging
from typing import TYPE_CHECKING
from .mcp_client import MCPClient

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


async def send_mcp_message(conn: "ConnectionHandler", payload: dict) -> bool:
    """Helper to send MCP messages, encapsulating common logic."""
    if not (getattr(conn, "features", None) or {}).get("mcp"):
        logger.bind(tag=TAG).warning("客户端不支持MCP，无法发送MCP消息")
        return False
    if (getattr(conn, "stop_event", None) and conn.stop_event.is_set()) or not getattr(conn, "websocket", None):
        return False

    message = json.dumps({"type": "mcp", "payload": payload})

    try:
        await conn.websocket.send(message)
        logger.bind(tag=TAG).debug(f"成功发送MCP消息: {message}")
        return True
    except Exception as e:
        if getattr(conn, "stop_event", None) and conn.stop_event.is_set():
            return False
        logger.bind(tag=TAG).error(f"发送MCP消息失败: {e}")
        return False


def _safe_msg_id(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


async def handle_mcp_message(
    conn: "ConnectionHandler", mcp_client: MCPClient, payload: dict
):
    """处理MCP消息,包括初始化、工具列表和工具调用响应等"""
    logger.bind(tag=TAG).debug(f"处理MCP消息: {str(payload)[:100]}")

    if not isinstance(payload, dict):
        logger.bind(tag=TAG).error("MCP消息缺少payload字段或格式错误")
        return

    # Handle result
    if "result" in payload:
        result = payload["result"]
        msg_id = _safe_msg_id(payload.get("id", 0))

        # Check for tool call response first
        if msg_id in mcp_client.call_results:
            logger.bind(tag=TAG).debug(
                f"收到工具调用响应，ID: {msg_id}, 结果: {result}"
            )
            await mcp_client.resolve_call_result(msg_id, result)
            return

        if msg_id == 1:  # mcpInitializeID
            logger.bind(tag=TAG).debug("收到MCP初始化响应")
            server_info = result.get("serverInfo") if isinstance(result, dict) else None
            if isinstance(server_info, dict):
                name = server_info.get("name")
                version = server_info.get("version")
                logger.bind(tag=TAG).debug(
                    f"客户端MCP服务器信息: name={name}, version={version}"
                )

            async def _delayed_req_tools():
                await asyncio.sleep(0)
                if getattr(conn, "stop_event", None) and conn.stop_event.is_set():
                    return
                logger.bind(tag=TAG).debug("初始化完成，开始请求MCP工具列表")
                await send_mcp_tools_list_request(conn)

            asyncio.create_task(_delayed_req_tools())
            return

        elif msg_id == 2:  # mcpToolsListID
            logger.bind(tag=TAG).debug("收到MCP工具列表响应")
            if isinstance(result, dict) and "tools" in result:
                tools_data = result["tools"]
                if not isinstance(tools_data, list):
                    logger.bind(tag=TAG).error("工具列表格式错误")
                    return

                logger.bind(tag=TAG).info(
                    f"客户端设备支持的工具数量: {len(tools_data)}"
                )

                for i, tool in enumerate(tools_data):
                    if not isinstance(tool, dict):
                        continue

                    raw_name = tool.get("name")
                    name = raw_name.strip() if isinstance(raw_name, str) else ""
                    if not name:
                        continue
                    raw_desc = tool.get("description")
                    description = raw_desc if isinstance(raw_desc, str) else ""
                    input_schema = {"type": "object", "properties": {}, "required": []}

                    if "inputSchema" in tool and isinstance(tool["inputSchema"], dict):
                        schema = tool["inputSchema"]
                        input_schema["type"] = schema.get("type") or "object"
                        input_schema["properties"] = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
                        input_schema["required"] = [
                            s for s in (schema.get("required") or []) if isinstance(s, str)
                        ]

                    new_tool = {
                        "name": name,
                        "description": description,
                        "_raw_description": description,
                        "inputSchema": input_schema,
                    }
                    await mcp_client.add_tool(new_tool)
                    logger.bind(tag=TAG).debug(f"客户端工具 #{i+1}: {name}")

                # 替换所有工具描述中的工具名称（按原始名称长度降序，基于原始描述幂等替换避免前缀互踩）
                ordered_map = sorted(
                    ((sn, on) for sn, on in mcp_client.name_mapping.items() if on),
                    key=lambda kv: len(kv[1]),
                    reverse=True,
                )
                for tool_data in mcp_client.tools.values():
                    raw_d = tool_data.get("_raw_description")
                    if not isinstance(raw_d, str):
                        raw_d = tool_data.get("description")
                        if isinstance(raw_d, str):
                            tool_data["_raw_description"] = raw_d
                    if isinstance(raw_d, str) and raw_d:
                        desc_out = raw_d
                        for sanitized_name, original_name in ordered_map:
                            desc_out = desc_out.replace(original_name, sanitized_name)
                        tool_data["description"] = desc_out

                next_cursor = result.get("nextCursor", "")
                pages = getattr(mcp_client, "_cursor_pages", 0)
                if next_cursor and pages < 16:
                    mcp_client._cursor_pages = pages + 1
                    logger.bind(tag=TAG).debug(f"有更多工具，nextCursor: {next_cursor}")
                    await send_mcp_tools_list_continue_request(conn, next_cursor)
                else:
                    mcp_client._cursor_pages = 0
                    await mcp_client.set_ready(True)
                    logger.bind(tag=TAG).debug("所有工具已获取，MCP客户端准备就绪")

                    # 刷新工具缓存，确保MCP工具被包含在函数列表中
                    if hasattr(conn, "func_handler") and conn.func_handler:
                        conn.func_handler.tool_manager.refresh_tools()
                        conn.func_handler.current_support_functions()
            return

    # Handle method calls (requests from the client)
    elif "method" in payload:
        method = payload["method"]
        logger.bind(tag=TAG).info(f"收到MCP客户端请求: {method}")

    elif "error" in payload:
        error_data = payload["error"]
        error_msg = error_data.get("message", "未知错误") if isinstance(error_data, dict) else str(error_data or "未知错误")
        logger.bind(tag=TAG).error(f"收到MCP错误响应: {error_msg}")

        msg_id = _safe_msg_id(payload.get("id", 0))
        if msg_id in mcp_client.call_results:
            await mcp_client.reject_call_result(
                msg_id, Exception(f"MCP错误: {error_msg}")
            )


async def send_mcp_initialize_message(conn: "ConnectionHandler"):
    """发送MCP初始化消息"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,  # mcpInitializeID
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
            },
            "clientInfo": {
                "name": "XiaozhiClient",
                "version": "1.0.0",
            },
        },
    }
    logger.bind(tag=TAG).debug("发送MCP初始化消息")
    await send_mcp_message(conn, payload)


async def send_mcp_tools_list_request(conn: "ConnectionHandler"):
    """发送MCP工具列表请求"""
    mcp_client = getattr(conn, "mcp_client", None)
    if mcp_client is not None:
        mcp_client._cursor_pages = 0
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID
        "method": "tools/list",
    }
    logger.bind(tag=TAG).debug("发送MCP工具列表请求")
    await send_mcp_message(conn, payload)


async def send_mcp_tools_list_continue_request(conn: "ConnectionHandler", cursor: str):
    """发送带有cursor的MCP工具列表请求"""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID (same ID for continuation)
        "method": "tools/list",
        "params": {"cursor": cursor},
    }
    logger.bind(tag=TAG).info(f"发送带cursor的MCP工具列表请求: {cursor}")
    await send_mcp_message(conn, payload)


async def call_mcp_tool(
    conn: "ConnectionHandler",
    mcp_client: MCPClient,
    tool_name: str,
    args: str = "{}",
    timeout: int = 30,
):
    """
    调用指定的工具，并等待响应
    """
    if not await mcp_client.is_ready():
        raise RuntimeError("MCP客户端尚未准备就绪")

    if not mcp_client.has_tool(tool_name):
        raise ValueError(f"工具 {tool_name} 不存在")

    # 处理参数（在分配 ID 和注册 Future 之前先校验，避免异常时泄漏 Future）
    try:
        if isinstance(args, str):
            # 确保字符串是有效的JSON
            if not args.strip():
                arguments = {}
            else:
                try:
                    # 尝试直接解析
                    arguments = json.loads(args)
                except json.JSONDecodeError:
                    # 如果解析失败，尝试合并多个JSON对象
                    try:
                        # 使用正则表达式匹配所有JSON对象
                        json_objects = re.findall(r"\{[^{}]*\}", args)
                        if len(json_objects) > 1:
                            # 合并所有JSON对象
                            merged_dict = {}
                            for json_str in json_objects:
                                try:
                                    obj = json.loads(json_str)
                                    if isinstance(obj, dict):
                                        merged_dict.update(obj)
                                except json.JSONDecodeError:
                                    continue
                            if merged_dict:
                                arguments = merged_dict
                            else:
                                raise ValueError(f"无法解析任何有效的JSON对象: {args}")
                        else:
                            raise ValueError(f"参数JSON解析失败: {args}")
                    except Exception as e:
                        logger.bind(tag=TAG).error(
                            f"参数JSON解析失败: {str(e)}, 原始参数: {args}"
                        )
                        raise ValueError(f"参数JSON解析失败: {str(e)}")
        elif isinstance(args, dict):
            arguments = args
        else:
            raise ValueError(f"参数类型错误，期望字符串或字典，实际类型: {type(args)}")

        # 确保参数是字典类型
        if not isinstance(arguments, dict):
            raise ValueError(f"参数必须是字典类型，实际类型: {type(arguments)}")

    except Exception as e:
        if not isinstance(e, ValueError):
            raise ValueError(f"参数处理失败: {str(e)}")
        raise e

    if (getattr(conn, "stop_event", None) and conn.stop_event.is_set()) or not getattr(conn, "websocket", None):
        raise ConnectionError("MCP连接已关闭")

    tool_call_id = await mcp_client.get_next_id()
    result_future = asyncio.Future()
    await mcp_client.register_call_result_future(tool_call_id, result_future)

    resolved_name = tool_name if tool_name in mcp_client.tools else sanitize_tool_name(tool_name)
    actual_name = mcp_client.name_mapping.get(resolved_name, tool_name)
    payload = {
        "jsonrpc": "2.0",
        "id": tool_call_id,
        "method": "tools/call",
        "params": {"name": actual_name, "arguments": arguments},
    }

    logger.bind(tag=TAG).info(f"发送客户端mcp工具调用请求: {actual_name}，参数: {args}")
    try:
        if not await send_mcp_message(conn, payload):
            await mcp_client.cleanup_call_result(tool_call_id)
            raise ConnectionError("MCP消息发送失败")
        # Wait for response or timeout
        raw_result = await asyncio.wait_for(result_future, timeout=timeout)
        logger.bind(tag=TAG).info(
            f"客户端mcp工具调用 {actual_name} 成功，原始结果: {raw_result}"
        )

        if isinstance(raw_result, dict):
            if raw_result.get("isError") is True:
                err_txt = raw_result.get("error")
                if not err_txt:
                    c_list = raw_result.get("content")
                    if isinstance(c_list, list) and c_list and isinstance(c_list[0], dict):
                        err_txt = c_list[0].get("text")
                error_msg = err_txt or "工具调用返回错误，但未提供具体错误信息"
                raise RuntimeError(f"工具调用错误: {error_msg}")

            content = raw_result.get("content")
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    # 直接返回文本内容，不进行JSON解析
                    return content[0]["text"]
        # 如果结果不是预期的格式，将其转换为字符串
        return str(raw_result)
    except asyncio.TimeoutError:
        raise TimeoutError("工具调用请求超时")
    finally:
        await mcp_client.cleanup_call_result(tool_call_id)
