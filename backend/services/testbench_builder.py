"""
Deterministic testbench construction.

The testbench structure (declarations, DUT instantiation, stimulus loop,
clock, reset, comparisons, PASS/FAIL lines, summary, waveform dump) is built
here from the design's parsed port list, so it is correct by construction.
The AI only supplies the expected-value statements (see ai_service).
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from services.simulation_service import blank_comments


CLOCK_NAMES = {"clk", "clock", "clk_i", "i_clk"}
RESET_PATTERN = re.compile(
    r"^(?:rst|reset|areset|arst|srst|rst_i|i_rst|reset_i)(?:_?n|_b)?$|^(?:n_?rst|n_?reset|rstn|resetn)$",
    re.IGNORECASE,
)
MAX_EXHAUSTIVE_BITS = 10
RANDOM_VECTORS = 256
SEQUENTIAL_CYCLES = 32


@dataclass
class Port:
    name: str
    direction: str  # input | output | inout
    width: int

    @property
    def range_text(self) -> str:
        return f"[{self.width - 1}:0] " if self.width > 1 else ""


@dataclass
class DesignInterface:
    module: str
    ports: list[Port] = field(default_factory=list)

    @property
    def inputs(self) -> list[Port]:
        return [p for p in self.ports if p.direction == "input"]

    @property
    def outputs(self) -> list[Port]:
        return [p for p in self.ports if p.direction == "output"]

    @property
    def clock(self) -> Optional[Port]:
        clocks = [p for p in self.inputs if p.name.lower() in CLOCK_NAMES and p.width == 1]
        return clocks[0] if len(clocks) == 1 else None

    @property
    def reset(self) -> Optional[Port]:
        resets = [p for p in self.inputs if RESET_PATTERN.match(p.name) and p.width == 1]
        return resets[0] if len(resets) == 1 else None

    @property
    def reset_active_low(self) -> bool:
        name = self.reset.name.lower() if self.reset else ""
        return (
            name.endswith(("_n", "_b"))
            or name in {"rstn", "resetn", "arstn", "aresetn", "srstn"}
            or name.startswith(("nrst", "n_rst", "nreset", "n_reset"))
        )

    @property
    def data_inputs(self) -> list[Port]:
        special = {p.name for p in (self.clock, self.reset) if p}
        return [p for p in self.inputs if p.name not in special]

    @property
    def is_sequential(self) -> bool:
        return self.clock is not None

    def describe_ports(self) -> str:
        lines = []
        for p in self.ports:
            role = ""
            if self.clock and p.name == self.clock.name:
                role = " (clock)"
            elif self.reset and p.name == self.reset.name:
                role = " (reset, active-low)" if self.reset_active_low else " (reset, active-high)"
            lines.append(f"- {p.direction} {p.name}: {p.width} bit{'s' if p.width > 1 else ''}{role}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Port parsing
# ---------------------------------------------------------------------------

_SAFE_EXPR = re.compile(r"^[\d\s+\-*/()]+$")


def _eval_width_expr(expr: str, params: dict[str, int]) -> Optional[int]:
    """Evaluate e.g. 'WIDTH-1' with known parameters; None if not simple."""

    def substitute(match: re.Match) -> str:
        name = match.group(0)
        return str(params[name]) if name in params else name

    expr = re.sub(r"[A-Za-z_]\w*", substitute, expr)
    if not _SAFE_EXPR.match(expr):
        return None
    try:
        return int(eval(expr, {"__builtins__": {}}, {}))  # digits/operators only
    except Exception:
        return None


def _range_width(range_text: Optional[str], params: dict[str, int]) -> Optional[int]:
    if not range_text:
        return 1
    match = re.match(r"\[\s*([^:\]]+)\s*:\s*([^\]]+)\s*\]", range_text)
    if not match:
        return None
    msb = _eval_width_expr(match.group(1), params)
    lsb = _eval_width_expr(match.group(2), params)
    if msb is None or lsb is None:
        return None
    return abs(msb - lsb) + 1


def _parameters(text: str) -> dict[str, int]:
    params: dict[str, int] = {}
    for match in re.finditer(
        r"\b(?:parameter|localparam)\b(?:\s+(?:integer|signed|\[[^\]]*\]))*\s+([^;)]*)",
        text,
    ):
        for assignment in match.group(1).split(","):
            parts = assignment.split("=")
            if len(parts) != 2:
                continue
            name = parts[0].strip().split()[-1] if parts[0].strip() else ""
            value = _eval_width_expr(parts[1].strip(), params)
            if re.fullmatch(r"[A-Za-z_]\w*", name) and value is not None:
                params[name] = value
    return params


_PORT_DECL = re.compile(
    r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:signed\s*)?(\[[^\]]*\])?\s*"
)


def _parse_declaration_list(text: str, params: dict[str, int]) -> Optional[list[Port]]:
    """Parse 'input wire [3:0] a, b, output reg y' style text into ports."""

    ports: list[Port] = []
    matches = list(_PORT_DECL.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        names_text = text[match.end():end]
        names_text = names_text.split(";")[0]
        width = _range_width(match.group(2), params)
        if width is None:
            return None
        for raw in names_text.split(","):
            name = raw.strip().split("=")[0].strip()
            if not name:
                continue
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                return None
            ports.append(Port(name=name, direction=match.group(1), width=width))
    return ports


def parse_interface(module_code: str) -> Optional[DesignInterface]:
    """
    Parse the top module's ports. Supports ANSI headers and classic
    port lists with body declarations. Returns None when the interface
    cannot be determined exactly (the caller then falls back).
    """

    body = blank_comments(module_code)
    modules = list(re.finditer(r"\bmodule\s+([A-Za-z_][\w$]*)([\s\S]*?)\bendmodule\b", body))
    if not modules:
        return None

    # Top module = one that no other module instantiates (last one wins).
    names = [m.group(1) for m in modules]
    instantiated = {
        name for name in names
        for other in modules
        if other.group(1) != name
        and re.search(rf"\b{re.escape(name)}\s*(?:#\s*\([^;]*?\))?\s*[A-Za-z_]\w*\s*\(", other.group(2))
    }
    candidates = [m for m in modules if m.group(1) not in instantiated]
    top = (candidates or modules)[-1]
    name, text = top.group(1), top.group(2)

    params = _parameters(text)
    header = re.match(r"\s*(?:#\s*\(([\s\S]*?)\)\s*)?\(([\s\S]*?)\)\s*;", text)
    if not header:
        return None
    if header.group(1):
        params.update(_parameters("parameter " + header.group(1)))
    port_list = header.group(2)
    rest = text[header.end():]

    if _PORT_DECL.search(port_list):
        ports = _parse_declaration_list(port_list, params)
    else:
        # Non-ANSI: header lists names, body declares directions.
        order = [p.strip() for p in port_list.split(",") if p.strip()]
        declared: dict[str, Port] = {}
        for statement in re.findall(r"\b(?:input|output|inout)\b[^;]*;", rest):
            parsed = _parse_declaration_list(statement, params)
            if parsed is None:
                return None
            for port in parsed:
                declared[port.name] = port
        if any(p not in declared for p in order):
            return None
        ports = [declared[p] for p in order]

    if not ports or not any(p.direction == "output" for p in ports):
        return None
    if any(p.direction == "inout" for p in ports):
        return None
    return DesignInterface(module=name, ports=ports)


# ---------------------------------------------------------------------------
# Testbench assembly
# ---------------------------------------------------------------------------

def _fmt(port: Port) -> str:
    return "%b" if port.width <= 8 else "%h"


def _value_display(ports: list[Port], prefix: str = "") -> tuple[str, str]:
    """Format string and argument list for a set of signals."""
    fmt = " ".join(f"{p.name}={_fmt(p)}" for p in ports)
    args = ", ".join(f"{prefix}{p.name}" for p in ports)
    return fmt, args


def _indent(code: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line.strip() if line.strip() else "" for line in code.strip().splitlines())


def _random_concat(width: int) -> str:
    words = (width + 31) // 32
    calls = ", ".join("$random(seed)" for _ in range(words))
    return f"{{{calls}}}" if words > 1 else "$random(seed)"


def stimulus_bits(interface: DesignInterface) -> int:
    return sum(p.width for p in interface.data_inputs)


def build_testbench(
    interface: DesignInterface,
    expected_logic: str,
    reset_logic: str = "",
    declarations: str = "",
) -> str:
    """
    Assemble a complete self-checking Verilog-2001 testbench.

    Combinational designs: expected_logic runs after each input vector and
    sets every expected_<output>.
    Sequential designs: reset_logic sets expected_* to the post-reset values;
    expected_logic runs once per cycle, before the rising edge, and updates
    expected_* to the values the outputs will have after that edge.
    """

    tb = f"tb_{interface.module}"
    outputs = interface.outputs
    data_inputs = interface.data_inputs
    clock, reset = interface.clock, interface.reset

    lines = [f"module {tb};"]
    for p in interface.inputs:
        lines.append(f"    reg {p.range_text}{p.name};")
    for p in outputs:
        lines.append(f"    wire {p.range_text}{p.name};")
    for p in outputs:
        lines.append(f"    reg {p.range_text}expected_{p.name};")
    lines.append("    integer failures;")
    lines.append("    integer total_tests;")
    lines.append("    integer i;")
    lines.append("    integer seed;")
    if declarations.strip():
        lines.append("    // reference-model helpers")
        lines.append(_indent(declarations, 4))
    lines.append("")
    connections = ",\n".join(f"        .{p.name}({p.name})" for p in interface.ports)
    lines.append(f"    {interface.module} dut (\n{connections}\n    );")
    lines.append("")
    if clock:
        lines.append(f"    always #5 {clock.name} = ~{clock.name};")
        lines.append("")

    mismatch = " || ".join(f"{p.name} !== expected_{p.name}" for p in outputs)
    in_fmt, in_args = _value_display(data_inputs)
    exp_fmt, exp_args = _value_display(outputs, "expected_")
    got_fmt, got_args = _value_display(outputs)

    def check_block(label_fmt: str, label_args: str, pad: int) -> str:
        fail_args = ", ".join(a for a in (label_args, exp_args, got_args) if a)
        pass_args = ", ".join(a for a in (label_args, got_args) if a)
        body = [
            "total_tests = total_tests + 1;",
            f"if ({mismatch}) begin",
            "    failures = failures + 1;",
            f'    $display("FAIL: {label_fmt} | expected {exp_fmt} | got {got_fmt}", {fail_args});',
            "end else begin",
            f'    $display("PASS: {label_fmt} | {got_fmt}", {pass_args});',
            "end",
        ]
        return "\n".join(" " * pad + b for b in body)

    lines.append("    initial begin")
    lines.append("        failures = 0;")
    lines.append("        total_tests = 0;")
    lines.append("        seed = 1;")
    for p in interface.inputs:
        lines.append(f"        {p.name} = 0;")
    lines.append("")
    lines.append('        $dumpfile("logicforge_waveform.vcd");')
    lines.append(f"        $dumpvars(0, {tb});")
    lines.append("")

    if not clock:
        bits = stimulus_bits(interface)
        concat = "{" + ", ".join(p.name for p in data_inputs) + "}"
        if bits <= MAX_EXHAUSTIVE_BITS:
            count = 2 ** bits
            assign = f"{concat} = i;"
            lines.append(f"        // all {count} input combinations")
        else:
            count = RANDOM_VECTORS
            assign = (
                f"if (i == 0) {concat} = {{{bits}{{1'b0}}}};\n"
                f"else if (i == 1) {concat} = {{{bits}{{1'b1}}}};\n"
                f"else {concat} = {_random_concat(bits)};"
            )
            lines.append(f"        // {count} vectors: all zeros, all ones, then random")
        lines.append(f"        for (i = 0; i < {count}; i = i + 1) begin")
        lines.append(_indent(assign, 12))
        lines.append("            #10;")
        lines.append("            // ---- expected values (reference model) ----")
        lines.append(_indent(expected_logic, 12))
        lines.append("            // -------------------------------------------")
        lines.append(check_block(in_fmt, in_args, 12))
        lines.append("        end")
    else:
        active = "1'b0" if interface.reset_active_low else "1'b1"
        inactive = "1'b1" if interface.reset_active_low else "1'b0"
        if reset:
            lines.append(f"        {reset.name} = {active};")
            lines.append(f"        @(posedge {clock.name}); #1;")
            lines.append(f"        @(posedge {clock.name}); #1;")
            lines.append(f"        {reset.name} = {inactive};")
        lines.append("        // ---- expected values right after reset ----")
        lines.append(_indent(reset_logic or "", 8))
        lines.append("        // -------------------------------------------")
        lines.append(f"        for (i = 0; i < {SEQUENTIAL_CYCLES}; i = i + 1) begin")
        lines.append(check_block("cycle=%0d" + (" " + in_fmt if in_fmt else ""),
                                 "i" + (", " + in_args if in_args else ""), 12))
        if data_inputs:
            concat = "{" + ", ".join(p.name for p in data_inputs) + "}"
            lines.append(f"            {concat} = {_random_concat(stimulus_bits(interface))};")
        lines.append("            // ---- next expected values (reference model) ----")
        lines.append(_indent(expected_logic, 12))
        lines.append("            // ------------------------------------------------")
        lines.append(f"            @(posedge {clock.name}); #1;")
        lines.append("        end")

    lines.append("")
    lines.append('        $display("Total tests: %0d, Failures: %0d", total_tests, failures);')
    lines.append("        if (failures == 0)")
    lines.append('            $display("OVERALL PASS");')
    lines.append("        else")
    lines.append('            $display("OVERALL FAIL");')
    lines.append("        $finish;")
    lines.append("    end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Validation of the AI-written snippets
# ---------------------------------------------------------------------------

FORBIDDEN_IN_SNIPPET = re.compile(
    r"\b(module|endmodule|initial|always|assign|task|function|fork)\b|\$finish|\$display|[#@]"
)
DECLARATION_LINE = re.compile(r"^\s*(reg|integer)\b[^;]*;\s*$")


def validate_snippets(
    interface: DesignInterface,
    expected_logic: str,
    reset_logic: str,
    declarations: str,
) -> list[str]:
    """Return problems with the AI's snippets (empty list when valid)."""

    problems = []
    for label, code in (("expected_logic", expected_logic), ("reset_logic", reset_logic)):
        stripped = blank_comments(code)
        bad = FORBIDDEN_IN_SNIPPET.search(stripped)
        if bad:
            problems.append(
                f"{label} must contain only procedural statements; remove "
                f"'{bad.group(0)}'."
            )
        for port in interface.outputs:
            if re.search(rf"(?<![\w$]){re.escape(port.name)}\b", stripped):
                problems.append(
                    f"{label} reads the DUT output '{port.name}'. Expected values "
                    f"must be computed from inputs (and expected_* values), never "
                    f"from DUT outputs. Use expected_{port.name} instead."
                )

    combined = blank_comments(expected_logic + "\n" + reset_logic)
    for port in interface.outputs:
        if f"expected_{port.name}" not in combined:
            problems.append(f"Nothing assigns expected_{port.name}.")

    if interface.is_sequential and not reset_logic.strip():
        problems.append("reset_logic is empty: set every expected_* to its value after reset.")

    for line in declarations.strip().splitlines():
        if line.strip() and not DECLARATION_LINE.match(blank_comments(line)):
            problems.append(f"declarations may only contain reg/integer declarations, not: {line.strip()}")

    return problems


# ---------------------------------------------------------------------------
# Golden-model testbench: DUT and an independent reference module side by side
# ---------------------------------------------------------------------------

def reference_header(interface: DesignInterface, ref_name: str) -> str:
    """The exact module header the reference model must use."""
    ports = ",\n".join(
        f"    {p.direction} {'wire ' if p.direction == 'input' else ''}{p.range_text}{p.name}"
        for p in interface.ports
    )
    return f"module {ref_name} (\n{ports}\n);"


def build_golden_testbench(
    interface: DesignInterface,
    reference_code: str,
    ref_name: str,
) -> str:
    """
    Testbench that drives the DUT and the reference module with the same
    inputs and compares every output: after each input vector
    (combinational) or every clock cycle after reset (sequential). The
    reference module's code is appended to the testbench file.
    """

    tb = f"tb_{interface.module}"
    outputs = interface.outputs
    data_inputs = interface.data_inputs
    clock, reset = interface.clock, interface.reset

    lines = [
        "// Self-checking testbench: the design (dut) is compared with an",
        "// independently written reference model (golden) on every test.",
        f"module {tb};",
    ]
    for p in interface.inputs:
        lines.append(f"    reg {p.range_text}{p.name};")
    for p in outputs:
        lines.append(f"    wire {p.range_text}{p.name};")
    for p in outputs:
        lines.append(f"    wire {p.range_text}expected_{p.name};")
    lines += [
        "    integer failures;",
        "    integer total_tests;",
        "    integer i;",
        "    integer seed;",
        "",
    ]
    dut_conn = ",\n".join(f"        .{p.name}({p.name})" for p in interface.ports)
    ref_conn = ",\n".join(
        f"        .{p.name}({'expected_' if p.direction == 'output' else ''}{p.name})"
        for p in interface.ports
    )
    lines.append(f"    {interface.module} dut (\n{dut_conn}\n    );")
    lines.append("")
    lines.append(f"    {ref_name} golden (\n{ref_conn}\n    );")
    lines.append("")
    if clock:
        lines.append(f"    always #5 {clock.name} = ~{clock.name};")
        lines.append("")

    mismatch = " || ".join(f"{p.name} !== expected_{p.name}" for p in outputs)
    in_fmt, in_args = _value_display(data_inputs)
    exp_fmt, exp_args = _value_display(outputs, "expected_")
    got_fmt, got_args = _value_display(outputs)

    def check_block(label_fmt: str, label_args: str, pad: int) -> str:
        fail_args = ", ".join(a for a in (label_args, exp_args, got_args) if a)
        pass_args = ", ".join(a for a in (label_args, got_args) if a)
        body = [
            "total_tests = total_tests + 1;",
            f"if ({mismatch}) begin",
            "    failures = failures + 1;",
            f'    $display("FAIL: {label_fmt} | expected {exp_fmt} | got {got_fmt}", {fail_args});',
            "end else begin",
            f'    $display("PASS: {label_fmt} | {got_fmt}", {pass_args});',
            "end",
        ]
        return "\n".join(" " * pad + b for b in body)

    lines += [
        "    initial begin",
        "        failures = 0;",
        "        total_tests = 0;",
        "        seed = 1;",
    ]
    for p in interface.inputs:
        lines.append(f"        {p.name} = 0;")
    lines += [
        "",
        '        $dumpfile("logicforge_waveform.vcd");',
        f"        $dumpvars(0, {tb});",
        "",
    ]

    if not clock:
        bits = stimulus_bits(interface)
        concat = "{" + ", ".join(p.name for p in data_inputs) + "}"
        if bits <= MAX_EXHAUSTIVE_BITS:
            count = 2 ** bits
            assign = f"{concat} = i;"
            lines.append(f"        // all {count} input combinations")
        else:
            count = RANDOM_VECTORS
            assign = (
                f"if (i == 0) {concat} = {{{bits}{{1'b0}}}};\n"
                f"else if (i == 1) {concat} = {{{bits}{{1'b1}}}};\n"
                f"else {concat} = {_random_concat(bits)};"
            )
            lines.append(f"        // {count} vectors: all zeros, all ones, then random")
        lines.append(f"        for (i = 0; i < {count}; i = i + 1) begin")
        lines.append(_indent(assign, 12))
        lines.append("            #10;")
        lines.append(check_block(in_fmt, in_args, 12))
        lines.append("        end")
    else:
        active = "1'b0" if interface.reset_active_low else "1'b1"
        inactive = "1'b1" if interface.reset_active_low else "1'b0"
        lines += [
            f"        {reset.name} = {active};",
            f"        @(posedge {clock.name}); #1;",
            f"        @(posedge {clock.name}); #1;",
            f"        {reset.name} = {inactive};",
            f"        for (i = 0; i < {SEQUENTIAL_CYCLES}; i = i + 1) begin",
            check_block("cycle=%0d" + (" " + in_fmt if in_fmt else ""),
                        "i" + (", " + in_args if in_args else ""), 12),
        ]
        if data_inputs:
            concat = "{" + ", ".join(p.name for p in data_inputs) + "}"
            lines.append(f"            {concat} = {_random_concat(stimulus_bits(interface))};")
        lines += [
            f"            @(posedge {clock.name}); #1;",
            "        end",
        ]

    lines += [
        "",
        '        $display("Total tests: %0d, Failures: %0d", total_tests, failures);',
        "        if (failures == 0)",
        '            $display("OVERALL PASS");',
        "        else",
        '            $display("OVERALL FAIL");',
        "        $finish;",
        "    end",
        "endmodule",
        "",
        "// ---- Reference model (written independently of the design) ----",
        reference_code.strip(),
        "",
    ]
    return "\n".join(lines)
