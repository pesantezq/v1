#!/bin/bash
# Deploy the version-controlled R&D sandbox runtime into StockBot-Agent-Lab.
# Usage (as root, in Agent-Lab):  install.sh <source_dir>
# where <source_dir> is a copy of ops/agent_lab/ from the repo. Installs the
# runtime, (re)generates the installed-hash manifest, and enables services.
set -e
SRC="${1:-.}"
install -m 755 "$SRC/rdsbx-offline-up.sh"        /usr/local/sbin/rdsbx-offline-up
install -m 755 "$SRC/sandbox-run.sh"             /usr/local/sbin/sandbox-run
install -m 755 "$SRC/ollama_inference_proxy.py"  /usr/local/sbin/rd-ollama-inference-proxy
install -m 644 "$SRC/systemd/rdsbx-offline.service"            /etc/systemd/system/rdsbx-offline.service
install -m 644 "$SRC/systemd/rd-ollama-inference-proxy.service" /etc/systemd/system/rd-ollama-inference-proxy.service
systemctl daemon-reload
systemctl enable --now rdsbx-offline.service
systemctl enable --now rd-ollama-inference-proxy.service
echo "installed runtime hashes:"
sha256sum /usr/local/sbin/rdsbx-offline-up /usr/local/sbin/sandbox-run \
          /usr/local/sbin/rd-ollama-inference-proxy \
          /etc/systemd/system/rdsbx-offline.service \
          /etc/systemd/system/rd-ollama-inference-proxy.service
echo "INSTALL_DONE"
