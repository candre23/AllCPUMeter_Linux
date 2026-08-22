#!/usr/bin/env bash
set -euo pipefail

pkill -f "$HOME/.local/share/allcpumeter-linux/allcpumeter.py --meter" 2>/dev/null || true

rm -rf "$HOME/.local/share/allcpumeter-linux"
rm -f "$HOME/.local/bin/allcpumeter-linux"
rm -f "$HOME/.local/share/applications/allcpumeter-linux.desktop"
rm -f "$HOME/.config/autostart/allcpumeter-linux.desktop"
rm -rf "$HOME/.cache/allcpumeter-linux"

echo "All CPU Meter for Linux has been removed."
echo "Saved settings under ~/.config/allcpumeter-linux were retained."
