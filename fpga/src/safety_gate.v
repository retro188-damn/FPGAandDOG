// -----------------------------------------------------------------------------
// safety_gate.v  --  AXI4-Lite safety gate for the Lite3 command path
//
// Purpose
//   A host (PS Linux or our own PL logic) programs a velocity command and must
//   re-arm it periodically. If the host stops re-arming (crash, reboot, network
//   loss) the gate forces the command output to zero within TIMEOUT_MS and
//   latches a sticky fault bit. A software or hardware e-stop forces the same.
//
// Register map (32-bit, byte offsets)
//   0x00 VER        RO  0x0001_0000
//   0x04 CTRL       RW  bit0 = enable, bit1 = estop (1 = stop), bit2 = clear_faults
//   0x08 TIMEOUT_MS RW  watchdog timeout in ms, default 200, 0 -> 200, max 60000
//   0x0C CMD_VX     RW  commanded forward velocity, signed, mm/s
//   0x10 CMD_VY     RW  commanded lateral velocity, signed, mm/s
//   0x14 CMD_WZ     RW  commanded yaw rate, signed, mrad/s
//   0x18 CMD_ARM    RW  write any value: latch CMD_* and re-arm the watchdog
//   0x1C OUT_VX     RO  gated forward velocity, signed, mm/s
//   0x20 OUT_VY     RO  gated lateral velocity, signed, mm/s
//   0x24 OUT_WZ     RO  gated yaw rate, signed, mrad/s
//   0x28 STATUS     RO  bit0 = gate open, bit1 = watchdog armed, bit2 = estop,
//                       bit3 = enabled, bit4 = fault_timeout (sticky),
//                       bit5 = fault_estop (sticky)
//   0x2C STATS      RO  [15:0] timeout count, [31:16] accepted arm count
//   0x30 HEARTBEAT  RO  free-running cycle counter (timebase self-check)
//   0x34 RELOADS    RO  number of watchdog reloads performed
//   0x38 RAMP_STEPS RO  cmd_shaper step counter (ramp throughput evidence)
//
// Clock
//   s_axi_aclk is PS FCLK_CLK0. On this board the IO PLL runs at 666.667 MHz
//   while PYNQ assumes 1000 MHz, so the clock actually delivered for a nominal
//   "41.6667 MHz" setting measures 27.778 MHz (5.0 s heartbeat count:
//   139032028 cycles / 5.0052 s). CLK_HZ below is that measured value; the
//   driver re-measures the heartbeat rate at load time and refuses to open the
//   gate if it deviates by more than 5 %, so a wrong constant cannot silently
//   stretch the safety deadline.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module safety_gate #(
    parameter integer CLK_HZ = 27_777_778,
    // Keep the ramp deltas as plain integers, never as signed vectors.
    // Vivado's block-design "module reference" wrapper emits a *vector*-typed
    // parameter as a quoted string, so `signed [17:0] ... = 18'sd20` reaches
    // synthesis as the ASCII code of its own decimal text ("20" = 0x3230 =
    // 12848) and the shaper jumps straight to the target in one 1 ms tick.
    // That is exactly what the 2026-10-26 bitstream did: RTL simulation ramped
    // in 5 ms steps of 20, the board moved 0 -> 500 in a single step and
    // gate_test.py failed "ramp step bounded by DELTA". Integer parameters are
    // emitted unquoted (see CLK_HZ in the generated wrapper).
    parameter integer RAMP_DELTA_V = 20,  // mm/s per 1 ms step
    parameter integer RAMP_DELTA_W = 20   // mrad/s per 1 ms step
) (
    // AXI4-Lite slave
    input  wire        s_axi_aclk,
    input  wire        s_axi_aresetn,
    input  wire [11:0] s_axi_awaddr,
    input  wire [2:0]  s_axi_awprot,
    input  wire        s_axi_awvalid,
    output reg         s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output reg         s_axi_wready,
    output reg  [1:0]  s_axi_bresp,
    output reg         s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [11:0] s_axi_araddr,
    input  wire [2:0]  s_axi_arprot,
    input  wire        s_axi_arvalid,
    output reg         s_axi_arready,
    output reg  [31:0] s_axi_rdata,
    output reg  [1:0]  s_axi_rresp,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready,
    // Gate side
    input  wire        estop_in,      // external active-high e-stop input (tied 0)
    output wire signed [31:0] vx_out, // gated command, mm/s
    output wire signed [31:0] vy_out, // gated command, mm/s
    output wire signed [31:0] wz_out, // gated command, mrad/s
    output wire        gate_open
);

    localparam [31:0] VERSION = 32'h0001_0000;
    localparam integer DEFAULT_TIMEOUT_MS = 200;
    localparam integer MAX_TIMEOUT_MS = 60000;
    localparam [31:0] CYCLES_PER_MS = CLK_HZ / 1000;

    // Register file
    reg        r_enable;
    reg        r_estop;
    reg [31:0] r_timeout_ms;
    reg signed [31:0] r_cmd_vx;
    reg signed [31:0] r_cmd_vy;
    reg signed [31:0] r_cmd_wz;

    // Gated output registers
    reg signed [31:0] r_out_vx;
    reg signed [31:0] r_out_vy;
    reg signed [31:0] r_out_wz;

    // Fault bookkeeping
    reg        f_timeout;     // sticky
    reg        f_estop;       // sticky
    reg [15:0] timeout_count;
    reg [15:0] arm_count;
    reg [15:0] reload_count;
    reg [31:0] heartbeat;

    // Watchdog state (written only by the watchdog block below)
    reg [31:0] wd_cycles_left;
    reg        wd_armed;

    wire [31:0] timeout_cycles = (r_timeout_ms == 32'd0)
                               ? DEFAULT_TIMEOUT_MS * CYCLES_PER_MS
                               : r_timeout_ms * CYCLES_PER_MS;

    wire estop_any = r_estop | estop_in | f_estop;
    assign gate_open = r_enable & wd_armed & ~estop_any & ~f_timeout;

    // ----------------------------------------------------------------
    // Gated outputs, rate-limited in hardware (cmd_shaper).
    //
    // The host is allowed to command a full-speed step at any 50 Hz tick; a
    // paused thread or a fat-fingered CLI must not become a mechanical jerk.
    // The shaper bounds |d(out)/dt| to RAMP_DELTA_* per 1 ms, so the dog sees
    // a ramp no matter what arrives on the wire.  When the gate closes the
    // shaper is driven toward zero and the outputs are forced to zero, which
    // keeps the watchdog behaviour identical to before this block existed.
    // ----------------------------------------------------------------
    wire signed [17:0] shp_cmd_vx = (r_cmd_vx > 32'sd131071) ? 18'sd131071 :
                                    (r_cmd_vx < -32'sd131071) ? -18'sd131071 :
                                    r_cmd_vx[17:0];
    wire signed [17:0] shp_cmd_vy = (r_cmd_vy > 32'sd131071) ? 18'sd131071 :
                                    (r_cmd_vy < -32'sd131071) ? -18'sd131071 :
                                    r_cmd_vy[17:0];
    wire signed [17:0] shp_cmd_wz = (r_cmd_wz > 32'sd131071) ? 18'sd131071 :
                                    (r_cmd_wz < -32'sd131071) ? -18'sd131071 :
                                    r_cmd_wz[17:0];

    wire signed [17:0] shp_out_vx;
    wire signed [17:0] shp_out_vy;
    wire signed [17:0] shp_out_wz;
    wire               shp_step;
    wire [31:0]        shp_steps;

    // cmd_valid is a level here: the shaper latches the newest target on every
    // clock, which removes the arm-vs-command ordering hazard (a pulse driven
    // by the ARM write would be seen one cycle before gate_open rises).
    cmd_shaper #(
        .CLK_HZ(CLK_HZ),
        .STEP_CYCLES(CLK_HZ / 1000),
        .DELTA_V(RAMP_DELTA_V),
        .DELTA_W(RAMP_DELTA_W)
    ) u_shaper (
        .clk(s_axi_aclk),
        .rst(~s_axi_aresetn),
        .cmd_vx(gate_open ? shp_cmd_vx : 18'sd0),
        .cmd_vy(gate_open ? shp_cmd_vy : 18'sd0),
        .cmd_wz(gate_open ? shp_cmd_wz : 18'sd0),
        .cmd_valid(1'b1),
        .out_vx(shp_out_vx),
        .out_vy(shp_out_vy),
        .out_wz(shp_out_wz),
        .step_pulse(shp_step),
        .step_count(shp_steps)
    );

    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            r_out_vx <= 32'sd0;
            r_out_vy <= 32'sd0;
            r_out_wz <= 32'sd0;
        end else if (!gate_open) begin
            r_out_vx <= 32'sd0;
            r_out_vy <= 32'sd0;
            r_out_wz <= 32'sd0;
        end else begin
            r_out_vx <= {{14{shp_out_vx[17]}}, shp_out_vx};
            r_out_vy <= {{14{shp_out_vy[17]}}, shp_out_vy};
            r_out_wz <= {{14{shp_out_wz[17]}}, shp_out_wz};
        end
    end

    assign vx_out = r_out_vx;
    assign vy_out = r_out_vy;
    assign wz_out = r_out_wz;

    // ----------------------------------------------------------------
    // AXI4-Lite write channel
    // ----------------------------------------------------------------
    reg [11:0] wr_addr;
    reg [31:0] wr_data;
    reg        wr_active;

    wire wr_fire = (!wr_active) && s_axi_awvalid && s_axi_wvalid;

    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            s_axi_awready <= 1'b0;
            s_axi_wready  <= 1'b0;
            s_axi_bvalid  <= 1'b0;
            s_axi_bresp   <= 2'b00;
            wr_active     <= 1'b0;
            wr_addr       <= 12'd0;
            wr_data       <= 32'd0;
        end else begin
            s_axi_awready <= 1'b0;
            s_axi_wready  <= 1'b0;
            s_axi_bvalid  <= (s_axi_bvalid && !s_axi_bready) ? 1'b1 : 1'b0;

            if (wr_fire) begin
                wr_addr       <= s_axi_awaddr;
                wr_data       <= s_axi_wdata;
                s_axi_awready <= 1'b1;
                s_axi_wready  <= 1'b1;
                wr_active     <= 1'b1;
            end

            if (wr_active) begin
                wr_active    <= 1'b0;
                s_axi_bvalid <= 1'b1;
                s_axi_bresp  <= 2'b00;
            end
        end
    end

    // ----------------------------------------------------------------
    // AXI4-Lite read channel
    // ----------------------------------------------------------------
    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            s_axi_arready <= 1'b0;
            s_axi_rvalid  <= 1'b0;
            s_axi_rdata   <= 32'd0;
            s_axi_rresp   <= 2'b00;
        end else begin
            s_axi_arready <= 1'b0;
            s_axi_rvalid  <= (s_axi_rvalid && !s_axi_rready) ? 1'b1 : 1'b0;

            if (!s_axi_arready && s_axi_arvalid && !s_axi_rvalid) begin
                s_axi_arready <= 1'b1;
                s_axi_rvalid  <= 1'b1;
                s_axi_rresp   <= 2'b00;
                case (s_axi_araddr[7:0])
                    8'h00:   s_axi_rdata <= VERSION;
                    8'h04:   s_axi_rdata <= {29'd0, r_estop, r_enable};
                    8'h08:   s_axi_rdata <= r_timeout_ms;
                    8'h0C:   s_axi_rdata <= r_cmd_vx;
                    8'h10:   s_axi_rdata <= r_cmd_vy;
                    8'h14:   s_axi_rdata <= r_cmd_wz;
                    8'h1C:   s_axi_rdata <= r_out_vx;
                    8'h20:   s_axi_rdata <= r_out_vy;
                    8'h24:   s_axi_rdata <= r_out_wz;
                    8'h28:   s_axi_rdata <= {26'd0, f_estop, f_timeout, r_enable,
                                             ~estop_any, wd_armed, gate_open};
                    8'h2C:   s_axi_rdata <= {arm_count, timeout_count};
                    8'h30:   s_axi_rdata <= heartbeat;
                    8'h34:   s_axi_rdata <= {16'd0, reload_count};
                    8'h38:   s_axi_rdata <= shp_steps;   // ramp steps (throughput evidence)
                    default: s_axi_rdata <= 32'hDEAD_BEEF;
                endcase
            end
        end
    end

    // ----------------------------------------------------------------
    // Register write decode (one-cycle pulse from the write FSM)
    // ----------------------------------------------------------------
    wire        wr_pulse  = wr_active;
    wire        wr_is_arm = wr_pulse && (wr_addr[7:0] == 8'h18);
    reg         arm_req;      // registered: the watchdog block acts one cycle later
    reg         clr_faults;   // registered clear-faults request

    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            r_enable      <= 1'b0;
            r_estop       <= 1'b0;
            r_timeout_ms  <= DEFAULT_TIMEOUT_MS;
            r_cmd_vx      <= 32'sd0;
            r_cmd_vy      <= 32'sd0;
            r_cmd_wz      <= 32'sd0;
            arm_req       <= 1'b0;
            clr_faults    <= 1'b0;
            arm_count     <= 16'd0;
            heartbeat     <= 32'd0;
        end else begin
            heartbeat <= heartbeat + 32'd1;
            arm_req   <= 1'b0;
            clr_faults <= 1'b0;

            if (wr_pulse) begin
                case (wr_addr[7:0])
                    8'h04: begin
                        r_enable <= wr_data[0];
                        r_estop  <= wr_data[1];
                        if (wr_data[2]) begin
                            clr_faults <= 1'b1;
                        end
                    end
                    8'h08: r_timeout_ms <= (wr_data == 32'd0) ? DEFAULT_TIMEOUT_MS :
                                      (wr_data > MAX_TIMEOUT_MS) ? MAX_TIMEOUT_MS : wr_data;
                    8'h0C: r_cmd_vx <= wr_data;
                    8'h10: r_cmd_vy <= wr_data;
                    8'h14: r_cmd_wz <= wr_data;
                    8'h18: begin
                        arm_req <= 1'b1;
                        if (arm_count != 16'hFFFF) begin
                            arm_count <= arm_count + 16'd1;
                        end
                    end
                    default: ; // read-only or unused address
                endcase
            end
        end
    end

    // ----------------------------------------------------------------
    // Watchdog: the ONLY block that drives wd_armed / wd_cycles_left.
    // A reload and a countdown never compete here: the reload branch takes
    // priority explicitly and no other block assigns these registers.
    // ----------------------------------------------------------------
    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            wd_armed       <= 1'b0;
            wd_cycles_left <= 32'd0;
            f_timeout      <= 1'b0;
            f_estop        <= 1'b0;
            timeout_count  <= 16'd0;
            reload_count   <= 16'd0;
        end else begin
            if (clr_faults) begin
                f_timeout <= 1'b0;
                f_estop   <= 1'b0;
            end

            if (arm_req) begin
                // latch a fresh deadline and re-arm
                wd_armed       <= 1'b1;
                wd_cycles_left <= timeout_cycles;
                if (reload_count != 16'hFFFF) begin
                    reload_count <= reload_count + 16'd1;
                end
            end else if (!r_enable) begin
                // gate disabled: disarm, so a later arm always reloads
                wd_armed <= 1'b0;
            end else if (wd_armed) begin
                if (wd_cycles_left == 32'd0) begin
                    wd_armed  <= 1'b0;
                    f_timeout <= 1'b1;
                    if (timeout_count != 16'hFFFF) begin
                        timeout_count <= timeout_count + 16'd1;
                    end
                end else begin
                    wd_cycles_left <= wd_cycles_left - 32'd1;
                end
            end

            // sticky external e-stop
            if (estop_in) begin
                f_estop <= 1'b1;
            end
        end
    end


endmodule
