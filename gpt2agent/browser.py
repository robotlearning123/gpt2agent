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
# 2026-09-16 UI: the old model-switcher-dropdown-button testid is gone;
# the model picker is now a CHIP right of the composer (text like
# "6 Pro", aria-haspopup menu). Old testid kept first in case it returns.
SEL_MODEL_CHIP = ('button[data-testid="model-switcher-dropdown-button"], '
                  'form button[aria-haspopup="menu"]')
SEL_MODEL_OPTION = '[role="menuitem"], [role="option"]'
SEL_STREAMING = 'button[data-testid="stop-button"], .result-streaming'
SEL_ASSISTANT = '[data-message-author-role="assistant"]'
SEL_LOGGED_OUT = 'button[data-testid="login-button"], a[href*="auth/login"]'
SEL_MODES_BUTTON = 'button[data-testid="composer-plus-btn"], button[aria-label*="Tools"]'
SEL_MODE_AGENT = '[role="menuitem"]:has-text("Agent"), [role="option"]:has-text("Agent mode")'
SEL_MODE_DEEP_RESEARCH = '[role="menuitem"]:has-text("Deep research"), [role="option"]:has-text("Deep research")'
SEL_MODE_IMAGES = '[role="menuitem"]:has-text("Image"), [role="option"]:has-text("Create image")'
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


_MODE_OPTS = {
    "agent": ("SEL_MODE_AGENT", SEL_MODE_AGENT),
    "research": ("SEL_MODE_DEEP_RESEARCH", SEL_MODE_DEEP_RESEARCH),
    "images": ("SEL_MODE_IMAGES", SEL_MODE_IMAGES),
}


def _pw_timeout_errors() -> tuple[type[BaseException], ...]:
    """Playwright >=1.55 raises its OWN TimeoutError
    (playwright._impl._errors.TimeoutError -> Error -> Exception) which is
    NOT a subclass of the builtin — `except TimeoutError` never catches it.
    Return both classes so fail-closed paths fire in production; the test
    fakes expose their PW-style class through playwright.async_api too."""
    errs: list[type[BaseException]] = [TimeoutError]
    try:  # pragma: no cover - exercised via fakes in tests
        from playwright.async_api import TimeoutError as PWTimeout
        errs.append(PWTimeout)
    except Exception:
        pass
    return tuple(errs)


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
        self._timeout_errors: tuple[type[BaseException], ...] = (TimeoutError,)

    async def chat(
        self,
        prompt: str,
        model: str | None = None,
        temporary: bool = True,
        mode: str | None = None,
        url: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Run one chatgpt.com chat turn and return the assistant reply text.

        ``mode`` enables a composer mode ("agent", "research", "images")
        before typing — fail-closed via the constants block. ``url``
        overrides the chatgpt.com root (e.g. a Custom-GPT /g/ page).
        ``effort`` is a BEST-EFFORT reasoning-effort pick through the model
        picker: a missing picker or unmatched option logs a warning and
        proceeds, never raises.
        """
        if mode is not None and mode not in _MODE_OPTS:
            raise ValueError(
                f"unknown browser mode {mode!r} — expected one of "
                f"{sorted(_MODE_OPTS)}"
            )
        async_playwright = _load_playwright()
        self._timeout_errors = _pw_timeout_errors()
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="chrome",
                headless=not self.headed,
                # Playwright defaults to --password-store=basic, whose key
                # cannot decrypt cookies written by the desktop Chrome that
                # did the one-time login (measured 2026-09-16: sessions
                # vanished on every relaunch until this was dropped).
                ignore_default_args=["--password-store=basic"],
            )
            try:
                page = await ctx.new_page()
                await page.goto(url or _CHATGPT_URL)
                await self._await_composer(page)
                if temporary:
                    await self._click_required(
                        page, SEL_TEMPORARY, "SEL_TEMPORARY", "start a temporary chat"
                    )
                if mode:
                    await self._pick_mode(page, mode)
                if model:
                    await self._pick_model(page, model)
                if effort:
                    await self._pick_effort(page, effort)
                try:
                    await page.locator(SEL_PROMPT).press_sequentially(prompt)
                except self._timeout_errors as exc:
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
        """Wait for the composer; logged-out goes to the one-time login path
        immediately instead of burning the full composer timeout first."""
        if await page.locator(SEL_LOGGED_OUT).count() > 0:
            await self._await_login(page)
            return
        composer = page.locator(SEL_PROMPT).first
        try:
            await composer.wait_for(state="visible", timeout=self.timeout_s * 1000)
            return
        except self._timeout_errors:
            pass
        if await page.locator(SEL_LOGGED_OUT).count() > 0:
            await self._await_login(page)
            return
        raise _drift("SEL_PROMPT", "find the chat composer")

    async def _await_login(self, page: Any) -> None:
        """One-time interactive login into the persistent profile — the only
        human step, once ever; the profile keeps the session. Logged via
        WARNING (not print): stdout is the MCP stdio protocol channel."""
        _log.warning(
            "chatgpt.com is logged out in browser profile %s — log in once "
            "in the opened Chrome window (one-time; the profile persists the "
            "session); waiting up to %ss",
            self.profile_dir,
            self.timeout_s,
        )
        composer = page.locator(SEL_PROMPT).first
        try:
            await composer.wait_for(state="visible", timeout=self.timeout_s * 1000)
            return
        except self._timeout_errors:
            pass
        raise _drift("SEL_PROMPT", "find the chat composer after login")

    async def _click_required(
        self, page: Any, const: str, name: str, action: str
    ) -> None:
        try:
            await page.locator(const).first.click(
                timeout=self.timeout_s * 1000
            )
        except self._timeout_errors as exc:
            raise _drift(name, action) from exc

    async def _pick_mode(self, page: Any, mode: str) -> None:
        """Enable a composer mode via the tools menu — fail-closed on a miss."""
        name, const = _MODE_OPTS[mode]
        await self._click_required(
            page, SEL_MODES_BUTTON, "SEL_MODES_BUTTON",
            "open the composer tools menu",
        )
        await self._click_required(page, const, name, f"enable {mode} mode")

    async def _pick_effort(self, page: Any, effort: str) -> None:
        """Best-effort reasoning-effort pick through the model picker: a
        missing picker OR unmatched option only warns and proceeds (unlike
        _pick_model, where an option miss is fail-closed drift)."""
        btn = page.locator(SEL_MODEL_CHIP).first
        if await btn.count() == 0:
            _log.warning(
                "effort picker not present; sending without switching "
                "(wanted %r)",
                effort,
            )
            return
        try:
            await btn.click(timeout=self.timeout_s * 1000)
            opt = page.locator(SEL_MODEL_OPTION).filter(has_text=effort)
            if await opt.count() == 0:
                _log.warning(
                    "no effort option matching %r; proceeding with the default",
                    effort,
                )
                await self._dismiss_menu(page)
                return
            await opt.first.click(timeout=self.timeout_s * 1000)
        except self._timeout_errors:
            # effort is best-effort by spec: never abort the conversation
            _log.warning(
                "effort picker not actionable (wanted %r); proceeding "
                "with the default",
                effort,
            )
            await self._dismiss_menu(page)

    async def _dismiss_menu(self, page: Any) -> None:
        """Best-effort dropdown dismissal so typing cannot hit an open menu."""
        kb = getattr(page, "keyboard", None)
        if kb is None:
            return
        try:
            await kb.press("Escape")
        except self._timeout_errors:
            pass

    async def _pick_model(self, page: Any, model: str) -> None:
        """Best-effort model switch: a missing picker is NOT an error."""
        btn = page.locator(SEL_MODEL_CHIP).first
        if await btn.count() == 0:
            _log.warning(
                "model picker not present; sending without switching "
                "(wanted %r)",
                model,
            )
            return
        try:
            await btn.click(timeout=self.timeout_s * 1000)
            opt = page.locator(SEL_MODEL_OPTION).filter(has_text=model)
            if await opt.count() == 0:
                raise _drift("SEL_MODEL_OPTION", f"choose model {model!r}")
            await opt.first.click(timeout=self.timeout_s * 1000)
        except self._timeout_errors:
            # Best-effort like the missing-picker path: a picker that is
            # present but not actionable must not abort the conversation
            # (grok round-2 residual, wrapped for consistency). An option
            # MISS stays fail-closed via _drift, which is a RuntimeError and
            # therefore not caught by this timeout-tuple handler.
            _log.warning(
                "model picker not actionable (wanted %r); sending with the "
                "current model",
                model,
            )
            await self._dismiss_menu(page)

    async def _await_reply_done(self, page: Any) -> None:
        """Two-phase wait (grok review finding, 2026-09-15): right after send
        the streaming indicator has not rendered yet, so its ABSENCE is the
        pre-send DOM, not completion. Phase 1 — wait until the reply STARTS
        (streaming indicator or assistant node appears). Phase 2 — wait until
        the streaming indicator disappears. Ultra-fast replies that finish
        before any indicator is seen still pass: the assistant node starts
        phase 2, and one more poll confirms the indicator is absent."""
        deadline = time.monotonic() + self.timeout_s
        started = False
        while True:
            streaming = await page.locator(SEL_STREAMING).count()
            if started and streaming == 0:
                return
            if not started and (
                streaming > 0 or await page.locator(SEL_ASSISTANT).count() > 0
            ):
                started = True
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timeout after {self.timeout_s}s waiting for the "
                    "assistant reply to finish streaming"
                )
            await asyncio.sleep(0.5)
