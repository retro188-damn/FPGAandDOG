#!/bin/bash
# Install (or remove) the resident PL bridge service on the ZYNQ board.
#
# Run ON THE BOARD as root, with the deployed files already under
# /home/xilinx/safety_gate/ (pl_service.py, gate_bridge.py, system.bit,
# system.hwh, fpga-gate-bridge.service).
#
#   sudo bash install_service.sh            # install + enable + start
#   sudo bash install_service.sh --dry-run  # print what would happen
#   sudo bash install_service.sh --remove   # stop + disable + delete the unit
#
# The unit file is the only thing written outside /home/xilinx/safety_gate/.
set -eu

SRC_DIR=${SRC_DIR:-/home/xilinx/safety_gate}
UNIT=fpga-gate-bridge.service
DEST=/etc/systemd/system/$UNIT
MODE=${1:-install}

if [ "$MODE" = "--dry-run" ] || [ "$MODE" = "dry-run" ]; then
    echo "would install $SRC_DIR/$UNIT -> $DEST"
    echo "would run: systemctl daemon-reload; systemctl enable --now $UNIT"
    exit 0
fi

if [ "$MODE" = "--remove" ] || [ "$MODE" = "remove" ]; then
    systemctl disable --now "$UNIT" 2>/dev/null || true
    rm -f "$DEST"
    systemctl daemon-reload
    echo "removed $UNIT; the board is back to manual CLI use"
    exit 0
fi

set -x
install -d "$SRC_DIR/logs" /home/xilinx/safety_gate
install -m 644 "$SRC_DIR/$UNIT" "$DEST"
systemctl daemon-reload
systemctl enable --now "$UNIT"
set +x

sleep 3
echo "--- service status ---"
systemctl --no-pager --lines=20 status "$UNIT" || true
echo
echo "--- listening sockets ---"
ss -ulpn | grep -E '4390[23]' || true
echo
echo "--- log tail ---"
tail -5 "$SRC_DIR/logs/pl_service.log" 2>/dev/null || true
