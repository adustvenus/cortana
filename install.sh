#!/usr/bin/env bash
# Run from inside the cortana/ folder: bash install.sh
set -e

sudo apt update
# Base runtime. The second line is the desktop/knowledge layer: wmctrl+xdotool
# drive windows, xclip is the clipboard, xprintidle is how presence knows you
# are at the desk, playerctl reaches non-Spotify players over MPRIS, and
# poppler-utils supplies pdftotext so PDFs are searchable. All of them were
# MISSING on the runtime box - every caller degrades to a sentence without
# them, so this is what turns those sentences into working features.
sudo apt install -y python3-venv python3-pip portaudio19-dev ffmpeg espeak-ng git
sudo apt install -y wmctrl xdotool xclip xprintidle playerctl poppler-utils xdg-utils libnotify-bin

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

[ -f .env ] || cp .env.example .env

echo ""
echo "=== Installed. Next: ==="
echo "1) Edit .env with your API keys:   nano .env"
echo "2) Test in text mode:              ./venv/bin/python main.py --text"
echo "3) Then voice mode:                ./venv/bin/python main.py"
echo "4) Run at boot:                    bash install-services.sh"
