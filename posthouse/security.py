"""Password hashing, token helpers and a small in-memory rate limiter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

_PBKDF2_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def constant_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_token(prefix: str = "", nbytes: int = 32) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class RateLimiter:
    """Counts failures per key in a sliding window; blocks once the limit is hit."""

    def __init__(self, max_failures: int, window_seconds: int):
        self.max_failures = max_failures
        self.window = window_seconds
        self._fail: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        items = [t for t in self._fail.get(key, []) if now - t < self.window]
        if items:
            self._fail[key] = items
        else:
            self._fail.pop(key, None)
        return items

    def blocked(self, key: str) -> bool:
        if self.max_failures <= 0:
            return False
        with self._lock:
            return len(self._prune(key, time.time())) >= self.max_failures

    def failure(self, key: str) -> None:
        with self._lock:
            now = time.time()
            self._prune(key, now)
            self._fail.setdefault(key, []).append(now)

    def success(self, key: str) -> None:
        with self._lock:
            self._fail.pop(key, None)
