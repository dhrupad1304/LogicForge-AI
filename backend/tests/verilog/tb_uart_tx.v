// Self-checking testbench: sends several bytes and decodes the serial line
// like a UART receiver (sampling the middle of every bit).
module tb_uart_tx;
    localparam CPB = 8;  // small CLKS_PER_BIT keeps the simulation short

    reg        clk;
    reg        rst;
    reg        tx_start;
    reg  [7:0] data_in;
    wire       tx;
    wire       tx_busy;
    wire       tx_done;

    reg  [7:0] vectors [0:5];
    reg  [7:0] sent;
    reg  [7:0] received;
    reg        start_ok;
    reg        stop_ok;
    reg        busy_ok;
    integer    failures;
    integer    total_tests;
    integer    k;
    integer    b;

    uart_tx #(.CLKS_PER_BIT(CPB)) dut (
        .clk(clk),
        .rst(rst),
        .tx_start(tx_start),
        .data_in(data_in),
        .tx(tx),
        .tx_busy(tx_busy),
        .tx_done(tx_done)
    );

    always #5 clk = ~clk;

    // Watchdog: a broken transmitter must FAIL, not hang the simulation.
    // Normal run: 6 frames x 10 bits x CPB clocks x 10 ns, well under this.
    initial begin
        #20000;
        failures = failures + 1;
        $display("FAIL: timeout | the transmitter never completed all frames");
        $display("Total tests: %0d, Failures: %0d", total_tests, failures);
        $display("OVERALL FAIL");
        $finish;
    end

    initial begin
        failures    = 0;
        total_tests = 0;
        clk         = 0;
        rst         = 1;
        tx_start    = 0;
        data_in     = 8'd0;
        vectors[0]  = 8'h55;
        vectors[1]  = 8'hA3;
        vectors[2]  = 8'h00;
        vectors[3]  = 8'hFF;
        vectors[4]  = 8'h0F;
        vectors[5]  = 8'h81;

        $dumpfile("logicforge_waveform.vcd");
        $dumpvars(0, tb_uart_tx);

        repeat (2) @(posedge clk);
        #1 rst = 0;

        // The line must idle high and not busy after reset.
        total_tests = total_tests + 1;
        if (tx !== 1'b1 || tx_busy !== 1'b0) begin
            failures = failures + 1;
            $display("FAIL: idle after reset | expected tx=1 busy=0 | got tx=%b busy=%b", tx, tx_busy);
        end else begin
            $display("PASS: idle after reset | tx=1 busy=0");
        end

        for (k = 0; k < 6; k = k + 1) begin
            sent = vectors[k];

            @(negedge clk);
            data_in  = sent;
            tx_start = 1;
            @(negedge clk);
            tx_start = 0;

            // Receiver: find the start bit, then sample mid-bit.
            wait (tx == 1'b0);
            repeat (CPB / 2) @(posedge clk);
            start_ok = (tx == 1'b0);
            busy_ok  = (tx_busy == 1'b1);
            for (b = 0; b < 8; b = b + 1) begin
                repeat (CPB) @(posedge clk);
                received[b] = tx;
            end
            repeat (CPB) @(posedge clk);
            stop_ok = (tx == 1'b1);

            wait (tx_done == 1'b1);
            @(posedge clk); #1;

            total_tests = total_tests + 1;
            if (!start_ok || !stop_ok || !busy_ok || received !== sent) begin
                failures = failures + 1;
                $display("FAIL: byte %0d | sent=%h received=%h start=%b stop=%b busy=%b",
                         k, sent, received, start_ok, stop_ok, busy_ok);
            end else begin
                $display("PASS: byte %0d | sent=%h received=%h", k, sent, received);
            end
        end

        // Busy must clear after the last frame.
        total_tests = total_tests + 1;
        if (tx_busy !== 1'b0 || tx !== 1'b1) begin
            failures = failures + 1;
            $display("FAIL: idle after last frame | got tx=%b busy=%b", tx, tx_busy);
        end else begin
            $display("PASS: idle after last frame | tx=1 busy=0");
        end

        $display("Total tests: %0d, Failures: %0d", total_tests, failures);
        if (failures == 0)
            $display("OVERALL PASS");
        else
            $display("OVERALL FAIL");
        $finish;
    end

endmodule
