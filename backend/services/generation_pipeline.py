"""
Template-based generation pipeline.

Generate (description -> design + testbench):
  1. The AI writes the DESIGN only. It is cleaned, fixed deterministically,
     compile-checked (Verilog-2005) and AI-repaired if needed.
  2. The design's ports are parsed in code and the TESTBENCH is built in code
     (testbench_builder), so its structure is always correct.
  3. In a SEPARATE request, the AI writes only the reference model (the
     expected-value statements) from the description and the port list. It
     never sees the design's code, so the two are independent.
  4. The pair is simulated. On FAIL the pipeline tries a fresh design, then a
     fresh reference model, and keeps the pair that agrees (or the closest).

Generate testbench (user's design -> testbench): steps 2-4 with the user's
design locked; the reference model is derived from the design's code.

Anything the template cannot handle (unparseable ports, clocked designs
without a reset, ...) falls back to the earlier free-form generators.
"""

import re
from typing import Optional

import config
from ai.prompts import (
    DESIGN_ONLY_PROMPT,
    DESIGN_REPAIR_PROMPT,
    REFERENCE_MODEL_COMBINATIONAL,
    REFERENCE_MODEL_REPAIR,
    REFERENCE_MODEL_SEQUENTIAL,
    GOLDEN_MODEL_PROMPT,
    GOLDEN_REPAIR_PROMPT,
    GOLDEN_STYLE_FROM_DESIGN,
    GOLDEN_STYLE_FROM_SPEC,
    fill_prompt,
)
from services import ai_service
from services.ai_service import (
    AIProviderError,
    _annotate_errors,
    _balance_endmodule,
    _call_model,
    _fix_assign_to_reg,
    _fix_procedural_outputs,
    _looks_like_testbench,
    _module_blocks,
    _undriven_outputs,
    clean_verilog,
    is_verilog_source,
)
from services.simulation_service import blank_comments, compile_only, simulate
from services.testbench_builder import (
    DesignInterface,
    build_golden_testbench,
    build_testbench,
    reference_header,
    parse_interface,
    validate_snippets,
)

DESIGN_REPAIRS = 2
MODEL_REPAIRS = 2
SNIPPET_FIELDS = ["expected_logic"]
SNIPPET_OPTIONAL = ["reset_logic", "declarations", "explanation"]


class TemplateUnsupported(Exception):
    """The template pipeline cannot handle this design; use the fallback."""


# ---------------------------------------------------------------------------
# Design
# ---------------------------------------------------------------------------

def _ollama_options(temperature: float, num_predict: int) -> dict:
    return {
        "temperature": temperature,
        "num_ctx": ai_service.OLLAMA_CONTEXT,
        "num_predict": num_predict,
    }


def _tidy_design(code: str, log: list[str]) -> str:
    code = clean_verilog(code)
    code, notes = _balance_endmodule(code, "design")
    log.extend(notes)
    code, notes = _fix_assign_to_reg(code)
    log.extend(notes)
    code, notes = _fix_procedural_outputs(code)
    log.extend(notes)
    return code


def _design_problems(code: str) -> str:
    """Problems that stop a design from being used, or "" when it is usable."""

    if not is_verilog_source(code):
        return (
            "module_code does not contain Verilog (module ... endmodule). "
            f"It starts with: {code.strip()[:120]!r}"
        )
    blocks = _module_blocks(code)
    if blocks and all(_looks_like_testbench(name, body) for name, _, _, body in blocks):
        return (
            "module_code contains a testbench, not the design. Write only the "
            "synthesizable design module, without initial blocks or $display."
        )
    undriven = _undriven_outputs(code)
    if undriven:
        return (
            "These outputs are never driven: " + ", ".join(undriven)
            + ". Add the logic that assigns them."
        )
    compiled = compile_only(code, strict=True)
    if compiled.get("tool_missing"):
        raise RuntimeError(compiled["errors"])
    if not compiled["success"]:
        return "Compilation errors:\n" + _annotate_errors(compiled["errors"], code, "")
    if parse_interface(code) is None:
        return (
            "The port list could not be read. Use an ANSI header such as "
            "module name (input wire [3:0] a, output wire y); with numeric "
            "or parameter-based widths and no inout ports."
        )
    return ""


def _repair_design(code: str, problems: str, log: list[str]) -> str:
    for attempt in range(1, DESIGN_REPAIRS + 1):
        log.append(f"Design check: {problems.splitlines()[0][:200]}")
        result = _call_model(
            system_prompt="You fix Verilog modules. Return only valid JSON.",
            user_prompt=fill_prompt(
                DESIGN_REPAIR_PROMPT, problems=problems[-3000:], module_code=code
            ),
            options=_ollama_options(0.1, 3000),
            timeout=240,
            required_fields=["module_code"],
            optional_fields=["explanation"],
            task="Design repair",
        )
        code = _tidy_design(result["module_code"], log)
        problems = _design_problems(code)
        if not problems:
            log.append(f"Design repaired (attempt {attempt}).")
            return code
    raise RuntimeError("The design still does not compile: " + problems.splitlines()[0])


def _new_design(description: str, temperature: float, log: list[str]) -> dict:
    result = _call_model(
        system_prompt=(
            "You are an expert Verilog RTL designer. Return only valid JSON "
            "with module_code and explanation. No Markdown fences."
        ),
        user_prompt=fill_prompt(DESIGN_ONLY_PROMPT, input=description),
        options=_ollama_options(temperature, 3000),
        timeout=240,
        required_fields=["module_code"],
        optional_fields=["explanation"],
        task="Design generation",
    )
    code = _tidy_design(result["module_code"], log)
    problems = _design_problems(code)
    if problems:
        code = _repair_design(code, problems, log)
    return {"module_code": code, "explanation": result["explanation"]}


# ---------------------------------------------------------------------------
# Reference model + template testbench
# ---------------------------------------------------------------------------

def _check_supported(interface: DesignInterface) -> None:
    if not interface.data_inputs and not interface.is_sequential:
        raise TemplateUnsupported("combinational design without inputs")
    if interface.is_sequential and interface.reset is None:
        raise TemplateUnsupported("clocked design without a recognisable reset")


def _model_request(interface: DesignInterface, spec: str) -> str:
    template = (
        REFERENCE_MODEL_SEQUENTIAL if interface.is_sequential
        else REFERENCE_MODEL_COMBINATIONAL
    )
    readable = [p.name for p in interface.data_inputs] + [
        f"expected_{p.name}" for p in interface.outputs
    ] if interface.is_sequential else [p.name for p in interface.data_inputs]
    return fill_prompt(
        template,
        spec=spec,
        ports=interface.describe_ports(),
        readable=", ".join(readable) or "(no data inputs)",
        assignable=", ".join(
            f"expected_{p.name} ({p.width} bit{'s' if p.width > 1 else ''})"
            for p in interface.outputs
        ),
        outputs=", ".join(p.name for p in interface.outputs),
    )


def _use_expected_for_outputs(
    interface: DesignInterface, snippets: dict
) -> tuple[dict, list[str]]:
    """
    A reference model must never read the DUT's outputs. When the model
    writes e.g. `expected_count = count + 1`, it means the current expected
    value, so rewrite the output name to expected_<name>.
    """

    notes = []
    for key in ("expected_logic", "reset_logic"):
        code = snippets[key]
        for port in interface.outputs:
            pattern = rf"(?<![\w$]){re.escape(port.name)}\b"
            if re.search(pattern, code):
                code = re.sub(pattern, f"expected_{port.name}", code)
                notes.append(
                    f"Reference model read output '{port.name}'; replaced with "
                    f"expected_{port.name}."
                )
        snippets[key] = code
    return snippets, notes


def _snippet_problems(
    interface: DesignInterface,
    module_code: str,
    snippets: dict,
    locked: bool,
) -> tuple[str, Optional[str]]:
    """Validate snippets and compile the assembled testbench."""

    problems = validate_snippets(
        interface, snippets["expected_logic"], snippets["reset_logic"], snippets["declarations"]
    )
    if problems:
        return "\n".join(f"- {p}" for p in problems), None

    testbench = build_testbench(
        interface, snippets["expected_logic"], snippets["reset_logic"], snippets["declarations"]
    )
    compiled = compile_only(module_code, testbench, strict=not locked)
    if not compiled["success"]:
        return (
            "The testbench with your statements does not compile:\n"
            + _annotate_errors(compiled["errors"], module_code, testbench)
        ), None
    return "", compiled["testbench_code"]


def _reference_model(
    interface: DesignInterface,
    module_code: str,
    spec: str,
    temperature: float,
    locked: bool,
    log: list[str],
) -> dict:
    request = _model_request(interface, spec)
    result = _call_model(
        system_prompt=(
            "You are a hardware verification engineer writing reference-model "
            "statements. Return only valid JSON. No Markdown fences."
        ),
        user_prompt=request,
        options=_ollama_options(temperature, 2000),
        timeout=240,
        required_fields=SNIPPET_FIELDS,
        optional_fields=SNIPPET_OPTIONAL,
        task="Reference model",
    )

    for attempt in range(MODEL_REPAIRS + 1):
        snippets = {
            key: clean_verilog(result.get(key, "")) for key in
            ("expected_logic", "reset_logic", "declarations")
        }
        snippets, notes = _use_expected_for_outputs(interface, snippets)
        log.extend(notes)
        problems, testbench = _snippet_problems(interface, module_code, snippets, locked)
        if not problems:
            return {**snippets, "testbench_code": testbench,
                    "explanation": result.get("explanation", "")}
        log.append(f"Reference model check: {problems.splitlines()[0][:200]}")
        if attempt == MODEL_REPAIRS:
            break
        result = _call_model(
            system_prompt="You fix reference-model statements. Return only valid JSON.",
            user_prompt=fill_prompt(
                REFERENCE_MODEL_REPAIR,
                problems=problems[-3000:],
                expected_logic=snippets["expected_logic"],
                reset_logic=snippets["reset_logic"] or "(empty)",
                declarations=snippets["declarations"] or "(empty)",
                original=request,
            ),
            options=_ollama_options(0.1, 2000),
            timeout=240,
            required_fields=SNIPPET_FIELDS,
            optional_fields=SNIPPET_OPTIONAL,
            task="Reference model repair",
        )
    raise RuntimeError("The reference model could not be made to compile.")


def _normalized_body(code: str) -> str:
    """Module text without comments, whitespace and the module name."""
    body = blank_comments(code)
    body = re.sub(r"\bmodule\s+[A-Za-z_][\w$]*", "module", body)
    return re.sub(r"\s+", "", body)


def _golden_problems(
    interface: DesignInterface,
    module_code: str,
    reference: str,
    ref_name: str,
    locked: bool,
) -> tuple[str, Optional[str]]:
    """Check the reference module; return (problems, testbench) ."""

    if not is_verilog_source(reference):
        return "module_code does not contain a Verilog module.", None
    ref_interface = parse_interface(reference)
    if ref_interface is None:
        return "The module header could not be read; use the header given.", None
    if _signature(ref_interface) != _signature(interface):
        expected = ", ".join(f"{d} {n}[{w}]" for n, d, w in _signature(interface))
        got = ", ".join(f"{d} {n}[{w}]" for n, d, w in _signature(ref_interface))
        return (
            "The ports do not match the required header.\n"
            f"Required: {expected}\nYours:    {got}"
        ), None
    undriven = _undriven_outputs(reference)
    if undriven:
        return "These outputs are never driven: " + ", ".join(undriven), None
    if locked and _normalized_body(reference) == _normalized_body(module_code):
        return (
            "This is a copy of the design. Write an independent behavioural "
            "version of the same behaviour (different structure)."
        ), None

    testbench = build_golden_testbench(interface, reference, ref_name)
    compiled = compile_only(module_code, testbench, strict=not locked)
    if not compiled["success"]:
        return (
            "The reference module does not compile:\n"
            + _annotate_errors(compiled["errors"], module_code, testbench)
        ), None
    return "", compiled["testbench_code"]


def _golden_model(
    interface: DesignInterface,
    module_code: str,
    spec: str,
    temperature: float,
    locked: bool,
    log: list[str],
) -> dict:
    """An independently written reference module + the testbench using it."""

    ref_name = f"{interface.module}_ref"
    header = reference_header(interface, ref_name)
    reset_note = (
        f"Reset: {interface.reset.name} is active-"
        f"{'low' if interface.reset_active_low else 'high'}; it clears the state."
        if interface.reset else "There is no reset input."
    )
    request = fill_prompt(
        GOLDEN_MODEL_PROMPT,
        spec=spec,
        header=header,
        style=GOLDEN_STYLE_FROM_DESIGN if locked else GOLDEN_STYLE_FROM_SPEC,
        clock=interface.clock.name if interface.clock else "clk",
        reset_note=reset_note,
    )
    result = _call_model(
        system_prompt=(
            "You are a hardware verification engineer writing a reference "
            "model in Verilog. Return only valid JSON. No Markdown fences."
        ),
        user_prompt=request,
        options=_ollama_options(temperature, 3000),
        timeout=240,
        required_fields=["module_code"],
        optional_fields=["explanation"],
        task="Reference model",
    )

    for attempt in range(MODEL_REPAIRS + 1):
        code = clean_verilog(result["module_code"])
        code, _ = _balance_endmodule(code, "reference")
        code, _ = _fix_assign_to_reg(code)
        code, _ = _fix_procedural_outputs(code)
        # Keep the required module name even if the model renamed it.
        code = re.sub(r"\bmodule\s+[A-Za-z_][\w$]*", f"module {ref_name}", code, count=1)
        problems, testbench = _golden_problems(interface, module_code, code, ref_name, locked)
        if not problems:
            return {"testbench_code": testbench, "explanation": result.get("explanation", ""),
                    "kind": "golden module"}
        log.append(f"Reference module check: {problems.splitlines()[0][:200]}")
        if attempt == MODEL_REPAIRS:
            break
        result = _call_model(
            system_prompt="You fix Verilog reference models. Return only valid JSON.",
            user_prompt=fill_prompt(
                GOLDEN_REPAIR_PROMPT, problems=problems[-3000:], module_code=code, header=header
            ),
            options=_ollama_options(0.1, 3000),
            timeout=240,
            required_fields=["module_code"],
            optional_fields=["explanation"],
            task="Reference model repair",
        )
    raise RuntimeError("The reference module could not be made to work.")


def _any_reference(
    interface: DesignInterface,
    module_code: str,
    spec: str,
    temperature: float,
    locked: bool,
    log: list[str],
) -> dict:
    """Golden reference module first; expected-value statements as backup."""
    try:
        return _golden_model(interface, module_code, spec, temperature, locked, log)
    except RuntimeError as exc:
        if isinstance(exc, AIProviderError):
            raise
        log.append(f"{exc} Trying expected-value statements instead.")
        model = _reference_model(interface, module_code, spec, temperature, locked, log)
        model["kind"] = "expected-value statements"
        return model


def _signature(interface: DesignInterface) -> tuple:
    return tuple((p.name, p.direction, p.width) for p in interface.ports)


def _score(result: dict) -> tuple:
    """Lower is better: PASS first, then fewest failures."""
    verdict = result.get("verdict")
    if verdict == "PASS":
        return (0, 0)
    if verdict == "FAIL":
        return (1, result.get("failures") or 0)
    return (2, 0)


def _verification(sim: dict, attempts: int, log: list[str], independent: bool) -> dict:
    verdict = sim.get("verdict")
    ready = verdict in ("PASS", "FAIL")
    return {
        "compiled": True,
        "ready": ready,
        "verdict": verdict if ready else None,
        "total_tests": sim.get("total_tests"),
        "failures": sim.get("failures"),
        "repair_attempts": max(0, attempts - 1),
        "attempts": attempts,
        "problems": None if ready else (sim.get("error") or "Simulation did not finish."),
        "log": log,
        "method": "template",
        "independent": independent,
        "model": config.model_label(),
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def generate_design_and_testbench(description: str) -> dict:
    """Description -> verified design + template testbench."""

    if not description or not description.strip():
        raise ValueError("Circuit description cannot be empty.")
    description = description.strip()
    max_attempts = max(1, config.MAX_GENERATION_ATTEMPTS)
    log: list[str] = []
    spec = (
        description
        + "\n\nThe design uses exactly the ports listed below. Implement the "
        "behaviour the description asks for."
    )

    try:
        design = _new_design(description, 0.2, log)
        interface = parse_interface(design["module_code"])
        _check_supported(interface)
    except TemplateUnsupported as reason:
        log.append(f"Template not used ({reason}); used free-form generation.")
        return _legacy_generate(description, log)
    except RuntimeError as exc:
        if isinstance(exc, AIProviderError):
            raise
        log.append(f"First design failed: {exc}")
        return _legacy_generate(description, log)

    try:
        model = _any_reference(interface, design["module_code"], spec, 0.2, False, log)
    except RuntimeError as exc:
        if isinstance(exc, AIProviderError):
            raise
        log.append(f"Reference model failed: {exc}")
        return _legacy_generate(description, log)
    candidates = []
    attempts = 0

    def run(design_entry: dict, model_entry: dict, label: str) -> dict:
        nonlocal attempts
        attempts += 1
        sim = simulate(
            module_code=design_entry["module_code"],
            testbench_code=model_entry["testbench_code"],
        )
        entry = {"design": design_entry, "model": model_entry, "sim": sim}
        candidates.append(entry)
        verdict = sim.get("verdict")
        log.append(
            f"Attempt {attempts} ({label}): {verdict}"
            + (f", {sim.get('failures')} of {sim.get('total_tests')} tests failed"
               if verdict == "FAIL" else "")
        )
        return entry

    best = run(design, model, "design 1 vs reference model 1")

    # Attempt 2: a fresh, independently generated design.
    if best["sim"].get("verdict") != "PASS" and attempts < max_attempts:
        try:
            fresh = _new_design(description, 0.6, log)
            fresh_interface = parse_interface(fresh["module_code"])
            _check_supported(fresh_interface)
            fresh_model = model
            if _signature(fresh_interface) != _signature(interface):
                fresh_model = _any_reference(
                    fresh_interface, fresh["module_code"], spec, 0.2, False, log
                )
            run(fresh, fresh_model, "fresh design vs reference model")
        except (RuntimeError, TemplateUnsupported) as exc:
            log.append(f"Fresh design failed: {exc}")
        best = min(candidates, key=lambda c: _score(c["sim"]))

    # Attempt 3: a fresh reference model for the best design so far.
    if best["sim"].get("verdict") != "PASS" and attempts < max_attempts:
        try:
            best_interface = parse_interface(best["design"]["module_code"])
            fresh_model = _any_reference(
                best_interface, best["design"]["module_code"], spec, 0.6, False, log
            )
            run(best["design"], fresh_model, "best design vs fresh reference model")
        except RuntimeError as exc:
            if isinstance(exc, AIProviderError):
                raise
            log.append(f"Fresh reference model failed: {exc}")
        best = min(candidates, key=lambda c: _score(c["sim"]))

    if best["sim"].get("verdict") not in ("PASS", "FAIL"):
        verification = _verification(best["sim"], attempts, log, independent=True)
        verification["gave_up"] = True
        verification["message"] = GAVE_UP_MESSAGE.format(model=config.model_label())
        return {
            "module_code": best["design"]["module_code"],
            "testbench_code": best["model"]["testbench_code"],
            "explanation": best["design"]["explanation"] or "",
            "verification": verification,
        }

    if best["sim"].get("verdict") == "FAIL":
        log.append(
            "No design and reference model agreed. The FAIL lines show the "
            "inputs where they differ: check them against the specification."
        )

    explanation = best["design"]["explanation"] or ""
    if best["model"].get("explanation"):
        explanation = (explanation + "\nTest: " + best["model"]["explanation"]).strip()

    return {
        "module_code": best["design"]["module_code"],
        "testbench_code": best["model"]["testbench_code"],
        "explanation": explanation or "Design generated and verified.",
        "verification": _verification(best["sim"], attempts, log, independent=True),
    }


def generate_testbench_for(module_code: str) -> dict:
    """User's design (never modified) -> template testbench."""

    if not module_code or not module_code.strip():
        raise ValueError("Verilog module code cannot be empty.")
    module_code = module_code.strip()
    log: list[str] = []

    design_check = compile_only(module_code)
    if design_check.get("tool_missing"):
        raise RuntimeError(design_check["errors"])
    if not design_check["success"]:
        raise ValueError(
            "The design module does not compile, so a testbench cannot be "
            "generated for it yet. Fix these errors first:\n"
            + (design_check["errors"] or "")
        )

    interface = parse_interface(module_code)
    try:
        if interface is None:
            raise TemplateUnsupported("the port list could not be parsed")
        _check_supported(interface)
    except TemplateUnsupported as reason:
        result = ai_service.generate_testbench(module_code)
        result["verification"]["log"] = [
            f"Template not used ({reason}); used free-form generation."
        ] + result["verification"]["log"]
        return result

    spec = (
        "The intended behaviour is the behaviour of this design module "
        "(read it as the specification):\n" + module_code
    )
    try:
        model = _any_reference(interface, module_code, spec, 0.1, True, log)
    except RuntimeError as exc:
        if isinstance(exc, AIProviderError):
            raise
        result = ai_service.generate_testbench(module_code)
        result["verification"]["log"] = [
            f"Reference model failed ({exc}); used free-form generation."
        ] + result["verification"]["log"]
        return result
    sim = simulate(module_code=module_code, testbench_code=model["testbench_code"])
    attempts = 1
    log.append(f"Attempt 1: {sim.get('verdict')}")

    if sim.get("verdict") != "PASS" and config.MAX_GENERATION_ATTEMPTS > 1:
        try:
            second = _any_reference(interface, module_code, spec, 0.6, True, log)
            second_sim = simulate(module_code=module_code, testbench_code=second["testbench_code"])
            attempts = 2
            log.append(f"Attempt 2 (fresh reference model): {second_sim.get('verdict')}")
            if _score(second_sim) < _score(sim):
                model, sim = second, second_sim
        except RuntimeError as exc:
            if isinstance(exc, AIProviderError):
                raise
            log.append(f"Fresh reference model failed: {exc}")

    if sim.get("verdict") == "FAIL":
        log.append(
            "The reference is the AI's own re-implementation of your design. "
            "A FAIL means the two disagree: either your design has a bug or "
            "the AI misunderstood it. Check the FAIL lines (inputs, expected, "
            "got) against what your design should do."
        )

    return {
        "testbench_code": model["testbench_code"],
        "explanation": model.get("explanation") or "Self-checking testbench generated.",
        "verification": _verification(sim, attempts, log, independent=False),
    }


GAVE_UP_MESSAGE = (
    "{model} could not produce a working design for this circuit after "
    "several attempts. Try describing the ports and behaviour more "
    "precisely, or switch to a more capable model (see README: Choosing "
    "the AI model)."
)


def _legacy_generate(description: str, log: list[str]) -> dict:
    try:
        result = ai_service.generate_verilog(description)
    except RuntimeError as exc:
        if isinstance(exc, AIProviderError):
            raise
        log.append(f"Free-form generation failed: {exc}")
        return {
            "module_code": "",
            "testbench_code": "",
            "explanation": "",
            "verification": {
                "compiled": False, "ready": False, "verdict": None,
                "repair_attempts": 0, "attempts": 0, "problems": str(exc),
                "log": log, "method": "free-form", "model": config.model_label(),
                "gave_up": True,
                "message": GAVE_UP_MESSAGE.format(model=config.model_label()),
            },
        }
    verification = result["verification"]
    verification["log"] = log + verification["log"]
    verification["method"] = "free-form"
    verification["model"] = config.model_label()
    if not verification.get("ready"):
        # Every route failed: say so instead of presenting broken code.
        verification["gave_up"] = True
        verification["message"] = GAVE_UP_MESSAGE.format(model=config.model_label())
    return result
