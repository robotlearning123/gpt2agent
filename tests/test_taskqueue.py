"""taskqueue.py — shared file-backed task queue mechanics."""

import json
import time
from pathlib import Path

from gpt2agent.taskqueue import TaskQueue


def _q(tmp_path: Path, worker: str = "w1") -> TaskQueue:
    q = TaskQueue(dir=tmp_path)
    q.worker_id = worker
    return q


def test_submit_and_get(tmp_path: Path) -> None:
    q = _q(tmp_path)
    t = q.submit("chat", {"prompt": "hi"})
    assert t["status"] == "queued"
    assert t["id"]
    got = q.get(t["id"])
    assert got["payload"] == {"prompt": "hi"}
    assert (tmp_path / f"{t['id']}.json").exists()


def test_claim_next_fifo_and_marks_claimed(tmp_path: Path) -> None:
    q = _q(tmp_path)
    a = q.submit("chat", {"prompt": "a"})
    b = q.submit("chat", {"prompt": "b"})
    claimed = q.claim_next()
    assert claimed["id"] == a["id"]
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "w1"
    assert claimed["attempts"] == 1
    # second claim gets the next task
    assert q.claim_next()["id"] == b["id"]
    assert q.claim_next() is None


def test_claim_race_two_workers_no_double_claim(tmp_path: Path) -> None:
    q1, q2 = _q(tmp_path, "w1"), _q(tmp_path, "w2")
    q1.submit("chat", {"prompt": "x"})
    c1 = q1.claim_next()
    assert c1["claimed_by"] == "w1"
    # the file now says claimed — the other process sees nothing runnable
    assert q2.claim_next() is None


def test_waiting_task_not_claimed_until_not_before(tmp_path: Path) -> None:
    q = _q(tmp_path)
    t = q.submit("chat", {"prompt": "x"})
    q.wait_until(t["id"], time.time() + 3600, "quota resets")
    assert q.claim_next() is None
    # past the wait → claimable again
    q.wait_until(t["id"], time.time() - 1, "")
    assert q.claim_next()["id"] == t["id"]


def test_wait_until_parses_iso(tmp_path: Path) -> None:
    q = _q(tmp_path)
    t = q.submit("chat", {"prompt": "x"})
    out = q.wait_until(t["id"], "2999-01-01T00:00:00Z", "cap")
    assert out["status"] == "waiting"
    assert out["not_before"] > time.time()


def test_complete_fail_cancel(tmp_path: Path) -> None:
    q = _q(tmp_path)
    t = q.submit("chat", {"prompt": "x"})
    q.complete(t["id"], {"text": "ok"})
    assert q.get(t["id"])["status"] == "done"
    assert q.get(t["id"])["result"] == {"text": "ok"}

    t2 = q.submit("chat", {"prompt": "y"})
    q.fail(t2["id"], "boom")
    assert q.get(t2["id"])["error"] == "boom"

    t3 = q.submit("chat", {"prompt": "z"})
    q.cancel(t3["id"])
    assert q.get(t3["id"])["status"] == "cancelled"
    # terminal tasks are never claimed
    assert q.claim_next() is None


def test_requeue_stale_claims(tmp_path: Path) -> None:
    q = _q(tmp_path)
    t = q.submit("chat", {"prompt": "x"})
    q.claim_next()
    # fresh claim → not stale
    assert q.requeue_stale(stale_s=60) == 0
    # age the claim
    path = tmp_path / f"{t['id']}.json"
    data = json.loads(path.read_text())
    data["claimed_at"] = time.time() - 3600
    path.write_text(json.dumps(data))
    assert q.requeue_stale(stale_s=60) == 1
    assert q.get(t["id"])["status"] == "queued"


def test_corrupt_file_tolerated(tmp_path: Path) -> None:
    q = _q(tmp_path)
    (tmp_path / "bad.json").write_text("{not json")
    assert q.list() == []
    assert q.claim_next() is None


def test_bad_task_id_rejected(tmp_path: Path) -> None:
    q = _q(tmp_path)
    assert q.get("../etc/passwd") is None
    assert q.get("") is None
