#!/usr/bin/env bash
# Retire Feuillet. Les réglages et les pages en cours restent, sauf avec --purge.
set -euo pipefail

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"

rm -f "$HOME/.local/bin/feuillet" "$data_home/applications/feuillet.desktop" \
      "$data_home/icons/hicolor/scalable/apps/feuillet.svg"
rm -rf "$data_home/feuillet/app" "$data_home/feuillet/venv"

if [ "${1:-}" = "--purge" ]; then
    rm -rf "$data_home/feuillet" "$config_home/feuillet"
    echo "Feuillet est retiré, avec ses réglages et ses pages en cours."
else
    rmdir "$data_home/feuillet" 2>/dev/null || true
    echo "Feuillet est retiré. Pour effacer aussi tes réglages et tes scans : ./uninstall.sh --purge"
fi
