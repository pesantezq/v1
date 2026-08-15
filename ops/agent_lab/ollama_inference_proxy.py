#!/usr/bin/env python3
"""Inference-only Ollama proxy for the R&D sandbox (Phase 0B hardening, P0.4).

Runs in the TRUSTED (main) network namespace, listening on the veth address the
sandboxed worker can reach. Forwards ONLY inference + read-only availability
requests to the local Ollama daemon (127.0.0.1:11434) and refuses all
model-management / mutation endpoints. Bounds request and response size.

The worker's OFFLINE_LOCAL firewall permits ONLY this proxy address:port, so raw
Ollama (11434, full API incl. pull/push/delete) is never reachable by the worker.

Allowlist (method + exact path):
    POST /api/generate, /api/chat, /api/embed, /api/embeddings   (inference)
    GET  /api/version, /api/tags, /api/ps                        (read-only availability)
Everything else -> 403 (including pull/push/create/copy/delete/blobs).

No external network, no auth material, no state. Stdlib only.
"""
from __future__ import annotations

import http.server
import socket
import sys
import urllib.request
import urllib.error

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 11434
MAX_REQ_BYTES = 1 << 20          # 1 MiB request cap
MAX_RESP_BYTES = 16 << 20        # 16 MiB response cap
UPSTREAM_TIMEOUT = 120

_ALLOW = {
    ("POST", "/api/generate"), ("POST", "/api/chat"),
    ("POST", "/api/embed"), ("POST", "/api/embeddings"),
    ("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rd-ollama-proxy/1"

    def _deny(self, code: int, msg: str) -> None:
        body = ("{\"error\":\"" + msg + "\"}").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _proxy(self, method: str) -> None:
        path = self._path()
        if (method, path) not in _ALLOW:
            self._deny(403, "endpoint not permitted (inference-only proxy)")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQ_BYTES:
            self._deny(413, "request too large")
            return
        data = self.rfile.read(length) if length else None
        url = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}{self.path}"
        req = urllib.request.Request(url, data=data, method=method)
        ct = self.headers.get("Content-Type")
        if ct:
            req.add_header("Content-Type", ct)
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RESP_BYTES:
                        break
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")
        except urllib.error.HTTPError as e:
            self._deny(e.code, "upstream error")
        except Exception:
            self._deny(502, "upstream unavailable")

    def do_GET(self) -> None:
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")

    # Refuse every other method explicitly.
    def do_PUT(self): self._deny(403, "method not permitted")
    def do_DELETE(self): self._deny(403, "method not permitted")
    def do_PATCH(self): self._deny(403, "method not permitted")
    def do_HEAD(self): self._deny(403, "method not permitted")

    def log_message(self, *a):  # quiet
        pass


def main() -> int:
    bind = sys.argv[1] if len(sys.argv) > 1 else "10.200.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 11435
    httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
