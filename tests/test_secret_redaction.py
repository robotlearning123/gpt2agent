from __future__ import annotations

import pytest

from gpt2agent.tools._redact import redact
from gpt2agent.tools.conversations import normalize_conversation_detail
from gpt2agent.tools.instructions import normalize_custom_instructions
from gpt2agent.tools.memory import normalize_memories


AWS_SECRET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/ab"
AWS_SESSION = "IQoJb3JpZ2luX2VjEFgaCXVzLWVhc3QtMSJHMEUCIQDzExampleSessionToken1234567890"
SLACK_TOKEN = "xox" + "b-123456789012-123456789012-abcdefghijklmnopqrstuvwx"
GOOGLE_API_KEY = "AIza" + "A" * 35
DATABASE_URL = "postgresql://app_user:correct-horse-battery-staple@db.example/app"
PRIVATE_KEY = (
    "-----BEGIN " + "PRIVATE KEY-----\n"
    "U1lOVEhFVElDLU5PVC1BLVJFQUklWQVRFLUtFWQ==\n"
    "-----END " + "PRIVATE KEY-----"
)
GROK_CONVERSATION_ID = "conv-private-42"
GROK_RESPONSE_ID = "resp-private-99"
GROK_ATTACHMENT_ID = "attach-private-17"
GROK_UUID_ID = "8cb6f8ef-b988-4f5a-a721-926c7e52e770"
GROK_UUID_LIKE_ID = "00000000-0000-0000-0000-000000000042"


def test_memory_projection_redacts_complete_provider_credentials() -> None:
    content = "\n".join(
        (
            f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}",
            f'AWS_SESSION_TOKEN="{AWS_SESSION}"',
            f"Slack bot: {SLACK_TOKEN}",
            f"Google key: {GOOGLE_API_KEY}",
            f"DATABASE_URL={DATABASE_URL}",
        )
    )

    projected = normalize_memories(
        {"memories": [{"id": "memory-1", "content": content}]}
    )[0]["content"]

    for credential in (
        AWS_SECRET,
        AWS_SESSION,
        SLACK_TOKEN,
        GOOGLE_API_KEY,
        "app_user:correct-horse-battery-staple",
    ):
        assert credential not in projected
    assert "AWS_SECRET_ACCESS_KEY=<REDACTED>" in projected
    assert 'AWS_SESSION_TOKEN="<REDACTED>"' in projected
    assert "Slack bot: <TOKEN>" in projected
    assert "Google key: <APIKEY>" in projected
    assert "DATABASE_URL=postgresql://<REDACTED>@db.example/app" in projected


def test_custom_instruction_projection_redacts_label_aware_assignments() -> None:
    instructions = "\n".join(
        (
            'password = "correct horse battery staple"',
            "service_token: tok_live_abcdefghijklmnopqrstuvwxyz",
            "api_key=provider-key-abcdefghijklmnopqrstuvwxyz",
            "client_secret='client-secret-abcdefghijklmnopqrstuvwxyz'",
            "key=standalone-key-abcdefghijklmnopqrstuvwxyz",
        )
    )

    projected = normalize_custom_instructions(
        {
            "about_user_message": instructions,
            "about_model_message": f"Use {DATABASE_URL} for migrations",
        }
    )

    assert projected["about_user"] == "\n".join(
        (
            'password = "<REDACTED>"',
            "service_token: <REDACTED>",
            "api_key=<REDACTED>",
            "client_secret='<REDACTED>'",
            "key=<REDACTED>",
        )
    )
    assert projected["about_model"] == (
        "Use postgresql://<REDACTED>@db.example/app for migrations"
    )


def test_json_assignments_keep_labels_and_quotes() -> None:
    source = (
        f'{{"aws_secret_access_key":"{AWS_SECRET}",'
        '"refresh_token":"refresh-token-abcdefghijklmnopqrstuvwxyz"}'
    )

    assert redact(source) == (
        '{"aws_secret_access_key":"<REDACTED>",'
        '"refresh_token":"<REDACTED>"}'
    )


def test_conversation_projection_redacts_provider_credentials_and_private_keys() -> None:
    text = f"{SLACK_TOKEN}\n{DATABASE_URL}\n{PRIVATE_KEY}"
    projected = normalize_conversation_detail(
        {
            "id": "conversation-1",
            "mapping": {
                "message-1": {
                    "message": {
                        "id": "message-1",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": [text]},
                        "status": "finished_successfully",
                        "metadata": {},
                    }
                }
            },
        },
        expected_id="conversation-1",
        max_messages=100,
    )["messages"][0]["text"]

    assert SLACK_TOKEN not in projected
    assert "app_user:correct-horse-battery-staple" not in projected
    assert "BEGIN PRIVATE KEY" not in projected
    assert "END PRIVATE KEY" not in projected
    assert "<PRIVATE_KEY>" in projected


def test_secret_redaction_preserves_noncredential_near_misses() -> None:
    near_misses = (
        "token budget: 4096",
        "password policy: require twelve characters",
        "Press the key: Enter to continue",
        "keyboard_key=Enter",
        "public_key=ssh-rsa",
        "postgresql://db.example/app",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "xoxb-short",
        "AIza-too-short",
        "released 2026-05-26 as planned",
    )

    for value in near_misses:
        assert redact(value) == value


@pytest.mark.parametrize(
    "source",
    [
        "sso=planted-sso-secret",
        "sso-rw=planted-sso-rw-secret",
        'sso="planted-quoted-cookie-secret"',
        "cf_clearance=planted-clearance-secret",
        "grok_device_id=planted-device-secret",
        "Cookie: sso=planted-cookie-secret",
        "Set-Cookie: sso-rw=planted-set-cookie-secret",
        "xai-planted-api-token-1234567890",
        "/asset?X-Amz-Credential=planted-credential&X-Amz-Signature=planted-signature"
        "&X-Amz-Security-Token=planted-security-token",
        "identity_id=identity-private-3 accountId=account-private-8",
    ],
)
def test_grok_free_text_redacts_credentials_and_identity_fields(source: str) -> None:
    rendered = redact(source)

    assert isinstance(rendered, str)
    for secret in (
        "planted-sso-secret",
        "planted-sso-rw-secret",
        "planted-quoted-cookie-secret",
        "planted-clearance-secret",
        "planted-device-secret",
        "planted-cookie-secret",
        "planted-set-cookie-secret",
        "xai-planted-api-token-1234567890",
        "planted-credential",
        "planted-signature",
        "planted-security-token",
        "identity-private-3",
        "account-private-8",
    ):
        assert secret not in rendered


def test_grok_opaque_ids_are_not_globally_erased_from_structured_fields() -> None:
    for value in (
        GROK_CONVERSATION_ID,
        GROK_RESPONSE_ID,
        GROK_ATTACHMENT_ID,
        GROK_UUID_ID,
        GROK_UUID_LIKE_ID,
    ):
        assert redact(value) == value
