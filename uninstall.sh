#!/usr/bin/env bash
set -euo pipefail

pkill -f 'conky.*allcpumeter-linux.conf' 2>/dev/null || true
rm -rf "$HOME/.local/share/allcpumeter-linux"
rm -f "$HOME/.local/bin/allcpumeter-linux"
rm -f "$HOME/.local/share/applications/allcpumeter-linux.desktop"
rm -f "$HOME/.config/autostart/allcpumeter-linux.desktop"
rm -f "$HOME/.config/conky/allcpumeter-linux.conf"
rm -rf "$HOME/.config/allcpumeter-linux"

echo "All CPU Meter for Linux has been removed."
