"""Focused renderer modules split out of ``watchlist_scanner.daily_memo``.

``daily_memo`` had grown to 3,749 lines and hosted four of the eleven rendering
defects found by the 2026-08-07 memo review. 492 of those lines were
unreachable — ``build_daily_memo`` and ``build_daily_memo_md`` were each
DEFINED TWICE at module level, so the earlier pair was permanently shadowed.

The remaining sections are being moved here one cohesive group at a time, each
re-exported from ``daily_memo`` so every existing import path keeps working.
Groups move only when their coupling is genuinely low; anything still sharing a
long tail of module constants stays put until it can be moved without inventing
a new tangle to replace the old one.
"""
