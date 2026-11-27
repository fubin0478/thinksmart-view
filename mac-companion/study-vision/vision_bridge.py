#!/usr/bin/env python3
"""Encrypted bridge between the ThinkSmart View study monitor and the local Qwen3.5-9B vision model.

The device reaches this Mac through LAN or the token-gated public_gateway.py
(route /<token>/study/v1 -> 127.0.0.1:8084/study/v1). Pictures of the child must not cross the
network readable, so every request and reply is sealed end to end with a shared secret
(stdlib only, the same code runs on the device):

  keys      enc/mac = HMAC-SHA256(secret, "study-vision enc|mac <direction>")
  cipher    ciphertext = plaintext XOR SHAKE256(enc_key || nonce)        (16-byte random nonce)
  integrity tag = HMAC-SHA256(mac_key, "v1|<ts>|" || nonce || ciphertext)  (encrypt-then-MAC)
  replay    requests older/newer than 120 s or with a seen nonce are refused;
            the reply carries the request nonce and is sealed in the other direction.

Nothing is kept: the pictures (one check = a burst of up to 4 frames, in time order) live in memory
for the one model call. When the device stands in portrait / upside down, it sends its gravity-sensor
rotation and all burst frames are turned upright in one batch with macOS's built-in `sips` in a
private temp dir (0700, removed immediately) before the model sees them.
Secret: ~/.config/study-vision/secret (hex, chmod 600).
Model: llama-server (STUDY_VISION_LLM, default http://127.0.0.1:8083).
"""
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx

SECRET_FILE = os.environ.get("STUDY_VISION_SECRET_FILE", os.path.expanduser("~/.config/study-vision/secret"))
SECRET = bytes.fromhex(Path(SECRET_FILE).read_text().strip())
LLM = os.environ.get("STUDY_VISION_LLM", "http://127.0.0.1:8083").strip().rstrip("/")
LLM_FALLBACK = os.environ.get("STUDY_VISION_LLM_FALLBACK", "").strip().rstrip("/")
MODEL_NAME = os.environ.get("STUDY_VISION_MODEL", "qwen3.5-9b").strip() or "qwen3.5-9b"
LLM_API_KEY_ENV = os.environ.get("STUDY_VISION_API_KEY", "").strip()
LLM_API_KEY_FILE = Path(
    os.path.expanduser(os.environ.get("STUDY_VISION_API_KEY_FILE", "~/.config/study-vision/llm_api_key"))
)
PORT = int(os.environ.get("STUDY_VISION_PORT", "8084"))
MAX_BODY = 8 * 1024 * 1024
MAX_IMAGES = 4          # a check sends a short burst of frames (3, 4 s apart) in time order
MAX_AGE = 120

CACHE = Path(os.path.expanduser("~/Library/Caches/study-vision"))
LOG_DIR = Path(os.path.expanduser("~/Library/Logs/study-vision"))
LOG_FILES = ("bridge.out.log", "bridge.err.log", "llm.out.log", "llm.err.log")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_ROTATE_INTERVAL = 300.0
_log_rotate_lock = threading.Lock()
_last_log_rotate = 0.0
_seen = {}


def _rotate_logs_if_needed(force: bool = False) -> None:
    """Copy-and-truncate study-vision LaunchAgent logs exceeding 10 MB without breaking open FDs."""
    global _last_log_rotate
    now = time.monotonic()
    if not force and (now - _last_log_rotate) < LOG_ROTATE_INTERVAL:
        return
    if not _log_rotate_lock.acquire(blocking=force):
        return
    try:
        now = time.monotonic()
        if not force and (now - _last_log_rotate) < LOG_ROTATE_INTERVAL:
            return
        _last_log_rotate = now
        for name in LOG_FILES:
            p = LOG_DIR / name
            try:
                if p.exists() and p.stat().st_size > LOG_MAX_BYTES:
                    shutil.copy2(p, LOG_DIR / f"{name}.1")
                    os.truncate(p, 0)
            except OSError:
                pass
    finally:
        _log_rotate_lock.release()


_seen_lock = threading.Lock()
_model_lock = threading.Lock()  # llama-server has one slot; queue requests here
MAX_IMAGE_BYTES = 2 * 1024 * 1024


class ModelBusy(RuntimeError):
    pass


# ---- sealing (identical on the device) -------------------------------------------------
def _keys(direction):
    enc = hmac.new(SECRET, b"study-vision enc " + direction.encode(), hashlib.sha256).digest()
    mac = hmac.new(SECRET, b"study-vision mac " + direction.encode(), hashlib.sha256).digest()
    return enc, mac


def _xor(data, enc, nonce):
    if not data:
        return b""
    ks = hashlib.shake_256(enc + nonce).digest(len(data))
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(data), "big")


def seal(direction, obj):
    enc, mac = _keys(direction)
    nonce, ts = os.urandom(16), int(time.time())
    ct = _xor(json.dumps(obj, ensure_ascii=False).encode(), enc, nonce)
    tag = hmac.new(mac, b"v1|%d|" % ts + nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "ts": ts, "nonce": b64(nonce), "ct": b64(ct), "tag": b64(tag)}


def unseal(direction, env):
    enc, mac = _keys(direction)
    if not isinstance(env, dict) or type(env.get("v")) is not int or env["v"] != 1 or type(env.get("ts")) is not int:
        raise PermissionError("bad envelope")
    ts = env["ts"]
    nonce, ct, tag = (base64.b64decode(env[k], validate=True) for k in ("nonce", "ct", "tag"))
    want = hmac.new(mac, b"v1|%d|" % ts + nonce + ct, hashlib.sha256).digest()
    canonical = base64.b64encode(nonce).decode()
    if len(nonce) != 16 or len(tag) != 32 or env["nonce"] != canonical or not hmac.compare_digest(tag, want):
        raise PermissionError("bad seal")
    if abs(time.time() - ts) > MAX_AGE:
        raise PermissionError("stale")
    data = json.loads(_xor(ct, enc, nonce))
    if not isinstance(data, dict):
        raise ValueError("request must be an object")
    return data, canonical


def _fresh_nonce(nonce):
    now = time.time()
    with _seen_lock:
        for k in [k for k, t in _seen.items() if now - t > 2 * MAX_AGE]:
            del _seen[k]
        if nonce in _seen:
            return False
        _seen[nonce] = now
        return True


def upright_batch(frames_b64, rotate):
    """Turn all camera frames clockwise by `rotate` degrees in a single `sips` invocation."""
    raws = []
    for image_b64 in frames_b64:
        if not isinstance(image_b64, str) or len(image_b64) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("image size")
        raw = base64.b64decode(image_b64, validate=True)
        if not 4 <= len(raw) <= MAX_IMAGE_BYTES or not raw.startswith(b"\xff\xd8\xff"):
            raise ValueError("image must be JPEG")
        raws.append(raw)
    if rotate not in (90, 180, 270) or not raws:
        return list(frames_b64)
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE) as d:
        paths = []
        for idx, raw in enumerate(raws):
            path = os.path.join(d, f"f{idx}.jpg")
            with open(path, "wb") as f:
                f.write(raw)
            paths.append(path)
        if os.path.exists("/usr/bin/sips"):
            subprocess.run(["/usr/bin/sips", "-r", str(rotate), *paths], capture_output=True, timeout=10, check=True)
            out_paths = paths
        else:
            vf = {90: "transpose=1", 180: "transpose=2,transpose=2", 270: "transpose=2"}[rotate]
            out_paths = []
            for idx, path in enumerate(paths):
                rpath = os.path.join(d, f"r{idx}.jpg")
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", path, "-vf", vf, "-q:v", "3", rpath],
                    capture_output=True,
                    timeout=10,
                    check=True,
                )
                out_paths.append(rpath)
        out = []
        for path in out_paths:
            with open(path, "rb") as f:
                out.append(base64.b64encode(f.read()).decode())
        return out


def upright(image_b64, rotate):
    """Turn the camera frame clockwise by `rotate` degrees (the intercom's gravity-sensor rot_deg)."""
    return upright_batch([image_b64], rotate)[0]


# ---- voice/GPU coordination ------------------------------------------------------------
VOICE_HEALTH_URLS = [
    u.strip()
    for u in os.environ.get(
        "STUDY_VISION_VOICE_HEALTH_URLS",
        "http://127.0.0.1:8010/health,http://127.0.0.1:8011/health",
    ).split(",")
    if u.strip()
]
VOICE_WAIT_MAX_S = float(os.environ.get("STUDY_VISION_VOICE_WAIT_MAX", "14.0"))
VOICE_QUIET_S = float(os.environ.get("STUDY_VISION_VOICE_QUIET", "2.5"))
VOICE_ASR_QUIET_S = float(os.environ.get("STUDY_VISION_VOICE_ASR_QUIET", "4.2"))
TOTAL_MODEL_TIMEOUT_S = float(os.environ.get("STUDY_VISION_TOTAL_TIMEOUT", "75.0"))
INFERRING_MARKER = CACHE / "inferring.json"
GPU_HOLD_DIR = Path(os.path.expanduser("~/Library/Caches/xiaozhi-voice/gpu-holds"))
_LLM_PARSED = urlparse(LLM)
_LLM_PORT = _LLM_PARSED.port or 8083
_cached_llm_pid = {"pid": 0, "ts": 0.0}
_llm_pid_lock = threading.Lock()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_local_llama_pid(pid: int) -> bool:
    if pid <= 1 or pid == os.getpid() or not _is_pid_alive(pid):
        return False
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        cmd = (res.stdout or "").strip()
        return "llama-server" in cmd and bool(re.search(rf"(?:^|\s|=){_LLM_PORT}(?:\s|$)", cmd))
    except Exception:
        return False


def _find_llm_pid() -> int:
    if _LLM_PARSED.hostname not in (None, "", "127.0.0.1", "localhost"):
        return 0
    now = time.monotonic()
    with _llm_pid_lock:
        cached = int(_cached_llm_pid["pid"])
        if cached > 1 and (now - _cached_llm_pid["ts"]) < 600.0 and _is_pid_alive(cached):
            return cached
        try:
            res = subprocess.run(
                ["lsof", "-nPt", f"-iTCP:{_LLM_PORT}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            for line in (res.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if _is_local_llama_pid(pid):
                        _cached_llm_pid["pid"] = pid
                        _cached_llm_pid["ts"] = now
                        return pid
        except Exception:
            pass
        _cached_llm_pid["pid"] = 0
        _cached_llm_pid["ts"] = now
        return 0


@contextlib.contextmanager
def _gpu_hold_flock():
    GPU_HOLD_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(GPU_HOLD_DIR / ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _is_voice_busy_or_recent() -> bool:
    for url in VOICE_HEALTH_URLS:
        try:
            with urllib.request.urlopen(url, timeout=0.3) as r:
                data = json.loads(r.read(65536))
            if (
                data.get("voice_active")
                or data.get("tts_busy")
                or data.get("asr_busy")
                or data.get("translation_busy")
            ):
                return True
            asr_age = data.get("last_asr_age_s")
            if isinstance(asr_age, (int, float)) and asr_age < VOICE_ASR_QUIET_S:
                return True
            age = data.get("last_voice_age_s")
            if isinstance(age, (int, float)) and age < VOICE_QUIET_S:
                return True
        except Exception:
            pass
    return False


def _wait_for_voice_quiet(max_wait: float = VOICE_WAIT_MAX_S) -> float:
    if max_wait <= 0 or not VOICE_HEALTH_URLS:
        return 0.0
    t0 = time.monotonic()
    waited = 0.0
    while _is_voice_busy_or_recent():
        waited = time.monotonic() - t0
        if waited >= max_wait:
            return waited
        time.sleep(min(0.25, max_wait - waited))
    return time.monotonic() - t0


def _mark_inferring_start(pid: int) -> int:
    if pid <= 1 or not _is_pid_alive(pid):
        pid = _find_llm_pid()
    if pid <= 1:
        return 0
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CACHE / f".inferring.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps({"pid": pid, "ts": time.time()}), encoding="utf-8")
    os.replace(tmp, INFERRING_MARKER)
    return pid


def _clear_inferring_and_resume(target_pid: int = 0) -> None:
    try:
        with _gpu_hold_flock():
            with contextlib.suppress(OSError):
                INFERRING_MARKER.unlink(missing_ok=True)
            now = time.time()
            live_for_target = 0
            for p in GPU_HOLD_DIR.glob("*.lease"):
                remove = False
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    owner_pid = int(data.get("owner_pid") or 0)
                    tpid = int(data.get("target_pid") or 0)
                    exp = float(data.get("exp") or 0.0)
                    is_grace = bool(data.get("grace"))
                    if exp <= now or not _is_pid_alive(owner_pid) or is_grace:
                        remove = True
                    elif target_pid <= 1 or tpid == target_pid:
                        live_for_target += 1
                except Exception:
                    remove = True
                if remove:
                    with contextlib.suppress(OSError):
                        p.unlink(missing_ok=True)
            if target_pid > 1 and live_for_target == 0 and _is_local_llama_pid(target_pid):
                with contextlib.suppress(OSError):
                    os.kill(target_pid, signal.SIGCONT)
    except Exception:
        with contextlib.suppress(OSError):
            INFERRING_MARKER.unlink(missing_ok=True)


# ---- model call ------------------------------------------------------------------------
_HTTP_CLIENT = httpx.Client(
    limits=httpx.Limits(max_keepalive_connections=4, max_connections=8, keepalive_expiry=300.0),
    http2=False,
)


def _llm_headers(extra=None):
    headers = dict(extra or {})
    if LLM_API_KEY_ENV:
        headers["Authorization"] = f"Bearer {LLM_API_KEY_ENV}"
        return headers
    try:
        if LLM_API_KEY_FILE.exists():
            key = LLM_API_KEY_FILE.read_text(encoding="utf-8").strip()
            if key:
                headers["Authorization"] = f"Bearer {key}"
    except OSError:
        pass
    return headers


def _resolve_llm_url(base: str, path: str) -> str:
    if path == "/v1/chat/completions":
        if base.endswith("/chat/completions"):
            return base
        if base.endswith(("/v1", "/v3", "/v4")):
            return base + "/chat/completions"
    return base + path


def _llm_request(method, path, json_bytes=None, timeout=10.0):
    bases = [LLM]
    if LLM_FALLBACK and LLM_FALLBACK != LLM:
        bases.append(LLM_FALLBACK)
    headers = _llm_headers({"Content-Type": "application/json"} if json_bytes is not None else None)
    last_exc = None
    for i, base in enumerate(bases):
        try:
            resp = _HTTP_CLIENT.request(
                method, _resolve_llm_url(base, path), content=json_bytes, headers=headers, timeout=timeout
            )
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError:
            raise
        except httpx.TransportError as exc:
            last_exc = exc
            if i + 1 < len(bases):
                continue
            raise
    raise last_exc


def ask_model(prompt, images_b64, max_tokens):
    parts = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in images_b64]
    # Place static instruction text before images so llama-server --cache-ram hits prefix KV cache across checks
    body = {
        "model": MODEL_NAME,
        "temperature": 0.1,
        "max_tokens": max(16, min(int(max_tokens), 768)),
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}] + parts}],
    }
    payload = json.dumps(body).encode()
    if not _model_lock.acquire(timeout=20.0):
        raise ModelBusy("another check is still processing")
    is_local_gpu = _LLM_PARSED.hostname in (None, "", "127.0.0.1", "localhost")
    vl_pid = 0
    try:
        pre_pid = _find_llm_pid() if is_local_gpu else 0
        waited = _wait_for_voice_quiet() if is_local_gpu else 0.0
        if waited >= 0.3:
            print(time.strftime("%H:%M:%S"), f"study wait_voice_quiet {waited:.2f}s", flush=True)
        vl_pid = _mark_inferring_start(pre_pid) if is_local_gpu else 0
        llm_timeout = max(25.0, TOTAL_MODEL_TIMEOUT_S - waited)
        r = _llm_request(
            "POST",
            "/v1/chat/completions",
            json_bytes=payload,
            timeout=httpx.Timeout(llm_timeout, connect=4.0),
        )
        data = r.json()
    finally:
        try:
            if is_local_gpu:
                _clear_inferring_and_resume(vl_pid)
        finally:
            _model_lock.release()
    content = data["choices"][0]["message"]["content"]
    return re.sub(r"^\s*<think>.*?</think>\s*", "", content, flags=re.DOTALL)


class Handler(BaseHTTPRequestHandler):
    server_version = "study-vision"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):  # no request lines with addresses in the log
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            self.wfile.write(raw)

    def do_GET(self):
        _rotate_logs_if_needed()
        if self.path == "/health":
            return self._send(200, {"status": "ok", "ok": True})
        if self.path != "/study/health":
            return self._send(404, {"error": "not found"})
        try:
            _llm_request("GET", "/health", timeout=3.0)
            ok = True
        except Exception:
            ok = False
        self._send(200, {"ok": True, "model_ready": ok, "model": MODEL_NAME})

    def do_POST(self):
        _rotate_logs_if_needed()
        if self.path != "/study/v1":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "invalid length"})
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "size"})
        try:
            req, nonce = unseal("req", json.loads(self.rfile.read(length)))
            if not _fresh_nonce(nonce):
                raise PermissionError("replay")
        except Exception:
            return self._send(403, {"error": "denied"})
        started = time.monotonic()
        try:
            rotate = req.get("rotate", 0)
            if type(rotate) is not int or rotate not in (0, 90, 180, 270):
                raise ValueError("rotation")
            frames = req.get("images_b64") or [req.get("image_b64", "")]   # old single-frame clients too
            if not isinstance(frames, list) or not 0 < len(frames) <= MAX_IMAGES:
                raise ValueError("frames")
            prompt = req.get("prompt")
            if not isinstance(prompt, str) or not 0 < len(prompt) <= 8000:
                raise ValueError("prompt")
            images = upright_batch(frames, rotate)
            text = ask_model(prompt, images, req.get("max_tokens", 200))
            reply = {
                "ok": True,
                "re": nonce,
                "text": text,
                "rotated": rotate,
                "frames": len(images),
                "ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as e:
            reply = {"ok": False, "re": nonce, "error": type(e).__name__}
        print(
            time.strftime("%H:%M:%S"),
            "study check",
            "ok" if reply["ok"] else reply["error"],
            f'{reply.get("frames", 0)}f',
            f'{reply.get("ms", 0)}ms',
            flush=True,
        )
        self._send(200, seal("resp", reply))


class _VisionServer(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    _rotate_logs_if_needed(force=True)
    _clear_inferring_and_resume(_find_llm_pid())
    _VisionServer(("127.0.0.1", PORT), Handler).serve_forever()
