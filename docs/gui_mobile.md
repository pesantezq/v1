# GUI Mobile Usage

## Overview

The StockBot Dashboard v2 is mobile-first. It renders correctly in a standard phone
browser at 390 px viewport width (iPhone 14 reference). No native app is required.

---

## Bottom Navigation

On screens narrower than the `md` Tailwind breakpoint (< 768 px), the top navigation
bar hides its link row and a **fixed bottom navigation bar** appears instead.

The five bottom-nav tabs correspond directly to the five main persona routes:

| Icon | Label | Route |
|---|---|---|
| House | Today | `/dashboard/today` |
| Grid | Portfolio | `/dashboard/portfolio` |
| Hash | Quant | `/dashboard/quant` |
| Server | System | `/dashboard/system` |
| Document | Memo | `/dashboard/memo` |

The bottom nav is rendered by `gui_v2/templates/components/bottom_nav.html` and
included in the base layout (`base.html`). It is hidden on `md:` and wider screens
(`md:hidden` class), where the top nav takes over.

The `/dashboard/portfolio-sync` and `/dashboard/portfolio-config` utility views are
accessible from the Portfolio tab on mobile.

---

## Stacked Cards — No Wide Tables, No Horizontal Scroll

Cards on all dashboard pages use `grid-cols-1` as the base column layout, expanding
to `sm:grid-cols-2` and `md:grid-cols-3` on wider screens. This means:

- On a phone, every card stacks vertically — no side-by-side overflow.
- Artifact paths in evidence drawers wrap naturally; they do not force horizontal
  scroll.
- No wide data tables exist in the persona views. Where tabular data is needed,
  it is presented as labeled key-value rows or card lists.

---

## Mobile Status Bar

A compact status bar is injected at the top of every page on mobile, showing the
current `nav_severity` (OK / WARN / FAIL) as a colored dot plus label:

```
StockBot            • OK
```

This is rendered by `gui_v2/templates/components/mobile_status_bar.html` and is
hidden on desktop (`md:hidden`).

---

## "Today" Answers in Under 15 Seconds

The `/dashboard/today` page is designed to answer "what matters right now?" in
under 15 seconds on a phone:

1. The observe-only banner confirms no trade has been placed.
2. The mobile status bar shows system health at a glance.
3. Status cards (Decision Plan, System Health, Risk Delta) surface the three
   most important signals in compact, labeled chips.
4. Cards auto-refresh every 60 seconds via HTMX `hx-trigger="every 60s"` so
   the page stays current without a manual reload.

---

## 390 px Viewport Compatibility

The layout uses:
- `max-w-7xl mx-auto px-6` on desktop (constrained, centered)
- Full-width stacked cards on mobile with `px-4` internal padding
- `text-xs`/`text-sm` for labels and summaries — readable without zooming
- Touch-friendly tap targets: bottom-nav items are `px-3 py-1` with icon +
  label, well above the 44 px minimum recommended tap size

---

## Accessing the Dashboard on Your Phone

The dashboard runs at `http://<vps-ip>:8502`. To access it safely from a phone
outside your local network, use Tailscale (recommended) or Cloudflare Tunnel. See
`docs/gui_remote_access.md` for the full setup guide.
