#!/usr/bin/env bash
# Fetch Fama-French monthly factor returns into data/factors/ff_monthly.csv.
# One-shot; the loader reads the cache offline thereafter. Optional — factor
# attribution degrades gracefully (status: factor_data_unavailable) without it.
#
# Source: Kenneth R. French Data Library
#   https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
# 5-factor: F-F_Research_Data_5_Factors_2x3_CSV.zip ; Momentum: F-F_Momentum_Factor_CSV.zip
#
# Because the published files are zipped CSVs with header/footer cruft, this
# script documents the steps; adapt the parse to the current file format. The
# loader expects columns: month(YYYY-MM),Mkt-RF,SMB,HML,RMW,CMA,MOM,RF (percent ok).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${ROOT}/data/factors"
mkdir -p "${OUT_DIR}"
echo "Target: ${OUT_DIR}/ff_monthly.csv"
echo "Download the 5-factor + momentum monthly CSVs from the Kenneth French"
echo "Data Library, merge on month, and write columns:"
echo "  month,Mkt-RF,SMB,HML,RMW,CMA,MOM,RF   (percent values are auto-normalized)"
echo "Then factor attribution activates automatically on the next strategy-lab run."
