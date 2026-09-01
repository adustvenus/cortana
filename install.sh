#!/usr/bin/env bash
# Run from inside the cortana/ folder: bash install.sh
set -e

sudo apt update

# Base runtime.
sudo apt install -y python3-venv python3-pip portaudio19-dev ffmpeg espeak-ng git

# Desktop / knowledge layer. wmctrl+xdotool drive windows, xclip is the
# clipboard, xprintidle is how presence knows you are at the desk, playerctl
# reaches non-Spotify players over MPRIS, poppler-utils supplies pdftotext so
# PDFs are searchable. Every caller degrades to a spoken sentence without these,
# so this line is what turns those sentences into working features.
sudo apt install -y wmctrl xdotool xclip xprintidle playerctl poppler-utils \
                    xdg-utils libnotify-bin

# Installs have used different venv names over time (venv, cortana_venv, .venv),
# which is why install-services.sh and bridge/install-bridge.sh both probe for
# one. This used to hardcode ./venv - so on a box whose venv is cortana_venv it
# quietly built a SECOND environment and installed every dependency into the one
# the service does not run from. That failure looks exactly like "install.sh
# succeeded but the new imports are still missing", which is the worst shape a
# failure can have.
# The probe RUNS the interpreter rather than testing the executable bit.
# `[ -x venv/bin/pip ]` was the wrong measurement: a venv whose base python has
# been upgraded or removed still has an executable pip whose shebang points at a
# dangling symlink, so -x passes and the very next line dies with the confusing
#     ./venv/bin/pip: cannot execute: required file not found
# which is a SHEBANG error, not a missing-file error. Same class of mistake as
# checking a script with `bash -n` in a shell that tolerates CRLF: a guard has
# to run in the same context as the thing that fails.
VENV=""
for d in venv cortana_venv .venv; do
  if [ -d "$d" ] && "$d/bin/python" -c "" 2>/dev/null; then VENV="$d"; break; fi
done

if [ -z "$VENV" ]; then
  # Distinguish "never had one" from "had one and it is broken", because the
  # second needs the corpse moved out of the way first and is silent otherwise.
  for d in venv cortana_venv .venv; do
    if [ -d "$d" ]; then
      echo "Found $d/ but its python does not run - the base interpreter it was"
      echo "built against is gone (an OS python upgrade does this). Rebuilding it."
      mv "$d" "$d.broken.$(date +%s)"
    fi
  done
  echo "Creating ./venv"
  python3 -m venv venv
  VENV="venv"
fi
echo "Using virtualenv: $VENV"

# A venv's console scripts (pip, pytest, ...) hardcode an ABSOLUTE interpreter
# path in their shebang at creation time, while bin/python is a relative symlink
# chain. So moving the checkout leaves a venv whose python runs perfectly and
# whose every console script dies with
#     ./venv/bin/pip: cannot execute: required file not found
# This is exactly what happened here: the tree moved from ~/AI/cortana to
# ~/cortana. Re-running venv over the existing directory rewrites bin/ and
# re-installs pip with a correct shebang; without --clear it leaves
# site-packages alone, so nothing already installed is lost.
SHEBANG=$(head -1 "$VENV/bin/pip" 2>/dev/null | sed 's|^#!||')
if [ -n "$SHEBANG" ] && [ ! -x "$SHEBANG" ]; then
  echo "  $VENV/bin/pip points at a python that no longer exists:"
  echo "    $SHEBANG"
  echo "  (the checkout moved). Rewriting the venv's scripts - packages are kept."
  python3 -m venv "$VENV"
fi

# A directory literally named venv<CR> is debris from the CRLF bug (see
# CLAUDE.md): `python3 -m venv venv\r` created it before .gitattributes landed.
if ls -d venv*$'\r' >/dev/null 2>&1; then
  echo "NOTE: leftover CRLF directory found. Remove it with:"
  printf '      rm -rf %q\n' venv$'\r'
fi

"./$VENV/bin/python" -m pip install --upgrade pip
"./$VENV/bin/python" -m pip install -r requirements.txt

[ -f .env ] || cp .env.example .env

echo ""
echo "=== Installed. Next: ==="
echo "1) Edit .env with your API keys:   nano .env"
echo "2) Test in text mode:              ./$VENV/bin/python main.py --text"
echo "3) Then voice mode:                ./$VENV/bin/python main.py"
echo "4) Run at boot:                    bash install-services.sh"
echo "5) Check everything:               bash selftest.sh"
