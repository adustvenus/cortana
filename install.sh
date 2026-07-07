#!/usr/bin/env bash
# Run from inside the cortana/ folder: bash install.sh
set -e

sudo apt update
sudo apt install -y python3-venv python3-pip portaudio19-dev ffmpeg espeak-ng git

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

[ -f .env ] || cp .env.example .env

echo ""
echo "=== Installed. Next: ==="
echo "1) Edit .env with your API keys:   nano .env"
echo "2) Test in text mode:              ./venv/bin/python main.py --text"
echo "3) Then voice mode:                ./venv/bin/python main.py"
