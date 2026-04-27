"""
theme_discovery CLI entry point.

Usage (from project root):
    python -m theme_discovery
    python -m theme_discovery --top-n 15
    python -m theme_discovery --dry-run
    python -m theme_discovery --max-articles 50 --debug
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from theme_discovery.collector import collect_articles
from theme_discovery.extractor import extract
from theme_discovery.history import load_theme_history, update_theme_history
from theme_discovery.scorer import score

logger = logging.getLogger("theme_discovery")

_OUTPUT_PATH = _ROOT / "outputs" / "latest" / "theme_opportunities.json"
_HISTORY_PATH = _ROOT / "outputs" / "history" / "theme_history.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover market themes from news feeds (no LLM required).",
    )
    parser.add_argument("--top-n", type=int, default=10,
                        help="Maximum themes to output (default: 10)")
    parser.add_argument("--max-articles", type=int, default=100,
                        help="Maximum articles to collect (default: 100)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print JSON to stdout; do not write any files")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    generated_at = datetime.now(timezone.utc).isoformat()

    # 1. Collect
    articles = collect_articles(root=_ROOT, max_items=args.max_articles)
    if not articles:
        logger.warning("theme_discovery: no articles collected — output will be empty")

    # 2. Extract
    extract_result = extract(articles)

    if args.debug:
        logger.debug(
            "theme_discovery: collected %d articles | classified themes=%d | emerging phrases=%d",
            len(articles),
            len(extract_result.classified),
            len(extract_result.emerging),
        )
        _log_top_groups("classified", extract_result.classified)
        _log_top_groups("emerging", extract_result.emerging)

    # 3. Load history
    history = load_theme_history(_HISTORY_PATH)
    prior_runs = len(history.get("runs", []))
    if args.debug:
        logger.debug(
            "theme_discovery: history loaded — %d prior runs from %s",
            prior_runs,
            _HISTORY_PATH,
        )

    # 4. Score
    opportunities = score(extract_result, history, top_n=args.top_n)

    # 5. Build output
    output = {
        "generated_at": generated_at,
        "article_count": len(articles),
        "theme_count": len(opportunities),
        "themes": [o.to_dict() for o in opportunities],
    }

    if args.dry_run:
        print(json.dumps(output, indent=2))
        return 0

    # 6. Write latest output
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    logger.info("theme_discovery: wrote %d themes → %s", len(opportunities), _OUTPUT_PATH)

    # 7. Update history (not written on dry-run)
    update_theme_history(_HISTORY_PATH, generated_at, opportunities)
    logger.info("theme_discovery: updated history → %s", _HISTORY_PATH)

    return 0


def _log_top_groups(label: str, groups: dict) -> None:
    if not groups:
        logger.debug("theme_discovery: %s — (none)", label)
        return
    ranked = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]
    for name, sigs in ranked:
        logger.debug("  %s  %-30s  mentions=%d", label, name, len(sigs))


if __name__ == "__main__":
    sys.exit(main())
