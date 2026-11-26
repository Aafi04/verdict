"""Signed anonymous cookie and zero-IP-storage rate limiter for the quiz."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque

_UUID_RE = re.compile(
    r"^(?:sim-)?[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$"
)


def new_visitor_id(simulated: bool = False) -> str:
    """Create a random anonymous visitor ID (prefixed 'sim-' only in simulation mode)."""
    uid = str(uuid.uuid4())
    return f"sim-{uid}" if simulated else uid


def sign_visitor_id(vid: str, secret: str) -> str:
    """Return '<vid>.<hmac_sha256_hex>'."""
    sig = hmac.new(secret.encode("utf-8"), vid.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{vid}.{sig}"


def verify_visitor_cookie(
    token: str | None, secret: str, *, allow_sim_prefix: bool = False
) -> str | None:
    """Verify signed cookie and return visitor ID, or None if invalid/tampered."""
    if not token or "." not in token:
        return None
    vid, sig = token.rsplit(".", 1)
    if not _UUID_RE.match(vid):
        return None
    if vid.startswith("sim-") and not allow_sim_prefix:
        return None
    expected = hmac.new(secret.encode("utf-8"), vid.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return vid


class EphemeralRateLimiter:
    """In-memory per-IP rate limiter that stores only ephemeral salted hashes, never raw IPs."""

    def __init__(self, max_requests: int = 120, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._salt = os.urandom(32)
        self._buckets: dict[bytes, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _hash_ip(self, client_ip: str | None) -> bytes:
        normalized = (client_ip or "unknown").strip()
        return hmac.new(self._salt, normalized.encode("utf-8"), hashlib.sha256).digest()[:16]

    def allow(self, client_ip: str | None) -> bool:
        """Return True if under rate limit, False if throttled."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        key = self._hash_ip(client_ip)
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
            # Opportunistically prune stale empty buckets to bound memory.
            if len(self._buckets) > 2048:
                stale = [k for k, q in self._buckets.items() if not q or q[-1] < cutoff]
                for k in stale:
                    del self._buckets[k]
            return True
