#!/usr/bin/env bash
# Screen off, machine on - the dashboard's burn-in guard.
#
# Turns the display off with DPMS and waits. Any KEYBOARD press wakes it (the
# X server relights the panel on input); pointer devices are disabled for the
# duration so a nudged mouse or desk vibration can't relight the screen. They
# are re-enabled the moment the display comes back, and also on any exit path
# (trap) so a crash can never strand the machine mouseless.
#
# Requires: xset (X11 core) and xinput (xserver-xorg-input tools; present on
# stock Ubuntu). Degrades safely: without xinput the screen still sleeps, the
# mouse just becomes a wake source too.
set -u
export DISPLAY="${DISPLAY:-:0}"

# Single instance - a second click while already dark must not double-disable.
LOCK="/tmp/.dusk-sleep-screen.lock"
exec 9>"$LOCK"
flock -n 9 || exit 0

POINTERS=()
if command -v xinput >/dev/null 2>&1; then
  while IFS= read -r id; do
    POINTERS+=("$id")
  done < <(xinput list --short 2>/dev/null | sed -n 's/.*id=\([0-9]*\).*slave *pointer.*/\1/p')
fi

reenable() {
  for id in "${POINTERS[@]:-}"; do
    [ -n "$id" ] && xinput enable "$id" 2>/dev/null
  done
}
trap reenable EXIT INT TERM

for id in "${POINTERS[@]:-}"; do
  [ -n "$id" ] && xinput disable "$id" 2>/dev/null
done

sleep 0.4              # let the click that triggered this settle
xset dpms force off

# Wait for a wake (keyboard input relights the panel), then restore pointers
# via the trap. Poll rather than block: xset has no wait primitive.
while sleep 1; do
  if xset q 2>/dev/null | grep -q "Monitor is On"; then
    break
  fi
done
exit 0
