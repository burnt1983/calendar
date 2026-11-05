#!/usr/bin/env bash
set -euo pipefail
rm -f \
  "${HOME}/.local/bin/calendar-widget" \
  "${HOME}/.local/share/applications/calendar-widget.desktop"
rm -rf "${HOME}/.local/share/calendar-widget"
echo "Removed the program. Saved settings under ~/.config were left in place."
