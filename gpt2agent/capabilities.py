"""Deterministic, shape-only account capability inventory for v0.0.12."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from gpt2agent.errors import BackendContractError, BackendHTTPError
from gpt2agent.model_catalog import normalize_general_models, normalize_work_models
from gpt2agent.tool_manifest import exposes
from gpt2agent.tools.apps import normalize_apps
from gpt2agent.tools.automations import normalize_scheduled_page
from gpt2agent.tools.codex import normalize_codex_environments
from gpt2agent.tools.conversations import normalize_background_tasks
from gpt2agent.tools.gpts import normalize_custom_gpts
from gpt2agent.tools.plugins import normalize_installed_plugins, normalize_plugin_catalog
from gpt2agent.tools.sites import normalize_sites_access, normalize_sites_page


CAPABILITY_IDS = (
    "chat_models",
    "agent_mode",
    "code_interpreter",
    "canvas",
    "image_generation",
    "deep_research",
    "work_models",
    "apps",
    "plugins",
    "installed_plugins",
    "background_jobs",
    "scheduled_automations",
    "sites",
    "voice_catalog",
    "voice_transcript",
    "gpt_live",
    "conversations",
    "custom_gpts",
    "memory",
    "custom_instructions",
    "codex",
    "projects",
)

_META = {
    "chat_models": ("chat", "catalog", "list_models", True),
    "agent_mode": ("chat", "none", "agent", False),
    "code_interpreter": ("chat", "none", "code_interpreter", False),
    "canvas": ("chat", "none", "canvas_execute", False),
    "image_generation": ("chat", "none", "generate_image", False),
    "deep_research": ("chat", "none", "deep_research", False),
    "work_models": ("work", "catalog", "list_work_models", True),
    "apps": ("account", "catalog", "list_apps", True),
    "plugins": ("account", "catalog", "list_plugins", True),
    "installed_plugins": ("account", "catalog", "list_installed_plugins", True),
    "background_jobs": ("account", "route", "list_tasks", True),
    "scheduled_automations": ("account", "route", "list_scheduled_tasks", True),
    "sites": ("account", "route", "list_sites", True),
    "voice_catalog": ("voice", "none", None, False),
    "voice_transcript": ("voice", "none", None, False),
    "gpt_live": ("voice", "none", None, False),
    "conversations": ("account", "none", "list_conversations", True),
    "custom_gpts": ("account", "route", "list_custom_gpts", True),
    "memory": ("account", "none", "memory_list", True),
    "custom_instructions": ("account", "none", "custom_instructions_get", False),
    "codex": ("codex", "route", "list_codex_envs", True),
    "projects": ("account", "route", None, False),
}

_PUBLIC_BUNDLE_COLLECTIONS = {
    "chat_models",
    "work_models",
    "apps",
    "plugins",
    "installed_plugins",
    "background_jobs",
    "scheduled_automations",
    "sites",
    "conversations",
    "custom_gpts",
    "memory",
    "codex",
}

_MIN_PROBE_TIMEOUT_SECONDS = 0.1
_MAX_PROBE_TIMEOUT_SECONDS = 20.0


class _ProbeBudgetExceeded(Exception):
    """Internal control flow for an exhausted account-inventory deadline."""


def _record(
    capability_id: str,
    observed_at: str,
    *,
    entitled: bool | None,
    reachable_now: bool | None,
    status: str,
    reason: str,
    populated: bool = False,
) -> dict:
    surface, reachability_scope, tool_name, is_collection = _META[capability_id]
    if not is_collection:
        item_status = "not_applicable"
    elif populated:
        item_status = "live_verified"
    elif capability_id in _PUBLIC_BUNDLE_COLLECTIONS:
        item_status = "public_bundle_only"
    else:
        item_status = "unverified_live"
    evidence = ["packaged_contract"]
    if status not in {"unverified", "temporarily_failed"}:
        evidence.append("live_account")
    return {
        "id": capability_id,
        "surface": surface,
        "entitled": entitled,
        "reachable_now": reachable_now,
        "reachability_scope": reachability_scope,
        "exposed_by_mcp": bool(tool_name and exposes(tool_name)),
        "officially_supported": False,
        "evidence_source": evidence,
        "observed_at": observed_at,
        "status": status,
        "reason": reason[:256],
        "item_contract_status": item_status,
    }


def _failed_record(capability_id: str, observed_at: str, exc: BaseException) -> dict:
    reachable_now: bool | None = None
    if isinstance(exc, BackendHTTPError):
        status = exc.code
        if exc.status_code in {404, 405} and status == "contract_changed":
            reachable_now = False
    elif isinstance(exc, BackendContractError):
        status = "contract_changed"
    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        status = "temporarily_failed"
    else:
        status = "temporarily_failed"
    return _record(
        capability_id,
        observed_at,
        entitled=None,
        reachable_now=reachable_now,
        status=status,
        reason=f"{capability_id} probe {status.replace('_', ' ')}",
    )


def _budget_record(capability_id: str, observed_at: str) -> dict:
    return _record(
        capability_id,
        observed_at,
        entitled=None,
        reachable_now=None,
        status="temporarily_failed",
        reason="probe budget exhausted",
    )


async def build_account_capabilities(
    client,
    *,
    monotonic=time.monotonic,
    budget_seconds: float = 90.0,
) -> dict:
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    start = monotonic()
    records: dict[str, dict] = {}
    if hasattr(client, "request_headers"):
        auth_headers = client.request_headers()
    elif hasattr(client, "operation_snapshot"):
        auth_headers = client.operation_snapshot()
    else:
        auth_headers = None

    def remaining_budget() -> float:
        return budget_seconds - (monotonic() - start)

    def within_budget() -> bool:
        return remaining_budget() >= _MIN_PROBE_TIMEOUT_SECONDS

    async def get(path: str, *, params=None, established: bool = True) -> Any:
        remaining = remaining_budget()
        if remaining < _MIN_PROBE_TIMEOUT_SECONDS:
            raise _ProbeBudgetExceeded
        kwargs = {
            "target_path": path,
            "fixed_probe": True,
            "established": established,
            "timeout_seconds": min(_MAX_PROBE_TIMEOUT_SECONDS, remaining),
        }
        if params is not None:
            kwargs["params"] = params
        if auth_headers is not None:
            kwargs["auth_headers"] = auth_headers
        data = await asyncio.to_thread(client.get, path, **kwargs)
        if remaining_budget() < 0:
            raise _ProbeBudgetExceeded
        return data

    def probe_failure(capability_id: str, exc: BaseException) -> dict:
        if isinstance(exc, _ProbeBudgetExceeded):
            return _budget_record(capability_id, observed_at)
        return _failed_record(capability_id, observed_at, exc)

    model_ids = (
        "chat_models",
        "agent_mode",
        "code_interpreter",
        "canvas",
        "image_generation",
        "deep_research",
    )
    if not within_budget():
        records.update({item: _budget_record(item, observed_at) for item in model_ids})
    else:
        try:
            data = await get(
                "/backend-api/models",
                params={"history_and_training_disabled": "false"},
            )
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise BackendContractError("capabilities.models", "models list required")
            models = normalize_general_models(data["models"])
            records["chat_models"] = _record(
                "chat_models",
                observed_at,
                entitled=True if models else None,
                reachable_now=True,
                status="ok",
                reason="general model catalog satisfied its minimum schema",
                populated=bool(models),
            )
            predicates = {
                "agent_mode": lambda slug, tools: slug == "agent-mode" or "agent_mode" in tools,
                "code_interpreter": lambda slug, tools: "code_interpreter" in tools,
                "canvas": lambda slug, tools: "canvas" in tools,
                "image_generation": lambda slug, tools: bool(
                    {"image_gen_tool_enabled", "dalle_3"} & tools
                ),
                "deep_research": lambda slug, tools: slug == "research" or "deep_research" in tools,
            }
            for capability_id, predicate in predicates.items():
                matched = False
                for model in models:
                    tools = {
                        value
                        for value in (model.get("enabled_tools") or [])
                        if isinstance(value, str)
                    }
                    if predicate(model.get("slug"), tools):
                        matched = True
                        break
                records[capability_id] = _record(
                    capability_id,
                    observed_at,
                    entitled=True if matched else None,
                    reachable_now=None,
                    status="unverified",
                    reason="exact general-catalog marker matched"
                    if matched
                    else "no exact general-catalog marker matched",
                )
        except Exception as exc:
            for capability_id in model_ids:
                records[capability_id] = probe_failure(capability_id, exc)
                if capability_id != "chat_models":
                    records[capability_id]["reachable_now"] = None

    async def simple_collection(
        capability_id: str,
        path: str,
        *,
        params: dict | None,
        extract,
        entitlement="nonempty",
        established: bool = True,
    ) -> list | None:
        if not within_budget():
            records[capability_id] = _budget_record(capability_id, observed_at)
            return None
        try:
            data = await get(path, params=params, established=established)
            items = extract(data)
            entitled = True if entitlement == "always" or items else None
            records[capability_id] = _record(
                capability_id,
                observed_at,
                entitled=entitled,
                reachable_now=True,
                status="ok",
                reason=f"{capability_id} minimum schema satisfied",
                populated=bool(items),
            )
            return items
        except Exception as exc:
            records[capability_id] = probe_failure(capability_id, exc)
            return None

    def object_list(data, field: str, adapter: str) -> list:
        if not isinstance(data, dict) or not isinstance(data.get(field), list):
            raise BackendContractError(adapter, f"{field} list required")
        return data[field]

    await simple_collection(
        "work_models",
        "/backend-api/tpp/models/",
        params=None,
        extract=lambda data: normalize_work_models(object_list(data, "models", "work_models")),
    )
    await simple_collection(
        "apps",
        "/backend-api/apps/list",
        params=None,
        extract=normalize_apps,
    )
    plugin_items = await simple_collection(
        "plugins",
        "/backend-api/plugins/list",
        params={"scope": "USER", "limit": 1},
        extract=lambda data: normalize_plugin_catalog(data, limit=1, cursor=None)["items"],
    )
    plugin_entitlement = records["plugins"]["entitled"] if plugin_items is not None else None

    if not within_budget():
        records["installed_plugins"] = _budget_record("installed_plugins", observed_at)
    else:
        try:
            installed_data = await get("/backend-api/plugins/installed")
            installed = normalize_installed_plugins(installed_data)["items"]
            records["installed_plugins"] = _record(
                "installed_plugins",
                observed_at,
                entitled=True if installed else plugin_entitlement,
                reachable_now=True,
                status="ok",
                reason="installed Plugin minimum schema satisfied",
                populated=bool(installed),
            )
        except Exception as exc:
            records["installed_plugins"] = probe_failure("installed_plugins", exc)

    await simple_collection(
        "background_jobs",
        "/backend-api/tasks",
        params={"limit": 1},
        extract=lambda data: normalize_background_tasks(data, limit=1),
    )

    scheduled = await simple_collection(
        "scheduled_automations",
        "/backend-api/automations",
        params={"filter": "scheduled"},
        extract=lambda data: normalize_scheduled_page(data)["items"],
    )
    if scheduled is not None:
        records["scheduled_automations"]["entitled"] = None

    if not within_budget():
        records["sites"] = _budget_record("sites", observed_at)
    else:
        try:
            access = normalize_sites_access(await get("/backend-api/websites/access"))
            enabled = access["enabled"]
            if enabled is False:
                records["sites"] = _record(
                    "sites",
                    observed_at,
                    entitled=False,
                    reachable_now=True,
                    status="unavailable",
                    reason="Sites access is explicitly disabled",
                )
            elif not within_budget():
                records["sites"] = _budget_record("sites", observed_at)
                if enabled is True:
                    records["sites"]["entitled"] = True
            else:
                try:
                    sites_page = normalize_sites_page(
                        await get("/backend-api/websites", params={"limit": 1})
                    )
                    records["sites"] = _record(
                        "sites",
                        observed_at,
                        entitled=True if enabled is True else None,
                        reachable_now=True,
                        status="ok",
                        reason="Sites access and list minimum schemas satisfied",
                        populated=bool(sites_page["items"]),
                    )
                except Exception as exc:
                    records["sites"] = probe_failure("sites", exc)
                    records["sites"]["reachable_now"] = None
                    records["sites"]["entitled"] = True if enabled is True else None
        except Exception as exc:
            records["sites"] = probe_failure("sites", exc)

    for capability_id in ("voice_catalog", "voice_transcript", "gpt_live"):
        records[capability_id] = _record(
            capability_id,
            observed_at,
            entitled=None,
            reachable_now=None,
            status="unverified",
            reason="no permitted Voice probe in 0.0.12",
        )

    for capability_id in ("conversations", "memory", "custom_instructions"):
        records[capability_id] = _record(
            capability_id,
            observed_at,
            entitled=None,
            reachable_now=None,
            status="unverified",
            reason="automatic probe omitted to avoid reading private account content",
        )

    await simple_collection(
        "custom_gpts",
        "/backend-api/gizmos/snorlax/sidebar",
        params=None,
        extract=normalize_custom_gpts,
    )

    await simple_collection(
        "codex",
        "/backend-api/codex/environments",
        params=None,
        extract=normalize_codex_environments,
    )

    if not within_budget():
        records["projects"] = _budget_record("projects", observed_at)
    else:
        try:
            await get("/backend-api/projects", established=False)
            records["projects"] = _record(
                "projects",
                observed_at,
                entitled=None,
                reachable_now=True,
                status="ok",
                reason="candidate Projects route returned a response without entitlement inference",
            )
        except Exception as exc:
            records["projects"] = probe_failure("projects", exc)

    return {
        "schema_version": "1",
        "observed_at": observed_at,
        "capabilities": [records[item] for item in CAPABILITY_IDS],
    }
