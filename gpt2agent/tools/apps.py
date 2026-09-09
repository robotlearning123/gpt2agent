from __future__ import annotations

from gpt2agent.backend import BackendClient, UpstreamEndpointError
from gpt2agent.tools._backend import async_get


def _classify(app_id: str) -> str:
    if app_id.startswith("connector_"):
        return "official_connector"
    if app_id.startswith("asdk_app_"):
        return "third_party_sdk"
    return "unknown"


def register(mcp, client: BackendClient) -> None:
    @mcp.tool()
    async def list_apps() -> list:
        """Return ChatGPT connected apps/connectors. Names unresolvable — IDs with type classification returned."""
        try:
            data = await async_get(
                client,
                "/backend-api/apps/list",
                target_path="/backend-api/apps/list",
            ) or {}
        except RuntimeError as exc:
            # 405 = the endpoint no longer accepts this request shape at all.
            # Surface it as a named upstream change instead of a raw HTTP error:
            # retrying or re-logging in cannot fix a moved endpoint.
            if "405" in str(exc):
                raise UpstreamEndpointError(
                    f"list_apps is broken upstream: {exc}.\n"
                    "ChatGPT moved or removed this endpoint, so the installed "
                    "gpt2agent can no longer list connected apps. This is not a "
                    "problem with your token or configuration. Other read-only "
                    "tools still work — run `gpt2agent doctor` for a live "
                    "status table."
                ) from exc
            raise
        return [
            {
                "id": a.get("id"),
                "type": _classify(a.get("id") or ""),
                "enabled": a.get("enabled"),
                # Check key presence, not truthiness: `is_connected: False` (a
                # disconnected app) must report False, not fall through to
                # `connected` or None.
                "connected": (
                    a["is_connected"] if "is_connected" in a else a.get("connected")
                ),
            }
            for a in (data.get("apps") or [])
            if isinstance(a, dict)
        ]
