from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError, InputValidationError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import (
    bounded_string,
    bounded_string_list,
    validate_cursor,
    validate_int,
)


_LOCAL_PREFIX = "g2a-local-v1:"
_MAX_INSTALLED_ITEMS = 100
_SCALAR_FIELDS = (
    "id",
    "name",
    "marketplace_name",
    "display_name",
    "version",
    "enabled",
    "scope",
    "status",
    "installation_policy",
    "release_version",
)
_LIST_FIELDS = (
    "skill_names",
    "disabled_skill_names",
    "app_ids",
    "app_template_ids",
    "canonical_connector_ids",
    "mcp_server_keys",
    "capability_names",
)


def _normalize_item(raw: Any, *, require_release: bool = False) -> dict:
    adapter = "plugins"
    if not isinstance(raw, dict):
        raise BackendContractError(adapter, "plugin item must be an object")
    release = raw.get("release")
    if require_release and not isinstance(release, dict):
        raise BackendContractError(adapter, "release object is required")
    if release is not None and not isinstance(release, dict):
        raise BackendContractError(adapter, "release must be an object")

    item: dict[str, Any] = {}
    for field in _SCALAR_FIELDS:
        if field == "release_version":
            value = release.get("version") if isinstance(release, dict) else raw.get(field)
        else:
            value = raw.get(field)
        if field == "id":
            item[field] = bounded_string(value, adapter=adapter, field=field, required=True)
        elif field == "enabled":
            if value is not None and not isinstance(value, bool):
                raise BackendContractError(adapter, "enabled must be boolean or null")
            item[field] = value
        else:
            item[field] = bounded_string(
                value,
                adapter=adapter,
                field=field,
                redact_value=field in {"name", "marketplace_name", "display_name"},
            )
    for field in _LIST_FIELDS:
        item[field] = bounded_string_list(
            raw.get(field),
            adapter=adapter,
            field=field,
            redact_values=field in {"skill_names", "disabled_skill_names", "capability_names"},
        )
    return item


def _catalog_fingerprint(items: list[dict]) -> str:
    encoded = json.dumps(
        [item["id"] for item in items], ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_local_cursor(fingerprint: str, offset: int) -> str:
    payload = json.dumps({"f": fingerprint, "o": offset}, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return _LOCAL_PREFIX + token


def _decode_local_cursor(cursor: str) -> tuple[str, int]:
    try:
        token = cursor[len(_LOCAL_PREFIX) :]
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        fingerprint = payload["f"]
        offset = payload["o"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise InputValidationError("cursor is not a valid g2a-local-v1 cursor") from None
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(ch not in "0123456789abcdef" for ch in fingerprint)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise InputValidationError("cursor is not a valid g2a-local-v1 cursor")
    return fingerprint, offset


def normalize_plugin_catalog(data: Any, *, limit: int, cursor: str | None) -> dict:
    if isinstance(data, list):
        items = [_normalize_item(raw) for raw in data]
        fingerprint = _catalog_fingerprint(items)
        offset = 0
        if cursor is not None:
            if not cursor.startswith(_LOCAL_PREFIX):
                raise BackendContractError(
                    "plugins", "backend cursor was ignored by a root-array catalog"
                )
            expected, offset = _decode_local_cursor(cursor)
            if expected != fingerprint:
                raise BackendContractError("plugins", "local cursor fingerprint is stale")
        if offset > len(items):
            raise BackendContractError("plugins", "local cursor offset exceeds catalog")
        page = items[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            _encode_local_cursor(fingerprint, next_offset) if next_offset < len(items) else None
        )
        return {"items": page, "cursor": next_cursor}

    if not isinstance(data, dict):
        raise BackendContractError("plugins", "known catalog envelope required")
    if cursor is not None and cursor.startswith(_LOCAL_PREFIX):
        raise BackendContractError("plugins", "local cursor cannot address a backend page")
    raw_items = data.get("plugins")
    pagination = data.get("pagination")
    if not isinstance(raw_items, list) or not isinstance(pagination, dict):
        raise BackendContractError("plugins", "plugins/pagination envelope required")
    if len(raw_items) > limit:
        raise BackendContractError("plugins", "page exceeds requested limit")
    items = [_normalize_item(raw, require_release=True) for raw in raw_items]
    next_cursor = validate_cursor(
        pagination.get("next_page_token"), adapter="plugins"
    )
    if next_cursor is not None and next_cursor.startswith(_LOCAL_PREFIX):
        raise BackendContractError("plugins", "backend cursor uses reserved local prefix")
    return {"items": items, "cursor": next_cursor}


def _derive_installed(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise BackendContractError("installed_plugins", "plugin item must be an object")
    derived = dict(raw)
    marketplace = raw.get("marketplace")
    if isinstance(marketplace, dict) and derived.get("marketplace_name") is None:
        derived["marketplace_name"] = marketplace.get("name")
    apps = raw.get("apps")
    if apps is not None:
        if not isinstance(apps, list) or len(apps) > 100:
            raise BackendContractError("installed_plugins", "apps must be a bounded list")
        derived["app_ids"] = [entry.get("id") for entry in apps if isinstance(entry, dict)]
    skills = raw.get("skills")
    if skills is not None:
        if not isinstance(skills, list) or len(skills) > 100:
            raise BackendContractError("installed_plugins", "skills must be a bounded list")
        names = [entry.get("name") for entry in skills if isinstance(entry, dict)]
        derived["skill_names"] = names
        derived["disabled_skill_names"] = [
            entry.get("name")
            for entry in skills
            if isinstance(entry, dict) and entry.get("enabled") is False
        ]
    return _normalize_item(derived)


def normalize_installed_plugins(data: Any) -> dict:
    if not isinstance(data, dict) or "plugins" not in data:
        raise BackendContractError("installed_plugins", "plugins envelope required")
    envelope = data["plugins"]
    if isinstance(envelope, list):
        raw_items = envelope
    elif isinstance(envelope, dict):
        raw_items = envelope.get("results")
        page = envelope.get("page", {})
        if not isinstance(raw_items, list) or not isinstance(page, dict):
            raise BackendContractError(
                "installed_plugins", "nested plugins.results/plugins.page envelope required"
            )
        has_more = page.get("has_more")
        if has_more is not None and not isinstance(has_more, bool):
            raise BackendContractError("installed_plugins", "page.has_more must be boolean")
        if has_more is True:
            raise BackendContractError("installed_plugins", "installed page has_more is true")
    else:
        raise BackendContractError("installed_plugins", "plugins must be a list or object")
    if len(raw_items) > _MAX_INSTALLED_ITEMS:
        raise BackendContractError(
            "installed_plugins", "installed result exceeds 100 items"
        )
    return {"items": [_derive_installed(raw) for raw in raw_items]}


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("list_plugins"))
    async def list_plugins(
        scope: str = "USER", limit: int = 50, cursor: str | None = None
    ) -> dict:
        """Return one bounded page of the account Plugin catalog."""
        if scope not in {"USER", "WORKSPACE"}:
            raise InputValidationError("scope must be USER or WORKSPACE")
        limit = validate_int(limit, name="limit", minimum=1, maximum=50)
        cursor = validate_cursor(cursor)
        params: dict[str, Any] = {"scope": scope, "limit": limit}
        if cursor is not None and not cursor.startswith(_LOCAL_PREFIX):
            params["pageToken"] = cursor
        data = await async_get(
            client,
            "/backend-api/plugins/list",
            params=params,
            target_path="/backend-api/plugins/list",
            fixed_probe=False,
        )
        return normalize_plugin_catalog(data, limit=limit, cursor=cursor)

    @mcp.tool(annotations=tool_annotations("list_installed_plugins"))
    async def list_installed_plugins() -> dict:
        """Return installed Plugins using only bounded, non-identifying fields."""
        data = await async_get(
            client,
            "/backend-api/plugins/installed",
            target_path="/backend-api/plugins/installed",
            fixed_probe=True,
        )
        return normalize_installed_plugins(data)
