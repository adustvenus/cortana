#!/usr/bin/env bash
# Cortana Mobile Bridge installer. Run from inside cortana/: bash bridge/install-bridge.sh
set -e
cd "$(dirname "$0")/.."

./venv/bin/pip install -r requirements.txt   # picks up aiohttp

mkdir -p ~/.config/systemd/user
cp cortana-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cortana-bridge

echo ""
echo "=== Bridge running. Next: ==="
echo "1) Install Tailscale on this machine and your phone (https://tailscale.com)"
echo "2) Add the MOBILE LINK module on the Dusk dashboard (edit mode -> tray)"
echo "3) In the Cortana Mobile app: enter this machine's Tailscale IP/name,"
echo "   port ${BRIDGE_PORT:-8765}, and the pairing code shown on the dashboard."
echo "Status:  systemctl --user status cortana-bridge"
echo "Logs:    journalctl --user -u cortana-bridge -f"
