from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.model_catalog import ModelCatalog
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import bounded_string, nullable_bool


def _bounded_groups(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 100:
        raise BackendContractError(
            "account_status", "groups must be a bounded string list or null"
        )
    groups: list[str] = []
    for entry in value:
        normalized = bounded_string(
            entry,
            adapter="account_status",
            field="groups entry",
            required=True,
            redact_value=True,
        )
        assert normalized is not None
        groups.append(normalized)
    return groups


def normalize_account_status(me: Any, check: Any) -> dict:
    """Project the two private responses onto the documented safe fields."""
    adapter = "account_status"
    if not isinstance(me, dict):
        raise BackendContractError(adapter, "account profile object required")
    if not isinstance(check, dict) or not isinstance(check.get("accounts"), dict):
        raise BackendContractError(adapter, "accounts object envelope required")

    accounts = check["accounts"]
    first = next(iter(accounts.values()), None)
    if first is None:
        entitlement: dict = {}
        features: list = []
    else:
        if not isinstance(first, dict):
            raise BackendContractError(adapter, "account entry object required")
        raw_entitlement = first.get("entitlement")
        if raw_entitlement is not None and not isinstance(raw_entitlement, dict):
            raise BackendContractError(adapter, "entitlement object or null required")
        entitlement = raw_entitlement or {}
        raw_features = first.get("features")
        if raw_features is not None and not isinstance(raw_features, list):
            raise BackendContractError(adapter, "features list or null required")
        features = raw_features or []

    email = bounded_string(
        me.get("email"),
        adapter=adapter,
        field="email",
        redact_value=True,
        maximum=512,
    )
    return {
        "email": email or "",
        "country": bounded_string(
            me.get("country"),
            adapter=adapter,
            field="country",
            redact_value=True,
        ),
        "groups": _bounded_groups(me.get("groups")),
        "subscription": bounded_string(
            entitlement.get("subscription_plan"),
            adapter=adapter,
            field="subscription_plan",
            redact_value=True,
        ),
        "has_active_subscription": nullable_bool(
            entitlement.get("has_active_subscription"),
            adapter=adapter,
            field="has_active_subscription",
        ),
        "expires_at": bounded_string(
            entitlement.get("expires_at"),
            adapter=adapter,
            field="expires_at",
            redact_value=True,
            maximum=2_048,
        ),
        "features_count": len(features),
    }


def register(
    mcp,
    client: BackendClient,
    *,
    model_catalog: ModelCatalog | None = None,
) -> None:
    catalog = model_catalog or ModelCatalog(client)

    @mcp.tool(annotations=tool_annotations("account_status"))
    async def account_status() -> dict:
        """Return ChatGPT account info.

        Returns a dict with: `email` (redacted), `country`, `groups`,
        `subscription` (plan slug, e.g. "plus"/"pro"/None), `has_active_subscription`,
        `expires_at`, and `features_count` (number of entitled features).
        """
        auth_headers = client.request_headers()
        me = await async_get(
            client,
            "/backend-api/me",
            target_path="/backend-api/me",
            auth_headers=auth_headers,
        )
        check = await async_get(
            client,
            "/backend-api/accounts/check/v4-2023-04-27",
            target_path="/backend-api/accounts/check/v4-2023-04-27",
            auth_headers=auth_headers,
        )
        return normalize_account_status(me, check)

    @mcp.tool(annotations=tool_annotations("list_models"))
    async def list_models() -> list:
        """List the models available on your account.

        Returns a list of model dicts; the `slug` field of each (e.g.
        "gpt-5-5-pro", "o3-pro") is exactly what you pass as `model=` to the
        `chat` tool. Other keys: title, description, max_tokens, reasoning_type,
        capabilities, enabled_tools.
        """
        models = await catalog.general()
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
            for m in models
        ]
