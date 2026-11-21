module tb_up_counter;
    reg clk, rst;
    wire [3:0] count;
    reg [3:0] expected_count;
    integer failures;
    integer total_tests;
    integer i;

    up_counter dut (.clk(clk), .rst(rst), .count(count));

    always #5 clk = ~clk;

    initial begin
        failures = 0;
        total_tests = 0;
        clk = 0;
        rst = 1;

        $dumpfile("logicforge_waveform.vcd");
        $dumpvars(0, tb_up_counter);

        @(posedge clk); #1;
        rst = 0;
        expected_count = 4'd0;

        for (i = 0; i < 20; i = i + 1) begin
            total_tests = total_tests + 1;
            if (count !== expected_count) begin
                failures = failures + 1;
                $display("FAIL: cycle=%0d expected count=%0d got count=%0d", i, expected_count, count);
            end else begin
                $display("PASS: cycle=%0d count=%0d", i, count);
            end
            @(posedge clk); #1;
            expected_count = expected_count + 4'd1;
        end

        $display("Total tests: %0d, Failures: %0d", total_tests, failures);
        if (failures == 0)
            $display("OVERALL PASS");
        else
            $display("OVERALL FAIL");
        $finish;
    end
endmodule
