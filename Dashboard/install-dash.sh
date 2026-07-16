#!/usr/bin/env bash
# Dusk Dashboard installer. Run from anywhere: bash Dashboard/install-dash.sh
# Needs network ONCE (npm fetches Electron). Runtime is fully offline.
set -e
cd "$(dirname "$0")"

echo "[1/4] Installing Electron (one-time download)..."
( cd app && npm install --no-audit --no-fund )

echo "[2/4] Installing application icon + desktop entry..."
mkdir -p ~/.local/share/applications
sed "s|__HOME__|$HOME|g" dusk-dash.desktop > ~/.local/share/applications/dusk-dash.desktop
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database ~/.local/share/applications || true

echo "[3/4] Installing systemd user service (start at boot)..."
mkdir -p ~/.config/systemd/user
cp cortana-dash.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable cortana-dash

echo "[4/4] Done. Start now with:  systemctl --user start cortana-dash"
echo "Or find 'Dusk Dashboard' in your application menu."
echo "Esc or minimize = shrink to the floating bubble. Right-click bubble = quit."
