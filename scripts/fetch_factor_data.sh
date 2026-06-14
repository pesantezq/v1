#!/usr/bin/env bash
# Fetch Fama-French monthly factor returns into data/factors/ff_monthly.csv.
# One-shot; the loader (portfolio_automation/portfolio_sim/factor_data.py) reads
# the cache offline thereafter. Optional — factor attribution degrades gracefully
# (status: factor_data_unavailable) without it.
#
# Source: Kenneth R. French Data Library
#   https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
#   5-factor: F-F_Research_Data_5_Factors_2x3_CSV.zip
#   momentum: F-F_Momentum_Factor_CSV.zip
#
# The published files are zipped CSVs with header/footer cruft and an annual
# section after the monthly rows; we parse only the monthly (YYYYMM) block and
# write columns: month(YYYY-MM),Mkt-RF,SMB,HML,RMW,CMA,MOM,RF in DECIMAL form.
# (French publishes percent; we divide by 100 so the loader's percent-vs-decimal
# heuristic is a no-op and small monthly returns are not mis-scaled.)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${ROOT}/data/factors"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
mkdir -p "${OUT_DIR}"

BASE="https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
echo "Downloading Fama-French 5-factor + momentum monthly CSVs..."
curl -sSL --max-time 60 -o "${TMP_DIR}/ff5.zip" "${BASE}/F-F_Research_Data_5_Factors_2x3_CSV.zip"
curl -sSL --max-time 60 -o "${TMP_DIR}/mom.zip" "${BASE}/F-F_Momentum_Factor_CSV.zip"

PY="${ROOT}/.venv/bin/python"
[ -x "${PY}" ] || PY="python3"
"${PY}" - "${TMP_DIR}/ff5.zip" "${TMP_DIR}/mom.zip" "${OUT_DIR}/ff_monthly.csv" <<'PYEOF'
import csv, sys, zipfile

ff5_zip, mom_zip, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

def parse_ff(zip_path, value_cols):
    """Return {YYYYMM: {col: decimal}} for monthly rows only."""
    with zipfile.ZipFile(zip_path) as zf:
        text = zf.read(zf.namelist()[0]).decode("latin-1")
    out, header = {}, None
    for raw in text.splitlines():
        parts = [p.strip() for p in raw.split(",")]
        if header is None:
            if parts and parts[0] == "" and len(parts) > 1 and parts[1] in ("Mkt-RF", "Mom"):
                header = parts[1:]
            continue
        key = parts[0]
        if not (len(key) == 6 and key.isdigit()):  # monthly rows only; skips annual footer + blanks
            continue
        rec = {}
        for col, v in zip(header, parts[1:]):
            if col not in value_cols:
                continue
            try:
                x = float(v)
            except ValueError:
                continue
            if x in (-99.99, -999.0):  # F-F missing-data sentinels
                continue
            rec[col] = x / 100.0  # percent -> decimal
        if rec:
            out[key] = rec
    return out

ff5 = parse_ff(ff5_zip, ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"))
mom = parse_ff(mom_zip, ("Mom",))
cols = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "MOM", "RF"]
n = 0
with open(out_path, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["month"] + cols)
    for ym in sorted(ff5):
        rec = dict(ff5[ym])
        m = mom.get(ym, {}).get("Mom")
        if m is not None:
            rec["MOM"] = m
        w.writerow([f"{ym[:4]}-{ym[4:]}"] + [f"{rec[c]:.6f}" if c in rec else "" for c in cols])
        n += 1
print(f"Wrote {out_path}: {n} monthly rows ({min(ff5)}..{max(ff5)})")
PYEOF

echo "Done. Factor attribution activates automatically on the next strategy-lab run:"
echo "  ${PY} -m portfolio_automation.portfolio_sim.run_strategy_lab --root ${ROOT} --run-mode discovery"
