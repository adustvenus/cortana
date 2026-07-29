#!/usr/bin/env bash
# Push the current APK to the phone over WIRELESS adb - no cable.
#
# Why this exists: some Android skins (OxygenOS/ColorOS on OnePlus/OPPO in
# particular) silently drop tap-to-install and in-app-updater installs, showing
# no error at all. adb installs take the privileged path instead of that
# installer UI, so they just work. Wireless debugging gives us that same path
# without the wire.
#
# ONE-TIME on the phone:
#   Settings -> About device -> tap "Build number" 7x  (unlocks developer mode)
#   Settings -> System -> Developer options -> Wireless debugging -> ON
#   Tap "Pair device with pairing code" -> note the IP:PORT and 6-digit code
# Then on the workstation:
#   bash mobile/push-update.sh pair 192.168.1.50:41234 123456
#
# EVERY TIME AFTER (phone just needs Wireless debugging ON, same Wi-Fi):
#   bash mobile/push-update.sh
#
# The pairing survives reboots; the *connect* port changes, so this script
# re-discovers it via mDNS each run.
set -e
cd "$(dirname "$0")/.."

APK="mobile/dist/cortana-mobile.apk"

command -v adb >/dev/null 2>&1 || {
  echo "adb not installed. Run: sudo apt install -y android-tools-adb" >&2
  exit 1
}

if [ "$1" = "pair" ]; then
  [ -n "$2" ] && [ -n "$3" ] || { echo "usage: $0 pair <IP:PORT> <CODE>" >&2; exit 1; }
  adb pair "$2" "$3"
  echo "Paired. Now run:  bash mobile/push-update.sh connect <IP:PORT>"
  echo "(use the IP:PORT on the MAIN Wireless debugging screen - it differs"
  echo " from the pairing dialog's port)"
  exit 0
fi

# Explicit address beats discovery. mDNS auto-discovery is flaky in general and
# Tailscale's DNS can swallow it outright, so this is the reliable path: use the
# IP:PORT shown on the phone's main Wireless debugging screen.
if [ "$1" = "connect" ]; then
  [ -n "$2" ] || { echo "usage: $0 connect <IP:PORT>" >&2; exit 1; }
  adb disconnect >/dev/null 2>&1 || true
  adb connect "$2"
fi

[ -f "$APK" ] || { echo "No APK at $APK - run git pull first." >&2; exit 1; }

# Already connected? Otherwise find the phone's wireless-debug endpoint via
# mDNS (the port changes every time the phone toggles wireless debugging).
if ! adb devices | grep -q "device$"; then
  echo "Looking for the phone on the network..."
  target="$(adb mdns services 2>/dev/null | awk '/_adb-tls-connect/ {print $3; exit}')"
  if [ -z "$target" ]; then
    cat >&2 <<'EOF'
Couldn't auto-discover the phone (mDNS is flaky, and Tailscale's DNS can
swallow it - this is normal, just connect explicitly instead):

  On the phone: Developer options -> Wireless debugging
  Read the "IP address & Port" on THAT screen, then run:

     bash mobile/push-update.sh connect 192.168.x.x:PORT

If you have never paired this phone, pair once first (different port, from
the "Pair device with pairing code" dialog):

     bash mobile/push-update.sh pair 192.168.x.x:PAIRPORT 123456
EOF
    exit 1
  fi
  echo "Connecting to $target"
  adb connect "$target"
fi

ver="$(python3 -c "import json;print(json.load(open('mobile/dist/version.json'))['version'])" 2>/dev/null || echo '?')"
echo "Installing Cortana Mobile v$ver ..."
adb install -r "$APK"
echo "Done - open Cortana on the phone."
