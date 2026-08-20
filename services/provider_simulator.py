"""Deterministic HTTP provider simulator."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from services.catalog import ScenarioCatalog
from services.service_identity import GoogleServiceIdentityVerifier, ServiceIdentityError


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str


def create_app(
    catalog: ScenarioCatalog | None = None,
    *,
    identity_verifier: Callable[[Mapping[str, str]], None] | None = None,
) -> FastAPI:
    application = FastAPI(title="Private gateway provider simulator", version="0.1.0")
    application.state.catalog = catalog or ScenarioCatalog()

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "provider-simulator"}

    @application.post("/simulate/chat/completions")
    async def simulate(payload: SimulationRequest, request: Request) -> Response:
        if identity_verifier is None:
            return JSONResponse(status_code=503, content={"error": {"code": "simulator_not_configured"}})
        try:
            identity_verifier(request.headers)
        except ServiceIdentityError:
            return JSONResponse(status_code=401, content={"error": {"code": "invalid_service_identity"}})
        scenario = application.state.catalog.get(payload.scenario_id)
        if scenario is None:
            return JSONResponse(status_code=404, content={"error": {"code": "scenario_not_found"}})

        behavior = scenario["behavior"]
        if behavior == "rate_limit":
            return JSONResponse(
                status_code=429,
                content={"error": {"code": "simulated_rate_limit", "message": "Synthetic provider rate limit."}},
            )
        if behavior == "malformed":
            return Response(content=b'{"not":"complete"', media_type="application/json")
        if behavior == "timeout":
            await asyncio.sleep(12)
        elif behavior == "latency":
            await asyncio.sleep(0.02)

        chat_request = scenario["request"]
        if chat_request.get("stream"):
            return StreamingResponse(
                _stream_scenario(scenario),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse(content=_completion(scenario))

    return application


def _completion(scenario: dict[str, Any]) -> dict[str, Any]:
    response = scenario["response"]
    return {
        "id": f"sim-{scenario['id']}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "simulator-v1",
        "choices": [{"index": 0, "message": response["message"], "finish_reason": response["finish_reason"]}],
        "usage": response["usage"],
    }


async def _stream_scenario(scenario: dict[str, Any]) -> AsyncIterator[str]:
    response = scenario["response"]
    completion_id = f"sim-{scenario['id']}"
    base = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "simulator-v1",
    }
    yield _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})

    if scenario["behavior"] == "partial_stream":
        yield _sse({**base, "choices": [{"index": 0, "delta": {"content": "Partial synthetic "}, "finish_reason": None}]})
        yield _sse({"error": {"type": "provider_error", "code": "simulated_stream_failure"}})
        yield "data: [DONE]\n\n"
        return

    for fragment in response["fragments"]:
        yield _sse({**base, "choices": [{"index": 0, "delta": {"content": fragment}, "finish_reason": None}]})
        await asyncio.sleep(0)
    yield _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": response["finish_reason"]}]})
    if scenario["request"].get("stream_options", {}).get("include_usage"):
        yield _sse({**base, "choices": [], "usage": response["usage"]})
    yield "data: [DONE]\n\n"


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


_audience = os.getenv("SIMULATOR_AUDIENCE")
_caller_email = os.getenv("SIMULATOR_CALLER_EMAIL")
_identity_verifier = (
    GoogleServiceIdentityVerifier(_audience, _caller_email).verify
    if _audience and _caller_email
    else None
)
app = create_app(identity_verifier=_identity_verifier)
