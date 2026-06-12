"""
Cross-run mention-history persistence + market-context join for Crowd Radar.

Two things the classifier needs that a single run of post text cannot provide:

1. **Mention velocity** — a z-score needs a baseline of prior daily mention
   counts. We persist a rolling per-ticker ledger and feed the prior window to
   the aggregator; today's counts are appended afterward. Without this, every
   velocity-dependent state (emerging_dd / hype_acceleration / crowd_exhaustion)
   is unreachable.

2. **Market context** — whether a ticker already has public news, and whether
   price/volume already moved. We join this from artifacts the daily pipeline
   already produces (news_intelligence.json, watchlist_signals.json), so the
   join is free (no FMP/network) and degrades to neutral/None when absent.

All functions are fail-safe: any read/parse error degrades to empty/neutral and
never raises.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.social_intelligence.context_join")

# Sandbox-namespace ledger (relative to outputs/sandbox/).
MENTION_HISTORY_REL = "discovery/crowd_mention_history.json"
DEFAULT_WINDOW = 20  # rolling trading-day window for the velocity baseline


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Mention history (velocity baseline)
# ---------------------------------------------------------------------------

def load_mention_history(root: Path) -> dict[str, list[int]]:
    """Return ``{ticker: [prior daily counts]}`` (excludes today). Empty on miss."""
    doc = _safe_json(root / "outputs" / "sandbox" / MENTION_HISTORY_REL)
    if not isinstance(doc, dict):
        return {}
    hist = doc.get("history")
    if not isinstance(hist, dict):
        return {}
    out: dict[str, list[int]] = {}
    for tkr, counts in hist.items():
        if isinstance(counts, list):
            out[str(tkr).upper()] = [int(c) for c in counts if isinstance(c, (int, float))]
    return out


def update_mention_history(
    prior: dict[str, list[int]],
    today_counts: dict[str, int],
    *,
    window: int = DEFAULT_WINDOW,
) -> dict[str, list[int]]:
    """
    Append today's per-ticker counts to *prior*, trimming each series to *window*.

    Tickers seen historically but not today get a 0 appended (so a ticker that
    goes quiet decays out of the baseline rather than vanishing instantly).
    """
    updated: dict[str, list[int]] = {}
    tickers = set(prior) | {t.upper() for t in today_counts}
    for tkr in tickers:
        series = list(prior.get(tkr, []))
        series.append(int(today_counts.get(tkr, 0)))
        updated[tkr] = series[-window:]
    return updated


def build_history_payload(history: dict[str, list[int]], *, window: int, created_at: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "source": "public_knowledge_velocity_layer",
        "updated_at": created_at,
        "window": window,
        "ticker_count": len(history),
        "history": history,
    }


# ---------------------------------------------------------------------------
# Market context (news / price / volume join)
# ---------------------------------------------------------------------------

def build_market_context(root: Path) -> dict[str, dict[str, Any]]:
    """
    Build ``{ticker: {external_news_match, price_move_before_social_spike,
    volume_confirmation, options_or_short_interest_context}}`` from existing
    pipeline artifacts. Fail-safe: missing artifacts → empty / None fields.

    - ``external_news_match`` from ``news_intelligence.json`` evidence packets
      (entity_key + related_tickers).
    - ``price_move_before_social_spike`` (pct) and ``volume_confirmation`` (bool)
      from ``watchlist_signals.json`` results.
    - ``options_or_short_interest_context`` is left ``None`` — no free artifact
      carries short-interest / options skew yet (reflexive_squeeze_risk stays
      dormant until a short-interest feed is wired; see docs).
    """
    ctx: dict[str, dict[str, Any]] = {}

    # News match.
    news = _safe_json(root / "outputs" / "latest" / "news_intelligence.json")
    news_tickers: set[str] = set()
    if isinstance(news, dict):
        for pkt in news.get("evidence_packets") or []:
            if not isinstance(pkt, dict):
                continue
            ek = str(pkt.get("entity_key", "")).upper().strip()
            if ek:
                news_tickers.add(ek)
            for rt in pkt.get("related_tickers") or []:
                news_tickers.add(str(rt).upper().strip())

    # Price / volume.
    signals = _safe_json(root / "outputs" / "latest" / "watchlist_signals.json")
    if isinstance(signals, dict):
        for row in signals.get("results") or []:
            if not isinstance(row, dict):
                continue
            tkr = str(row.get("ticker", "")).upper().strip()
            if not tkr:
                continue
            entry = ctx.setdefault(tkr, {})
            pc = row.get("price_change_pct")
            if isinstance(pc, (int, float)):
                entry["price_move_before_social_spike"] = float(pc)
            vs = row.get("volume_spike")
            if vs is not None:
                entry["volume_confirmation"] = bool(vs)

    # Fold in news match for every ticker mentioned in either source.
    for tkr in news_tickers:
        ctx.setdefault(tkr, {})["external_news_match"] = True

    # Ensure neutral defaults exist for any partially-populated ticker.
    for entry in ctx.values():
        entry.setdefault("external_news_match", False)
        entry.setdefault("options_or_short_interest_context", None)

    return ctx
