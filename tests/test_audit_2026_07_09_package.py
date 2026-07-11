"""Release-package regressions found during the 2026-07-09 audit.

These tests are deliberately offline.  Shell entry points run against recording
executables, skill installation uses temporary source/target trees, and the
sdist is built and unpacked entirely below ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from gpt2agent import __version__
from gpt2agent import install as install_mod


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"
DR_WRAPPER = REPO_ROOT / "gpt2agent" / "skills" / "deep-research" / "bin" / "run.sh"
QUOTA_WRAPPER = REPO_ROOT / "gpt2agent" / "skills" / "deep-research" / "bin" / "quota.sh"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _recording_installer_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "pipx-argv.jsonl"
    _write_executable(
        fake_bin / "python3.13",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = '-c' ]; then printf '%s\\n' \"$0\"; exit 0; fi\n"
        "if [ \"${1:-}\" = '--version' ]; then echo 'Python 3.13.0'; exit 0; fi\n"
        "exit 64\n",
    )
    fake_pipx = fake_bin / "pipx"
    _write_executable(
        fake_pipx,
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import shutil
            import sys

            args = sys.argv[1:]
            if args == ["--version"]:
                print("fake-pipx 1.0")
                raise SystemExit(0)
            if args == ["environment", "--value", "PIPX_HOME"]:
                print(os.environ["FAKE_PIPX_HOME"])
                raise SystemExit(0)

            with open(os.environ["FAKE_PIPX_LOG"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(args) + "\\n")

            if args[:1] == ["uninstall"]:
                status = int(os.environ.get("FAKE_PIPX_UNINSTALL_STATUS", "0"))
                if status == 0:
                    shutil.rmtree(
                        os.path.join(os.environ["FAKE_PIPX_HOME"], "venvs", "gpt2agent"),
                        ignore_errors=True,
                    )
                raise SystemExit(status)

            is_git = any(arg.startswith("git+https://") for arg in args)
            status_var = "FAKE_PIPX_GIT_STATUS" if is_git else "FAKE_PIPX_PYPI_STATUS"
            status = int(os.environ.get(status_var, "0"))
            if status:
                print("simulated PyPI failure", file=sys.stderr)
                raise SystemExit(status)
            venv = os.path.join(os.environ["FAKE_PIPX_HOME"], "venvs", "gpt2agent")
            os.makedirs(venv, exist_ok=True)
            with open(os.path.join(venv, "installed-version"), "w", encoding="utf-8") as fh:
                fh.write("new\\n")
            if not os.environ.get("FAKE_PIPX_SKIP_VENV_APP"):
                app_dir = os.path.join(venv, "bin")
                os.makedirs(app_dir, exist_ok=True)
                app = os.path.join(app_dir, "gpt2agent")
                with open(app, "w", encoding="utf-8") as fh:
                    fh.write(
                        "#!/bin/sh\\n"
                        "if [ \\\"${{1:-}}\\\" = '--version' ]; then exit 0; fi\\n"
                        "exit \\\"${{FAKE_GPT2AGENT_STATUS:-0}}\\\"\\n"
                    )
                os.chmod(app, 0o755)
            """
        ),
    )
    _write_executable(fake_bin / "gpt2agent", "#!/bin/sh\nexit 0\n")

    home = tmp_path / "home"
    home.mkdir()
    pipx_home = tmp_path / "pipx-home"
    pipx_home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "FAKE_PIPX_LOG": str(log),
            "FAKE_PIPX_HOME": str(pipx_home),
            "HOME": str(home),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    return env, log


def _pipx_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("source_kind", ["pypi", "url", "directory"])
def test_installer_passes_selected_python_to_every_pipx_install_branch(
    tmp_path: Path, source_kind: str
) -> None:
    env, log = _recording_installer_env(tmp_path)
    command = [str(INSTALLER), "--no-register"]
    install_flags = ["--force"]
    source = "gpt2agent"
    selected_python = str(Path(env["PATH"].split(os.pathsep, 1)[0]) / "python3.13")

    if source_kind == "url":
        source = "git+https://example.invalid/gpt2agent.git@main"
        command.extend(["--source", source])
    elif source_kind == "directory":
        source_dir = tmp_path / "local-source"
        source_dir.mkdir()
        source = str(source_dir)
        command.extend(["--source", source])
        install_flags.insert(0, "--editable")

    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _pipx_calls(log) == [
        ["install", *install_flags, "--python", selected_python, source]
    ]


def test_checkout_installer_defaults_to_named_pypi_package(tmp_path: Path) -> None:
    env, log = _recording_installer_env(tmp_path)

    result = subprocess.run(
        [str(INSTALLER), "--no-register"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    selected_python = str(Path(env["PATH"].split(os.pathsep, 1)[0]) / "python3.13")
    assert _pipx_calls(log) == [
        ["install", "--force", "--python", selected_python, "gpt2agent"]
    ]


def test_installer_replaces_existing_pipx_environment_without_uninstall(
    tmp_path: Path,
) -> None:
    env, log = _recording_installer_env(tmp_path)
    venv = Path(env["FAKE_PIPX_HOME"]) / "venvs" / "gpt2agent"
    venv.mkdir(parents=True)
    marker = venv / "installed-version"
    marker.write_text("old\n", encoding="utf-8")
    selected_python = str(Path(env["PATH"].split(os.pathsep, 1)[0]) / "python3.13")

    result = subprocess.run(
        [str(INSTALLER), "--no-register"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _pipx_calls(log) == [
        ["install", "--force", "--python", selected_python, "gpt2agent"],
    ]
    assert marker.read_text(encoding="utf-8") == "new\n"
    assert not list(Path(env["FAKE_PIPX_HOME"]).glob(".gpt2agent-upgrade.*"))


def test_failed_upgrade_restores_existing_pipx_environment(
    tmp_path: Path,
) -> None:
    env, log = _recording_installer_env(tmp_path)
    venv = Path(env["FAKE_PIPX_HOME"]) / "venvs" / "gpt2agent"
    venv.mkdir(parents=True)
    marker = venv / "installed-version"
    marker.write_text("old\n", encoding="utf-8")
    env["FAKE_PIPX_PYPI_STATUS"] = "42"
    selected_python = str(Path(env["PATH"].split(os.pathsep, 1)[0]) / "python3.13")

    result = subprocess.run(
        [str(INSTALLER), "--no-register"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 42
    assert marker.read_text(encoding="utf-8") == "old\n"
    assert _pipx_calls(log) == [
        ["install", "--force", "--python", selected_python, "gpt2agent"]
    ]
    assert not list(Path(env["FAKE_PIPX_HOME"]).glob(".gpt2agent-upgrade.*"))


def test_successful_upgrade_is_not_rolled_back_when_app_bin_is_not_on_path(
    tmp_path: Path,
) -> None:
    env, _ = _recording_installer_env(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
    (fake_bin / "gpt2agent").unlink()
    venv = Path(env["FAKE_PIPX_HOME"]) / "venvs" / "gpt2agent"
    venv.mkdir(parents=True)
    marker = venv / "installed-version"
    marker.write_text("old\n", encoding="utf-8")

    result = subprocess.run(
        [str(INSTALLER), "--no-register"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == "new\n"
    assert "pipx ensurepath" in result.stdout
    assert not list(Path(env["FAKE_PIPX_HOME"]).glob(".gpt2agent-upgrade.*"))


def test_failed_registration_restores_existing_pipx_environment(
    tmp_path: Path,
) -> None:
    env, _ = _recording_installer_env(tmp_path)
    venv = Path(env["FAKE_PIPX_HOME"]) / "venvs" / "gpt2agent"
    venv.mkdir(parents=True)
    marker = venv / "installed-version"
    marker.write_text("old\n", encoding="utf-8")
    env["FAKE_GPT2AGENT_STATUS"] = "47"

    result = subprocess.run(
        [str(INSTALLER)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 47
    assert marker.read_text(encoding="utf-8") == "old\n"
    assert not list(Path(env["FAKE_PIPX_HOME"]).glob(".gpt2agent-upgrade.*"))


def test_stale_path_app_cannot_commit_incomplete_pipx_upgrade(
    tmp_path: Path,
) -> None:
    env, _ = _recording_installer_env(tmp_path)
    venv = Path(env["FAKE_PIPX_HOME"]) / "venvs" / "gpt2agent"
    venv.mkdir(parents=True)
    marker = venv / "installed-version"
    marker.write_text("old\n", encoding="utf-8")
    env["FAKE_PIPX_SKIP_VENV_APP"] = "1"

    result = subprocess.run(
        [str(INSTALLER), "--no-register"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "does not contain an executable gpt2agent app" in result.stderr
    assert marker.read_text(encoding="utf-8") == "old\n"
    assert not list(Path(env["FAKE_PIPX_HOME"]).glob(".gpt2agent-upgrade.*"))


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--transport", "http"], "HTTP transport is disabled"),
        (["--port", "9000"], "--port is unavailable"),
    ],
)
def test_installer_rejects_http_options_before_pipx_mutation(
    tmp_path: Path, arguments: list[str], message: str
) -> None:
    env, log = _recording_installer_env(tmp_path)

    result = subprocess.run(
        [str(INSTALLER), *arguments, "--no-register"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert not log.exists()


def test_installer_does_not_advertise_or_forward_stale_http_port() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    result = subprocess.run(
        [str(INSTALLER), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--transport http" not in result.stdout
    assert "--port" not in result.stdout
    assert "\nPORT=" not in text
    assert "--http-port" not in text


def test_pypi_failure_fails_closed_without_git_fallback(tmp_path: Path) -> None:
    env, log = _recording_installer_env(tmp_path)
    env["FAKE_PIPX_PYPI_STATUS"] = "42"
    env["FAKE_PIPX_GIT_STATUS"] = "0"
    outside_checkout = tmp_path / "outside-checkout"
    outside_checkout.mkdir()

    result = subprocess.run(
        [str(INSTALLER), "--no-register"],
        cwd=outside_checkout,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "simulated PyPI failure" in result.stdout + result.stderr
    calls = _pipx_calls(log)
    selected_python = str(Path(env["PATH"].split(os.pathsep, 1)[0]) / "python3.13")
    assert calls[0] == [
        "install",
        "--force",
        "--python",
        selected_python,
        "gpt2agent",
    ]
    assert not any(arg.startswith("git+https://") for call in calls for arg in call)


def test_checkout_installer_honors_codex_home_for_login_check(tmp_path: Path) -> None:
    env, _log = _recording_installer_env(tmp_path)
    codex_home = tmp_path / "alternate-codex"
    codex_home.mkdir()
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"tokens": {}}\n')
    env["CODEX_HOME"] = str(codex_home)

    result = subprocess.run(
        [str(INSTALLER), "--no-register"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"codex token found at {auth_file}" in output
    assert f"No {env['HOME']}/.codex/auth.json" not in output


def test_deep_research_wrapper_accepts_saved_token_without_codex(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    python_log = tmp_path / "python-argv.txt"
    fake_python = tmp_path / "recording-python"
    _write_executable(
        fake_python,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_PYTHON_LOG\"\n",
    )
    _write_executable(fake_bin / "gpt2agent", f"#!{fake_python}\n")
    _write_executable(
        fake_bin / "readlink",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"-f\" ]; then\n"
        "  echo 'readlink: illegal option -- f' >&2\n"
        "  exit 64\n"
        "fi\n"
        "exit 65\n",
    )

    home = tmp_path / "home"
    home.mkdir()
    saved_token_dir = home / ".gpt2agent"
    saved_token_dir.mkdir()
    (saved_token_dir / "token.json").write_text('{"access_token": "saved"}\n')
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    env = os.environ.copy()
    env.pop("CODEX_HOME", None)
    env.update(
        {
            "FAKE_PYTHON_LOG": str(python_log),
            "HOME": str(home),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )

    result = subprocess.run(
        [str(DR_WRAPPER), "test topic"],
        cwd=unrelated_cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    argv = python_log.read_text().splitlines()
    assert Path(argv[0]) == DR_WRAPPER.parent / "deep_research.py"
    assert argv[1:] == ["test topic"]


def test_deep_research_wrapper_rejects_missing_auth_before_python(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    python_marker = tmp_path / "python-was-invoked"
    fake_python = tmp_path / "recording-python"
    _write_executable(
        fake_python,
        "#!/bin/sh\nprintf invoked > \"$FAKE_PYTHON_MARKER\"\n",
    )
    _write_executable(fake_bin / "gpt2agent", f"#!{fake_python}\n")

    home = tmp_path / "home"
    home.mkdir()
    selected_codex_home = tmp_path / "selected-codex"
    env = os.environ.copy()
    env.update(
        {
            "CODEX_HOME": str(selected_codex_home),
            "FAKE_PYTHON_MARKER": str(python_marker),
            "HOME": str(home),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )

    result = subprocess.run(
        [str(DR_WRAPPER), "test topic"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "error: no ChatGPT auth file found" in result.stderr
    assert str(selected_codex_home / "auth.json") in result.stderr
    assert str(home / ".gpt2agent" / "token.json") in result.stderr
    assert "codex login or gpt2agent setup" in result.stderr
    assert not python_marker.exists()


@pytest.mark.parametrize(
    ("backend_source", "expected"),
    [
        (
            'class BackendClient:\n'
            '    def __init__(self):\n'
            '        raise RuntimeError("no token")\n',
            "error: RuntimeError: no token",
        ),
        (
            'class BackendClient:\n'
            '    def post(self, *args, **kwargs):\n'
            '        raise RuntimeError("offline")\n',
            "error: RuntimeError: offline",
        ),
        (
            'class BackendClient:\n'
            '    def post(self, *args, **kwargs):\n'
            '        return {"limits_progress": []}\n',
            "deep_research quota entry not found",
        ),
        (
            'class BackendClient:\n'
            '    def post(self, *args, **kwargs):\n'
            '        return {"limits_progress": [{"feature_name": "deep_research"}]}\n',
            "deep_research quota remaining is missing or invalid",
        ),
        (
            'class BackendClient:\n'
            '    def post(self, *args, **kwargs):\n'
            '        return {"limits_progress": [{"feature_name": "deep_research", "remaining": "many"}]}\n',
            "deep_research quota remaining is missing or invalid",
        ),
    ],
)
def test_quota_wrapper_fails_when_quota_is_unverified(
    tmp_path: Path, backend_source: str, expected: str
) -> None:
    fake_bin = tmp_path / "bin"
    _write_executable(fake_bin / "gpt2agent", f"#!{sys.executable}\n")
    fake_package = tmp_path / "fake-package" / "gpt2agent"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text("")
    (fake_package / "backend.py").write_text(backend_source)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PYTHONPATH": str(fake_package.parent),
        }
    )

    result = subprocess.run(
        [str(QUOTA_WRAPPER)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected in result.stderr
    assert result.stdout == ""


def test_quota_wrapper_reports_verified_zero_remaining(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    _write_executable(fake_bin / "gpt2agent", f"#!{sys.executable}\n")
    fake_package = tmp_path / "fake-package" / "gpt2agent"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text("")
    (fake_package / "backend.py").write_text(
        'class BackendClient:\n'
        '    def post(self, *args, **kwargs):\n'
        '        return {"limits_progress": [{"feature_name": "deep_research", '
        '"remaining": 0, "reset_after": "tomorrow"}]}\n'
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PYTHONPATH": str(fake_package.parent),
        }
    )

    result = subprocess.run(
        [str(QUOTA_WRAPPER)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "deep_research remaining = 0  reset = tomorrow"
    assert result.stderr == ""


def test_deep_research_wrapper_missing_python_recommends_pypi(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": "/usr/bin:/bin"})

    result = subprocess.run(
        [str(DR_WRAPPER), "test topic"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "fix:   pipx install gpt2agent" in result.stderr
    assert "git+https://" not in result.stderr


def test_deep_research_skill_uses_stdin_instead_of_shared_tmp_query() -> None:
    skill = (DR_WRAPPER.parents[1] / "SKILL.md").read_text(encoding="utf-8")

    assert "/tmp/dr_query.txt" not in skill
    assert " - <<'EOF'" in skill


def test_python_module_entry_reports_package_version() -> None:
    expected = f"gpt2agent {__version__}"
    result = subprocess.run(
        [sys.executable, "-m", "gpt2agent", "--version"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def _packaged_deep_research_skill(tmp_path: Path) -> Path:
    src = tmp_path / "package" / "skills" / "deep-research"
    (src / "bin" / "__pycache__").mkdir(parents=True)
    (src / "SKILL.md").write_text("# packaged skill\n")
    (src / "bin" / "run.sh").write_text("#!/bin/sh\n")
    (src / "bin" / "quota.sh").write_text("#!/bin/sh\n")
    (src / "bin" / "deep_research.py").write_text("# packaged helper\n")
    (src / "bin" / "__pycache__" / "x.pyc").write_bytes(b"bytecode")
    (src / "bin" / "cached.pyo").write_bytes(b"optimized-bytecode")
    return src


def _seed_active_skill(tmp_path: Path) -> tuple[Path, Path]:
    skills_root = tmp_path / "installed" / "skills"
    active = skills_root / "deep-research"
    active.mkdir(parents=True)
    (active / "ACTIVE_SENTINEL.txt").write_text("original active skill\n")
    return skills_root, active


def test_skill_copy_filters_python_bytecode(tmp_path: Path) -> None:
    src = _packaged_deep_research_skill(tmp_path)
    skills_root = tmp_path / "installed" / "skills"

    install_mod._install_one_skill("deep-research", src, skills_root)

    installed = skills_root / "deep-research"
    assert (installed / "SKILL.md").is_file()
    assert not any(path.name == "__pycache__" for path in installed.rglob("*"))
    assert not list(installed.rglob("*.pyc"))
    assert not list(installed.rglob("*.pyo"))


def test_skill_copy_failure_keeps_original_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _packaged_deep_research_skill(tmp_path)
    skills_root, active = _seed_active_skill(tmp_path)

    def fail_copy(_src: Path, dst: Path, *args: object, **kwargs: object) -> None:
        del args, kwargs
        destination = Path(dst)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "PARTIAL_COPY.txt").write_text("partial\n")
        raise OSError("injected staged-copy failure")

    monkeypatch.setattr(install_mod.shutil, "copytree", fail_copy)

    with pytest.raises(OSError, match="injected staged-copy failure"):
        install_mod._install_one_skill("deep-research", src, skills_root)

    assert (active / "ACTIVE_SENTINEL.txt").read_text() == "original active skill\n"
    assert not (active / "PARTIAL_COPY.txt").exists()


def test_skill_swap_failure_restores_original_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _packaged_deep_research_skill(tmp_path)
    skills_root, active = _seed_active_skill(tmp_path)
    real_move = shutil.move

    def fail_staging_swap(src_path: str, dst_path: str, *args: object, **kwargs: object):
        source = Path(src_path)
        destination = Path(dst_path)
        if ".staging-" in source.name and destination == active:
            raise OSError("injected staged-swap failure")
        return real_move(src_path, dst_path, *args, **kwargs)

    monkeypatch.setattr(install_mod.shutil, "move", fail_staging_swap)

    with pytest.raises(OSError, match="injected staged-swap failure"):
        install_mod._install_one_skill("deep-research", src, skills_root)

    assert (active / "ACTIVE_SENTINEL.txt").read_text() == "original active skill\n"
    assert not (active / "SKILL.md").exists()


def test_skill_validation_failure_keeps_original_active(tmp_path: Path) -> None:
    src = _packaged_deep_research_skill(tmp_path)
    (src / "bin" / "run.sh").unlink()
    skills_root, active = _seed_active_skill(tmp_path)

    with pytest.raises(RuntimeError, match="missing required file"):
        install_mod._install_one_skill("deep-research", src, skills_root)

    assert (active / "ACTIVE_SENTINEL.txt").read_text() == "original active skill\n"


def _python_with_build_backend() -> tuple[list[str], str]:
    """Return an offline sdist command prefix and its backend kind.

    The repository's dev extra intentionally does not add packaging tools.  Use
    ``build`` when it and the declared backend requirements are available,
    otherwise invoke a compatible setuptools backend directly.  A system Python
    is a final fallback for minimal test venvs; incompatible ambient backends
    must not be mistaken for the project's exact locked build backend.
    """
    candidates = [sys.executable]
    system_python = shutil.which("python3")
    # A venv executable and its base interpreter can resolve to the same inode
    # while exposing intentionally different module paths, so compare the
    # executable spellings rather than resolving symlinks here.
    if system_python and Path(system_python).absolute() != Path(sys.executable).absolute():
        candidates.append(system_python)

    backend_probe = (
        "from importlib.metadata import version; "
        "import setuptools.build_meta, wheel; "
        "assert version('setuptools') == '83.0.0'; "
        "assert version('wheel') == '0.47.0'"
    )
    for python in candidates:
        has_build = subprocess.run(
            [python, "-c", f"import build; {backend_probe}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if has_build.returncode == 0:
            return [python, "-m", "build", "--sdist", "--no-isolation"], "build"

        has_setuptools = subprocess.run(
            [python, "-c", backend_probe],
            text=True,
            capture_output=True,
            check=False,
        )
        if has_setuptools.returncode == 0:
            code = (
                "from setuptools.build_meta import build_sdist; "
                "import sys; print(build_sdist(sys.argv[1]))"
            )
            return [python, "-c", code], "setuptools"

    pytest.skip(
        "sdist test requires compatible local packaging tools; the release workflow "
        "performs the mandatory isolated artifact build"
    )


def test_build_backend_probe_skips_when_offline_backend_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, returncode=1),
    )

    with pytest.raises(pytest.skip.Exception, match="compatible local packaging tools"):
        _python_with_build_backend()


def test_sdist_runs_release_heavy_parser_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(
        REPO_ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "*.pyc",
            "*.egg-info",
            "build",
            "dist",
        ),
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    command, backend = _python_with_build_backend()
    if backend == "build":
        command.extend(["--outdir", str(dist)])
    else:
        command.append(str(dist))

    built = subprocess.run(
        command,
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    archives = list(dist.glob("*.tar.gz"))
    assert len(archives) == 1
    unpacked = tmp_path / "unpacked"
    with tarfile.open(archives[0]) as archive:
        if sys.version_info >= (3, 12):
            archive.extractall(unpacked, filter="data")
        else:  # Python 3.10/3.11 lack the extraction filter API.
            archive.extractall(unpacked)
    sdist_root = next(path for path in unpacked.iterdir() if path.is_dir())
    tests_root = sdist_root / "tests"
    expected_test_files = {
        Path("test_heavy_dr_parser.py"),
        Path("fixtures/heavy_dr_conversation_detail_h2.json"),
        Path("fixtures/heavy_dr_widget_state.json"),
    }
    assert {
        path.relative_to(tests_root) for path in tests_root.rglob("*") if path.is_file()
    } == expected_test_files

    tested = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_heavy_dr_parser.py"],
        cwd=sdist_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr
