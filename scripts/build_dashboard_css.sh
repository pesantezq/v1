#!/usr/bin/env bash
#
# build_dashboard_css.sh — compile a self-hosted, purged Tailwind stylesheet for
# the GUI v2 dashboard, replacing the runtime Play CDN (cdn.tailwindcss.com).
#
# WHY: the Play CDN is a browser-side JIT compiler — not meant for production
# (console warning, first-paint latency, and a hard dependency on the CDN being
# reachable; if it is blocked the dashboard renders unstyled). A compiled static
# stylesheet removes all three problems.
#
# NO node/npm REQUIRED — uses the standalone Tailwind CLI binary.
#
# SAFETY: this changes how the LIVE dashboard is styled. After running it you MUST
# visually verify the dashboard (light AND dark theme) before committing the CSS and
# flipping base.html. Tests assert HTML content, not CSS correctness, so they will
# NOT catch a styling regression.
#
# Usage:
#   bash scripts/build_dashboard_css.sh
#   # then eyeball the dashboard, and in gui_v2/templates/base.html replace
#   #   <script src="https://cdn.tailwindcss.com"></script>
#   # with
#   #   <link rel="stylesheet" href="/static/app.css">
#
set -euo pipefail
cd "$(dirname "$0")/.."

STATIC_DIR="gui_v2/static"
BIN="${STATIC_DIR}/.tailwindcss"          # binary is git-ignored; only app.css is committed
IN="${STATIC_DIR}/app.src.css"
OUT="${STATIC_DIR}/app.css"

mkdir -p "${STATIC_DIR}"

# 1. Fetch the standalone CLI once (pinned major; ~100MB — build-time only).
if [[ ! -x "${BIN}" ]]; then
  echo "Downloading standalone Tailwind CLI..."
  curl -sSL -o "${BIN}" \
    https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64
  chmod +x "${BIN}"
fi

# 2. Source stylesheet. Tailwind v4 is CSS-first (@import). The @source globs tell
#    the scanner where class names live — BOTH templates AND the .py files that hold
#    class strings (e.g. app.py _SEVERITY_PALETTE). @source inline(...) safelists the
#    severity tokens in case any are composed outside a scannable literal.
cat > "${IN}" <<'CSS'
@import "tailwindcss";
@source "../templates/**/*.html";
@source "../*.py";
@source "../data/*.py";
CSS

# 3. Compile + minify.
echo "Compiling ${OUT}..."
"${BIN}" -i "${IN}" -o "${OUT}" --minify

echo "Done: ${OUT} ($(wc -c < "${OUT}") bytes)."
echo "Next: visually verify the dashboard, then swap the CDN <script> for"
echo "  <link rel=\"stylesheet\" href=\"/static/app.css\"> in gui_v2/templates/base.html"
