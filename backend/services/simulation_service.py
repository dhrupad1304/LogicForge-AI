
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


COMPILE_TIMEOUT = 15
SIMULATION_TIMEOUT = 15
MAX_CODE_SIZE = 50000
MAX_VCD_SIZE = 1000000

# Hard caps for every tool run on user-supplied Verilog (iverilog, vvp,
# yosys), applied with ulimit in a wrapper shell: CPU seconds, largest file
# written (KB) and, on Linux, address space (KB). They stop a runaway
# design from exhausting a deployed server.
TOOL_CPU_SECONDS = 30
TOOL_MAX_FILE_KB = 64 * 1024
TOOL_MAX_MEMORY_KB = 1024 * 1024


MISSING_ICARUS = (
    "Icarus Verilog was not found. Install iverilog and vvp before simulating."
)


def icarus_installed() -> bool:
    return bool(shutil.which("iverilog") and shutil.which("vvp"))


def limited(command: list[str]) -> list[str]:
    """Wrap a command so it runs under resource limits."""
    limits = f"ulimit -t {TOOL_CPU_SECONDS} -f {TOOL_MAX_FILE_KB}"
    if sys.platform.startswith("linux"):
        limits += f" -v {TOOL_MAX_MEMORY_KB}"
    return ["/bin/sh", "-c", f'{limits} && exec "$@"', "logicforge-sandbox", *command]


# Constructs that let Verilog read or write files on the host. Simulation runs
# as the backend's user account, so these are rejected before compiling.
FORBIDDEN_CONSTRUCTS = {
    r"`include\b": "`include",
    r"\$fopen[rw]?\b": "$fopen",
    r"\$readmem[bh]\b": "$readmemb / $readmemh",
    r"\$writemem[bh]\b": "$writememb / $writememh",
    r"\$system\b": "$system",
}

# $dumpfile may only name a plain .vcd file in the simulation directory.
DUMPFILE_PATTERN = re.compile(r"\$dumpfile\s*\(\s*(?P<arg>[^)]*)\)")
SAFE_DUMPFILE_ARG = re.compile(r'^"[\w.-]+\.vcd"$')

# A failed check printed by the testbench, e.g. "FAIL: expected 1, got 0".
FAIL_LINE_PATTERN = re.compile(r"^\s*(?:OVERALL\s+)?FAIL(?:ED)?\b", re.MULTILINE)

# Final summary printed by LogicForge testbenches. Accepts both
# "Total tests: 8, Failures: 0" and the two-line form.
TOTAL_TESTS_PATTERN = re.compile(r"Total\s+tests\s*:\s*(\d+)", re.IGNORECASE)
FAILURES_PATTERN = re.compile(r"Failures\s*:\s*(\d+)", re.IGNORECASE)
OVERALL_PASS_PATTERN = re.compile(r"^\s*OVERALL\s+PASS\b", re.MULTILINE)


def parse_test_summary(output: str) -> dict:
    """
    Read the testbench's own summary from simulation output.

    verdict is:
      PASS       - summary present, at least one test, zero failures,
                   OVERALL PASS printed and no FAIL lines.
      FAIL       - any FAIL line, or the summary reports failures.
      NO_TESTS   - summary reports zero executed tests.
      UNVERIFIED - the testbench printed no recognisable summary, so a
                   PASS cannot be claimed.
    """

    output = output or ""
    total_match = TOTAL_TESTS_PATTERN.search(output)
    failures_match = FAILURES_PATTERN.search(output)

    total_tests = int(total_match.group(1)) if total_match else None
    failures = int(failures_match.group(1)) if failures_match else None
    has_fail_line = bool(FAIL_LINE_PATTERN.search(output))
    has_overall_pass = bool(OVERALL_PASS_PATTERN.search(output))

    if has_fail_line or (failures is not None and failures > 0):
        verdict = "FAIL"
    elif total_tests == 0:
        verdict = "NO_TESTS"
    elif total_tests is not None and failures == 0 and has_overall_pass:
        verdict = "PASS"
    else:
        verdict = "UNVERIFIED"

    return {
        "verdict": verdict,
        "total_tests": total_tests,
        "failures": failures,
    }


def blank_comments(code: str) -> str:
    """
    Replace Verilog comments with spaces, keeping string literals and every
    character offset unchanged so matches map back to the original code.
    """

    pattern = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')

    def replace(match: re.Match) -> str:
        text = match.group(0)
        if text.startswith('"'):
            return text
        return re.sub(r"[^\n]", " ", text)

    return pattern.sub(replace, code)


def find_unsafe_constructs(code: str) -> list[str]:
    """Return the names of file-access constructs used in the code."""

    stripped = blank_comments(code)
    found = [
        name
        for pattern, name in FORBIDDEN_CONSTRUCTS.items()
        if re.search(pattern, stripped)
    ]

    for match in DUMPFILE_PATTERN.finditer(stripped):
        if not SAFE_DUMPFILE_ARG.match(match.group("arg").strip()):
            found.append("$dumpfile with a path (use a plain name like \"wave.vcd\")")

    return found


def repair_testbench_counters(testbench_code: str) -> str:
    """
    Automatically declare missing testbench counters when they are used.
    Existing declarations are preserved.
    """

    stripped = blank_comments(testbench_code)
    counters = ("failures", "total_tests")
    missing_counters = []

    for counter in counters:
        # Only add a counter if the testbench actually uses it.
        if not re.search(rf"\b{counter}\b", stripped):
            continue

        # Check whether the counter is already declared (ignoring comments).
        declaration_pattern = (
            rf"\b(?:integer|int|reg|logic|wire)\s+"
            rf"(?:\[[^\]]+\]\s*)?[^;]*\b{counter}\b[^;]*;"
        )

        if not re.search(declaration_pattern, stripped):
            missing_counters.append(counter)

    if not missing_counters:
        return testbench_code

    # Find the first module header, including an optional #(...) parameter
    # list and an optional port list.
    module_pattern = (
        r"(?m)^\s*module\s+[\w$]+\s*"
        r"(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?"
        r"(?:\([^;]*\))?\s*;"
    )

    match = re.search(module_pattern, stripped)

    if not match:
        return testbench_code

    declarations = "\n\n" + "\n".join(
        f"  integer {counter};"
        for counter in missing_counters
    ) + "\n"

    # Comment blanking keeps offsets, so match.end() is valid in the original.
    return (
        testbench_code[:match.end()]
        + declarations
        + testbench_code[match.end():]
    )


def compile_only(
    module_code: str,
    testbench_code: str = None,
    strict: bool = False,
) -> dict:
    """
    Compile Verilog with Icarus without running it.

    Used by the AI generate/repair loop. When testbench_code is None only the
    design module is checked. Applies the same safety checks and counter
    repair as simulate(), and returns the testbench as it was compiled.

    strict=True compiles as Verilog-2005 instead of SystemVerilog, so
    SystemVerilog-only shortcuts (e.g. assign to a reg) are reported as
    errors. Use it for AI-written code; user code keeps -g2012.

    Returns {"success", "errors", "warnings", "testbench_code"}.
    """

    if not module_code or not module_code.strip():
        return {"success": False, "errors": "Module code is empty.",
                "warnings": None, "testbench_code": testbench_code}

    sources = {"design.v": module_code}

    if testbench_code is not None:
        if not testbench_code.strip():
            return {"success": False, "errors": "Testbench code is empty.",
                    "warnings": None, "testbench_code": testbench_code}
        testbench_code = repair_testbench_counters(testbench_code)
        sources["testbench.v"] = testbench_code

    if any(len(source) > MAX_CODE_SIZE for source in sources.values()):
        return {"success": False,
                "errors": "Verilog code exceeds the maximum allowed size.",
                "warnings": None, "testbench_code": testbench_code}

    unsafe = sorted({
        construct
        for source in sources.values()
        for construct in find_unsafe_constructs(source)
    })

    if unsafe:
        return {
            "success": False,
            "errors": (
                "File access is not allowed in simulations. Remove: "
                + ", ".join(unsafe)
            ),
            "warnings": None,
            "testbench_code": testbench_code,
        }

    if not icarus_installed():
        return {"success": False, "errors": MISSING_ICARUS, "warnings": None,
                "testbench_code": testbench_code, "tool_missing": True}

    try:
        with tempfile.TemporaryDirectory(prefix="logicforge_") as temp_dir:
            working_dir = Path(temp_dir)

            for filename, source in sources.items():
                (working_dir / filename).write_text(source, encoding="utf-8")

            proc = subprocess.run(
                limited([
                    "iverilog",
                    "-g2005" if strict else "-g2012",
                    "-o",
                    str(working_dir / "compile_check.out"),
                    *[str(working_dir / name) for name in sources],
                ]),
                capture_output=True,
                text=True,
                timeout=COMPILE_TIMEOUT,
                cwd=working_dir,
            )

            # Report paths relative to the file names the AI sees.
            errors = proc.stderr.replace(str(working_dir) + "/", "")

            return {
                "success": proc.returncode == 0,
                "errors": None if proc.returncode == 0 else (
                    errors or f"iverilog exited with code {proc.returncode}."
                ),
                "warnings": errors if proc.returncode == 0 and errors else None,
                "testbench_code": testbench_code,
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "errors": "Verilog compilation timed out.",
                "warnings": None, "testbench_code": testbench_code}

    except FileNotFoundError:
        return {
            "success": False,
            "errors": (
                "Icarus Verilog was not found. "
                "Install iverilog and vvp before simulating."
            ),
            "warnings": None,
            "testbench_code": testbench_code,
            # The AI cannot fix a missing compiler, so do not retry.
            "tool_missing": True,
        }


def simulate(
    code: str = None,
    module_code: str = None,
    testbench_code: str = None
):
    """
    Compile and simulate Verilog code.

    Supports:
    1. Legacy single-file simulation using code.
    2. Separate design and testbench files.
    3. Automatic missing-counter declaration repair.
    4. Automatic VCD waveform discovery.
    """

    if module_code is not None and testbench_code is not None:

        if not module_code.strip() or not testbench_code.strip():
            return {
                "success": False,
                "error": "Module code and testbench code cannot be empty."
            }

        if len(module_code) > MAX_CODE_SIZE or len(testbench_code) > MAX_CODE_SIZE:
            return {
                "success": False,
                "error": "Verilog code exceeds the maximum allowed size."
            }

        # Repair missing testbench counter declarations.
        testbench_code = repair_testbench_counters(testbench_code)

        design_filename = "design.v"
        testbench_filename = "testbench.v"
        source_files = [design_filename, testbench_filename]
        sources = [module_code, testbench_code]

    elif code is not None and code.strip():

        if len(code) > MAX_CODE_SIZE:
            return {
                "success": False,
                "error": "Verilog code exceeds the maximum allowed size."
            }

        design_filename = "design.v"
        source_files = [design_filename]
        sources = [code]
        module_code = None
        testbench_code = None

    else:
        return {
            "success": False,
            "error": "Provide either code or both module_code and testbench_code."
        }

    unsafe = sorted({
        construct
        for source in sources
        for construct in find_unsafe_constructs(source)
    })

    if unsafe:
        return {
            "success": False,
            "error": (
                "File access is not allowed in simulations. Remove: "
                + ", ".join(unsafe)
            )
        }

    if not icarus_installed():
        return {"success": False, "error": MISSING_ICARUS}

    try:
        with tempfile.TemporaryDirectory(prefix="logicforge_") as temp_dir:

            working_dir = Path(temp_dir)

            (working_dir / design_filename).write_text(
                module_code if module_code is not None else code,
                encoding="utf-8"
            )

            if testbench_code is not None:
                (working_dir / testbench_filename).write_text(
                    testbench_code,
                    encoding="utf-8"
                )

            output_file = working_dir / "simulation.out"

            compile_command = [
                "iverilog",
                "-g2012",
                "-o",
                str(output_file),
                *[
                    str(working_dir / filename)
                    for filename in source_files
                ]
            ]

            try:
                compile_proc = subprocess.run(
                    limited(compile_command),
                    capture_output=True,
                    text=True,
                    timeout=COMPILE_TIMEOUT,
                    cwd=working_dir
                )

            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "error": "Verilog compilation timed out."
                }

            if compile_proc.returncode != 0:
                return {
                    "success": False,
                    "error": compile_proc.stderr,
                    "output": compile_proc.stdout
                }

            try:
                run_proc = subprocess.run(
                    limited(["vvp", str(output_file)]),
                    capture_output=True,
                    text=True,
                    timeout=SIMULATION_TIMEOUT,
                    cwd=working_dir
                )

            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "error": "Simulation timed out. Check for an infinite loop."
                }

            # Automatically discover generated VCD files.
            vcd_files = sorted(working_dir.glob("*.vcd"))

            waveform = None
            waveform_filename = None
            waveform_error = None

            if vcd_files:
                waveform_file = vcd_files[0]
                waveform_filename = waveform_file.name

                try:
                    if waveform_file.stat().st_size > MAX_VCD_SIZE:
                        waveform_error = (
                            "VCD file exceeds the maximum size of "
                            "1 MB and was not returned."
                        )
                    else:
                        waveform = waveform_file.read_text(
                            encoding="utf-8",
                            errors="replace"
                        )

                except OSError as exc:
                    waveform_error = f"Could not read VCD file: {exc}"

            else:
                waveform_error = (
                    "No VCD file was generated. Add $dumpfile and "
                    "$dumpvars to the testbench to generate waveforms."
                )

            summary = parse_test_summary(run_proc.stdout)

            # Any FAIL line (or OVERALL FAIL) means a check failed, even if
            # the testbench never printed a final summary. A summary that
            # reports failures or zero tests is also a failure. A testbench
            # without a summary keeps the old behaviour (pass when nothing
            # failed) but is reported as UNVERIFIED.
            test_failure = summary["verdict"] in ("FAIL", "NO_TESTS")

            success = (
                run_proc.returncode == 0
                and not test_failure
            )

            if run_proc.returncode != 0:
                error = run_proc.stderr or (
                    f"Simulation exited with code {run_proc.returncode}."
                )
            elif summary["verdict"] == "NO_TESTS":
                error = "The testbench executed zero test cases."
            elif test_failure:
                error = "One or more test cases failed."
            else:
                error = None

            return {
                "success": success,
                "output": run_proc.stdout,
                "error": error,
                "warnings": compile_proc.stderr or None,
                "vcd": waveform,
                "waveform": waveform,
                "waveform_filename": waveform_filename,
                "waveform_error": waveform_error,
                "verdict": summary["verdict"] if run_proc.returncode == 0 else "ERROR",
                "total_tests": summary["total_tests"],
                "failures": summary["failures"],
            }

    except FileNotFoundError:
        return {
            "success": False,
            "error": (
                "Icarus Verilog was not found. "
                "Install iverilog and vvp before simulating."
            )
        }

    except Exception as exc:
        return {
            "success": False,
            "error": f"Simulation error: {str(exc)}"
        }
