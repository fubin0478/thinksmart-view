#!/usr/bin/env python3
"""Encrypted bridge between the ThinkSmart View study monitor and the local Qwen3-VL model.

The device reaches this Mac only through the router's public port and the plain-HTTP
public_gateway.py (route /<token>/study/v1 -> 127.0.0.1:8084/study/v1). Pictures of the child
must not cross the internet readable, so every request and reply is sealed end to end with a
shared secret (stdlib only, the same code runs on the device):

  keys      enc/mac = HMAC-SHA256(secret, "study-vision enc|mac <direction>")
  cipher    ciphertext = plaintext XOR SHAKE256(enc_key || nonce)        (16-byte random nonce)
  integrity tag = HMAC-SHA256(mac_key, "v1|<ts>|" || nonce || ciphertext)  (encrypt-then-MAC)
  replay    requests older/newer than 120 s or with a seen nonce are refused;
            the reply carries the request nonce and is sealed in the other direction.

Nothing is kept: the pictures (one check = a burst of up to 4 frames, in time order) live in memory for the one model call. When the device stands in
portrait / upside down, it sends its gravity-sensor rotation and the frame is turned upright with
macOS's built-in `sips` in a private temp dir (0700, removed immediately) before the model sees it.
Secret: ~/.config/study-vision/secret (hex, chmod 600).  Model: llama-server on 127.0.0.1:8083.
"""
import base64
import hashlib
import hmac
import json
import os
import contextlib
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SECRET_FILE = os.environ.get("STUDY_VISION_SECRET_FILE", os.path.expanduser("~/.config/study-vision/secret"))
SECRET = bytes.fromhex(Path(SECRET_FILE).read_text().strip())
LLM = os.environ.get("STUDY_VISION_LLM", "http://127.0.0.1:8083").rstrip("/")
LLM_MODEL = os.environ.get("STUDY_VISION_MODEL", "qwen3-vl-8b").strip() or "qwen3-vl-8b"
LLM_API_KEY = os.environ.get("STUDY_VISION_API_KEY", "").strip()
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
    if not isinstance(env, dict) or type(env.get('v')) is not int or env['v'] != 1 or type(env.get('ts')) is not int:
        raise PermissionError('bad envelope')
    ts = env['ts']
    nonce, ct, tag = (base64.b64decode(env[k], validate=True) for k in ("nonce", "ct", "tag"))
    want = hmac.new(mac, b"v1|%d|" % ts + nonce + ct, hashlib.sha256).digest()
    canonical = base64.b64encode(nonce).decode()
    if len(nonce) != 16 or len(tag) != 32 or env['nonce'] != canonical or not hmac.compare_digest(tag, want):
        raise PermissionError("bad seal")
    if abs(time.time() - ts) > MAX_AGE:
        raise PermissionError("stale")
    data = json.loads(_xor(ct, enc, nonce))
    if not isinstance(data, dict):
        raise ValueError('request must be an object')
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


def upright(image_b64, rotate):
    """Turn the camera frame clockwise by `rotate` degrees (the intercom's gravity-sensor rot_deg)."""
    if not isinstance(image_b64, str) or len(image_b64) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError('image size')
    raw = base64.b64decode(image_b64, validate=True)
    if not 4 <= len(raw) <= MAX_IMAGE_BYTES or not raw.startswith(b'\xff\xd8\xff'):
        raise ValueError('image must be JPEG')
    if rotate not in (90, 180, 270):
        return image_b64
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE) as d:
        path = os.path.join(d, "f.jpg")
        with open(path, "wb") as f:
            f.write(raw)
        if os.path.exists("/usr/bin/sips"):
            subprocess.run(["/usr/bin/sips", "-r", str(rotate), path], capture_output=True, timeout=10, check=True)
            out_path = path
        else:
            out_path = os.path.join(d, "r.jpg")
            vf = {90: "transpose=1", 180: "transpose=2,transpose=2", 270: "transpose=2"}[rotate]
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", path, "-vf", vf, "-q:v", "3", out_path],
                           capture_output=True, timeout=10, check=True)
        with open(out_path, "rb") as f:
            return base64.b64encode(f.read()).decode()


# ---- model call ------------------------------------------------------------------------
def _chat_completions_url(base: str) -> str:
    if base.endswith("/chat/completions"):
        return base
    if base.endswith(("/v1", "/v3", "/v4")):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def ask_model(prompt, images_b64, max_tokens):
    parts = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in images_b64]
    body = {
        "model": LLM_MODEL,
        "temperature": 0.1,
        "max_tokens": max(16, min(int(max_tokens), 512)),
        "messages": [{"role": "user", "content": parts + [{"type": "text", "text": prompt}]}],
    }
    hdrs = {"Content-Type": "application/json", "User-Agent": "thinksmart-study-vision/1.0"}
    if LLM_API_KEY:
        hdrs["Authorization"] = f"Bearer {LLM_API_KEY}"
    req = urllib.request.Request(_chat_completions_url(LLM), data=json.dumps(body).encode(), headers=hdrs)
    if not _model_lock.acquire(timeout=0.5):
        raise ModelBusy('another check is still processing')
    try:
        with urllib.request.urlopen(req, timeout=75) as r:
            data = json.loads(r.read(1024 * 1024))
    finally:
        _model_lock.release()
    return data["choices"][0]["message"]["content"]


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
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
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
            urllib.request.urlopen(LLM + "/health", timeout=3).read()
            ok = True
        except Exception:
            ok = False
        self._send(200, {"ok": True, "model_ready": ok})

    def do_POST(self):
        _rotate_logs_if_needed()
        if self.path != "/study/v1":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {'error': 'invalid length'})
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
            rotate = req.get('rotate', 0)
            if type(rotate) is not int or rotate not in (0, 90, 180, 270):
                raise ValueError('rotation')
            frames = req.get("images_b64") or [req.get("image_b64", "")]   # old single-frame clients too
            if not isinstance(frames, list) or not 0 < len(frames) <= MAX_IMAGES:
                raise ValueError("frames")
            prompt = req.get('prompt')
            if not isinstance(prompt, str) or not 0 < len(prompt) <= 8000:
                raise ValueError('prompt')
            images = [upright(b, rotate) for b in frames]
            text = ask_model(prompt, images, req.get("max_tokens", 200))
            reply = {"ok": True, "re": nonce, "text": text, "rotated": rotate, "frames": len(images),
                     "ms": int((time.monotonic() - started) * 1000)}
        except Exception as e:
            reply = {"ok": False, "re": nonce, "error": type(e).__name__}
        print(time.strftime("%H:%M:%S"), "study check", "ok" if reply["ok"] else reply["error"], f'{reply.get("frames", 0)}f',
              f'{reply.get("ms", 0)}ms', flush=True)
        self._send(200, seal("resp", reply))


class _VisionServer(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    _rotate_logs_if_needed(force=True)
    _VisionServer(("127.0.0.1", PORT), Handler).serve_forever()
