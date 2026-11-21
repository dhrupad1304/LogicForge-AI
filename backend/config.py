import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ---------------------------------------------------------------------------
# AI model selection: LOGICFORGE_AI_PROVIDER
#
#   ollama      local model through Ollama (default; free, offline)
#   groq        Groq API, free tier          key: LOGICFORGE_AI_API_KEY
#   gemini      Google Gemini API, free tier key: LOGICFORGE_AI_API_KEY
#   openrouter  OpenRouter (":free" models)  key: LOGICFORGE_AI_API_KEY
#   openai      any other OpenAI-compatible API (set LOGICFORGE_AI_BASE_URL)
#   anthropic   Claude API (paid)            key: ANTHROPIC_API_KEY
#   none        AI features switched off (simulation and schematics still work)
#
# A cloud provider without its key switches AI off instead of failing.
# Set these in backend/.env or the host's environment, then restart.
# ---------------------------------------------------------------------------

AI_PROVIDER = os.getenv("LOGICFORGE_AI_PROVIDER", "ollama").strip().lower()

OLLAMA_URL = os.getenv("LOGICFORGE_OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.getenv("LOGICFORGE_OLLAMA_MODEL", "qwen2.5-coder:3b")

ANTHROPIC_MODEL = os.getenv("LOGICFORGE_ANTHROPIC_MODEL", "claude-opus-5-5")
# Thinking depth on Claude: low | medium | high | xhigh | max
ANTHROPIC_EFFORT = os.getenv("LOGICFORGE_ANTHROPIC_EFFORT", "high")

# OpenAI-compatible providers: (display name, base URL, default model).
# Model names change over time; override with LOGICFORGE_AI_MODEL using a
# name from the provider's model list.
OPENAI_COMPATIBLE_PRESETS = {
    "groq": ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
               "gemini-2.5-flash"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1",
                   "qwen/qwen-2.5-coder-32b-instruct:free"),
    "openai": ("OpenAI-compatible", "", ""),
}

_preset = OPENAI_COMPATIBLE_PRESETS.get(AI_PROVIDER, ("", "", ""))
AI_PROVIDER_NAME = _preset[0]
AI_BASE_URL = (os.getenv("LOGICFORGE_AI_BASE_URL") or _preset[1]).rstrip("/")
AI_MODEL = os.getenv("LOGICFORGE_AI_MODEL") or _preset[2]
AI_API_KEY = os.getenv("LOGICFORGE_AI_API_KEY", "").strip()

VALID_PROVIDERS = ("ollama", "anthropic", "none", *OPENAI_COMPATIBLE_PRESETS)
if AI_PROVIDER not in VALID_PROVIDERS:
    raise ValueError(
        f"LOGICFORGE_AI_PROVIDER must be one of {', '.join(VALID_PROVIDERS)}, "
        f"not {AI_PROVIDER!r}"
    )


def _key_present(value: str | None) -> bool:
    return bool(value and value.strip() and value.strip().lower() != "none")


def ai_disabled_reason() -> str:
    """Why AI is off ("" when it is on)."""
    if AI_PROVIDER == "none":
        return "AI features are turned off on this server."
    if AI_PROVIDER == "anthropic" and not (
        _key_present(os.getenv("ANTHROPIC_API_KEY"))
        or _key_present(os.getenv("ANTHROPIC_AUTH_TOKEN"))
    ):
        return "AI is off: no Claude API key is configured on this server."
    if AI_PROVIDER in OPENAI_COMPATIBLE_PRESETS:
        if not _key_present(AI_API_KEY):
            return f"AI is off: no {AI_PROVIDER_NAME} API key is configured on this server."
        if not AI_BASE_URL or not AI_MODEL:
            return "AI is off: set LOGICFORGE_AI_BASE_URL and LOGICFORGE_AI_MODEL."
    return ""


AI_ENABLED = not ai_disabled_reason()

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

def model_label() -> str:
    """Short description of the active model for the UI."""
    if ai_disabled_reason():
        return "AI off"
    if AI_PROVIDER == "anthropic":
        return f"{ANTHROPIC_MODEL} · Claude API"
    if AI_PROVIDER in OPENAI_COMPATIBLE_PRESETS:
        return f"{AI_MODEL} · {AI_PROVIDER_NAME}"
    return f"{OLLAMA_MODEL} · local"
