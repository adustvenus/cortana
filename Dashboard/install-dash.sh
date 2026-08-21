#!/usr/bin/env bash
# Dusk Dashboard installer. Run from anywhere: bash Dashboard/install-dash.sh
# Needs network ONCE (npm fetches Electron). Runtime is fully offline.
set -e
cd "$(dirname "$0")"

# ── prerequisite check ──────────────────────────────────────────────────────
missing=""
command -v node >/dev/null 2>&1 || missing="$missing node"
command -v npm  >/dev/null 2>&1 || missing="$missing npm"
if [ -n "$missing" ]; then
  cat >&2 <<'EOF'

  ✗ Node.js and npm are required but not installed.

  Install them, then re-run this script:

    Debian / Ubuntu :  sudo apt update && sudo apt install -y nodejs npm
    Fedora          :  sudo dnf install -y nodejs npm
    Arch            :  sudo pacman -S --noconfirm nodejs npm

  If your distro ships Node < 18 (Electron needs >= 18), use NodeSource:
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - \
      && sudo apt install -y nodejs

  Re-run:  bash Dashboard/install-dash.sh
EOF
  exit 1
fi

# Reject a Node too old for the pinned Electron (major < 18).
node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$node_major" -lt 18 ]; then
  echo "  ✗ Node $(node --version) is too old; Electron needs Node >= 18." >&2
  echo "    Upgrade via NodeSource:" >&2
  echo "      curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs" >&2
  exit 1
fi

echo "[1/4] Installing Electron (one-time download)..."
if ! ( cd app && npm install --no-audit --no-fund ); then
  cat >&2 <<'EOF'

  ✗ Electron install failed. Common causes:
    - No network (this step needs it once; runtime is offline).
    - npm registry blocked/proxied — check: npm config get registry
  Fix the above and re-run:  bash Dashboard/install-dash.sh
EOF
  exit 1
fi

# Seed the per-machine Spotify config from the template (never overwrite an
# existing one - it holds the user's Client ID and is gitignored).
[ -f app/spotify.json ] || cp app/spotify.example.json app/spotify.json

echo "[2/4] Installing application icon + desktop entry..."
mkdir -p ~/.local/share/applications
sed "s|__HOME__|$HOME|g" dusk-dash.desktop > ~/.local/share/applications/dusk-dash.desktop
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database ~/.local/share/applications || true

echo "[3/4] Installing systemd user service (start at boot)..."
mkdir -p ~/.config/systemd/user
# The committed unit is a template assuming the checkout is at ~/cortana and a
# GDM cookie under %t. Resolve both against this machine instead: a wrong
# ExecStart shows up only as status=203/EXEC, and a wrong XAUTHORITY shows up
# as nothing at all - Electron just never gets a display.
XAUTH=""
for c in "${XAUTHORITY:-}" "/run/user/$(id -u)/gdm/Xauthority" "$HOME/.Xauthority"; do
  if [ -n "$c" ] && [ -f "$c" ]; then XAUTH="$c"; break; fi
done
[ -n "$XAUTH" ] || XAUTH="/run/user/$(id -u)/gdm/Xauthority"
sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$PWD/app|"     -e "s|^ExecStart=.*|ExecStart=$PWD/app/dusk-dash.sh|"     -e "s|^Environment=DISPLAY=.*|Environment=DISPLAY=${DISPLAY:-:0}|"     -e "s|^Environment=XAUTHORITY=.*|Environment=XAUTHORITY=$XAUTH|"     cortana-dash.service > ~/.config/systemd/user/cortana-dash.service
systemctl --user daemon-reload
systemctl --user enable cortana-dash
echo "      checkout $PWD/app   DISPLAY ${DISPLAY:-:0}   XAUTHORITY $XAUTH"

echo "[4/4] Done. Start now with:  systemctl --user start cortana-dash"
echo "Or find 'Dusk Dashboard' in your application menu."
echo "Esc or minimize = shrink to the floating bubble. Right-click bubble = quit."
