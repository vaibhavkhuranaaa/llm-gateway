from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from services.data_plane import create_app as create_data_plane
from services.governance import GovernanceError, Reservation
from services.live_configuration import (
    CredentialResolutionError,
    FirestoreRouteLoader,
    LIVE_PRICE_TABLE_VERSION,
    LIVE_PROVIDER_SPECS,
    SecretManagerCredentialResolver,
)
from services.routing import RoutePolicy, Target
from tests.test_protocol import SCENARIOS, TestKeyStore, request_headers


NOW = datetime(2026, 8, 10, 15, tzinfo=UTC)


def pinned_target(provider: str, *, fallbacks: tuple[str, ...] = ()) -> Target:
    specification = LIVE_PROVIDER_SPECS[provider]
    return Target(
        id=provider,
        provider=provider,
        model=str(specification["model"]),
        base_url=str(specification["origin"]),
        weight=100,
        fallbacks=fallbacks,
    )


def openai_completion(scenario_id: str) -> dict[str, object]:
    scenario = SCENARIOS[scenario_id]
    response = scenario["response"]
    return {
        "id": "synthetic-openai",
        "object": "chat.completion",
        "created": 1,
        "model": LIVE_PROVIDER_SPECS["openai"]["model"],
        "choices": [{"index": 0, "message": response["message"], "finish_reason": response["finish_reason"]}],
        "usage": response["usage"],
    }


class StaticRouteLoader:
    def __init__(self, policy: RoutePolicy) -> None:
        self.policy = policy

    def load(self, tenant_id: str) -> RoutePolicy:
        assert tenant_id == "tenant_test"
        return self.policy


class GateGovernance:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.live_state = "active"
        self.reserve_error: GovernanceError | None = None

    def eligible_live_target_ids(self, tenant_id, targets, *, required_price_table_version=None):
        self.events.append("eligible")
        assert required_price_table_version == LIVE_PRICE_TABLE_VERSION
        return (
            {target.id for target in targets if target.provider != "simulator"}
            if self.live_state == "active"
            else set()
        )

    def reserve(self, **kwargs) -> Reservation:
        self.events.append("reserve")
        assert kwargs["required_price_table_version"] == LIVE_PRICE_TABLE_VERSION
        if self.reserve_error is not None:
            raise self.reserve_error
        return Reservation("request", "tenant_test", "key", "trace", "window", 1024, 1000, 1, "session")

    def reconcile(self, reservation, receipt, **kwargs):
        self.events.append("reconcile")
        return {"cost_usd_micros": 25, "accounting_status": "reconciled"}


def test_firestore_route_loader_accepts_only_current_pinned_configuration() -> None:
    client = firestore.Client(
        project=f"live-route-{uuid.uuid4().hex}",
        credentials=AnonymousCredentials(),
    )
    reference = (
        client.collection("tenants")
        .document("tenant_test")
        .collection("routes")
        .document("gateway-general")
    )
    loader = FirestoreRouteLoader(client, clock=lambda: NOW, simulator_url="http://simulator")
    assert loader.load("tenant_test") is None

    document = {
        "alias": "gateway/general",
        "version": "live-route-v1",
        "enabled": True,
        "price_table_version": LIVE_PRICE_TABLE_VERSION,
        "updated_at": NOW,
        "targets": [
            {"id": "openai", "provider": "openai", "model": LIVE_PROVIDER_SPECS["openai"]["model"], "origin": "openai", "weight": 100, "fallbacks": ["anthropic"]},
            {"id": "anthropic", "provider": "anthropic", "model": LIVE_PROVIDER_SPECS["anthropic"]["model"], "origin": "anthropic", "weight": 0, "fallbacks": []},
        ],
    }
    reference.set(document)
    loaded = loader.load("tenant_test")
    assert loaded is not None
    assert loaded.version == "live-route-v1"
    assert loaded.price_table_version == LIVE_PRICE_TABLE_VERSION
    assert [(target.provider, target.model, target.base_url) for target in loaded.targets] == [
        ("openai", "gpt-5-mini-2025-08-07", "https://api.openai.com"),
        ("anthropic", "claude-haiku-4-5-20251001", "https://api.anthropic.com"),
    ]

    reference.update({"targets": [{**document["targets"][0], "model": "gpt-5-mini"}]})
    assert loader.load("tenant_test") is None
    reference.set({**document, "updated_at": NOW - timedelta(days=31)})
    assert loader.load("tenant_test") is None
    reference.set({**document, "enabled": False})
    assert loader.load("tenant_test") is None


def test_secret_resolver_uses_only_exact_regional_names() -> None:
    class SecretClient:
        def __init__(self) -> None:
            self.names: list[str] = []

        def access_secret_version(self, *, request):
            self.names.append(request["name"])
            return SimpleNamespace(payload=SimpleNamespace(data=b"synthetic-local-key"))

    client = SecretClient()
    resolver = SecretManagerCredentialResolver("project-test", client=client)
    assert resolver(pinned_target("openai")) == "synthetic-local-key"
    assert client.names == [
        "projects/project-test/locations/us-central1/secrets/openai-api-key/versions/latest"
    ]
    with pytest.raises(CredentialResolutionError):
        resolver(Target("openai", "openai", "gpt-5-mini", "https://api.openai.com", weight=100))
    assert len(client.names) == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("scenario_id", ["text.nonstream", "text.stream", "tools.weather", "structured.release"])
def test_pinned_provider_protocols_use_only_synthetic_transports(provider: str, scenario_id: str) -> None:
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        seen.append(wire)
        assert wire["model"] == LIVE_PROVIDER_SPECS[provider]["model"]
        if scenario_id == "text.stream":
            if provider == "openai":
                usage = SCENARIOS[scenario_id]["response"]["usage"]
                body = (
                    'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"index":0,"delta":{"content":"Synthetic stream."},"finish_reason":"stop"}]}\n\n'
                    f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n"
                    "data: [DONE]\n\n"
                )
            else:
                events = [
                    {"type": "message_start", "message": {"usage": {"input_tokens": 8}}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Synthetic stream."}},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
                    {"type": "message_stop"},
                ]
                body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"}, request=request)
        if provider == "openai":
            return httpx.Response(200, json=openai_completion(scenario_id), request=request)
        response = SCENARIOS[scenario_id]["response"]
        content = (
            [{"type": "tool_use", "id": "call_synthetic_weather", "name": "get_weather", "input": {"city": "Sample City"}}]
            if scenario_id == "tools.weather"
            else [{"type": "text", "text": response["message"]["content"]}]
        )
        return httpx.Response(
            200,
            json={"id": "synthetic-anthropic", "content": content, "stop_reason": "tool_use" if scenario_id == "tools.weather" else "end_turn", "usage": {"input_tokens": response["usage"]["prompt_tokens"], "output_tokens": response["usage"]["completion_tokens"]}},
            request=request,
        )

    policy = RoutePolicy(
        "gateway/general",
        "live-pinned",
        (pinned_target(provider),),
        price_table_version=LIVE_PRICE_TABLE_VERSION,
    )
    application = create_data_plane(
        provider_transports={provider: httpx.MockTransport(handler)},
        provider_api_keys={provider: "synthetic-local-key"},
        route_loader=StaticRouteLoader(policy),
        key_store=TestKeyStore(),
    )
    scenario = SCENARIOS[scenario_id]
    client = TestClient(application)
    if scenario["request"].get("stream"):
        with client.stream("POST", "/v1/chat/completions", headers=request_headers(scenario_id), json=scenario["request"]) as response:
            lines = [line for line in response.iter_lines() if line]
        assert response.status_code == 200
        assert lines[-1] == "data: [DONE]"
    else:
        response = client.post("/v1/chat/completions", headers=request_headers(scenario_id), json=scenario["request"])
        assert response.status_code == 200
    assert len(seen) == 1
    assert application.state.receipts[-1]["provider_model"] == LIVE_PROVIDER_SPECS[provider]["model"]


def test_credential_resolution_occurs_only_after_reservation_and_never_on_refusal_paths() -> None:
    governance = GateGovernance()
    events = governance.events

    def resolve(target: Target) -> str:
        events.append("secret")
        return "synthetic-local-key"

    async def handler(request: httpx.Request) -> httpx.Response:
        events.append("provider")
        assert request.headers["authorization"] == "Bearer synthetic-local-key"
        return httpx.Response(200, json=openai_completion("text.nonstream"), request=request)

    policy = RoutePolicy(
        "gateway/general",
        "live-post-reservation",
        (pinned_target("openai"),),
        price_table_version=LIVE_PRICE_TABLE_VERSION,
    )
    application = create_data_plane(
        provider_transports={"openai": httpx.MockTransport(handler)},
        route_loader=StaticRouteLoader(policy),
        credential_resolver=resolve,
        key_store=TestKeyStore(),
        governance_store=governance,
    )
    client = TestClient(application)
    scenario = SCENARIOS["text.nonstream"]

    assert client.get("/healthz").status_code == 200
    assert client.post("/v1/chat/completions", headers=request_headers(scenario["id"], authenticated=False), json=scenario["request"]).status_code == 401
    altered = json.loads(json.dumps(scenario["request"]))
    altered["messages"][0]["content"] = "UNCOMMITTED_LIVE_CANARY"
    assert client.post("/v1/chat/completions", headers=request_headers(scenario["id"]), json=altered).status_code == 403
    assert "secret" not in events

    for state in ("inactive", "expired", "stopped"):
        governance.live_state = state
        assert client.post("/v1/chat/completions", headers=request_headers(scenario["id"]), json=scenario["request"]).status_code == 503
        assert "secret" not in events

    governance.live_state = "active"
    for code in ("quota_exceeded", "budget_exceeded"):
        governance.reserve_error = GovernanceError(code, 429)
        assert client.post("/v1/chat/completions", headers=request_headers(scenario["id"]), json=scenario["request"]).status_code == 429
        assert "secret" not in events

    governance.reserve_error = None
    response = client.post("/v1/chat/completions", headers=request_headers(scenario["id"]), json=scenario["request"])
    assert response.status_code == 200
    assert events[-5:] == ["eligible", "reserve", "secret", "provider", "reconcile"]


def test_missing_live_secret_fails_closed_and_preserves_simulator_recovery() -> None:
    governance = GateGovernance()
    calls: list[str] = []

    def unavailable(target: Target) -> str:
        raise CredentialResolutionError("local synthetic secret failure")

    async def simulator(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=openai_completion("text.nonstream"), request=request)

    live = pinned_target("openai", fallbacks=("simulator",))
    recovery = Target("simulator", "simulator", "simulator-v1", "http://simulator", weight=0)
    policy = RoutePolicy(
        "gateway/general",
        "live-secret-recovery",
        (live, recovery),
        price_table_version=LIVE_PRICE_TABLE_VERSION,
    )
    application = create_data_plane(
        provider_transports={"simulator": httpx.MockTransport(simulator)},
        route_loader=StaticRouteLoader(policy),
        credential_resolver=unavailable,
        key_store=TestKeyStore(),
        governance_store=governance,
    )
    scenario = SCENARIOS["text.nonstream"]
    response = TestClient(application).post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert response.status_code == 200
    assert calls == ["/simulate/chat/completions"]
    assert response.headers["x-gateway-provider"] == "simulator"
    assert application.state.receipts[-1]["attempted_targets"] == ["openai", "simulator"]
    assert "local synthetic secret failure" not in response.text
