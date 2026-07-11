from __future__ import annotations

import asyncio

from dataclasses import dataclass

import pytest

from gpt2agent.capabilities import CAPABILITY_IDS, build_account_capabilities
from gpt2agent.errors import BackendHTTPError
from gpt2agent.tool_manifest import TOOL_NAMES
from gpt2agent.tools import capabilities
from tests.test_tools import FakeMCP


def _run(awaitable):
    return asyncio.run(awaitable)


@dataclass(frozen=True)
class Snapshot:
    generation: int


class CapabilityClient:
    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, object, object]] = []
        self.timeouts: list[float | None] = []
        self.snapshot_calls = 0

    def operation_snapshot(self) -> Snapshot:
        self.snapshot_calls += 1
        return Snapshot(self.snapshot_calls)

    def get(self, path: str, *, auth_headers=None, params=None, **kwargs):
        self.calls.append((path, auth_headers, params))
        self.timeouts.append(kwargs.get("timeout_seconds"))
        value = self.routes.get(path, {})
        if isinstance(value, BaseException):
            raise value
        return value


def _valid_routes() -> dict[str, object]:
    return {
        "/backend-api/models": {
            "models": [
                {
                    "slug": "research",
                    "enabled_tools": ["canvas", "image_gen_tool_enabled"],
                }
            ]
        },
        "/backend-api/tpp/models/": {"models": [{"slug": "work"}]},
        "/backend-api/apps/list": {"apps": ["connector_x"]},
        "/backend-api/plugins/list": [{"id": "p1"}],
        "/backend-api/plugins/installed": {"plugins": []},
        "/backend-api/tasks": {"tasks": []},
        "/backend-api/automations": {"items": [], "cursor": None},
        "/backend-api/websites/access": {
            "enabled": True,
            "custom_domains_enabled": False,
            "requires_workspace_slug": False,
        },
        "/backend-api/websites": {"items": [], "cursor": None},
        "/backend-api/conversations": {"items": []},
        "/backend-api/gizmos/snorlax/sidebar": {"items": []},
        "/backend-api/memories": {"memories": []},
        "/backend-api/user_system_messages": {"enabled": True},
        "/backend-api/codex/environments": {"environments": []},
        "/backend-api/projects": BackendHTTPError(
            "GET", "projects", 404, code="unsupported", retryable=False
        ),
    }


def test_capabilities_are_serial_single_snapshot_and_voice_is_never_called() -> None:
    client = CapabilityClient(_valid_routes())
    result = _run(build_account_capabilities(client))

    assert result["schema_version"] == "1"
    assert [record["id"] for record in result["capabilities"]] == list(CAPABILITY_IDS)
    assert client.snapshot_calls == 1
    assert all(snapshot == Snapshot(1) for _, snapshot, _ in client.calls)
    paths = [path for path, _, _ in client.calls]
    assert not any(
        token in path.lower()
        for path in paths
        for token in ("voice", "realtime", "webrtc", "transcript", "/call", "/session")
    )


def test_capabilities_do_not_probe_private_content_routes() -> None:
    client = CapabilityClient(_valid_routes())
    result = _run(build_account_capabilities(client))
    paths = {path for path, _, _ in client.calls}

    assert paths.isdisjoint(
        {
            "/backend-api/conversations",
            "/backend-api/memories",
            "/backend-api/user_system_messages",
        }
    )
    records = {record["id"]: record for record in result["capabilities"]}
    for capability_id in ("conversations", "memory", "custom_instructions"):
        assert records[capability_id]["entitled"] is None
        assert records[capability_id]["reachable_now"] is None
        assert records[capability_id]["reachability_scope"] == "none"
        assert records[capability_id]["status"] == "unverified"
        assert records[capability_id]["evidence_source"] == ["packaged_contract"]


def test_model_predicates_are_exact_and_absence_never_means_false() -> None:
    result = _run(build_account_capabilities(CapabilityClient(_valid_routes())))
    records = {record["id"]: record for record in result["capabilities"]}

    assert records["deep_research"]["entitled"] is True
    assert records["canvas"]["entitled"] is True
    assert records["image_generation"]["entitled"] is True
    assert records["agent_mode"]["entitled"] is None
    assert records["code_interpreter"]["entitled"] is None
    assert records["deep_research"]["reachable_now"] is None
    assert records["deep_research"]["status"] == "unverified"


def test_model_without_optional_enabled_tools_keeps_catalog_reachable() -> None:
    routes = _valid_routes()
    routes["/backend-api/models"] = {"models": [{"slug": "plain"}]}

    result = _run(build_account_capabilities(CapabilityClient(routes)))
    records = {record["id"]: record for record in result["capabilities"]}

    assert records["chat_models"]["status"] == "ok"
    assert records["chat_models"]["reachable_now"] is True
    for capability_id in (
        "agent_mode",
        "code_interpreter",
        "canvas",
        "image_generation",
        "deep_research",
    ):
        assert records[capability_id]["status"] == "unverified"
        assert records[capability_id]["entitled"] is None


def test_installed_empty_inherits_successful_plugin_entitlement() -> None:
    result = _run(build_account_capabilities(CapabilityClient(_valid_routes())))
    installed = next(r for r in result["capabilities"] if r["id"] == "installed_plugins")
    assert installed["entitled"] is True
    assert installed["status"] == "ok"


def test_installed_failure_never_inherits_plugin_entitlement() -> None:
    routes = _valid_routes()
    routes["/backend-api/plugins/installed"] = BackendHTTPError(
        "GET", "installed_plugins", 503, code="temporarily_failed", retryable=True
    )
    result = _run(build_account_capabilities(CapabilityClient(routes)))
    installed = next(r for r in result["capabilities"] if r["id"] == "installed_plugins")
    assert installed["entitled"] is None
    assert installed["reachable_now"] is None
    assert installed["status"] == "temporarily_failed"


def test_sites_disabled_skips_list_and_reports_explicit_false() -> None:
    routes = _valid_routes()
    routes["/backend-api/websites/access"] = {
        "enabled": False,
        "custom_domains_enabled": False,
        "requires_workspace_slug": False,
    }
    client = CapabilityClient(routes)
    result = _run(build_account_capabilities(client))
    sites = next(r for r in result["capabilities"] if r["id"] == "sites")
    assert sites["reachable_now"] is True
    assert sites["entitled"] is False
    assert sites["status"] == "unavailable"
    assert "/backend-api/websites" not in [path for path, _, _ in client.calls]


@pytest.mark.parametrize("status_code", [404, 405])
@pytest.mark.parametrize(("enabled", "expected_entitled"), [(True, True), (None, None)])
def test_sites_list_failure_keeps_reachability_unknown_and_only_explicit_true(
    status_code: int,
    enabled: bool | None,
    expected_entitled: bool | None,
) -> None:
    routes = _valid_routes()
    routes["/backend-api/websites/access"] = {
        "enabled": enabled,
        "custom_domains_enabled": False,
        "requires_workspace_slug": False,
    }
    routes["/backend-api/websites"] = BackendHTTPError(
        "GET",
        "/backend-api/websites",
        status_code,
        code="contract_changed",
    )

    result = _run(build_account_capabilities(CapabilityClient(routes)))
    sites = next(item for item in result["capabilities"] if item["id"] == "sites")

    assert sites["status"] == "contract_changed"
    assert sites["reachable_now"] is None
    assert sites["entitled"] is expected_entitled


def test_budget_exhaustion_marks_unstarted_probes_without_calling_them() -> None:
    ticks = iter([0.0, 0.0, 91.0] + [91.0] * 100)
    client = CapabilityClient(_valid_routes())
    result = _run(build_account_capabilities(client, monotonic=lambda: next(ticks)))
    exhausted = [r for r in result["capabilities"] if r["reason"] == "probe budget exhausted"]
    assert exhausted
    assert all(r["status"] == "temporarily_failed" for r in exhausted)
    assert all(r["reachable_now"] is None and r["entitled"] is None for r in exhausted)


def test_each_probe_timeout_is_clamped_to_the_remaining_global_budget() -> None:
    ticks = iter([0.0] + [89.5] * 100)
    client = CapabilityClient(_valid_routes())

    _run(build_account_capabilities(client, monotonic=lambda: next(ticks)))

    assert client.timeouts
    assert all(timeout == pytest.approx(0.5) for timeout in client.timeouts)


def test_probe_that_finishes_after_deadline_is_discarded_as_budget_exhausted() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()

    class LateClient(CapabilityClient):
        def get(self, path: str, **kwargs):
            value = super().get(path, **kwargs)
            clock.value = 91.0
            return value

    client = LateClient(_valid_routes())
    result = _run(build_account_capabilities(client, monotonic=clock))
    records = {record["id"]: record for record in result["capabilities"]}

    assert client.calls == [client.calls[0]]
    assert records["chat_models"]["reason"] == "probe budget exhausted"
    assert records["chat_models"]["reachable_now"] is None


def test_tool_wrapper_uses_packaged_manifest_and_structured_envelope() -> None:
    client = CapabilityClient(_valid_routes())
    mcp = FakeMCP()
    capabilities.register(mcp, client)
    result = _run(mcp.tools["account_capabilities"]())
    exposed = {record["id"]: record["exposed_by_mcp"] for record in result["capabilities"]}
    assert exposed["voice_catalog"] is False
    assert "account_capabilities" in TOOL_NAMES


def test_projects_is_candidate_unsupported_and_not_applicable() -> None:
    result = _run(build_account_capabilities(CapabilityClient(_valid_routes())))
    projects = next(r for r in result["capabilities"] if r["id"] == "projects")
    assert projects["status"] == "unsupported"
    assert projects["reachable_now"] is None
    assert projects["item_contract_status"] == "not_applicable"


def test_established_404_proves_route_unreachable_but_not_unentitled() -> None:
    routes = _valid_routes()
    routes["/backend-api/apps/list"] = BackendHTTPError(
        "GET", "/backend-api/apps/list", 404, code="contract_changed"
    )

    result = _run(build_account_capabilities(CapabilityClient(routes)))
    apps_record = next(item for item in result["capabilities"] if item["id"] == "apps")

    assert apps_record["status"] == "contract_changed"
    assert apps_record["reachable_now"] is False
    assert apps_record["entitled"] is None


def test_voice_inventory_records_are_non_collections() -> None:
    result = _run(build_account_capabilities(CapabilityClient(_valid_routes())))
    records = {item["id"]: item for item in result["capabilities"]}

    for capability_id in ("voice_catalog", "voice_transcript", "gpt_live"):
        assert records[capability_id]["item_contract_status"] == "not_applicable"


def test_capability_cancellation_is_not_converted_to_partial_evidence() -> None:
    routes = _valid_routes()
    routes["/backend-api/tpp/models/"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        _run(build_account_capabilities(CapabilityClient(routes)))


@pytest.mark.parametrize(
    ("route", "payload", "capability_id"),
    [
        ("/backend-api/tasks", {"tasks": [None]}, "background_jobs"),
        (
            "/backend-api/automations",
            {"items": [{"id": None}], "cursor": None},
            "scheduled_automations",
        ),
        (
            "/backend-api/gizmos/snorlax/sidebar",
            {"items": [None]},
            "custom_gpts",
        ),
        (
            "/backend-api/codex/environments",
            {"environments": [None]},
            "codex",
        ),
    ],
)
def test_populated_capability_requires_the_runtime_item_normalizer(
    route: str, payload: object, capability_id: str
) -> None:
    routes = _valid_routes()
    routes[route] = payload

    result = _run(build_account_capabilities(CapabilityClient(routes)))
    record = next(item for item in result["capabilities"] if item["id"] == capability_id)

    assert record["status"] == "contract_changed"
    assert record["reachable_now"] is None
    assert record["entitled"] is None
    assert record["item_contract_status"] != "live_verified"


def test_apps_capability_counts_only_entries_accepted_by_list_apps() -> None:
    routes = _valid_routes()
    routes["/backend-api/apps/list"] = {"apps": [None, 17, {"enabled": True}]}

    result = _run(build_account_capabilities(CapabilityClient(routes)))
    apps_record = next(item for item in result["capabilities"] if item["id"] == "apps")

    assert apps_record["status"] == "ok"
    assert apps_record["reachable_now"] is True
    assert apps_record["entitled"] is None
    assert apps_record["item_contract_status"] == "public_bundle_only"
