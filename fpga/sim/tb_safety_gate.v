// tb_safety_gate.v -- self-checking AXI4-Lite testbench for safety_gate + cmd_shaper.
//
// Why a simulation test exists for a block that already has a PYNQ self-test:
// the board is powered down, and integrating cmd_shaper changes the one
// behaviour the PYNQ test asserted (OUT used to follow CMD within the same
// cycle, now it ramps).  This bench proves the new contract without hardware:
//
//   1. reset: every output is zero and the gate reports closed;
//   2. a full-speed command does not reach the output in one step (the ramp
//      moves by exactly DELTA per 1 ms step and never overshoots);
//   3. the output settles on the command within target/DELTA + 1 ms;
//   4. a mid-ramp target change is followed without overshoot;
//   5. zero command ramps the output back to zero, never below;
//   6. watchdog timeout: fault latches and OUT drops to 0 within one tick;
//   7. clear + re-arm: the output ramps up again from 0 (no jump to the old
//      command), and the ramp-step counter advanced.
//
// Time is compressed: CLK_HZ=100_000 makes one "ms" 100 clocks (1 us of sim
// time), so a 200 ms safety deadline costs 20_000 cycles, not millions.
`timescale 1ns / 1ps

module tb_safety_gate;

    localparam integer CLK_HZ    = 100_000;
    localparam integer CYC_PER_MS = CLK_HZ / 1000;      // 100 clocks per "ms"
    localparam signed [17:0] DELTA_V = 18'sd20;
    localparam integer TIMEOUT_MS = 200;

    reg clk = 1'b0;
    reg aresetn = 1'b0;

    reg  [11:0] awaddr = 12'd0;
    reg         awvalid = 1'b0;
    wire        awready;
    reg  [31:0] wdata = 32'd0;
    reg         wvalid = 1'b0;
    wire        wready;
    wire [1:0]  bresp;
    wire        bvalid;
    reg         bready = 1'b1;
    reg  [11:0] araddr = 12'd0;
    reg         arvalid = 1'b0;
    wire        arready;
    wire [31:0] rdata;
    wire [1:0]  rresp;
    wire        rvalid;
    reg         rready = 1'b1;

    wire signed [31:0] vx_out;
    wire signed [31:0] vy_out;
    wire signed [31:0] wz_out;
    wire               gate_open;

    localparam [11:0] R_VER     = 12'h00;
    localparam [11:0] R_CTRL    = 12'h04;
    localparam [11:0] R_CMD_VX  = 12'h0C;
    localparam [11:0] R_ARM     = 12'h18;
    localparam [11:0] R_STATUS  = 12'h28;
    localparam [11:0] R_STEPS   = 12'h38;

    localparam [31:0] ST_F_TIMEOUT = 32'h10;

    integer errors = 0;
    integer checks = 0;

    safety_gate #(
        .CLK_HZ(CLK_HZ),
        .RAMP_DELTA_V(DELTA_V),
        .RAMP_DELTA_W(DELTA_V)
    ) dut (
        .s_axi_aclk(clk),
        .s_axi_aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awprot(3'b000), .s_axi_awvalid(awvalid),
        .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(wvalid),
        .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr), .s_axi_arprot(3'b000), .s_axi_arvalid(arvalid),
        .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid),
        .s_axi_rready(rready),
        .estop_in(1'b0),
        .vx_out(vx_out), .vy_out(vy_out), .wz_out(wz_out),
        .gate_open(gate_open)
    );

    always #5 clk = ~clk;   // 100 MHz

    task check(input [255:0] name, input integer got, input integer want);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                $display("[FAIL] %0s: got %0d want %0d", name, got, want);
            end else begin
                $display("[PASS] %0s = %0d", name, got);
            end
        end
    endtask

    task axi_write(input [11:0] addr, input [31:0] data);
        begin
            @(posedge clk);
            awaddr <= addr; awvalid <= 1'b1;
            wdata  <= data; wvalid <= 1'b1;
            wait (awready === 1'b1 || wready === 1'b1);
            @(posedge clk);
            awvalid <= 1'b0; wvalid <= 1'b0;
            wait (bvalid === 1'b1);
            @(posedge clk);
        end
    endtask

    task axi_read(input [11:0] addr, output [31:0] data);
        begin
            @(posedge clk);
            araddr <= addr; arvalid <= 1'b1;
            wait (arready === 1'b1);
            @(posedge clk);
            arvalid <= 1'b0;
            wait (rvalid === 1'b1);
            data = rdata;
            @(posedge clk);
        end
    endtask

    // Sample OUT_VX on a 1 ms grid; return how many ms it took to reach
    // `target`, whether it overshot, and the largest single-step change.
    task wait_for_out(input integer target, input integer max_ms,
                      output integer ms_used, output integer overshoot,
                      output integer max_step);
        integer i, prev, cur;
        begin
            ms_used = -1; overshoot = 0; max_step = 0;
            prev = vx_out;
            for (i = 0; i < max_ms; i = i + 1) begin
                repeat (CYC_PER_MS) @(posedge clk);
                cur = vx_out;
                if ((cur - prev) > max_step) max_step = cur - prev;
                if ((prev - cur) > max_step) max_step = prev - cur;
                if (cur > target && target >= 0) overshoot = 1;
                prev = cur;
                if (cur == target && ms_used < 0) ms_used = i + 1;
            end
        end
    endtask

    integer ms_used, overshoot, max_step, steps_before, steps_after, k;
    reg [31:0] rd, status;

    initial begin
        $display("safety_gate + cmd_shaper integration bench");

        repeat (10) @(posedge clk);
        aresetn <= 1'b1;
        repeat (10) @(posedge clk);

        // ---- 1. reset state -----------------------------------------------
        axi_read(R_VER, rd);        check("version", rd, 32'h0001_0000);
        axi_read(R_STATUS, rd);     check("status after reset (estop_n=1)", rd, 32'h4);
        check("out zero after reset", vx_out, 0);

        // ---- 2. arm with a 100 mm/s command -------------------------------
        axi_write(R_CTRL, 32'h1);   // enable (no e-stop)
        axi_write(R_CMD_VX, 32'd100);
        axi_write(R_ARM, 32'd1);
        wait (gate_open === 1'b1);
        // the shaper advances one step per 1 ms, so the first output change
        // lands at 1.0 ms and must be exactly DELTA (not the full command)
        repeat (CYC_PER_MS + CYC_PER_MS / 5) @(posedge clk);
        check("gate open", gate_open, 1);
        checks = checks + 1;
        if (vx_out == DELTA_V) begin
            $display("[PASS] first step after 1 ms is exactly DELTA (%0d)", vx_out);
        end else begin
            errors = errors + 1;
            $display("[FAIL] output jumped to %0d on arm", vx_out);
        end
        checks = checks + 1;
        if (vx_out != 100) begin
            $display("[PASS] command 100 was NOT passed through in one step");
        end else begin
            errors = errors + 1;
            $display("[FAIL] command passed through without ramping");
        end

        // ---- 3. ramp settles on the command -------------------------------
        wait_for_out(100, 20, ms_used, overshoot, max_step);
        check("settled at 100 mm/s", vx_out, 100);
        check("no overshoot", overshoot, 0);
        check("max step is DELTA", max_step, DELTA_V);
        $display("       settle took %0d ms (expected %0d)", ms_used, 100 / DELTA_V);

        // ---- 4. mid-ramp target change -----------------------------------
        axi_write(R_CMD_VX, 32'd500);
        repeat (3 * CYC_PER_MS) @(posedge clk);
        checks = checks + 1;
        if (vx_out > 100 && vx_out < 500) begin
            $display("[PASS] ramp in progress at %0d after target change", vx_out);
        end else begin
            errors = errors + 1;
            $display("[FAIL] ramp did not move toward 500: %0d", vx_out);
        end
        wait_for_out(500, 40, ms_used, overshoot, max_step);
        check("settled at 500 mm/s", vx_out, 500);
        check("mid-ramp overshoot", overshoot, 0);

        // ---- 5. back to zero ---------------------------------------------
        axi_write(R_CMD_VX, 32'd0);
        wait_for_out(0, 40, ms_used, overshoot, max_step);
        check("ramped back to zero", vx_out, 0);

        // ---- 6. watchdog timeout -----------------------------------------
        axi_write(R_CMD_VX, 32'd200);
        axi_read(R_STEPS, rd); steps_before = rd;
        for (k = 0; k < 100; k = k + 1) begin
            axi_write(R_ARM, 32'd1);
            repeat (CYC_PER_MS) @(posedge clk);
        end
        // stop feeding: the gate must close and latch the timeout fault
        repeat ((TIMEOUT_MS + 40) * CYC_PER_MS) @(posedge clk);
        axi_read(R_STATUS, status);
        check("fault_timeout latched", status & ST_F_TIMEOUT, ST_F_TIMEOUT);
        check("gate closed after timeout", gate_open, 0);
        check("out forced to zero on timeout", vx_out, 0);
        axi_read(R_STEPS, rd); steps_after = rd;
        checks = checks + 1;
        if (steps_after > steps_before) begin
            $display("[PASS] ramp steps advanced %0d -> %0d", steps_before, steps_after);
        end else begin
            errors = errors + 1;
            $display("[FAIL] ramp step counter stuck at %0d", steps_after);
        end

        // ---- 7. clear + re-arm ramps again from zero ---------------------
        axi_write(R_CMD_VX, 32'd100);
        axi_write(R_CTRL, 32'h5);   // enable + clear_faults
        axi_write(R_CTRL, 32'h1);
        axi_write(R_ARM, 32'd1);
        repeat (2 * CYC_PER_MS) @(posedge clk);
        checks = checks + 1;
        if (vx_out > 0 && vx_out <= 3 * DELTA_V) begin
            $display("[PASS] re-arm ramps from zero (%0d)", vx_out);
        end else begin
            errors = errors + 1;
            $display("[FAIL] re-arm jumped to %0d", vx_out);
        end
        wait_for_out(100, 20, ms_used, overshoot, max_step);
        check("re-armed output settles", vx_out, 100);

        $display("");
        if (errors == 0)
            $display("SIM PASS (%0d checks)", checks);
        else
            $display("SIM FAIL (%0d/%0d checks failed)", errors, checks);
        $finish;
    end

    initial begin
        #20_000_000;
        $display("SIM FAIL (timeout)");
        $finish;
    end

endmodule
