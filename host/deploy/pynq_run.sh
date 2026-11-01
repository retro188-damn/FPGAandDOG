#!/bin/sh
# Run a Python script on the PYNQ board with the environment the PYNQ stack
# actually expects.
#
# Why this wrapper exists: the board's PYNQ lives in a venv
# (/usr/local/share/pynq-venv) and its device discovery needs XILINX_XRT=/usr.
# A plain `sudo python3 ...` from an ssh session has neither, and fails with
# "No devices found, is the XRT environment sourced?" / "No module named 'pynq'".
#
# Usage on the board (script path is required):
#   echo xilinx | sudo -S /home/xilinx/safety_gate/run.sh /home/xilinx/safety_gate/gate_test.py
set -e
export XILINX_XRT=/usr
export PATH=/usr/local/share/pynq-venv/bin:$PATH
exec /usr/local/share/pynq-venv/bin/python3 "$@"
