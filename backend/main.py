import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from api.routes import router
from api.security import SecurityMiddleware

app = FastAPI(title="LogicForge API")

# Only the configured frontends may call this API. Allowing every origin would
# let any website the user visits read, overwrite or delete workspace files.
#   LOGICFORGE_ALLOWED_ORIGINS       comma-separated exact origins
#   LOGICFORGE_ALLOWED_ORIGIN_REGEX  optional pattern, e.g. for Vercel previews:
#                                    https://logicforge-[a-z0-9-]+\.vercel\.app
DEFAULT_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"
ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("LOGICFORGE_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if origin.strip()
]
ALLOWED_ORIGIN_REGEX = os.getenv("LOGICFORGE_ALLOWED_ORIGIN_REGEX") or None

# Middleware added last runs first: CORS must wrap the security checks so
# that 401 / 429 responses still carry CORS headers the browser can read.
app.add_middleware(SecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(router)


@app.get("/")
def root():
    """Health check (public)."""
    return {"message": "LogicForge AI running", "status": "ok"}


@app.get("/auth/status")
def auth_status():
    """Tells the frontend whether it must ask for the access password."""
    return {"password_required": bool(config.ACCESS_PASSWORD)}
