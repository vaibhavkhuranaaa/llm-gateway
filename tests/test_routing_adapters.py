from __future__ import annotations

import asyncio
import json
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from services.data_plane import create_app as create_data_plane
from services.providers import ProviderFailure, build_request, normalize_response, normalized_stream
from services.routing import RoutePolicy, Target
from tests.test_protocol import SCENARIOS, TestKeyStore, request_headers


def policy(*targets: Target) -> RoutePolicy:
    return RoutePolicy("gateway/general", "routing-test", targets)


def target(
    target_id: str,
    *,
    provider: str = "simulator",
    weight: int = 0,
    fallbacks: tuple[str, ...] = (),
    circuit_open: bool = False,
) -> Target:
    return Target(
        id=target_id,
        provider=provider,
        model=f"{provider}-model",
        base_url=f"https://{target_id}.test",
        weight=weight,
        fallbacks=fallbacks,
        circuit_open=circuit_open,
    )


def completion(scenario_id: str, *, content: str | None = None) -> dict[str, object]:
    scenario = SCENARIOS[scenario_id]
    response = scenario.get("response") or {
        "message": {"role": "assistant", "content": "Synthetic fallback completed."},
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
    }
    message = dict(response.get("message") or {"role": "assistant", "content": "Synthetic fallback completed."})
    if content is not None:
        message["content"] = content
    return {
        "id": "provider-result",
        "object": "chat.completion",
        "created": 1,
        "model": "provider-model",
        "choices": [{"index": 0, "message": message, "finish_reason": response["finish_reason"]}],
        "usage": response["usage"],
    }


def test_stable_weighted_selection_and_ordered_fallback() -> None:
    route = policy(
        target("a", weight=3, fallbacks=("backup",)),
        target("b", weight=1, fallbacks=("backup",)),
        target("backup"),
    )
    selections = [route.ordered_targets(f"request-{index}")[0].id for index in range(4000)]
    counts = Counter(selections)
    assert 2850 <= counts["a"] <= 3150
    assert 850 <= counts["b"] <= 1150
    assert route.ordered_targets("same-request") == route.ordered_targets("same-request")
    assert [item.id for item in route.ordered_targets("same-request")][1:] == ["backup"]

    circuit_route = policy(
        target("open", weight=100, circuit_open=True),
        target("healthy", weight=1),
    )
    assert [item.id for item in circuit_route.ordered_targets("request")] == ["healthy"]


def test_openai_and_anthropic_wire_contracts_and_normalization() -> None:
    openai = target("openai", provider="openai", weight=1)
    path, headers, body = build_request(openai, SCENARIOS["tools.weather"]["request"], "tools.weather", "oa-key")
    assert path == "/v1/chat/completions"
    assert headers == {"Authorization": "Bearer oa-key"}
    assert body["model"] == "openai-model"
    assert body["store"] is False
    assert body["tools"][0]["function"]["parameters"]["additionalProperties"] is False

    anthropic = target("anthropic", provider="anthropic", weight=1)
    path, headers, body = build_request(
        anthropic,
        SCENARIOS["structured.release"]["request"],
        "structured.release",
        "an-key",
    )
    assert path == "/v1/messages"
    assert headers["X-Api-Key"] == "an-key"
    assert headers["Anthropic-Version"] == "2023-06-01"
    assert body["model"] == "anthropic-model"
    assert body["output_config"]["format"] == {
        "type": "json_schema",
        "schema": SCENARIOS["structured.release"]["request"]["response_format"]["json_schema"]["schema"],
    }

    tool_request = SCENARIOS["tools.weather"]["request"]
    _, _, tool_body = build_request(anthropic, tool_request, "tools.weather", "an-key")
    assert tool_body["tools"][0]["name"] == "get_weather"
    assert tool_body["tools"][0]["input_schema"] == tool_request["tools"][0]["function"]["parameters"]
    _, _, required_body = build_request(
        anthropic,
        {**tool_request, "tool_choice": "required"},
        "tools.weather",
        "an-key",
    )
    assert required_body["tool_choice"] == {"type": "any"}

    result = normalize_response(
        "anthropic",
        {
            "id": "msg_synthetic",
            "content": [
                {"type": "text", "text": "Synthetic answer."},
                {"type": "tool_use", "id": "tool_1", "name": "get_weather", "input": {"city": "Sample City"}},
            ],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        },
        "gateway/general",
    )
    assert result.body["choices"][0]["finish_reason"] == "tool_calls"
    assert result.body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"city":"Sample City"}'
    assert result.body["usage"]["total_tokens"] == 18
    assert result.cost_inputs == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cached_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    }

    with pytest.raises(ProviderFailure) as unsupported:
        build_request(anthropic, {**tool_request, "seed": 7}, "tools.weather", "an-key")
    assert unsupported.value.code == "unsupported_parameter"


def test_anthropic_stream_normalizes_text_tools_usage_and_done() -> None:
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Synthetic "}},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tool_1", "name": "get_weather", "input": {}},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"city"'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 6}},
        {"type": "message_stop"},
    ]
    response = httpx.Response(200, content="".join(f"data: {json.dumps(event)}\n\n" for event in events))

    async def collect() -> list[dict[str, object] | None]:
        return [event async for event in normalized_stream("anthropic", response, "gateway/general")]

    normalized = asyncio.run(collect())
    assert normalized[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert normalized[1]["choices"][0]["delta"] == {"content": "Synthetic "}
    assert normalized[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert normalized[3]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"city"'
    assert normalized[4]["choices"][0]["finish_reason"] == "tool_calls"
    assert normalized[5]["choices"] == []
    assert normalized[5]["usage"]["total_tokens"] == 11
    assert normalized[-1] is None


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_data_plane_uses_normalized_live_adapter_contract_without_live_calls(provider: str) -> None:
    seen: list[httpx.Request] = []
    scenario = SCENARIOS["structured.release"]

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        wire = json.loads(request.content)
        if provider == "openai":
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer synthetic-test-key"
            assert wire["store"] is False
            return httpx.Response(200, json=completion(scenario["id"]), request=request)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "synthetic-test-key"
        assert wire["output_config"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "msg_synthetic",
                "content": [{"type": "text", "text": scenario["response"]["message"]["content"]}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 14, "output_tokens": 11},
            },
            request=request,
        )

    adapter_target = target("adapter", provider=provider, weight=100)
    application = create_data_plane(
        provider_transports={"adapter": httpx.MockTransport(handler)},
        provider_api_keys={provider: "synthetic-test-key"},
        route_policy=policy(adapter_target),
        key_store=TestKeyStore(),
    )
    response = TestClient(application).post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert response.status_code == 200
    assert json.loads(response.json()["choices"][0]["message"]["content"]) == {
        "status": "on_track",
        "risk": "low",
    }
    assert len(seen) == 1
    receipt = application.state.receipts[-1]
    assert receipt["provider"] == provider
    assert receipt["cost_inputs"]["input_tokens"] == 14
    assert receipt["cost_inputs"]["output_tokens"] == 11


@pytest.mark.parametrize(
    "failure_mode",
    ["connect", "timeout", "rate_limit", "server_error", "malformed", "contract", "structured"],
)
def test_every_pre_response_fallback_class_uses_ordered_backup(failure_mode: str) -> None:
    scenario_id = "structured.release" if failure_mode == "structured" else "text.nonstream"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            if failure_mode == "connect":
                raise httpx.ConnectError("synthetic connection failure", request=request)
            if failure_mode == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if failure_mode == "rate_limit":
                return httpx.Response(429, json={"error": {"code": "synthetic_rate"}}, request=request)
            if failure_mode == "server_error":
                return httpx.Response(503, json={"error": {"code": "synthetic_server"}}, request=request)
            if failure_mode == "malformed":
                return httpx.Response(200, content=b'{"broken"', request=request)
            if failure_mode == "contract":
                return httpx.Response(200, json={"choices": []}, request=request)
            return httpx.Response(200, json=completion(scenario_id, content='{"status":1}'), request=request)
        return httpx.Response(200, json=completion(scenario_id), request=request)

    transport = httpx.MockTransport(handler)
    route = policy(
        target("primary", weight=100, fallbacks=("backup",)),
        target("backup"),
    )
    application = create_data_plane(
        provider_transports={"primary": transport, "backup": transport},
        route_policy=route,
        key_store=TestKeyStore(),
        provider_timeout=0.1,
    )
    response = TestClient(application).post(
        "/v1/chat/completions",
        headers=request_headers(scenario_id),
        json=SCENARIOS[scenario_id]["request"],
    )
    assert response.status_code == 200
    assert calls == ["primary.test", "backup.test"]
    assert response.headers["x-gateway-attempts"] == "2"
    assert application.state.receipts[-1]["attempted_targets"] == ["primary", "backup"]


def test_nonfallback_provider_rejection_and_local_refusal_make_no_extra_attempt() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(400, json={"error": {"code": "bad_request"}}, request=request)

    transport = httpx.MockTransport(handler)
    application = create_data_plane(
        provider_transports={"primary": transport, "backup": transport},
        route_policy=policy(target("primary", weight=100, fallbacks=("backup",)), target("backup")),
        key_store=TestKeyStore(),
    )
    client = TestClient(application)
    scenario = SCENARIOS["text.nonstream"]
    rejected = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=scenario["request"],
    )
    assert rejected.status_code == 502
    assert rejected.json()["error"]["code"] == "provider_rejected_request"
    assert calls == ["primary.test"]

    calls.clear()
    altered = json.loads(json.dumps(scenario["request"]))
    altered["messages"][0]["content"] = "UNRETAINED_ROUTING_CANARY"
    local_refusal = client.post(
        "/v1/chat/completions",
        headers=request_headers(scenario["id"]),
        json=altered,
    )
    assert local_refusal.status_code == 403
    assert calls == []


def test_stream_falls_back_before_response_but_never_splices_after_output() -> None:
    calls: list[str] = []
    mode = "pre_response"

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.test" and mode == "pre_response":
            return httpx.Response(429, request=request)
        if request.url.host == "primary.test":
            body = (
                'data: {"id":"p","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Partial synthetic "},"finish_reason":null}]}\n\n'
                'data: {"error":{"code":"synthetic_stream_failure"}}\n\n'
            )
            return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"}, request=request)
        scenario = SCENARIOS["text.stream"]
        body = "".join(
            [
                'data: {"id":"b","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
                'data: {"id":"b","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Backup synthetic."},"finish_reason":"stop"}]}\n\n',
                f'data: {{"id":"b","object":"chat.completion.chunk","choices":[],"usage":{json.dumps(scenario["response"]["usage"])}}}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"}, request=request)

    transport = httpx.MockTransport(handler)
    application = create_data_plane(
        provider_transports={"primary": transport, "backup": transport},
        route_policy=policy(target("primary", weight=100, fallbacks=("backup",)), target("backup")),
        key_store=TestKeyStore(),
    )
    client = TestClient(application)
    scenario = SCENARIOS["text.stream"]

    with client.stream("POST", "/v1/chat/completions", headers=request_headers(scenario["id"]), json=scenario["request"]) as response:
        lines = [line for line in response.iter_lines() if line]
    assert calls == ["primary.test", "backup.test"]
    assert lines[-1] == "data: [DONE]"
    assert not any('"error"' in line for line in lines)

    calls.clear()
    mode = "after_output"
    with client.stream("POST", "/v1/chat/completions", headers=request_headers(scenario["id"]), json=scenario["request"]) as response:
        lines = [line for line in response.iter_lines() if line]
    assert calls == ["primary.test"]
    assert sum('"error"' in line for line in lines) == 1
    assert lines[-1] == "data: [DONE]"
    assert application.state.receipts[-1]["outcome"] == "partial"
