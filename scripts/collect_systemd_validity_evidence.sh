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
RELEASE_POINTER="${STOCKBOT_RELEASE_POINTER:-/opt/stockbot/current}"

# The unit load directories this gate anchors. ASKED OF SYSTEMD rather than
# hardcoded: `systemd-analyze unit-paths` lists the load directories for
# units, and the effective path on the reference host (systemd 255) is wider
# than the three obvious ones -- it also includes /etc/systemd/system.control,
# /run/systemd/system.control, /run/systemd/transient, the .attached and
# generator directories, and /usr/local/lib/systemd/system. A transient
# fragment or drop-in created and removed under an omitted directory would be
# read by the verifier while every anchor stayed unchanged, so anchoring a
# subset is a false PASS waiting to happen.
#
# Still not a filesystem-wide watch: this is exactly the set systemd itself
# resolves units from. `unit-paths` is read-only. If it cannot be asked, the
# static fallback is used AND the evidence says so, so a narrowed anchor set
# is visible to the certifier rather than silent.
SEARCH_PATH_SOURCE="systemd-analyze unit-paths"
SEARCH_PATH="${STOCKBOT_UNIT_SEARCH_PATH:-}"
if [ -z "$SEARCH_PATH" ]; then
  SEARCH_PATH=$(systemd-analyze unit-paths 2>/dev/null | tr '\n' ' ')
  if [ -z "$SEARCH_PATH" ]; then
    SEARCH_PATH_SOURCE="static fallback (unit-paths unavailable)"
    SEARCH_PATH="/etc/systemd/system.control /run/systemd/system.control \
/run/systemd/transient /run/systemd/generator.early /etc/systemd/system \
/etc/systemd/system.attached /run/systemd/system /run/systemd/system.attached \
/run/systemd/generator /usr/local/lib/systemd/system /usr/lib/systemd/system \
/run/systemd/generator.late"
  fi
else
  SEARCH_PATH_SOURCE="STOCKBOT_UNIT_SEARCH_PATH override"
fi

_release_state() {
  local resolved sha
  resolved=$(readlink -f "$RELEASE_POINTER" 2>/dev/null)
  if [ -z "$resolved" ]; then echo "unreadable"; return; fi
  sha=$(git -C "$resolved" rev-parse HEAD 2>/dev/null)
  if [ -z "$sha" ]; then echo "unreadable"; return; fi
  echo "$sha"
}

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
# Issued by the collection FLOW and passed in, never invented here. The whole
# purpose of the id is that the same value appears on the pointer and scheduler
# evidence gathered in the same run, so a value this script made up could only
# ever match itself. Absent means the three-gate aggregate cannot bind the
# evidence together and must report NOT_ESTABLISHED -- which is the correct
# outcome, not a defect to be papered over with a generated default.
echo "##OBSERVATION_ID"
echo "${STOCKBOT_OBSERVATION_ID:-}"
echo "##CHECKED_AT"
date -u +%Y-%m-%dT%H:%M:%SZ

# Which release this host was running when the evidence was taken. A shared
# observation id proves the collectors were TOLD they belong to one run; it
# cannot prove the system held still during it. If a deployment lands between
# the pointer gate's reading and this one, both still carry the same id while
# describing different releases. The release the collector actually observed is
# therefore recorded, bracketing the whole run, and the aggregate compares it
# against what the pointer gate certified. Both commands are read-only.
echo "##RELEASE_POINTER_BEFORE"
_release_state
echo "##SEARCH_PATH_SOURCE"
echo "$SEARCH_PATH_SOURCE"
echo "##SEARCH_PATH"
echo "$SEARCH_PATH"
# What systemd says the load path REALLY is, recorded even when an override
# narrowed what the anchors observed. An override changes only which
# directories `_diranchor` looks at -- it cannot constrain where the verifier
# actually resolves units from, so an anchor set narrower than the effective
# path is a blind spot, not a configuration choice. Recording both lets the
# certifier refuse rather than trust a capture that could not have seen a
# transient drop-in under a directory it never looked at.
echo "##SEARCH_PATH_EFFECTIVE"
systemd-analyze unit-paths 2>/dev/null | tr '\n' ' '
echo
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

# The effective on-disk configuration is DIGESTED as well as described, so a
# change during this run is detectable afterwards. `systemctl show` alone
# cannot establish that: NeedDaemonReload compares loaded state to disk at the
# instant it is asked, so a unit rewritten AND reloaded inside the collection
# window reports `no` at both ends while the verifier read bytes PID 1 never
# had loaded. Hashing the fragment and its drop-ins pins WHICH bytes the
# verifier saw. Both sha256sum and `systemctl show` are read-only.
_digest() {
  local paths=() p out rc
  for p in "$@"; do
    if [ -n "$p" ]; then paths+=("$p"); fi
  done
  if [ "${#paths[@]}" -eq 0 ]; then echo "none"; return; fi
  out=$(sha256sum "${paths[@]}" 2>/dev/null); rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then echo "unreadable"; return; fi
  # Hash of the per-file hashes AND their paths, so a drop-in appearing,
  # vanishing or being reordered changes the value too.
  printf '%s\n' "$out" | sha256sum | awk '{print $1}'
}

# Content alone cannot see a change that RETURNS. A deployment that moves a
# unit A -> B and is rolled back to A before the re-observation leaves both
# endpoint digests equal to A while the verifier actually read B. So each
# observation also records inode, size and nanosecond mtime/ctime: restoring
# identical bytes still rewrites the file, and ctime in particular cannot be
# moved backwards by an ordinary write. Measured locally -- rewriting a file
# with identical content keeps the inode but advances mtime and ctime.
_statsig() {
  local paths=() p out rc
  for p in "$@"; do
    if [ -n "$p" ]; then paths+=("$p"); fi
  done
  if [ "${#paths[@]}" -eq 0 ]; then echo "none"; return; fi
  out=$(stat -c '%n|%i|%s|%.9Y|%.9Z' "${paths[@]}" 2>/dev/null); rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then echo "unreadable"; return; fi
  printf '%s\n' "$out" | sha256sum | awk '{print $1}'
}

# The per-file anchors above cover bytes that CHANGE. They are structurally
# blind to a file that APPEARS AND DISAPPEARS: a drop-in added before
# verification and removed before the re-observation is absent from
# DropInPaths at BOTH ends, so its digest and its stat signature each read
# "none" twice -- while `systemd-analyze verify`, which reads the search path
# from DISK rather than from the loaded manager state, demonstrably parsed it.
# Measured on systemd 255: the verifier reports the transient drop-in's
# contents while every per-file anchor is byte-identical across the window.
#
# What witnesses that is the DIRECTORY. Adding or removing an entry advances
# the containing directory's mtime and ctime, so anchoring the unit search
# directories and each unit's drop-in directory closes the case the per-file
# anchors structurally cannot. A path that does not exist is recorded as
# "absent" rather than skipped, so a directory that appears and vanishes
# cannot read as unchanged either. `stat` is read-only.
_diranchor() {
  local u="$1" d p out=""
  for d in $SEARCH_PATH; do
    for p in "$d" "$d/$u.d"; do
      if [ -e "$p" ]; then
        out="$out$(stat -c '%n|%i|%.9Y|%.9Z' "$p" 2>/dev/null || echo "$p|unreadable")
"
      else
        out="$out$p|absent
"
      fi
    done
  done
  printf '%s' "$out" | sha256sum | awk '{print $1}'
}

# One observation of a unit's loaded state and of the bytes behind it. Emitted
# once before verification and once after; the certifier requires them to
# agree before it will treat the verifier's result as describing the running
# configuration.
_snapshot() {
  local u="$1" frag drops
  systemctl show "$u" \
    -p Id -p LoadState -p FragmentPath -p DropInPaths \
    -p NeedDaemonReload -p LoadError --no-pager 2>/dev/null
  frag=$(systemctl show "$u" -p FragmentPath --value --no-pager 2>/dev/null)
  drops=$(systemctl show "$u" -p DropInPaths --value --no-pager 2>/dev/null)
  # Synthetic records, deliberately in the same KEY=VALUE shape the reader
  # already parses. Prefixed so they cannot collide with a real systemd
  # property now or in a future version.
  echo "NorthstarFragmentDigest=$(_digest "$frag")"
  # Unquoted on purpose: DropInPaths is a space-separated list.
  echo "NorthstarDropInDigest=$(_digest $drops)"
  echo "NorthstarFragmentStat=$(_statsig "$frag")"
  echo "NorthstarDropInStat=$(_statsig $drops)"
  echo "NorthstarSearchPathAnchor=$(_diranchor "$u")"
}

for u in $UNITS; do
  echo "##SHOW $u"
  _snapshot "$u"
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

# Re-observe AFTER verification. `systemd-analyze verify` reads unit files on
# DISK; this pass is what establishes that those files, and their relationship
# to what PID 1 has loaded, did not change while it ran. Without it the stream
# can pair a NeedDaemonReload captured before a concurrent deployment with a
# clean verifier result produced after it, and so certify a configuration that
# is not the one running. A change here must make the run NOT_CERTIFIABLE --
# never a PASS, and never a daemon-reload to tidy it up.
for u in $UNITS; do
  echo "##RECHECK $u"
  _snapshot "$u"
done

echo "##RELEASE_POINTER_AFTER"
_release_state

echo "##END"
