#!/usr/bin/env bash
# Installs the cortana systemd USER unit with every path resolved for THIS
# machine. Run from inside the checkout: bash install-services.sh
#
# cortana.service is committed as a TEMPLATE. Used verbatim it assumes the
# checkout sits at ~/cortana and that a GDM X cookie exists at a fixed uid.
# Both assumptions fail on a rebuilt box, and they fail in ways that are hard
# to read: a wrong ExecStart is "status=203/EXEC", and a wrong XAUTHORITY
# throws nothing at all - Cortana starts, then screenshots and the F9/F10
# hotkeys silently do nothing.
#
# bridge/install-bridge.sh already resolves its unit this way; this is the
# same treatment for the assistant itself. Dashboard/install-dash.sh does its
# own.
set -e
cd "$(dirname "$0")"
ROOT="$PWD"

# Installs have used different venv names over time.
PY=""
for d in venv cortana_venv .venv; do
  if [ -x "$d/bin/python" ]; then PY="$ROOT/$d/bin/python"; break; fi
done
[ -n "$PY" ] || { echo "No virtualenv found in $ROOT - run: bash install.sh" >&2; exit 1; }

DISP="${DISPLAY:-:0}"

# Whatever cookie THIS session actually uses, then the display manager's, then
# the classic home-dir one. Checked for existence rather than assumed, because
# a wrong value here is invisible until a screenshot quietly returns nothing.
XAUTH=""
for c in "${XAUTHORITY:-}" "/run/user/$(id -u)/gdm/Xauthority" "$HOME/.Xauthority"; do
  if [ -n "$c" ] && [ -f "$c" ]; then XAUTH="$c"; break; fi
done
if [ -z "$XAUTH" ]; then
  XAUTH="/run/user/$(id -u)/gdm/Xauthority"
  echo "  ! No X authority file found on disk." >&2
  echo "    Screenshots and F9/F10 will not work until one exists. If you ran" >&2
  echo "    this over SSH, re-run it from a terminal inside your desktop session." >&2
fi

mkdir -p ~/.config/systemd/user
sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
    -e "s|^ExecStart=.*|ExecStart=$PY $ROOT/launcher.py|" \
    -e "s|^Environment=DISPLAY=.*|Environment=DISPLAY=$DISP|" \
    -e "s|^Environment=XAUTHORITY=.*|Environment=XAUTHORITY=$XAUTH|" \
    cortana.service > ~/.config/systemd/user/cortana.service

systemctl --user daemon-reload
systemctl --user enable cortana
# User units must survive logout / start at boot.
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

echo ""
echo "=== cortana.service installed ==="
echo "  checkout    $ROOT"
echo "  python      $PY"
echo "  DISPLAY     $DISP"
echo "  XAUTHORITY  $XAUTH"
echo ""
echo "Start:   systemctl --user start cortana"
echo "Status:  systemctl --user status cortana"
echo "Logs:    journalctl --user -u cortana -f"
