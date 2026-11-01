# build_all.tcl -- full non-interactive flow: BD + bitstream.
#
# The board is switched off, so the only way to keep the RTL honest is to
# rebuild the bitstream here and check timing/utilisation from the reports.
set root "E:/zongy/WORK/Claude code/FPGAandDOG"
source "$root/fpga/build/build.tcl"

launch_runs synth_1 -jobs 8
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
    error "synthesis did not reach 100%: [get_property STATUS [get_runs synth_1]]"
}
puts "SYNTH_OK [get_property STATUS [get_runs synth_1]]"

# --- guard: the ramp delta must reach synthesis as the integer 20 ---------------
# Vivado's block-design "module reference" wrapper hands *vector*-typed module
# parameters over as quoted strings, so `parameter signed [17:0] = 18'sd20` gets
# synthesized as the ASCII code of "20" (0x3230 = 12848) and the command shaper
# jumps straight to its target in one 1 ms tick instead of ramping.  Simulation
# cannot see this (it elaborates the module directly) - only the board can, which
# is how the 2026-10-26 bitstream slipped through until gate_test.py failed
# "ramp step bounded by DELTA".  Fail the build instead of the acceptance test.
set occl "$prjdir/safety_gate.runs/system_safety_gate_0_0_synth_1/runme.log"
if {[file exists $occl]} {
    set fh [open $occl r]
    set body [read $fh]
    close $fh
    set delta_ok 0
    foreach line [split $body "\n"] {
        if {[regexp {Parameter DELTA_V bound to: 20 - type: integer} $line]} {
            set delta_ok 1
        }
        if {[regexp {Parameter DELTA_V bound to: (\S+) - type: (\w+)} $line -> val typ]} {
            puts "RAMP_PARAM_BIND: DELTA_V=$val type=$typ"
        }
    }
    if {!$delta_ok} {
        error "cmd_shaper DELTA_V did not bind to the integer 20 (see $occl): a vector-typed safety_gate parameter reached synthesis as a quoted string"
    }
    puts "RAMP_PARAM_CHECK_OK"
}

launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    error "implementation did not reach 100%: [get_property STATUS [get_runs impl_1]]"
}
puts "IMPL_OK [get_property STATUS [get_runs impl_1]]"

set wns [get_property STATS.WNS [get_runs impl_1]]
set whs [get_property STATS.WHS [get_runs impl_1]]
puts "TIMING_WNS=$wns WHS=$whs"
puts "BUILD_ALL_OK"
