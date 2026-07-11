from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_account_gate_runtime.sh"
RELEASE_TAG = "20260623"
ASSET_NAME = (
    "cpython-3.12.13+20260623-x86_64-unknown-linux-gnu-"
    "install_only_stripped.tar.gz"
)
ARCHIVE_SIZE = 34_159_178
ARCHIVE_SHA256 = "10a452caac7041357805f0c19a60576df53f1ab06d1abfc9200f1f0157cb3bd1"
PYTHON_SHA256 = "9544d2a29138833e6177d45dbc57468d37710b5080c901fbb579d53f251cdd6f"


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Keep secure-ancestry fixtures off world-writable /tmp ancestors."""
    home = Path.home().resolve(strict=True)
    directory = Path(tempfile.mkdtemp(prefix=".gpt2agent-installer-test.", dir=home))
    directory.chmod(0o700)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_archive(
    tmp_path: Path,
    *,
    valid_tar: bool = True,
    version: str = "3.12.13",
    ready_file: Path | None = None,
    pause_seconds: int = 0,
    pause_base: Path | None = None,
) -> tuple[Path, str]:
    source = tmp_path / "archive-source"
    python = source / "bin" / "python3.12"
    python.parent.mkdir(parents=True)
    lines = ["#!/usr/bin/bash -p", "base=${0%/bin/python3.12}"]
    if ready_file is not None:
        hook = (
            f"/usr/bin/touch -- {str(ready_file)!r}",
            f"/usr/bin/sleep {pause_seconds}",
        )
        if pause_base is None:
            lines.extend(hook)
        else:
            lines.append(f"if [[ \"$base\" == {str(pause_base)!r} ]]; then")
            lines.extend(f"  {line}" for line in hook)
            lines.append("fi")
    lines.append(f"printf '%s\\n' cpython {version} linux x86_64 \"$base\" \"$0\"")
    python.write_text("\n".join(lines) + "\n", encoding="utf-8")
    python.chmod(0o755)
    archive = tmp_path / ASSET_NAME
    if valid_tar:
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(source, arcname="python")
    else:
        archive.write_bytes(b"not a tar archive")
    return archive, _sha256(python)


def _patched_installer(
    tmp_path: Path,
    *,
    archive_size: int,
    archive_sha256: str,
    python_sha256: str,
) -> Path:
    contents = INSTALLER.read_text(encoding="utf-8")
    assert contents.count(str(ARCHIVE_SIZE)) == 1
    assert contents.count(ARCHIVE_SHA256) == 1
    assert contents.count(PYTHON_SHA256) == 1
    contents = contents.replace(str(ARCHIVE_SIZE), str(archive_size))
    contents = contents.replace(ARCHIVE_SHA256, archive_sha256)
    contents = contents.replace(PYTHON_SHA256, python_sha256)
    installer = tmp_path / "install-account-runtime"
    installer.write_text(contents, encoding="utf-8")
    installer.chmod(0o700)
    return installer


def _private_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "runtime-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return parent


def _run(
    installer: Path,
    archive: Path,
    destination: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(env or {})
    return subprocess.run(
        [
            str(installer),
            "--archive",
            str(archive),
            "--destination",
            str(destination),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _start(
    installer: Path,
    archive: Path,
    destination: Path,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            str(installer),
            "--archive",
            str(archive),
            "--destination",
            str(destination),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"installer exited before synchronization: {process.returncode}\n"
                f"stdout={stdout!r}\nstderr={stderr!r}"
            )
        time.sleep(0.01)
    process.kill()
    stdout, stderr = process.communicate()
    raise AssertionError(f"installer synchronization timed out: {stdout!r} {stderr!r}")


def _scratch_entries(parent: Path, destination: Path) -> list[Path]:
    return list(parent.glob(f".{destination.name}.gpt2agent-runtime-install.*"))


def test_installer_locks_the_reviewed_official_archive_and_python() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/bash -p\n")
    assert "set +o posix" in text
    assert "POSIXLY_CORRECT" in text
    assert f"EXPECTED_ARCHIVE_SIZE={ARCHIVE_SIZE}" in text
    assert ARCHIVE_SHA256 in text
    assert PYTHON_SHA256 in text
    assert ASSET_NAME in text
    assert RELEASE_TAG in text
    assert "--strip-components=1" in text
    assert '(3, 12, 13)' in text


def test_installer_uses_closed_path_ignores_bash_env_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    archive, python_sha256 = _fixture_archive(tmp_path)
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256=python_sha256,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "cpython-3.12.13-linux-x86_64"
    canary = tmp_path / "hostile-command-ran"
    hostile = tmp_path / "hostile-path"
    hostile.mkdir(mode=0o700)
    for name in (
        "bash",
        "chmod",
        "cp",
        "dirname",
        "env",
        "id",
        "install",
        "mktemp",
        "mv",
        "realpath",
        "rm",
        "sha256sum",
        "stat",
        "tar",
        "uname",
    ):
        command = hostile / name
        command.write_text(
            f"#!/usr/bin/bash -p\nprintf '%s\\n' {name!r} >> {str(canary)!r}\nexit 91\n",
            encoding="utf-8",
        )
        command.chmod(0o700)
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        f"printf '%s\\n' BASH_ENV >> {str(canary)!r}\nexit 92\n",
        encoding="utf-8",
    )

    result = _run(
        installer,
        archive,
        destination,
        env={
            "PATH": str(hostile),
            "BASH_ENV": str(bash_env),
            "ENV": str(bash_env),
            "POSIXLY_CORRECT": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{destination}\n"
    assert not canary.exists()
    assert destination.is_dir() and not destination.is_symlink()
    assert destination.stat().st_mode & 0o777 == 0o700
    python = destination / "bin" / "python3.12"
    assert python.is_file() and not python.is_symlink()
    assert os.access(python, os.X_OK)
    assert _sha256(python) == python_sha256
    assert not (destination / "python").exists()
    assert not _scratch_entries(parent, destination)


def test_installer_rejects_wrong_archive_size_before_creating_scratch(tmp_path: Path) -> None:
    archive, _python_sha256 = _fixture_archive(tmp_path, valid_tar=False)
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"

    result = _run(INSTALLER, archive, destination)

    assert result.returncode != 0
    assert "archive size" in result.stderr
    assert "extract" not in result.stderr
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


def test_installer_rejects_wrong_archive_checksum_before_extraction(tmp_path: Path) -> None:
    archive, _python_sha256 = _fixture_archive(tmp_path, valid_tar=False)
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256="0" * 64,
        python_sha256="0" * 64,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"

    result = _run(installer, archive, destination)

    assert result.returncode != 0
    assert "archive checksum" in result.stderr
    assert "extract" not in result.stderr
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


def test_installer_cleans_extraction_failure_without_publishing(tmp_path: Path) -> None:
    archive, _python_sha256 = _fixture_archive(tmp_path, valid_tar=False)
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256="0" * 64,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"

    result = _run(installer, archive, destination)

    assert result.returncode != 0
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


def test_installer_cleans_python_digest_failure_without_publishing(tmp_path: Path) -> None:
    archive, _python_sha256 = _fixture_archive(tmp_path)
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256="0" * 64,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"

    result = _run(installer, archive, destination)

    assert result.returncode != 0
    assert "Python executable checksum" in result.stderr
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


def test_installer_rejects_mislabeled_python_version_without_publishing(
    tmp_path: Path,
) -> None:
    archive, python_sha256 = _fixture_archive(tmp_path, version="3.12.3")
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256=python_sha256,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"

    result = _run(installer, archive, destination)

    assert result.returncode != 0
    assert "CPython 3.12.13 for Linux x86_64" in result.stderr
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


def test_installer_preserves_existing_destination(tmp_path: Path) -> None:
    archive, python_sha256 = _fixture_archive(tmp_path)
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256=python_sha256,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"
    destination.mkdir(mode=0o700)
    marker = destination / "keep"
    marker.write_text("existing\n", encoding="utf-8")

    result = _run(installer, archive, destination)

    assert result.returncode != 0
    assert "must not already exist" in result.stderr
    assert marker.read_text(encoding="utf-8") == "existing\n"
    assert not _scratch_entries(parent, destination)


def test_installer_does_not_replace_destination_created_during_verification(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "staged-verification-ready"
    archive, python_sha256 = _fixture_archive(
        tmp_path,
        ready_file=ready,
        pause_seconds=2,
    )
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256=python_sha256,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"
    process = _start(installer, archive, destination)
    _wait_for(ready, process)
    destination.mkdir(mode=0o700)
    marker = destination / "competitor-owned"
    marker.write_text("preserve\n", encoding="utf-8")

    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "appeared during installation" in stderr
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert not _scratch_entries(parent, destination)


def test_installer_signal_after_publication_removes_only_its_runtime(
    tmp_path: Path,
) -> None:
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"
    ready = tmp_path / "published-verification-ready"
    archive, python_sha256 = _fixture_archive(
        tmp_path,
        ready_file=ready,
        pause_seconds=3,
        pause_base=destination,
    )
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256=python_sha256,
    )
    process = _start(installer, archive, destination)
    _wait_for(ready, process)
    published_identity = (destination.stat().st_dev, destination.stat().st_ino)

    process.terminate()
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, (stdout, stderr)
    assert published_identity[1] > 0
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


def test_installer_signal_does_not_delete_concurrent_replacement(tmp_path: Path) -> None:
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"
    ready = tmp_path / "published-replacement-ready"
    archive, python_sha256 = _fixture_archive(
        tmp_path,
        ready_file=ready,
        pause_seconds=3,
        pause_base=destination,
    )
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256=python_sha256,
    )
    process = _start(installer, archive, destination)
    _wait_for(ready, process)
    displaced = parent / "displaced-runtime"
    destination.rename(displaced)
    destination.mkdir(mode=0o700)
    marker = destination / "replacement-owned"
    marker.write_text("preserve\n", encoding="utf-8")

    process.terminate()
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, (stdout, stderr)
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert displaced.is_dir()
    assert not _scratch_entries(parent, destination)


@pytest.mark.parametrize("control", ("\x01", "\t", "\n", "\r", "\x1b", "\x7f"))
def test_installer_rejects_ascii_controls_in_destination(
    tmp_path: Path,
    control: str,
) -> None:
    archive, _python_sha256 = _fixture_archive(tmp_path)
    parent = _private_parent(tmp_path)
    destination = parent / f"run{control}time"

    result = _run(INSTALLER, archive, destination)

    assert result.returncode != 0
    assert "ASCII control" in result.stderr
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


@pytest.mark.parametrize("control", ("\x01", "\t", "\n", "\r", "\x1b", "\x7f"))
def test_installer_rejects_ascii_controls_in_archive_path(
    tmp_path: Path,
    control: str,
) -> None:
    archive, _python_sha256 = _fixture_archive(tmp_path)
    controlled_archive = archive.with_name(f"python{control}.tar.gz")
    archive.rename(controlled_archive)
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"

    result = _run(INSTALLER, controlled_archive, destination)

    assert result.returncode != 0
    assert "ASCII control" in result.stderr
    assert not destination.exists()
    assert not _scratch_entries(parent, destination)


@pytest.mark.parametrize("unsafe", ("relative", "parent-mode", "symlink"))
def test_installer_refuses_unsafe_destination(tmp_path: Path, unsafe: str) -> None:
    archive, python_sha256 = _fixture_archive(tmp_path)
    installer = _patched_installer(
        tmp_path,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256(archive),
        python_sha256=python_sha256,
    )
    parent = _private_parent(tmp_path)
    destination = parent / "runtime"
    if unsafe == "relative":
        destination = Path("relative-runtime")
    elif unsafe == "parent-mode":
        parent.chmod(0o770)
    else:
        target = tmp_path / "symlink-target"
        target.mkdir(mode=0o700)
        parent.rmdir()
        parent.symlink_to(target, target_is_directory=True)

    result = _run(installer, archive, destination)

    assert result.returncode != 0
    assert not destination.exists()
    assert not (ROOT / "relative-runtime").exists()
    if parent.is_dir() and not parent.is_symlink():
        assert not _scratch_entries(parent, destination)
