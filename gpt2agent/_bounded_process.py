from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


class BoundedProcessError(RuntimeError):
    def __init__(self, code: Literal["timeout", "output_too_large"]) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _WindowsJob:
    """Own a Windows process tree through a kill-on-close Job Object."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    @classmethod
    def create(cls) -> _WindowsJob:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        return cls(handle)

    def assign(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        process_handle = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process_handle)

    def close(self) -> None:
        if self._handle is None:
            return
        import ctypes
        from ctypes import wintypes

        handle = self._handle
        self._handle = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


def _resume_windows_process(pid: int) -> None:
    """Resume the sole initial thread of a CREATE_SUSPENDED process."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    thread_id: int | None = None
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.th32OwnerProcessID == pid:
                thread_id = entry.th32ThreadID
                break
            has_entry = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if thread_id is None:
        raise ctypes.WinError(1168)

    thread_handle = kernel32.OpenThread(0x0002, False, thread_id)
    if not thread_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel32.ResumeThread(thread_handle) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(thread_handle)


async def _read_limited(
    stream: asyncio.StreamReader | None, max_output_bytes: int
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(min(65_536, max_output_bytes + 1))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > max_output_bytes:
            raise BoundedProcessError("output_too_large")
        chunks.append(chunk)


async def _discard_stream(stream: asyncio.StreamReader | None) -> None:
    """Drain remaining output without retaining it in memory."""
    if stream is None:
        return
    while await stream.read(65_536):
        pass


async def _terminate_process_group(
    process: asyncio.subprocess.Process, windows_job: _WindowsJob | None
) -> None:
    cleanup_tasks = [
        asyncio.create_task(process.wait()),
        asyncio.create_task(_discard_stream(process.stdout)),
        asyncio.create_task(_discard_stream(process.stderr)),
    ]
    if os.name == "posix":
        deadline = asyncio.get_running_loop().time() + 1.0
        try:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            while True:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                await asyncio.sleep(min(0.05, remaining))
        finally:
            await asyncio.gather(*cleanup_tasks)
        return

    if windows_job is not None:
        windows_job.close()
    elif process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    cleanup = asyncio.gather(*cleanup_tasks)
    try:
        await asyncio.wait_for(asyncio.shield(cleanup), timeout=1.0)
    except asyncio.TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await cleanup


async def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
) -> ProcessResult:
    """Run one argv without a shell and own its complete process tree."""
    windows_job: _WindowsJob | None = None
    creation: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        windows_job = _WindowsJob.create()
        creation = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | 0x00000004  # CREATE_SUSPENDED; assign the Job before child code runs.
        }
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **creation,
        )
    except BaseException:
        if windows_job is not None:
            windows_job.close()
        raise
    if windows_job is not None:
        try:
            windows_job.assign(process.pid)
            _resume_windows_process(process.pid)
        except BaseException:
            windows_job.close()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise

    tasks = [
        asyncio.create_task(process.wait()),
        asyncio.create_task(_read_limited(process.stdout, max_output_bytes)),
        asyncio.create_task(_read_limited(process.stderr, max_output_bytes)),
    ]
    try:
        returncode, stdout, stderr = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=timeout_seconds
        )
        result = ProcessResult(returncode, stdout, stderr)
        if os.name == "posix":
            await _terminate_process_group(process, windows_job)
        return result
    except asyncio.TimeoutError as exc:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _terminate_process_group(process, windows_job)
        raise BoundedProcessError("timeout") from exc
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _terminate_process_group(process, windows_job)
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if windows_job is not None:
            windows_job.close()
