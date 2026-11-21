"""
Shared fixtures for the LogicForge backend tests.

Run from backend/:   python3 -m pytest
No AI model is called: tests that exercise the generation pipeline replace
the model with a scripted stub, so they are fast, free and deterministic.
Tests that compile or simulate need Icarus Verilog; schematic tests need
Yosys and backend/node_modules. Missing tools skip those tests.
"""

import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

VERILOG_DIR = Path(__file__).resolve().parent / "verilog"

HAS_IVERILOG = bool(shutil.which("iverilog") and shutil.which("vvp"))
HAS_YOSYS = bool(
    shutil.which("yosys")
    and shutil.which("node")
    and (BACKEND / "node_modules" / "netlistsvg" / "bin" / "netlistsvg.js").exists()
)

requires_iverilog = pytest.mark.skipif(not HAS_IVERILOG, reason="Icarus Verilog not installed")
requires_yosys = pytest.mark.skipif(
    not HAS_YOSYS, reason="Yosys / netlistsvg not installed"
)


def load(name: str) -> str:
    return (VERILOG_DIR / name).read_text()


@pytest.fixture
def full_adder() -> str:
    return load("full_adder.v")


@pytest.fixture
def tb_full_adder() -> str:
    return load("tb_full_adder.v")


@pytest.fixture
def up_counter() -> str:
    return load("up_counter.v")


@pytest.fixture
def tb_up_counter() -> str:
    return load("tb_up_counter.v")


@pytest.fixture
def uart_tx() -> str:
    return load("uart_tx.v")


@pytest.fixture
def tb_uart_tx() -> str:
    return load("tb_uart_tx.v")


class ScriptedModel:
    """
    Stand-in for the AI. `responses` maps a task name ("Design generation",
    "Reference model", "Reference model repair", "Design repair", ...) to a
    list of dicts returned in order. Every call is recorded in `calls`.
    """

    def __init__(self, responses: dict[str, list[dict]]):
        self.responses = {task: list(items) for task, items in responses.items()}
        self.calls: list[str] = []

    def __call__(self, **kwargs) -> dict:
        task = kwargs["task"]
        self.calls.append(task)
        queue = self.responses.get(task)
        if not queue:
            raise RuntimeError(f"Scripted model has no response for {task!r}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"explanation": "", "reset_logic": "", "declarations": "", **item}


@pytest.fixture
def scripted_model(monkeypatch):
    """Install a ScriptedModel in place of every AI call; returns a factory."""

    import services.ai_service as ai_service
    import services.generation_pipeline as pipeline

    def install(responses: dict[str, list[dict]]) -> ScriptedModel:
        model = ScriptedModel(responses)
        monkeypatch.setattr(pipeline, "_call_model", model)
        monkeypatch.setattr(ai_service, "_call_model", model)
        return model

    return install
