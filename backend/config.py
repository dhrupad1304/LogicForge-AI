import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ---------------------------------------------------------------------------
# AI model selection
#
#   LOGICFORGE_AI_PROVIDER = ollama     (default: local, free, offline)
#                          = anthropic  (Claude API: needs ANTHROPIC_API_KEY)
#
# Set these in backend/.env or the environment, then restart the backend.
# ---------------------------------------------------------------------------

AI_PROVIDER = os.getenv("LOGICFORGE_AI_PROVIDER", "ollama").strip().lower()

OLLAMA_URL = os.getenv("LOGICFORGE_OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.getenv("LOGICFORGE_OLLAMA_MODEL", "qwen2.5-coder:3b")

ANTHROPIC_MODEL = os.getenv("LOGICFORGE_ANTHROPIC_MODEL", "claude-opus-5-5")
# Thinking depth on Claude: low | medium | high | xhigh | max
ANTHROPIC_EFFORT = os.getenv("LOGICFORGE_ANTHROPIC_EFFORT", "high")

# How many design / reference-model attempts the generate pipeline may make
# before it returns its best result.
MAX_GENERATION_ATTEMPTS = int(os.getenv("LOGICFORGE_MAX_ATTEMPTS", "3"))

# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

# Shared access password. Empty (the default, for local use) = no password.
ACCESS_PASSWORD = os.getenv("LOGICFORGE_ACCESS_PASSWORD", "")

# Per-visitor hourly limits (0 = unlimited). AI requests cost API money.
AI_REQUESTS_PER_HOUR = int(os.getenv("LOGICFORGE_AI_REQUESTS_PER_HOUR", "30"))
COMPUTE_REQUESTS_PER_HOUR = int(os.getenv("LOGICFORGE_COMPUTE_REQUESTS_PER_HOUR", "300"))

if AI_PROVIDER not in ("ollama", "anthropic"):
    raise ValueError(
        f"LOGICFORGE_AI_PROVIDER must be 'ollama' or 'anthropic', not {AI_PROVIDER!r}"
    )


def model_label() -> str:
    """Short description of the active model for the UI."""
    if AI_PROVIDER == "anthropic":
        return f"{ANTHROPIC_MODEL} · Claude API"
    return f"{OLLAMA_MODEL} · local"
