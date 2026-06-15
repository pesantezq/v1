# Dashboard Hosting — Cloudflare Tunnel

The gui_v2 operator dashboard (`stockbot-dashboard.service`, uvicorn) is published
on the public internet at **https://dashboard.portfolio-ops-center.com** via a
**Cloudflare named tunnel** — no inbound ports are opened on the VPS, the origin IP
stays hidden, and TLS is terminated at the Cloudflare edge with an automatic,
auto-renewing certificate (nothing to manage or renew on the box).

```
browser ──HTTPS──▶ Cloudflare edge (auto cert) ──tunnel──▶ 127.0.0.1:8502 (uvicorn)
```

## Components

| Piece | Value |
|---|---|
| Public URL | `https://dashboard.portfolio-ops-center.com` (auth-gated by gui_v2 `_require_auth`) |
| Cloudflare zone | `portfolio-ops-center.com` (DNS managed in Cloudflare) |
| Tunnel | `stockbot-reauth` (UUID `47c23adf-2ca0-49b1-bb88-a1b799448205`) — shared with the Schwab work |
| Tunnel config | `/root/.cloudflared/config.yml` |
| Tunnel creds | `/root/.cloudflared/47c23adf-…json`; account cert `/root/.cloudflared/cert.pem` |
| Tunnel service | `cloudflared-stockbot.service` (systemd, runs as root, `Restart=on-failure`) |
| Origin bind | uvicorn `--host 127.0.0.1 --port 8502` (localhost-only; not publicly reachable) |

`/root/.cloudflared/config.yml`:

```yaml
tunnel: 47c23adf-2ca0-49b1-bb88-a1b799448205
credentials-file: /root/.cloudflared/47c23adf-2ca0-49b1-bb88-a1b799448205.json
ingress:
  - hostname: dashboard.portfolio-ops-center.com
    service: http://127.0.0.1:8502
  - service: http_status:404
```

## Why a tunnel (vs. A-record + cert)

- **DNS alone** (an A record → the VPS IP) makes the dashboard reachable by name but
  only over plain HTTP — unacceptable for a portfolio/broker dashboard behind a login.
- **Reverse proxy + Let's Encrypt** (Caddy/nginx) would work but requires opening
  ports 80/443 and exposing the VPS IP.
- **Cloudflare tunnel** needs neither: no open ports, IP hidden, free auto cert, and it
  reuses the cloudflared already installed for the Schwab OAuth callback.

## Operations

**Initial setup (one-time, already done 2026-06-15):**
```bash
# cloudflared was already authenticated (cert.pem present) — do NOT re-run `tunnel login`.
# config.yml written; DNS route created:
cloudflared tunnel route dns stockbot-reauth dashboard.portfolio-ops-center.com
```

**Persistent service:**
```bash
sudo systemctl enable --now cloudflared-stockbot.service
sudo systemctl status cloudflared-stockbot.service
```

**Add / change a hostname:** edit the `ingress:` list in `config.yml`, then
`cloudflared tunnel route dns stockbot-reauth <new-host>` and
`sudo systemctl restart cloudflared-stockbot.service`.

**Verify:** `curl -sI https://dashboard.portfolio-ops-center.com/` → expect a `302`
(auth redirect) and `server: cloudflare`. A `530` means the tunnel/origin is down
(check `cloudflared-stockbot.service` and that uvicorn is listening on `127.0.0.1:8502`).

## Security

- The dashboard keeps its own auth gate (`_require_auth`). It is now internet-reachable
  by name, so optionally add a **Cloudflare Access** policy (Zero Trust → Access →
  Applications) on the hostname to require email/SSO before the page loads — zero code.
- The origin is bound to `127.0.0.1`, so `46.224.25.135:8502` is no longer publicly
  reachable; the tunnel is the only path in.

## Schwab callback (separate)

`stockbot.portfolio-ops-center.com` (the `SCHWAB_REDIRECT_URI` host) is intentionally
**not** routed by this dashboard config — it belongs to the Schwab re-auth flow. To make
the dashboard app also answer the Schwab callback, add a second `ingress` rule for that
hostname pointing at the handler's port. See `docs/schwab_integration.md`.
