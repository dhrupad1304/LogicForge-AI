from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import config
from services.ai_service import debug_verilog
from services.generation_pipeline import (
    generate_design_and_testbench,
    generate_testbench_for,
)
from services.simulation_service import simulate
from services.schematic_service import render_schematic
from services import project_service

router = APIRouter()


class Prompt(BaseModel):
    prompt: str = Field(min_length=5, max_length=5000)


class Code(BaseModel):
    code: Optional[str] = Field(default=None, max_length=50000)
    module_code: Optional[str] = Field(default=None, max_length=50000)
    testbench_code: Optional[str] = Field(default=None, max_length=50000)

class DebugRequest(BaseModel):
    module_code: str = Field(min_length=1, max_length=50000)
    testbench_code: str = Field(min_length=1, max_length=50000)
    error: str = Field(default="", max_length=20000)
    output: str = Field(default="", max_length=20000)
class TestbenchRequest(BaseModel):
    module_code: str = Field(min_length=1, max_length=50000)


class SchematicRequest(BaseModel):
    module_code: str = Field(min_length=1, max_length=50000)
    view: str = Field(default="auto", pattern="^(auto|gates|rtl)$")


@router.get("/model")
def model_info():
    """The AI model the backend is configured to use."""
    return {
        "provider": config.AI_PROVIDER,
        "model": (
            config.ANTHROPIC_MODEL if config.AI_PROVIDER == "anthropic"
            else config.OLLAMA_MODEL
        ),
        "label": config.model_label(),
    }


@router.post("/generate")
def generate(req: Prompt):
    try:
        result = generate_design_and_testbench(req.prompt)

        return {
            "success": True,
            "code": result["module_code"],
            "module_code": result["module_code"],
            "testbench_code": result["testbench_code"],
            "explanation": result["explanation"],
            # Compile / dry-run / auto-repair report (see verify_and_repair).
            "verification": result.get("verification"),
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/simulate")
def simulate_code(req: Code):
    if req.module_code is not None or req.testbench_code is not None:
        if not req.module_code or not req.testbench_code:
            raise HTTPException(
                status_code=400,
                detail="Both module_code and testbench_code are required."
            )

        result = simulate(
            module_code=req.module_code,
            testbench_code=req.testbench_code
        )

    elif req.code:
        result = simulate(code=req.code)

    else:
        raise HTTPException(
            status_code=400,
            detail="Provide code or both module_code and testbench_code."
        )

    return result

@router.post("/debug")
def debug_code(req: DebugRequest):
    try:
        result = debug_verilog(
            module_code=req.module_code,
            testbench_code=req.testbench_code,
            error=req.error,
            output=req.output
        )

        return {
            "success": True,
            **result
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

@router.post("/schematic")
def schematic(req: SchematicRequest):
    """Draw the design as a gate-level or RTL schematic (SVG)."""
    return render_schematic(req.module_code, req.view)


@router.post("/generate-testbench")
def generate_testbench_code(req: TestbenchRequest):
    try:
        result = generate_testbench_for(req.module_code)

        return {
            "success": True,
            **result
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    
class ProjectItemRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    item_type: str = Field(default="file")


class ProjectReadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class ProjectSaveRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=500000)


class ProjectRenameRequest(BaseModel):
    old_path: str = Field(min_length=1, max_length=500)
    new_path: str = Field(min_length=1, max_length=500)


class ProjectDeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


@router.get("/project/list")
def project_list(path: str = ""):
    try:
        return project_service.list_directory(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/project/create")
def project_create(req: ProjectItemRequest):
    try:
        return project_service.create_item(req.path, req.item_type)
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/project/read")
def project_read(req: ProjectReadRequest):
    try:
        return project_service.read_file(req.path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/project/save")
def project_save(req: ProjectSaveRequest):
    try:
        return project_service.save_file(req.path, req.content)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/project/rename")
def project_rename(req: ProjectRenameRequest):
    try:
        return project_service.rename_item(req.old_path, req.new_path)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/project/delete")
def project_delete(req: ProjectDeleteRequest):
    try:
        return project_service.delete_item(req.path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

