// cmd_shaper.v -- hardware command shaper for the Lite3 velocity stream.
//
// Why this lives in the PL and not on the host
// --------------------------------------------
// The host can command the dog through the safety gate at 50 Hz, but a host
// glitch (a re-scheduled thread, a GC pause, a fat-fingered CLI) can still emit
// a full-speed step in one period.  The dog then lurches.  This block bounds the
// rate of change *in hardware*: whatever the host asks for, the output moves
// toward it by at most DELTA_V mm/s (DELTA_W mrad/s) every STEP_CYCLES clocks.
//
// Interface
// ---------
// Plain synchronous ports (no AXI here): the AXI4-Lite register file in
// safety_gate.v writes cmd_* / pulses cmd_valid, and reads out_* / step_count.
//
// * cmd_valid is a single-cycle pulse that latches a new target.
// * out_* ramps toward the latched target; step_pulse marks cycles where an
//   output actually changed.
// * step_count is a free-running saturating counter of ramp steps (throughput
//   evidence for the report).
//
// All three axes use signed two's complement, matching the gate's mm/s and
// mrad/s registers.  Reset drives every output to zero, which is the same
// fail-safe level the watchdog produces.
`timescale 1ns / 1ps

module cmd_shaper #(
    parameter integer CLK_HZ = 27_777_778,
    parameter integer STEP_CYCLES = CLK_HZ / 1000,  // one ramp step per 1 ms
    parameter signed [17:0] DELTA_V = 18'sd20,      // 20 mm/s per step
    parameter signed [17:0] DELTA_W = 18'sd20       // 20 mrad/s per step
) (
    input  wire                 clk,
    input  wire                 rst,          // synchronous, active high
    input  wire signed [17:0]   cmd_vx,       // mm/s
    input  wire signed [17:0]   cmd_vy,       // mm/s
    input  wire signed [17:0]   cmd_wz,       // mrad/s
    input  wire                 cmd_valid,
    output reg  signed [17:0]   out_vx,
    output reg  signed [17:0]   out_vy,
    output reg  signed [17:0]   out_wz,
    output reg                  step_pulse,
    output reg  [31:0]          step_count
);

    reg signed [17:0] tgt_vx;
    reg signed [17:0] tgt_vy;
    reg signed [17:0] tgt_wz;
    reg [15:0] divider;

    // One ramp step: move `cur` toward `tgt` by at most `delta`.
    function signed [17:0] ramp;
        input signed [17:0] cur;
        input signed [17:0] tgt;
        input signed [17:0] delta;
        reg signed [18:0] diff;
        begin
            diff = $signed(tgt) - $signed(cur);
            if (diff > $signed({1'b0, delta}))
                ramp = cur + delta;
            else if (diff < -$signed({1'b0, delta}))
                ramp = cur - delta;
            else
                ramp = tgt;
        end
    endfunction

    always @(posedge clk) begin
        if (rst) begin
            tgt_vx     <= 18'sd0;
            tgt_vy     <= 18'sd0;
            tgt_wz     <= 18'sd0;
            out_vx     <= 18'sd0;
            out_vy     <= 18'sd0;
            out_wz     <= 18'sd0;
            divider    <= 16'd0;
            step_pulse <= 1'b0;
            step_count <= 32'd0;
        end else begin
            step_pulse <= 1'b0;

            if (cmd_valid) begin
                tgt_vx <= cmd_vx;
                tgt_vy <= cmd_vy;
                tgt_wz <= cmd_wz;
            end

            if (divider >= (STEP_CYCLES - 1)) begin
                divider <= 16'd0;
                if ((out_vx !== tgt_vx) || (out_vy !== tgt_vy) || (out_wz !== tgt_wz)) begin
                    out_vx <= ramp(out_vx, tgt_vx, DELTA_V);
                    out_vy <= ramp(out_vy, tgt_vy, DELTA_V);
                    out_wz <= ramp(out_wz, tgt_wz, DELTA_W);
                    step_pulse <= 1'b1;
                    if (step_count != 32'hFFFF_FFFF)
                        step_count <= step_count + 32'd1;
                end
            end else begin
                divider <= divider + 16'd1;
            end
        end
    end

endmodule
