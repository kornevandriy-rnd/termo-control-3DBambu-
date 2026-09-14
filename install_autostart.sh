#!/usr/bin/env bash
# Install kiosk autostart so the Farm monitor launches fullscreen on login.
# Run on the Pi from inside the app folder:  bash install_autostart.sh
# Disable later:  bash install_autostart.sh --remove
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH="bash $DIR/kiosk.sh"
XDG="$HOME/.config/autostart/farm-monitor.desktop"
LABWC="$HOME/.config/labwc/autostart"
WAYFIRE="$HOME/.config/wayfire.ini"

if [ "${1:-}" = "--remove" ]; then
    rm -f "$XDG"
    [ -f "$LABWC" ] && sed -i '/kiosk\.sh/d' "$LABWC"
    [ -f "$WAYFIRE" ] && sed -i '/kiosk\.sh/d' "$WAYFIRE"
    sudo rm -f /etc/sudoers.d/farm-monitor
    echo "Autostart removed. Reboot to apply."
    exit 0
fi

chmod +x "$DIR/kiosk.sh" 2>/dev/null || true

# allow the on-screen "Вимкнути" button to power off without a password
SUDOERS=/etc/sudoers.d/farm-monitor
if [ ! -f "$SUDOERS" ]; then
    echo "Configuring passwordless shutdown (sudo password may be asked once)..."
    printf '%s ALL=(root) NOPASSWD: /usr/sbin/poweroff, /sbin/poweroff, /usr/sbin/shutdown, /sbin/shutdown\n' \
        "$USER" | sudo tee "$SUDOERS" >/dev/null
    sudo chmod 440 "$SUDOERS"
fi

# 1) XDG autostart — honoured by most Raspberry Pi OS desktop sessions
mkdir -p "$HOME/.config/autostart"
cat > "$XDG" <<EOF
[Desktop Entry]
Type=Application
Name=Farm climate monitor
Comment=3D-farm climate dashboard (kiosk)
Exec=$LAUNCH
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

# 2) labwc autostart — default compositor on Pi 4/5 (Bookworm/Trixie)
if [ -d "$HOME/.config/labwc" ] || command -v labwc >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/labwc"
    touch "$LABWC"
    grep -qF "kiosk.sh" "$LABWC" || echo "$LAUNCH &" >> "$LABWC"
fi

# 3) wayfire autostart — older Raspberry Pi OS
if [ -f "$WAYFIRE" ] && ! grep -qF "kiosk.sh" "$WAYFIRE"; then
    if grep -q "^\[autostart\]" "$WAYFIRE"; then
        sed -i "/^\[autostart\]/a farmmon = $LAUNCH" "$WAYFIRE"
    else
        printf "\n[autostart]\nfarmmon = %s\n" "$LAUNCH" >> "$WAYFIRE"
    fi
fi

echo "Autostart installed."
echo "  launcher: $LAUNCH"
echo "Single-instance lock prevents double windows if more than one hook fires."
echo "Reboot to test:  sudo reboot"
echo "Disable later:   bash install_autostart.sh --remove"
