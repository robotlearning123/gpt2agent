"""Shared browser-transport gate + construction for conversation tools."""
from __future__ import annotations

from pathlib import Path


def browser_transport(bcfg: dict | None, tool: str):
    """Gate on ``[browser] enabled`` and build a BrowserTransport.

    ``BrowserTransport`` is imported lazily at call time so neither this
    module nor its importers make the optional ``gpt2agent[browser]`` extra
    mandatory.
    """
    bcfg = bcfg or {}
    if not bcfg.get("enabled"):
        raise RuntimeError(
            f"{tool}(browser=True) requires [browser] enabled = true in "
            'config.toml (and the optional extra: pip install '
            '"gpt2agent[browser]")'
        )
    from gpt2agent.browser import BrowserTransport

    profile = bcfg.get("profile_dir")
    return BrowserTransport(
        profile_dir=Path(profile).expanduser() if profile else None,
        headed=bool(bcfg.get("headed", True)),
        timeout_s=bcfg.get("timeout_s", 180),
    )
