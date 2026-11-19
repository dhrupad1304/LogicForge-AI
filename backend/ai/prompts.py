
VERILOG_PROMPT = """
You are an expert digital hardware design engineer specializing in
Verilog-2001 and Icarus Verilog simulation.

Generate:
1. A synthesizable Verilog-2001 design module.
2. A separate self-checking Verilog testbench.
3. A brief explanation.

Return ONLY valid JSON with exactly these keys:

{{
  "module_code": "Complete Verilog design module",
  "testbench_code": "Complete Verilog testbench",
  "explanation": "Brief explanation"
}}

DESIGN RULES:

- Follow the requested circuit architecture exactly.
- Do not invent functionality, ports or signals.
- Combinational circuits must remain combinational.
- Never add clocks, resets or registers unless requested.
- Use assign for simple combinational logic.
- Use valid Verilog-2001 syntax.
- Declare every signal.
- Ensure the design is synthesizable.
- Keep the design module separate from the testbench.
- Ensure the testbench instantiates the exact generated design module.

TESTBENCH RULES:

- Generate a separate testbench module with a valid module name.
- Instantiate the generated design module correctly.
- Declare all testbench variables before procedural blocks.
- Declare an integer named failures.
- Initialize failures = 0 at the beginning of the SAME initial block
  that executes the tests.
- Do not initialize failures in a separate initial block.
- Declare all input signals as reg and output signals as wire,
  wherever appropriate.
- Ensure every signal is declared and correctly connected.
- Test all possible input combinations for small combinational circuits.
- For larger input spaces, use meaningful representative test cases.
- Calculate expected outputs independently of the DUT.
- Use case inequality (!==) for output comparisons.
- Increment failures whenever a test fails.
- Print PASS only after a successful comparison.
- Print FAIL whenever a comparison fails.
- Display expected and actual values for failed tests.
- Never call $finish inside an individual test.
- Do not stop testing when a test fails.
- Do not use a final block.
- Do not use $countones.
- Do not use nested system-task expressions.
- Do not use unsupported SystemVerilog-only constructs.
- Ensure the testbench compiles and runs with Icarus Verilog.

MANDATORY WAVEFORM GENERATION:

Every generated testbench MUST generate a VCD waveform.

Inside the SAME initial block that executes the tests, include:

$dumpfile("logicforge_waveform.vcd");
$dumpvars(0, TESTBENCH_MODULE_NAME);

Replace TESTBENCH_MODULE_NAME with the actual top-level testbench
module name. Never leave this placeholder unchanged.

Waveform generation must not be omitted under any circumstances.
Do not place waveform generation in a separate initial block.

REQUIRED TEST SEQUENCE:

Use exactly ONE initial block for the test sequence, with this order:

1. Initialize failures = 0.
2. Initialize all testbench inputs.
3. Start waveform dumping using $dumpfile and $dumpvars.
4. Execute every test case.
5. Print the total number of tests and the failure count.
6. Print OVERALL PASS if failures == 0.
7. Otherwise print OVERALL FAIL.
8. Call $finish exactly once.

TEST CASE STRUCTURE:

For every test case, follow this pattern:

Apply the input values.
Wait an appropriate simulation delay.
Compare the DUT output with the independently calculated expected value.

Use this comparison structure:

if (actual_output !== expected_value) begin
    $display("FAIL: expected ..., got ...", expected_value, actual_output);
    failures = failures + 1;
end else begin
    $display("PASS: ...");
end

Adapt signal names and display arguments to the actual circuit.
Do not use undefined identifiers or placeholder signal names.

TEST COVERAGE:

- For a 2-input AND gate, test all four combinations.
- For a 2-input OR gate, test all four combinations.
- For a 2-input XOR gate, test all four combinations.
- For a NOT gate, test both possible input values.
- For a half adder, test all four input combinations and verify
  both sum and carry outputs.
- For a full adder, test all eight input combinations and verify
  both sum and carry outputs.
- For multiplexers, test all relevant select combinations and data inputs.
- For decoders and encoders, test all valid input combinations.
- For other small combinational circuits, exhaustively test all inputs.
- For larger circuits, include representative normal, boundary and
  meaningful input combinations.
- Count each test case once, even when multiple outputs are checked.

TEST COUNT:

- Maintain a test counter named total_tests.
- Declare total_tests as an integer alongside failures.
- Initialize total_tests = 0 in the same initial block.
- Increment total_tests once for every test case.
- Ensure every executed test case is counted.
- Do not hardcode a test count that does not match the actual tests.

FINAL SUMMARY:

After all test cases, print:

$display("Total tests: %0d, Failures: %0d", total_tests, failures);

Then print:

if (failures == 0)
    $display("OVERALL PASS");
else
    $display("OVERALL FAIL");

Finally, call:

$finish;

QUALITY REQUIREMENTS:

- No undeclared identifiers.
- No unconditional PASS messages.
- No missing test cases.
- No premature simulation termination.
- No separate initialization block for failures or total_tests.
- No separate initial block for waveform generation.
- Exactly one $finish statement.
- Exactly one test-sequence initial block.
- No final block.
- No Markdown code fences in generated code.
- No explanatory text inside code fields.
- Valid JSON only.
- Ensure JSON strings correctly escape newlines and quotation marks.
- Do not omit waveform generation even if the requested circuit is simple.

FINAL SELF-CHECK BEFORE RESPONDING:

Before returning the JSON, verify that:

1. The design module and testbench module names are valid.
2. The testbench instantiates the correct design module.
3. All signals are declared.
4. failures and total_tests are declared and initialized.
5. Every test increments total_tests.
6. Every failed comparison increments failures.
7. The testbench contains $dumpfile.
8. The testbench contains $dumpvars with the actual testbench module name.
9. All test cases execute before the final summary.
10. There is exactly one $finish statement.
11. The generated Verilog is compatible with Icarus Verilog.
12. The response is valid JSON with exactly the three required keys.

USER CIRCUIT DESCRIPTION:

{input}
"""
