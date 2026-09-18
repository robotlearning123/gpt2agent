"""Shared file-backed task queue for gpt2agent.

Why: many agents share one ChatGPT account. Instead of every agent racing
the backend (and each burning a sentinel mint to rediscover the same quota
cap), callers submit work to ``~/.gpt2agent/tasks/<id>.json`` and whichever
gpt2agent server process is running drains the queue serially — under the
shared rate limiter, so pacing/cooldowns apply automatically.

Semantics:

* ``submit()`` writes a task file atomically (tmp + rename).
* ``claim_next()`` flock's a task file before marking it ``claimed`` —
  two gpt2agent processes can never execute the same task twice.
* A task that hits ``UsageLimitError`` is parked ``waiting`` until the
  upstream ``resets_after`` — queued heavy-DR jobs fire themselves after
  the monthly/hourly cap lifts.
* ``requeue_stale()`` rescues tasks whose worker died mid-claim.

Kill switch: ``GPT2AGENT_QUEUE_OFF=1`` (tests set this).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

try:  # POSIX file lock; absent on Windows
    import fcntl
except ImportError:  # pragma: no cover - windows
    fcntl = None

_TASK_DIR = Path.home() / ".gpt2agent" / "tasks"

QUEUED = "queued"
CLAIMED = "claimed"
WAITING = "waiting"   # parked until not_before (e.g. upstream reset)
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = {DONE, FAILED, CANCELLED}
RUNNABLE = {QUEUED, WAITING}

_CLAIM_STALE_S = 900.0  # claimed-but-silent for this long → requeue


def _now() -> float:
    return time.time()


def _parse_ts(value) -> float | None:
    """ISO-8601 or epoch → epoch seconds; None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class TaskQueue:
    """One JSON file per task under ``dir`` — atomic writes, flock claims."""

    def __init__(self, dir: Path | None = None) -> None:
        self.dir = Path(
            dir or os.environ.get("GPT2AGENT_TASK_DIR") or _TASK_DIR
        )
        self.enabled = not os.environ.get("GPT2AGENT_QUEUE_OFF")
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def _path(self, task_id: str) -> Path:
        # task ids are uuids — guard the join anyway
        if not task_id or "/" in task_id or ".." in task_id:
            raise ValueError(f"bad task id: {task_id!r}")
        return self.dir / f"{task_id}.json"

    def _write(self, path: Path, task: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(task, f)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read(self, path: Path) -> dict | None:
        try:
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _mutate_locked(self, path: Path, fn) -> dict | None:
        """flock the task file, apply ``fn(task)`` if it returns a dict,
        persist. Returns the (possibly updated) task or None."""
        if fcntl is None:
            task = self._read(path)
            if task is None:
                return None
            out = fn(task)
            if isinstance(out, dict):
                self._write(path, out)
                return out
            return task
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read()
                task = json.loads(raw) if raw.strip() else None
                if not isinstance(task, dict):
                    return None
                out = fn(task)
                if isinstance(out, dict):
                    self._write(path, out)
                    return out
                return task
            except json.JSONDecodeError:
                return None
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # ── API ──────────────────────────────────────────────────────────

    def submit(self, kind: str, payload: dict[str, Any]) -> dict:
        task = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "payload": payload,
            "status": QUEUED,
            "created_at": _now(),
            "not_before": 0.0,
            "claimed_by": None,
            "claimed_at": None,
            "result": None,
            "error": None,
            "attempts": 0,
        }
        self._write(self._path(task["id"]), task)
        return task

    def get(self, task_id: str) -> dict | None:
        try:
            return self._read(self._path(task_id))
        except ValueError:
            return None

    def list(self, status: str | None = None) -> list[dict]:
        if not self.dir.exists():
            return []
        tasks = []
        for p in sorted(self.dir.glob("*.json")):
            t = self._read(p)
            if t and (status is None or t.get("status") == status):
                tasks.append(t)
        tasks.sort(key=lambda t: t.get("created_at") or 0)
        return tasks

    def claim_next(self) -> dict | None:
        """Claim the oldest runnable task whose ``not_before`` has passed.
        flock ensures only one process wins; the claimed marker is written
        while the lock is held."""
        now = _now()
        for task in self.list():
            if task.get("status") not in RUNNABLE:
                continue
            if (task.get("not_before") or 0) > now:
                continue
            wid = self.worker_id

            def _claim(t: dict) -> dict | None:
                if t.get("status") not in RUNNABLE:
                    return None  # someone else claimed it first
                t["status"] = CLAIMED
                t["claimed_by"] = wid
                t["claimed_at"] = _now()
                t["attempts"] = int(t.get("attempts") or 0) + 1
                return t

            claimed = self._mutate_locked(self._path(task["id"]), _claim)
            if claimed and claimed.get("claimed_by") == wid:
                return claimed
        return None

    def complete(self, task_id: str, result: Any) -> dict | None:
        def _done(t: dict) -> dict:
            t["status"] = DONE
            t["result"] = result
            t["done_at"] = _now()
            return t

        return self._mutate_locked(self._path(task_id), _done)

    def fail(self, task_id: str, error: str) -> dict | None:
        def _fail(t: dict) -> dict:
            t["status"] = FAILED
            t["error"] = error
            t["done_at"] = _now()
            return t

        return self._mutate_locked(self._path(task_id), _fail)

    def cancel(self, task_id: str) -> dict | None:
        def _cancel(t: dict) -> dict | None:
            if t.get("status") in TERMINAL:
                return None
            t["status"] = CANCELLED
            t["done_at"] = _now()
            return t

        return self._mutate_locked(self._path(task_id), _cancel)

    def wait_until(self, task_id: str, until, reason: str = "") -> dict | None:
        """Park a task until ``until`` (ISO ts or epoch) — e.g. an upstream
        ``resets_after``. Falls back to +1h when the reset is unknown so a
        queued job retries instead of dying."""
        ts = _parse_ts(until) or (_now() + 3600)

        def _wait(t: dict) -> dict:
            t["status"] = WAITING
            t["not_before"] = ts
            t["claimed_by"] = None
            t["error"] = reason or t.get("error")
            return t

        return self._mutate_locked(self._path(task_id), _wait)

    def requeue_stale(self, stale_s: float = _CLAIM_STALE_S) -> int:
        """Rescue tasks whose worker died mid-claim. Returns count."""
        now = _now()
        n = 0
        for task in self.list(status=CLAIMED):
            if now - (task.get("claimed_at") or 0) < stale_s:
                continue

            def _requeue(t: dict) -> dict | None:
                if t.get("status") != CLAIMED:
                    return None
                t["status"] = QUEUED
                t["claimed_by"] = None
                return t

            if self._mutate_locked(self._path(task["id"]), _requeue):
                n += 1
        return n


_queue: TaskQueue | None = None


def get_queue() -> TaskQueue:
    """Process-wide queue singleton (state lives on disk — every process
    sees the same tasks)."""
    global _queue
    if _queue is None:
        _queue = TaskQueue()
    return _queue
