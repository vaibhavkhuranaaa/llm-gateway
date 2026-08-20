"""OpenAI-compatible chat data plane for committed synthetic scenarios."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from services.catalog import ScenarioCatalog
from services.governance import GovernanceError, GovernanceStore, ReceiptMetadata, Reservation
from services.identity import FirestoreIdentityStore, KeyPrincipal
from services.live_configuration import (
    CredentialResolutionError,
    FirestoreRouteLoader,
    SecretManagerCredentialResolver,
)
from services.providers import (
    ProviderFailure,
    build_request,
    classify_status,
    normalize_response,
    normalized_stream,
)
from services.routing import RoutePolicy, Target
from services.service_identity import GoogleIdentityTokenProvider, ServiceIdentityError


class FailedAuthLimiter:
    """Bound failed authentication per client before key lookup."""

    def __init__(self, *, limit: int = 30, window_seconds: int = 60, max_clients: int = 5000) -> None:
        if min(limit, window_seconds, max_clients) < 1:
            raise ValueError("positive authentication throttle settings are required")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, client_id: str, now: float | None = None) -> bool:
        with self._lock:
            failures = self._active(client_id, time.monotonic() if now is None else now)
            return len(failures) >= self.limit

    def record_failure(self, client_id: str, now: float | None = None) -> None:
        with self._lock:
            current = time.monotonic() if now is None else now
            failures = self._active(client_id, current)
            if client_id not in self._failures and len(self._failures) >= self.max_clients:
                self._failures.pop(next(iter(self._failures)))
            failures.append(current)
            self._failures[client_id] = failures

    def clear(self, client_id: str) -> None:
        with self._lock:
            self._failures.pop(client_id, None)

    def _active(self, client_id: str, now: float) -> deque[float]:
        failures = self._failures.get(client_id, deque())
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if failures:
            self._failures[client_id] = failures
        else:
            self._failures.pop(client_id, None)
        return failures


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class FunctionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    parameters: dict[str, Any]


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: FunctionDefinition


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_usage: bool = False


class JsonSchemaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    schema_: dict[str, Any] = Field(alias="schema")
    strict: Literal[True]


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: JsonSchemaDefinition | None = None

    @model_validator(mode="after")
    def require_schema(self) -> "ResponseFormat":
        if (self.type == "json_schema") != (self.json_schema is not None):
            raise ValueError("json_schema definition must match response format type")
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[Message]
    stream: bool = False
    stream_options: StreamOptions | None = None
    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: ResponseFormat | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    stop: str | list[str] | None = None
    seed: int | None = None
    n: Literal[1] = 1

    @model_validator(mode="after")
    def validate_alias_and_stream(self) -> "ChatRequest":
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens and max_completion_tokens are mutually exclusive")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        return self


def create_app(
    *,
    provider_transport: httpx.AsyncBaseTransport | None = None,
    provider_transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    provider_api_keys: dict[str, str] | None = None,
    route_policy: RoutePolicy | None = None,
    route_loader: FirestoreRouteLoader | None = None,
    credential_resolver: Callable[[Target], str] | None = None,
    key_store: FirestoreIdentityStore | None = None,
    governance_store: GovernanceStore | None = None,
    failed_auth_limiter: FailedAuthLimiter | None = None,
    simulator_token_provider: Callable[[], str] | None = None,
    provider_timeout: float | None = None,
    catalog: ScenarioCatalog | None = None,
) -> FastAPI:
    application = FastAPI(title="Private LLM Gateway data plane", version="0.1.0")
    application.state.catalog = catalog or ScenarioCatalog()
    application.state.provider_transports = dict(provider_transports or {})
    if provider_transport is not None:
        application.state.provider_transports.setdefault("simulator", provider_transport)
    application.state.provider_api_keys = dict(provider_api_keys or {})
    application.state.route_policy = route_policy or RoutePolicy(
        alias="gateway/general",
        version="simulator-default",
        targets=(
            Target(
                id="simulator",
                provider="simulator",
                model="simulator-v1",
                base_url=os.getenv("PROVIDER_SIMULATOR_URL", "http://provider-simulator"),
                weight=100,
            ),
        ),
    )
    application.state.route_loader = route_loader
    application.state.credential_resolver = credential_resolver
    application.state.key_store = key_store
    application.state.governance_store = governance_store
    application.state.failed_auth_limiter = failed_auth_limiter or FailedAuthLimiter()
    application.state.simulator_token_provider = simulator_token_provider
    application.state.provider_timeout = provider_timeout or float(os.getenv("SIMULATOR_TIMEOUT_SECONDS", "2"))
    application.state.receipts = deque(maxlen=100)
    _validate_provider_origins(application.state.route_policy, application.state.provider_transports)

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "data-plane", "provider": "simulator"}

    @application.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        key_principal, auth_error = await _authenticate(
            request,
            application.state.key_store,
            application.state.failed_auth_limiter,
        )
        if auth_error is not None:
            return auth_error
        assert key_principal is not None

        scenario_id = request.headers.get("X-Gateway-Scenario-ID", "")
        scenario = application.state.catalog.get(scenario_id)
        if scenario is None:
            return _error(403, "hosted_prompt_not_allowed", "A committed synthetic scenario is required.")

        try:
            raw = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "invalid_request", "Request body must be valid JSON.")
        if not isinstance(raw, dict):
            return _error(400, "invalid_request", "Request body must be a JSON object.")
        try:
            chat_request = ChatRequest.model_validate(raw)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.errors(include_input=False)[0]["loc"])
            return _error(400, "invalid_request", "Request does not match the supported chat schema.", location)
        if raw != scenario["request"]:
            return _error(403, "hosted_prompt_not_allowed", "Request does not match the committed scenario.")

        request_id = f"chatcmpl-{secrets.token_hex(12)}"
        trace_id = secrets.token_hex(16)
        default_policy = application.state.route_policy
        if chat_request.model != default_policy.alias:
            return _error(404, "route_not_found", "The requested logical route does not exist.")
        policy = await _load_route_policy(application, key_principal.tenant_id)
        governance = application.state.governance_store
        if governance is not None and any(target.provider != "simulator" for target in policy.targets):
            eligible_live_ids = await asyncio.to_thread(
                governance.eligible_live_target_ids,
                key_principal.tenant_id,
                policy.targets,
                required_price_table_version=policy.price_table_version,
            )
            targets = policy.ordered_targets(
                request_id,
                lambda target: target.provider == "simulator" or target.id in eligible_live_ids,
            )
        else:
            targets = policy.ordered_targets(request_id)
        if not targets:
            if any(target.provider != "simulator" and target.enabled for target in policy.targets):
                return _error(503, "live_provider_disabled", "Live providers are disabled.")
            return _error(503, "no_eligible_provider", "No eligible provider target is available.")
        raw_request = chat_request.model_dump(by_alias=True, exclude_none=True)
        reservation: Reservation | None = None
        if governance is not None:
            try:
                reservation = await asyncio.to_thread(
                    governance.reserve,
                    tenant_id=key_principal.tenant_id,
                    key_id=key_principal.key_id,
                    request_id=request_id,
                    trace_id=trace_id,
                    targets=targets,
                    input_characters=sum(len(message.content or "") for message in chat_request.messages),
                    max_output_tokens=chat_request.max_completion_tokens or chat_request.max_tokens or 1024,
                    required_price_table_version=policy.price_table_version,
                )
            except GovernanceError as error:
                return _governance_error(error)
        started = time.perf_counter()

        if chat_request.stream:
            opened, attempts, failure = await _open_stream(application, targets, raw_request, scenario_id)
            if opened is None:
                assert failure is not None
                last_target = targets[attempts - 1] if attempts else None
                await _record_receipt(
                    application,
                    policy,
                    scenario_id,
                    chat_request,
                    request_id,
                    trace_id,
                    key_principal,
                    {"usage": {}},
                    "error",
                    last_target,
                    [target.id for target in targets[:attempts]],
                    {},
                    reservation,
                    started,
                    failure.status,
                    terminal_certain=False,
                )
                return _provider_error(failure, _headers(request_id, trace_id, chat_request.model, None, attempts))
            client, upstream, target, attempted_targets = opened
            return StreamingResponse(
                _stream_response(
                    application,
                    policy,
                    client,
                    upstream,
                    target,
                    attempted_targets,
                    scenario_id,
                    chat_request,
                    request_id,
                    trace_id,
                    key_principal,
                    reservation,
                    started,
                ),
                media_type="text/event-stream",
                headers={
                    **_headers(
                        request_id,
                        trace_id,
                        chat_request.model,
                        target,
                        attempts,
                        reservation.cost_reservation_micros if reservation else 0,
                        "reserved" if reservation else "reported",
                    ),
                    "Cache-Control": "no-cache",
                },
            )

        payload: dict[str, Any] | None = None
        selected: Target | None = None
        cost_inputs: dict[str, int] = {}
        attempted_targets: list[str] = []
        failure: ProviderFailure | None = None
        for target in targets:
            attempted_targets.append(target.id)
            try:
                client, upstream = await _send(application, target, raw_request, scenario_id, stream=False)
                try:
                    result = normalize_response(target.provider, upstream.json(), chat_request.model)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise ProviderFailure("provider_protocol_error", 502, fallback_allowed=True) from error
                finally:
                    await upstream.aclose()
                    await client.aclose()
                _validate_completion(result.body, chat_request)
                payload, selected, cost_inputs = result.body, target, result.cost_inputs
                break
            except (ValueError, JsonSchemaValidationError) as error:
                failure = ProviderFailure("provider_protocol_error", 502, fallback_allowed=True)
            except ProviderFailure as error:
                failure = error
            if failure is not None and not failure.fallback_allowed:
                break
        if payload is None or selected is None:
            assert failure is not None
            last_target = (
                next((item for item in targets if item.id == attempted_targets[-1]), None)
                if attempted_targets
                else None
            )
            await _record_receipt(
                application,
                policy,
                scenario_id,
                chat_request,
                request_id,
                trace_id,
                key_principal,
                {"usage": {}},
                "error",
                last_target,
                attempted_targets,
                {},
                reservation,
                started,
                failure.status,
                terminal_certain=False,
            )
            return _provider_error(
                failure,
                _headers(request_id, trace_id, chat_request.model, None, len(attempted_targets)),
            )

        payload["id"] = request_id
        payload["model"] = chat_request.model
        receipt = await _record_receipt(
            application,
            policy,
            scenario_id,
            chat_request,
            request_id,
            trace_id,
            key_principal,
            payload,
            "complete",
            selected,
            attempted_targets,
            cost_inputs,
            reservation,
            started,
            200,
            terminal_certain=len(attempted_targets) == 1 and _usage_reported(payload.get("usage")),
        )
        return JSONResponse(
            content=payload,
            headers=_headers(
                request_id,
                trace_id,
                chat_request.model,
                selected,
                len(attempted_targets),
                int(round(receipt["cost_usd"] * 1_000_000)),
                str(receipt["accounting_status"]),
            ),
        )

    return application


async def _load_route_policy(application: FastAPI, tenant_id: str) -> RoutePolicy:
    default = application.state.route_policy
    loader = application.state.route_loader
    if loader is None:
        return default
    try:
        loaded = await asyncio.to_thread(loader.load, tenant_id)
        if loaded is None:
            return default
        _validate_provider_origins(loaded, application.state.provider_transports)
        return loaded
    except Exception:
        return default


async def _authenticate(
    request: Request,
    key_store: FirestoreIdentityStore | None,
    limiter: FailedAuthLimiter,
) -> tuple[KeyPrincipal | None, JSONResponse | None]:
    if key_store is None:
        return None, _error(503, "gateway_not_configured", "Gateway authentication is not configured.")
    client_id = _client_identifier(request)
    if limiter.blocked(client_id):
        return None, JSONResponse(
            status_code=429,
            content={"error": {"message": "Authentication attempts are temporarily limited.", "type": "gateway_error", "param": None, "code": "authentication_rate_limited"}},
            headers={"Retry-After": str(limiter.window_seconds)},
        )
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        limiter.record_failure(client_id)
        return None, _error(401, "invalid_api_key", "A gateway bearer key is required.")
    supplied = authorization.removeprefix("Bearer ")
    authentication = await asyncio.to_thread(key_store.authenticate_key, supplied, "chat:completions")
    if authentication.code == "invalid_api_key":
        limiter.record_failure(client_id)
        return None, _error(401, "invalid_api_key", "The gateway key is invalid.")
    if authentication.code == "key_forbidden":
        limiter.record_failure(client_id)
        return None, _error(403, "key_forbidden", "The gateway key is expired, revoked, or out of scope.")
    limiter.clear(client_id)
    return authentication.principal, None


def _client_identifier(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    candidate = forwarded or (request.client.host if request.client else "unknown")
    try:
        normalized = ipaddress.ip_address(candidate).compressed
    except ValueError:
        normalized = "unknown"
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def _validate_completion(payload: dict[str, Any], request: ChatRequest) -> None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("one choice required")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("assistant message required")
    response_format = request.response_format
    if response_format is None or response_format.type == "text":
        return
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("structured content must be text")
    document = json.loads(content)
    if response_format.type == "json_schema":
        assert response_format.json_schema is not None
        schema = response_format.json_schema.schema_
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)


async def _send(
    application: FastAPI,
    target: Target,
    request: dict[str, Any],
    scenario_id: str,
    *,
    stream: bool,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    api_key = application.state.provider_api_keys.get(target.provider)
    if target.provider != "simulator" and api_key is None and application.state.credential_resolver is not None:
        try:
            api_key = await asyncio.to_thread(application.state.credential_resolver, target)
        except CredentialResolutionError as error:
            raise ProviderFailure("provider_unavailable", 503, fallback_allowed=True) from error
    if target.provider == "simulator" and application.state.simulator_token_provider is not None:
        try:
            api_key = await asyncio.to_thread(application.state.simulator_token_provider)
        except ServiceIdentityError as error:
            raise ProviderFailure("provider_unavailable", 503, fallback_allowed=True) from error
    path, headers, body = build_request(
        target,
        request,
        scenario_id,
        api_key,
    )
    transport = application.state.provider_transports.get(target.id)
    if transport is None:
        transport = application.state.provider_transports.get(target.provider)
    client = httpx.AsyncClient(
        base_url=target.base_url,
        transport=transport,
        timeout=application.state.provider_timeout,
    )
    try:
        response = await client.send(client.build_request("POST", path, headers=headers, json=body), stream=stream)
    except httpx.TimeoutException as error:
        await client.aclose()
        raise ProviderFailure("provider_timeout", 504, fallback_allowed=True) from error
    except httpx.HTTPError as error:
        await client.aclose()
        raise ProviderFailure("provider_unavailable", 503, fallback_allowed=True) from error
    if response.status_code < 200 or response.status_code >= 300:
        failure = classify_status(response.status_code)
        await response.aclose()
        await client.aclose()
        raise failure
    return client, response


async def _open_stream(
    application: FastAPI,
    targets: tuple[Target, ...],
    request: dict[str, Any],
    scenario_id: str,
) -> tuple[
    tuple[httpx.AsyncClient, httpx.Response, Target, list[str]] | None,
    int,
    ProviderFailure | None,
]:
    attempted: list[str] = []
    failure: ProviderFailure | None = None
    for target in targets:
        attempted.append(target.id)
        try:
            client, response = await _send(application, target, request, scenario_id, stream=True)
            return (client, response, target, attempted), len(attempted), None
        except ProviderFailure as error:
            failure = error
            if not error.fallback_allowed:
                break
    return None, len(attempted), failure


async def _stream_response(
    application: FastAPI,
    route_policy: RoutePolicy,
    client: httpx.AsyncClient,
    upstream: httpx.Response,
    target: Target,
    attempted_targets: list[str],
    scenario_id: str,
    chat_request: ChatRequest,
    request_id: str,
    trace_id: str,
    key_principal: KeyPrincipal,
    reservation: Reservation | None,
    started: float,
) -> AsyncIterator[str]:
    output_characters = 0
    usage: dict[str, Any] | None = None
    outcome = "complete"
    done = False
    try:
        async for chunk in normalized_stream(target.provider, upstream, chat_request.model):
            if chunk is None:
                done = True
                yield "data: [DONE]\n\n"
                break
            chunk["id"] = request_id
            chunk["model"] = chat_request.model
            usage = chunk.get("usage") or usage
            if chunk.get("choices") == [] and chunk.get("usage") and not (
                chat_request.stream_options and chat_request.stream_options.include_usage
            ):
                continue
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                output_characters += len(delta.get("content") or "")
            yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
        if not done:
            outcome = "partial"
            yield _sse_error("provider_stream_ended")
            yield "data: [DONE]\n\n"
    except ProviderFailure as error:
        outcome = "partial"
        yield _sse_error(error.code)
        yield "data: [DONE]\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        outcome = "cancelled"
        raise
    finally:
        await upstream.aclose()
        await client.aclose()
        await _record_receipt(
            application,
            route_policy,
            scenario_id,
            chat_request,
            request_id,
            trace_id,
            key_principal,
            {"usage": usage or {}, "output_characters": output_characters},
            outcome,
            target,
            attempted_targets,
            {
                "input_tokens": int((usage or {}).get("prompt_tokens", 0)),
                "output_tokens": int((usage or {}).get("completion_tokens", 0)),
                "cached_input_tokens": int(((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            },
            reservation,
            started,
            200,
            terminal_certain=(
                outcome == "complete"
                and len(attempted_targets) == 1
                and _usage_reported(usage)
            ),
        )


async def _record_receipt(
    application: FastAPI,
    route_policy: RoutePolicy,
    scenario_id: str,
    request: ChatRequest,
    request_id: str,
    trace_id: str,
    key_principal: KeyPrincipal,
    payload: dict[str, Any],
    outcome: str,
    target: Target | None,
    attempted_targets: list[str],
    cost_inputs: dict[str, int],
    reservation: Reservation | None,
    started: float,
    http_status: int,
    *,
    terminal_certain: bool,
) -> dict[str, Any]:
    input_characters = sum(len(message.content or "") for message in request.messages)
    message = ((payload.get("choices") or [{}])[0].get("message") or {}) if "choices" in payload else {}
    output_characters = payload.get("output_characters", len(message.get("content") or ""))
    local_receipt = {
        "request_id": request_id,
        "trace_id": trace_id,
        "tenant_id": key_principal.tenant_id,
        "key_id": key_principal.key_id,
        "scenario_id": scenario_id,
        "route": request.model,
        "provider": target.provider if target else "none",
        "provider_model": target.model if target else "none",
        "model": request.model,
        "attempts": len(attempted_targets),
        "attempted_targets": attempted_targets,
        "outcome": outcome,
        "input_characters": input_characters,
        "output_characters": output_characters,
        "tool_count": len(request.tools or []),
        "response_format": request.response_format.type if request.response_format else "text",
        "usage": payload.get("usage") or {},
        "cost_inputs": cost_inputs,
        "cost_usd": 0.0,
        "accounting_status": "unmanaged_local",
        "recorded_at": int(time.time()),
    }
    governance = application.state.governance_store
    if governance is not None and reservation is not None:
        usage = payload.get("usage") or {}
        metadata = ReceiptMetadata(
            request_id=request_id,
            trace_id=trace_id,
            tenant_id=key_principal.tenant_id,
            key_id=key_principal.key_id,
            scenario_id=scenario_id,
            route=request.model,
            provider=target.provider if target else "none",
            provider_model=target.model if target else "none",
            attempted_targets=tuple(attempted_targets),
            outcome=outcome,
            http_status=http_status,
            input_characters=input_characters,
            output_characters=output_characters,
            tool_count=len(request.tools or []),
            response_format=request.response_format.type if request.response_format else "text",
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            cached_input_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            recorded_at=datetime.now(UTC),
        )
        target_by_id = {item.id: item for item in route_policy.targets}
        live_attempts = sum(
            target_by_id[target_id].provider != "simulator"
            for target_id in attempted_targets
            if target_id in target_by_id
        )
        document = await asyncio.to_thread(
            governance.reconcile,
            reservation,
            metadata,
            selected_target=target,
            live_attempts=live_attempts,
            terminal_certain=terminal_certain,
        )
        local_receipt["cost_usd"] = int(document["cost_usd_micros"]) / 1_000_000
        local_receipt["accounting_status"] = document["accounting_status"]
    application.state.receipts.append(local_receipt)
    return local_receipt


def _headers(
    request_id: str,
    trace_id: str,
    model: str,
    target: Target | None,
    attempts: int,
    cost_micros: int = 0,
    usage_status: str = "reported",
) -> dict[str, str]:
    return {
        "X-Request-ID": request_id,
        "X-Trace-ID": trace_id,
        "X-Gateway-Route": model,
        "X-Gateway-Provider": target.provider if target else "none",
        "X-Gateway-Model": target.model if target else "none",
        "X-Gateway-Attempts": str(attempts),
        "X-Gateway-Cost-USD": f"{cost_micros / 1_000_000:.6f}",
        "X-Gateway-Usage-Status": usage_status,
    }


def _usage_reported(usage: Any) -> bool:
    return isinstance(usage, dict) and all(
        isinstance(usage.get(field), int)
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


def _provider_error(failure: ProviderFailure, headers: dict[str, str]) -> JSONResponse:
    messages = {
        "provider_timeout": "All eligible provider attempts timed out.",
        "provider_protocol_error": "All eligible providers violated the response contract.",
        "provider_rejected_request": "The provider rejected the normalized request.",
        "live_provider_disabled": "Live providers are disabled.",
    }
    code = failure.code
    if failure.code in {"provider_unavailable", "provider_not_supported"}:
        code = "no_eligible_provider"
    return _error(
        failure.status,
        code,
        messages.get(failure.code, "No eligible provider completed the request."),
        headers=headers,
    )


def _governance_error(error: GovernanceError) -> JSONResponse:
    public_code = "quota_exceeded" if error.code == "token_quota_exceeded" else error.code
    messages = {
        "quota_exceeded": "The request quota is exhausted.",
        "token_quota_exceeded": "The token quota is exhausted.",
        "budget_exceeded": "The monetary budget is exhausted.",
        "live_provider_disabled": "Live providers are disabled.",
        "live_session_limit": "The bounded live session has reached a fixed cap.",
        "policy_unavailable": "Admission policy is unavailable.",
        "price_unavailable": "Live-provider price configuration is unavailable.",
    }
    return _error(error.status, public_code, messages.get(error.code, "Admission was refused."))


def _error(
    status: int,
    code: str,
    message: str,
    param: str | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "gateway_error", "param": param, "code": code}},
        headers=headers,
    )


def _sse_error(code: str) -> str:
    payload = {"error": {"message": "The synthetic provider stream failed.", "type": "provider_error", "code": code}}
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _validate_provider_origins(policy: RoutePolicy, transports: dict[str, httpx.AsyncBaseTransport]) -> None:
    expected = {
        "simulator": os.getenv("PROVIDER_SIMULATOR_URL", "http://provider-simulator"),
        "openai": "https://api.openai.com",
        "anthropic": "https://api.anthropic.com",
    }
    for target in policy.targets:
        parsed = urlsplit(target.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (target.provider != "simulator" and parsed.scheme != "https")
        ):
            raise ValueError("invalid provider base URL")
        if target.id in transports or target.provider in transports:
            continue
        allowed = expected.get(target.provider)
        if allowed is None or target.base_url.rstrip("/") != allowed.rstrip("/"):
            raise ValueError("provider base URL is not allowlisted")


_identity_store = FirestoreIdentityStore.from_environment()
_simulator_audience = os.getenv("SIMULATOR_AUDIENCE")
_route_loader = FirestoreRouteLoader(_identity_store.client) if _identity_store else None
_credential_resolver = SecretManagerCredentialResolver.from_environment()
app = create_app(
    key_store=_identity_store,
    governance_store=GovernanceStore(_identity_store.client) if _identity_store else None,
    route_loader=_route_loader,
    credential_resolver=_credential_resolver,
    simulator_token_provider=(GoogleIdentityTokenProvider(_simulator_audience) if _simulator_audience else None),
)
