"""
Generation pipeline decisions, with the AI replaced by a scripted stub.

Each test scripts what the "model" answers and checks what the pipeline
does with it: accept, repair, retry with a fresh design or reference,
fall back, or give up honestly.
"""

import pytest

from conftest import requires_iverilog
from services import ai_service
from services import generation_pipeline as pipeline

pytestmark = requires_iverilog

FULL_ADDER = (
    "module full_adder(input wire a, input wire b, input wire cin, output wire sum, output wire cout);\n"
    "  assign sum = a ^ b ^ cin;\n  assign cout = (a & b) | (b & cin) | (a & cin);\nendmodule"
)
BUGGY_ADDER = FULL_ADDER.replace("a ^ b ^ cin", "a ^ b")
ADDER_REF = (
    "module full_adder_ref(input wire a, input wire b, input wire cin, output reg sum, output reg cout);\n"
    "  always @(*) {cout, sum} = a + b + cin;\nendmodule"
)
COUNTER = (
    "module up_counter(input wire clk, input wire rst, output reg [3:0] count);\n"
    "  always @(posedge clk) if (rst) count <= 4'd0; else count <= count + 4'd1;\nendmodule"
)
COUNTER_REF = (
    "module up_counter_ref(input wire clk, input wire rst, output reg [3:0] count);\n"
    "  always @(posedge clk) count <= rst ? 4'd0 : count + 4'd1;\nendmodule"
)


def design(code):
    return {"module_code": code}


# ---------------------------------------------------------------- generate

def test_design_and_reference_agree(scripted_model):
    model = scripted_model({"Design generation": [design(FULL_ADDER)], "Reference model": [design(ADDER_REF)]})
    result = pipeline.generate_design_and_testbench("full adder")
    v = result["verification"]
    assert (v["verdict"], v["attempts"], v["method"], v["independent"]) == ("PASS", 1, "template", True)
    assert "full_adder_ref golden (" in result["testbench_code"]
    assert model.calls == ["Design generation", "Reference model"]


def test_wrong_design_is_replaced_by_a_fresh_one(scripted_model):
    scripted_model({
        "Design generation": [design(BUGGY_ADDER), design(FULL_ADDER)],
        "Reference model": [design(ADDER_REF)],
    })
    result = pipeline.generate_design_and_testbench("full adder")
    assert result["verification"]["verdict"] == "PASS"
    assert result["verification"]["attempts"] == 2
    assert "a ^ b ^ cin" in result["module_code"]


def test_wrong_reference_is_replaced_by_a_fresh_one(scripted_model):
    wrong_ref = ADDER_REF.replace("a + b + cin", "a + b")
    scripted_model({
        "Design generation": [design(FULL_ADDER), design(FULL_ADDER)],
        "Reference model": [design(wrong_ref), design(ADDER_REF)],
    })
    result = pipeline.generate_design_and_testbench("full adder")
    assert (result["verification"]["verdict"], result["verification"]["attempts"]) == ("PASS", 3)


def test_disagreement_is_reported_as_an_honest_fail(scripted_model):
    scripted_model({
        "Design generation": [design(BUGGY_ADDER), design(BUGGY_ADDER)],
        "Reference model": [design(ADDER_REF), design(ADDER_REF)],
    })
    v = pipeline.generate_design_and_testbench("full adder")["verification"]
    assert v["verdict"] == "FAIL" and not v.get("gave_up")
    assert "No design and reference model agreed" in v["log"][-1]


def test_reference_with_wrong_ports_is_repaired(scripted_model):
    bad_ports = ADDER_REF.replace("output reg sum", "output reg [1:0] sum")
    model = scripted_model({
        "Design generation": [design(FULL_ADDER)],
        "Reference model": [design(bad_ports)],
        "Reference model repair": [design(ADDER_REF)],
    })
    assert pipeline.generate_design_and_testbench("full adder")["verification"]["verdict"] == "PASS"
    assert "Reference model repair" in model.calls


def test_reference_without_reg_is_fixed_in_code(scripted_model):
    no_reg = ADDER_REF.replace("output reg sum, output reg cout", "output sum, output cout")
    model = scripted_model({"Design generation": [design(FULL_ADDER)], "Reference model": [design(no_reg)]})
    assert pipeline.generate_design_and_testbench("full adder")["verification"]["verdict"] == "PASS"
    assert "Reference model repair" not in model.calls


def test_design_mistakes_are_fixed_in_code(scripted_model):
    messy = (FULL_ADDER.replace("output wire sum, output wire cout", "output reg sum, output reg cout")
             .replace("endmodule", ""))
    model = scripted_model({"Design generation": [design(messy)], "Reference model": [design(ADDER_REF)]})
    result = pipeline.generate_design_and_testbench("full adder")
    assert result["verification"]["verdict"] == "PASS"
    assert "Design repair" not in model.calls
    assert "output wire sum" in result["module_code"] and result["module_code"].rstrip().endswith("endmodule")


def test_golden_failure_falls_back_to_expected_value_statements(scripted_model):
    scripted_model({
        "Design generation": [design(FULL_ADDER)],
        "Reference model": [design("nonsense"),
                            {"expected_logic": "{expected_cout, expected_sum} = a + b + cin;"}],
        "Reference model repair": [design("nonsense"), design("nonsense")],
    })
    result = pipeline.generate_design_and_testbench("full adder")
    assert result["verification"]["verdict"] == "PASS"
    assert "golden (" not in result["testbench_code"]  # snippet testbench used


def test_model_failure_gives_up_honestly(scripted_model):
    error = RuntimeError("AI response is missing a valid 'explanation' field.")
    scripted_model({"Design generation": [error], "Verilog generation": [error]})
    v = pipeline.generate_design_and_testbench("ripple adder")["verification"]
    assert v["gave_up"] is True
    assert "could not produce a working design" in v["message"]


def test_empty_description_is_rejected():
    with pytest.raises(ValueError):
        pipeline.generate_design_and_testbench("   ")


# ---------------------------------------------------------------- testbench for a user design

def test_testbench_for_user_design(scripted_model):
    scripted_model({"Reference model": [design(COUNTER_REF)]})
    result = pipeline.generate_testbench_for(COUNTER)
    v = result["verification"]
    assert (v["verdict"], v["independent"]) == ("PASS", False)
    assert "module up_counter_ref" in result["testbench_code"]


def test_copied_reference_is_rejected(scripted_model):
    copy = COUNTER.replace("module up_counter(", "module up_counter_ref(")
    model = scripted_model({
        "Reference model": [design(copy)],
        "Reference model repair": [design(COUNTER_REF)],
    })
    assert pipeline.generate_testbench_for(COUNTER)["verification"]["verdict"] == "PASS"
    assert "Reference model repair" in model.calls


def test_buggy_user_design_is_not_hidden(scripted_model):
    scripted_model({"Reference model": [design(COUNTER_REF), design(COUNTER_REF)]})
    v = pipeline.generate_testbench_for(COUNTER.replace("count + 4'd1", "count + 4'd2"))["verification"]
    assert v["verdict"] == "FAIL"
    assert any("re-implementation of your design" in line for line in v["log"])


def test_user_design_that_does_not_compile_is_rejected():
    with pytest.raises(ValueError, match="does not compile"):
        pipeline.generate_testbench_for("module x(input a output y); endmodule")


# ---------------------------------------------------------------- debug & fix

def _debug_answer(module_code, testbench_code):
    return {"root_cause": "x", "explanation": "y", "changes_made": "z",
            "fixed_module_code": module_code, "fixed_testbench_code": testbench_code}


def test_debug_never_puts_prose_into_the_editors(scripted_model, full_adder, tb_full_adder):
    scripted_model({"AI debugging": [_debug_answer(
        "The original Verilog module code is already correct.",
        "The corrected Verilog testbench code is as follows:",
    )]})
    result = ai_service.debug_verilog(BUGGY_ADDER, tb_full_adder)
    assert result["fixed_module_code"] == BUGGY_ADDER
    assert result["fixed_testbench_code"] == tb_full_adder
    assert (result["module_changed"], result["testbench_changed"]) == (False, False)
    assert len(result["notes"]) == 2


def test_debug_fixes_known_testbench_mistakes_without_ai(scripted_model, up_counter, tb_up_counter):
    model = scripted_model({})
    narrow = tb_up_counter.replace("reg [3:0] expected_count;", "reg expected_count;")
    result = ai_service.debug_verilog(up_counter, narrow)
    assert model.calls == []  # solved without the AI
    assert result["testbench_changed"] and not result["module_changed"]
    assert result["verification"]["verdict"] == "PASS"
    assert "reg [3:0] expected_count;" in result["fixed_testbench_code"]
