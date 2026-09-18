"""Account usage report — how much is used, what is left, when it resets.

Aggregates the two sources of truth:

* ``POST /backend-api/conversation/init`` — the same bookkeeping call the web
  app issues on page load. Carries ``model_limits`` (per-model caps),
  ``limits_progress`` (per-feature remaining counters), ``blocked_features``
  and the default/intended model slugs.
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
    }

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
    return "\n".join(lines)
