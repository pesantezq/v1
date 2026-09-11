#!/bin/bash
#
# One bracketed, strictly read-only evidence collection spanning all three
# release-identity gates.
#
# Why this exists
# ---------------
# The three gates are deliberately independent, and each reads something
# different: the pointer gate reads the release pointer, the scheduler gate
# reads unit ExecStart text and cron, the validity gate reads unit files
# through systemd-analyze. A shared observation id proves the collectors were
# TOLD they belong to one run; it cannot prove the system held still during it.
#
# Unit files under /etc/systemd/system are not part of the release, so binding
# the gates to a release SHA leaves a hole:
#
#     scheduler evidence collected   unit A is aligned
#     fragment replaced              unit B, valid but NOT aligned
#     validity evidence collected    unit B verifies
#     pointer SHA                    unchanged throughout
#                                    -> three green gates, and B is installed
#
# So this flow brackets the WHOLE collection. It measures the mutation
# witnesses of every configuration input BEFORE the first gate is collected and
# AFTER the last one completes. Equal endpoints mean nothing the gates depend on
# moved between them.
#
# The anchors are owned HERE, by the collection flow -- not by whoever calls
# the aggregate. A caller who could supply an anchor could supply agreement,
# which is the thing being checked.
#
# What it claims
# --------------
# Not a filesystem snapshot and not a transactional one. It establishes that
# the measured witnesses -- inode, size, nanosecond mtime/ctime, and the
# directory entries of every applicable unit load and drop-in directory, plus
# the release pointer -- read identically at both endpoints. Those witnesses
# are not restorable by ordinary means: no userspace API sets ctime, a rename
# changes the inode, and the kernel stamps a directory on link/unlink.
#
# It does NOT defend against a privileged actor forging evidence, moving the
# system clock, or mutating state through a path outside the measured set.
#
# Strictly read-only. Every command here observes: systemd-analyze, systemctl
# show/list-unit-files, crontab -l, sha256sum, stat, readlink -f, git rev-parse.
# Nothing reloads, starts, stops, enables, masks or writes, and a test asserts
# it.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_POINTER="${STOCKBOT_RELEASE_POINTER:-/opt/stockbot/current}"
RELEASES_ROOT="${STOCKBOT_RELEASES_ROOT:-/opt/stockbot/releases}"

UNITS="${STOCKBOT_EXPECTED_UNITS:-\
stockbot-streamlit.service \
stockbot-dashboard.service \
stockbot-daily.service \
stockbot-daily.timer}"

# The effective unit load path, asked of systemd rather than assumed. See
# collect_systemd_validity_evidence.sh for why a hardcoded subset is a false
# PASS waiting to happen.
SEARCH_PATH="${STOCKBOT_UNIT_SEARCH_PATH:-}"
if [ -z "$SEARCH_PATH" ]; then
  SEARCH_PATH=$(systemd-analyze unit-paths 2>/dev/null | tr '\n' ' ')
fi
if [ -z "$SEARCH_PATH" ]; then
  SEARCH_PATH="/etc/systemd/system.control /run/systemd/system.control \
/run/systemd/transient /run/systemd/generator.early /etc/systemd/system \
/etc/systemd/system.attached /run/systemd/system /run/systemd/system.attached \
/run/systemd/generator /usr/local/lib/systemd/system /usr/lib/systemd/system \
/run/systemd/generator.late"
fi

# Every drop-in directory systemd would apply to a unit -- the type directory,
# the exact one, each dash-truncated prefix, and the template form. Measured on
# systemd 255; `-.service.d/` was NOT applied and so is not generated.
_dropin_dirs() {
  local u="$1" type="${1##*.}" stem="${1%.*}" prefix
  echo "$type.d"
  echo "$u.d"
  prefix="$stem"
  while [ "${prefix%-*}" != "$prefix" ]; do
    prefix="${prefix%-*}"
    [ -n "$prefix" ] && echo "$prefix-.$type.d"
  done
  case "$stem" in *@*) echo "${stem%%@*}@.$type.d";; esac
}

# The configuration anchor: one digest over every mutable input the three gates
# depend on. A path that does not exist is recorded as "absent" rather than
# skipped, so something that appears and vanishes cannot read as unchanged.
_configuration_anchor() {
  local out="" d u p sub frag drops
  # the release pointer itself, and what it resolves to
  if [ -L "$RELEASE_POINTER" ] || [ -e "$RELEASE_POINTER" ]; then
    out="$out$(stat -c '%n|%i|%s|%.9Y|%.9Z' "$RELEASE_POINTER" 2>/dev/null)
"
    out="$out pointer_target=$(readlink -f "$RELEASE_POINTER" 2>/dev/null)
"
    out="$out pointer_sha=$(git -C "$(readlink -f "$RELEASE_POINTER" 2>/dev/null)" \
          rev-parse HEAD 2>/dev/null)
"
  else
    out="$out$RELEASE_POINTER|absent
"
  fi
  # every unit load directory and every applicable drop-in directory
  for d in $SEARCH_PATH; do
    for u in $UNITS; do
      for sub in $(_dropin_dirs "$u"); do
        p="$d/$sub"
        if [ -e "$p" ]; then
          out="$out$(stat -c '%n|%i|%.9Y|%.9Z' "$p" 2>/dev/null)
"
        else
          out="$out$p|absent
"
        fi
      done
    done
    if [ -e "$d" ]; then
      out="$out$(stat -c '%n|%i|%.9Y|%.9Z' "$d" 2>/dev/null)
"
    else
      out="$out$d|absent
"
    fi
  done
  # the unit files themselves, and the drop-ins currently applied to them
  for u in $UNITS; do
    frag=$(systemctl show "$u" -p FragmentPath --value --no-pager 2>/dev/null)
    drops=$(systemctl show "$u" -p DropInPaths --value --no-pager 2>/dev/null)
    for p in "$frag" $drops; do
      [ -n "$p" ] || continue
      if [ -e "$p" ]; then
        out="$out$(stat -c '%n|%i|%s|%.9Y|%.9Z' "$p" 2>/dev/null)
"
        out="$out$(sha256sum "$p" 2>/dev/null)
"
      else
        out="$out$p|absent
"
      fi
    done
  done
  # cron is a scheduler input too
  out="$out crontab=$(crontab -l 2>/dev/null | sha256sum | awk '{print $1}')
"
  printf '%s' "$out" | sha256sum | awk '{print $1}'
}

# ---------------------------------------------------------------------------
# A. one host, one observation id
# ---------------------------------------------------------------------------
echo "##HOST"
hostname
echo "##OBSERVATION_ID"
echo "${STOCKBOT_OBSERVATION_ID:-}"
echo "##CHECKED_AT"
date -u +%Y-%m-%dT%H:%M:%SZ

# ---------------------------------------------------------------------------
# B. capture the bracket BEFORE any gate evidence is acquired
# ---------------------------------------------------------------------------
echo "##CONFIGURATION_ANCHOR_BEFORE"
_configuration_anchor

# ---------------------------------------------------------------------------
# C. scheduler alignment evidence
# ---------------------------------------------------------------------------
echo "##RELEASE_ROOT"
echo "$RELEASE_POINTER"
echo "##RELEASES_ROOT"
echo "$RELEASES_ROOT"
for u in $UNITS; do
  echo "##SCHEDULER_UNIT $u"
  # ExecStart and the directives that decide which code actually runs.
  systemctl show "$u" \
    -p Id -p ExecStart -p WorkingDirectory -p RootDirectory \
    -p EnvironmentFiles -p LoadState --no-pager 2>/dev/null
done
echo "##SCHEDULER_CRON"
crontab -l 2>/dev/null

# ---------------------------------------------------------------------------
# D. release pointer identity evidence
# ---------------------------------------------------------------------------
echo "##POINTER_PATH"
echo "$RELEASE_POINTER"
echo "##POINTER_IS_SYMLINK"
if [ -L "$RELEASE_POINTER" ]; then echo yes; else echo no; fi
echo "##POINTER_RESOLVED"
readlink -f "$RELEASE_POINTER" 2>/dev/null
echo "##POINTER_SHA"
git -C "$(readlink -f "$RELEASE_POINTER" 2>/dev/null)" rev-parse HEAD 2>/dev/null
echo "##POINTER_DIRTY"
if git -C "$(readlink -f "$RELEASE_POINTER" 2>/dev/null)" \
     diff --quiet HEAD 2>/dev/null; then echo no; else echo yes; fi

# ---------------------------------------------------------------------------
# E. systemd unit validity evidence -- the existing collector, reused rather
#    than reimplemented, so the two flows cannot drift apart.
# ---------------------------------------------------------------------------
echo "##VALIDITY_BEGIN"
STOCKBOT_EXPECTED_UNITS="$UNITS" \
STOCKBOT_OBSERVATION_ID="${STOCKBOT_OBSERVATION_ID:-}" \
STOCKBOT_RELEASE_POINTER="$RELEASE_POINTER" \
  bash "$HERE/collect_systemd_validity_evidence.sh"
echo "##VALIDITY_END"

# ---------------------------------------------------------------------------
# F. close the bracket AFTER every gate observation is complete
# ---------------------------------------------------------------------------
echo "##CONFIGURATION_ANCHOR_AFTER"
_configuration_anchor

echo "##OBSERVATION_END"
