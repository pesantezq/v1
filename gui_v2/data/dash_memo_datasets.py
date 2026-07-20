"""Observe-only domain sub-tab view over outputs/latest/memo_datasets.json.

Pure consumer: reshapes the memo_datasets producer artifact (Tasks 1-3) into a
render-ready ``{"has_datasets": bool, "domains": [...]}`` shape for the
/dashboard/memo domain sub-tabs. No recompute — every field is surfaced
verbatim from the artifact. Never feeds the decision engine
(``feeds_decision_engine`` is always False here, mirroring the producer).
Null-tolerant: an absent or corrupt artifact degrades to the empty shape
instead of raising, so the memo route never breaks on a missing producer run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import _read_json


def collect_memo_datasets_view(root: Path) -> dict[str, Any]:
    """Load and reshape ``outputs/latest/memo_datasets.json`` for the GUI.

    Returns::

        {
          "has_datasets":         bool,
          "domains":              [{"key", "headline", "status", "sections",
                                     "warnings"} ...],
          "feeds_decision_engine": False,
          "generated_at":         str | None,   # only when has_datasets
        }
    """
    art = _read_json(Path(root) / "outputs" / "latest" / "memo_datasets.json")
    if not isinstance(art, dict):
        return {"has_datasets": False, "domains": [], "feeds_decision_engine": False}

    raw_domains = art.get("domains")
    if not isinstance(raw_domains, dict) or not raw_domains:
        return {"has_datasets": False, "domains": [], "feeds_decision_engine": False}

    domains: list[dict[str, Any]] = []
    for key, dom in raw_domains.items():
        if not isinstance(dom, dict):
            continue
        domains.append({
            "key": key,
            "headline": dom.get("headline", key),
            "status": dom.get("status", "unavailable"),
            "sections": dom.get("sections", []),
            "warnings": dom.get("warnings", []),
        })

    return {
        "has_datasets": True,
        "domains": domains,
        "feeds_decision_engine": False,
        "generated_at": art.get("generated_at"),
    }
