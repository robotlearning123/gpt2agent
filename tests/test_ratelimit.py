"""Tests for gpt2agent/ratelimit.py — the shared, file-backed budget.

The conftest sets GPT2AGENT_RATELIMIT_OFF=1 globally; these tests construct
RateLimiter instances directly (bypassing the env check via explicit
``enabled=True`` config is not possible — the env wins), so each test
temporarily clears the env var.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from gpt2agent.backend import UsageLimitError
from gpt2agent.ratelimit import LocalRateLimitError, RateLimiter


@pytest.fixture()
def limiter(tmp_path, monkeypatch):
    monkeypatch.delenv("GPT2AGENT_RATELIMIT_OFF", raising=False)
    cfg = {
        "rate_limit": {
            "enabled": True,
            "min_interval_s": 5.0,
            "read_min_interval_s": 2.0,
            "max_per_window": 3,
            "window_s": 3600.0,
            "max_wait_s": 30.0,
        }
    }
    return RateLimiter(cfg, state_path=tmp_path / "state.json")


@pytest.fixture()
def zero_wait_limiter(tmp_path, monkeypatch):
    """min_interval=0 so tests never actually sleep."""
    monkeypatch.delenv("GPT2AGENT_RATELIMIT_OFF", raising=False)
    cfg = {
        "rate_limit": {
            "enabled": True,
            "min_interval_s": 0.0,
            "read_min_interval_s": 0.0,
            "max_per_window": 3,
            "window_s": 3600.0,
            "max_wait_s": 5.0,
        }
    }
    return RateLimiter(cfg, state_path=tmp_path / "state.json")


def test_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GPT2AGENT_RATELIMIT_OFF", "1")
    lim = RateLimiter({"rate_limit": {"enabled": True}},
                      state_path=tmp_path / "s.json")
    assert lim.enabled is False


def test_min_interval_pacing(zero_wait_limiter):
    # First request waits 0; second reserved 5s out... but our zero-wait
    # limiter has min_interval 0, so use the paced fixture instead.
    cfg = {
        "rate_limit": {
            "enabled": True,
            "min_interval_s": 100.0,
            "max_per_window": 100,
            "window_s": 3600.0,
            "max_wait_s": 500.0,
        }
    }
    lim = RateLimiter(cfg, state_path=zero_wait_limiter.state_path)
    wait1 = lim._reserve("conv_requests", "last_conv", lim.min_interval_s, True)
    wait2 = lim._reserve("conv_requests", "last_conv", lim.min_interval_s, True)
    assert wait1 == 0.0
    assert 99.0 < wait2 <= 100.5


def test_window_cap_raises(zero_wait_limiter):
    for _ in range(3):
        zero_wait_limiter._reserve(
            "conv_requests", "last_conv", 0.0, windowed=True
        )
    with pytest.raises(LocalRateLimitError):
        zero_wait_limiter._reserve(
            "conv_requests", "last_conv", 0.0, windowed=True
        )


def test_cooldown_blocks_model(zero_wait_limiter):
    future = time.time() + 7200
    zero_wait_limiter.note_cooldown("model:gpt-6-pro", future)
    with pytest.raises(UsageLimitError, match="gpt-6-pro"):
        asyncio.run(zero_wait_limiter.acquire_conversation("gpt-6-pro"))
    # A different model is unaffected.
    asyncio.run(zero_wait_limiter.acquire_conversation("gpt-5-6"))


def test_cooldown_iso_format(zero_wait_limiter):
    zero_wait_limiter.note_cooldown(
        "feature:deep_research", "2099-01-01T00:00:00+00:00"
    )
    assert zero_wait_limiter.cooldown_for("feature:deep_research") is not None


def test_expired_cooldown_ignored(zero_wait_limiter):
    zero_wait_limiter.note_cooldown("model:x", time.time() - 10)
    assert zero_wait_limiter.cooldown_for("model:x") is None


def test_state_shared_across_instances(tmp_path, monkeypatch):
    """A second RateLimiter on the same state file sees the cooldown — the
    whole point for multi-agent fleets."""
    monkeypatch.delenv("GPT2AGENT_RATELIMIT_OFF", raising=False)
    path = tmp_path / "shared.json"
    a = RateLimiter({"rate_limit": {"enabled": True}}, state_path=path)
    b = RateLimiter({"rate_limit": {"enabled": True}}, state_path=path)
    a.note_cooldown("model:gpt-6-pro", time.time() + 3600)
    assert b.cooldown_for("model:gpt-6-pro") is not None


def test_state_survives_corrupt_file(zero_wait_limiter):
    zero_wait_limiter.state_path.write_text("{not json")
    # Should not raise — falls back to empty state.
    assert zero_wait_limiter.cooldown_for("model:x") is None


def test_read_lane_records(zero_wait_limiter):
    zero_wait_limiter.acquire_read()
    st = json.loads(zero_wait_limiter.state_path.read_text())
    assert st["last_read"] > 0


def test_429_backoff_escalates(zero_wait_limiter):
    lim = zero_wait_limiter
    lim.note_429("read")
    first = lim.cooldown_for("http429:read")
    assert first and first - time.time() <= 61
    lim.note_429("read")
    second = lim.cooldown_for("http429:read")
    assert second > first  # second strike pushes the cooldown further out
    lim.note_429("read")
    lim.note_429("read")
    lim.note_429("read")
    fifth = lim.cooldown_for("http429:read")
    assert fifth - time.time() <= 481  # capped at 480s


def test_429_streak_clears_on_success(zero_wait_limiter):
    lim = zero_wait_limiter
    lim.note_429("read")
    lim.note_429("read")
    lim.clear_429_streak("read")
    lim.note_429("read")
    # Back at the base 60s cooldown, not the escalated one.
    assert lim.cooldown_for("http429:read") - time.time() <= 61


def test_breaker_trips_after_threshold(zero_wait_limiter):
    lim = zero_wait_limiter
    assert lim.note_failure("mint", threshold=3, cooldown_s=600) is False
    assert lim.note_failure("mint", threshold=3, cooldown_s=600) is False
    assert lim.note_failure("mint", threshold=3, cooldown_s=600) is True
    assert lim.breaker_open("mint") is not None
    lim.note_success("mint")
    assert lim.breaker_open("mint") is None


def test_breaker_shared_across_instances(tmp_path, monkeypatch):
    monkeypatch.delenv("GPT2AGENT_RATELIMIT_OFF", raising=False)
    path = tmp_path / "s.json"
    a = RateLimiter({"rate_limit": {"enabled": True}}, state_path=path)
    b = RateLimiter({"rate_limit": {"enabled": True}}, state_path=path)
    for _ in range(3):
        a.note_failure("mint", threshold=3, cooldown_s=600)
    assert b.breaker_open("mint") is not None
