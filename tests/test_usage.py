"""usage.py — account usage report from conversation/init + local limiter."""

import json

from gpt2agent.usage import build_usage_report, format_usage_report

_INIT = {
    "default_model_slug": "gpt-5-6-thinking",
    "intended_default_model_slug": "gpt-6-pro",
    "model_limits": [
        {"model_slug": "gpt-6-pro", "resets_after": "2026-09-20T01:31:58Z"}
    ],
    "limits_progress": [
        {
            "feature_name": "deep_research",
            "remaining": 0,
            "reset_after": "2026-09-18T21:56:57Z",
        },
        {
            "feature_name": "deep_research_standard",
            "remaining": 7,
            "reset_after": "2026-10-01T00:00:00Z",
        },
        {"feature_name": "image_gen", "remaining": 999},
        "garbage-entry",
        {"no_name": True},
    ],
    "blocked_features": [
        {"name": "reason", "resets_after": "2026-09-20T00:00:00Z"},
        "other_feature",
        None,
    ],
}


class _Client:
    def __init__(self, init):
        self._init = init
        self.posts = []

    def post(self, path, json=None, **kw):
        self.posts.append(path)
        return self._init


def test_report_models_and_downgrade_flag() -> None:
    rep = build_usage_report(_Client(_INIT))
    models = rep["models"]
    assert models["default_model_slug"] == "gpt-5-6-thinking"
    assert models["intended_default_model_slug"] == "gpt-6-pro"
    assert models["downgraded"] is True
    assert models["capped"] == [
        {"model_slug": "gpt-6-pro", "resets_after": "2026-09-20T01:31:58Z"}
    ]


def test_report_features_skips_malformed() -> None:
    rep = build_usage_report(_Client(_INIT))
    names = [f["feature_name"] for f in rep["features"]]
    assert names == ["deep_research", "deep_research_standard", "image_gen"]
    assert rep["features"][0]["remaining"] == 0
    assert rep["features"][0]["reset_after"] == "2026-09-18T21:56:57Z"


def test_report_splits_light_and_heavy_dr() -> None:
    rep = build_usage_report(_Client(_INIT))
    dr = rep["deep_research"]
    assert dr["light_remaining"] == 0
    assert dr["light_resets_after"] == "2026-09-18T21:56:57Z"
    assert dr["heavy_remaining"] == 7
    assert dr["heavy_counter"] == "deep_research_standard"
    assert dr["heavy_blocked"] is False


def test_report_heavy_dr_absent_when_not_exposed() -> None:
    init = {
        "limits_progress": [
            {"feature_name": "deep_research", "remaining": 0}
        ]
    }
    rep = build_usage_report(_Client(init))
    dr = rep["deep_research"]
    assert dr["light_remaining"] == 0
    assert dr["heavy_remaining"] is None
    assert dr["heavy_counter"] is None
    assert dr["heavy_blocked"] is False


def test_report_blocked_features_string_and_dict() -> None:
    rep = build_usage_report(_Client(_INIT))
    names = [b["name"] for b in rep["blocked_features"]]
    assert names == ["reason", "other_feature"]
    assert rep["blocked_features"][0]["resets_after"] == "2026-09-20T00:00:00Z"


def test_report_empty_init() -> None:
    rep = build_usage_report(_Client(None))
    assert rep["models"]["capped"] == []
    assert rep["features"] == []
    assert rep["deep_research"]["light_remaining"] is None


def test_format_renders_key_lines() -> None:
    rep = build_usage_report(_Client(_INIT))
    out = format_usage_report(rep)
    assert "gpt-5-6-thinking" in out
    assert "DOWNGRADED" in out
    assert "deep_research" in out and "remaining=0" in out
    assert "heavy (connector)" in out and "remaining=7" in out
    assert "reason" in out


def test_report_is_json_serializable() -> None:
    rep = build_usage_report(_Client(_INIT))
    json.dumps(rep)


_ACCT = {
    "accounts": {
        "default": {
            "account": {"plan_type": "pro", "plan_display_name": "Pro"},
            "features": ["canvas", "dalle_3", "caterpillar"],
            "entitlement": {
                "subscription_plan": "chatgptpro",
                "has_active_subscription": True,
                "expires_at": "2026-09-21T08:15:49+00:00",
                "renews_at": "2026-09-21T02:15:49+00:00",
                "is_delinquent": False,
                "scheduled_plan_change": {
                    "changes_at": "2026-09-21T02:15:49+00:00",
                    "plan_type": "plus",
                },
            },
        }
    }
}


class _ClientWithAccounts(_Client):
    def get(self, path, **kw):
        if "accounts/check" in path:
            return _ACCT
        raise RuntimeError("unexpected")


def test_report_subscription_and_scheduled_downgrade() -> None:
    rep = build_usage_report(_ClientWithAccounts(_INIT))
    sub = rep["subscription"]
    assert sub["plan"] == "chatgptpro"
    assert sub["has_active_subscription"] is True
    assert sub["expires_at"] == "2026-09-21T08:15:49+00:00"
    assert sub["scheduled_plan_change"] == {
        "plan_type": "plus",
        "changes_at": "2026-09-21T02:15:49+00:00",
    }
    assert sub["features"] == ["canvas", "dalle_3", "caterpillar"]


def test_report_subscription_failsoft_without_get() -> None:
    rep = build_usage_report(_Client(_INIT))
    assert rep["subscription"] == {}


def test_report_banner_captured() -> None:
    init = dict(_INIT)
    init["banner_info"] = {
        "name": "account_sharing_degrade",
        "title": "Suspicious activity detected",
        "resets_after": "2026-09-19T21:45:00Z",
    }
    rep = build_usage_report(_ClientWithAccounts(init))
    assert rep["banner"]["name"] == "account_sharing_degrade"
    out = format_usage_report(rep)
    assert "Suspicious activity detected" in out
    assert "2026-09-19T21:45:00" in out


def test_format_renders_subscription_section() -> None:
    rep = build_usage_report(_ClientWithAccounts(_INIT))
    out = format_usage_report(rep)
    assert "chatgptpro" in out
    assert "scheduled downgrade to plus" in out
    assert "2026-09-21T02:15:49" in out
    assert "features enabled: 3" in out
