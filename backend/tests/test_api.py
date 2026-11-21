"""HTTP API (FastAPI TestClient) and the schematic service."""

import pytest
from fastapi.testclient import TestClient

import main
from api import routes
from conftest import requires_iverilog, requires_yosys
from services.schematic_service import render_schematic


@pytest.fixture
def client():
    return TestClient(main.app)


# ---------------------------------------------------------------- basics

def test_model_endpoint_reports_configured_model(client):
    data = client.get("/model").json()
    assert data["provider"] in ("ollama", "anthropic")
    assert data["model"] and data["label"]


def test_cors_allows_only_the_local_frontend(client):
    allowed = client.options("/model", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    blocked = client.options("/model", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in blocked.headers


# ---------------------------------------------------------------- simulate

@requires_iverilog
def test_simulate_endpoint(client, full_adder, tb_full_adder):
    data = client.post("/simulate", json={"module_code": full_adder, "testbench_code": tb_full_adder}).json()
    assert (data["success"], data["verdict"], data["total_tests"], data["failures"]) == (True, "PASS", 8, 0)
    assert data["vcd"]


def test_simulate_requires_both_files(client, full_adder):
    assert client.post("/simulate", json={"module_code": full_adder}).status_code == 400


# ---------------------------------------------------------------- generate

def test_generate_returns_code_and_verification(client, monkeypatch, full_adder, tb_full_adder):
    monkeypatch.setattr(routes, "generate_design_and_testbench", lambda prompt: {
        "module_code": full_adder, "testbench_code": tb_full_adder,
        "explanation": "adds three bits", "verification": {"ready": True, "verdict": "PASS"},
    })
    data = client.post("/generate", json={"prompt": "full adder"}).json()
    # The fields the frontend relies on are all present.
    assert {"success", "code", "module_code", "testbench_code", "explanation", "verification"} <= set(data)
    assert data["code"] == data["module_code"] == full_adder


def test_generate_turns_model_errors_into_502(client, monkeypatch):
    def fail(prompt):
        raise RuntimeError("Cannot connect to Ollama. Make sure Ollama is running.")
    monkeypatch.setattr(routes, "generate_design_and_testbench", fail)
    response = client.post("/generate", json={"prompt": "full adder"})
    assert response.status_code == 502
    assert "Ollama" in response.json()["detail"]


def test_generate_validates_prompt_length(client):
    assert client.post("/generate", json={"prompt": "ab"}).status_code == 422


@requires_iverilog
def test_generate_testbench_rejects_uncompilable_design(client):
    response = client.post("/generate-testbench", json={"module_code": "module x(input a output y); endmodule"})
    assert response.status_code == 400
    assert "does not compile" in response.json()["detail"]


# ---------------------------------------------------------------- schematics

def test_schematic_rejects_bad_view(client, full_adder):
    assert client.post("/schematic", json={"module_code": full_adder, "view": "3d"}).status_code == 422


def test_schematic_blocks_file_access():
    result = render_schematic('`include "secret.v"\nmodule m; endmodule')
    assert result["success"] is False and "File access" in result["error"]


@requires_yosys
def test_full_adder_gate_schematic(full_adder):
    result = render_schematic(full_adder)
    assert result["success"] and result["view"] == "gates"
    assert result["cells"] == 7  # 2 XOR, 3 AND, 2 OR: the gates as written
    assert result["svg"].startswith("<svg") and "#f0a43a" in result["svg"]  # themed


@requires_yosys
def test_counter_rtl_schematic(up_counter):
    result = render_schematic(up_counter, "rtl")
    assert result["success"] and result["view"] == "rtl"
    assert result["cells"] == 3  # adder, reset mux, register
    assert "multiplexer in front" in result["note"]


@requires_yosys
def test_large_design_auto_selects_rtl(uart_tx):
    result = render_schematic(uart_tx)
    assert result["success"] and result["view"] == "rtl"
    assert result["gate_cells"] > 80
    assert "gate-level version has" in result["note"]


@requires_yosys
def test_schematic_reports_syntax_errors():
    result = render_schematic("module x(input a output y); endmodule")
    assert result["success"] is False and "syntax error" in result["error"]


@requires_yosys
def test_schematic_endpoint(client, full_adder):
    data = client.post("/schematic", json={"module_code": full_adder, "view": "rtl"}).json()
    assert data["success"] and data["view"] == "rtl"
