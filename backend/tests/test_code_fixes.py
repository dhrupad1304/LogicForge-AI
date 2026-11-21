"""Deterministic repairs applied to AI output before (or instead of) asking the AI."""

import pytest

from ai.prompts import fill_prompt
from conftest import requires_iverilog
from services import ai_service as ai
from services.testbench_builder import parse_interface


# ---------------------------------------------------------------- cleaning

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("```verilog\nmodule m; endmodule\n```", "module m; endmodule"),
        ("Here is the code:\n```verilog\nmodule m; endmodule\n```\nthanks", "module m; endmodule"),
        ("Here is the corrected module:\nmodule m; endmodule", "module m; endmodule"),
        ("`timescale 1ns/1ps\nmodule m; endmodule", "`timescale 1ns/1ps\nmodule m; endmodule"),
        ("// header comment\nmodule m; endmodule", "// header comment\nmodule m; endmodule"),
    ],
)
def test_clean_verilog(raw, expected):
    assert ai.clean_verilog(raw) == expected


@pytest.mark.parametrize(
    "text, is_code",
    [
        ("module m(input a, output y); assign y = a; endmodule", True),
        ("The original Verilog module code is already correct.", False),
        ("The corrected Verilog testbench code is as follows:", False),
        ("// module m; endmodule only in a comment", False),
        ("", False),
    ],
)
def test_is_verilog_source(text, is_code):
    assert ai.is_verilog_source(text) is is_code


def test_fill_prompt_is_single_pass_and_ignores_verilog_braces():
    template = "spec {input} | concat {expected_cout, expected_sum} | {other}"
    filled = fill_prompt(template, input="{other}", other="X")
    assert filled == "spec {other} | concat {expected_cout, expected_sum} | X"


# ---------------------------------------------------------------- reg / wire

def test_assign_to_reg_becomes_wire():
    code = "module m(input wire [1:0] sel, input wire [3:0] d, output reg y);\n  assign y = d[sel];\nendmodule"
    fixed, notes = ai._fix_assign_to_reg(code)
    assert "output wire y" in fixed and notes


def test_assign_to_reg_keeps_procedural_regs():
    code = ("module m(input wire a, output reg [3:0] y, output reg z);\n"
            "  assign y = {4{a}};\n  always @(*) z = a;\nendmodule")
    fixed, _ = ai._fix_assign_to_reg(code)
    assert "output wire [3:0] y" in fixed and "output reg z" in fixed


def test_assign_to_reg_leaves_ambiguous_list_alone():
    code = "module m(input wire a, output reg y, z);\n  assign y = a;\n  always @(*) z = a;\nendmodule"
    assert ai._fix_assign_to_reg(code) == (code, [])


def test_procedural_outputs_become_reg_including_concatenations():
    code = ("module m(input wire a, input wire b, input wire c,\n"
            "  output sum, output wire cout, output lt);\n"
            "  assign lt = a < b;\n"
            "  always @(*) {cout, sum} = a + b + c;\nendmodule")
    fixed, notes = ai._fix_procedural_outputs(code)
    assert "output reg sum" in fixed and "output reg cout" in fixed
    assert "output lt" in fixed  # driven by assign: unchanged
    assert len(notes) == 2


def test_balance_endmodule():
    fixed, notes = ai._balance_endmodule("module a(input x, output y);\n assign y = x;\n", "design")
    assert fixed.rstrip().endswith("endmodule") and notes
    assert ai._balance_endmodule("module a; endmodule", "design")[1] == []


def test_undriven_outputs():
    assert ai._undriven_outputs("module m(input [1:0] s, input a, b, output y);\nendmodule") == ["y"]
    assert ai._undriven_outputs("module m(input a, output y, output z);\n assign y = a;\nendmodule") == ["z"]
    assert ai._undriven_outputs("module fa(input a, b, c, output s, co);\n assign {co, s} = a + b + c;\nendmodule") == []
    assert ai._undriven_outputs("module t(input a, output y);\n sub u0(.i(a), .o(y));\nendmodule") == []


# ---------------------------------------------------------------- testbench fixes

def test_expected_width_matches_compared_output(tb_up_counter):
    narrow = tb_up_counter.replace("reg [3:0] expected_count;", "reg expected_count;")
    fixed, notes = ai._fix_expected_widths(narrow)
    assert "reg [3:0] expected_count;" in fixed and notes


def test_port_width_fix_splits_declaration_lists(tb_full_adder):
    wide = tb_full_adder.replace("reg a, b, cin;", "reg [3:0] a, b, cin;")
    warnings = "testbench.v:9: warning: Port 3 (cin) of module full_adder expects 1 bit(s), given 4."
    fixed, notes = ai._fix_port_widths(wide, warnings)
    assert "reg [3:0] a, b;" in fixed and "reg cin;" in fixed and notes


def test_missing_clock_generator_is_added(up_counter, tb_up_counter):
    no_clock = tb_up_counter.replace("    always #5 clk = ~clk;\n", "")
    fixed, notes = ai._ensure_clock(up_counter, no_clock)
    assert "always #5 clk = ~clk;" in fixed and notes
    assert ai._ensure_clock(up_counter, tb_up_counter) == (tb_up_counter, [])


def test_duplicated_modules_are_separated(full_adder, tb_full_adder):
    design, testbench, notes = ai._separate_modules(full_adder + "\n" + tb_full_adder, tb_full_adder, False)
    assert "tb_full_adder" not in design and notes
    design, testbench, notes = ai._separate_modules(full_adder, full_adder + "\n" + tb_full_adder, True)
    assert "module full_adder (" not in testbench and notes


@requires_iverilog
def test_error_excerpts_point_at_the_bad_line(full_adder, tb_full_adder):
    broken = tb_full_adder.replace("{expected_cout, expected_sum} = a + b + cin;", "expected_sum = 4'b15;")
    problems = ai._find_problems(full_adder, broken, True)["problems"]
    assert "OFFENDING LINES" in problems
    assert ">>" in problems and "4'b15" in problems


# ---------------------------------------------------------------- free-form loop

@requires_iverilog
def test_verify_and_repair_needs_no_ai_for_known_mistakes(up_counter, tb_up_counter, monkeypatch):
    def no_ai(*args, **kwargs):
        raise AssertionError("AI should not be needed")
    monkeypatch.setattr(ai, "_ai_repair", no_ai)

    broken = (tb_up_counter
              .replace("reg [3:0] expected_count;", "reg expected_count;")  # too narrow
              .replace("    always #5 clk = ~clk;\n", ""))                    # no clock
    result = ai.verify_and_repair(up_counter, broken, module_locked=True)
    assert result["verification"]["verdict"] == "PASS"


@requires_iverilog
def test_genuine_failures_are_reported_not_repaired(full_adder, tb_full_adder, monkeypatch):
    monkeypatch.setattr(ai, "_ai_repair", lambda *a, **k: pytest.fail("must not repair a real FAIL"))
    buggy = full_adder.replace("a ^ b ^ cin", "a ^ b")
    result = ai.verify_and_repair(buggy, tb_full_adder, module_locked=False)
    assert result["verification"]["verdict"] == "FAIL"


def test_output_reads_in_reference_are_rewritten(up_counter):
    from services.generation_pipeline import _use_expected_for_outputs
    snippets, notes = _use_expected_for_outputs(
        parse_interface(up_counter),
        {"expected_logic": "expected_count = count + 4'd1;", "reset_logic": "expected_count = 4'd0;",
         "declarations": ""},
    )
    assert snippets["expected_logic"] == "expected_count = expected_count + 4'd1;" and notes
