module tb_full_adder;
    reg a, b, cin;
    wire sum, cout;
    reg expected_sum, expected_cout;
    integer failures;
    integer total_tests;
    integer i;

    full_adder dut (
        .a(a), .b(b), .cin(cin),
        .sum(sum), .cout(cout)
    );

    initial begin
        failures = 0;
        total_tests = 0;
        a = 0; b = 0; cin = 0;

        $dumpfile("logicforge_waveform.vcd");
        $dumpvars(0, tb_full_adder);

        for (i = 0; i < 8; i = i + 1) begin
            {a, b, cin} = i;
            #10;
            {expected_cout, expected_sum} = a + b + cin;
            total_tests = total_tests + 1;
            if (sum !== expected_sum || cout !== expected_cout) begin
                failures = failures + 1;
                $display("FAIL: a=%b b=%b cin=%b expected sum=%b cout=%b got sum=%b cout=%b",
                         a, b, cin, expected_sum, expected_cout, sum, cout);
            end else begin
                $display("PASS: a=%b b=%b cin=%b sum=%b cout=%b", a, b, cin, sum, cout);
            end
        end

        $display("Total tests: %0d, Failures: %0d", total_tests, failures);
        if (failures == 0)
            $display("OVERALL PASS");
        else
            $display("OVERALL FAIL");
        $finish;
    end
endmodule