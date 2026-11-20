import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import router

app = FastAPI()

# Only the local frontend may call this API. Allowing every origin would let
# any website the user visits read, overwrite or delete workspace files.
# Override with a comma-separated list in LOGICFORGE_ALLOWED_ORIGINS.
DEFAULT_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("LOGICFORGE_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(router)

@app.get("/")
def root():
    return {"message": "LogicForge AI running 🚀"}
