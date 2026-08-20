from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from services.catalog import ScenarioCatalog
from services.data_plane import create_app as create_data_plane
from services.identity import KeyAuthentication, KeyPrincipal
from services.provider_simulator import create_app as create_simulator
from services.service_identity import ServiceIdentityError


KEY = "gw_" + secrets.token_hex(32)
SIMULATOR_TOKEN = "synthetic-simulator-identity"
CATALOG_DOCUMENT = json.loads(
    (Path(__file__).resolve().parents[1] / "scenarios" / "catalog.json").read_text(encoding="utf-8")
)
SCENARIOS = {scenario["id"]: scenario for scenario in CATALOG_DOCUMENT["scenarios"]}


class TestKeyStore:
    def authenticate_key(self, raw_key: str, required_scope: str) -> KeyAuthentication:
        if secrets.compare_digest(raw_key, KEY) and required_scope == "chat:completions":
            return KeyAuthentication(
                KeyPrincipal("0123456789abcdef", "tenant_test", ("chat:completions",)),
                "ok",
            )
        return KeyAuthentication(None, "invalid_api_key")


def simulator_identity(headers) -> None:
    if headers.get("Authorization") != f"Bearer {SIMULATOR_TOKEN}":
        raise ServiceIdentityError("invalid synthetic simulator identity")


def request_headers(scenario_id: str, *, authenticated: bool = True) -> dict[str, str]:
    headers = {"X-Gateway-Scenario-ID": scenario_id}
    if authenticated:
        headers["Authorization"] = f"Bearer {KEY}"
    return headers


def data_client(
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    timeout: float = 1,
) -> tuple[TestClient, object]:
    simulator = create_simulator(identity_verifier=simulator_identity)
    provider_transport = transport or httpx.ASGITransport(app=simulator)
    application = create_data_plane(
        provider_transport=provider_transport,
        provider_api_keys={"simulator": SIMULATOR_TOKEN},
        key_store=TestKeyStore(),
        provider_timeout=timeout,
    )
    return TestClient(application), application


def test_catalog_health_auth_and_hosted_boundary() -> None:
    ScenarioCatalog()
    client, application = data_client()
    assert client.get("/healthz").json() == {
        "status": "ok",
        "service": "data-plane",
        "provider": "simulator",
    }
    assert client.get("/v1/models").status_code == 404

    scenario = SCENARIOS["text.nonstream"]
    missing_auth = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"], authenticated=False),
        json=scenario["request"],
    )
    assert missing_auth.status_code == 401
    assert missing_auth.json()["error"]["code"] == "invalid_api_key"

    altered = json.loads(json.dumps(scenario["request"]))
    altered["messages"][0]["content"] = "UNRETAINED_INPUT_CANARY"
    refused = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=altered,
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "hosted_prompt_not_allowed"
    assert "UNRETAINED_INPUT_CANARY" not in refused.text
    assert list(application.state.receipts) == []

    invalid = {**scenario["request"], "unsupported_provider_field": "UNRETAINED_SCHEMA_CANARY"}
    validation = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=invalid,
    )
    assert validation.status_code == 400
    assert validation.json()["error"]["code"] == "invalid_request"
    assert "UNRETAINED_SCHEMA_CANARY" not in validation.text


def test_nonstreaming_response_headers_and_metadata_only_receipt() -> None:
    client, application = data_client()
    scenario = SCENARIOS["text.nonstream"]
    response = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "gateway/general"
    assert body["choices"][0]["message"] == scenario["response"]["message"]
    assert body["usage"]["total_tokens"] == 25
    assert response.headers["x-request-id"] == body["id"]
    assert len(response.headers["x-trace-id"]) == 32
    assert response.headers["x-gateway-provider"] == "simulator"
    assert response.headers["x-gateway-attempts"] == "1"
    assert response.headers["x-gateway-cost-usd"] == "0.000000"

    receipt_text = json.dumps(list(application.state.receipts), sort_keys=True)
    assert scenario["request"]["messages"][0]["content"] not in receipt_text
    assert scenario["response"]["message"]["content"] not in receipt_text
    receipt = application.state.receipts[-1]
    assert set(receipt) == {
        "request_id",
        "trace_id",
        "tenant_id",
        "key_id",
        "scenario_id",
        "route",
        "provider",
        "provider_model",
        "model",
        "attempts",
        "attempted_targets",
        "outcome",
        "input_characters",
        "output_characters",
        "tool_count",
        "response_format",
        "usage",
        "cost_inputs",
        "cost_usd",
        "accounting_status",
        "recorded_at",
    }


@pytest.mark.parametrize("scenario_id", ["tools.weather", "structured.release"])
def test_tools_and_structured_output(scenario_id: str) -> None:
    client, _ = data_client()
    scenario = SCENARIOS[scenario_id]
    response = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario_id),
        json=scenario["request"],
    )
    assert response.status_code == 200
    message = response.json()["choices"][0]["message"]
    if scenario_id == "tools.weather":
        assert message["content"] is None
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
    else:
        assert json.loads(message["content"]) == {"status": "on_track", "risk": "low"}


def test_sse_streaming_and_partial_failure() -> None:
    client, application = data_client()
    success = SCENARIOS["text.stream"]
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=request_headers(success["id"]),
        json=success["request"],
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["total_tokens"] == 17
    assert all(chunk["model"] == "gateway/general" for chunk in chunks)
    assert application.state.receipts[-1]["outcome"] == "complete"

    partial = SCENARIOS["fault.partial_stream"]
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=request_headers(partial["id"]),
        json=partial["request"],
    ) as response:
        partial_lines = [line for line in response.iter_lines() if line]
    assert partial_lines[-1] == "data: [DONE]"
    assert sum('"error"' in line for line in partial_lines) == 1
    assert application.state.receipts[-1]["outcome"] == "partial"


def test_simulator_fault_modes_and_data_plane_mapping() -> None:
    simulator = TestClient(create_simulator(identity_verifier=simulator_identity))
    simulator_headers = {"Authorization": f"Bearer {SIMULATOR_TOKEN}"}
    started = time.perf_counter()
    latency = simulator.post("/simulate/chat/completions", headers=simulator_headers, json={"scenario_id": "fault.latency"})
    assert latency.status_code == 200
    assert time.perf_counter() - started >= 0.015
    assert simulator.post("/simulate/chat/completions", headers=simulator_headers, json={"scenario_id": "fault.rate_limit"}).status_code == 429
    malformed = simulator.post("/simulate/chat/completions", headers=simulator_headers, json={"scenario_id": "fault.malformed"})
    assert malformed.status_code == 200
    with pytest.raises(json.JSONDecodeError):
        malformed.json()

    client, _ = data_client()
    rate = SCENARIOS["fault.rate_limit"]
    rate_response = client.post(
        "/v1/chat/completions",
        headers=request_headers(rate["id"]),
        json=rate["request"],
    )
    assert rate_response.status_code == 503
    assert rate_response.json()["error"]["code"] == "no_eligible_provider"

    bad = SCENARIOS["fault.malformed"]
    bad_response = client.post(
        "/v1/chat/completions",
        headers=request_headers(bad["id"]),
        json=bad["request"],
    )
    assert bad_response.status_code == 502
    assert bad_response.json()["error"]["code"] == "provider_protocol_error"

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    timeout_client, _ = data_client(httpx.MockTransport(timeout_handler), timeout=0.01)
    timeout = SCENARIOS["fault.timeout"]
    timeout_response = timeout_client.post(
        "/v1/chat/completions",
        headers=request_headers(timeout["id"]),
        json=timeout["request"],
    )
    assert timeout_response.status_code == 504
    assert timeout_response.json()["error"]["code"] == "provider_timeout"
