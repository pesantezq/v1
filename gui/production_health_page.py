"""
Production Health Page — additive read-only Streamlit page.

Single operator-facing surface that consolidates everything the hardening
track shipped:

  - tools.status.collect_status            — pipeline / sandbox / AI / memo / outcomes
  - tools.smoke_test.validate_registry     — per-artifact shape validation
  - portfolio_automation.env.check_state   — env-var registry state
  - portfolio_automation.artifacts_registry — registry inventory

The page is a strict consumer:

  - imports only library-mode helpers; never invokes a writer
  - reads only files under outputs/* and the bare process environment
  - all artifact loads degrade gracefully (the underlying probes never raise)

Separated into two layers so the data-collection layer can be unit-tested
without Streamlit:

  - :func:`collect_production_health` — pure aggregator, no Streamlit calls
  - :func:`render_production_health_page` — Streamlit UI on top
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Severity badges (mirror tools.status labels)
# ---------------------------------------------------------------------------

SEV_OK = "OK"
SEV_INFO = "INFO"
SEV_WARN = "WARN"
SEV_FAIL = "FAIL"

_BADGE_EMOJI = {
    SEV_OK:   "🟢",
    SEV_INFO: "🔵",
    SEV_WARN: "🟡",
    SEV_FAIL: "🔴",
}


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def _safe(callable_, *args, **kwargs) -> tuple[Any | None, str | None]:
    """Run callable_; return (result, None) on success, (None, error_str) otherwise."""
    try:
        return callable_(*args, **kwargs), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def collect_production_health(repo_root: Path) -> dict[str, Any]:
    """
    Aggregate every probe + registry view into a single dict.  Pure function:
    no Streamlit calls, no file writes, no exceptions surface — internal
    failures become per-section ``error`` keys.

    Output shape::

        {
          "advisory_only": True,
          "no_trade": True,
          "repo_root": str,
          "status":        { ...tools.status.collect_status report... },
          "smoke":         { ...tools.smoke_test.validate_registry report... },
          "env":           { ...portfolio_automation.env.check_state... },
          "registry":      { "total": int, "by_namespace": {ns: int},
                             "entries": [ {name, namespace, ...}, ... ] },
        }

    Any individual section may instead be ``{"error": "<message>"}``.
    """
    out: dict[str, Any] = {
        "advisory_only": True,
        "no_trade": True,
        "repo_root": str(repo_root),
    }

    # --- status (tools.status) ---
    try:
        from tools.status import collect_status
        report, err = _safe(collect_status, repo_root)
        if err is None and report is not None:
            out["status"] = report.to_dict()
        else:
            out["status"] = {"error": err or "no report"}
    except Exception as exc:
        out["status"] = {"error": f"import_failed: {exc}"}

    # --- smoke (tools.smoke_test) ---
    try:
        from tools.smoke_test import validate_registry
        report, err = _safe(validate_registry, repo_root)
        if err is None and report is not None:
            out["smoke"] = report.to_dict()
        else:
            out["smoke"] = {"error": err or "no report"}
    except Exception as exc:
        out["smoke"] = {"error": f"import_failed: {exc}"}

    # --- env (portfolio_automation.env) ---
    try:
        from portfolio_automation.env import check_state
        state, err = _safe(check_state)
        if err is None and state is not None:
            out["env"] = state
        else:
            out["env"] = {"error": err or "no state"}
    except Exception as exc:
        out["env"] = {"error": f"import_failed: {exc}"}

    # --- registry inventory ---
    try:
        from portfolio_automation.artifacts_registry import REGISTRY
        by_ns: dict[str, int] = {}
        entries: list[dict[str, Any]] = []
        for art in REGISTRY:
            ns = art.namespace.value
            by_ns[ns] = by_ns.get(ns, 0) + 1
            entries.append({
                "name": art.name,
                "namespace": ns,
                "relative_path": art.relative_path,
                "format": art.format,
                "writer_module": art.writer_module,
                "optional": art.optional,
                "append_only": art.append_only,
                "observe_only_required": art.observe_only_required,
                "description": art.description,
            })
        out["registry"] = {
            "total": len(entries),
            "by_namespace": by_ns,
            "entries": entries,
        }
    except Exception as exc:
        out["registry"] = {"error": f"import_failed: {exc}"}

    return out


def overall_severity(health: dict[str, Any]) -> str:
    """
    Compute a single overall severity from the status + smoke sub-reports.
    Returns one of OK / INFO / WARN / FAIL.  Higher always wins.
    """
    order = {SEV_OK: 0, SEV_INFO: 1, SEV_WARN: 2, SEV_FAIL: 3}
    worst = SEV_OK
    for key in ("status", "smoke"):
        section = health.get(key, {})
        if not isinstance(section, dict):
            continue
        sev = section.get("overall_severity") or SEV_OK
        if order.get(sev, 0) > order.get(worst, 0):
            worst = sev
    if health.get("env", {}).get("summary", {}).get("required_missing", 0) > 0:
        if order["WARN"] > order.get(worst, 0):
            worst = SEV_WARN
    return worst


# ---------------------------------------------------------------------------
# Streamlit rendering
# ---------------------------------------------------------------------------

def render_production_health_page(repo_root: Path) -> None:
    """
    Render the Production Health page.  Streamlit calls only; data shape
    comes from :func:`collect_production_health`.
    """
    import streamlit as st  # imported lazily so tests can import this module

    st.title("Production Health")
    st.caption(
        "Read-only consolidated view of every probe shipped by the hardening "
        "track. Nothing here writes, mutates, or affects the daily pipeline."
    )

    col_refresh, _ = st.columns([1, 6])
    with col_refresh:
        if st.button("Refresh", key="prod_health_refresh", width="stretch"):
            st.rerun()

    health = collect_production_health(repo_root)
    sev = overall_severity(health)

    # --- Overall banner ---
    banner = f"{_BADGE_EMOJI.get(sev, '')}  **Overall: {sev}**"
    if sev == SEV_FAIL:
        st.error(banner)
    elif sev == SEV_WARN:
        st.warning(banner)
    elif sev == SEV_INFO:
        st.info(banner)
    else:
        st.success(banner)

    # --- Status (tools.status) ---
    st.markdown("### Pipeline & feature health")
    _render_status_section(st, health.get("status", {}))

    # --- Smoke (tools.smoke_test) ---
    st.markdown("### Artifact shape (smoke test)")
    _render_smoke_section(st, health.get("smoke", {}))

    # --- Env (portfolio_automation.env) ---
    st.markdown("### Environment variables")
    _render_env_section(st, health.get("env", {}))

    # --- Registry ---
    st.markdown("### Artifact registry")
    _render_registry_section(st, health.get("registry", {}))

    st.divider()
    st.caption("Advisory only — no trades executed.")


def _render_status_section(st, section: dict[str, Any]) -> None:
    if "error" in section:
        st.error(f"tools.status unavailable: {section['error']}")
        return
    counts = section.get("severity_counts", {})
    cols = st.columns(4)
    for col, label in zip(cols, ("OK", "INFO", "WARN", "FAIL")):
        with col:
            st.metric(label, counts.get(label, 0))
    for check in section.get("checks", []):
        sev = check.get("severity", SEV_INFO)
        emoji = _BADGE_EMOJI.get(sev, "")
        name = check.get("name", "?")
        msg = check.get("message", "")
        with st.expander(f"{emoji} {sev} — {name}: {msg}", expanded=(sev in (SEV_WARN, SEV_FAIL))):
            details = check.get("details", {})
            if details:
                st.json(details, expanded=False)
            else:
                st.caption("(no details)")


def _render_smoke_section(st, section: dict[str, Any]) -> None:
    if "error" in section:
        st.error(f"tools.smoke_test unavailable: {section['error']}")
        return
    counts = section.get("severity_counts", {})
    cols = st.columns(4)
    for col, label in zip(cols, ("OK", "INFO", "WARN", "FAIL")):
        with col:
            st.metric(label, counts.get(label, 0))
    rows: list[dict[str, Any]] = section.get("results", [])
    # Show non-OK rows first; fold OK into a collapsed expander.
    problems = [r for r in rows if r.get("severity") != SEV_OK]
    oks = [r for r in rows if r.get("severity") == SEV_OK]
    for r in problems:
        sev = r.get("severity", SEV_INFO)
        emoji = _BADGE_EMOJI.get(sev, "")
        name = r.get("name", "?")
        msg = r.get("message", "")
        st.write(f"{emoji} **{sev}** — `{name}`: {msg}")
    if oks:
        with st.expander(f"OK artifacts ({len(oks)})", expanded=False):
            for r in oks:
                st.write(f"🟢 `{r.get('name')}` — {r.get('message')}")


def _render_env_section(st, section: dict[str, Any]) -> None:
    if "error" in section:
        st.error(f"env.check_state unavailable: {section['error']}")
        return
    summary = section.get("summary", {})
    cols = st.columns(4)
    with cols[0]:
        st.metric("Total", summary.get("total", 0))
    with cols[1]:
        st.metric("Required set", summary.get("required_set", 0))
    with cols[2]:
        st.metric(
            "Required missing",
            summary.get("required_missing", 0),
            delta=None if summary.get("required_missing", 0) == 0 else "!",
        )
    with cols[3]:
        st.metric("Secrets set", summary.get("secrets_set", 0))

    missing = section.get("missing_required", [])
    if missing:
        st.error(f"Missing required env vars: {', '.join(missing)}")

    loaded_from = section.get("dotenv_loaded_from")
    if loaded_from:
        st.caption(f".env loaded from: `{loaded_from}`")

    groups = section.get("groups", {})
    for group_name in sorted(groups.keys()):
        items = groups[group_name]
        if not items:
            continue
        with st.expander(f"[{group_name}] ({len(items)} vars)", expanded=False):
            for it in items:
                req_marker = "**REQ**" if it.get("required") else "opt"
                secret_marker = " 🔒" if it.get("secret") else ""
                value_display = it.get("value") if it.get("value") is not None else "_(unset)_"
                source = it.get("source", "?")
                st.write(
                    f"- {req_marker} `{it.get('name')}` ({source}) = "
                    f"`{value_display}`{secret_marker}"
                )
                if it.get("aliases_set"):
                    st.caption(
                        f"  also-set aliases: {', '.join(it['aliases_set'])}"
                    )


def _render_registry_section(st, section: dict[str, Any]) -> None:
    if "error" in section:
        st.error(f"artifacts_registry unavailable: {section['error']}")
        return
    total = section.get("total", 0)
    by_ns = section.get("by_namespace", {})
    st.caption(
        f"{total} registered artifacts across "
        f"{', '.join(f'{ns}={n}' for ns, n in sorted(by_ns.items()))}"
    )
    with st.expander("Full registry inventory", expanded=False):
        entries = section.get("entries", [])
        try:
            import pandas as pd  # type: ignore
            df = pd.DataFrame(entries)
            st.dataframe(df, width="stretch", hide_index=True)
        except Exception:
            for e in entries:
                st.json(e, expanded=False)
