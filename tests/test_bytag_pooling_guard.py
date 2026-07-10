"""Cross-gauge pooling guard for pattern_efficacy.by_tag consumers.

pattern_efficacy_monthly.by_tag pools sector/tag outcomes across EVERY gauge era.
A sector a retired gauge handled badly reads significance=='loser' forever (and one
it boosted reads 'winner' forever) even after the current gauge flipped. The D3
sector_drag detector in quant_watch_probes.py was fixed (commit 50da3c88) to cross-
check the current fingerprint's own sector_composition. These tests cover the two
sibling consumers that also made a current-scope claim off the pooled view:

  * gui_v2/data/dash_quant._efficacy_label_and_status — GUARDED (veto on genuine
    pooled-vs-live disagreement), with the required
    "pooled-loser-but-current-fp-winner" and "genuinely-bad-on-current-fp" fixtures.
  * watchlist_scanner/daily_memo._pattern_confirmed_section — LABELLED (display-only
    pooled/all-era caveat).
"""
from __future__ import annotations

from gui_v2.data.dash_quant import (
    _current_fp_sector_sign,
    _efficacy_label_and_status,
)
from watchlist_scanner.daily_memo import (
    _PATTERN_CONFIRMED_POOLED_CAVEAT,
    _pattern_confirmed_section,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FP = "5687885c755dd6c9"


def _retune(sector_label: str, mean_return_1d: float, resolved_1d: int) -> dict:
    """A retune_impact.json shaped payload with one sector on the current fp."""
    return {
        "current_fingerprint": _FP,
        "outcome_attribution": {
            "by_fingerprint": {
                _FP: {
                    "sector_composition": {
                        sector_label: {
                            "mean_return_1d": mean_return_1d,
                            "resolved_1d": resolved_1d,
                        }
                    }
                }
            }
        },
    }


def _label(by_tag: dict, retune: dict | None) -> tuple[str, str]:
    # snapshots/rows comfortably above the thin-sample gates so we exercise by_tag.
    return _efficacy_label_and_status(
        snapshots_consumed=200,
        rows_matched=200,
        by_tag=by_tag,
        lookback_days=30,
        retune=retune,
    )


# ---------------------------------------------------------------------------
# _current_fp_sector_sign — label normalization + sign
# ---------------------------------------------------------------------------

def test_sign_matches_across_label_conventions():
    # by_tag uses 'Communication_Services'; sector_composition uses spaces.
    r = _retune("Communication Services", 1.97, 26)
    assert _current_fp_sector_sign(r, "Communication_Services") == "positive"


def test_sign_unknown_when_thin_sample():
    r = _retune("Energy", 0.5, 5)  # resolved_1d below the 20 min
    assert _current_fp_sector_sign(r, "Energy") == "unknown"


def test_sign_unknown_when_absent():
    assert _current_fp_sector_sign(None, "Energy") == "unknown"
    r = _retune("Energy", 0.5, 40)
    assert _current_fp_sector_sign(r, "Technology") == "unknown"


# ---------------------------------------------------------------------------
# GUARDED consumer: dash_quant._efficacy_label_and_status
# ---------------------------------------------------------------------------

def test_pooled_loser_but_current_fp_winner_is_not_weak():
    """The Communication_Services 2026-07-10 case: pooled 'loser' but the live
    gauge treats it as the best sector. The guard must NOT report 'Weak'."""
    by_tag = {"sector:Communication_Services": {"significance": "loser"}}
    retune = _retune("Communication Services", 1.97, 26)  # live gauge positive
    label, status = _label(by_tag, retune)
    assert status != "warning"
    assert label != "Weak"


def test_genuinely_bad_on_current_fp_is_weak():
    """Same pooled 'loser', but the live gauge also has a negative mean → the
    verdict is confirmed, the probe/weakness stands."""
    by_tag = {"sector:Communication_Services": {"significance": "loser"}}
    retune = _retune("Communication Services", -0.85, 26)  # live gauge negative
    label, status = _label(by_tag, retune)
    assert (label, status) == ("Weak", "warning")


def test_pooled_winner_but_current_fp_negative_is_not_improving():
    """Symmetric direction: a pooled 'winner' the current gauge cooled on must not
    inflate the card to 'Improving'."""
    by_tag = {"sector:Financial_Services": {"significance": "winner"}}
    retune = _retune("Financial Services", -0.20, 52)  # live gauge negative
    label, status = _label(by_tag, retune)
    assert label != "Improving"


def test_pooled_winner_confirmed_by_current_fp_stays_improving():
    by_tag = {"sector:Financial_Services": {"significance": "winner"}}
    retune = _retune("Financial Services", 0.61, 52)  # live gauge positive
    label, status = _label(by_tag, retune)
    assert (label, status) == ("Improving", "ok")


def test_backward_compatible_without_retune():
    """retune=None ⇒ prior behaviour (pooled loser counts as weak)."""
    by_tag = {"sector:Communication_Services": {"significance": "loser"}}
    label, status = _label(by_tag, None)
    assert (label, status) == ("Weak", "warning")


def test_non_sector_tag_never_guarded():
    """The guard is sector-scoped only — a source: tag loser is untouched even when
    a retune payload is present."""
    by_tag = {"source:recent_signal": {"significance": "loser"}}
    retune = _retune("Communication Services", 1.97, 26)
    label, status = _label(by_tag, retune)
    assert (label, status) == ("Weak", "warning")


def test_thin_current_fp_slice_does_not_veto():
    """When the current-fp slice is too thin to trust, the pooled verdict stands."""
    by_tag = {"sector:Communication_Services": {"significance": "loser"}}
    retune = _retune("Communication Services", 1.97, 5)  # thin → unknown
    label, status = _label(by_tag, retune)
    assert (label, status) == ("Weak", "warning")


# ---------------------------------------------------------------------------
# LABELLED consumer: daily_memo._pattern_confirmed_section
# ---------------------------------------------------------------------------

_ROWS = ["`AAPL` (Technology) — 2 winning tags: Technology, recent_signal"]


def test_section_empty_rows_returns_nothing():
    assert _pattern_confirmed_section([], markdown=True) == []
    assert _pattern_confirmed_section([], markdown=False) == []


def test_markdown_section_carries_pooled_caveat_and_body():
    lines = _pattern_confirmed_section(_ROWS, markdown=True)
    joined = "\n".join(lines)
    assert _PATTERN_CONFIRMED_POOLED_CAVEAT in joined
    assert any("pattern-confirmed candidates" in ln.lower() for ln in lines)
    assert any("AAPL" in ln for ln in lines)


def test_text_section_carries_pooled_caveat_and_strips_backticks():
    lines = _pattern_confirmed_section(_ROWS, markdown=False)
    joined = "\n".join(lines)
    assert _PATTERN_CONFIRMED_POOLED_CAVEAT in joined
    assert any("PATTERN-CONFIRMED CANDIDATES" in ln for ln in lines)
    # plain-text variant strips markdown backticks
    assert "`" not in joined
