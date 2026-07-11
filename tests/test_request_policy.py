"""Process-wide request limiting and per-route cooldown contracts."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from gpt2agent.errors import BackendHTTPError
from gpt2agent.request_policy import RequestPolicy, configured_max_in_flight


def test_concurrency_configuration_defaults_and_enforces_hard_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GPT2AGENT_MAX_IN_FLIGHT", raising=False)
    assert configured_max_in_flight() == 4

    for value in ("1", "8"):
        monkeypatch.setenv("GPT2AGENT_MAX_IN_FLIGHT", value)
        assert configured_max_in_flight() == int(value)

    for value in ("0", "9", "1.5", "many", ""):
        monkeypatch.setenv("GPT2AGENT_MAX_IN_FLIGHT", value)
        with pytest.raises(ValueError, match="GPT2AGENT_MAX_IN_FLIGHT"):
            configured_max_in_flight()


def test_global_overlap_never_exceeds_configured_limit() -> None:
    policy = RequestPolicy(max_in_flight=2, acquire_timeout=1.0)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def operation() -> None:
        nonlocal active, maximum
        with policy.request("GET", "/backend-api/example"):
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: operation(), range(24)))

    assert maximum == 2
    assert policy.max_in_flight == 2


def test_acquisition_fails_safely_within_bounded_timeout() -> None:
    policy = RequestPolicy(max_in_flight=1, acquire_timeout=0.02)

    with policy.request("GET", "/backend-api/busy"):
        started = time.monotonic()
        with pytest.raises(BackendHTTPError) as caught:
            with policy.request("GET", "/backend-api/other"):
                pytest.fail("unreachable")
        elapsed = time.monotonic() - started

    assert elapsed < 0.20
    assert caught.value.code == "temporarily_failed"
    assert caught.value.retryable is True
    assert caught.value.retry_after == pytest.approx(0.02)


@pytest.mark.parametrize("failure", [RuntimeError("boom"), KeyboardInterrupt()])
def test_permit_releases_after_exception_or_cancellation(
    failure: BaseException,
) -> None:
    policy = RequestPolicy(max_in_flight=1, acquire_timeout=0.01)

    with pytest.raises(type(failure)):
        with policy.request("POST", "/backend-api/example"):
            raise failure

    with policy.request("GET", "/backend-api/next"):
        pass


def test_retry_after_parses_numeric_http_date_invalid_and_caps() -> None:
    monotonic_now = [100.0]
    wall_now = 1_800_000_000.0
    policy = RequestPolicy(
        clock=lambda: monotonic_now[0],
        wall_clock=lambda: wall_now,
    )
    future = format_datetime(
        datetime.fromtimestamp(wall_now + 12, tz=timezone.utc), usegmt=True
    )

    assert policy.parse_retry_after("2.5") == pytest.approx(2.5)
    assert policy.parse_retry_after(future) == pytest.approx(12.0)
    assert policy.parse_retry_after("120") == pytest.approx(60.0)
    assert policy.parse_retry_after("not-a-delay") == pytest.approx(5.0)
    assert policy.parse_retry_after("-1") == pytest.approx(5.0)
    assert policy.parse_retry_after(None) == pytest.approx(5.0)


def test_cooldown_is_normalized_route_local_monotonic_and_later_wins() -> None:
    now = [50.0]
    policy = RequestPolicy(clock=lambda: now[0], wall_clock=lambda: 0.0)

    assert policy.activate_cooldown("/backend-api/a?private=one", "10") == 10.0
    now[0] += 2.0
    assert policy.activate_cooldown("https://chatgpt.com/backend-api/a?private=two", "3") == 8.0
    assert policy.activate_cooldown("/backend-api/a", "20") == 20.0

    with pytest.raises(BackendHTTPError) as caught:
        with policy.request("GET", "/backend-api/a?another=secret"):
            pytest.fail("unreachable")

    assert caught.value.route == "/backend-api/a"
    assert caught.value.retry_after == pytest.approx(20.0)
    with policy.request("GET", "/backend-api/b"):
        pass

    now[0] += 21.0
    with policy.request("GET", "/backend-api/a"):
        pass
