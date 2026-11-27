"""Token-protected public entrance to the local voice models (ASR, TTS, translation) and study-vision.

The router forwards an external port to this process; it relays only the model routes of the
8010 voice service and never the /upstream, /local-llm, or /slm relays. The token goes in the path,
because xiaozhi providers can only set a URL for some of these:

    http://<host>:<port>/<token>/tts
    http://<host>:<port>/<token>/v1/audio/transcriptions
    http://<host>:<port>/<token>/translation/v1/chat/completions
    http://<host>:<port>/<token>/study/v1     (ThinkSmart View study monitor -> study-vision bridge;
                                               the body is sealed end to end, see study-vision/vision_bridge.py)

GET /health stays open and only returns {"status":"ok"} so upstream health probes succeed without
exposing internal state.

Config: ~/.config/thinksmart-voice/public_gateway.json  {"port": 8080, "token": "..."}
"""
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

CONFIG_PATH = Path(
    os.path.expanduser(os.environ.get("GATEWAY_CONFIG_FILE", "~/.config/thinksmart-voice/public_gateway.json"))
)
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
TOKEN = CONFIG["token"]
UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "http://127.0.0.1:8010")
STUDY_UPSTREAM = os.environ.get("GATEWAY_STUDY_UPSTREAM", "http://127.0.0.1:8084")
ALLOWED = (
    ("POST", "tts"),
    ("POST", "v1/audio/speech"),
    ("POST", "v1/audio/transcriptions"),
    ("GET", "v1/models"),
    ("GET", "translation/v1/"),
    ("POST", "translation/v1/"),
    ("POST", "study/v1"),
)
HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "authorization"}

MAX_BODY_BYTES = 16 * 1024 * 1024
logger = logging.getLogger("xiaozhi-gateway")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
client = httpx.AsyncClient(
    timeout=httpx.Timeout(120.0, connect=3.0),
    limits=httpx.Limits(max_keepalive_connections=32, max_connections=64, keepalive_expiry=300.0),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def _allowed(method: str, path: str) -> bool:
    return any(method == m and (path == p or (p.endswith("/") and path.startswith(p))) for m, p in ALLOWED)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.api_route("/{token}/{path:path}", methods=["GET", "POST"])
async def relay(token: str, path: str, request: Request):
    client_ip = request.client.host if request.client else "?"
    if not hmac.compare_digest(token, TOKEN):
        logger.warning("rejected bad token from %s for /%s", client_ip, path)
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _allowed(request.method, path):
        logger.warning("rejected %s /%s from %s (not a model route)", request.method, path, client_ip)
        return JSONResponse({"error": "not found"}, status_code=404)

    cl_hdr = request.headers.get("content-length")
    if cl_hdr:
        try:
            if int(cl_hdr) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "Payload too large"}, status_code=413)
        except ValueError:
            pass
    chunks = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
        chunks.append(chunk)
    body_bytes = b"".join(chunks)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    base = STUDY_UPSTREAM if path == "study/v1" else UPSTREAM
    upstream = client.build_request(
        request.method, f"{base}/{path}", params=request.query_params, headers=headers, content=body_bytes
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        logger.warning(
            "upstream error for %s /%s from %s: %s: %s",
            request.method, path, client_ip, type(exc).__name__, exc,
        )
        return JSONResponse({"error": "upstream unavailable"}, status_code=502)
    logger.info("%s /%s from %s -> %d", request.method, path, client_ip, resp.status_code)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_HEADERS | {"content-encoding"}}

    closed = False

    async def close_upstream():
        nonlocal closed
        if not closed:
            closed = True
            await resp.aclose()

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await close_upstream()

    return StreamingResponse(
        body(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=resp.headers.get("content-type"),
        background=BackgroundTask(close_upstream),
    )


@app.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def everything_else(rest: str):
    return Response(status_code=404)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    uvicorn.run(app, host="0.0.0.0", port=int(CONFIG.get("port", 8080)), log_level="warning")
