from fastapi import APIRouter
from pydantic import BaseModel

from services.ai_service import generate_verilog
from services.simulation_service import simulate

router = APIRouter()

class Prompt(BaseModel):
    prompt: str

class Code(BaseModel):
    code: str

@router.post("/generate")
def generate(req: Prompt):
    code = generate_verilog(req.prompt)
    return {"code": code}

@router.post("/simulate")
def simulate_code(req: Code):
    return simulate(req.code)