"""管理后台的密码哈希与登录会话。"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from pathlib import Path

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 256
MAX_SESSIONS = 64


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"密码最多 {MAX_PASSWORD_LENGTH} 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
        return False
    if not isinstance(stored, str):
        return False
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(digest, expected)


class SessionStore:
    """内存中的登录会话；服务重启后需要重新登录（后台点了更新网关时除外，见 save/restore）。

    只存令牌的 SHA-256：交接文件被人看到也拿不到能用的令牌。"""

    def __init__(self, ttl_seconds: int = 12 * 3600):
        self.ttl = ttl_seconds
        self._sessions = {}  # 令牌的哈希 -> 过期时间

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self) -> str:
        self._purge()
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions, key=self._sessions.get)
            del self._sessions[oldest]
        token = secrets.token_urlsafe(32)
        self._sessions[self._key(token)] = time.time() + self.ttl
        return token

    def valid(self, token) -> bool:
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        key = self._key(token)
        expiry = self._sessions.get(key)
        if expiry is None or expiry < time.time():
            self._sessions.pop(key, None)
            return False
        return True

    def revoke(self, token) -> None:
        if isinstance(token, str) and token and len(token) <= 256:
            self._sessions.pop(self._key(token), None)

    def revoke_all(self) -> None:
        self._sessions.clear()

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, exp in self._sessions.items() if exp < now]:
            del self._sessions[key]

    def save(self, path: Path) -> None:
        """后台更新网关前调用：把登录会话交给重启后的新进程，更新完页面不用重新登录。"""
        self._purge()
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError(f"Refusing symlink session path: {path}")
        tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump({"saved": time.time(), "sessions": self._sessions}, f)
            if path.is_symlink():
                raise OSError(f"Refusing symlink session path: {path}")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def restore(self, path: Path, max_age: int = 1800) -> int:
        """启动时调用：读入 max_age 秒内交接过来的会话，读完就删掉文件。返回读入了几个。"""
        path = Path(path)
        try:
            st0 = os.lstat(path)
        except OSError:
            return 0
        if not stat.S_ISREG(st0.st_mode):
            if stat.S_ISLNK(st0.st_mode):
                path.unlink(missing_ok=True)
            return 0
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(path, flags)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
                    raise OSError("Invalid session handoff file")
                chunks, rem = [], 65537
                while rem > 0:
                    b = os.read(fd, min(16384, rem))
                    if not b:
                        break
                    chunks.append(b)
                    rem -= len(b)
                raw = b"".join(chunks)
                if len(raw) > 65536:
                    raise OSError("Session handoff file too large")
                data = json.loads(raw.decode("utf-8"))
            finally:
                os.close(fd)
        except (OSError, ValueError, UnicodeDecodeError):
            return 0
        finally:
            if not path.is_symlink():
                path.unlink(missing_ok=True)
        if not isinstance(data, dict) or time.time() - float(data.get("saved") or 0) > max_age:
            return 0
        now = time.time()
        raw_sessions = data.get("sessions")
        if not isinstance(raw_sessions, dict):
            return 0
        sessions = {
            k: float(v)
            for k, v in raw_sessions.items()
            if isinstance(k, str) and len(k) == 64 and isinstance(v, (int, float)) and v > now
        }
        if len(sessions) > MAX_SESSIONS:
            sessions = dict(sorted(sessions.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SESSIONS])
        self._sessions.update(sessions)
        return len(sessions)
