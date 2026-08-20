"""IAP-protected control-plane API foundation."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from services.catalog import ScenarioCatalog
from services.identity import (
    AuthorizationError,
    FirestoreIdentityStore,
    IAPIdentityVerifier,
    IdentityError,
    KeyLifecycleError,
    UserPrincipal,
)
from services.governance import GovernanceError, GovernanceStore
from services.live_configuration import public_live_configuration


class InviteUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    role: Literal["owner", "demo_operator"]
    subject: str | None = None


class IssueKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopes: list[str]
    expires_in_days: int = Field(default=30, ge=1, le=365)


class RotateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlap_seconds: int = Field(default=300, ge=0, le=600)


class PriceRates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int = Field(ge=0)
    output: int = Field(ge=0)
    cached_input: int | None = Field(default=None, ge=0)


class PolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_limit: int = Field(ge=1)
    token_limit: int = Field(ge=1)
    budget_usd_micros: int = Field(ge=1)
    price_table_version: str = Field(min_length=1, max_length=64)
    prices: dict[str, PriceRates]
    expected_version: str | None = None


class WorkbenchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1, max_length=128)


def create_app(
    *,
    store: FirestoreIdentityStore | None = None,
    verifier: IAPIdentityVerifier | None = None,
    governance_store: GovernanceStore | None = None,
    catalog: ScenarioCatalog | None = None,
    data_plane_base_url: str | None = None,
    data_plane_transport: httpx.AsyncBaseTransport | None = None,
    gateway_key: str | None = None,
    console_dist: Path | None = None,
) -> FastAPI:
    application = FastAPI(title="Private gateway control plane", version="0.1.0")
    application.state.identity_store = store
    application.state.iap_verifier = verifier
    application.state.governance_store = governance_store or (GovernanceStore(store.client) if store else None)
    application.state.catalog = catalog or ScenarioCatalog()
    application.state.data_plane_base_url = data_plane_base_url
    application.state.data_plane_transport = data_plane_transport
    application.state.gateway_key = gateway_key

    @application.middleware("http")
    async def browser_security(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("X-Workbench-CSRF") != "1":
            response = _error(403, "csrf_required")
        else:
            response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    def current_user(request: Request) -> UserPrincipal | JSONResponse:
        active_store = application.state.identity_store
        active_verifier = application.state.iap_verifier
        if active_store is None or active_verifier is None:
            return _error(503, "control_plane_not_configured")
        try:
            identity = active_verifier.verify(request.headers)
            return active_store.resolve_user(identity)
        except IdentityError:
            return _error(401, "invalid_iap_identity")
        except AuthorizationError:
            return _error(403, "role_forbidden")

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "control-plane"}

    @application.get("/v1/session")
    async def session(request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        return {"email": actor.email, "role": actor.role, "tenant_id": actor.tenant_id}

    @application.post("/v1/admin/users")
    async def invite_user(payload: InviteUserRequest, request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            invited = application.state.identity_store.invite_user(
                actor,
                payload.email,
                payload.role,
                subject=payload.subject,
            )
        except (AuthorizationError, IdentityError, ValueError):
            return _error(403, "role_forbidden")
        return {
            "email": invited.email,
            "role": invited.role,
            "tenant_id": invited.tenant_id,
            "status": "active",
        }

    @application.delete("/v1/admin/users/{email}")
    async def revoke_user(email: str, request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            application.state.identity_store.revoke_user(actor, email)
        except (AuthorizationError, IdentityError):
            return _error(403, "role_forbidden")
        return Response(status_code=204)

    @application.post("/v1/admin/gateway-keys")
    async def issue_key(payload: IssueKeyRequest, request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            issued = application.state.identity_store.issue_key(
                actor,
                payload.scopes,
                expires_at=datetime.now(UTC) + timedelta(days=payload.expires_in_days),
            )
        except (AuthorizationError, ValueError):
            return _error(403, "role_forbidden")
        return _issued_key_response(issued)

    @application.post("/v1/admin/gateway-keys/{key_id}/rotate")
    async def rotate_key(key_id: str, payload: RotateKeyRequest, request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            issued = application.state.identity_store.rotate_key(
                actor,
                key_id,
                overlap=timedelta(seconds=payload.overlap_seconds),
            )
        except AuthorizationError:
            return _error(403, "role_forbidden")
        except (KeyLifecycleError, ValueError):
            return _error(404, "gateway_key_not_found")
        return _issued_key_response(issued)

    @application.post("/v1/admin/gateway-keys/{key_id}/revoke")
    async def revoke_key(key_id: str, request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            application.state.identity_store.revoke_key(actor, key_id)
        except AuthorizationError:
            return _error(403, "role_forbidden")
        except KeyLifecycleError:
            return _error(404, "gateway_key_not_found")
        return Response(status_code=204)

    @application.get("/v1/operations/status")
    async def operations_status(request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        governance = application.state.governance_store
        if governance is None:
            return _error(503, "control_plane_not_configured")
        return {
            **governance.status(actor.tenant_id),
            "live_configuration": public_live_configuration(),
        }

    @application.get("/v1/workbench/scenarios")
    async def workbench_scenarios(request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        return {"scenarios": application.state.catalog.public_scenarios()}

    @application.post("/v1/workbench/runs")
    async def workbench_run(payload: WorkbenchRunRequest, request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        scenario = application.state.catalog.get(payload.scenario_id)
        if scenario is None:
            return _error(404, "scenario_not_found")
        base_url = application.state.data_plane_base_url
        gateway_key = application.state.gateway_key
        transport = application.state.data_plane_transport
        if not gateway_key or (not base_url and transport is None):
            return _error(503, "workbench_not_configured")

        client = httpx.AsyncClient(
            base_url=base_url or "http://data-plane",
            transport=transport,
            timeout=30,
        )
        upstream_request = client.build_request(
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {gateway_key}",
                "X-Gateway-Scenario-ID": payload.scenario_id,
            },
            json=scenario["request"],
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return _error(503, "data_plane_unavailable")

        headers = _workbench_headers(upstream.headers, payload.scenario_id)
        if upstream.status_code >= 400 or not scenario["request"].get("stream"):
            body = await upstream.aread()
            media_type = upstream.headers.get("content-type", "application/json").split(";", 1)[0]
            await upstream.aclose()
            await client.aclose()
            return Response(
                content=body,
                status_code=upstream.status_code,
                media_type=media_type,
                headers=headers,
            )

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type="text/event-stream",
            headers=headers,
        )

    @application.put("/v1/admin/policy")
    async def configure_policy(payload: PolicyRequest, request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        governance = application.state.governance_store
        if governance is None:
            return _error(503, "control_plane_not_configured")
        try:
            version = governance.configure_policy(
                actor,
                request_limit=payload.request_limit,
                token_limit=payload.token_limit,
                budget_usd_micros=payload.budget_usd_micros,
                price_table_version=payload.price_table_version,
                prices={key: value.model_dump(exclude_none=True) for key, value in payload.prices.items()},
                expected_version=payload.expected_version,
            )
        except AuthorizationError:
            return _error(403, "role_forbidden")
        except GovernanceError as error:
            return _error(error.status, error.code)
        except ValueError:
            return _error(400, "invalid_policy")
        return {"status": "updated", "version": version}

    @application.post("/v1/admin/live-session")
    async def arm_live_session(request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        governance = application.state.governance_store
        if governance is None:
            return _error(503, "control_plane_not_configured")
        try:
            session = governance.arm_live_session(actor)
        except AuthorizationError:
            return _error(403, "role_forbidden")
        except GovernanceError as error:
            return _error(error.status, error.code)
        return asdict(session)

    @application.delete("/v1/admin/live-session")
    async def stop_live_session(request: Request):
        actor = current_user(request)
        if isinstance(actor, JSONResponse):
            return actor
        governance = application.state.governance_store
        if governance is None:
            return _error(503, "control_plane_not_configured")
        try:
            governance.stop_live_session(actor)
        except AuthorizationError:
            return _error(403, "role_forbidden")
        except GovernanceError as error:
            return _error(error.status, error.code)
        return Response(status_code=204)

    dist = console_dist or Path(os.getenv("CONSOLE_DIST_PATH", "console/dist"))
    assets = dist / "assets"
    if dist.is_dir() and (dist / "index.html").is_file():
        if assets.is_dir():
            application.mount("/assets", StaticFiles(directory=assets), name="console-assets")

        @application.get("/", include_in_schema=False)
        async def console_index() -> FileResponse:
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})

    return application


def _issued_key_response(issued) -> JSONResponse:
    return JSONResponse(
        content={
            "key_id": issued.key_id,
            "gateway_key": issued.raw_key,
            "tenant_id": issued.tenant_id,
            "scopes": list(issued.scopes),
            "expires_at": issued.expires_at.isoformat(),
            "reveal": "once",
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"type": "control_plane_error", "code": code}},
        headers={"Cache-Control": "no-store"},
    )


_WORKBENCH_HEADER_ALLOWLIST = {
    "x-request-id",
    "x-trace-id",
    "x-gateway-route",
    "x-gateway-provider",
    "x-gateway-model",
    "x-gateway-attempts",
    "x-gateway-cost-usd",
    "x-gateway-usage-status",
}


def _workbench_headers(headers: httpx.Headers, scenario_id: str) -> dict[str, str]:
    allowed = {
        name: value
        for name, value in headers.items()
        if name.lower() in _WORKBENCH_HEADER_ALLOWLIST
    }
    return {
        **allowed,
        "X-Gateway-Scenario-ID": scenario_id,
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }


def _default_app() -> FastAPI:
    store = FirestoreIdentityStore.from_environment()
    audience = os.getenv("IAP_AUDIENCE")
    verifier = IAPIdentityVerifier(audience) if audience else None
    return create_app(
        store=store,
        verifier=verifier,
        data_plane_base_url=os.getenv("DATA_PLANE_BASE_URL"),
        gateway_key=os.getenv("CONSOLE_GATEWAY_KEY"),
    )


app = _default_app()
