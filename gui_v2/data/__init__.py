"""Pure data-collection layer for gui_v2 pages.

Every collect_*_view function:
  - reads outputs/* and registry data only
  - returns a dict
  - never raises (internal failures become per-section `error` keys)
"""
