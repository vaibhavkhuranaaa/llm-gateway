from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from services.catalog import ScenarioCatalog
from services.control_plane import create_app
from services.governance import GovernanceStore
from services.identity import FirestoreIdentityStore, IAPIdentityVerifier
from tests.test_identity import AUDIENCE, iap_headers, token_verifier


class AsyncContent(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):
        yield self.content


def workbench_app(handler):
    client = firestore.Client(
        project=f"workbench-{uuid4().hex}",
        database="(default)",
        credentials=AnonymousCredentials(),
    )
    store = FirestoreIdentityStore(client, pepper=b"workbench-pepper" * 4, clock=lambda: datetime(2026, 8, 9, tzinfo=UTC))
    owner = store.bootstrap_owner("owner@example.com", "tenant_alpha", subject="owner-subject")
    store.invite_user(owner, "demo@example.com", "demo_operator", subject="demo-subject")
    application = create_app(
        store=store,
        verifier=IAPIdentityVerifier(AUDIENCE, token_verifier=token_verifier),
        governance_store=GovernanceStore(client),
        data_plane_transport=httpx.MockTransport(handler),
        gateway_key="gw_server_only_secret",
    )
    return TestClient(application)


def test_catalog_projection_and_proxy_keep_prompt_and_gateway_key_server_side() -> None:
    catalog = ScenarioCatalog()
    scenario = catalog.get("text.nonstream")
    assert scenario is not None
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "authorization": request.headers.get("Authorization"),
                "scenario_id": request.headers.get("X-Gateway-Scenario-ID"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(
            200,
            headers={
                "X-Request-ID": "chatcmpl-workbench",
                "X-Trace-ID": "trace-workbench",
                "X-Gateway-Provider": "simulator",
                "X-Gateway-Model": "simulator-v1",
                "X-Gateway-Attempts": "1",
                "X-Gateway-Usage-Status": "reconciled",
            },
            json={
                "choices": [{"message": {"role": "assistant", "content": "Transient response"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
            request=request,
        )

    client = workbench_app(handler)
    headers = iap_headers("demo-token", "demo@example.com")
    projection = client.get("/v1/workbench/scenarios", headers=headers)
    assert projection.status_code == 200
    serialized = projection.text
    assert scenario["request"]["messages"][0]["content"] not in serialized
    assert scenario["response"]["message"]["content"] not in serialized
    assert "gw_server_only_secret" not in serialized

    response = client.post(
        "/v1/workbench/runs",
        headers=headers,
        json={"scenario_id": "text.nonstream"},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Transient response"
    assert response.headers["x-request-id"] == "chatcmpl-workbench"
    assert response.headers["cache-control"] == "no-store"
    assert "gw_server_only_secret" not in response.text
    assert "gw_server_only_secret" not in json.dumps(dict(response.headers))
    assert captured == [
        {
            "authorization": "Bearer gw_server_only_secret",
            "scenario_id": "text.nonstream",
            "body": scenario["request"],
        }
    ]


def test_workbench_rejects_unknown_scenario_before_data_plane_and_proxies_sse() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["X-Gateway-Scenario-ID"])
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Request-ID": "chatcmpl-stream",
                "X-Trace-ID": "trace-stream",
                "X-Gateway-Provider": "simulator",
                "X-Gateway-Model": "simulator-v1",
                "X-Gateway-Attempts": "1",
                "X-Gateway-Usage-Status": "reconciled",
            },
            stream=AsyncContent((
                'data: {"choices":[{"delta":{"content":"Transient"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":" stream"}}]}\n\n'
                "data: [DONE]\n\n"
            ).encode()),
            request=request,
        )

    client = workbench_app(handler)
    headers = iap_headers("demo-token", "demo@example.com")
    missing = client.post("/v1/workbench/runs", headers=headers, json={"scenario_id": "unknown"})
    assert missing.status_code == 404
    assert calls == []

    stream = client.post("/v1/workbench/runs", headers=headers, json={"scenario_id": "text.stream"})
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-store"
    assert stream.text.endswith("data: [DONE]\n\n")
    assert calls == ["text.stream"]


def test_workbench_requires_invited_identity_and_never_exposes_owner_controls_to_demo() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider must not be called")

    client = workbench_app(handler)
    assert client.get("/v1/workbench/scenarios").status_code == 401
    demo_headers = iap_headers("demo-token", "demo@example.com")
    assert client.post("/v1/admin/live-session", headers=demo_headers).status_code == 403
    assert client.put(
        "/v1/admin/policy",
        headers=demo_headers,
        json={
            "request_limit": 1,
            "token_limit": 1,
            "budget_usd_micros": 1,
            "price_table_version": "synthetic",
            "prices": {},
        },
    ).status_code == 403
