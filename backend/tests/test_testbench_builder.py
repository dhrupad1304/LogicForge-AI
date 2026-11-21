"""Port parsing and the template / golden-model testbench builders."""

import pytest

from conftest import requires_iverilog
from services.simulation_service import simulate
from services.testbench_builder import (
    build_golden_testbench,
    build_testbench,
    parse_interface,
    reference_header,
    validate_snippets,
)

COUNTER_ASYNC_N = (
    "module cnt(input wire clk, input wire rst_n, input wire en, output reg [7:0] q);\n"
    "  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else if (en) q <= q + 1;\n"
    "endmodule"
)
MUX_NON_ANSI = (
    "module mux4(sel, d, y);\n input [1:0] sel;\n input [3:0] d;\n output y;\n"
    " assign y = d[sel];\nendmodule"
)
PARAM_ADDER = (
    "module add #(parameter W = 16) (input [W-1:0] a, input [W-1:0] b, output [W:0] s);\n"
    "  assign s = a + b;\nendmodule"
)
SHIFT_REG = (
    "module sr(input clk, input reset, input din, output [3:0] q);\n reg [3:0] r; assign q = r;\n"
    " always @(posedge clk) if (reset) r <= 0; else r <= {r[2:0], din};\nendmodule"
)


# ---------------------------------------------------------------- parsing

def signature(interface):
    return [(p.name, p.direction, p.width) for p in interface.ports]


def test_parse_ansi_ports(full_adder):
    interface = parse_interface(full_adder)
    assert interface.module == "full_adder"
    assert signature(interface) == [
        ("a", "input", 1), ("b", "input", 1), ("cin", "input", 1),
        ("sum", "output", 1), ("cout", "output", 1),
    ]
    assert not interface.is_sequential


def test_parse_non_ansi_ports():
    assert signature(parse_interface(MUX_NON_ANSI)) == [
        ("sel", "input", 2), ("d", "input", 4), ("y", "output", 1),
    ]


def test_parse_parameterised_widths():
    assert signature(parse_interface(PARAM_ADDER)) == [
        ("a", "input", 16), ("b", "input", 16), ("s", "output", 17),
    ]


def test_clock_and_active_low_reset_are_recognised():
    interface = parse_interface(COUNTER_ASYNC_N)
    assert interface.is_sequential
    assert interface.clock.name == "clk"
    assert interface.reset.name == "rst_n"
    assert interface.reset_active_low is True
    assert [p.name for p in interface.data_inputs] == ["en"]


@pytest.mark.parametrize("name, low", [("rst", False), ("reset", False), ("rst_n", True),
                                       ("resetn", True), ("nrst", True), ("arst_n", True)])
def test_reset_polarity(name, low):
    code = f"module m(input clk, input {name}, output reg q); always @(posedge clk) q <= 1; endmodule"
    interface = parse_interface(code)
    assert interface.reset.name == name
    assert interface.reset_active_low is low


def test_top_module_is_the_one_not_instantiated():
    code = (
        "module half(input a, input b, output s, output c); assign s = a ^ b; assign c = a & b; endmodule\n"
        "module top(input x, input y, output z, output w); half h0 (.a(x), .b(y), .s(z), .c(w)); endmodule"
    )
    assert parse_interface(code).module == "top"


@pytest.mark.parametrize(
    "code",
    [
        "not verilog at all",
        "module m(inout wire bus, output y); assign y = bus; endmodule",  # inout unsupported
        "module m(input a); endmodule",  # no outputs
        "module m #(parameter W = 4) (input [$clog2(W):0] a, output y); endmodule",  # unparseable width
    ],
)
def test_unsupported_interfaces_return_none(code):
    assert parse_interface(code) is None


# ---------------------------------------------------------------- snippet template

SNIPPET_CASES = {
    "full adder": ("full_adder", "{expected_cout, expected_sum} = a + b + cin;", "", "", "a ^ b ^ cin", "a ^ b"),
    "counter": ("up_counter", "expected_count = expected_count + 4'd1;", "expected_count = 4'd0;", "",
                "count + 4'd1", "count + 4'd2"),
    "async rst_n + enable": (COUNTER_ASYNC_N, "if (en) expected_q = expected_q + 8'd1;", "expected_q = 8'd0;", "",
                             "q <= q + 1", "q <= q + 2"),
    "non-ANSI mux": (MUX_NON_ANSI, "expected_y = d[sel];", "", "", "d[sel]", "d[0]"),
    "16-bit adder (random)": (PARAM_ADDER, "expected_s = a + b;", "", "", "a + b", "a - b"),
    "shift register + helper": (SHIFT_REG, "model = {model[2:0], din};\nexpected_q = model;",
                                "model = 4'd0;\nexpected_q = 4'd0;", "reg [3:0] model;",
                                "{r[2:0], din}", "{din, r[3:1]}"),
}


@requires_iverilog
@pytest.mark.parametrize("case", SNIPPET_CASES)
def test_snippet_testbench_passes_correct_and_fails_buggy(case, request):
    design, expected, reset, decls, good, bad = SNIPPET_CASES[case]
    if design in ("full_adder", "up_counter"):
        design = request.getfixturevalue(design)
    interface = parse_interface(design)
    assert validate_snippets(interface, expected, reset, decls) == []
    testbench = build_testbench(interface, expected, reset, decls)

    passing = simulate(module_code=design, testbench_code=testbench)
    assert passing["verdict"] == "PASS", passing.get("error") or passing.get("output")

    assert good in design
    failing = simulate(module_code=design.replace(good, bad), testbench_code=testbench)
    assert failing["verdict"] == "FAIL"


def test_exhaustive_vs_random_stimulus(full_adder):
    small = build_testbench(parse_interface(full_adder), "{expected_cout, expected_sum} = a + b + cin;")
    assert "all 8 input combinations" in small
    wide = build_testbench(parse_interface(PARAM_ADDER), "expected_s = a + b;")
    assert "256 vectors" in wide and "$random(seed)" in wide


# ---------------------------------------------------------------- snippet validation

def test_snippet_reading_dut_output_is_rejected(up_counter):
    problems = validate_snippets(parse_interface(up_counter), "expected_count = count + 1;",
                                 "expected_count = 0;", "")
    assert any("reads the DUT output 'count'" in p for p in problems)


def test_snippet_forbidden_constructs_are_rejected(full_adder):
    problems = validate_snippets(parse_interface(full_adder),
                                 "#5 expected_sum = a; $display(a);", "", "wire x;")
    joined = "\n".join(problems)
    assert "remove '#'" in joined
    assert "Nothing assigns expected_cout" in joined
    assert "reg/integer declarations" in joined


def test_sequential_snippet_needs_reset_logic(up_counter):
    problems = validate_snippets(parse_interface(up_counter), "expected_count = expected_count + 1;", "", "")
    assert any("reset_logic is empty" in p for p in problems)


# ---------------------------------------------------------------- golden template

def test_reference_header_matches_ports(up_counter):
    header = reference_header(parse_interface(up_counter), "up_counter_ref")
    assert header.startswith("module up_counter_ref (")
    assert "input wire clk" in header and "output [3:0] count" in header


@requires_iverilog
def test_golden_testbench_compares_design_with_reference(full_adder, up_counter):
    cases = [
        (full_adder,
         "module full_adder_ref(input wire a, input wire b, input wire cin, output reg sum, output reg cout);\n"
         "  always @(*) {cout, sum} = a + b + cin;\nendmodule",
         "a ^ b ^ cin", "a ^ b", 8),
        (up_counter,
         "module up_counter_ref(input wire clk, input wire rst, output reg [3:0] count);\n"
         "  always @(posedge clk) count <= rst ? 4'd0 : count + 4'd1;\nendmodule",
         "count + 4'd1", "count + 4'd2", 32),
    ]
    for design, reference, good, bad, tests in cases:
        interface = parse_interface(design)
        testbench = build_golden_testbench(interface, reference, f"{interface.module}_ref")
        assert "_ref golden (" in testbench  # instance not named `ref` (SystemVerilog keyword)

        passing = simulate(module_code=design, testbench_code=testbench)
        assert (passing["verdict"], passing["total_tests"]) == ("PASS", tests)
        failing = simulate(module_code=design.replace(good, bad), testbench_code=testbench)
        assert failing["verdict"] == "FAIL"
