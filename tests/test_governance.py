from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import httpx
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from services.governance import (
    LIVE_REQUEST_LIMIT,
    LIVE_SPEND_LIMIT_MICROS,
    GovernanceError,
    GovernanceStore,
    ReceiptMetadata,
)
from services.data_plane import create_app as create_data_plane
from services.control_plane import create_app as create_control_plane
from services.identity import AuthorizationError, FirestoreIdentityStore, IAPIdentityVerifier, UserPrincipal
from services.routing import RoutePolicy, Target
from tests.test_protocol import SCENARIOS, TestKeyStore, request_headers
from tests.test_routing_adapters import completion
from tests.test_identity import AUDIENCE, iap_headers, token_verifier


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 9, 20, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def governance(clock: MutableClock) -> GovernanceStore:
    client = firestore.Client(
        project=f"governance-{uuid4().hex}",
        database="(default)",
        credentials=AnonymousCredentials(),
    )
    return GovernanceStore(client, clock=clock)


@pytest.fixture
def owner() -> UserPrincipal:
    return UserPrincipal("owner@example.com", "owner-subject", "owner", "tenant_alpha")


SIMULATOR = Target("simulator", "simulator", "simulator-v1", "http://simulator", weight=100)
LIVE = Target("openai", "openai", "synthetic-live-model", "https://openai.test", weight=100)


def configure(
    governance: GovernanceStore,
    owner: UserPrincipal,
    *,
    request_limit: int = 100,
    token_limit: int = 100_000,
    budget: int = 10_000_000,
    input_rate: int = 1_000_000,
    output_rate: int = 2_000_000,
) -> None:
    current = governance.status(owner.tenant_id)["policy"]
    governance.configure_policy(
        owner,
        request_limit=request_limit,
        token_limit=token_limit,
        budget_usd_micros=budget,
        price_table_version="synthetic-v1",
        prices={
            "openai:synthetic-live-model": {"input": input_rate, "output": output_rate},
            "anthropic:synthetic-live-model": {"input": input_rate, "output": output_rate},
        },
        expected_version=current["version"] if current else None,
    )


def receipt(request_id: str, clock: MutableClock, *, outcome: str = "complete") -> ReceiptMetadata:
    return ReceiptMetadata(
        request_id=request_id,
        trace_id=f"trace-{request_id}",
        tenant_id="tenant_alpha",
        key_id="key-test",
        scenario_id="text.nonstream",
        route="gateway/general",
        provider="simulator",
        provider_model="simulator-v1",
        attempted_targets=("simulator",),
        outcome=outcome,
        http_status=200,
        input_characters=39,
        output_characters=51,
        tool_count=0,
        response_format="text",
        prompt_tokens=12,
        completion_tokens=13,
        cached_input_tokens=0,
        latency_ms=25,
        recorded_at=clock(),
    )


def test_reserve_reconcile_quota_budget_and_metadata_only_views(
    governance: GovernanceStore,
    owner: UserPrincipal,
    clock: MutableClock,
) -> None:
    configure(governance, owner, request_limit=2, token_limit=3000, budget=1_000_000)
    reserved = governance.reserve(
        tenant_id=owner.tenant_id,
        key_id="key-test",
        request_id="request-1",
        trace_id="trace-request-1",
        targets=(SIMULATOR,),
        input_characters=39,
        max_output_tokens=1024,
    )
    before = governance.bucket(owner.tenant_id, reserved.window_id)
    assert before is not None
    assert before["request_charged"] == 1
    assert before["token_charged"] == 1034
    assert before["reserved_tokens"] == 1034
    assert before["delete_at"] == clock() + timedelta(days=35)

    document = governance.reconcile(
        reserved,
        receipt("request-1", clock),
        selected_target=SIMULATOR,
        live_attempts=0,
        terminal_certain=True,
    )
    assert document["accounting_status"] == "reconciled"
    assert document["cost_usd_micros"] == 0
    after = governance.bucket(owner.tenant_id, reserved.window_id)
    assert after is not None
    assert after["token_charged"] == 25
    assert after["reserved_tokens"] == 0
    assert after["reconciled_tokens"] == 25

    # Reconciliation is idempotent and emits each correlated telemetry view once.
    governance.reconcile(
        reserved,
        receipt("request-1", clock),
        selected_target=SIMULATOR,
        live_attempts=0,
        terminal_certain=True,
    )
    assert len(governance.telemetry.logs) == 1
    assert len(governance.telemetry.traces) == 1
    assert len(governance.telemetry.costs) == 1
    assert sum(governance.telemetry.metrics.values()) == 1
    correlation = {"request_id": "request-1", "trace_id": "trace-request-1", "tenant_id": "tenant_alpha"}
    for view in (*governance.telemetry.logs, *governance.telemetry.traces, *governance.telemetry.costs):
        assert all(view[key] == value for key, value in correlation.items())

    uncertain = governance.reserve(
        tenant_id=owner.tenant_id,
        key_id="key-test",
        request_id="request-2",
        trace_id="trace-request-2",
        targets=(SIMULATOR,),
        input_characters=1,
        max_output_tokens=1,
    )
    uncertain_document = governance.reconcile(
        uncertain,
        receipt("request-2", clock, outcome="partial"),
        selected_target=SIMULATOR,
        live_attempts=0,
        terminal_certain=False,
    )
    assert uncertain_document["accounting_status"] == "reserved_uncertain"
    uncertain_bucket = governance.bucket(owner.tenant_id, uncertain.window_id)
    assert uncertain_bucket is not None
    assert uncertain_bucket["reserved_tokens"] == uncertain.token_reservation
    with pytest.raises(GovernanceError) as quota:
        governance.reserve(
            tenant_id=owner.tenant_id,
            key_id="key-test",
            request_id="request-3",
            trace_id="trace-request-3",
            targets=(SIMULATOR,),
            input_characters=1,
            max_output_tokens=1,
        )
    assert quota.value.code == "quota_exceeded"

    retained = json.dumps(
        {
            "receipt": governance.receipt(owner.tenant_id, "request-1"),
            "logs": list(governance.telemetry.logs),
            "traces": list(governance.telemetry.traces),
            "costs": list(governance.telemetry.costs),
        },
        default=str,
    )
    assert "UNRETAINED_PROMPT_CANARY" not in retained
    assert "UNRETAINED_RESPONSE_CANARY" not in retained


def test_live_session_owner_boundary_time_request_and_spend_caps(
    governance: GovernanceStore,
    owner: UserPrincipal,
    clock: MutableClock,
) -> None:
    with pytest.raises(GovernanceError) as missing_prices:
        governance.arm_live_session(owner)
    assert missing_prices.value.code == "price_unavailable"
    configure(governance, owner, input_rate=0, output_rate=0)
    demo = UserPrincipal("demo@example.com", "demo-subject", "demo_operator", owner.tenant_id)
    with pytest.raises(AuthorizationError):
        governance.arm_live_session(demo)

    session = governance.arm_live_session(owner)
    assert session.request_limit == 20
    assert session.spend_limit_micros == 1_000_000
    with pytest.raises(GovernanceError) as active:
        governance.arm_live_session(owner)
    assert active.value.code == "live_session_active"

    for index in range(LIVE_REQUEST_LIMIT):
        governance.reserve(
            tenant_id=owner.tenant_id,
            key_id="key-test",
            request_id=f"live-request-{index}",
            trace_id=f"live-trace-{index}",
            targets=(LIVE,),
            input_characters=0,
            max_output_tokens=1,
        )
    status = governance.status(owner.tenant_id)["live_session"]
    assert status["requests_charged"] == LIVE_REQUEST_LIMIT
    assert status["state"] == "cap_reached"
    with pytest.raises(GovernanceError) as capped:
        governance.reserve(
            tenant_id=owner.tenant_id,
            key_id="key-test",
            request_id="live-request-21",
            trace_id="live-trace-21",
            targets=(LIVE,),
            input_characters=0,
            max_output_tokens=1,
        )
    assert capped.value.code == "live_provider_disabled"

    configure(
        governance,
        owner,
        input_rate=0,
        output_rate=LIVE_SPEND_LIMIT_MICROS * 1_000_000,
        budget=2_000_000,
    )
    spend_session = governance.arm_live_session(owner)
    governance.reserve(
        tenant_id=owner.tenant_id,
        key_id="key-test",
        request_id="spend-cap",
        trace_id="spend-cap-trace",
        targets=(LIVE,),
        input_characters=0,
        max_output_tokens=1,
    )
    spend_status = governance.status(owner.tenant_id)["live_session"]
    assert spend_status["session_id"] == spend_session.session_id
    assert spend_status["spend_charged_micros"] == LIVE_SPEND_LIMIT_MICROS
    assert spend_status["state"] == "cap_reached"

    configure(governance, owner, input_rate=0, output_rate=0)
    expiring = governance.arm_live_session(owner)
    clock.advance(minutes=30)
    with pytest.raises(GovernanceError) as expired:
        governance.reserve(
            tenant_id=owner.tenant_id,
            key_id="key-test",
            request_id="expired-request",
            trace_id="expired-trace",
            targets=(LIVE,),
            input_characters=0,
            max_output_tokens=1,
        )
    assert expired.value.code == "live_provider_disabled"
    expired_status = governance.status(owner.tenant_id)["live_session"]
    assert expired_status["session_id"] == expiring.session_id
    assert expired_status["state"] == "expired"


def test_concurrent_admission_never_exceeds_limit(
    governance: GovernanceStore,
    owner: UserPrincipal,
) -> None:
    configure(governance, owner, request_limit=10, token_limit=100_000)
    governance.reserve(
        tenant_id=owner.tenant_id,
        key_id="key-test",
        request_id="concurrent-seed",
        trace_id="trace-seed",
        targets=(SIMULATOR,),
        input_characters=1,
        max_output_tokens=1,
    )

    def admit(index: int) -> str:
        try:
            governance.reserve(
                tenant_id=owner.tenant_id,
                key_id="key-test",
                request_id=f"concurrent-{index}",
                trace_id=f"trace-{index}",
                targets=(SIMULATOR,),
                input_characters=1,
                max_output_tokens=1,
            )
            return "admitted"
        except GovernanceError as error:
            return error.code
        except Exception:
            return "transaction_exhausted"

    with ThreadPoolExecutor(max_workers=24) as executor:
        outcomes = list(executor.map(admit, range(24)))
    admitted = 1 + outcomes.count("admitted")
    window_id = datetime(2026, 8, 9, 20, tzinfo=UTC).strftime("%Y%m%d%H")
    bucket = governance.bucket(owner.tenant_id, window_id)
    assert bucket is not None
    assert admitted <= 10
    assert bucket["request_charged"] == admitted
    assert admitted >= 1


def test_data_plane_reserves_before_provider_reconciles_and_refuses_without_call(
    governance: GovernanceStore,
) -> None:
    tenant_owner = UserPrincipal("owner@example.com", "owner-subject", "owner", "tenant_test")
    configure(governance, tenant_owner, request_limit=1, token_limit=10_000)
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        scenario_id = json.loads(request.content)["scenario_id"]
        return httpx.Response(200, json=completion(scenario_id), request=request)

    transport = httpx.MockTransport(handler)
    route = RoutePolicy("gateway/general", "governance-test", (SIMULATOR,))
    application = create_data_plane(
        provider_transport=transport,
        route_policy=route,
        key_store=TestKeyStore(),
        governance_store=governance,
    )
    client = TestClient(application)
    scenario = SCENARIOS["text.nonstream"]
    first = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert first.status_code == 200
    assert calls == ["simulator"]
    assert first.headers["x-gateway-usage-status"] == "reconciled"
    request_id = first.headers["x-request-id"]
    durable = governance.receipt("tenant_test", request_id)
    assert durable is not None
    assert durable["request_id"] == request_id
    assert durable["trace_id"] == first.headers["x-trace-id"]
    assert durable["accounting_status"] == "reconciled"
    retained = json.dumps(durable, default=str)
    assert scenario["request"]["messages"][0]["content"] not in retained
    assert scenario["response"]["message"]["content"] not in retained

    refused = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "quota_exceeded"
    assert calls == ["simulator"]

    configure(governance, tenant_owner, request_limit=100, token_limit=100_000)
    live_route = RoutePolicy("gateway/general", "governance-live", (LIVE,))
    live_application = create_data_plane(
        provider_transports={"openai": transport},
        provider_api_keys={"openai": "synthetic-test-key"},
        route_policy=live_route,
        key_store=TestKeyStore(),
        governance_store=governance,
    )
    live_refused = TestClient(live_application).post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert live_refused.status_code == 503
    assert live_refused.json()["error"]["code"] == "live_provider_disabled"
    assert calls == ["simulator"]

    simulator_with_live_fallback = Target(
        "simulator",
        "simulator",
        "simulator-v1",
        "http://simulator",
        weight=100,
        fallbacks=("openai",),
    )
    mixed_application = create_data_plane(
        provider_transports={"simulator": transport, "openai": transport},
        provider_api_keys={"openai": "synthetic-test-key"},
        route_policy=RoutePolicy("gateway/general", "governance-mixed", (simulator_with_live_fallback, LIVE)),
        key_store=TestKeyStore(),
        governance_store=governance,
    )
    simulator_safe = TestClient(mixed_application).post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert simulator_safe.status_code == 200
    assert calls == ["simulator", "simulator"]

    configure(governance, tenant_owner, request_limit=100, token_limit=1)
    token_refused = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert token_refused.status_code == 429
    assert token_refused.json()["error"]["code"] == "quota_exceeded"
    assert calls == ["simulator", "simulator"]

    configure(
        governance,
        tenant_owner,
        request_limit=100,
        token_limit=100_000,
        budget=1,
        input_rate=1_000_000,
        output_rate=2_000_000,
    )
    governance.arm_live_session(tenant_owner)
    budget_refused = TestClient(live_application).post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert budget_refused.status_code == 429
    assert budget_refused.json()["error"]["code"] == "budget_exceeded"
    assert calls == ["simulator", "simulator"]


def test_control_plane_enforces_owner_policy_and_live_session_operations(
    governance: GovernanceStore,
    clock: MutableClock,
) -> None:
    identities = FirestoreIdentityStore(governance.client, pepper=b"x" * 32, clock=clock)
    owner = identities.bootstrap_owner("owner@example.com", "tenant_alpha", subject="owner-subject")
    identities.invite_user(owner, "demo@example.com", "demo_operator", subject="demo-subject")
    verifier = IAPIdentityVerifier(AUDIENCE, token_verifier=token_verifier)
    application = create_control_plane(store=identities, verifier=verifier, governance_store=governance)
    client = TestClient(application)
    policy_payload = {
        "request_limit": 100,
        "token_limit": 100_000,
        "budget_usd_micros": 2_000_000,
        "price_table_version": "synthetic-v1",
        "prices": {
            "openai:synthetic-live-model": {"input": 1, "output": 2},
            "anthropic:synthetic-live-model": {"input": 1, "output": 2},
        },
    }

    demo_policy = client.put(
        "/v1/admin/policy",
        headers=iap_headers("demo-token", "demo@example.com"),
        json=policy_payload,
    )
    assert demo_policy.status_code == 403
    owner_policy = client.put(
        "/v1/admin/policy",
        headers=iap_headers("owner-token", "owner@example.com"),
        json=policy_payload,
    )
    assert owner_policy.status_code == 200
    conflict = client.put(
        "/v1/admin/policy",
        headers=iap_headers("owner-token", "owner@example.com"),
        json=policy_payload,
    )
    assert conflict.status_code == 409
    updated = client.put(
        "/v1/admin/policy",
        headers=iap_headers("owner-token", "owner@example.com"),
        json={**policy_payload, "expected_version": owner_policy.json()["version"]},
    )
    assert updated.status_code == 200

    demo_arm = client.post(
        "/v1/admin/live-session",
        headers=iap_headers("demo-token", "demo@example.com"),
    )
    assert demo_arm.status_code == 403
    armed = client.post(
        "/v1/admin/live-session",
        headers=iap_headers("owner-token", "owner@example.com"),
    )
    assert armed.status_code == 200
    assert armed.json()["request_limit"] == 20
    assert armed.json()["spend_limit_micros"] == 1_000_000
    assert armed.json()["expires_at"] == (clock() + timedelta(minutes=30)).isoformat()

    status = client.get(
        "/v1/operations/status",
        headers=iap_headers("demo-token", "demo@example.com"),
    )
    assert status.status_code == 200
    status_text = status.text
    assert '"state":"active"' in status_text
    assert "owner@example.com" not in status_text
    assert client.delete(
        "/v1/admin/live-session",
        headers=iap_headers("owner-token", "owner@example.com"),
    ).status_code == 204
