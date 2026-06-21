"""
Stage 9c4 — Social Sentiment Pipeline CLI runner.

Reads the top-N tickers from crowd_multi_source_velocity.json (written by
Stage 9c1), then runs the full sentiment pipeline: fetch text posts from
configured free connectors (Bluesky/Mastodon/Lemmy), score with FinBERT,
aggregate cross-source, write simulation adjustments.

Never feeds decision_plan.json. Sandbox/simulation only.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("stockbot.social_sentiment.runner")

_DEFAULT_TOP_N = 25
_VELOCITY_ARTIFACT = "outputs/sandbox/discovery/crowd_multi_source_velocity.json"


def _load_top_tickers(root: Path, top_n: int) -> list[str]:
    """Read velocity artifact and return top-N tickers sorted by mention_velocity."""
    vel_path = root / _VELOCITY_ARTIFACT
    if not vel_path.exists():
        logger.warning("Velocity artifact not found: %s — skipping sentiment run", vel_path)
        return []
    try:
        doc = json.loads(vel_path.read_text())
        records = doc.get("records") or []
        sorted_recs = sorted(records, key=lambda r: float(r.get("mention_velocity") or 0), reverse=True)
        return [r["ticker"] for r in sorted_recs[:top_n] if r.get("ticker")]
    except Exception as exc:
        logger.warning("Failed to load velocity artifact: %s", exc)
        return []


def _load_attention_data(root: Path) -> dict[str, float]:
    """Build {ticker: attention_score} from the velocity artifact for Phase 9 bus extension."""
    vel_path = root / _VELOCITY_ARTIFACT
    if not vel_path.exists():
        return {}
    try:
        doc = json.loads(vel_path.read_text())
        return {
            r["ticker"]: float(r.get("mention_velocity", 0))
            for r in (doc.get("records") or [])
            if r.get("ticker")
        }
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Social Sentiment Pipeline (Stage 9c4)")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--top-n", type=int, default=_DEFAULT_TOP_N,
                        help=f"Top-N tickers to score (default {_DEFAULT_TOP_N})")
    parser.add_argument("--run-mode", default="discovery")
    args = parser.parse_args()

    root = Path(args.root).resolve()

    # Load config
    cfg_path = root / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"config load failed: {exc}"}))
        return 1

    crowd_cfg = cfg.get("crowd_radar") or {}
    if not crowd_cfg.get("enabled", False):
        print(json.dumps({"status": "disabled", "reason": "crowd_radar.enabled=false"}))
        return 0

    sim_cfg = crowd_cfg.get("simulation_social_sentiment") or {}
    if not sim_cfg.get("enabled", False):
        print(json.dumps({"status": "disabled",
                          "reason": "simulation_social_sentiment.enabled=false"}))
        return 0

    # Pick tickers from velocity artifact
    tickers = _load_top_tickers(root, args.top_n)
    if not tickers:
        print(json.dumps({"status": "skipped", "reason": "no_velocity_tickers",
                          "tickers_scored": 0}))
        return 0

    logger.info("Running social sentiment pipeline for %d tickers: %s...",
                len(tickers), tickers[:5])

    attention_data = _load_attention_data(root)

    from portfolio_automation.social_sentiment.pipeline import run_social_sentiment_pipeline
    result = run_social_sentiment_pipeline(
        tickers,
        root=root,
        cfg=crowd_cfg,
        attention_data=attention_data,
    )

    print(json.dumps({
        "status": result.get("status", "unknown"),
        "tickers_processed": result.get("tickers_processed", 0),
        "tickers_scored": result.get("tickers_scored", 0),
        "simulation_active": result.get("simulation_active", True),
        "feeds_decision_engine": result.get("feeds_decision_engine", False),
        "warnings": result.get("warnings", [])[:5],
    }))
    return 0 if result.get("status") in ("ok", "insufficient_data", "disabled") else 1


if __name__ == "__main__":
    sys.exit(main())
