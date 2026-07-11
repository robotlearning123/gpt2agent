"""Versioned public tool-name manifest shared by resources and capabilities."""

from __future__ import annotations

from gpt2agent.tool_contracts import TOOL_ANNOTATION_MANIFEST


TOOL_NAMES: tuple[str, ...] = tuple(TOOL_ANNOTATION_MANIFEST)


def exposes(tool_name: str) -> bool:
    return tool_name in TOOL_ANNOTATION_MANIFEST
