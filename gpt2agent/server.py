"""gpt2agent — MCP server backed by native chatgpt.com SSE client."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from mcp.server.fastmcp import FastMCP

# ── config ──────────────────────────────────────────────────────────────────

_CONFIG_SEARCH = [
    Path.home() / ".gpt2agent" / "config.toml",
    Path("config.toml"),
    Path.home() / ".config" / "gpt2agent" / "config.toml",
]

_DEFAULTS: dict[str, Any] = {
    # Loopback by default: the HTTP transport has no authentication and proxies a
    # full ChatGPT account, so binding all interfaces would expose the account to
    # the LAN/WAN. Set host explicitly (and GPT2AGENT_ALLOW_REMOTE=1) to opt in.
    "server": {"host": "127.0.0.1", "port": 9000},
    "models": {
        "chat": "gpt-5-6",
        # Optional slug to retry with when the requested model hits the
        # account usage cap (UsageLimitError). None = surface the error.
        "fallback": None,
    },
    # Phase-1 browser transport (gpt2agent/browser.py): drives chatgpt.com in
    # a real Chrome via Playwright while the sentinel challenge blocks the
    # conversation endpoint. Off unless [browser] enabled = true.
    "browser": {"enabled": False, "headed": True, "timeout_s": 180},
    # Website-simulation identity (gpt2agent/sim.py): the shared session
    # fingerprint used for sentinel mint → conversation POST. All optional;
    # sensible browser-like defaults apply.
    "sentinel": {
        "timezone": None,     # IANA name, e.g. "America/New_York"
        "locale": None,       # e.g. "en-US"
        "impersonate": None,  # curl_cffi profile, e.g. "chrome136"
        "screen": None,       # "WxH", e.g. "1920x1080"
    },
    # Shared client-side budget (gpt2agent/ratelimit.py): protects the
    # account-level quotas when many agents share this account. State is
    # file-backed (~/.gpt2agent/ratelimit-state.json) so separate gpt2agent
    # processes share one budget. GPT2AGENT_RATELIMIT_OFF=1 disables.
    "rate_limit": {
        "enabled": True,
        "min_interval_s": 15.0,      # min gap between conversation POSTs
        "read_min_interval_s": 1.0,  # bookkeeping GET pacing
        "max_per_window": 100,       # conversation POSTs per window
        "window_s": 10800,           # sliding window (3h)
        "max_wait_s": 300,           # queue up to this, then error
    },
}

# Hosts that keep the unauthenticated HTTP transport reachable only from the
# local machine. Anything else requires an explicit GPT2AGENT_ALLOW_REMOTE opt-in.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})

# Prompt wrapper for memory_create_via_chat — shared by the REST/SSE path and
# the manual=True handoff so both send byte-identical text.
_MEMORY_PROMPT_PREFIX = (
    "Please commit the following to memory verbatim. "
    "Do not summarize, paraphrase, or ask for confirmation:\n\n"
)


def _http_bind_decision(host: str, allow_remote: bool) -> str:
    """Classify an HTTP bind request: ``ok-loopback`` | ``ok-remote`` | ``refuse``.

    The HTTP transport is unauthenticated and proxies a full ChatGPT account, so
    binding a non-loopback interface is refused unless the operator explicitly
    opts in via ``GPT2AGENT_ALLOW_REMOTE=1`` (``allow_remote``).
    """
    if host in _LOOPBACK_HOSTS:
        return "ok-loopback"
    return "ok-remote" if allow_remote else "refuse"


def load_config(path: Path | None = None) -> dict[str, Any]:
    # An explicitly-requested config that doesn't exist is a user error (likely a
    # typo) — fail loudly instead of silently falling back to defaults.
    if path is not None and not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    candidates = [path] if path else _CONFIG_SEARCH
    for p in candidates:
        if p and p.exists():
            with open(p, "rb") as f:
                data = tomllib.load(f)
            merged = {k: dict(v) for k, v in _DEFAULTS.items()}
            for section, values in data.items():
                # A top-level scalar (`port = 9001` without a [server] header)
                # is a user error — fail with an actionable message, not the
                # bare TypeError dict.update() would raise.
                if not isinstance(values, dict):
                    raise ValueError(
                        f"config {p}: top-level key {section!r} must live inside "
                        f"a section header such as [server] or [models]"
                    )
                merged.setdefault(section, {}).update(values)
            return merged
    return {k: dict(v) for k, v in _DEFAULTS.items()}


# ── server ───────────────────────────────────────────────────────────────────


def _dr_incomplete_note(timed_out: bool) -> str:
    """Marker appended when a DR stream never reached finished_successfully.

    Without it, a truncated or timed-out report is indistinguishable from a
    complete one — the caller would archive partial research as final.
    """
    note = (
        "\n\n---\n**⚠ Report may be incomplete** — the stream ended before the "
        "server marked the response finished"
    )
    if timed_out:
        note += " (completion polling timed out)"
    return note + ". Retry, or use get_conversation to check for a fuller report."


def build_server(cfg: dict[str, Any]) -> FastMCP:
    srv = cfg["server"]
    models = cfg["models"]

    from gpt2agent.backend import (
        BackendClient,
        UpstreamChallengeError,
        UsageLimitError,
    )
    from gpt2agent.ratelimit import get_limiter
    from gpt2agent.sse import ConversationClient
    from gpt2agent.sim import get_profile
    from gpt2agent.tools._browser import browser_transport
    from gpt2agent.tools.manual import build_handoff, gpt_chat_url

    # Configure the shared simulation profile before any client reads it —
    # get_profile() is a singleton; first caller wins.
    get_profile(cfg)
    # Same for the shared rate limiter (file-backed, cross-process).
    get_limiter(cfg)
    _backend = BackendClient()
    conv = ConversationClient(_backend)

    mcp = FastMCP(
        "gpt2agent",
        host=str(srv.get("host", "127.0.0.1")),
        port=int(srv.get("port", 9000)),
        log_level="WARNING",
    )

    chat_model = models.get("chat", "gpt-5-6")
    agent_model = models.get("agent", "agent-mode")
    heavy_dr_model = models.get("heavy_dr")  # None → ConversationClient uses sse.HEAVY_DR_MODEL

    _DR_IMPERATIVE_PREFIX = (
        "Begin the deep research immediately without asking for confirmation. "
        "Do not ask clarifying questions; proceed with the best interpretation. "
    )

    async def _chat_via_browser(prompt: str, **kw) -> str:
        """Browser-lane fallback when the direct path is challenge-blocked."""
        transport = browser_transport(cfg.get("browser", {}), "chat")
        out = await transport.chat(prompt, **kw)
        return (
            "**Note** — direct backend was challenge-blocked; answered via "
            "the browser lane.\n\n" + (out or "(no response)")
        )

    @mcp.tool()
    async def chat(
        prompt: str,
        model: str = chat_model,
        temporary: bool = True,
        browser: bool = False,
        manual: bool = False,
    ) -> str:
        """Chat with any ChatGPT model on your account.

        Pass `model` to switch slugs — e.g. `gpt-5-5-pro` (410K, pro reasoning),
        `gpt-6-pro`, `o3-pro`, `gpt-5-6-thinking`, `gpt-5-6` (default). Call `list_models`
        first to enumerate what your account has access to.

        Set `temporary=False` to allow tool-based features (image gen, code
        interpreter, canvas). Temporary chats (default) cannot use these tools.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        """
        if manual:
            return json.dumps(
                build_handoff("chat", prompt, model=model, temporary=temporary),
                indent=2,
            )
        if browser:
            transport = browser_transport(cfg.get("browser", {}), "chat")
            return await transport.chat(prompt, model=model, temporary=temporary)
        try:
            text = await conv.complete(
                model, [{"role": "user", "content": prompt}], temporary=temporary
            )
        except UpstreamChallengeError:
            if cfg.get("browser", {}).get("enabled"):
                return await _chat_via_browser(
                    prompt, model=model, temporary=temporary
                )
            raise
        except UsageLimitError:
            fallback = models.get("fallback")
            if not fallback or fallback == model:
                raise
            text = await conv.complete(
                fallback,
                [{"role": "user", "content": prompt}],
                temporary=temporary,
            )
            return (
                f"**Note** — `{model}` is usage-capped on this account; "
                f"answered with configured fallback `{fallback}`.\n\n"
                + (text or "(no response)")
            )
        return text or "(no response)"

    @mcp.tool()
    async def agent(
        prompt: str, browser: bool = False, manual: bool = False
    ) -> str:
        """ChatGPT Agent Mode — 262K context with autonomous browsing, code
        execution, and tool use. Best for multi-step tasks (literature gathering,
        document workflows, browser automation). SSE-only (no REST endpoint).

        Returns "(no response)" if the agent run times out rather than an empty
        string, so callers can tell a timeout apart from a real empty answer.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        """
        if manual:
            return json.dumps(
                build_handoff(
                    "agent", prompt, model=None, temporary=False,
                    extra={"mode": "agent"},
                ),
                indent=2,
            )
        if browser:
            transport = browser_transport(cfg.get("browser", {}), "agent")
            return await transport.chat(prompt, temporary=False, mode="agent")
        try:
            text = await conv.complete(
                agent_model,
                [{"role": "user", "content": prompt}],
                temporary=False,
                poll_async=True,  # agent mode runs async — poll the conversation
            )
        except UpstreamChallengeError:
            if cfg.get("browser", {}).get("enabled"):
                transport = browser_transport(cfg.get("browser", {}), "agent")
                out = await transport.chat(
                    prompt, temporary=False, mode="agent"
                )
                return (
                    "**Note** — direct backend was challenge-blocked; answered "
                    "via the browser lane.\n\n" + (out or "(no response)")
                )
            raise
        return text or "(no response)"

    @mcp.tool()
    async def deep_research(
        query: str, auto_confirm: bool = True, browser: bool = False,
        manual: bool = False,
    ) -> str:
        """Search the web and synthesize a detailed report with citations.

        Best for: current events, literature review, market research.
        Takes 30–120 seconds. Uses model='research' + system_hints=['research'].

        When `auto_confirm` is True (default), an imperative prefix is prepended
        so the model proceeds without asking "Do you want me to start?".

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        The browser path returns the report text without a Sources section.
        """
        q = _DR_IMPERATIVE_PREFIX + query if auto_confirm else query
        if manual:
            return json.dumps(
                build_handoff(
                    "deep_research", q, model=None, temporary=False,
                    extra={"mode": "deep_research"},
                ),
                indent=2,
            )
        if browser:
            transport = browser_transport(
                cfg.get("browser", {}), "deep_research")
            return await transport.chat(q, temporary=False, mode="research")
        final_text = ""
        tool_calls: list[str] = []
        refs: list = []
        truncated = False
        timed_out = False

        try:
            async for event in conv.deep_research(q):
                if event["type"] == "tool":
                    tool_calls.append(event["call"])
                elif event["type"] == "done":
                    final_text = event["text"]
                    refs = event.get("content_references", [])
                    truncated = bool(event.get("terminated_abnormally"))
                    timed_out = bool(event.get("timeout"))
        except UpstreamChallengeError:
            if cfg.get("browser", {}).get("enabled"):
                transport = browser_transport(
                    cfg.get("browser", {}), "deep_research")
                out = await transport.chat(q, temporary=False, mode="research")
                return (
                    "**Note** — direct backend was challenge-blocked; answered "
                    "via the browser lane (no Sources section).\n\n"
                    + (out or "(no response)")
                )
            raise

        # Append a brief sources section if citations were returned
        if refs:
            lines = ["\n\n---\n**Sources:**"]
            seen: set[str] = set()
            for ref in refs:
                for item in ref.get("items", []):
                    url = item.get("url", "")
                    title = item.get("title", url)
                    if url and url not in seen:
                        seen.add(url)
                        lines.append(f"- [{title}]({url})")
            final_text += "\n".join(lines)

        if truncated:
            final_text += _dr_incomplete_note(timed_out)

        return final_text or "(no response)"

    @mcp.tool()
    async def deep_research_heavy(
        query: str, auto_confirm: bool = True, browser: bool = False,
        manual: bool = False,
    ) -> str:
        """Long-form Deep Research using gpt-6-pro (5–30 min, uses monthly DR quota — check /backend-api/conversation/init for remaining). For short web-augmented answers use `deep_research` instead.

        When `auto_confirm` is True (default), an imperative prefix is prepended
        so the model proceeds without asking "Do you want me to start?".

        Returns the report text with a Sources section appended. Citations are
        recovered from the connector's widget state; grouped source URLs are
        usually present but not guaranteed — if absent, the model may have cited
        sources inline in the body. Returns "(no response)" on timeout.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        The browser path returns the report text without a Sources section.
        """
        q = _DR_IMPERATIVE_PREFIX + query if auto_confirm else query
        if manual:
            return json.dumps(
                build_handoff(
                    "deep_research_heavy", q, model=None, temporary=False,
                    extra={"mode": "deep_research"},
                ),
                indent=2,
            )
        if browser:
            transport = browser_transport(
                cfg.get("browser", {}), "deep_research_heavy")
            # The browser effort picker wants effort LABELS ("Pro"), not the
            # REST model slug from [models].heavy_dr — passing the slug was a
            # silent best-effort no-op (grok finding, 2026-09-16).
            return await transport.chat(
                q, temporary=False, mode="research", effort="Pro")
        final_text = ""
        refs: list = []
        connector_failed = False
        tool_error_msg = ""
        truncated = False
        timed_out = False

        async for event in conv.deep_research_heavy(q, model=heavy_dr_model):
            etype = event.get("type")
            if etype == "done":
                final_text = event["text"]
                refs = event.get("content_references", [])
                if event.get("connector_failed"):
                    connector_failed = True
                truncated = bool(event.get("terminated_abnormally"))
                timed_out = bool(event.get("timeout"))
            elif etype == "tool_error":
                tool_error_msg = event.get("message", "")

        if refs:
            lines = ["\n\n---\n**Sources:**"]
            seen: set[str] = set()
            for ref in refs:
                for item in ref.get("items", []):
                    url = item.get("url", "")
                    title = item.get("title", url)
                    if url and url not in seen:
                        seen.add(url)
                        lines.append(f"- [{title}]({url})")
            final_text += "\n".join(lines)

        if connector_failed:
            warning = (
                "\n\n---\n**⚠ DR connector unavailable** — the Deep Research "
                "connector (`connector_openai_deep_research`) returned an error, "
                "so this response came from the fallback orchestrator (i-mini-m) "
                "instead of the full Pro-tier DR pipeline. Enable the Deep "
                "Research source at chatgpt.com → Settings → Connectors, then "
                "retry."
            )
            if tool_error_msg:
                first_line = tool_error_msg.splitlines()[0][:200]
                warning += f"\n\n*Server message:* `{first_line}`"
            final_text += warning

        if truncated:
            final_text += _dr_incomplete_note(timed_out)

        return final_text or "(no response)"

    @mcp.tool()
    async def gpt_chat(
        gizmo_id: str, prompt: str, browser: bool = False,
        manual: bool = False,
    ) -> str:
        """Chat through one of your private Custom GPTs.

        `gizmo_id`: pass the `short_url` returned by `list_custom_gpts` (call it
        first to discover your Custom GPTs). The gizmo's instructions, files, and
        memory_scope apply to the reply.

        EXPERIMENTAL: passes `gizmo_id` into the conversation payload via the
        `conversation_origin` field reverse-engineered from chatgpt.com web
        bundles. Returns "(no response)" on timeout.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        The browser path navigates to the GPT's /g/ page directly.
        """
        if manual:
            return json.dumps(
                build_handoff(
                    "gpt_chat", prompt, model=None, temporary=False,
                    extra={"gizmo_id": gizmo_id},
                ),
                indent=2,
            )
        if browser:
            transport = browser_transport(cfg.get("browser", {}), "gpt_chat")
            return await transport.chat(
                prompt, temporary=False, url=gpt_chat_url(gizmo_id))
        text = await conv.complete(
            chat_model,
            [{"role": "user", "content": prompt}],
            gizmo_id=gizmo_id,
            temporary=False,
        )
        return text or "(no response)"

    @mcp.tool()
    async def memory_create_via_chat(
        content: str, browser: bool = False, manual: bool = False
    ) -> str:
        """Add an entry to your ChatGPT memories.

        Workaround for `POST /backend-api/memories` returning 405 — ChatGPT only
        allows model-initiated memory writes. This tool asks the model to remember
        the content directly, then returns the assistant's reply (which usually
        confirms what was stored). Use `memory_search` to verify after.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        """
        prompt = _MEMORY_PROMPT_PREFIX + content
        if manual:
            return json.dumps(
                build_handoff(
                    "memory_create_via_chat", prompt, model=None,
                    temporary=False,
                ),
                indent=2,
            )
        if browser:
            transport = browser_transport(
                cfg.get("browser", {}), "memory_create_via_chat")
            return await transport.chat(prompt, temporary=False)
        text = await conv.complete(
            chat_model, [{"role": "user", "content": prompt}], temporary=False
        )
        return text or "(no response)"

    try:
        from gpt2agent.tools import register_all

        register_all(mcp, _backend, conv, cfg)
    except Exception:
        # P0 #4 fix — log the traceback (was bare warning, hid the cause)
        logging.getLogger(__name__).exception(
            "backend tools registration failed — some MCP tools will be unavailable"
        )

    return mcp


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    from gpt2agent import __version__

    parser = argparse.ArgumentParser(
        prog="gpt2agent",
        description="Use your ChatGPT Plus/Pro in Claude Code and other AI agents.",
    )
    parser.add_argument(
        "--version", action="version", version=f"gpt2agent {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    # setup subcommand
    sub.add_parser("setup", help="First-time setup wizard (login + register)")

    # doctor subcommand — read-only health check against the live account
    sub.add_parser(
        "doctor",
        help="Probe the account read-only and report which tools work right now",
    )

    # install subcommand — register gpt2agent with one or more MCP clients
    from gpt2agent.install import SUPPORTED_CLIENTS

    install_p = sub.add_parser(
        "install",
        help="Register gpt2agent with an MCP client (or 'all' to auto-detect)",
    )
    install_p.add_argument(
        "--client",
        choices=[*SUPPORTED_CLIENTS, "all"],
        default="all",
        help="Target MCP client: " + ", ".join(SUPPORTED_CLIENTS) + ", or all "
        "(default: auto-detect installed clients)",
    )
    install_p.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio — preferred for Claude Code/Codex)",
    )
    install_p.add_argument(
        "--http-port",
        type=int,
        default=9000,
        help="Port for HTTP transport (default: 9000)",
    )
    install_p.add_argument(
        "--no-skill",
        action="store_true",
        help="Skip installing the Claude Code deep-research skill bundle",
    )
    install_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing any files",
    )

    # run (default)
    run_p = sub.add_parser("run", help="Start the MCP server")
    run_p.add_argument("--config", type=Path, help="Path to config.toml")
    run_p.add_argument("--port", type=int)
    run_p.add_argument("--host")
    run_p.add_argument(
        "--stdio", action="store_true", help="stdio transport (Claude Code legacy)"
    )

    # bare flags for backward compat: gpt2agent --stdio --config ...
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--host")
    parser.add_argument("--stdio", action="store_true")

    args = parser.parse_args()

    if args.command == "setup":
        from gpt2agent.setup import run_setup

        run_setup()
        return

    if args.command == "doctor":
        from gpt2agent.doctor import run_doctor

        raise SystemExit(run_doctor())

    if args.command == "install":
        from gpt2agent.install import run_install

        rc = run_install(
            client=args.client,
            transport=args.transport,
            http_port=args.http_port,
            install_skill=not args.no_skill,
            dry_run=args.dry_run,
        )
        raise SystemExit(rc)

    # default: run server
    cfg_path = getattr(args, "config", None)
    cfg = load_config(cfg_path)
    if getattr(args, "port", None):
        cfg["server"]["port"] = args.port
    if getattr(args, "host", None):
        cfg["server"]["host"] = args.host

    from gpt2agent.backend import TokenNotFoundError

    try:
        mcp = build_server(cfg)
    except TokenNotFoundError as exc:
        # Common first-run state: installed + registered with the MCP client, but
        # `codex login` / `gpt2agent setup` not run yet, so no token exists. Exit
        # with a clean, actionable one-liner (like the HTTP-bind refusal below)
        # instead of dumping a backend.py traceback into the client's MCP logs.
        raise SystemExit(str(exc)) from None
    tools = list(cfg["models"].keys())
    stdio = getattr(args, "stdio", False)

    if stdio:
        mcp.run(transport="stdio")
    else:
        host = cfg["server"]["host"]
        port = cfg["server"]["port"]
        # The HTTP transport has NO authentication and proxies a full ChatGPT
        # account (read history, spend DR quota, overwrite custom instructions,
        # launch Codex tasks). Refuse to bind a non-loopback interface unless the
        # operator explicitly opts in, so a stray `gpt2agent run` can't expose the
        # account to the LAN/WAN.
        decision = _http_bind_decision(
            host, os.environ.get("GPT2AGENT_ALLOW_REMOTE") == "1"
        )
        if decision == "refuse":
            raise SystemExit(
                f"Refusing to start the unauthenticated HTTP server on non-loopback "
                f"host {host!r}: this would expose your full ChatGPT account to the "
                f"network with no auth.\n"
                f"  • For local clients (Claude Code/Codex) use stdio: gpt2agent run --stdio\n"
                f"  • To bind {host!r} anyway (e.g. behind your own auth proxy), set "
                f"GPT2AGENT_ALLOW_REMOTE=1."
            )
        if decision == "ok-remote":
            print(
                f"⚠ gpt2agent: serving an UNAUTHENTICATED account proxy on {host}:{port} "
                f"(GPT2AGENT_ALLOW_REMOTE=1). Anyone who can reach this port controls "
                f"your ChatGPT account. Put it behind your own auth/firewall.",
                flush=True,
            )
        print(f"gpt2agent  http://{host}:{port}/mcp  [{', '.join(tools)}]", flush=True)
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
