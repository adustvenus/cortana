#!/usr/bin/env bash
# Cortana Mobile Bridge installer. Run from inside cortana/: bash bridge/install-bridge.sh
set -e
cd "$(dirname "$0")/.."

# Find the Python env Cortana actually uses - installs have used different
# folder names over time (venv, cortana_venv, .venv). Create ./venv if none.
PY=""
for d in venv cortana_venv .venv; do
  if [ -x "$d/bin/python" ]; then PY="$PWD/$d/bin/python"; break; fi
done
if [ -z "$PY" ]; then
  echo "No virtualenv found - creating ./venv"
  python3 -m venv venv
  PY="$PWD/venv/bin/python"
fi
echo "Using $PY"
"$PY" -m pip install -r requirements.txt   # picks up aiohttp

mkdir -p ~/.config/systemd/user
# Point the unit at the resolved interpreter + this checkout (the committed
# unit assumes ~/cortana/venv, which isn't true on every machine).
sed -e "s|^ExecStart=.*|ExecStart=$PY -m bridge.server|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$PWD|" \
    cortana-bridge.service > ~/.config/systemd/user/cortana-bridge.service
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
