"""Compilation, simulation, verdict parsing and safety checks."""

import pytest

from conftest import requires_iverilog
from services.simulation_service import (
    compile_only,
    find_unsafe_constructs,
    parse_test_summary,
    repair_testbench_counters,
    simulate,
)


# ---------------------------------------------------------------- verdicts

@pytest.mark.parametrize(
    "output, verdict, total, failures",
    [
        ("PASS: x\nTotal tests: 4, Failures: 0\nOVERALL PASS\n", "PASS", 4, 0),
        ("Total tests: 8\nFailures: 0\nOVERALL PASS\n", "PASS", 8, 0),  # two-line form
        ("FAIL: a=1\nTotal tests: 4, Failures: 1\nOVERALL FAIL\n", "FAIL", 4, 1),
        ("Total tests: 3, Failures: 2\n", "FAIL", 3, 2),
        ("Total tests: 0, Failures: 0\nOVERALL PASS\n", "NO_TESTS", 0, 0),
        ("hello world\n", "UNVERIFIED", None, None),
        # A summary without OVERALL PASS is not enough to claim a pass.
        ("Total tests: 4, Failures: 0\n", "UNVERIFIED", 4, 0),
    ],
)
def test_parse_test_summary(output, verdict, total, failures):
    summary = parse_test_summary(output)
    assert summary == {"verdict": verdict, "total_tests": total, "failures": failures}


def test_fail_line_beats_a_passing_summary():
    output = "FAIL: case 3\nTotal tests: 4, Failures: 0\nOVERALL PASS\n"
    assert parse_test_summary(output)["verdict"] == "FAIL"


# ---------------------------------------------------------------- safety

@pytest.mark.parametrize(
    "code, construct",
    [
        ('`include "secret.v"', "`include"),
        ('integer f; initial f = $fopen("/etc/passwd");', "$fopen"),
        ('initial $readmemh("x.hex", mem);', "$readmemb / $readmemh"),
        ('initial $system("rm -rf /");', "$system"),
        ('initial $dumpfile("/tmp/x.vcd");', 'path'),
    ],
)
def test_unsafe_constructs_are_detected(code, construct):
    found = find_unsafe_constructs(code)
    assert any(construct in item for item in found), found


def test_unsafe_construct_inside_comment_is_ignored():
    code = '// $fopen("x")\n/* `include "y" */\ninitial $dumpfile("wave.vcd");'
    assert find_unsafe_constructs(code) == []


@requires_iverilog
def test_simulate_rejects_file_access(full_adder):
    result = simulate(module_code=full_adder, testbench_code='module tb; initial $system("ls"); endmodule')
    assert result["success"] is False
    assert "File access is not allowed" in result["error"]


# ---------------------------------------------------------------- counters

def test_missing_counters_are_declared():
    tb = "module tb;\n  initial begin failures = 0; total_tests = 0; end\nendmodule"
    repaired = repair_testbench_counters(tb)
    assert "integer failures;" in repaired
    assert "integer total_tests;" in repaired


def test_declared_counters_are_left_alone(tb_full_adder):
    assert repair_testbench_counters(tb_full_adder) == tb_full_adder


# ---------------------------------------------------------------- compile

@requires_iverilog
def test_compile_only_accepts_valid_design(full_adder):
    assert compile_only(full_adder)["success"] is True


@requires_iverilog
def test_compile_only_reports_syntax_errors():
    result = compile_only("module m(input a, output y); assign y = a endmodule")
    assert result["success"] is False
    assert "design.v:1" in result["errors"]


@requires_iverilog
def test_strict_mode_rejects_assign_to_reg():
    code = "module m(input wire a, output reg y);\n  assign y = a;\nendmodule"
    assert compile_only(code, strict=False)["success"] is True   # SystemVerilog allows it
    assert compile_only(code, strict=True)["success"] is False   # Verilog-2005 does not


# ---------------------------------------------------------------- simulate

@requires_iverilog
def test_full_adder_passes(full_adder, tb_full_adder):
    result = simulate(module_code=full_adder, testbench_code=tb_full_adder)
    assert result["verdict"] == "PASS"
    assert result["total_tests"] == 8
    assert result["success"] is True
    assert result["vcd"]  # waveform produced


@requires_iverilog
def test_broken_full_adder_fails(full_adder, tb_full_adder):
    broken = full_adder.replace("a ^ b ^ cin", "a ^ b")
    result = simulate(module_code=broken, testbench_code=tb_full_adder)
    assert result["verdict"] == "FAIL"
    assert result["success"] is False
    assert result["failures"] == 4


@requires_iverilog
def test_counter_passes(up_counter, tb_up_counter):
    result = simulate(module_code=up_counter, testbench_code=tb_up_counter)
    assert (result["verdict"], result["total_tests"], result["failures"]) == ("PASS", 20, 0)


@requires_iverilog
def test_single_file_mode_still_works(full_adder):
    result = simulate(code=full_adder)
    assert result["success"] is True
    assert result["verdict"] == "UNVERIFIED"  # no testbench, so no PASS claim


# ---------------------------------------------------------------- UART

@requires_iverilog
def test_uart_reference_design_passes(uart_tx, tb_uart_tx):
    assert compile_only(uart_tx, strict=True)["success"] is True
    result = simulate(module_code=uart_tx, testbench_code=tb_uart_tx)
    assert (result["verdict"], result["total_tests"]) == ("PASS", 8)


@requires_iverilog
@pytest.mark.parametrize(
    "label, original, mutation",
    [
        ("MSB first", "tx        <= shift_reg[0];", "tx        <= shift_reg[7];"),
        ("no stop bit", "tx        <= 1'b1;      // stop bit", "tx        <= 1'b0;      // stop bit"),
        ("tx_done never pulses", "tx_done   <= 1'b1;", "tx_done   <= 1'b0;"),
        (
            "wrong bit timing",
            "if (clk_count == CLKS_PER_BIT - 1) begin\n                        clk_count <= 16'd0;\n                        if (bit_index",
            "if (clk_count == CLKS_PER_BIT - 2) begin\n                        clk_count <= 16'd0;\n                        if (bit_index",
        ),
    ],
)
def test_uart_testbench_catches_bugs(uart_tx, tb_uart_tx, label, original, mutation):
    assert original in uart_tx, f"fixture changed: {label}"
    result = simulate(module_code=uart_tx.replace(original, mutation), testbench_code=tb_uart_tx)
    assert result["verdict"] == "FAIL", label  # fails cleanly, never hangs
