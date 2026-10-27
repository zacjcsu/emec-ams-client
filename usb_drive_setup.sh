#!/bin/bash
# Sets up utils/usb_drive.py on a Pi 4 on PoE: its USB-C port becomes a flash drive for the machine, with the folders
# LOCAL (the dashboard's programs) and USB (the flash drive in the Pi, while one is in). Reboot once afterwards.
#   sudo bash /home/emec/emec-ams/usb_drive_setup.sh            install
#   sudo bash /home/emec/emec-ams/usb_drive_setup.sh --remove   undo
# The machine's USB port must not get the Pi's 5 V. Tape over the VBUS pin of the plug that goes to the machine.
set -euo pipefail

APP_DIR=/home/emec/emec-ams
APP_USER=emec
PROGRAMS=/home/emec/haas-programs
UNIT=/etc/systemd/system/emec-usb-drive.service
MODULES=/etc/modules-load.d/emec-usb-drive.conf
OVERLAY="dtoverlay=dwc2,dr_mode=peripheral"
CONFIG_TXT=/boot/firmware/config.txt
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt

[[ $EUID -eq 0 ]] || { echo "Run it with sudo."; exit 1; }

if [[ "${1:-}" == "--remove" ]]; then
    systemctl disable --now emec-usb-drive 2>/dev/null || true
    rm -f "$UNIT" "$MODULES"
    systemctl daemon-reload
    sed -i "/^${OVERLAY}\$/d" "$CONFIG_TXT"
    echo "Removed. ${PROGRAMS} is left in place. Reboot to give the USB-C port back."
    exit 0
fi

if ! grep -qx "$OVERLAY" "$CONFIG_TXT"; then
    printf '\n[all]\n%s\n' "$OVERLAY" >> "$CONFIG_TXT"
    echo "Added ${OVERLAY} to ${CONFIG_TXT}."
fi
printf 'nbd\nlibcomposite\n' > "$MODULES"
# The app copies the dashboard's programs here once it exists, after its next restart.
install -d -o "$APP_USER" -g "$APP_USER" "$PROGRAMS"

cat > "$UNIT" <<EOF
[Unit]
Description=EMEC USB drive for the machine
After=local-fs.target

[Service]
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -m utils.usb_drive
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable emec-usb-drive >/dev/null
echo "Installed emec-usb-drive. Reboot, then check: journalctl -u emec-usb-drive, logs/usb_drive.log"
