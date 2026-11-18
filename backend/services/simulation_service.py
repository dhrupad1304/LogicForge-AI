import subprocess
from utils.file_utils import save_file

def simulate(code: str):
    file = save_file(code)
    out_file = file.replace(".v", ".out")

    compile_proc = subprocess.run(
        ["iverilog", "-o", out_file, file],
        capture_output=True,
        text=True
    )

    if compile_proc.returncode != 0:
        return {"success": False, "error": compile_proc.stderr}

    run_proc = subprocess.run(
        ["vvp", out_file],
        capture_output=True,
        text=True
    )

    return {
        "success": True,
        "output": run_proc.stdout
    }