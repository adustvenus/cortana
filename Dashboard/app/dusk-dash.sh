#!/usr/bin/env bash
# Dusk Dashboard launcher. Safe to run repeatedly: the app holds a
# single-instance lock, so a second launch just focuses the existing window.
cd "$(dirname "$0")"
if [ ! -x node_modules/.bin/electron ]; then
  echo "Electron not installed. Run: bash ../install-dash.sh" >&2
  exit 1
fi
exec ./node_modules/.bin/electron . "$@"
