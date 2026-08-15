#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/.local/share/allcpumeter-linux"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "All CPU Meter for Linux v0.1.1 installer"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This release supports Ubuntu/Debian systems using apt."
    exit 1
fi

echo "Installing base requirements..."
sudo apt-get update
sudo apt-get install -y python3 python3-tk conky-all pciutils procps util-linux iproute2 pkexec

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp allcpumeter.py "$APP_DIR/allcpumeter.py"
cp uninstall.sh "$APP_DIR/uninstall.sh"
chmod +x "$APP_DIR/allcpumeter.py" "$APP_DIR/uninstall.sh"

cat > "$BIN_DIR/allcpumeter-linux" <<'LAUNCHER'
#!/usr/bin/env bash
exec python3 "$HOME/.local/share/allcpumeter-linux/allcpumeter.py" "$@"
LAUNCHER
chmod +x "$BIN_DIR/allcpumeter-linux"

cat > "$DESKTOP_DIR/allcpumeter-linux.desktop" <<EOF2
[Desktop Entry]
Type=Application
Name=All CPU Meter for Linux
Comment=Configure a compact desktop hardware meter
Exec=$BIN_DIR/allcpumeter-linux
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
EOF2

echo
echo "Installation complete."
echo "Launch 'All CPU Meter for Linux' from the Ubuntu application menu."
echo "You can also start it from a terminal with:"
echo "  $BIN_DIR/allcpumeter-linux"
