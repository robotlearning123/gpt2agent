"""Account usage report — how much is used, what is left, when it resets.

Aggregates the two sources of truth:

* ``POST /backend-api/conversation/init`` — the same bookkeeping call the web
  app issues on page load. Carries ``model_limits`` (per-model caps),
  ``limits_progress`` (per-feature remaining counters), ``blocked_features``,
  ``banner_info`` (account-safety flags) and the default/intended model slugs.
* ``GET /backend-api/accounts/check/v4-2023-04-27`` — subscription
  entitlement (plan, renews/expires, scheduled plan change such as a
  pending downgrade to Plus) and enabled account feature flags. Fail-soft:
  if the call fails the report still renders without the section.
* the shared rate-limiter state file — how much of the local client-side
  budget the fleet has committed plus active upstream cooldowns.

The ``deep_research`` counter under ``limits_progress`` gates the LIGHT DR
path only; ``deep_research_heavy`` draws on an independent monthly cap that
the backend reports under a ``deep_research_*`` variant name when it exposes
one at all (verified live 2026-09-19: heavy dispatch succeeded with the
generic counter at 0). The report surfaces both separately.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

_INIT_PATH = "/backend-api/conversation/init"
_INIT_PAYLOAD = {"conversation_mode_kind": "primary_assistant"}
_ACCOUNTS_PATH = "/backend-api/accounts/check/v4-2023-04-27"
_ME_PATH = "/backend-api/me"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def build_usage_report(client) -> dict[str, Any]:
    """Fetch ``conversation/init`` and shape it into a usage report.

    ``client`` is a ``BackendClient`` (or a test double exposing ``post``).
    Local limiter state is appended when available. Raises on transport
    failure — callers decide whether quota being unknown is fatal.
    """
    init = client.post(_INIT_PATH, json=_INIT_PAYLOAD) or {}

    features: list[dict[str, Any]] = []
    for lim in init.get("limits_progress") or []:
        if not isinstance(lim, dict) or not lim.get("feature_name"):
            continue
        entry = dict(lim)  # passthrough — upstream may add fields
        rem = entry.get("remaining")
        if not isinstance(rem, (int, float)) or isinstance(rem, bool):
            entry["remaining"] = None
        entry.setdefault("reset_after", entry.get("resets_after"))
        features.append(entry)

    blocked: list[dict[str, Any]] = []
    for feat in init.get("blocked_features") or []:
        if isinstance(feat, dict) and feat.get("name"):
            blocked.append(
                {"name": feat["name"], "resets_after": feat.get("resets_after")}
            )
        elif isinstance(feat, str):
            blocked.append({"name": feat, "resets_after": None})

    capped_models = [
        {"model_slug": m["model_slug"], "resets_after": m.get("resets_after")}
        for m in init.get("model_limits") or []
        if isinstance(m, dict) and m.get("model_slug")
    ]

    default = init.get("default_model_slug")
    intended = init.get("intended_default_model_slug")

    # init.banner_info — account-safety banners (e.g. account_sharing_degrade)
    banner = init.get("banner_info")
    if not isinstance(banner, dict) or not banner.get("name"):
        banner = None

    # accounts/check — subscription entitlement + enabled feature flags.
    # Fail-soft: the report still works without it (test doubles may only
    # implement ``post``).
    subscription: dict[str, Any] = {}
    account: dict[str, Any] = {}
    try:
        acct = client.get(_ACCOUNTS_PATH) or {}
        acc = (acct.get("accounts") or {}).get("default") or {}
        meta = acc.get("account") or {}
        ent = acc.get("entitlement") or {}
        sched = ent.get("scheduled_plan_change") or {}
        subscription = {
            "plan": ent.get("subscription_plan"),
            "plan_display_name": meta.get("plan_display_name"),
            "has_active_subscription": ent.get("has_active_subscription"),
            "expires_at": ent.get("expires_at"),
            "renews_at": ent.get("renews_at"),
            "is_delinquent": ent.get("is_delinquent"),
            "scheduled_plan_change": {
                "plan_type": sched.get("plan_type"),
                "changes_at": sched.get("changes_at"),
            }
            if sched.get("plan_type")
            else None,
            "features": meta.get("features") or acc.get("features") or [],
        }
        account = {
            "account_id": meta.get("account_id"),
            "created_time": meta.get("created_time"),
            "structure": meta.get("structure"),
            "has_previously_paid_subscription": meta.get(
                "has_previously_paid_subscription"
            ),
        }
    except Exception:
        subscription = {}

    # /me — identity. Fail-soft as well.
    try:
        me = client.get(_ME_PATH) or {}
        orgs = ((me.get("orgs") or {}).get("data")) or []
        account.update(
            {
                "user_id": me.get("id"),
                "email": me.get("email"),
                "name": me.get("name"),
                "country": me.get("country"),
                "orgs": [o.get("title") or o.get("name") for o in orgs],
                "created": me.get("created"),
            }
        )
    except Exception:
        pass

    def _feature(name: str) -> dict[str, Any] | None:
        return next((f for f in features if f["feature_name"] == name), None)

    light = _feature("deep_research")
    heavy = next(
        (
            f for f in features
            if f["feature_name"].startswith("deep_research_")
            and f["feature_name"] != "deep_research"
        ),
        None,
    )
    deep_research: dict[str, Any] = {
        "light_remaining": (light or {}).get("remaining"),
        "light_resets_after": (light or {}).get("reset_after"),
        "heavy_remaining": (heavy or {}).get("remaining"),
        "heavy_resets_after": (heavy or {}).get("reset_after"),
        "heavy_counter": (heavy or {}).get("feature_name"),
        "heavy_blocked": any(
            b["name"].startswith("deep_research_") for b in blocked
        ),
    }

    report: dict[str, Any] = {
        "fetched_at": _iso(time.time()),
        "models": {
            "default_model_slug": default,
            "intended_default_model_slug": intended,
            "downgraded": bool(intended and default and default != intended),
            "capped": capped_models,
        },
        "features": features,
        "blocked_features": blocked,
        "deep_research": deep_research,
        "banner": banner,
        "subscription": subscription,
        "account": account,
    }

    # Local task queue — counts per lifecycle state (fail-soft).
    try:
        from gpt2agent.taskqueue import TaskQueue

        counts: dict[str, int] = {}
        for t in TaskQueue().list():
            s = t.get("status") or "unknown"
            counts[s] = counts.get(s, 0) + 1
        report["task_queue"] = {"counts": counts, "total": sum(counts.values())}
    except Exception:
        pass

    try:
        from gpt2agent.ratelimit import get_limiter

        lim = get_limiter()
        if lim.enabled:
            st = lim._locked_state()
            now = time.time()
            cooldowns = {}
            for key, until in sorted(st["cooldowns"].items()):
                ts = lim.cooldown_for(key)
                if ts:
                    cooldowns[key] = _iso(ts)
            report["local_rate_limit"] = {
                "enabled": True,
                "conversation_posts_in_window": len(
                    [t for t in st["conv_requests"] if now - t < lim.window_s]
                ),
                "max_per_window": lim.max_per_window,
                "window_s": lim.window_s,
                "min_interval_s": lim.min_interval_s,
                "cooldowns": cooldowns,
            }
        else:
            report["local_rate_limit"] = {"enabled": False}
    except Exception:
        pass

    return report


def format_usage_report(report: dict[str, Any]) -> str:
    """Human-readable rendering for the ``gpt2agent usage`` CLI."""
    lines = [f"Account usage (fetched {report.get('fetched_at') or '?'})", ""]

    acct = report.get("account") or {}
    if acct:
        who = acct.get("email") or acct.get("name") or "?"
        lines.append("Account")
        lines.append(f"  {who}" + (f"  ({acct['name']})" if acct.get("name") else ""))
        bits = []
        if acct.get("account_id"):
            bits.append(f"id {acct['account_id']}")
        if acct.get("country"):
            bits.append(acct["country"])
        if acct.get("created_time"):
            bits.append(f"created {acct['created_time'][:10]}")
        if acct.get("orgs"):
            bits.append("orgs: " + ", ".join(str(o) for o in acct["orgs"]))
        if bits:
            lines.append("  " + "  |  ".join(bits))
        lines.append("")

    sub = report.get("subscription") or {}
    if sub:
        lines.append("Subscription")
        plan = sub.get("plan") or "?"
        status = "active" if sub.get("has_active_subscription") else "INACTIVE"
        renew = sub.get("renews_at") or sub.get("expires_at")
        tail = f"  renews {renew}" if renew else ""
        if sub.get("is_delinquent"):
            tail += "  DELINQUENT"
        lines.append(f"  plan: {plan}  ({status}){tail}")
        sched = sub.get("scheduled_plan_change") or {}
        if sched.get("plan_type"):
            lines.append(
                f"  ⚠ scheduled downgrade to {sched['plan_type']}"
                f" at {sched.get('changes_at')}"
            )
        feats = sub.get("features") or []
        if feats:
            lines.append(f"  account features enabled: {len(feats)}")
        lines.append("")

    banner = report.get("banner")
    if banner:
        note = banner.get("title") or banner.get("name")
        rst = banner.get("resets_after")
        lines.append(
            f"⚠ Account flag: {note}"
            + (f" (resets {rst})" if rst else "")
            + "\n"
        )

    models = report.get("models") or {}
    default = models.get("default_model_slug") or "?"
    intended = models.get("intended_default_model_slug")
    note = f"  intended: {intended} — DOWNGRADED" if models.get("downgraded") else ""
    lines.append(f"Models\n  default: {default}{note}")
    for m in models.get("capped") or []:
        lines.append(f"  capped:  {m['model_slug']} resets {m.get('resets_after')}")

    feats = report.get("features") or []
    if feats:
        lines.append("\nFeature quotas")
        width = max(len(str(f["feature_name"])) for f in feats)
        for f in feats:
            rem = f.get("remaining")
            rem_s = "?" if rem is None else str(rem)
            reset = f.get("reset_after")
            tail = f"  resets {reset}" if reset else ""
            lines.append(f"  {str(f['feature_name']):<{width}}  remaining={rem_s}{tail}")

    dr = report.get("deep_research") or {}
    lines.append("\nDeep Research")
    lr = dr.get("light_remaining")
    lines.append(
        f"  light (deep_research):    remaining={lr if lr is not None else '?'}"
        + (f"  resets {dr['light_resets_after']}" if dr.get("light_resets_after") else "")
    )
    hr = dr.get("heavy_remaining")
    counter = dr.get("heavy_counter") or "no dedicated counter reported"
    if dr.get("heavy_blocked"):
        heavy_s = "BLOCKED" + (
            f" until {dr['heavy_resets_after']}" if dr.get("heavy_resets_after") else ""
        )
    elif hr is not None:
        heavy_s = f"remaining={hr}  resets {dr.get('heavy_resets_after')}"
    else:
        heavy_s = "independent monthly cap — not exposed by this counter"
    lines.append(f"  heavy (connector):        {heavy_s}  [{counter}]")

    blocked = report.get("blocked_features") or []
    if blocked:
        lines.append("\nBlocked features")
        for b in blocked:
            tail = f"  resets {b['resets_after']}" if b.get("resets_after") else ""
            lines.append(f"  {b['name']}{tail}")

    rl = report.get("local_rate_limit")
    if rl:
        lines.append("\nLocal shared budget (this host)")
        if rl.get("enabled"):
            lines.append(
                f"  {rl['conversation_posts_in_window']}/{rl['max_per_window']} "
                f"conversation posts in {rl['window_s'] / 3600:g}h window; "
                f"min interval {rl['min_interval_s']:g}s"
            )
            for key, until in (rl.get("cooldowns") or {}).items():
                lines.append(f"  cooldown {key} until {until}")
        else:
            lines.append("  disabled")

    tq = report.get("task_queue")
    if tq:
        counts = tq.get("counts") or {}
        lines.append("\nLocal task queue")
        if not counts:
            lines.append("  empty")
        else:
            for state, n in sorted(counts.items()):
                lines.append(f"  {state}: {n}")
    return "\n".join(lines)
