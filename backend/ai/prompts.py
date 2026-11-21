"""
Prompt templates for LogicForge AI.

Templates use plain single braces. Fill them with fill_prompt(), which uses
str.replace, so Verilog concatenations like {a, b, cin} and user text
containing braces are always safe. Never call .format() on these templates.

The prompts are kept short on purpose: the local model is small and follows
a concrete example far more reliably than a long list of rules.
"""


import re


def fill_prompt(template: str, **values: str) -> str:
    """
    Replace {name} placeholders in one pass without using format().
    Only the given names are replaced; other braces are left untouched, and
    inserted text is never scanned again for placeholders.
    """

    if not values:
        return template

    pattern = re.compile(
        r"\{(" + "|".join(re.escape(name) for name in values) + r")\}"
    )
    return pattern.sub(lambda match: values[match.group(1)], template)


# Shared rules and a reference testbench that is verified to compile and pass
# in Icarus Verilog 13 (see tests in the repo history / README).
TESTBENCH_RULES = """
TESTBENCH RULES (Verilog-2001, must compile with Icarus Verilog):
- Testbench module name: tb_<design module name>. No ports.
- Declare EVERY identifier before the initial block:
  reg for each DUT input, wire for each DUT output (same widths as the DUT),
  reg for each expected value (same width as the output it checks),
  integer failures; integer total_tests; integer i; (loop counter).
- Instantiate the DUT with named port connections: .port(signal).
- Exactly ONE initial begin ... end block. No tasks, functions, fork,
  assert, final, logic, int, bit, or other SystemVerilog.
- Inside the initial block, in this order:
  1. failures = 0; total_tests = 0; set every input to 0.
  2. $dumpfile("logicforge_waveform.vcd"); $dumpvars(0, tb_<name>);
  3. Run the tests.
  4. $display("Total tests: %0d, Failures: %0d", total_tests, failures);
  5. if (failures == 0) $display("OVERALL PASS"); else $display("OVERALL FAIL");
  6. $finish;   (exactly once, at the very end)
- Combinational designs with N total input bits where N <= 10: test ALL
  2**N combinations with ONE for loop:
    for (i = 0; i < 2**N; i = i + 1) begin
        {all inputs concatenated} = i;
        #10;
        ...
    end
  Write the numeric value of 2**N (for example 8, 16, 256).
- More than 10 input bits: test zero, all ones, single-bit patterns and a
  few typical values; still increment total_tests for each.
- Sequential designs (a clk input): follow SEQUENTIAL EXAMPLE below. Add
  `always #5 clk = ~clk;` outside the initial block, assert reset for one
  clock, then keep a reference model (expected value) in the testbench and
  update it after every @(posedge clk); #1;
  The reference model must implement THIS design's behaviour, not the
  example's: a counter adds 1, a D flip-flop captures d
  (expected_q = d sampled before the edge), a shift register shifts, etc.
  Change the data inputs inside the loop (e.g. d = i[0];) so they are tested.
- Identifiers start with a letter or underscore: mux4to1, not 4_to_1_mux.
- Sized literals use valid digits for their base: 4'b1111 or 4'd15,
  never 4'b15. Prefer decimal (4'd15) for numbers.
- For every test: apply inputs, wait #10, compute the expected value from
  the INPUTS using Verilog operators (never from DUT outputs), then
  total_tests = total_tests + 1; and compare with !== .
  On mismatch: failures = failures + 1; and $display a line starting with
  "FAIL:" that shows the inputs, expected and actual values.
  On match: $display a line starting with "PASS:".
- Every $display format specifier must have a matching argument.
- Use only identifiers you declared. Never write placeholder names.

REFERENCE EXAMPLE (for a 1-bit full adder; adapt names, widths, loop bound
and expected-value expressions to the actual design):

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

SEQUENTIAL EXAMPLE (for a 4-bit up counter with clk, rst, count[3:0];
only the test section differs from the combinational example):

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

        (then the same summary, OVERALL PASS/FAIL and $finish)
"""


DESIGN_RULES = """
DESIGN RULES (Verilog-2001, synthesizable, must compile with Icarus Verilog):
- ANSI port list: module name (input wire a, ..., output wire y);
- Every port has an explicit direction and width ([N-1:0] when N > 1).
- Outputs driven by assign are wire. Outputs assigned inside always are
  output reg. Never mix assign and always for the same signal.
- Combinational logic: assign, or always @(*) with every output assigned
  in every branch. No clock, reset or registers unless requested.
- Sequential logic: always @(posedge clk) with non-blocking (<=).
- Declare every internal signal. End with endmodule.
- Do not invent ports or features the user did not ask for.

STANDARD CIRCUITS (use exactly these ports unless the user asks otherwise):
- AND/OR/XOR/NAND/NOR/XNOR gate: inputs a, b; output y (1 bit each).
- NOT gate: input a; output y.
- Half adder: inputs a, b; outputs sum, carry. sum = a ^ b; carry = a & b.
- Full adder: inputs a, b, cin; outputs sum, cout (ALL 1 bit).
  sum = a ^ b ^ cin; cout = (a & b) | (b & cin) | (a & cin).
  A full adder is never a multi-bit adder.
- N-bit adder (only when asked): a[N-1:0], b[N-1:0], cin -> sum[N-1:0], cout.
"""


VERILOG_PROMPT = """
You are an expert Verilog design and verification engineer.

Generate a Verilog design module AND a self-checking testbench for the
circuit described below.

Return ONLY a JSON object with exactly these three string keys:
{"module_code": "...", "testbench_code": "...", "explanation": "..."}

- module_code: ONLY the design module (module <name>(ports); ... endmodule).
  No initial blocks, no $display, no testbench in module_code.
- testbench_code: ONLY the testbench module (module tb_<name>; ... endmodule).
  Do not repeat the design module in testbench_code.
- explanation: one or two sentences.
- No Markdown fences. Escape newlines as \\n and quotes as \\" inside JSON.
""" + DESIGN_RULES + TESTBENCH_RULES + """
CIRCUIT DESCRIPTION:
{input}
"""


TESTBENCH_GENERATION_PROMPT = """
You are an expert Verilog verification engineer.

Write a self-checking testbench for the design module below. Do not change
the design. Read its exact module name, port names, directions and widths
and test it exactly as written.

Return ONLY a JSON object with exactly these two string keys:
{"testbench_code": "...", "explanation": "..."}

- testbench_code: only the testbench module (module tb_... endmodule).
- explanation: one or two sentences describing the test coverage.
- No Markdown fences. Escape newlines as \\n and quotes as \\" inside JSON.
""" + TESTBENCH_RULES + """
DESIGN MODULE:
{module_code}
"""


REPAIR_PROMPT = """
You are an expert Verilog engineer. The code below failed to compile with
Icarus Verilog (Verilog-2005 rules) or did not run correctly.

PROBLEMS REPORTED:
{errors}

{scope_instructions}

Fix ONLY what is needed to resolve these problems. Typical causes:
undeclared identifiers, missing semicolons, missing end/endmodule,
SystemVerilog-only syntax, reg/wire mistakes, wrong port names in the
DUT instantiation, $display arguments that do not match the format.
Keep every test case. Never delete or weaken checks to make tests pass.
""" + TESTBENCH_RULES + """
DESIGN MODULE (design.v):
{module_code}

TESTBENCH (testbench.v):
{testbench_code}

Return ONLY a JSON object with exactly these string keys:
{response_keys}
Each code field must contain the complete corrected file.
No Markdown fences. Escape newlines as \\n and quotes as \\" inside JSON.
"""


REPAIR_SCOPE_BOTH = (
    "You may correct both the design module and the testbench. Keep the "
    "design's module name, ports and intended behaviour unchanged."
)

REPAIR_SCOPE_TESTBENCH_ONLY = (
    "The design module is provided by the user and is correct as written. "
    "Do NOT change it. Correct ONLY the testbench so it matches the design's "
    "exact module name, port names and widths."
)

REPAIR_KEYS_BOTH = (
    '{"module_code": "...", "testbench_code": "...", '
    '"changes_made": "one sentence"}'
)

REPAIR_KEYS_TESTBENCH_ONLY = (
    '{"testbench_code": "...", "changes_made": "one sentence"}'
)


# ===========================================================================
# Template pipeline prompts
#
# The design and the reference model are written in SEPARATE requests. The
# reference model only sees the specification and the port list (never the
# design's code) in the generate flow, so a mistake in one is caught by the
# other instead of being copied into both.
# ===========================================================================

COMMON_CONVENTIONS = """
CONVENTIONS (follow these unless the description says otherwise):
- N-to-2^N decoder: input [N-1:0] a (plus en when an enable is requested),
  output [2^N-1:0] y, one-hot: y = en ? (1 << a) : 0. A 2-to-4 decoder has
  a 4-bit output.
- Priority encoder: the highest set input bit wins; add an output valid
  that is 1 when any input bit is set.
- Multiplexer: select input sel; y equals the selected data input.
- Shift register: shifts LEFT on every clock; the serial input enters bit 0:
  q <= {q[N-2:0], serial_in}.
- Counter: counts up by 1 and wraps around.
- Comparator (unsigned): gt = (a > b), eq = (a == b), lt = (a < b).
- Even parity generator: parity = ^data (XOR of all data bits).
- Resets are synchronous unless the description says asynchronous.
"""


DESIGN_ONLY_PROMPT = """
You are an expert Verilog RTL designer. Write ONE synthesizable Verilog-2001
module that implements the circuit described below. Do not write a testbench.

Return ONLY a JSON object with exactly these string keys:
{"module_code": "...", "explanation": "..."}

- module_code: the complete module (module ... endmodule), nothing else.
- explanation: one or two sentences on how the circuit works.
- No Markdown fences. Escape newlines as \\n and quotes as \\" inside JSON.
""" + DESIGN_RULES + """
- Name a clock input clk. Name a reset rst (active-high) or rst_n
  (active-low). Add a clock or reset only if the circuit needs one.
- No initial blocks, $display, $finish or delays (#) in the design.
""" + COMMON_CONVENTIONS + """
CIRCUIT DESCRIPTION:
{input}
"""


REFERENCE_MODEL_COMBINATIONAL = """
You are a verification engineer. A testbench for a COMBINATIONAL circuit is
already written. For every input vector it sets the inputs, waits, runs YOUR
statements, then checks each output against expected_<output>.

Write Verilog statements that set expected_<output> for EVERY output, computed
from the input values exactly as the specification requires.

SPECIFICATION:
{spec}

PORTS:
{ports}

You can read: {readable}
You must assign: {assignable}

RULES:
- Plain procedural statements with blocking "=": assignments, if/else,
  case, for loops, operators, concatenation, indexing.
- Never read the DUT outputs ({outputs}). Compute everything from inputs.
- No module, initial, always, assign, #delays, @events, $display.
- Helper variables must be declared in "declarations" (reg/integer only).
- Make widths explicit: a sum with a carry needs a concatenation such as
  {expected_cout, expected_sum} = a + b + cin;

""" + COMMON_CONVENTIONS + """
EXAMPLE (full adder with inputs a, b, cin and outputs sum, cout):
{"expected_logic": "{expected_cout, expected_sum} = a + b + cin;",
 "reset_logic": "", "declarations": "", "explanation": "sum and carry of a+b+cin"}

Return ONLY a JSON object with exactly these string keys:
{"expected_logic": "...", "reset_logic": "", "declarations": "...", "explanation": "..."}
No Markdown fences. Escape newlines as \\n inside JSON strings.
"""


REFERENCE_MODEL_SEQUENTIAL = """
You are a verification engineer. A testbench for a CLOCKED circuit is already
written. It holds reset for two clock edges, releases it, runs your
reset_logic, then repeats for many cycles:
  1. checks every output against expected_<output>,
  2. drives new random values on the data inputs,
  3. runs your expected_logic,
  4. applies one rising clock edge.

Write:
- reset_logic: statements that set every expected_<output> to its value
  right after reset (and initialise any helper state).
- expected_logic: statements that update expected_<output> to the values the
  outputs will have AFTER the next rising edge, given the current inputs and
  the current expected_* / helper values (the state before the edge).

SPECIFICATION:
{spec}

PORTS:
{ports}

You can read: {readable}
You must assign: {assignable}

RULES:
- Plain procedural statements with blocking "=": assignments, if/else,
  case, operators, concatenation, indexing.
- Never read the DUT outputs ({outputs}). Track state in expected_* or in
  helper variables declared in "declarations" (reg/integer only).
- No module, initial, always, assign, #delays, @events, $display.
- Model THIS circuit: a counter adds 1, a D flip-flop copies d, a shift
  register shifts, an FSM updates a state variable, etc.

""" + COMMON_CONVENTIONS + """
EXAMPLES:
4-bit counter (count[3:0]) with enable en:
{"reset_logic": "expected_count = 4'd0;",
 "expected_logic": "if (en) expected_count = expected_count + 4'd1;",
 "declarations": "", "explanation": "counts up while en is high"}
D flip-flop (d -> q):
{"reset_logic": "expected_q = 1'b0;", "expected_logic": "expected_q = d;",
 "declarations": "", "explanation": "q follows d after each edge"}

Return ONLY a JSON object with exactly these string keys:
{"expected_logic": "...", "reset_logic": "...", "declarations": "...", "explanation": "..."}
No Markdown fences. Escape newlines as \\n inside JSON strings.
"""


REFERENCE_MODEL_REPAIR = """
Your previous reference-model statements for this testbench have problems.

PROBLEMS:
{problems}

PREVIOUS ANSWER:
expected_logic:
{expected_logic}
reset_logic:
{reset_logic}
declarations:
{declarations}

Fix the problems and return the complete corrected answer in the same JSON
format: {"expected_logic": "...", "reset_logic": "...", "declarations": "...", "explanation": "..."}
Follow all the rules from the original request below.

ORIGINAL REQUEST:
{original}
"""


DESIGN_REPAIR_PROMPT = """
The Verilog module below does not compile with Icarus Verilog
(Verilog-2005 rules) or has structural problems.

PROBLEMS:
{problems}

MODULE:
{module_code}

Fix only what is needed. Keep the module name, ports and intended behaviour.
Outputs driven by assign are wire; outputs assigned in always blocks are reg.

Return ONLY a JSON object: {"module_code": "...", "explanation": "..."}
No Markdown fences. Escape newlines as \\n and quotes as \\" inside JSON.
"""
