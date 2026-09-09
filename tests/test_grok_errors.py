"""Secret-safe Grok error and route-normalization contracts."""

from __future__ import annotations

import json

import pytest

from gpt2agent._log_redact import redact_error
from gpt2agent.grok_errors import GROK_ERROR_CODES, GrokError, normalize_grok_route
from gpt2agent.tools._errors import serialize_tool_error


CONVERSATION_ID = "conv-private-42"
RESPONSE_ID = "resp-private-99"
ATTACHMENT_ID = "attach-private-17"
UUID_ID = "8cb6f8ef-b988-4f5a-a721-926c7e52e770"
UUID_LIKE_ID = "00000000-0000-0000-0000-000000000042"


def test_grok_routes_remove_conversation_and_response_ids() -> None:
    assert normalize_grok_route(
        f"/rest/app-chat/conversations/{CONVERSATION_ID}"
    ) == "/rest/app-chat/conversations/{id}"
    assert normalize_grok_route(
        f"/rest/app-chat/conversations/reconnect-response-v2/{RESPONSE_ID}"
    ) == "/rest/app-chat/conversations/reconnect-response-v2/{id}"


def test_grok_routes_remove_queries_attachment_ids_and_uuids() -> None:
    assert normalize_grok_route(
        f"/rest/app-chat/attachments/{ATTACHMENT_ID}?X-Amz-Signature=planted-secret"
    ) == "/rest/app-chat/attachments/{id}"
    assert normalize_grok_route(f"/rest/app-chat/responses/{UUID_ID}") == (
        "/rest/app-chat/responses/{id}"
    )
    assert normalize_grok_route(f"/rest/app-chat/events/{UUID_LIKE_ID}") == (
        "/rest/app-chat/events/{id}"
    )


@pytest.mark.parametrize(
    ("route", "planted"),
    [
        ("/rest/users/user@example.test", "user@example.test"),
        ("/rest/identities/account-private-8", "account-private-8"),
        ("/rest/sessions/xai-planted-route-secret-123456", "xai-planted-route-secret"),
    ],
)
def test_grok_routes_remove_identity_and_credential_segments(
    route: str,
    planted: str,
) -> None:
    normalized = normalize_grok_route(route)

    assert normalized is not None
    assert planted not in normalized


def test_unknown_grok_route_fails_closed_without_opaque_identifier() -> None:
    planted = "account-private-8"

    normalized = normalize_grok_route(f"/rest/sessions/{planted}")

    assert normalized == "<route>"
    assert planted not in normalized


def test_grok_error_is_typed_bounded_and_secret_free() -> None:
    error = GrokError(
        "GROK_WEB_AUTH_EXPIRED",
        method="POST",
        route=(
            f"/rest/app-chat/conversations/{CONVERSATION_ID}?token=planted-secret"
        ),
        status_code=401,
        retryable=False,
    )
    rendered = serialize_tool_error(error)
    assert rendered == (
        "GROK_WEB_AUTH_EXPIRED: POST "
        "/rest/app-chat/conversations/{id} failed (401)"
    )
    assert "planted-secret" not in rendered
    assert CONVERSATION_ID not in rendered


def test_grok_error_validates_code_and_bounds_metadata() -> None:
    assert len(GROK_ERROR_CODES) == 14
    with pytest.raises(ValueError, match="Grok error code"):
        GrokError("PLANTED_SECRET", retryable=False)
    with pytest.raises(ValueError, match="Grok error method"):
        GrokError("GROK_WEB_FAILED", method="X" * 17, retryable=False)

    error = GrokError(
        "GROK_WEB_RATE_LIMITED",
        method="post",
        route="/rest/models",
        status_code=429,
        retryable=True,
        retry_after=3600,
    )

    assert error.method == "POST"
    assert error.route == "/rest/models"
    assert error.status_code == 429
    assert error.retryable is True
    assert error.retry_after == 60.0
    assert len(str(error)) < 256


def test_only_typed_grok_errors_cross_the_tool_boundary() -> None:
    typed = GrokError("GROK_BUILD_TIMEOUT", retryable=True, retry_after=2)
    assert serialize_tool_error(typed) == str(typed)

    planted = f"unexpected Grok failure for {CONVERSATION_ID} at user@example.test"
    assert serialize_tool_error(RuntimeError(planted)) == (
        "unavailable: tool execution failed"
    )


@pytest.mark.parametrize(
    "planted",
    [
        "sso=planted-sso-secret",
        "sso-rw=planted-sso-rw-secret",
        'sso="planted-quoted-cookie-secret"',
        "cf_clearance=planted-clearance-secret",
        "grok_device_id=planted-device-secret",
        "Cookie: sso=planted-cookie-secret; theme=dark",
        "Set-Cookie: sso-rw=planted-set-cookie-secret; Secure",
        "Authorization: Bearer xai-planted-auth-secret-123456",
        "/asset?X-Amz-Credential=planted-credential&X-Amz-Signature=planted-signature"
        "&X-Amz-Security-Token=planted-security-token",
        "email=user@example.test account_id=account-private-8",
        f"conversation failed for {CONVERSATION_ID}",
        f"response failed for {RESPONSE_ID}",
        f"attachment failed for {ATTACHMENT_ID}",
        f"upstream identity {UUID_ID}",
        f"upstream identity {UUID_LIKE_ID}",
    ],
)
def test_grok_log_redaction_removes_credentials_identity_and_opaque_ids(
    planted: str,
) -> None:
    rendered = redact_error(planted, max_len=500)

    for secret in (
        "planted-sso-secret",
        "planted-sso-rw-secret",
        "planted-quoted-cookie-secret",
        "planted-clearance-secret",
        "planted-device-secret",
        "planted-cookie-secret",
        "planted-set-cookie-secret",
        "xai-planted-auth-secret-123456",
        "planted-credential",
        "planted-signature",
        "planted-security-token",
        "user@example.test",
        "account-private-8",
        CONVERSATION_ID,
        RESPONSE_ID,
        ATTACHMENT_ID,
        UUID_ID,
        UUID_LIKE_ID,
    ):
        assert secret not in rendered


def test_validated_grok_ids_survive_dedicated_structured_result_fields() -> None:
    result = {
        "conversation_id": CONVERSATION_ID,
        "response_id": RESPONSE_ID,
        "attachment_id": ATTACHMENT_ID,
        "request_id": UUID_ID,
        "trace_id": UUID_LIKE_ID,
    }

    rendered = json.dumps(result, sort_keys=True)

    assert CONVERSATION_ID in rendered
    assert RESPONSE_ID in rendered
    assert ATTACHMENT_ID in rendered
    assert UUID_ID in rendered
    assert UUID_LIKE_ID in rendered
