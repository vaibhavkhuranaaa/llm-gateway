from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from services.catalog import ScenarioCatalog, scenario_checksum
from services.data_plane import (
    ChatRequest,
    FailedAuthLimiter,
    _stream_response,
    create_app as create_data_plane,
)
from services.governance import GovernanceError, GovernanceStore
from services.identity import KeyPrincipal, UserPrincipal
from services.provider_simulator import create_app as create_simulator
from services.routing import RoutePolicy, Target
from services.service_identity import (
    GoogleIdentityTokenProvider,
    GoogleServiceIdentityVerifier,
    ServiceIdentityError,
)
from tests.test_protocol import SCENARIOS, TestKeyStore, request_headers
from tests.test_workbench import workbench_app


SIMULATOR = Target("simulator", "simulator", "simulator-v1", "http://simulator", weight=100)


def test_provider_origins_are_exactly_allowlisted_without_a_synthetic_transport() -> None:
    metadata_target = Target(
        "openai",
        "openai",
        "synthetic-model",
        "http://169.254.169.254/computeMetadata/v1",
        weight=100,
    )
    with pytest.raises(ValueError, match="provider base URL"):
        create_data_plane(
            route_policy=RoutePolicy("gateway/general", "ssrf-test", (metadata_target,)),
            key_store=TestKeyStore(),
        )

    wrong_live_origin = Target(
        "openai",
        "openai",
        "synthetic-model",
        "https://openai.example.invalid",
        weight=100,
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        create_data_plane(
            route_policy=RoutePolicy("gateway/general", "ssrf-test", (wrong_live_origin,)),
            key_store=TestKeyStore(),
        )

    official_origin = Target(
        "openai",
        "openai",
        "synthetic-model",
        "https://api.openai.com",
        weight=100,
    )
    create_data_plane(
        route_policy=RoutePolicy("gateway/general", "ssrf-test", (official_origin,)),
        key_store=TestKeyStore(),
    )


def test_failed_authentication_is_bounded_before_provider_access() -> None:
    provider_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request.url.path)
        return httpx.Response(200, json={"unexpected": True}, request=request)

    application = create_data_plane(
        provider_transport=httpx.MockTransport(handler),
        key_store=TestKeyStore(),
        failed_auth_limiter=FailedAuthLimiter(limit=3, window_seconds=60),
    )
    client = TestClient(application)
    scenario = SCENARIOS["text.nonstream"]
    headers = {
        "Authorization": "Bearer invalid",
        "X-Gateway-Scenario-ID": scenario["id"],
        "X-Forwarded-For": "203.0.113.10",
    }
    for _ in range(3):
        assert client.post("/v1/chat/completions", headers=headers, json=scenario["request"]).status_code == 401
    limited = client.post("/v1/chat/completions", headers=headers, json=scenario["request"])
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "authentication_rate_limited"
    assert limited.headers["retry-after"] == "60"
    assert provider_calls == []


def test_simulator_requires_exact_google_service_identity_and_token_provider_caches() -> None:
    audience = "https://provider-simulator.example"
    caller = "gateway-api@example.iam.gserviceaccount.com"

    def verify_token(token: str, expected_audience: str):
        if token == "valid-token":
            return {
                "iss": "https://accounts.google.com",
                "aud": expected_audience,
                "email": caller,
            }
        if token == "wrong-caller":
            return {
                "iss": "https://accounts.google.com",
                "aud": expected_audience,
                "email": "other@example.iam.gserviceaccount.com",
            }
        raise ValueError("invalid token")

    verifier = GoogleServiceIdentityVerifier(audience, caller, token_verifier=verify_token)
    simulator = TestClient(create_simulator(identity_verifier=verifier.verify))
    scenario = {"scenario_id": "text.nonstream"}
    assert simulator.post("/simulate/chat/completions", json=scenario).status_code == 401
    assert simulator.post(
        "/simulate/chat/completions",
        headers={"Authorization": "Bearer wrong-caller"},
        json=scenario,
    ).status_code == 401
    assert simulator.post(
        "/simulate/chat/completions",
        headers={"Authorization": "Bearer valid-token"},
        json=scenario,
    ).status_code == 200

    now = 1_800_000_000.0
    payload = base64.urlsafe_b64encode(json.dumps({"exp": now + 3600}).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{payload}.c2lnbmF0dXJl"
    fetches: list[str] = []
    provider = GoogleIdentityTokenProvider(
        audience,
        token_fetcher=lambda value: fetches.append(value) or token,
        clock=lambda: now,
    )
    assert provider() == token
    assert provider() == token
    assert fetches == [audience]


def test_control_plane_requires_csrf_header_and_sets_browser_security_headers() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("data plane must not be called")

    client = workbench_app(handler)
    session = client.get(
        "/v1/session",
        headers={
            "X-Goog-IAP-JWT-Assertion": "owner-token",
            "X-Goog-Authenticated-User-Email": "accounts.google.com:owner@example.com",
        },
    )
    assert session.status_code == 200
    assert session.headers["content-security-policy"].startswith("default-src 'self'")
    assert session.headers["x-content-type-options"] == "nosniff"
    assert session.headers["referrer-policy"] == "no-referrer"

    refused = client.post(
        "/v1/admin/live-session",
        headers={
            "X-Goog-IAP-JWT-Assertion": "owner-token",
            "X-Goog-Authenticated-User-Email": "accounts.google.com:owner@example.com",
        },
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "csrf_required"


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.block = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"Transient"}}]}\n\n'
        await self.block.wait()


def test_stream_disconnect_keeps_the_reservation_charged() -> None:
    client = firestore.Client(
        project=f"security-disconnect-{uuid4().hex}",
        database="(default)",
        credentials=AnonymousCredentials(),
    )
    governance = GovernanceStore(client)
    owner = UserPrincipal("owner@example.com", "owner-subject", "owner", "tenant_test")
    governance.configure_policy(
        owner,
        request_limit=10,
        token_limit=100_000,
        budget_usd_micros=1_000_000,
        price_table_version="synthetic-v1",
        prices={},
    )
    scenario = SCENARIOS["text.stream"]
    chat_request = ChatRequest.model_validate(scenario["request"])
    reservation = governance.reserve(
        tenant_id="tenant_test",
        key_id="key-test",
        request_id="disconnect-request",
        trace_id="disconnect-trace",
        targets=(SIMULATOR,),
        input_characters=20,
        max_output_tokens=1024,
    )
    application = create_data_plane(
        provider_transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
        route_policy=RoutePolicy("gateway/general", "disconnect", (SIMULATOR,)),
        key_store=TestKeyStore(),
        governance_store=governance,
    )

    async def disconnect() -> None:
        upstream_request = httpx.Request("POST", "http://simulator/simulate/chat/completions")
        upstream = httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=BlockingStream(),
            request=upstream_request,
        )
        provider_client = httpx.AsyncClient()
        stream = _stream_response(
            application,
            application.state.route_policy,
            provider_client,
            upstream,
            SIMULATOR,
            ["simulator"],
            scenario["id"],
            chat_request,
            "disconnect-request",
            "disconnect-trace",
            KeyPrincipal("key-test", "tenant_test", ("chat:completions",)),
            reservation,
            time.perf_counter(),
        )
        assert "Transient" in await stream.__anext__()
        await stream.aclose()

    asyncio.run(disconnect())
    receipt = governance.receipt("tenant_test", "disconnect-request")
    assert receipt is not None
    assert receipt["outcome"] == "cancelled"
    assert receipt["accounting_status"] == "reserved_uncertain"
    bucket = governance.bucket("tenant_test", reservation.window_id)
    assert bucket is not None
    assert bucket["reserved_tokens"] == reservation.token_reservation


def test_partitioned_usage_buckets_preserve_the_global_quota_upper_bound() -> None:
    client = firestore.Client(
        project=f"security-shards-{uuid4().hex}",
        database="(default)",
        credentials=AnonymousCredentials(),
    )
    governance = GovernanceStore(client)
    owner = UserPrincipal("owner@example.com", "owner-subject", "owner", "tenant_sharded")
    version = governance.configure_policy(
        owner,
        request_limit=16,
        token_limit=160_000,
        budget_usd_micros=160_000,
        price_table_version="simulator-only-v1",
        prices={},
        usage_bucket_shards=16,
    )
    governance.configure_policy(
        owner,
        request_limit=16,
        token_limit=160_000,
        budget_usd_micros=160_000,
        price_table_version="simulator-only-v1",
        prices={},
        expected_version=version,
    )
    policy = (
        client.collection("tenants")
        .document(owner.tenant_id)
        .collection("policies")
        .document("current")
        .get()
        .to_dict()
    )
    assert policy is not None
    assert policy["usage_bucket_shards"] == 16

    request_ids: dict[int, str] = {}
    candidate = 0
    while len(request_ids) < 16:
        request_id = f"partition-{candidate}"
        shard = int(hashlib.sha256(request_id.encode()).hexdigest(), 16) % 16
        request_ids.setdefault(shard, request_id)
        candidate += 1

    def reserve(request_id: str):
        return governance.reserve(
            tenant_id=owner.tenant_id,
            key_id="key-test",
            request_id=request_id,
            trace_id=f"trace-{request_id}",
            targets=(SIMULATOR,),
            input_characters=1,
            max_output_tokens=1,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        reservations = list(executor.map(reserve, request_ids.values()))
    assert len(reservations) == 16
    bucket = governance.bucket(owner.tenant_id, reservations[0].window_id)
    assert bucket is not None
    assert bucket["request_charged"] == 16
    assert bucket["bucket_shards"] == 16

    target_shard = reservations[0].bucket_shard
    over_cap = 0
    while True:
        over_cap_id = f"over-cap-{over_cap}"
        if int(hashlib.sha256(over_cap_id.encode()).hexdigest(), 16) % 16 == target_shard:
            break
        over_cap += 1
    with pytest.raises(GovernanceError) as capped:
        reserve(over_cap_id)
    assert capped.value.code == "quota_exceeded"


def test_prompt_response_tool_schema_and_provider_error_canaries_never_reach_durable_state(tmp_path: Path) -> None:
    canaries = {
        "prompt": "SECURITY_PROMPT_CANARY_8C6A",
        "response": "SECURITY_RESPONSE_CANARY_3F2D",
        "tool": "SECURITY_TOOL_CANARY_4B19",
        "schema": "security_schema_canary_7d11",
        "provider_error": "SECURITY_PROVIDER_ERROR_CANARY_9E20",
    }
    scenario = {
        "id": "security.canary",
        "author": "project",
        "purpose": "retention canary",
        "fictional_data_attestation": True,
        "behavior": "success",
        "request": {
            "model": "gateway/general",
            "messages": [{"role": "user", "content": canaries["prompt"]}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "inspect_synthetic",
                        "description": canaries["tool"],
                        "parameters": {
                            "type": "object",
                            "properties": {canaries["schema"]: {"type": "string"}},
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "max_completion_tokens": 32,
        },
        "response": {
            "message": {"role": "assistant", "content": canaries["response"]},
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        },
    }
    scenario["checksum"] = scenario_checksum(scenario)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"version": 1, "scenarios": [scenario]}), encoding="utf-8")
    catalog = ScenarioCatalog(path)

    client = firestore.Client(
        project=f"security-canary-{uuid4().hex}",
        database="(default)",
        credentials=AnonymousCredentials(),
    )
    governance = GovernanceStore(client)
    owner = UserPrincipal("owner@example.com", "owner-subject", "owner", "tenant_test")
    governance.configure_policy(
        owner,
        request_limit=10,
        token_limit=10_000,
        budget_usd_micros=1_000_000,
        price_table_version="synthetic-v1",
        prices={},
    )
    provider_error_mode = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if provider_error_mode:
            return httpx.Response(500, text=canaries["provider_error"], request=request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": scenario["response"]["message"], "finish_reason": "stop"}],
                "usage": scenario["response"]["usage"],
            },
            request=request,
        )

    application = create_data_plane(
        provider_transport=httpx.MockTransport(handler),
        route_policy=RoutePolicy("gateway/general", "canary", (SIMULATOR,)),
        key_store=TestKeyStore(),
        governance_store=governance,
        catalog=catalog,
    )
    api = TestClient(application)
    response = api.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == canaries["response"]
    provider_error_mode = True
    error_response = api.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert error_response.status_code == 503
    assert canaries["provider_error"] not in error_response.text

    retained: list[dict[str, object]] = list(application.state.receipts)
    retained.extend(governance.telemetry.logs)
    retained.extend(governance.telemetry.traces)
    retained.extend(governance.telemetry.costs)
    tenant = client.collection("tenants").document("tenant_test")
    for collection_name in ("policies", "usage_buckets", "reservations", "live_sessions", "receipts"):
        retained.extend(
            snapshot.to_dict() or {}
            for snapshot in tenant.collection(collection_name).stream()
        )
    serialized = json.dumps(retained, default=str)
    for canary in canaries.values():
        assert canary not in serialized
