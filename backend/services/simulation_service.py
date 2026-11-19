
import re
import subprocess
import tempfile
from pathlib import Path


COMPILE_TIMEOUT = 15
SIMULATION_TIMEOUT = 15
MAX_CODE_SIZE = 50000
MAX_VCD_SIZE = 1000000


def repair_testbench_counters(testbench_code: str) -> str:
    """
    Automatically declare missing testbench counters when they are used.
    Existing declarations are preserved.
    """

    counters = ("failures", "total_tests")
    missing_counters = []

    for counter in counters:
        # Only add a counter if the testbench actually uses it.
        if not re.search(rf"\b{counter}\b", testbench_code):
            continue

        # Check whether the counter is already declared.
        declaration_pattern = (
            rf"\b(?:integer|int|reg|logic|wire)\s+"
            rf"(?:\[[^\]]+\]\s*)?[^;]*\b{counter}\b[^;]*;"
        )

        if not re.search(
            declaration_pattern,
            testbench_code,
            flags=re.IGNORECASE
        ):
            missing_counters.append(counter)

    if not missing_counters:
        return testbench_code

    # Find the first module declaration.
    module_pattern = r"(?m)^\s*module\s+[\w$]+\s*(?:\([^;]*\))?\s*;"

    match = re.search(module_pattern, testbench_code)

    if not match:
        return testbench_code

    declarations = "\n\n" + "\n".join(
        f"  integer {counter};"
        for counter in missing_counters
    ) + "\n"

    # Insert declarations immediately after the module header.
    return (
        testbench_code[:match.end()]
        + declarations
        + testbench_code[match.end():]
    )


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

    elif code is not None and code.strip():

        if len(code) > MAX_CODE_SIZE:
            return {
                "success": False,
                "error": "Verilog code exceeds the maximum allowed size."
            }

        design_filename = "design.v"
        source_files = [design_filename]

    else:
        return {
            "success": False,
            "error": "Provide either code or both module_code and testbench_code."
        }

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
                    compile_command,
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
                    ["vvp", str(output_file)],
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

            # Detect explicit testbench failure.
            overall_failure = "OVERALL FAIL" in run_proc.stdout

            success = (
                run_proc.returncode == 0
                and not overall_failure
            )

            return {
                "success": success,
                "output": run_proc.stdout,
                "error": (
                    run_proc.stderr
                    if run_proc.returncode != 0
                    else None
                ),
                "vcd": waveform,
                "waveform": waveform,
                "waveform_filename": waveform_filename,
                "waveform_error": waveform_error
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
