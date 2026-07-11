"""Write-path integration tests.

Default run: only test_custom_instructions_roundtrip executes (idempotent).
memory_add and codex_task_create tests are gated by SKIP_LIVE env var —
they write to a live account and are skipped unless SKIP_LIVE=0 is set.

Run live:  SKIP_LIVE=0 pytest tests/test_writes.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_AUTH = (
    Path(os.environ["CODEX_HOME"])
    if os.environ.get("CODEX_HOME")
    else Path.home() / ".codex"
) / "auth.json"
_needs_auth = pytest.mark.skipif(
    not _AUTH.exists(), reason="selected Codex auth.json not present"
)
_skip_live = pytest.mark.skipif(
    os.environ.get("SKIP_LIVE", "1") == "1",
    reason="live write test — set SKIP_LIVE=0 to run",
)


@_needs_auth
@_skip_live
def test_custom_instructions_roundtrip() -> None:
    """Read current instructions, POST back the same values, verify 200 + fields match."""
    from gpt2agent.backend import BackendClient

    client = BackendClient()

    before = client.get(
        "/backend-api/user_system_messages",
        target_path="/backend-api/user_system_messages",
    )

    result = client.post(
        "/backend-api/user_system_messages",
        json={
            "enabled": before.get("enabled"),
            "about_user_message": before.get("about_user_message", ""),
            "about_model_message": before.get("about_model_message", ""),
            "traits_enabled": before.get("traits_enabled"),
            "personality_type_selection": before.get("personality_type_selection"),
            "disabled_tools": before.get("disabled_tools") or [],
        },
        target_path="/backend-api/user_system_messages",
    )

    assert result is not None
    assert result.get("enabled") == before.get("enabled")
    assert result.get("about_model_message") == before.get("about_model_message")
    assert result.get("about_user_message") == before.get("about_user_message")


@_skip_live
@_needs_auth
def test_memory_add_safe() -> None:
    """POST /backend-api/memories — currently returns 405, documents the finding."""
    from gpt2agent.backend import BackendClient

    client = BackendClient()
    # Confirmed 2026-04-23: POST → 405 Method Not Allowed, Allow: GET only.
    # This test is kept as a probe template; run manually after the API surface changes.
    with pytest.raises(RuntimeError, match="405"):
        client.post(
            "/backend-api/memories",
            json={
                "content": "Testing gpt2agent write path — created 2026-04-23, can be deleted."
            },
            target_path="/backend-api/memories",
        )


@_skip_live
@_needs_auth
def test_codex_task_create() -> None:
    """Create a Codex task on robotlearning123/mujoco-mcp (low-risk env)."""
    from gpt2agent.backend import BackendClient

    client = BackendClient()

    # Resolve env
    data = client.get(
        "/backend-api/codex/environments", target_path="/backend-api/codex/environments"
    )
    envs = data if isinstance(data, list) else (data.get("environments") or [])
    match = next(
        (e for e in envs if e.get("label") == "robotlearning123/mujoco-mcp"), None
    )
    assert match is not None, "mujoco-mcp env not found"

    result = client.post(
        "/backend-api/codex/tasks",
        json={
            "new_task": {
                "environment_id": match["id"],
                "branch": "main",
            },
            "input_items": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "content_type": "text",
                            "text": "probe-gpt2agent-DELETE-ME: echo hello",
                        }
                    ],
                }
            ],
        },
        target_path="/backend-api/codex/tasks",
    )

    assert result is not None
    task = result.get("task") or result
    assert task.get("id"), f"no task id in response: {result}"
