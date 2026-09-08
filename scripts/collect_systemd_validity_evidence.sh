#!/usr/bin/env bash
# Capture systemd unit-validity evidence from a host — STRICTLY READ-ONLY.
#
# Emits a simple record stream on stdout for `scripts/certify_systemd_validity.py`
# to turn into a verdict. The verdict is deliberately computed elsewhere: this
# script only observes, so the host never runs certification logic and the
# certification logic never runs commands.
#
# Every command here observes. None mutate:
#   systemd-analyze --version | verify   systemctl show | list-unit-files
# There is no daemon-reload, start/stop/restart/reload, enable/disable,
# mask/unmask, and no file is written anywhere on the host.
#
# `systemd-analyze verify` is invoked BY UNIT NAME so drop-ins apply with
# normal precedence, and with --recursive-errors=no so that warnings on the
# named unit produce a non-zero exit status without inheriting warnings from
# unrelated dependency units. See portfolio_automation/release/systemd_validity
# for the measurements behind both choices.
set -u

UNITS="${STOCKBOT_EXPECTED_UNITS:-\
stockbot-streamlit.service \
stockbot-dashboard.service \
stockbot-daily.service \
stockbot-sandbox-daily.service \
cloudflared-stockbot.service}"

# Which units count as "relevant" for inventory completeness.
PATTERN="${STOCKBOT_UNIT_PATTERN:-stockbot|cloudflared}"

echo "##HOST"
hostname
echo "##CHECKED_AT"
date -u +%Y-%m-%dT%H:%M:%SZ
echo "##SYSTEMD_VERSION"
systemd-analyze --version 2>/dev/null | head -1

echo "##EXPECTED"
for u in $UNITS; do echo "$u"; done

# Discovery is over unit FILES, so a relevant unit that exists but failed to
# load is still discovered — being unloaded must surface as a failure rather
# than as an absence nobody noticed.
echo "##DISCOVERED"
systemctl list-unit-files --no-pager --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -E "$PATTERN" | sort -u

for u in $UNITS; do
  echo "##SHOW $u"
  systemctl show "$u" \
    -p Id -p LoadState -p FragmentPath -p DropInPaths \
    -p NeedDaemonReload -p LoadError --no-pager 2>/dev/null
done

for u in $UNITS; do
  out=$(systemd-analyze verify --recursive-errors=no "$u" 2>&1)
  rc=$?
  echo "##VERIFY $u $rc"
  # Bounded here as well as in the certifier: an evidence stream is not a log.
  echo "$out" | head -20
done

echo "##END"
