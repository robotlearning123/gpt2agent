"""gpt2agent — MCP server backed by native chatgpt.com SSE client."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._redact import redact
from gpt2agent.tools._errors import SafeFastMCP as FastMCP

# ── config ──────────────────────────────────────────────────────────────────

_CONFIG_SEARCH = [
    Path.home() / ".gpt2agent" / "config.toml",
    Path("config.toml"),
    Path.home() / ".config" / "gpt2agent" / "config.toml",
]

_DEFAULTS: dict[str, Any] = {
    # Host and port are retained for config compatibility. Version 0.0.12
    # exposes only stdio because loopback TCP cannot isolate account access from
    # other users and processes on the same machine.
    "server": {"host": "127.0.0.1", "port": 9000},
    "models": {"chat": "gpt-5-3"},
}

# Canonical hosts retained for safe config normalization and compatibility.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})

# Citation metadata comes from an upstream widget payload, not from a typed API
# contract. Keep every input and output bounded before rendering it into MCP
# Markdown. These are deliberately module-level so the limits are visible to
# focused regression tests.
_MAX_CITATION_GROUPS = 50
_MAX_CITATION_ITEMS_INSPECTED = 200
_MAX_RENDERED_CITATIONS = 50
_MAX_CITATION_INPUT_URL_LENGTH = 4_096
_MAX_CITATION_URL_LENGTH = 2_048
_MAX_CITATION_TITLE_INPUT_LENGTH = 4_096
_MAX_CITATION_TITLE_LENGTH = 256
_MAX_CITATION_QUERY_FIELDS = 64
_MAX_CITATION_QUERY_KEY_LENGTH = 128
_MAX_CITATION_QUERY_VALUE_LENGTH = 512
_INTERNAL_CITATION_SUFFIXES = frozenset(
    {"corp", "home", "home.arpa", "internal", "lan", "local", "localdomain", "localhost"}
)

_MARKDOWN_TITLE_ESCAPES = frozenset("\\`*_[]{}()<>#+!|")
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "code",
        "cookie",
        "csrf",
        "jwt",
        "key",
        "policy",
        "secret",
        "session",
        "sig",
        "signature",
        "state",
        "token",
        "xsrf",
    }
)
_SENSITIVE_QUERY_COMPACT_PARTS = (
    "accesstoken",
    "apikey",
    "authorization",
    "authtoken",
    "bearer",
    "credential",
    "idtoken",
    "jwt",
    "oauth",
    "password",
    "passwd",
    "privatekey",
    "secretkey",
    "securitytoken",
    "sessionid",
    "sessionkey",
    "signature",
)
_TRACKING_QUERY_NAMES = frozenset(
    {"dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid", "yclid"}
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _valid_percent_encoding(value: str) -> bool:
    """Reject malformed percent escapes instead of rendering an ambiguous URL."""
    for index, char in enumerate(value):
        if char == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in _HEX_DIGITS
            or value[index + 2] not in _HEX_DIGITS
        ):
            return False
    return True


def _contains_percent_escape(value: str) -> bool:
    """Detect a second encoded layer after the one permitted URL decode."""
    return any(
        char == "%"
        and index + 2 < len(value)
        and value[index + 1] in _HEX_DIGITS
        and value[index + 2] in _HEX_DIGITS
        for index, char in enumerate(value)
    )


def _sensitive_citation_query_name(name: str) -> bool:
    """Return whether a query field is unsafe or nonessential to expose."""
    folded = name.casefold()
    if folded.startswith("utm_") or folded in _TRACKING_QUERY_NAMES:
        return True
    segments = tuple(part for part in folded.replace("-", "_").split("_") if part)
    if any(part in _SENSITIVE_QUERY_NAMES for part in segments):
        return True
    compact = "".join(char for char in folded if char.isascii() and char.isalnum())
    return any(part in compact for part in _SENSITIVE_QUERY_COMPACT_PARTS)


def _project_citation_host(hostname: str) -> str | None:
    """Canonicalize a URL host without accepting ambiguous host syntax."""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return None
    try:
        host = hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError:
        return None
    if not host or len(host) > 253:
        return None
    labels = host.split(".")
    if (
        len(labels) < 2
        or all(label.isdigit() for label in labels)
        or any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in _INTERNAL_CITATION_SUFFIXES
        )
        or any(
        not label
        or len(label) > 63
        or not label[0].isalnum()
        or not label[-1].isalnum()
        or any(not (char.isalnum() or char == "-") for char in label)
            for label in labels
        )
    ):
        return None
    return host


def _project_citation_url(value: object) -> str | None:
    """Return a bounded, normalized public HTTP(S) citation URL.

    Userinfo and fragments are never useful citation metadata. Sensitive,
    tracking, PII-bearing, and overlong query fields are discarded while benign
    fields are retained so links such as document selectors can keep working.
    """
    if type(value) is not str:
        return None
    raw = value.strip()
    if (
        not raw
        or len(raw) > _MAX_CITATION_INPUT_URL_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
    ):
        return None

    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            return None
        # Even empty userinfo (``https://@host``) makes a URL ambiguous and is
        # omitted rather than rewritten into a different authority.
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        return None
    if hostname is None:
        return None

    host = _project_citation_host(hostname)
    if host is None:
        return None
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        return None

    # Inspect exactly one decoded layer. A second percent-encoded layer is
    # ambiguous because a downstream service may decode it again; omit it rather
    # than risk exposing a doubly encoded secret. Query fields can instead be
    # filtered individually below.
    if not _valid_percent_encoding(parsed.path):
        return None
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError:
        return None
    if _contains_percent_escape(decoded_path):
        return None
    for path_value in (parsed.path, decoded_path):
        path_redacted = redact(path_value)
        if type(path_redacted) is not str or path_redacted != path_value:
            return None
    path = quote(parsed.path, safe="/:@!$&'()*+,;=-._~%")

    safe_query: list[tuple[str, str]] = []
    if parsed.query:
        try:
            fields = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=_MAX_CITATION_QUERY_FIELDS,
            )
        except ValueError:
            fields = []
        for key, query_value in fields:
            if (
                not key
                or len(key) > _MAX_CITATION_QUERY_KEY_LENGTH
                or len(query_value) > _MAX_CITATION_QUERY_VALUE_LENGTH
                or _contains_percent_escape(key)
                or _contains_percent_escape(query_value)
                or _sensitive_citation_query_name(key)
                or redact(key) != key
                or redact(query_value) != query_value
            ):
                continue
            safe_query.append((key, query_value))

    projected = urlunsplit((scheme, host, path, urlencode(safe_query), ""))
    if len(projected) > _MAX_CITATION_URL_LENGTH:
        return None
    return projected


def _project_citation_title(value: object) -> str:
    """Return a redacted, bounded Markdown-safe citation label."""
    if type(value) is not str:
        return "Source"
    bounded = value[:_MAX_CITATION_TITLE_INPUT_LENGTH]
    redacted = redact(bounded)
    if type(redacted) is not str:
        return "Source"
    title = " ".join(redacted.split())[:_MAX_CITATION_TITLE_LENGTH]
    if not title:
        return "Source"
    return "".join(f"\\{char}" if char in _MARKDOWN_TITLE_ESCAPES else char for char in title)


def _render_dr_sources(refs: object) -> str:
    """Project untrusted Deep Research references into a safe Sources block."""
    if type(refs) is not list:
        return ""

    entries: list[str] = []
    seen: set[str] = set()
    inspected = 0
    for ref in refs[:_MAX_CITATION_GROUPS]:
        if type(ref) is not dict:
            continue
        items = ref.get("items")
        if type(items) is not list:
            continue
        for item in items:
            if inspected >= _MAX_CITATION_ITEMS_INSPECTED:
                break
            inspected += 1
            if type(item) is not dict:
                continue
            url = _project_citation_url(item.get("url"))
            if url is None or url in seen:
                continue
            seen.add(url)
            title = _project_citation_title(item.get("title"))
            entries.append(f"- [{title}](<{url}>)")
            if len(entries) >= _MAX_RENDERED_CITATIONS:
                break
        if inspected >= _MAX_CITATION_ITEMS_INSPECTED or len(entries) >= _MAX_RENDERED_CITATIONS:
            break

    if not entries:
        return ""
    return "\n\n---\n**Sources:**\n" + "\n".join(entries)


def _canonical_loopback_host(host: object) -> str | None:
    normalized = str(host).strip().casefold()
    return normalized if normalized in _LOOPBACK_HOSTS else None


def _http_bind_decision(_host: str, _legacy_allow_remote: bool = False) -> str:
    """Refuse the unauthenticated HTTP account transport on every bind."""
    return "refuse"


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
    host = _canonical_loopback_host(srv.get("host", "127.0.0.1"))
    if host is None:
        raise ValueError("server.host must be a loopback address")

    from gpt2agent.backend import BackendClient
    from gpt2agent.grok_build import GrokBuildClient, GrokBuildConfig
    from gpt2agent.model_catalog import ModelCatalog
    from gpt2agent.sse import ConversationClient

    _backend = BackendClient()
    model_catalog = ModelCatalog(_backend)
    conv = ConversationClient(_backend)
    grok_build_client = GrokBuildClient(
        GrokBuildConfig.from_mapping(cfg.get("grok_build", {}))
    )

    mcp = FastMCP(
        "gpt2agent",
        host=host,
        port=int(srv.get("port", 9000)),
        log_level="WARNING",
    )

    chat_model = models.get("chat", "gpt-5-3")
    agent_model = models.get("agent", "agent-mode")
    heavy_dr_model = models.get("heavy_dr")  # None → ConversationClient uses sse.HEAVY_DR_MODEL

    _DR_IMPERATIVE_PREFIX = (
        "Begin the deep research immediately without asking for confirmation. "
        "Do not ask clarifying questions; proceed with the best interpretation. "
    )

    @mcp.tool(annotations=tool_annotations("chat"))
    async def chat(
        prompt: str,
        model: str = chat_model,
        temporary: bool = True,
        thinking_effort: str | None = None,
    ) -> str:
        """Chat with any ChatGPT model on your account.

        Pass `model` to switch slugs — e.g. `gpt-5-5-pro` (410K, pro reasoning),
        `o3-pro`, `gpt-5-4-thinking`, `gpt-5-3` (default). Call `list_models`
        first to enumerate what your account has access to.

        Set `temporary=False` to allow tool-based features (image gen, code
        interpreter, canvas). Temporary chats (default) cannot use these tools.
        Every completion ends with one authoritative final activity receipt,
        using a bounded category or `none`. Trust only the final footer; private
        dispatch and response payloads are never included.
        """
        auth_snapshot = _backend.auth_snapshot()
        await model_catalog.validate_general(
            model, thinking_effort, auth_snapshot=auth_snapshot
        )
        text = await conv.complete(
            model,
            [{"role": "user", "content": prompt}],
            temporary=temporary,
            thinking_effort=thinking_effort,
            auth_headers=auth_snapshot[1],
        )
        return text or "(no response)"

    @mcp.tool(annotations=tool_annotations("agent"))
    async def agent(prompt: str) -> str:
        """ChatGPT Agent Mode — 262K context with autonomous browsing, code
        execution, and tool use. Best for multi-step tasks (literature gathering,
        document workflows, browser automation). SSE-only (no REST endpoint).

        If polling times out without a completed assistant message, returns
        "(no final assistant response)" followed by the authoritative final
        activity receipt. The receipt uses a bounded category or `none` and
        never exposes hidden dispatch or response payloads.
        """
        auth_snapshot = _backend.auth_snapshot()
        await model_catalog.validate_general(
            agent_model, None, auth_snapshot=auth_snapshot
        )
        text = await conv.complete(
            agent_model,
            [{"role": "user", "content": prompt}],
            temporary=False,
            poll_async=True,  # agent mode runs async — poll the conversation
            auth_headers=auth_snapshot[1],
        )
        return text or "(no response)"

    @mcp.tool(annotations=tool_annotations("deep_research"))
    async def deep_research(query: str, auto_confirm: bool = True) -> str:
        """Search the web and synthesize a detailed report with citations.

        Best for: current events, literature review, market research.
        Takes 30–120 seconds. Uses model='research' + system_hints=['research'].

        When `auto_confirm` is True (default), an imperative prefix is prepended
        so the model proceeds without asking "Do you want me to start?".
        """
        q = _DR_IMPERATIVE_PREFIX + query if auto_confirm else query
        final_text = ""
        tool_calls: list[str] = []
        refs: object = []
        truncated = False
        timed_out = False

        async for event in conv.deep_research(q):
            if event["type"] == "tool":
                tool_calls.append(event["call"])
            elif event["type"] == "done":
                final_text = event["text"]
                refs = event.get("content_references", [])
                truncated = bool(event.get("terminated_abnormally"))
                timed_out = bool(event.get("timeout"))

        final_text += _render_dr_sources(refs)

        if truncated:
            final_text += _dr_incomplete_note(timed_out)

        return final_text or "(no response)"

    @mcp.tool(annotations=tool_annotations("deep_research_heavy"))
    async def deep_research_heavy(query: str, auto_confirm: bool = True) -> str:
        """Long-form Deep Research using gpt-5-5-pro (5–30 min, uses monthly DR quota — check /backend-api/conversation/init for remaining). For short web-augmented answers use `deep_research` instead.

        When `auto_confirm` is True (default), an imperative prefix is prepended
        so the model proceeds without asking "Do you want me to start?".

        Returns the report text with a Sources section appended. Citations are
        recovered from the connector's widget state; grouped source URLs are
        usually present but not guaranteed — if absent, the model may have cited
        sources inline in the body. Returns "(no response)" on timeout.
        """
        q = _DR_IMPERATIVE_PREFIX + query if auto_confirm else query
        final_text = ""
        refs: object = []
        connector_failed = False
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

        final_text += _render_dr_sources(refs)

        if connector_failed:
            warning = (
                "\n\n---\n**⚠ DR connector unavailable** — the Deep Research "
                "connector (`connector_openai_deep_research`) returned an error, "
                "so this response came from the fallback orchestrator (i-mini-m) "
                "instead of the full Pro-tier DR pipeline. Enable the Deep "
                "Research source at chatgpt.com → Settings → Connectors, then "
                "retry."
            )
            final_text += warning

        if truncated:
            final_text += _dr_incomplete_note(timed_out)

        return final_text or "(no response)"

    @mcp.tool(annotations=tool_annotations("gpt_chat"))
    async def gpt_chat(gizmo_id: str, prompt: str) -> str:
        """Chat through one of your private Custom GPTs.

        `gizmo_id`: pass the `short_url` returned by `list_custom_gpts` (call it
        first to discover your Custom GPTs). The gizmo's instructions, files, and
        memory_scope apply to the reply.

        EXPERIMENTAL: passes `gizmo_id` into the conversation payload via the
        `conversation_origin` field reverse-engineered from chatgpt.com web
        bundles. Every completion ends with one authoritative final bounded
        category receipt, including `none`; hidden payloads are withheld.
        """
        text = await conv.complete(
            chat_model,
            [{"role": "user", "content": prompt}],
            gizmo_id=gizmo_id,
            temporary=False,
        )
        return text or "(no response)"

    @mcp.tool(annotations=tool_annotations("memory_create_via_chat"))
    async def memory_create_via_chat(content: str) -> str:
        """Add an entry to your ChatGPT memories.

        Workaround for `POST /backend-api/memories` returning 405 — ChatGPT only
        allows model-initiated memory writes. This tool asks the model to remember
        the content directly, then returns the assistant's reply (which usually
        confirms what was stored). Use `memory_search` to verify after.
        """
        prompt = (
            "Please commit the following to memory verbatim. "
            "Do not summarize, paraphrase, or ask for confirmation:\n\n" + content
        )
        text = await conv.complete(
            chat_model, [{"role": "user", "content": prompt}], temporary=False
        )
        return text or "(no response)"

    from gpt2agent.tools import register_all

    register_all(
        mcp,
        _backend,
        conv,
        model_catalog=model_catalog,
        grok_build_client=grok_build_client,
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

    grok_setup_p = sub.add_parser(
        "grok-setup",
        help="Configure independent Grok Build and website authentication",
    )
    grok_setup_p.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the stored website authentication",
    )
    grok_setup_p.add_argument(
        "--chrome-profile",
        default="Default",
        help="Chrome profile selected for browser-assisted website setup",
    )
    grok_setup_p.add_argument(
        "--manual",
        action="store_true",
        help="Use hidden manual cookie input instead of browser assistance",
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
        choices=["stdio"],
        default="stdio",
        help="MCP transport (stdio only in 0.0.12)",
    )
    install_p.add_argument(
        "--http-port",
        type=int,
        default=9000,
        help="deprecated compatibility option; HTTP is disabled",
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
    run_p = sub.add_parser(
        "run",
        help="Start the MCP server",
        argument_default=argparse.SUPPRESS,
    )
    run_p.add_argument("--config", type=Path, help="Path to config.toml")
    run_p.add_argument("--port", type=int)
    run_p.add_argument("--host")
    run_transport = run_p.add_mutually_exclusive_group()
    run_transport.add_argument(
        "--stdio",
        action="store_true",
        help="stdio transport (default; preferred for local MCP clients)",
    )
    run_transport.add_argument(
        "--http",
        action="store_true",
        help="deprecated compatibility flag; HTTP is disabled in 0.0.12",
    )

    # bare flags for backward compat: gpt2agent --stdio --config ...
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--host")
    bare_transport = parser.add_mutually_exclusive_group()
    bare_transport.add_argument("--stdio", action="store_true")
    bare_transport.add_argument("--http", action="store_true")

    args = parser.parse_args()

    if args.command == "setup":
        from gpt2agent.setup import run_setup

        run_setup()
        return

    if args.command == "grok-setup":
        import asyncio
        import json

        from gpt2agent.grok_setup import run_grok_setup

        receipt = asyncio.run(
            run_grok_setup(
                refresh=args.refresh,
                chrome_profile=args.chrome_profile,
                manual=args.manual,
            )
        )
        print(json.dumps(receipt, sort_keys=True))
        raise SystemExit(0 if receipt["status"] == "ok" else 1)

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

    http = getattr(args, "http", False)
    if http:
        raise SystemExit(
            "HTTP transport is disabled: loopback TCP cannot isolate your full "
            "ChatGPT account from other local users or processes. Use --stdio."
        )

    # default: run server
    cfg_path = getattr(args, "config", None)
    cfg = load_config(cfg_path)
    if getattr(args, "port", None):
        cfg["server"]["port"] = args.port
    if getattr(args, "host", None):
        cfg["server"]["host"] = args.host

    # Host is unused by stdio. Force a safe inert value so legacy HTTP-era
    # configs cannot leak a non-loopback bind into programmatic construction.
    cfg["server"]["host"] = "127.0.0.1"

    mcp = build_server(cfg)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
