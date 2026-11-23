import json
import urllib.parse
import httpx
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()
MAX_RAGFLOW_BYTES = 1024 * 1024
MAX_QUESTION_CHARS = 500
MAX_CHUNK_CHARS = 2000

# 定义基础的函数描述模板
SEARCH_FROM_RAGFLOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "search_from_ragflow",
        "description": "从知识库中查询信息",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "查询的问题"}},
            "required": ["question"],
        },
    },
}


def _valid_ragflow_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        return ""
    cleaned = base_url.strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")) or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in cleaned):
        return ""
    try:
        parts = urllib.parse.urlsplit(cleaned)
        _ = parts.port
    except ValueError:
        return ""
    if not parts.hostname or parts.username is not None or parts.password is not None:
        return ""
    return cleaned


@register_function(
    "search_from_ragflow", SEARCH_FROM_RAGFLOW_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def search_from_ragflow(conn: "ConnectionHandler", question=None):
    question = (str(question) if question is not None else "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return ActionResponse(Action.RESPONSE, None, "请提供要查询的问题。")

    plugins_cfg = (conn.config or {}).get("plugins") or {}
    ragflow_config = plugins_cfg.get("search_from_ragflow") or {}
    if not isinstance(ragflow_config, dict):
        ragflow_config = {}
    base_url = _valid_ragflow_base_url(ragflow_config.get("base_url", ""))
    api_key = "".join(ch for ch in str(ragflow_config.get("api_key") or "").strip() if ord(ch) > 0x20 and ord(ch) != 0x7F)
    dataset_ids = ragflow_config.get("dataset_ids") or []
    verify_ssl = bool(ragflow_config.get("verify_ssl", True))

    if not base_url or not api_key:
        return ActionResponse(Action.RESPONSE, None, "知识库接口尚未配置完整，请在后台检查地址与密钥。")

    url = base_url + "/api/v1/retrieval"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"question": question, "dataset_ids": dataset_ids if isinstance(dataset_ids, list) else []}

    try:
        raw_buf = bytearray()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=3.0),
            verify=verify_ssl,
            follow_redirects=False,
        ) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if len(raw_buf) + len(chunk) > MAX_RAGFLOW_BYTES:
                        raise ValueError("RAGFlow response exceeded size limit")
                    raw_buf.extend(chunk)

        result = json.loads(raw_buf.decode("utf-8", errors="replace"))
        if not isinstance(result, dict):
            return ActionResponse(Action.RESPONSE, None, "RAG接口返回数据格式异常")

        if result.get("code") != 0:
            err_obj = result.get("error") if isinstance(result.get("error"), dict) else {}
            error_detail = str(err_obj.get("detail") or "未知错误")[:200]
            error_message = str(err_obj.get("message") or "")[:200]
            error_code = result.get("code", "")

            logger.bind(tag=TAG).error(
                f"RAGFlow API调用失败，响应码：{error_code}，错误详情：{error_detail}"
            )

            error_response = f"RAG接口返回异常（错误码：{error_code}）"
            if error_message:
                error_response += f"：{error_message}"
            if error_detail:
                error_response += f"\n详情：{error_detail}"
            return ActionResponse(Action.RESPONSE, None, error_response)

        data_obj = result.get("data") if isinstance(result.get("data"), dict) else {}
        chunks = data_obj.get("chunks") if isinstance(data_obj.get("chunks"), list) else []
        contents = []
        for chunk in chunks[:5]:
            if not isinstance(chunk, dict):
                continue
            content = chunk.get("content", "")
            if content:
                if isinstance(content, str):
                    text_part = content
                elif isinstance(content, bytes):
                    text_part = content.decode("utf-8", errors="replace")
                else:
                    text_part = str(content)
                contents.append(text_part[:MAX_CHUNK_CHARS])

        if contents:
            context_text = f"# 关于问题【{question}】查到知识库如下\n"
            context_text += "```\n\n\n".join(contents[:5])
            context_text += "\n```"
        else:
            context_text = "根据知识库查询结果，没有相关信息。"
        return ActionResponse(Action.REQLLM, context_text, None)

    except httpx.TimeoutException:
        return ActionResponse(
            Action.RESPONSE,
            None,
            "RAG接口请求超时\n可能原因：RAGflow服务响应缓慢或网络延迟\n解决方案：请稍后重试或检查RAGflow服务性能",
        )

    except httpx.HTTPStatusError as e:
        status_code = getattr(e.response, "status_code", "未知")
        return ActionResponse(Action.RESPONSE, None, f"RAG接口HTTP错误（状态码：{status_code}）")

    except httpx.HTTPError:
        return ActionResponse(
            Action.RESPONSE,
            None,
            "无法连接到RAG接口\n可能原因：RAGflow服务地址错误或服务未运行\n解决方案：请检查RAGflow服务地址配置和服务状态",
        )

    except Exception as e:
        error_type = type(e).__name__
        logger.bind(tag=TAG).error(
            f"RAGflow处理异常，异常类型：{error_type}，详情：{str(e)}"
        )
        return ActionResponse(Action.RESPONSE, None, f"RAG接口处理异常（{error_type}）")
