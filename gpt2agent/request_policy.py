"""Bounded process request policy for private ChatGPT account routes."""

from __future__ import annotations

import math
import os
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator

from gpt2agent.errors import BackendHTTPError, normalize_route


_DEFAULT_MAX_IN_FLIGHT = 4
_MIN_MAX_IN_FLIGHT = 1
_MAX_MAX_IN_FLIGHT = 8
_DEFAULT_RETRY_AFTER = 5.0
_MAX_RETRY_AFTER = 60.0


def configured_max_in_flight() -> int:
    """Read the process limit, rejecting ambiguous or unsafe values."""
    raw = os.environ.get("GPT2AGENT_MAX_IN_FLIGHT")
    if raw is None:
        return _DEFAULT_MAX_IN_FLIGHT
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("GPT2AGENT_MAX_IN_FLIGHT must be an integer from 1 to 8") from exc
    if str(value) != raw.strip() or not (_MIN_MAX_IN_FLIGHT <= value <= _MAX_MAX_IN_FLIGHT):
        raise ValueError("GPT2AGENT_MAX_IN_FLIGHT must be an integer from 1 to 8")
    return value


class RequestPolicy:
    def __init__(
        self,
        max_in_flight: int | None = None,
        acquire_timeout: float = 1.0,
        *,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        limit = configured_max_in_flight() if max_in_flight is None else max_in_flight
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("max_in_flight must be an integer from 1 to 8")
        if not (_MIN_MAX_IN_FLIGHT <= limit <= _MAX_MAX_IN_FLIGHT):
            raise ValueError("max_in_flight must be an integer from 1 to 8")
        timeout = float(acquire_timeout)
        if not math.isfinite(timeout) or not (0.0 < timeout <= 1.0):
            raise ValueError("acquire_timeout must be greater than 0 and at most 1 second")

        self.max_in_flight = limit
        self.acquire_timeout = timeout
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._permits = threading.BoundedSemaphore(limit)
        self._cooldown_lock = threading.Lock()
        self._cooldowns: dict[str, float] = {}

    def request(self, method: str, route: str) -> AbstractContextManager[None]:
        return self._request(method, route)

    @contextmanager
    def _request(self, method: str, route: str) -> Iterator[None]:
        normalized = normalize_route(route)
        self._raise_if_cooling_down(method, normalized)
        acquired = self._permits.acquire(timeout=self.acquire_timeout)
        if not acquired:
            raise BackendHTTPError(
                method,
                normalized,
                None,
                code="temporarily_failed",
                retryable=True,
                retry_after=self.acquire_timeout,
            )
        try:
            # A request ahead of us may have received a 429 while this caller
            # waited for a permit. Re-check after acquisition before touching
            # the network.
            self._raise_if_cooling_down(method, normalized)
            yield
        finally:
            self._permits.release()

    def parse_retry_after(self, value: Any) -> float:
        if isinstance(value, bool) or value is None:
            return _DEFAULT_RETRY_AFTER
        raw = str(value).strip()
        if not raw:
            return _DEFAULT_RETRY_AFTER
        try:
            seconds = float(raw)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed is None:
                    raise ValueError
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = max(0.0, parsed.timestamp() - self._wall_clock())
            except (TypeError, ValueError, OverflowError):
                return _DEFAULT_RETRY_AFTER
        if not math.isfinite(seconds) or seconds < 0.0:
            return _DEFAULT_RETRY_AFTER
        return min(seconds, _MAX_RETRY_AFTER)

    def activate_cooldown(self, route: str, retry_after: Any) -> float:
        normalized = normalize_route(route)
        delay = self.parse_retry_after(retry_after)
        now = self._clock()
        proposed = now + delay
        with self._cooldown_lock:
            expired = [key for key, deadline in self._cooldowns.items() if deadline <= now]
            for key in expired:
                del self._cooldowns[key]
            deadline = max(self._cooldowns.get(normalized, now), proposed)
            if deadline > now:
                self._cooldowns[normalized] = deadline
        return max(0.0, deadline - now)

    def cooldown_remaining(self, route: str) -> float:
        normalized = normalize_route(route)
        now = self._clock()
        with self._cooldown_lock:
            deadline = self._cooldowns.get(normalized)
            if deadline is None:
                return 0.0
            remaining = deadline - now
            if remaining <= 0.0:
                del self._cooldowns[normalized]
                return 0.0
            return min(remaining, _MAX_RETRY_AFTER)

    def _raise_if_cooling_down(self, method: str, route: str) -> None:
        remaining = self.cooldown_remaining(route)
        if remaining > 0.0:
            raise BackendHTTPError(
                method,
                route,
                429,
                code="temporarily_failed",
                retryable=True,
                retry_after=remaining,
            )
