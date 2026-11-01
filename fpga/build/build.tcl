# -----------------------------------------------------------------------------
# build.tcl -- create the safety_gate block design and run the bitstream flow
#
# Run from Vivado Tcl:
#   source E:/zongy/WORK/Claude code/FPGAandDOG/fpga/build/build.tcl
#
# Produces:
#   fpga/build/safety_gate/safety_gate.runs/impl_1/system_wrapper.bit
#   fpga/build/safety_gate/safety_gate.gen/.../hw_handoff/system.hwh
# -----------------------------------------------------------------------------
set root   "E:/zongy/WORK/Claude code/FPGAandDOG"
set prjdir "$root/fpga/build/safety_gate"
set src    "$root/fpga/src/safety_gate.v"
set shaper "$root/fpga/src/cmd_shaper.v"
set ps7cfg "$root/_scratch/vendor_bd/ps7_config.tcl"

proc fail {msg} {
    puts "BUILD_ERROR: $msg"
    return -code error $msg
}

catch { close_project }
catch { close_design }
file mkdir $prjdir
create_project -force safety_gate $prjdir -part xc7z020clg400-2
add_files -norecurse [list $src $shaper]
set_property file_type {Verilog} [get_files safety_gate.v]
set_property file_type {Verilog} [get_files cmd_shaper.v]
update_compile_order -fileset sources_1

create_bd_design "system"

# --- PS7 with the vendor PYNQ image's exact configuration -----------------------
source $ps7cfg
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
    -config {make_external "FIXED_IO, DDR" apply_board_preset "0" Master "Disable" Slave "Disable"} \
    [get_bd_cells processing_system7_0]

# --- our safety gate as a module reference -------------------------------------
create_bd_cell -type module -reference safety_gate safety_gate_0
set intfs [get_bd_intf_pins -of_objects [get_bd_cells safety_gate_0]]
puts "SAFETY_GATE_INTF_PINS: $intfs"

# tie the (not yet wired) hardware e-stop input to 0 so the design validates clean
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant const_estop
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] [get_bd_cells const_estop]
connect_bd_net [get_bd_pins const_estop/dout] [get_bd_pins safety_gate_0/estop_in]

set aximm ""
foreach p $intfs {
    if {[get_property VLNV $p] eq "xilinx.com:interface:aximm_rtl:1.0"} {
        set aximm $p
    }
}
if {$aximm eq ""} {
    fail "no aximm interface inferred on safety_gate_0 (check s_axi_* port names)"
}
puts "SAFETY_GATE_AXIMM: $aximm"

apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
    -config {Master "/processing_system7_0/M_AXI_GP0" Clk "Auto"} \
    $aximm

assign_bd_address -offset 0x43C00000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces processing_system7_0/Data] \
    [get_bd_addr_segs safety_gate_0/S_AXI/reg0]

regenerate_bd_layout
save_bd_design

validate_bd_design
save_bd_design

# --- wrapper + top -------------------------------------------------------------
set bd_file [get_files -quiet *system.bd]
puts "BD_FILE: $bd_file"
make_wrapper -files $bd_file -top -import
set_property top system_wrapper [current_fileset]
update_compile_order -fileset sources_1
# NOTE: the module-reference wrapper (which is where Vivado rewrites parameter
# values) is only generated during synthesis, so the quoted-parameter guard lives
# in build_all.tcl right after synth_1, not here.
puts "BUILD_STAGE_1_OK"
