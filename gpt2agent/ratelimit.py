"""Shared client-side rate limiter for gpt2agent.

Why: many agents share one ChatGPT account. Model quotas (e.g. ~200
``gpt-6-pro`` calls per window) and Cloudflare's abuse scoring are
account-level — a fleet that each self-throttles still overloads the
account. This limiter therefore lives *inside* gpt2agent, not in caller
policy, and keeps its state in ``~/.gpt2agent/ratelimit-state.json`` so
*separate gpt2agent processes* on this host share the same budget.

Two lanes:

* ``conversation`` — every POST to ``/backend-api[/f]/conversation``
  (chat, agent, image gen, tool_call, DR rounds). Sliding-window cap +
  minimum interval.
* ``read`` — ``BackendClient.get/post`` bookkeeping GETs. A light
  minimum interval; these still 429 under load.

Reactive cooldowns: when upstream answers ``usage_limit``/429 with a
``resets_after``, we record the cooldown keyed by model/feature so every
other process fails fast with the real reset time instead of burning a
sentinel mint to rediscover it.

Kill switch: ``GPT2AGENT_RATELIMIT_OFF=1`` (tests set this), or
``[rate_limit] enabled = false`` in config.toml.
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

_STATE_PATH = Path.home() / ".gpt2agent" / "ratelimit-state.json"

try:  # POSIX file lock for cross-process state; absent on Windows
    import fcntl
except ImportError:  # pragma: no cover - windows
    fcntl = None


class LocalRateLimitError(RuntimeError):
    """The shared client-side budget is exhausted and the wait would exceed
    ``max_wait_s``. Distinct from ``UsageLimitError`` (which reports an
    upstream account cap) — this one is ours."""


def _parse_ts(value) -> float | None:
    """ISO-8601 (or epoch) → epoch seconds; None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


class RateLimiter:
    """Sliding-window limiter with file-backed shared state."""

    def __init__(self, config: dict | None = None,
                 state_path: Path | None = None) -> None:
        rc = (config or {}).get("rate_limit", {}) or {}
        self.enabled = bool(rc.get("enabled", True)) and not os.environ.get(
            "GPT2AGENT_RATELIMIT_OFF"
        )
        self.min_interval_s = float(rc.get("min_interval_s", 15.0))
        self.read_min_interval_s = float(rc.get("read_min_interval_s", 1.0))
        self.max_per_window = int(rc.get("max_per_window", 100))
        self.window_s = float(rc.get("window_s", 3 * 3600))
        self.max_wait_s = float(rc.get("max_wait_s", 300.0))
        self.state_path = Path(
            os.environ.get("GPT2AGENT_RATELIMIT_STATE")
            or (state_path or _STATE_PATH)
        )
        self._lock_path = self.state_path.with_suffix(".lock")
        self._mem_lock = threading.Lock()

    # ── state file ────────────────────────────────────────────────────

    def _locked_state(self, mutate=None):
        """read-modify-write under an inter-process lock."""
        if not self.enabled:
            st = {"conv_requests": [], "read_requests": [], "cooldowns": {}}
            return mutate(st) if mutate else st
        with self._mem_lock:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a") as lock:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    st = self._read()
                    result = mutate(st) if mutate else st
                    if mutate is not None:
                        self._write(st)
                    return result
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            st = json.loads(self.state_path.read_text())
            if isinstance(st, dict):
                st.setdefault("conv_requests", [])
                st.setdefault("read_requests", [])
                st.setdefault("cooldowns", {})
                return st
        except Exception:
            pass
        return {"conv_requests": [], "read_requests": [], "cooldowns": {}}

    def _write(self, st: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.state_path.parent),
                prefix=".ratelimit-",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(st))
            os.replace(tmp, self.state_path)
        except Exception as exc:  # noqa: BLE001 - persistence best-effort
            _log.debug("ratelimit state save failed: %s", exc)

    # ── cooldowns (upstream-reported caps) ────────────────────────────

    def note_cooldown(self, key: str, resets_after) -> None:
        """Record an upstream cap — ``key`` like ``model:gpt-6-pro`` or
        ``feature:deep_research``. Other processes then fail fast."""
        until = _parse_ts(resets_after)
        if until is None or until <= time.time():
            return
        if not self.enabled:
            return
        self._locked_state(lambda st: st["cooldowns"].__setitem__(key, until))

    def cooldown_for(self, key: str) -> float | None:
        """Epoch the cooldown lifts, or None."""
        if not self.enabled:
            return None
        st = self._locked_state()
        until = _parse_ts(st["cooldowns"].get(key))
        if until and until > time.time():
            return until
        return None

    def note_429(self, lane: str, retry_s: float = 60.0) -> None:
        """Short shared cooldown after an upstream HTTP 429."""
        self.note_cooldown(f"http429:{lane}", time.time() + retry_s)

    # ── lanes ─────────────────────────────────────────────────────────

    def _lane_wait(self, st: dict, lane_key: str, last_key: str,
                   min_interval: float, windowed: bool) -> float:
        """Seconds to wait before the next request on this lane."""
        now = time.time()
        wait = 0.0
        last = st.get(last_key)
        if last:
            wait = max(wait, min_interval - (now - last))
        if windowed:
            reqs = [t for t in st[lane_key] if now - t < self.window_s]
            st[lane_key] = reqs
            if len(reqs) >= self.max_per_window:
                oldest = min(reqs)
                wait = max(wait, self.window_s - (now - oldest))
        return wait

    def _reserve(self, lane_key: str, last_key: str, min_interval: float,
                 windowed: bool) -> float:
        """Reserve a slot now; returns seconds the caller must sleep first."""

        def _mutate(st):
            wait = self._lane_wait(
                st, lane_key, last_key, min_interval, windowed
            )
            if wait > self.max_wait_s:
                raise LocalRateLimitError(
                    f"gpt2agent shared rate limit: {lane_key} lane needs "
                    f"{wait:.0f}s wait (>{self.max_wait_s:.0f}s max). "
                    "Retry later or raise [rate_limit] max_wait_s/window."
                )
            ts = time.time() + wait
            st[last_key] = ts
            if windowed:
                st[lane_key].append(ts)
            return wait

        return self._locked_state(_mutate)

    async def acquire_conversation(self, model: str | None = None) -> None:
        """Gate a conversation POST. Raises UsageLimitError when the model is
        in a known upstream cooldown; LocalRateLimitError when the shared
        budget is exhausted; otherwise sleeps the required wait."""
        import asyncio

        if not self.enabled:
            return
        if model:
            until = self.cooldown_for(f"model:{model}")
            if until:
                from gpt2agent.backend import UsageLimitError

                reset_iso = datetime.fromtimestamp(until).isoformat()
                raise UsageLimitError(
                    f"{model} is rate-limited on this account until "
                    f"{reset_iso} (seen by another caller on this host). "
                    "Try a different model or browser=True.",
                    resets_after=reset_iso,
                )
        until429 = self.cooldown_for("http429:conversation")
        if until429:
            wait429 = until429 - time.time()
            if wait429 > self.max_wait_s:
                raise LocalRateLimitError(
                    "gpt2agent shared rate limit: upstream returned HTTP 429 "
                    f"recently; cooling down {wait429:.0f}s "
                    f"(>{self.max_wait_s:.0f}s max)."
                )
            await asyncio.sleep(wait429)
        wait = self._reserve(
            "conv_requests", "last_conv", self.min_interval_s, windowed=True
        )
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0.05, 0.5))

    def acquire_read(self) -> None:
        """Gate a bookkeeping GET/POST — light min-interval pacing only."""
        if not self.enabled:
            return
        until = self.cooldown_for("http429:read")
        if until:
            time.sleep(min(until - time.time(), self.max_wait_s))
        wait = self._reserve(
            "read_requests", "last_read", self.read_min_interval_s,
            windowed=False,
        )
        if wait > 0:
            time.sleep(wait)


_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_limiter(config: dict | None = None) -> RateLimiter:
    """Process-wide singleton — first caller's config wins."""
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = RateLimiter(config)
        return _limiter


def reset_limiter() -> None:
    """Tests only."""
    global _limiter
    with _limiter_lock:
        _limiter = None
