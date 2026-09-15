"""Phase 1 — browser transport: drive chatgpt.com in a real Chrome via Playwright.

While the upstream Sentinel/Turnstile challenge blocks
``/backend-api/conversation``, this module sends ``chat`` prompts through a
persistent Chrome profile the way a human would. Playwright is an OPTIONAL
extra (``gpt2agent[browser]``) — it is imported lazily inside ``chat()`` so
this module (and ``gpt2agent.server``) import fine without it.

Fail-closed contract: any missing page element raises ``BrowserDriftError``
naming the constant that failed and the action attempted — never a silent
fallback and never an unbounded hang.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# === selectors — the ONLY place selectors live ===
SEL_PROMPT = "#prompt-textarea"
SEL_SEND = 'button[data-testid="send-button"]'
SEL_TEMPORARY = 'button[aria-label*="Temporary"], button[data-testid*="temporary"]'
SEL_MODEL_BUTTON = 'button[data-testid="model-switcher-dropdown-button"]'
SEL_MODEL_OPTION = '[role="menuitem"], [role="option"]'
SEL_STREAMING = 'button[data-testid="stop-button"], .result-streaming'
SEL_ASSISTANT = '[data-message-author-role="assistant"]'
SEL_LOGGED_OUT = 'button[data-testid="login-button"], a[href*="auth/login"]'
# === end selectors ===

_CHATGPT_URL = "https://chatgpt.com/"
_DEFAULT_PROFILE_DIR = Path.home() / ".gpt2agent" / "chrome-profile"
_INSTALL_HINT = 'pip install "gpt2agent[browser]"'


class BrowserDriftError(RuntimeError):
    """Raised when a chatgpt.com page element the transport needs is missing.

    The message names the failed constant and the action that was in flight —
    a signal to update the constants block, never to retry blindly.
    """


def _load_playwright() -> Any:
    """Import playwright lazily so the module loads without the extra."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "browser transport requires the optional extra: "
            f'{_INSTALL_HINT} (then `playwright install` is NOT needed — '
            "the system Chrome is used via channel='chrome')"
        ) from exc
    return async_playwright


def _drift(name: str, action: str) -> BrowserDriftError:
    return BrowserDriftError(
        f"{name} did not match while trying to {action} — the chatgpt.com "
        "page structure has likely drifted; update the constants block in "
        "gpt2agent/browser.py"
    )


class BrowserTransport:
    """Send a chat prompt through a real Chrome window on chatgpt.com."""

    def __init__(
        self,
        profile_dir: Path | None = None,
        headed: bool = True,
        timeout_s: float = 180.0,
    ) -> None:
        self.profile_dir = profile_dir if profile_dir is not None else _DEFAULT_PROFILE_DIR
        self.headed = headed
        self.timeout_s = timeout_s

    async def chat(
        self,
        prompt: str,
        model: str | None = None,
        temporary: bool = True,
    ) -> str:
        """Run one chatgpt.com chat turn and return the assistant reply text."""
        async_playwright = _load_playwright()
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="chrome",
                headless=not self.headed,
            )
            try:
                page = await ctx.new_page()
                await page.goto(_CHATGPT_URL)
                await self._await_composer(page)
                if temporary:
                    await self._click_required(
                        page, SEL_TEMPORARY, "SEL_TEMPORARY", "start a temporary chat"
                    )
                if model:
                    await self._pick_model(page, model)
                try:
                    await page.locator(SEL_PROMPT).press_sequentially(prompt)
                except TimeoutError as exc:
                    raise _drift("SEL_PROMPT", "type the prompt") from exc
                await self._click_required(
                    page, SEL_SEND, "SEL_SEND", "send the prompt"
                )
                await self._await_reply_done(page)
                reply = page.locator(SEL_ASSISTANT).last
                if await reply.count() == 0:
                    raise _drift("SEL_ASSISTANT", "read the assistant reply")
                return await reply.inner_text()
            finally:
                await ctx.close()

    async def _await_composer(self, page: Any) -> None:
        """Wait for the composer; on miss, distinguish logged-out from drift."""
        composer = page.locator(SEL_PROMPT).first
        try:
            await composer.wait_for(state="visible", timeout=self.timeout_s * 1000)
            return
        except TimeoutError:
            pass
        if await page.locator(SEL_LOGGED_OUT).count() > 0:
            # One-time interactive login into the persistent profile — the
            # only human step, once ever; the profile keeps the session.
            print("log in once in the opened window", flush=True)
            try:
                await composer.wait_for(
                    state="visible", timeout=self.timeout_s * 1000
                )
                return
            except TimeoutError:
                pass
        raise _drift("SEL_PROMPT", "find the chat composer")

    async def _click_required(
        self, page: Any, const: str, name: str, action: str
    ) -> None:
        try:
            await page.locator(const).first.click()
        except TimeoutError as exc:
            raise _drift(name, action) from exc

    async def _pick_model(self, page: Any, model: str) -> None:
        """Best-effort model switch: a missing picker is NOT an error."""
        btn = page.locator(SEL_MODEL_BUTTON).first
        if await btn.count() == 0:
            _log.warning(
                "model picker not present; sending without switching "
                "(wanted %r)",
                model,
            )
            return
        await btn.click()
        opt = page.locator(SEL_MODEL_OPTION).filter(has_text=model)
        if await opt.count() == 0:
            raise _drift("SEL_MODEL_OPTION", f"choose model {model!r}")
        await opt.first.click()

    async def _await_reply_done(self, page: Any) -> None:
        """Poll the streaming indicator until it disappears or times out."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            if await page.locator(SEL_STREAMING).count() == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timeout after {self.timeout_s}s waiting for the "
                    "assistant reply to finish streaming"
                )
            await asyncio.sleep(0.5)
