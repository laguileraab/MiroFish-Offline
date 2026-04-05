"""
Phase 14 TASK-047 — limit concurrent graph ingest LLM calls (shared across add_text paths).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from ..config import Config

_lock = threading.Lock()
_sem: Optional[threading.BoundedSemaphore] = None


def _ensure_sem() -> Optional[threading.BoundedSemaphore]:
    global _sem
    n = Config.LLM_INGEST_MAX_CONCURRENT
    if n is None or n <= 0:
        _sem = None
        return None
    with _lock:
        cap = max(1, int(n))
        if _sem is None or getattr(_sem, "_mirofish_cap", None) != cap:
            _sem = threading.BoundedSemaphore(cap)
            setattr(_sem, "_mirofish_cap", cap)
    return _sem


@contextmanager
def acquire_ingest_slot() -> Iterator[None]:
    """Block until a slot is available when LLM_INGEST_MAX_CONCURRENT > 0."""
    s = _ensure_sem()
    if s is None:
        yield
        return
    s.acquire()
    try:
        yield
    finally:
        s.release()
