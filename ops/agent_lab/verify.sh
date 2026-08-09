#!/bin/bash
# Verify the INSTALLED sandbox runtime matches the version-controlled source,
# binding git commit -> running security boundary. Usage: verify.sh <source_dir>
# Compares sha256 of installed files to freshly-computed source hashes.
set -e
SRC="${1:-.}"
declare -A MAP=(
  ["$SRC/rdsbx-offline-up.sh"]="/usr/local/sbin/rdsbx-offline-up"
  ["$SRC/sandbox-run.sh"]="/usr/local/sbin/sandbox-run"
  ["$SRC/ollama_inference_proxy.py"]="/usr/local/sbin/rd-ollama-inference-proxy"
  ["$SRC/systemd/rdsbx-offline.service"]="/etc/systemd/system/rdsbx-offline.service"
  ["$SRC/systemd/rd-ollama-inference-proxy.service"]="/etc/systemd/system/rd-ollama-inference-proxy.service"
)
rc=0
for s in "${!MAP[@]}"; do
  d="${MAP[$s]}"
  sh=$(sha256sum "$s" 2>/dev/null | cut -d' ' -f1)
  dh=$(sha256sum "$d" 2>/dev/null | cut -d' ' -f1)
  if [ -n "$sh" ] && [ "$sh" = "$dh" ]; then
    echo "OK   $(basename "$d")  $sh"
  else
    echo "FAIL $(basename "$d")  src=$sh installed=$dh"; rc=1
  fi
done
[ $rc -eq 0 ] && echo "VERIFY_OK (installed runtime == committed source)" || echo "VERIFY_FAIL"
exit $rc
