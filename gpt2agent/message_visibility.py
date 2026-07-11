"""Fail-closed visibility checks for ChatGPT conversation messages."""

from __future__ import annotations

from typing import Any


def is_user_visible_message(message: Any) -> bool:
    """Return whether a backend message is safe to expose as transcript output.

    ChatGPT uses ``metadata.is_visually_hidden_from_conversation`` for internal
    dispatch and widget carriers.  A malformed explicit visibility value is
    treated as hidden; messages from older response shapes that omit the field
    remain visible.
    """
    if not isinstance(message, dict):
        return False
    if "metadata" not in message:
        return True
    metadata = message["metadata"]
    if not isinstance(metadata, dict):
        return False
    if "is_visually_hidden_from_conversation" not in metadata:
        return True
    return metadata["is_visually_hidden_from_conversation"] is False
