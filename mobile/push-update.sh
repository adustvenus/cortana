#!/usr/bin/env bash
# Push the current APK to the phone over WIRELESS adb - no cable, no toggling
# Tailscale on or off.
#
# Why this exists: some Android skins (OxygenOS/ColorOS on OnePlus/OPPO in
# particular) silently drop tap-to-install and in-app-updater installs, showing
# no error at all. adb installs take the privileged path instead of that
# installer UI, so they just work.
#
# Why it prefers the tailnet: with Tailscale active on the phone, connecting to
# the phone's *LAN* address often hangs - the phone answers through the tunnel
# and the reply never comes back the way it went out. Its *Tailscale* address
# has no such split, so that is tried first and the VPN can stay on.
#
# ONE-TIME on the phone:
#   Settings -> About device -> tap "Build number" 7x   (unlocks developer mode)
#   Settings -> System -> Developer options -> Wireless debugging -> ON
#   Tap "Pair device with pairing code" -> note that dialog's IP:PORT and code
#     bash mobile/push-update.sh pair 192.168.1.50:41234 123456
#
# EVERY UPDATE AFTER THAT - read the port off the MAIN Wireless debugging
# screen (it changes each time the toggle is flipped) and run:
#     bash mobile/push-update.sh 37219
#
# With no argument it reuses the last address that worked.
set -e
cd "$(dirname "$0")/.."

APK="mobile/dist/cortana-mobile.apk"
LAST=".adb_target"          # last address that worked (gitignored)

command -v adb >/dev/null 2>&1 || {
  echo "adb not installed. Run: sudo apt install -y android-tools-adb" >&2
  exit 1
}

# ── one-time pairing ────────────────────────────────────────────────────────
if [ "$1" = "pair" ]; then
  [ -n "$2" ] && [ -n "$3" ] || { echo "usage: $0 pair <IP:PORT> <CODE>" >&2; exit 1; }
  adb pair "$2" "$3"
  echo "Paired. Now run:  bash mobile/push-update.sh <PORT>"
  echo "(the PORT on the main Wireless debugging screen - not the pairing one)"
  exit 0
fi

# ── work out where to connect ───────────────────────────────────────────────
# Candidate addresses, best first: the phone's tailnet IP (works with the VPN
# up), then anything mDNS finds, then whatever worked last time.
candidates() {
  local port="$1"
  if [ -n "$port" ] && command -v tailscale >/dev/null 2>&1; then
    # Peers on the tailnet; the phone is whichever one accepts the adb port.
    tailscale status 2>/dev/null | awk '/^100\./ {print $1":'"$port"'"}'
  fi
  adb mdns services 2>/dev/null | awk '/_adb-tls-connect/ {print $3}'
  [ -f "$LAST" ] && cat "$LAST"
}

target=""
case "$1" in
  connect) [ -n "$2" ] || { echo "usage: $0 connect <IP:PORT>" >&2; exit 1; }
           target="$2" ;;
  ''|*[!0-9]*) ;;                       # no arg (or not a bare port): discover
  *) PORT="$1" ;;                        # bare number = wireless-debugging port
esac

if [ -z "$target" ]; then
  adb disconnect >/dev/null 2>&1 || true
  while read -r addr; do
    [ -z "$addr" ] && continue
    echo "trying $addr ..."
    if adb connect "$addr" 2>&1 | grep -qi "connected to"; then
      target="$addr"
      break
    fi
  done < <(candidates "${PORT:-}" | awk '!seen[$0]++')
else
  adb disconnect >/dev/null 2>&1 || true
  adb connect "$target" >/dev/null 2>&1 || true
fi

if [ -z "$target" ] || ! adb devices | grep -q "device$"; then
  cat >&2 <<'EOF'

Couldn't reach the phone. On the phone check:
  Developer options -> Wireless debugging is ON
  It is on the same Wi-Fi as this machine, or on your tailnet

Then read the PORT from the main Wireless debugging screen and run:
     bash mobile/push-update.sh <PORT>

Never paired this machine with the phone? Do that once first, using the
DIFFERENT port shown in the "Pair device with pairing code" dialog:
     bash mobile/push-update.sh pair <IP:PAIRPORT> <CODE>
EOF
  exit 1
fi

echo "$target" > "$LAST"

ver="$(python3 -c "import json;print(json.load(open('mobile/dist/version.json'))['version'])" 2>/dev/null || echo '?')"
[ -f "$APK" ] || { echo "No APK at $APK - run git pull first." >&2; exit 1; }
echo "Installing Cortana Mobile v$ver to $target ..."
adb -s "$target" install -r "$APK"
echo "Done - open Cortana on the phone."
