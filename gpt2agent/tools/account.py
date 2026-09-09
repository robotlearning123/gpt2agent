from __future__ import annotations

from gpt2agent.backend import BackendClient
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._redact import redact


def register(mcp, client: BackendClient) -> None:
    @mcp.tool()
    async def account_status() -> dict:
        """Return ChatGPT account info.

        Returns a dict with: `email` (redacted), `country`, `groups`,
        `subscription` (plan slug, e.g. "plus"/"pro"/None), `has_active_subscription`,
        `expires_at`, and `features_count` (number of entitled features).
        """
        me = await async_get(client, "/backend-api/me", target_path="/backend-api/me") or {}
        check = await async_get(
            client,
            "/backend-api/accounts/check/v4-2023-04-27",
            target_path="/backend-api/accounts/check/v4-2023-04-27",
        ) or {}
        acc_keys = list((check.get("accounts") or {}).keys())
        first = (check.get("accounts") or {}).get(acc_keys[0], {}) if acc_keys else {}
        ent = first.get("entitlement") or {}
        return {
            "email": redact(me.get("email") or ""),
            "country": me.get("country"),
            "groups": me.get("groups"),
            "subscription": ent.get("subscription_plan"),
            "has_active_subscription": ent.get("has_active_subscription"),
            "expires_at": ent.get("expires_at"),
            "features_count": len(first.get("features") or []),
        }

    @mcp.tool()
    async def list_models() -> list:
        """List the models available on your account.

        Returns a list of model dicts; the `slug` field of each (e.g.
        "gpt-5-5-pro", "o3-pro") is exactly what you pass as `model=` to the
        `chat` tool. Other keys: title, description, max_tokens, reasoning_type,
        capabilities, enabled_tools.
        """
        data = await async_get(
            client,
            "/backend-api/models?history_and_training_disabled=false",
            target_path="/backend-api/models",
        ) or {}
        return [
            {
                "slug": m.get("slug"),
                "title": m.get("title"),
                "description": m.get("description"),
                "max_tokens": m.get("max_tokens"),
                "reasoning_type": m.get("reasoning_type"),
                "thinking_efforts": m.get("thinking_efforts"),
                "tags": m.get("tags"),
                "capabilities": m.get("capabilities"),
                "enabled_tools": m.get("enabled_tools"),
                "product_features_keys": m.get("product_features_keys"),
            }
            for m in (data.get("models") or [])
        ]
