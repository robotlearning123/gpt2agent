from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap_account_gate.sh"
LOCK = ROOT / "requirements-account-gate.txt"
INPUT = ROOT / "requirements-account-gate.in"
EXPECTED_LOCK = {
    "certifi": "2026.6.17",
    "cffi": "2.1.0",
    "curl-cffi": "0.15.0",
    "markdown-it-py": "4.2.0",
    "mdurl": "0.1.2",
    "pip": "26.1.2",
    "pycparser": "3.0",
    "pygments": "2.20.0",
    "rich": "15.0.0",
}


def _remove_certifi_hashes(text: str) -> str:
    start = text.index("certifi==2026.6.17")
    end = text.index("    # via curl-cffi", start)
    return f"{text[:start]}certifi==2026.6.17\n{text[end:]}"


def _logical_requirements(text: str) -> list[str]:
    requirements: list[str] = []
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        requirements.append(current)
        current = ""
    if current:
        raise AssertionError("lock file has an unterminated continuation")
    return requirements


def _parse_hashed_lock(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    unsafe = re.compile(
        r"(?:^|\s)(?:--(?:index-url|extra-index-url|find-links|trusted-host)|-e|--editable)"
        r"(?:\s|=)|(?:@|://|git\+|\.\.?/)"
    )
    pattern = re.compile(
        r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)"
        r"(?:\s+--hash=sha256:[0-9a-f]{64})+$"
    )
    for requirement in _logical_requirements(text):
        if unsafe.search(requirement):
            raise AssertionError(f"unsafe requirement source: {requirement}")
        match = pattern.fullmatch(requirement)
        if match is None:
            raise AssertionError(f"requirement is not exactly pinned and hashed: {requirement}")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        if name in parsed:
            raise AssertionError(f"duplicate requirement: {name}")
        parsed[name] = match.group(2)
    return parsed


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o700)


def _fake_python(tmp_path: Path, *, fail_stage: str = "") -> tuple[Path, Path]:
    trusted_dir = tmp_path / "trusted-python"
    trusted_dir.mkdir(mode=0o700)
    log = trusted_dir / "commands.log"
    python = trusted_dir / "python3.12"
    quoted_log = shlex.quote(str(log))
    quoted_failure = shlex.quote(fail_stage)
    quoted_base = shlex.quote(str(trusted_dir))
    _write_executable(
        python,
        f"""#!/usr/bin/env bash
set -euo pipefail
log={quoted_log}
fail_stage={quoted_failure}
base={quoted_base}
{{
  printf 'exe=%s home=%s openai=%s pythonpath=%s args=' "$0" "${{HOME-}}" \
    "${{OPENAI_API_KEY-}}" "${{PYTHONPATH-}}"
  printf '%q ' "$@"
  printf '\n'
}} >> "$log"
if [[ "${{1-}}" == "-I" && "${{2-}}" == "-S" && "${{3-}}" == "-B" \
  && "${{4-}}" == "-c" ]]; then
  [[ "${{5-}}" == *"(3, 12, 13)"* ]]
  [[ "${{5-}}" == *"linux"* ]]
  [[ "${{5-}}" == *"x86_64"* ]]
  printf '%s\n' "$base"
  exit 0
fi
if [[ "${{1-}}" == "-I" && "${{2-}}" == "-S" && "${{3-}}" == "-B" \
  && "${{4-}}" == "-" ]]; then
  exec /usr/bin/python3 "$@"
fi
if [[ "${{1-}}" == "-I" && "${{2-}}" == "-S" && "${{3-}}" == "-B" \
  && "${{4-}}" == "-m" && "${{5-}}" == "venv" ]]; then
  destination="${{@: -1}}"
  mkdir -p "$destination/bin" "$destination/lib/python3.12/site-packages"
  cp "$0" "$destination/bin/python"
  chmod 700 "$destination/bin/python"
  [[ "$fail_stage" != venv ]]
  exit 0
fi
if [[ " $* " == *" -m pip "* && " $* " == *" install "* ]]; then
  printf 'fake pip install output\n'
  [[ "$fail_stage" != install ]]
  exit 0
fi
if [[ " $* " == *" -m pip "* && " $* " == *" check "* ]]; then
  printf 'fake pip check output\n'
  [[ "$fail_stage" != pip-check ]]
  exit 0
fi
if [[ " $* " == *"verify_account_receipt.py check-runtime"* ]]; then
  printf 'fake runtime check output\n'
  [[ "$fail_stage" != runtime-check ]]
  exit 0
fi
printf 'unexpected fake Python invocation\n' >&2
exit 97
""",
    )
    return python, log


def _private_parent(tmp_path: Path, name: str = "account-runtime") -> Path:
    parent = tmp_path / name
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return parent


def _run_bootstrap(
    python: Path,
    venv: Path,
    *,
    script: Path = BOOTSTRAP,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(extra_env or {})
    return subprocess.run(
        [str(script), "--python", str(python), "--venv", str(venv)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _isolated_bootstrap_tree(tmp_path: Path, lock_text: str) -> Path:
    tree = tmp_path / "isolated-tree"
    scripts = tree / "scripts"
    scripts.mkdir(parents=True)
    copied = scripts / BOOTSTRAP.name
    shutil.copy2(BOOTSTRAP, copied)
    copied.chmod(0o700)
    (tree / LOCK.name).write_text(lock_text, encoding="utf-8")
    (scripts / "verify_account_receipt.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    return copied


def test_account_gate_input_pins_only_direct_bootstrap_dependencies() -> None:
    assert INPUT.read_text(encoding="utf-8") == "pip==26.1.2\ncurl_cffi==0.15.0\n"


def test_account_gate_lock_has_exact_hashed_closure_and_provenance() -> None:
    text = LOCK.read_text(encoding="utf-8")

    assert "# Resolver: uv 0.9.18" in text
    assert "# Index: https://pypi.org/simple" in text
    assert "# Target: CPython 3.12.13 on x86_64-unknown-linux-gnu" in text
    assert _parse_hashed_lock(text) == EXPECTED_LOCK


def test_main_ci_validates_the_fresh_runtime_without_account_auth() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step_start = workflow.index("Validate fresh isolated account-gate runtime without auth")
    step_end = workflow.index("      - name:", step_start + 10)
    step = workflow[step_start:step_end]

    assert "scripts/bootstrap_account_gate.sh" in step
    assert "python -I -S -B -c" in step
    assert "os.path.realpath(sys.executable)" in step
    assert '--python "$ACCOUNT_GATE_PYTHON"' in step
    assert "/usr/bin/python" not in step
    assert "verify_account_receipt.py check-runtime" in step
    assert "--trusted-site-packages" in step
    assert "OPENAI_API_KEY" not in step
    assert "CHATGPT_ACCESS_TOKEN" not in step
    assert "GH_TOKEN" not in step


def test_main_ci_pins_the_reviewed_setup_python_patch() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    package_start = workflow.index("  package:")
    package = workflow[package_start:]

    assert 'python-version: "3.12.13"' in package


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("certifi==2026.6.17", "certifi>=2026.6.17", 1),
        _remove_certifi_hashes,
        lambda text: text.replace(
            "certifi==2026.6.17 \\",
            "certifi @ https://example.invalid/certifi.whl \\",
            1,
        ),
        lambda text: "--extra-index-url https://example.invalid/simple\n" + text,
    ],
    ids=("unpinned", "missing-hash", "direct-url", "extra-index"),
)
def test_bootstrap_rejects_unsafe_lock_before_creating_venv(
    tmp_path: Path, mutation
) -> None:
    python, _log = _fake_python(tmp_path)
    script = _isolated_bootstrap_tree(tmp_path, mutation(LOCK.read_text(encoding="utf-8")))
    parent = _private_parent(tmp_path)
    venv = parent / "venv"

    result = _run_bootstrap(python, venv, script=script)

    assert result.returncode != 0
    assert "invalid account-gate lock:" in result.stderr
    assert not venv.exists()
    assert not list(parent.glob(".gpt2agent-account-bootstrap.*"))


def test_bootstrap_uses_isolated_hash_locked_commands_in_order(tmp_path: Path) -> None:
    python, log = _fake_python(tmp_path)
    parent = _private_parent(tmp_path)
    venv = parent / "venv"

    result = _run_bootstrap(
        python,
        venv,
        extra_env={
            "OPENAI_API_KEY": "must-not-cross-bootstrap-boundary",
            "CHATGPT_ACCESS_TOKEN": "must-not-cross-bootstrap-boundary",
            "PYTHONPATH": "/attacker/pythonpath",
            "PIP_INDEX_URL": "https://example.invalid/simple",
        },
    )

    assert result.returncode == 0, result.stderr
    assert venv.is_dir()
    assert venv.stat().st_mode & 0o777 == 0o700
    lines = log.read_text(encoding="utf-8").splitlines()
    joined = "\n".join(lines)
    assert "OPENAI_API_KEY" not in joined
    assert "must-not-cross-bootstrap-boundary" not in joined
    assert "/attacker/pythonpath" not in joined
    assert all(" openai= pythonpath=" in line for line in lines)
    assert "-I -S -B -m venv --copies" in joined
    install_index = next(index for index, line in enumerate(lines) if " install " in line)
    check_index = next(index for index, line in enumerate(lines) if " check " in line)
    runtime_index = next(index for index, line in enumerate(lines) if "check-runtime" in line)
    assert install_index < check_index < runtime_index
    install = lines[install_index]
    for required in (
        "-I -B -m pip --isolated --disable-pip-version-check install",
        "--index-url https://pypi.org/simple",
        "--require-hashes",
        "--only-binary=:all:",
        "--no-deps",
        "--no-compile",
        "--no-cache-dir",
        "--no-input",
    ):
        assert required in install
    assert str(LOCK) not in install
    assert ".gpt2agent-account-bootstrap." in install
    assert f"--trusted-site-packages {venv}/lib/python3.12/site-packages" in lines[
        runtime_index
    ]
    assert result.stdout == f"{venv}/lib/python3.12/site-packages\n"


@pytest.mark.parametrize("fail_stage", ["venv", "install", "pip-check", "runtime-check"])
def test_bootstrap_removes_partial_venv_on_failure(tmp_path: Path, fail_stage: str) -> None:
    python, _log = _fake_python(tmp_path, fail_stage=fail_stage)
    parent = _private_parent(tmp_path)
    venv = parent / "venv"

    result = _run_bootstrap(python, venv)

    assert result.returncode != 0
    assert not venv.exists()
    assert not list(parent.glob(".gpt2agent-account-bootstrap.*"))


def test_bootstrap_refuses_relative_existing_or_unsafe_destinations(tmp_path: Path) -> None:
    python, _log = _fake_python(tmp_path)
    private = _private_parent(tmp_path, "private")
    existing = private / "existing"
    existing.mkdir()
    unsafe = _private_parent(tmp_path, "unsafe")
    unsafe.chmod(0o755)

    relative_result = _run_bootstrap(python, Path("relative-venv"))
    existing_result = _run_bootstrap(python, existing)
    unsafe_result = _run_bootstrap(python, unsafe / "venv")

    assert relative_result.returncode != 0
    assert existing_result.returncode != 0
    assert unsafe_result.returncode != 0
    assert not (ROOT / "relative-venv").exists()
    assert existing.is_dir()
    assert not (unsafe / "venv").exists()


def test_bootstrap_requires_canonical_cpython_31213_linux_x86_64(tmp_path: Path) -> None:
    python, _log = _fake_python(tmp_path)
    python.write_text(
        python.read_text(encoding="utf-8").replace(
            "  exit 0\nfi",
            "  exit 86\nfi",
            1,
        ),
        encoding="utf-8",
    )
    python.chmod(0o700)
    parent = _private_parent(tmp_path)

    wrong_version = _run_bootstrap(python, parent / "wrong-version")
    relative_python = _run_bootstrap(Path("python3.12"), parent / "relative-python")
    symlink = python.parent / "python-link"
    symlink.symlink_to(python)
    symlink_python = _run_bootstrap(symlink, parent / "symlink-python")

    assert wrong_version.returncode != 0
    assert relative_python.returncode != 0
    assert symlink_python.returncode != 0
    assert not (parent / "wrong-version").exists()
    assert not (parent / "relative-python").exists()
    assert not (parent / "symlink-python").exists()


def test_bootstrap_rejects_non_cpython_implementation(tmp_path: Path) -> None:
    python, _log = _fake_python(tmp_path)
    python.write_text(
        python.read_text(encoding="utf-8").replace(
            "  exit 0\nfi",
            "  exit 86\nfi",
            1,
        ),
        encoding="utf-8",
    )
    python.chmod(0o700)
    parent = _private_parent(tmp_path)
    venv = parent / "venv"

    result = _run_bootstrap(python, venv)

    assert result.returncode != 0
    assert not venv.exists()


def test_bootstrap_rejects_non_reviewed_system_python_patch(tmp_path: Path) -> None:
    python = Path("/usr/bin/python3.12")
    if not python.is_file() or python.is_symlink():
        pytest.skip("a canonical system CPython 3.12 is unavailable")
    identity = subprocess.run(
        [
            str(python),
            "-I",
            "-S",
            "-B",
            "-c",
            "import os,sys; print(sys.version_info[:3], sys.platform, os.uname().machine)",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if identity == "(3, 12, 13) linux x86_64":
        pytest.skip("the system interpreter now matches the reviewed target")
    parent = _private_parent(tmp_path)
    venv = parent / "venv"

    result = _run_bootstrap(python, venv)

    assert result.returncode != 0
    assert "must be CPython 3.12.13 for Linux x86_64" in result.stderr
    assert not venv.exists()


@pytest.mark.parametrize("unsafe_component", ("tool", "base"))
def test_bootstrap_rejects_group_writable_user_installation(
    tmp_path: Path, unsafe_component: str
) -> None:
    python, _log = _fake_python(tmp_path)
    if unsafe_component == "tool":
        python.chmod(0o770)
    else:
        python.parent.chmod(0o770)
    parent = _private_parent(tmp_path)
    venv = parent / "venv"

    result = _run_bootstrap(python, venv)

    assert result.returncode != 0
    assert "group- or world-writable" in result.stderr
    assert "install or copy CPython into an owner-private base" in result.stderr
    assert not venv.exists()
