"""
Phase 14 TASK-048 — process-wide ingest counters (thread-safe, in-memory).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class IngestCounters:
    chunks_ok: int = 0
    chunks_failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_ok(self) -> None:
        with self._lock:
            self.chunks_ok += 1

    def record_fail(self) -> None:
        with self._lock:
            self.chunks_failed += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"chunks_ok": self.chunks_ok, "chunks_failed": self.chunks_failed}


_GLOBAL = IngestCounters()


def get_ingest_counters() -> IngestCounters:
    return _GLOBAL
