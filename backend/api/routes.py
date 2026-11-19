from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.ai_service import generate_verilog, debug_verilog, generate_testbench
from services.simulation_service import simulate

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


@router.post("/generate")
def generate(req: Prompt):
    try:
        result = generate_verilog(req.prompt)

        return {
            "success": True,
            "code": result["module_code"],
            "module_code": result["module_code"],
            "testbench_code": result["testbench_code"],
            "explanation": result["explanation"],
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

@router.post("/generate-testbench")
def generate_testbench_code(req: TestbenchRequest):
    try:
        result = generate_testbench(
            module_code=req.module_code
        )

        return {
            "success": True,
            **result
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
