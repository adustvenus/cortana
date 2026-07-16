#!/usr/bin/env bash
# Dusk Dashboard launcher. Safe to run repeatedly: the app holds a
# single-instance lock, so a second launch just focuses the existing window.
cd "$(dirname "$0")"
if [ ! -x node_modules/.bin/electron ]; then
  echo "Electron not installed. Run: bash ../install-dash.sh" >&2
  exit 1
fi
# With loginctl linger, the user manager starts this unit at boot - possibly
# before the X session exists. Wait for the display socket instead of
# crash-looping ("Missing X server or $DISPLAY").
disp="${DISPLAY#:}"; disp="${disp%%.*}"; disp="${disp:-0}"
for _ in $(seq 1 120); do
  [ -S "/tmp/.X11-unix/X${disp}" ] && break
  sleep 1
done
# --no-sandbox: this is a local single-user dashboard that loads only local
# files, and it avoids the chrome-sandbox SUID-root requirement that otherwise
# breaks on every Electron reinstall. Safe here; do not copy to a web-facing app.
exec ./node_modules/.bin/electron --no-sandbox . "$@"
