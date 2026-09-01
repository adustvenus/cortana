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
VENV=""
for d in venv cortana_venv .venv; do
  [ -x "$d/bin/pip" ] && { VENV="$d"; break; }
done
if [ -z "$VENV" ]; then
  echo "No virtualenv found - creating ./venv"
  python3 -m venv venv
  VENV="venv"
fi
echo "Using virtualenv: $VENV"

"./$VENV/bin/pip" install --upgrade pip
"./$VENV/bin/pip" install -r requirements.txt

[ -f .env ] || cp .env.example .env

echo ""
echo "=== Installed. Next: ==="
echo "1) Edit .env with your API keys:   nano .env"
echo "2) Test in text mode:              ./$VENV/bin/python main.py --text"
echo "3) Then voice mode:                ./$VENV/bin/python main.py"
echo "4) Run at boot:                    bash install-services.sh"
echo "5) Check everything:               bash selftest.sh"
