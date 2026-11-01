#!/bin/bash
# Simulate safety_gate with the integrated command shaper (no board needed).
set -e
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
bin="${VIVADO_BIN:-D:/zongy/vivado/2026.1/Vivado/bin}"
work="$root/_scratch/sim_gate"
mkdir -p "$work"
cd "$work"
"$bin/xvlog.bat" "$root/fpga/src/safety_gate.v" "$root/fpga/src/cmd_shaper.v" "$root/fpga/sim/tb_safety_gate.v"
"$bin/xelab.bat" tb_safety_gate -s tb_safety_gate
"$bin/xsim.bat" tb_safety_gate -R | tee gate_sim.log | grep -E "PASS|FAIL|SIM|settle|ramp"
grep -q "SIM PASS" gate_sim.log
