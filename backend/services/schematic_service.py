"""
Circuit schematics from Verilog.

Yosys synthesizes the design into a netlist and netlistsvg draws it:
- "gates": bit-level logic gates (AND/OR/XOR/NOT/MUX) and D flip-flops,
  like a textbook schematic.
- "rtl":   word-level blocks (adders, comparators, multiplexers, registers)
  with bus widths; readable for wide or larger designs.

Flip-flops with reset or enable are drawn as a plain D flip-flop with a
multiplexer in front (Yosys: async2sync + dffunmap), which every viewer
understands and the drawing library has symbols for. Asynchronous resets
therefore appear as that same mux; the note in the response says so.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from services.simulation_service import MAX_CODE_SIZE, find_unsafe_constructs

YOSYS_TIMEOUT = 30
DRAW_TIMEOUT = 30
# Above this many gates the gate view is unreadable; "auto" picks RTL.
AUTO_GATE_LIMIT = 80
# Above this the layout engine becomes slow; gate view is refused.
MAX_GATE_CELLS = 600

NETLISTSVG = (
    Path(__file__).resolve().parent.parent
    / "node_modules" / "netlistsvg" / "bin" / "netlistsvg.js"
)

_PREPARE = "proc; flatten; async2sync; dffunmap; opt -nosdff -nodffe"
_SCRIPTS = {
    "rtl": f"{_PREPARE}; opt_clean",
    "gates": f"{_PREPARE}; techmap; opt -nosdff -nodffe; dffunmap; opt_clean",
}


def _tools_missing() -> str:
    if not shutil.which("yosys"):
        return "Yosys is not installed. Install it with: brew install yosys"
    if not shutil.which("node"):
        return "Node.js is not installed; it is needed to draw schematics."
    if not NETLISTSVG.exists():
        return "netlistsvg is not installed. Run: cd backend && npm install"
    return ""


def _synthesize(work: Path, view: str) -> tuple[dict | None, str]:
    """Run Yosys for one view; return (netlist json, error)."""

    out = work / f"{view}.json"
    proc = subprocess.run(
        [
            "yosys", "-q", "-p",
            f"read_verilog -sv design.v; hierarchy -auto-top; {_SCRIPTS[view]}; "
            f"write_json {out.name}",
        ],
        capture_output=True,
        text=True,
        timeout=YOSYS_TIMEOUT,
        cwd=work,
    )
    if proc.returncode != 0 or not out.exists():
        errors = [
            line for line in (proc.stdout + proc.stderr).splitlines()
            if "ERROR" in line or "error" in line
        ]
        return None, "\n".join(errors[:8]) or "Yosys could not synthesize the design."
    return json.loads(out.read_text()), ""


def _cell_count(netlist: dict) -> int:
    modules = netlist.get("modules", {})
    top = next(
        (m for m in modules.values() if m.get("attributes", {}).get("top")),
        next(iter(modules.values()), {}),
    )
    return len(top.get("cells", {}))


def _draw(work: Path, view: str) -> tuple[str, str]:
    svg_path = work / f"{view}.svg"
    proc = subprocess.run(
        ["node", str(NETLISTSVG), f"{view}.json", "-o", svg_path.name],
        capture_output=True,
        text=True,
        timeout=DRAW_TIMEOUT,
        cwd=work,
    )
    if proc.returncode != 0 or not svg_path.exists():
        return "", (proc.stderr.strip().splitlines() or ["Drawing failed."])[-1]
    return svg_path.read_text(), ""


# Dark-theme colours matching the app: grey wires, amber symbols, light text.
_THEME_STYLE = """<style>svg {
    stroke:#8a8a93;
    fill:none;
  }
  path, circle, rect, polygon, ellipse {
    stroke:#f0a43a;
  }
  text {
    fill:#ececee;
    stroke:none;
    font-size:10px;
    font-weight:600;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  .nodelabel {
    text-anchor: middle;
  }
  .inputPortLabel {
    text-anchor: end;
  }
  .splitjoinBody {
    fill:#8a8a93;
    stroke:#8a8a93;
  }</style>"""


def _theme(svg: str) -> str:
    start, end = svg.find("<style>"), svg.find("</style>")
    if start == -1 or end == -1:
        return svg
    return svg[:start] + _THEME_STYLE + svg[end + len("</style>"):]


def render_schematic(module_code: str, view: str = "auto") -> dict:
    """
    Draw the design. view: "auto" | "gates" | "rtl".
    Returns {success, svg, view, cells, gate_cells, rtl_cells, note, error}.
    """

    if view not in ("auto", "gates", "rtl"):
        return {"success": False, "error": "view must be auto, gates or rtl."}
    if not module_code or not module_code.strip():
        return {"success": False, "error": "There is no design to draw."}
    if len(module_code) > MAX_CODE_SIZE:
        return {"success": False, "error": "The design exceeds the maximum allowed size."}
    unsafe = find_unsafe_constructs(module_code)
    if unsafe:
        return {"success": False, "error": "File access is not allowed: " + ", ".join(unsafe)}
    missing = _tools_missing()
    if missing:
        return {"success": False, "error": missing}

    try:
        with tempfile.TemporaryDirectory(prefix="logicforge_sch_") as temp_dir:
            work = Path(temp_dir)
            (work / "design.v").write_text(module_code, encoding="utf-8")

            rtl, error = _synthesize(work, "rtl")
            if rtl is None:
                return {"success": False, "error": error}
            gates, error = _synthesize(work, "gates")
            gate_cells = _cell_count(gates) if gates else None
            rtl_cells = _cell_count(rtl)

            chosen = view
            if view == "auto":
                chosen = "gates" if gate_cells is not None and gate_cells <= AUTO_GATE_LIMIT else "rtl"
            if chosen == "gates":
                if gates is None:
                    return {"success": False, "error": error}
                if gate_cells > MAX_GATE_CELLS:
                    return {
                        "success": False,
                        "error": (
                            f"The gate-level circuit has {gate_cells} cells, too many "
                            "to draw legibly. Use the RTL view."
                        ),
                        "gate_cells": gate_cells,
                        "rtl_cells": rtl_cells,
                    }

            svg, error = _draw(work, chosen)
            if not svg:
                return {"success": False, "error": error}
            svg = _theme(svg)

            note = ""
            if "posedge" in module_code or "negedge" in module_code:
                note = (
                    "Flip-flops with reset or enable are drawn as a D flip-flop "
                    "with a multiplexer in front of it."
                )
            if view == "auto" and chosen == "rtl" and gate_cells:
                note = (note + " " if note else "") + (
                    f"Shown as RTL blocks: the gate-level version has {gate_cells} gates."
                )

            return {
                "success": True,
                "svg": svg,
                "view": chosen,
                "cells": gate_cells if chosen == "gates" else rtl_cells,
                "gate_cells": gate_cells,
                "rtl_cells": rtl_cells,
                "note": note,
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Drawing the schematic took too long."}
    except (OSError, json.JSONDecodeError) as exc:
        return {"success": False, "error": f"Schematic error: {exc}"}
