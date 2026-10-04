import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def check(key: str, limit: int, window: int = 60) -> None:
    """Sliding-window limiter (in-memory, per process). Raises 429 when exceeded."""
    now = time.monotonic()
    with _lock:
        if len(_hits) > 10_000:
            _hits.clear()
        dq = _hits[key]
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(429, "Too many requests, please slow down.")
        dq.append(now)


def blocked(key: str, limit: int, window: int = 300) -> bool:
    """True if `key` already has >= limit recorded events inside the window (does not record)."""
    now = time.monotonic()
    with _lock:
        dq = _hits.get(key)
        if not dq:
            return False
        while dq and now - dq[0] > window:
            dq.popleft()
        return len(dq) >= limit


def record(key: str) -> None:
    with _lock:
        _hits[key].append(time.monotonic())
