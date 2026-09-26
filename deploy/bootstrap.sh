#!/usr/bin/env bash
# Set up the Lemnisca MCP server on a fresh Ubuntu 24.04 host.
#   sudo PUBLIC_HOST=1-2-3-4.sslip.io bash bootstrap.sh /tmp/lemkb-bundle
# The bundle directory holds: code.tar.gz, server.env
# SKIP_CADDY=1 / SKIP_SYSTEMD=1 are for rehearsing inside a container.
set -euo pipefail
BUNDLE="${1:?bundle directory}"
: "${PUBLIC_HOST:?set PUBLIC_HOST, e.g. 13-201-5-9.sslip.io}"
APP=/opt/lemkb

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null

id lemkb >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin lemkb
mkdir -p "$APP"
tar -xzf "$BUNDLE/code.tar.gz" -C "$APP"
install -m 600 "$BUNDLE/server.env" "$APP/.env"
rm -f "$APP/mcp_tokens.json"   # users now live in the database (mcp_users)
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/deploy/requirements-mcp.txt"
chown -R lemkb:lemkb "$APP"

if [ "${SKIP_SYSTEMD:-0}" != "1" ]; then
cat > /etc/systemd/system/lemkb-mcp.service <<UNIT
[Unit]
Description=Lemnisca Media KB MCP server
After=network-online.target
Wants=network-online.target

[Service]
User=lemkb
WorkingDirectory=$APP
Environment=PYTHONPATH=$APP/src
Environment=PYTHONIOENCODING=utf-8
Environment=MPLCONFIGDIR=/tmp/lemkb-mpl
ExecStart=$APP/.venv/bin/python -m lemkb.mcp_http --host 127.0.0.1 --port 8765 --public-host $PUBLIC_HOST
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/tmp

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable lemkb-mcp && systemctl restart lemkb-mcp   # restart so a redeploy runs the new code
fi

if [ "${SKIP_CADDY:-0}" != "1" ]; then
  # Caddy terminates HTTPS with an automatic Let's Encrypt certificate for PUBLIC_HOST.
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  cat > /etc/caddy/Caddyfile <<CADDY
$PUBLIC_HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1
    }
}
CADDY
  systemctl reload caddy || systemctl restart caddy
fi
echo "bootstrap complete for https://$PUBLIC_HOST"
