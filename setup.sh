#!/bin/bash
set -e

BIN_DIR="/opt/tgm"
CONF_DIR="/etc/tgm"
STORE_DIR="/var/lib/tgm"
JRNL_DIR="/var/log/tgm"
SYSD_DIR="/etc/systemd/system"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "=== TGM — Tank Gauge Monitor Setup ==="

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: run with sudo"
    exit 1
fi

echo "Dependencies..."
pip3 install pyserial requests --break-system-packages 2>/dev/null || pip3 install pyserial requests
apt-get install -y socat > /dev/null 2>&1 || true

echo "Directories..."
mkdir -p "$BIN_DIR" "$CONF_DIR" "$STORE_DIR" "$JRNL_DIR"

echo "Installing to $BIN_DIR..."
cp "$HERE/tgm.py" "$BIN_DIR/tgm.py"
cp "$HERE/tgm"    "$BIN_DIR/tgm"
chmod +x "$BIN_DIR/tgm.py" "$BIN_DIR/tgm"

[ -f "$HERE/integration_test.py" ] && cp "$HERE/integration_test.py" "$BIN_DIR/"

ln -sf "$BIN_DIR/tgm" /usr/local/bin/tgm
echo "  Command: tgm"

if [ ! -f "$CONF_DIR/site.json" ]; then
    cp "$HERE/site.example.json" "$CONF_DIR/site.json"
    echo ""
    echo "  ============================================"
    echo "  SITE CONFIG CREATED: $CONF_DIR/site.json"
    echo "  EDIT IT NOW:         sudo tgm settings edit"
    echo "  ============================================"
    echo ""
else
    echo "  Site config exists, not overwriting"
fi

WHO="${SUDO_USER:-$USER}"
if ! id -nG "$WHO" | grep -qw dialout; then
    usermod -aG dialout "$WHO"
    echo "  Added $WHO to dialout (log out/in to apply)"
fi

echo "Systemd timer..."
cat > "$SYSD_DIR/tgm.service" << 'EOF'
[Unit]
Description=Tank Gauge Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/tgm bridge
StandardOutput=append:/var/log/tgm/tgm.log
StandardError=append:/var/log/tgm/tgm.log
EOF

cat > "$SYSD_DIR/tgm.timer" << 'EOF'
[Unit]
Description=Tank Gauge Monitor Timer

[Timer]
OnBootSec=1min
OnCalendar=*:0/60
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable tgm.timer
systemctl start tgm.timer

echo ""
echo "=== Ready ==="
echo ""
echo "  NEXT:  sudo tgm settings edit"
echo "  TEST:  sudo tgm check"
echo ""
echo "  Commands:"
echo "    tgm info               # unit info + last reading"
echo "    tgm settings           # show site config"
echo "    sudo tgm settings edit # edit site config"
echo "    sudo tgm check         # one-shot field test"
echo "    sudo tgm check --raw   # field test, raw output"
echo "    sudo tgm bridge        # production bridge mode"
echo "    sudo tgm watch         # scheduled readings"
echo "    sudo tgm serve         # scheduled + on-demand HTTP"
echo "    tgm journal            # recent logs"
echo "    tgm journal --faults   # faults only"
