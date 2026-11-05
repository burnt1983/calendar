#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SHARE="${HOME}/.local/share/calendar-widget"
BIN="${HOME}/.local/bin"
mkdir -p "$SHARE" "$BIN"
install -m 0755 "$ROOT/calendar-widget.py" "$SHARE/calendar-widget.py"
install -m 0644 "$ROOT/desklet_geometry.py" "$SHARE/desklet_geometry.py"
APPS="${HOME}/.local/share/applications"
mkdir -p "$APPS"
sed "s|^Exec=calendar-widget$|Exec=${BIN}/calendar-widget|" \
  "$ROOT/data/calendar.desktop" > "$APPS/calendar-widget.desktop"
chmod 0644 "$APPS/calendar-widget.desktop"
ln -sfn "$SHARE/calendar-widget.py" "$BIN/calendar-widget"
echo "Installed."
echo "  Run: calendar-widget"
echo "  It does not start at login until you tick Open at login."
