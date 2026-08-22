#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/.local/share/allcpumeter-linux"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "All CPU Meter for Linux v0.2.1 installer"
echo

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This release currently supports Ubuntu/Debian systems using apt."
    exit 1
fi

echo "Installing GTK4/Python requirements..."
sudo apt-get update
sudo apt-get install -y \
    python3 \
    python3-gi \
    gir1.2-gtk-4.0 \
    pciutils \
    procps \
    util-linux \
    lm-sensors

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp allcpumeter.py "$APP_DIR/allcpumeter.py"
chmod +x "$APP_DIR/allcpumeter.py"

cat > "$BIN_DIR/allcpumeter-linux" <<'EOF'
#!/usr/bin/env bash
exec python3 "$HOME/.local/share/allcpumeter-linux/allcpumeter.py" "$@"
EOF
chmod +x "$BIN_DIR/allcpumeter-linux"

cat > "$DESKTOP_DIR/allcpumeter-linux.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=All CPU Meter for Linux
Comment=Configure a compact GTK4 hardware meter
Exec=$BIN_DIR/allcpumeter-linux
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
EOF


echo
echo "Installation complete."
echo "Launch 'All CPU Meter for Linux' from the application menu."
