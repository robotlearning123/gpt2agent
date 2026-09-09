from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from gpt2agent._bounded_process import (
    BoundedProcessError,
    ProcessResult,
    run_bounded_process,
)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            if stat_path.read_text().split()[2] == "Z":
                return False
        except (FileNotFoundError, ProcessLookupError):
            return False
    return True


async def _wait_for_file(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"timed out waiting for {path}")


async def _wait_for_pid_to_stop(pid: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"process {pid} was still running")


def _assert_code_only(
    error: BoundedProcessError, expected_code: str
) -> None:
    assert error.code == expected_code
    assert error.__dict__ == {"code": expected_code}


@pytest.mark.asyncio
async def test_run_bounded_process_captures_separate_streams(tmp_path: Path) -> None:
    result = await run_bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5.0,
        max_output_bytes=1024,
    )

    assert result == ProcessResult(0, b"out\n", b"err\n")


@pytest.mark.asyncio
async def test_run_bounded_process_disconnects_stdin(tmp_path: Path) -> None:
    result = await run_bounded_process(
        [sys.executable, "-c", "import sys; print(sys.stdin.read() == '')"],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5.0,
        max_output_bytes=1024,
    )

    assert result.stdout == b"True\n"


@pytest.mark.asyncio
async def test_run_bounded_process_caps_output_while_reading(tmp_path: Path) -> None:
    with pytest.raises(BoundedProcessError) as caught:
        await run_bounded_process(
            [sys.executable, "-c", "print('x' * 4096)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )

    _assert_code_only(caught.value, "output_too_large")


@pytest.mark.asyncio
async def test_run_bounded_process_times_out_without_waiting_for_child(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    with pytest.raises(BoundedProcessError) as caught:
        await run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=0.2,
            max_output_bytes=1024,
        )

    _assert_code_only(caught.value, "timeout")
    assert time.monotonic() - started < 5


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
@pytest.mark.asyncio
async def test_run_bounded_process_timeout_kills_sigterm_resistant_grandchild(
    tmp_path: Path,
) -> None:
    grandchild_pid_file = tmp_path / "grandchild.pid"
    grandchild_code = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
        "time.sleep(30)"
    )

    try:
        with pytest.raises(BoundedProcessError) as caught:
            await run_bounded_process(
                [sys.executable, "-c", parent_code],
                cwd=tmp_path,
                env=os.environ.copy(),
                timeout_seconds=0.5,
                max_output_bytes=1024,
            )
        assert caught.value.code == "timeout"
        await _wait_for_file(grandchild_pid_file)
        grandchild_pid = int(grandchild_pid_file.read_text())
        await _wait_for_pid_to_stop(grandchild_pid)
    finally:
        if grandchild_pid_file.exists():
            grandchild_pid = int(grandchild_pid_file.read_text())
            if _pid_is_running(grandchild_pid):
                os.kill(grandchild_pid, signal.SIGKILL)


@pytest.mark.parametrize("returncode", [0, 7])
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
@pytest.mark.asyncio
async def test_run_bounded_process_normal_return_cleans_up_detached_descendant(
    tmp_path: Path, returncode: int
) -> None:
    descendant_pid_file = tmp_path / f"descendant-{returncode}.pid"
    descendant_code = "import time; time.sleep(30)"
    parent_code = (
        "import pathlib, subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {descendant_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(descendant_pid_file)!r}).write_text(str(child.pid)); "
        "print('out'); print('err', file=sys.stderr); "
        f"sys.exit({returncode})"
    )

    try:
        result = await run_bounded_process(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )
        await _wait_for_file(descendant_pid_file)
        descendant_pid = int(descendant_pid_file.read_text())

        assert result == ProcessResult(returncode, b"out\n", b"err\n")
        assert not _pid_is_running(descendant_pid)
    finally:
        if descendant_pid_file.exists():
            descendant_pid = int(descendant_pid_file.read_text())
            if _pid_is_running(descendant_pid):
                os.kill(descendant_pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX leak check")
@pytest.mark.asyncio
async def test_run_bounded_process_output_cap_stops_live_producer(
    tmp_path: Path,
) -> None:
    producer_pid_file = tmp_path / "producer.pid"
    producer_code = (
        "import os, pathlib, sys; "
        f"pathlib.Path({str(producer_pid_file)!r}).write_text(str(os.getpid())); "
        "chunk = b'x' * 4096; "
        "[(sys.stdout.buffer.write(chunk), sys.stdout.buffer.flush()) "
        "for _ in iter(int, 1)]"
    )

    try:
        with pytest.raises(BoundedProcessError) as caught:
            await run_bounded_process(
                [sys.executable, "-c", producer_code],
                cwd=tmp_path,
                env=os.environ.copy(),
                timeout_seconds=5.0,
                max_output_bytes=1024,
            )
        assert caught.value.code == "output_too_large"
        await _wait_for_file(producer_pid_file)
        producer_pid = int(producer_pid_file.read_text())
        await _wait_for_pid_to_stop(producer_pid)
    finally:
        if producer_pid_file.exists():
            producer_pid = int(producer_pid_file.read_text())
            if _pid_is_running(producer_pid):
                os.kill(producer_pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX leak check")
@pytest.mark.asyncio
async def test_run_bounded_process_caller_cancellation_cleans_up_child(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "cancelled-child.pid"
    child_code = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    task = asyncio.create_task(
        run_bounded_process(
            [sys.executable, "-c", child_code],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=20.0,
            max_output_bytes=1024,
        )
    )

    await _wait_for_file(child_pid_file)
    child_pid = int(child_pid_file.read_text())
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_for_pid_to_stop(child_pid)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if _pid_is_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_run_bounded_process_drains_large_streams_concurrently(
    tmp_path: Path,
) -> None:
    stream_size = 256 * 1024
    child_code = (
        "import sys, threading; "
        f"size = {stream_size}; "
        "threads = ["
        "threading.Thread(target=lambda: (sys.stdout.buffer.write(b'o' * size), "
        "sys.stdout.buffer.flush())), "
        "threading.Thread(target=lambda: (sys.stderr.buffer.write(b'e' * size), "
        "sys.stderr.buffer.flush()))]; "
        "[thread.start() for thread in threads]; "
        "[thread.join() for thread in threads]"
    )

    result = await run_bounded_process(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5.0,
        max_output_bytes=stream_size,
    )

    assert result.stdout == b"o" * stream_size
    assert result.stderr == b"e" * stream_size


@pytest.mark.asyncio
async def test_run_bounded_process_applies_cap_independently_to_stderr(
    tmp_path: Path,
) -> None:
    with pytest.raises(BoundedProcessError) as caught:
        await run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; print('small'); sys.stderr.write('e' * 4096)",
            ],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=5.0,
            max_output_bytes=1024,
        )

    _assert_code_only(caught.value, "output_too_large")
