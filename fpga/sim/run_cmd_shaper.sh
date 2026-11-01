#!/bin/bash
# Simulate the PL command shaper (no board, no Vivado GUI).
# Expects Vivado's simulator on PATH or VIVADO_BIN set.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
bin="${VIVADO_BIN:-D:/zongy/vivado/2026.1/Vivado/bin}"
work="$root/_scratch/sim_shaper"
mkdir -p "$work"
cd "$work"
"$bin/xvlog.bat" "$root/fpga/src/cmd_shaper.v" "$root/fpga/sim/tb_cmd_shaper.v"
"$bin/xelab.bat" tb_cmd_shaper -s tb_cmd_shaper
"$bin/xsim.bat" tb_cmd_shaper -R | tee shaper_sim.log | grep -E "PASS|FAIL|SIM"
grep -q "SIM PASS" shaper_sim.log
