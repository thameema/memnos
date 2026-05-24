# Remote Deployment Guide

This guide covers deploying engram on a remote server (VPS, cloud VM, or home server) so it is accessible from Claude Code on any machine — including your laptop, desktop, or mobile via the Telegram/WhatsApp gateway.

## Architecture overview

```
Your laptop (Claude Code)
    │
    │  HTTPS/SSE  (port 443 or 8765)
    ▼
Remote server (engram)
    ├── engram Python server  (port 8766 REST, 8765 MCP/SSE)
    └── ArcadeDB              (port 2480, internal only)
```

ArcadeDB should **not** be exposed externally — only the engram API ports (8765 and 8766) need to be reachable.

---

## Option 1 — VPS / cloud VM (recommended)

Tested on Ubuntu 22.04 and Debian 12. Any Linux with Docker and Python 3.11+ will work.

### 1. Provision a server

Minimum specs:
- 1 vCPU, 2 GB RAM
- 20 GB SSD
- Ubuntu 22.04 LTS

Providers that work well: DigitalOcean, Hetzner, Vultr, AWS EC2 t3.small, Azure B2s.

### 2. Open firewall ports

Open ports 8765 and 8766 (or 443 if you put engram behind nginx/TLS). Keep port 2480 **closed** to the public.

```bash
# Ubuntu UFW example
sudo ufw allow 22/tcp
sudo ufw allow 8765/tcp
sudo ufw allow 8766/tcp
sudo ufw enable
```

### 3. Install engram

SSH into your server and run the one-command installer:

```bash
curl -fsSL https://raw.githubusercontent.com/thameema/engram/main/install.sh | bash
```

The installer will detect Linux and set up Docker, install packages, and create `~/.engram/`.

### 4. Edit `~/.engram/engram.yaml`

Change the server bind address so it listens on all interfaces:

```yaml
server:
  host: "0.0.0.0"
  api_port: 8766
  mcp_port: 8765
  log_level: INFO

auth:
  api_keys:
    - key: "your-strong-secret-key-here"
      user_id: "me"
      namespaces: ["*"]
```

Generate a strong key:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 5. Start engram as a systemd service

```bash
sudo tee /etc/systemd/system/engram.service > /dev/null << EOF
[Unit]
Description=engram memory server
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/.engram
ExecStart=$HOME/.local/bin/engram start
ExecStop=$HOME/.local/bin/engram stop
Restart=on-failure
RestartSec=15
Environment=ENGRAM_CONFIG=$HOME/.engram/engram.yaml
StandardOutput=append:$HOME/.engram/logs/engram.log
StandardError=append:$HOME/.engram/logs/engram.err

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable engram
sudo systemctl start engram
sudo systemctl status engram
```

### 6. Connect Claude Code on your laptop

In `~/.claude/settings.json` on your laptop, replace `localhost` with your server's IP or hostname:

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "http://YOUR_SERVER_IP:8765/sse",
      "headers": {
        "Authorization": "Bearer your-strong-secret-key-here"
      }
    }
  }
}
```

Restart Claude Code, then run `/mcp` to confirm the connection.

---

## Option 2 — TLS with nginx reverse proxy

Running engram behind nginx with a Let's Encrypt certificate means Claude Code connects over HTTPS instead of plain HTTP. This is recommended if your server is internet-facing.

### nginx config

```nginx
server {
    listen 443 ssl;
    server_name engram.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/engram.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/engram.yourdomain.com/privkey.pem;

    # MCP SSE endpoint — SSE requires buffering disabled
    location /sse {
        proxy_pass         http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header   Connection "";
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
    }

    # MCP message POST and REST API
    location / {
        proxy_pass       http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name engram.yourdomain.com;
    return 301 https://$host$request_uri;
}
```

Get a certificate with Certbot:
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d engram.yourdomain.com
```

Then in `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "https://engram.yourdomain.com/sse",
      "headers": {
        "Authorization": "Bearer your-strong-secret-key-here"
      }
    }
  }
}
```

---

## Option 3 — Tailscale (zero-config private network)

If you do not want to open firewall ports at all, Tailscale creates a private WireGuard network between your machines. No public IP, no port forwarding, no nginx needed.

```bash
# On the server
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# On your laptop (if not already on Tailscale)
# Install Tailscale, sign into the same account

# Get the server's Tailscale IP
tailscale ip -4
```

Then in `~/.claude/settings.json` on your laptop, use the Tailscale IP:
```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "http://100.x.x.x:8765/sse",
      "headers": {
        "Authorization": "Bearer your-strong-secret-key-here"
      }
    }
  }
}
```

This is the simplest setup for personal use or small teams.

---

## Backup and persistence

All data lives in ArcadeDB. The recommended approach is the included backup script:

```bash
# Uses tools/backup.sh — stops, rsyncs, restarts; keeps last 7 backups
bash tools/backup.sh --verify
```

Manual backup locations:

| Data | Location | Notes |
|------|----------|-------|
| ArcadeDB graph + vectors | `ENGRAM_DATA_DIR/arcadedb/` (Docker volume) | Stop ArcadeDB before copying |
| SQLite stores (learning, tasks) | `~/.engram/*.db` | Can copy while running |
| Config | `~/.engram/engram.yaml` | Keep in version control (without secrets) |

**Automated daily backup with cron:**

```bash
0 3 * * * cd ~/engram && bash tools/backup.sh >> ~/.engram/logs/backup.log 2>&1
```

---

## Multi-user teams

To give multiple team members access to a shared engram server, create an API key for each person with their own namespace:

```yaml
auth:
  api_keys:
    - key: "alice-secret-key"
      user_id: "alice"
      namespaces: ["personal:alice", "team:backend"]
    - key: "bob-secret-key"
      user_id: "bob"
      namespaces: ["personal:bob", "team:backend"]
    - key: "admin-secret-key"
      user_id: "admin"
      namespaces: ["*"]
```

Each person adds their own key to their `~/.claude/settings.json`. Memories written to `team:backend` are visible to both Alice and Bob. Personal namespaces are private.

---

## Health check

```bash
# Basic liveness
curl -s http://YOUR_SERVER:8766/api/v1/admin/health

# API health with auth
curl -s -H "Authorization: Bearer your-key" \
  http://YOUR_SERVER:8766/api/v1/admin/health | jq

# MCP tools list
curl -s -H "Authorization: Bearer your-key" \
  http://YOUR_SERVER:8765/health | jq
```

---

## Troubleshooting

**MCP SSE connection drops after ~60 seconds**
Increase the proxy read timeout (see nginx config above). Some load balancers kill idle SSE connections. Set `proxy_read_timeout 3600s`.

**Claude Code shows "engram: disconnected" in /mcp**
1. Check `engram status` on the server
2. Confirm the port is reachable: `curl http://YOUR_SERVER:8766/api/v1/admin/health`
3. Verify the API key in `~/.claude/settings.json` matches `engram.yaml`
4. Check logs: `engram logs`

**ArcadeDB out of memory**
Add JVM heap limits to `docker-compose.yml` under the `arcadedb` service:
```yaml
environment:
  JAVA_OPTS: "-Darcadedb.server.rootPassword=${ARCADEDB_PASSWORD:-engram} -Xmx1g"
```

**Embeddings slow on first search after restart**
The numpy cosine similarity layer caches all embeddings in memory with a 5-minute TTL. The first search after a restart loads all Memory records from ArcadeDB (~1s for 648 records). Subsequent searches within the TTL window are ~1ms. This is expected behaviour — no action needed.
