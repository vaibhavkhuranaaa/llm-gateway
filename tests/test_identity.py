from __future__ import annotations

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from services.control_plane import create_app as create_control_plane
from services.data_plane import create_app as create_data_plane
from services.identity import (
    AuthorizationError,
    FirestoreIdentityStore,
    IAPIdentity,
    IAPIdentityVerifier,
    IdentityError,
    KeyLifecycleError,
)
from services.provider_simulator import create_app as create_simulator
from tests.test_protocol import SCENARIOS, SIMULATOR_TOKEN, simulator_identity


AUDIENCE = "/projects/123456789/locations/us-central1/services/gateway-console"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def identity_store(clock: MutableClock) -> FirestoreIdentityStore:
    project = f"identity-{uuid4().hex}"
    client = firestore.Client(
        project=project,
        database="(default)",
        credentials=AnonymousCredentials(),
    )
    return FirestoreIdentityStore(client, pepper=secrets.token_bytes(32), clock=clock)


def token_verifier(token: str, audience: str) -> dict[str, object]:
    assert audience == AUDIENCE
    identities = {
        "owner-token": ("owner@example.com", "owner-subject"),
        "demo-token": ("demo@example.com", "demo-subject"),
        "other-token": ("other@example.com", "other-subject"),
        "ghost-token": ("ghost@example.com", "ghost-subject"),
    }
    if token not in identities:
        raise ValueError("synthetic signature failure")
    email, subject = identities[token]
    return {
        "iss": "https://cloud.google.com/iap",
        "aud": audience,
        "sub": subject,
        "email": email,
    }


def iap_headers(token: str, email: str) -> dict[str, str]:
    return {
        "X-Goog-IAP-JWT-Assertion": token,
        "X-Goog-Authenticated-User-Email": f"accounts.google.com:{email}",
        "X-Workbench-CSRF": "1",
    }


def test_iap_assertion_and_firestore_role_boundary(identity_store: FirestoreIdentityStore) -> None:
    verifier = IAPIdentityVerifier(AUDIENCE, token_verifier=token_verifier)
    owner = identity_store.bootstrap_owner("OWNER@EXAMPLE.COM", "tenant_alpha", subject="owner-subject")
    application = create_control_plane(store=identity_store, verifier=verifier)
    client = TestClient(application)

    session = client.get("/v1/session", headers=iap_headers("owner-token", "OWNER@example.com"))
    assert session.status_code == 200
    assert session.json() == {
        "email": "owner@example.com",
        "role": "owner",
        "tenant_id": "tenant_alpha",
    }

    invite = client.post(
        "/v1/admin/users",
        headers=iap_headers("owner-token", "owner@example.com"),
        json={"email": "DEMO@example.com", "role": "demo_operator", "subject": "demo-subject"},
    )
    assert invite.status_code == 200
    assert invite.json()["role"] == "demo_operator"
    demo_session = client.get("/v1/session", headers=iap_headers("demo-token", "demo@example.com"))
    assert demo_session.status_code == 200

    denied = client.post(
        "/v1/admin/gateway-keys",
        headers=iap_headers("demo-token", "demo@example.com"),
        json={"scopes": ["chat:completions"]},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "role_forbidden"

    issued = client.post(
        "/v1/admin/gateway-keys",
        headers=iap_headers("owner-token", "owner@example.com"),
        json={"scopes": ["chat:completions"]},
    )
    assert issued.status_code == 200
    assert issued.headers["cache-control"] == "no-store"
    assert issued.headers["pragma"] == "no-cache"
    reveal = issued.json()
    assert reveal["reveal"] == "once"
    stored = identity_store.key_document("tenant_alpha", reveal["key_id"])
    assert stored is not None
    assert reveal["gateway_key"] not in json.dumps(stored, default=str)

    assert client.get("/v1/session", headers=iap_headers("ghost-token", "ghost@example.com")).status_code == 403
    assert client.get("/v1/session", headers=iap_headers("owner-token", "demo@example.com")).status_code == 401
    assert client.get("/v1/session").status_code == 401

    other = identity_store.bootstrap_owner("other@example.com", "tenant_other", subject="other-subject")
    with pytest.raises(AuthorizationError):
        identity_store.revoke_user(owner, other.email)
    identity_store.revoke_user(owner, "demo@example.com")
    assert client.get("/v1/session", headers=iap_headers("demo-token", "demo@example.com")).status_code == 403

    with pytest.raises(IdentityError):
        verifier.verify(iap_headers("forged-token", "owner@example.com"))


def test_first_iap_login_binds_the_invited_email_to_one_google_subject(
    identity_store: FirestoreIdentityStore,
) -> None:
    owner = identity_store.bootstrap_owner("owner@example.com", "tenant_alpha")
    assert owner.subject == ""

    first = identity_store.resolve_user(IAPIdentity("owner@example.com", "first-subject"))
    assert first.subject == "first-subject"
    stored = identity_store.client.collection("users").document(owner.email).get().to_dict()
    assert stored is not None
    assert stored["subject"] == "first-subject"

    with pytest.raises(AuthorizationError, match="identity subject changed"):
        identity_store.resolve_user(IAPIdentity("owner@example.com", "different-subject"))


def test_key_issue_rotation_expiry_revocation_and_no_raw_storage(
    identity_store: FirestoreIdentityStore,
    clock: MutableClock,
) -> None:
    owner = identity_store.bootstrap_owner("owner@example.com", "tenant_alpha", subject="owner-subject")
    issued = identity_store.issue_key(
        owner,
        ["chat:completions", "receipts:read"],
        expires_at=clock() + timedelta(days=30),
    )
    assert issued.raw_key.startswith(f"gw_{issued.key_id}_")
    assert identity_store.authenticate_key(issued.raw_key, "chat:completions").principal is not None
    assert identity_store.authenticate_key(issued.raw_key, "admin:write").code == "key_forbidden"
    assert identity_store.authenticate_key("gw_invalid", "chat:completions").code == "invalid_api_key"

    stored = identity_store.key_document(owner.tenant_id, issued.key_id)
    assert stored is not None
    serialized = json.dumps(stored, default=str)
    assert issued.raw_key not in serialized
    assert issued.raw_key.rsplit("_", 1)[-1] not in serialized
    assert stored["key_prefix"] == f"gw_{issued.key_id}"
    assert len(stored["digest"]) == 64
    assert stored["last_used_at"] == clock()

    rotated = identity_store.rotate_key(owner, issued.key_id, overlap=timedelta(seconds=30))
    assert identity_store.authenticate_key(issued.raw_key, "chat:completions").code == "ok"
    assert identity_store.authenticate_key(rotated.raw_key, "chat:completions").code == "ok"
    clock.advance(seconds=31)
    assert identity_store.authenticate_key(issued.raw_key, "chat:completions").code == "key_forbidden"
    identity_store.revoke_key(owner, rotated.key_id)
    assert identity_store.authenticate_key(rotated.raw_key, "chat:completions").code == "key_forbidden"

    expiring = identity_store.issue_key(
        owner,
        ["chat:completions"],
        expires_at=clock() + timedelta(seconds=1),
    )
    clock.advance(seconds=2)
    assert identity_store.authenticate_key(expiring.raw_key, "chat:completions").code == "key_forbidden"


def test_transactional_concurrency_and_data_plane_revocation(
    identity_store: FirestoreIdentityStore,
    clock: MutableClock,
) -> None:
    owner = identity_store.bootstrap_owner("owner@example.com", "tenant_alpha", subject="owner-subject")
    issued = identity_store.issue_key(
        owner,
        ["chat:completions"],
        expires_at=clock() + timedelta(days=30),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        authentications = list(
            executor.map(
                lambda _: identity_store.authenticate_key(issued.raw_key, "chat:completions").code,
                range(16),
            )
        )
    assert set(authentications) == {"ok"}

    def rotate() -> str:
        try:
            return identity_store.rotate_key(owner, issued.key_id).key_id
        except KeyLifecycleError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        rotations = list(executor.map(lambda _: rotate(), range(2)))
    assert rotations.count("rejected") == 1
    replacement_id = next(value for value in rotations if value != "rejected")
    replacement_document = identity_store.key_document(owner.tenant_id, replacement_id)
    assert replacement_document is not None

    simulator = create_simulator(identity_verifier=simulator_identity)
    application = create_data_plane(
        provider_transport=httpx.ASGITransport(app=simulator),
        provider_api_keys={"simulator": SIMULATOR_TOKEN},
        key_store=identity_store,
    )
    client = TestClient(application)
    scenario = SCENARIOS["text.nonstream"]
    # Concurrent rotation returns the raw key only to its caller; issue a fresh key for the HTTP boundary.
    http_key = identity_store.issue_key(
        owner,
        ["chat:completions"],
        expires_at=clock() + timedelta(days=30),
    )
    headers = {
        "Authorization": f"Bearer {http_key.raw_key}",
        "X-Gateway-Scenario-ID": scenario["id"],
    }
    assert client.post("/v1/chat/completions", headers=headers, json=scenario["request"]).status_code == 200
    identity_store.revoke_key(owner, http_key.key_id)
    revoked = client.post("/v1/chat/completions", headers=headers, json=scenario["request"])
    assert revoked.status_code == 403
    assert revoked.json()["error"]["code"] == "key_forbidden"
    unknown = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer gw_{secrets.token_hex(8)}_{secrets.token_urlsafe(32)}",
            "X-Gateway-Scenario-ID": scenario["id"],
        },
        json=scenario["request"],
    )
    assert unknown.status_code == 401
