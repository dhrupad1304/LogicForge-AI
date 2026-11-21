"""Free OpenAI-compatible providers (Groq, Gemini, OpenRouter) and AI-off mode."""

import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

import config
import main
from api.security import limiter
from conftest import requires_iverilog
from services import ai_service


@pytest.fixture
def groq(monkeypatch):
    """Configure the Groq preset with a (fake) key."""
    monkeypatch.setattr(config, "AI_PROVIDER", "groq")
    monkeypatch.setattr(config, "AI_PROVIDER_NAME", "Groq")
    monkeypatch.setattr(config, "AI_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(config, "AI_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(config, "AI_API_KEY", "gsk_test")
    monkeypatch.setattr(ai_service.time, "sleep", lambda seconds: None)


class FakeResponse:
    def __init__(self, body: dict):
        self._data = json.dumps(body).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def chat(content: str, finish: str = "stop") -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": content}, "finish_reason": finish}]})


def http_error(code: int, body: str = "", headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.example/v1/chat/completions", code, "error", headers or {}, io.BytesIO(body.encode())
    )


@pytest.fixture
def fake_api(monkeypatch):
    """Script the provider's responses; records every request payload."""

    def install(*responses):
        queue = list(responses)
        sent = []

        def urlopen(request, timeout=None):
            sent.append({"url": request.full_url, "headers": dict(request.headers),
                         "payload": json.loads(request.data)})
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(ai_service.urllib.request, "urlopen", urlopen)
        return sent

    return install


def call(task="Design generation"):
    return ai_service._call_model(
        system_prompt="Return JSON.", user_prompt="full adder",
        options={"temperature": 0.2, "num_predict": 1000}, timeout=60,
        required_fields=["module_code"], optional_fields=["explanation"], task=task,
    )


# ---------------------------------------------------------------- requests

def test_request_shape_and_json_parsing(groq, fake_api):
    sent = fake_api(chat('```json\n{"module_code": "module m; endmodule"}\n```'))
    result = call()
    assert result == {"module_code": "module m; endmodule", "explanation": ""}
    request = sent[0]
    assert request["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer gsk_test"
    assert request["payload"]["model"] == "llama-3.3-70b-versatile"
    assert request["payload"]["response_format"] == {"type": "json_object"}
    assert request["payload"]["max_tokens"] == 1000


def test_json_mode_rejected_is_retried_without_it(groq, fake_api):
    sent = fake_api(http_error(400, '{"error": "response_format is not supported"}'),
                    chat('{"module_code": "module m; endmodule"}'))
    assert call()["module_code"] == "module m; endmodule"
    assert "response_format" not in sent[1]["payload"]


def test_rate_limit_waits_and_retries(groq, fake_api):
    sent = fake_api(http_error(429, "slow down", {"retry-after": "2"}),
                    chat('{"module_code": "module m; endmodule"}'))
    assert call()["module_code"] == "module m; endmodule"
    assert len(sent) == 2


def test_persistent_rate_limit_gives_a_clear_message(groq, fake_api):
    fake_api(*[http_error(429, "slow down", {"retry-after": "1"})] * 3)
    with pytest.raises(RuntimeError, match="free-tier rate limit"):
        call()


@pytest.mark.parametrize("code, message", [(401, "API key was rejected"),
                                           (404, "does not offer the model")])
def test_auth_and_model_errors_are_readable(groq, fake_api, code, message):
    fake_api(http_error(code, "nope"))
    with pytest.raises(RuntimeError, match=message):
        call()


def test_truncated_output_is_reported(groq, fake_api):
    fake_api(chat('{"module_code": "module m;', finish="length"))
    with pytest.raises(RuntimeError, match="cut off"):
        call()


def test_missing_required_field_is_rejected(groq, fake_api):
    fake_api(chat('{"explanation": "no code here"}'))
    with pytest.raises(RuntimeError, match="module_code"):
        call()


# ---------------------------------------------------------------- AI off

@pytest.mark.parametrize(
    "provider, key, enabled",
    [("none", "", False), ("groq", "", False), ("groq", "none", False),
     ("groq", "gsk_x", True), ("ollama", "", True)],
)
def test_ai_enabled_rules(monkeypatch, provider, key, enabled):
    monkeypatch.setattr(config, "AI_PROVIDER", provider)
    monkeypatch.setattr(config, "AI_PROVIDER_NAME", "Groq")
    monkeypatch.setattr(config, "AI_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(config, "AI_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(config, "AI_API_KEY", key)
    assert (config.ai_disabled_reason() == "") is enabled


def test_anthropic_without_key_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert "no Claude API key" in config.ai_disabled_reason()


@pytest.fixture
def ai_off(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "none")
    limiter.reset()
    return TestClient(main.app)


def test_model_endpoint_reports_ai_off(ai_off):
    data = ai_off.get("/model").json()
    assert data["ai_enabled"] is False
    assert data["label"] == "AI off"
    assert "turned off" in data["ai_disabled_reason"]


@pytest.mark.parametrize("path, body", [
    ("/generate", {"prompt": "full adder"}),
    ("/generate-testbench", {"module_code": "module m(input a, output y); assign y = a; endmodule"}),
    ("/debug", {"module_code": "module m; endmodule", "testbench_code": "module tb; endmodule"}),
])
def test_ai_routes_refuse_clearly_when_off(ai_off, path, body):
    response = ai_off.post(path, json=body)
    assert response.status_code == 503
    assert "turned off" in response.json()["detail"]


def test_model_call_refuses_when_off(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "none")
    with pytest.raises(ai_service.AIDisabledError):
        call()


@requires_iverilog
def test_simulation_still_works_when_ai_is_off(ai_off, full_adder, tb_full_adder):
    data = ai_off.post("/simulate", json={"module_code": full_adder, "testbench_code": tb_full_adder}).json()
    assert data["verdict"] == "PASS"
