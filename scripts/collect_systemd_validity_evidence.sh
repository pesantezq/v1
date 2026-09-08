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

# The .timer units are listed deliberately. They are production execution
# surfaces -- stockbot-daily.timer is what actually starts the daily run, and
# deploy/install_systemd.sh installs it alongside the service. Omitting them
# would both leave their syntax unverified and make discovery report them as
# unexpected, since the discovery pattern matches them.
UNITS="${STOCKBOT_EXPECTED_UNITS:-\
stockbot-streamlit.service \
stockbot-dashboard.service \
stockbot-daily.service \
stockbot-daily.timer \
stockbot-sandbox-daily.service \
stockbot-sandbox-daily.timer \
cloudflared-stockbot.service}"

# Units some deployments install and some do not. Absence is tolerated;
# presence is NOT a free pass -- an installed optional unit is verified exactly
# like a required one, so nothing here goes unchecked on a host that has it.
#
#   sandbox lane   -- deploy/install_systemd.sh does not install it and
#                     docs/DAILY_SANDBOX_RUN.md describes it as optional, so
#                     requiring it would make a standard install unable to PASS.
#   streamlit      -- docs/STREAMLIT_RETIREMENT.md is a supported procedure that
#                     ends in `systemctl disable --now stockbot-streamlit.service`
#                     and `rm /etc/systemd/system/stockbot-streamlit.service`.
#                     A host that has completed it is correctly configured, so a
#                     permanently-failing gate there would be the gate's bug, not
#                     the host's. It is still fully verified wherever it exists.
OPTIONAL="${STOCKBOT_OPTIONAL_UNITS:-\
stockbot-sandbox-daily.service \
stockbot-sandbox-daily.timer \
stockbot-streamlit.service}"

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
echo "##OPTIONAL"
for u in $OPTIONAL; do echo "$u"; done

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
  # An optional unit that is not installed is skipped here rather than being
  # recorded as a verifier failure; the certifier decides whether its absence
  # matters. Anything installed is verified, optional or not.
  if ! systemctl list-unit-files "$u" --no-pager --no-legend 2>/dev/null \
       | grep -q .; then
    case " $OPTIONAL " in *" $u "*) continue;; esac
  fi
  # The command is emitted as evidence, not reconstructed by the reader. A
  # certifier that assumes the ideal invocation cannot tell a capture taken
  # WITH the required flag from one taken without it -- and without the flag a
  # zero exit status means nothing (systemd-analyze(1)).
  CMD=(systemd-analyze verify --recursive-errors=no "$u")
  out=$("${CMD[@]}" 2>&1)
  rc=$?
  echo "##VERIFYCMD $u"
  echo "${CMD[*]}"
  echo "##VERIFY $u $rc"
  # Bounded here as well as in the certifier: an evidence stream is not a log.
  echo "$out" | head -20
done

echo "##END"
