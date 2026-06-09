# GUI Remote / Phone Access

## Principle: Never Expose the Dashboard Publicly

The StockBot Dashboard v2 listens on port 8502, bound to all interfaces
(`0.0.0.0`) by the systemd unit. **Do not open port 8502 to the public
internet without a protection layer.** The dashboard contains portfolio
positions, decision history, and signal metadata that should remain private.

---

## Recommended: Tailscale (Zero-Config VPN)

Tailscale creates an encrypted peer-to-peer VPN between your devices with no
firewall port-forwarding required.

**Setup (one-time):**

1. Install Tailscale on the VPS:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
2. Install the Tailscale app on your phone (iOS / Android).
3. Sign in to the same Tailscale account on both devices.

**Access:**

Once connected, navigate to `http://<tailscale-ip>:8502` in your phone browser.
The Tailscale IP appears in the Tailscale admin console or via `tailscale ip -4`
on the VPS.

Port 8502 does not need to be open in your VPS firewall because Tailscale traffic
is routed through its own encrypted tunnel.

**Why Tailscale is recommended:**
- No public port exposure — firewall stays closed.
- End-to-end encryption.
- Works through NAT and mobile carrier networks.
- No self-signed certificate required.
- One-command install; no nginx/Caddy proxy needed.

---

## Alternative: Cloudflare Tunnel + Access

Cloudflare Tunnel exposes the dashboard over HTTPS via Cloudflare's network
without opening any inbound firewall ports. Cloudflare Access adds an
authentication gate (email OTP, GitHub OAuth, etc.) in front of the tunnel.

**When to choose Cloudflare Tunnel:**
- You need HTTPS with a real certificate on a custom domain.
- You want to share the dashboard with a second person without adding them to a VPN.

**Setup:**

1. Install `cloudflared`:
   ```bash
   curl -L --output cloudflared.deb \
     https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
   sudo dpkg -i cloudflared.deb
   ```

2. Authenticate and create a tunnel:
   ```bash
   cloudflared login
   cloudflared tunnel create stockbot-dashboard
   ```

3. Create the tunnel config at `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: <tunnel-id>
   credentials-file: /root/.cloudflared/<tunnel-id>.json

   ingress:
     - hostname: dashboard.yourdomain.com
       service: http://localhost:8502
     - service: http_status:404
   ```

4. Start the tunnel (add to systemd for auto-start):
   ```bash
   cloudflared tunnel run stockbot-dashboard
   ```

5. Set up Cloudflare Access on the `dashboard.yourdomain.com` hostname in the
   Cloudflare Zero Trust dashboard to require authentication before the tunnel
   is reachable.

---

## Optional: HTTP Basic Auth (Built-in)

The dashboard has a built-in HTTP Basic Auth layer. When both `GUI_V2_AUTH_USER`
and `GUI_V2_AUTH_PASS` environment variables are set, every route requires matching
credentials. When either variable is absent, the dashboard is open (no auth
prompt).

**To enable:**

Add to `/opt/stockbot/.env`:
```
GUI_V2_AUTH_USER=operator
GUI_V2_AUTH_PASS=<strong-random-password>
```

Then restart the service:
```bash
sudo systemctl restart stockbot-dashboard
```

**Important:**
- Never hardcode credentials in source files or commit them to the repository.
- HTTP Basic Auth transmits credentials in Base64. Over plain HTTP it provides
  only minimal protection; always use it in conjunction with Tailscale or an
  HTTPS-terminating proxy (Cloudflare Tunnel, nginx with a certificate).
- Over Tailscale (which is end-to-end encrypted), HTTP Basic Auth adds a
  second layer — useful if the device is shared.

---

## Firewall Guidance

If using Tailscale or Cloudflare Tunnel, keep port 8502 **closed** in the VPS
firewall. Only port 22 (SSH) and whatever management ports you require need to
be open.

If you must expose the port directly (not recommended), restrict it to a known
IP range:
```bash
sudo ufw allow from <your-ip>/32 to any port 8502
```

---

## Never Hardcode Secrets

- Do not embed passwords, API keys, or tunnel tokens in any source file.
- All credentials go in `/opt/stockbot/.env` (not committed to git;
  `.gitignore` excludes it).
- Rotate `GUI_V2_AUTH_PASS` if it is ever exposed.

---

## Related Docs

- `docs/gui_usage.md` — dashboard overview and routes
- `docs/gui_mobile.md` — mobile browser layout
- `docs/gui_observe_only_safety.md` — observe-only model
