"""
Simple in-memory rate limiter for Flask.

Uses a per-IP token bucket. Suitable for single-process deployments.
For production multi-process use, swap to a Redis-backed implementation.
"""

import time
import threading
from collections import defaultdict

from flask import request, jsonify


class RateLimiter:
    """Token-bucket rate limiter keyed by client IP."""

    def __init__(self, requests_per_minute: int = 60):
        self.rpm = requests_per_minute
        self.interval = 60.0 / requests_per_minute  # seconds between replenishment basis
        self._last_request: dict[str, float] = defaultdict(float)
        self._tokens: dict[str, float] = defaultdict(lambda: float(requests_per_minute))
        self._lock = threading.Lock()

    def _get_key(self) -> str:
        """Use client IP as the rate limit key."""
        return request.remote_addr or '127.0.0.1'

    def is_allowed(self) -> bool:
        """Check if the request is allowed under the rate limit."""
        key = self._get_key()
        now = time.monotonic()

        with self._lock:
            elapsed = now - self._last_request[key]
            self._last_request[key] = now

            self._tokens[key] = min(
                float(self.rpm),
                self._tokens[key] + elapsed / self.interval
            )

            if self._tokens[key] >= 1.0:
                self._tokens[key] -= 1.0
                return True
            return False

    def limit(self):
        """Flask before_request handler. Returns 429 if rate limit exceeded."""
        if not self.is_allowed():
            return jsonify({
                "success": False,
                "error": "Rate limit exceeded. Please slow down.",
            }), 429
        return None


def init_rate_limiter(app, requests_per_minute: int = 60):
    """Register rate limiter as a before_request hook on the Flask app."""
    limiter = RateLimiter(requests_per_minute)
    app.before_request(limiter.limit)
    return limiter
