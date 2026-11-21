"""Deployment protections: access password, rate limits, CORS, tool sandboxing."""

import pytest
from fastapi.testclient import TestClient

import config
import main
from api import routes
from api.security import limiter
from services.simulation_service import limited

ORIGIN = {"Origin": "http://localhost:3000"}


@pytest.fixture
def client(monkeypatch):
    limiter.reset()
    monkeypatch.setattr(routes, "generate_design_and_testbench", lambda prompt: {
        "module_code": "module m; endmodule", "testbench_code": "", "explanation": "",
        "verification": {"ready": True},
    })
    yield TestClient(main.app)
    limiter.reset()


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_PASSWORD", "s3cret-pass")
    return "s3cret-pass"


# ---------------------------------------------------------------- password

def test_no_password_needed_by_default(client):
    assert client.get("/auth/status").json() == {"password_required": False}
    assert client.get("/model").status_code == 200


def test_password_is_required_when_configured(client, password):
    assert client.get("/auth/status").json() == {"password_required": True}
    assert client.get("/model").status_code == 401
    assert client.get("/model", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/model", headers={"Authorization": f"Bearer {password}"}).status_code == 200


def test_health_check_stays_public(client, password):
    assert client.get("/").status_code == 200


def test_every_api_route_is_protected(client, password):
    for method, path in [("get", "/project/list"), ("post", "/simulate"), ("post", "/generate"),
                         ("post", "/schematic"), ("post", "/debug"), ("post", "/project/delete")]:
        response = client.get(path) if method == "get" else client.post(path, json={})
        assert response.status_code == 401, path


def test_unauthorized_response_is_readable_by_the_frontend(client, password):
    # CORS headers must be present, or the browser hides the 401 behind a CORS error.
    response = client.get("/model", headers=ORIGIN)
    assert response.status_code == 401
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_authorization_header_is_allowed_by_cors(client):
    preflight = client.options("/generate", headers={
        **ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert preflight.status_code == 200
    assert "authorization" in preflight.headers.get("access-control-allow-headers", "").lower()


# ---------------------------------------------------------------- rate limits

def test_ai_requests_are_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "AI_REQUESTS_PER_HOUR", 2)
    for _ in range(2):
        assert client.post("/generate", json={"prompt": "full adder"}).status_code == 200
    blocked = client.post("/generate", json={"prompt": "full adder"}, headers=ORIGIN)
    assert blocked.status_code == 429
    assert "Hourly AI limit" in blocked.json()["detail"]
    assert int(blocked.headers["Retry-After"]) > 0
    assert blocked.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_limits_are_per_visitor(client, monkeypatch):
    monkeypatch.setattr(config, "AI_REQUESTS_PER_HOUR", 1)
    first = {"X-Forwarded-For": "203.0.113.1"}
    second = {"X-Forwarded-For": "203.0.113.2"}
    assert client.post("/generate", json={"prompt": "full adder"}, headers=first).status_code == 200
    assert client.post("/generate", json={"prompt": "full adder"}, headers=first).status_code == 429
    assert client.post("/generate", json={"prompt": "full adder"}, headers=second).status_code == 200


def test_zero_means_unlimited(client, monkeypatch):
    monkeypatch.setattr(config, "AI_REQUESTS_PER_HOUR", 0)
    for _ in range(5):
        assert client.post("/generate", json={"prompt": "full adder"}).status_code == 200


def test_cheap_routes_are_not_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "AI_REQUESTS_PER_HOUR", 1)
    monkeypatch.setattr(config, "COMPUTE_REQUESTS_PER_HOUR", 1)
    for _ in range(3):
        assert client.get("/model").status_code == 200


# ---------------------------------------------------------------- sandbox

def test_tools_run_under_resource_limits():
    command = limited(["iverilog", "-o", "out", "design.v"])
    assert command[:2] == ["/bin/sh", "-c"]
    assert "ulimit -t" in command[2] and "-f" in command[2]
    assert command[-4:] == ["iverilog", "-o", "out", "design.v"]
