"""IAP identity, Firestore roles, and gateway-key lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from google.auth.credentials import AnonymousCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore
from google.oauth2 import id_token


IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
IAP_ISSUER = "https://cloud.google.com/iap"
EMAIL_PATTERN = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
KEY_PATTERN = re.compile(r"^gw_([0-9a-f]{16})_([A-Za-z0-9_-]{43})$")
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9:_-]{1,63}$")
Role = Literal["owner", "demo_operator"]


class IdentityError(Exception):
    """The IAP identity could not be authenticated."""


class AuthorizationError(Exception):
    """The authenticated identity lacks application authority."""


class KeyLifecycleError(Exception):
    """A gateway-key mutation could not be completed."""


@dataclass(frozen=True)
class IAPIdentity:
    email: str
    subject: str


@dataclass(frozen=True)
class UserPrincipal:
    email: str
    subject: str
    role: Role
    tenant_id: str


@dataclass(frozen=True)
class KeyPrincipal:
    key_id: str
    tenant_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class KeyAuthentication:
    principal: KeyPrincipal | None
    code: Literal["ok", "invalid_api_key", "key_forbidden"]


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    raw_key: str
    tenant_id: str
    scopes: tuple[str, ...]
    expires_at: datetime


def normalize_email(value: str) -> str:
    normalized = value.strip().lower().removeprefix("accounts.google.com:")
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise IdentityError("invalid IAP email")
    return normalized


class IAPIdentityVerifier:
    def __init__(
        self,
        audience: str,
        *,
        token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
    ) -> None:
        if not audience:
            raise ValueError("IAP audience is required")
        self._audience = audience
        self._token_verifier = token_verifier or self._verify_google_token

    def verify(self, headers: Mapping[str, str]) -> IAPIdentity:
        lowered = {key.lower(): value for key, value in headers.items()}
        assertion = lowered.get("x-goog-iap-jwt-assertion")
        header_email = lowered.get("x-goog-authenticated-user-email")
        if not assertion or not header_email:
            raise IdentityError("missing IAP identity")
        try:
            claims = self._token_verifier(assertion, self._audience)
            if claims.get("iss") != IAP_ISSUER:
                raise IdentityError("invalid IAP issuer")
            if claims.get("aud") != self._audience:
                raise IdentityError("invalid IAP audience")
            subject = str(claims["sub"])
            claim_email = normalize_email(str(claims["email"]))
            trusted_header_email = normalize_email(header_email)
        except (IdentityError, KeyError, TypeError, ValueError) as error:
            raise IdentityError("invalid IAP assertion") from error
        if not subject or not secrets.compare_digest(claim_email, trusted_header_email):
            raise IdentityError("IAP identity mismatch")
        return IAPIdentity(email=claim_email, subject=subject)

    @staticmethod
    def _verify_google_token(token: str, audience: str) -> Mapping[str, Any]:
        return id_token.verify_token(
            token,
            GoogleAuthRequest(),
            audience=audience,
            certs_url=IAP_CERTS_URL,
        )


class FirestoreIdentityStore:
    def __init__(
        self,
        client: firestore.Client,
        *,
        pepper: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(pepper) < 32:
            raise ValueError("gateway-key pepper must be at least 32 bytes")
        self.client = client
        self._pepper = pepper
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_environment(cls) -> "FirestoreIdentityStore | None":
        project = os.getenv("GCP_PROJECT_ID")
        pepper = os.getenv("GATEWAY_KEY_PEPPER")
        if not project or not pepper:
            return None
        credentials = AnonymousCredentials() if os.getenv("FIRESTORE_EMULATOR_HOST") else None
        client = firestore.Client(
            project=project,
            database=os.getenv("FIRESTORE_DATABASE", "(default)"),
            credentials=credentials,
        )
        return cls(client, pepper=pepper.encode())

    def bootstrap_owner(
        self,
        email: str,
        tenant_id: str,
        *,
        subject: str | None = None,
    ) -> UserPrincipal:
        normalized = normalize_email(email)
        self._validate_tenant(tenant_id)
        now = self._clock()
        self.client.collection("users").document(normalized).set(
            {
                "email": normalized,
                "subject": subject,
                "role": "owner",
                "tenant_id": tenant_id,
                "status": "active",
                "invited_by": "bootstrap",
                "created_at": now,
                "revoked_at": None,
            }
        )
        return UserPrincipal(normalized, subject or "", "owner", tenant_id)

    def resolve_user(self, identity: IAPIdentity) -> UserPrincipal:
        reference = self.client.collection("users").document(identity.email)
        transaction = self.client.transaction()

        @firestore.transactional
        def resolve(transaction: firestore.Transaction) -> tuple[Role, str]:
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                raise AuthorizationError("identity is not invited")
            document = snapshot.to_dict() or {}
            if document.get("status") != "active":
                raise AuthorizationError("identity is inactive")
            role = document.get("role")
            if role not in {"owner", "demo_operator"}:
                raise AuthorizationError("identity role is invalid")
            subject = document.get("subject")
            if subject is None:
                transaction.update(
                    reference,
                    {"subject": identity.subject, "subject_bound_at": self._clock()},
                )
            elif subject != identity.subject:
                raise AuthorizationError("identity subject changed")
            return role, str(document["tenant_id"])

        role, tenant_id = resolve(transaction)
        return UserPrincipal(identity.email, identity.subject, role, tenant_id)

    def invite_user(self, actor: UserPrincipal, email: str, role: Role, *, subject: str | None = None) -> UserPrincipal:
        self._require_owner(actor)
        normalized = normalize_email(email)
        if role not in {"owner", "demo_operator"}:
            raise ValueError("unsupported role")
        now = self._clock()
        self.client.collection("users").document(normalized).set(
            {
                "email": normalized,
                "subject": subject,
                "role": role,
                "tenant_id": actor.tenant_id,
                "status": "active",
                "invited_by": actor.email,
                "created_at": now,
                "revoked_at": None,
            }
        )
        return UserPrincipal(normalized, subject or "", role, actor.tenant_id)

    def revoke_user(self, actor: UserPrincipal, email: str) -> None:
        self._require_owner(actor)
        normalized = normalize_email(email)
        reference = self.client.collection("users").document(normalized)
        snapshot = reference.get()
        document = snapshot.to_dict() or {}
        if not snapshot.exists or document.get("tenant_id") != actor.tenant_id:
            raise AuthorizationError("user is outside the actor tenant")
        if normalized == actor.email:
            raise AuthorizationError("owner cannot revoke the active self")
        reference.update({"status": "revoked", "revoked_at": self._clock()})

    def issue_key(
        self,
        actor: UserPrincipal,
        scopes: Sequence[str],
        *,
        expires_at: datetime,
        replaces: str | None = None,
    ) -> IssuedKey:
        self._require_owner(actor)
        normalized_scopes = self._validate_scopes(scopes)
        now = self._clock()
        if expires_at.tzinfo is None or expires_at <= now:
            raise ValueError("key expiry must be a future timezone-aware timestamp")
        issued, document, index = self._new_key(actor.tenant_id, normalized_scopes, expires_at, now, replaces)
        batch = self.client.batch()
        batch.create(self._key_reference(actor.tenant_id, issued.key_id), document)
        batch.create(self.client.collection("gateway_key_index").document(issued.key_id), index)
        batch.commit()
        return issued

    def rotate_key(
        self,
        actor: UserPrincipal,
        key_id: str,
        *,
        overlap: timedelta = timedelta(minutes=5),
        expires_at: datetime | None = None,
    ) -> IssuedKey:
        self._require_owner(actor)
        if overlap < timedelta(0) or overlap > timedelta(minutes=10):
            raise ValueError("rotation overlap must be between zero and ten minutes")
        now = self._clock()
        old_reference = self._key_reference(actor.tenant_id, key_id)
        old_snapshot = old_reference.get()
        old_document = old_snapshot.to_dict() or {}
        if not old_snapshot.exists or old_document.get("tenant_id") != actor.tenant_id:
            raise KeyLifecycleError("gateway key not found")
        scopes = self._validate_scopes(old_document.get("scopes", []))
        new_expiry = expires_at or old_document["expires_at"]
        issued, document, index = self._new_key(actor.tenant_id, scopes, new_expiry, now, key_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def rotate(transaction: firestore.Transaction) -> None:
            current = old_reference.get(transaction=transaction)
            current_document = current.to_dict() or {}
            if not current.exists or current_document.get("status") != "active":
                raise KeyLifecycleError("only an active key can rotate")
            transaction.create(self._key_reference(actor.tenant_id, issued.key_id), document)
            transaction.create(self.client.collection("gateway_key_index").document(issued.key_id), index)
            transaction.update(
                old_reference,
                {
                    "status": "rotating",
                    "overlap_expires_at": now + overlap,
                    "replaced_by": issued.key_id,
                },
            )

        rotate(transaction)
        return issued

    def revoke_key(self, actor: UserPrincipal, key_id: str) -> None:
        self._require_owner(actor)
        reference = self._key_reference(actor.tenant_id, key_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def revoke(transaction: firestore.Transaction) -> None:
            snapshot = reference.get(transaction=transaction)
            document = snapshot.to_dict() or {}
            if not snapshot.exists or document.get("tenant_id") != actor.tenant_id:
                raise KeyLifecycleError("gateway key not found")
            transaction.update(reference, {"status": "revoked", "revoked_at": self._clock()})

        revoke(transaction)

    def authenticate_key(self, raw_key: str, required_scope: str) -> KeyAuthentication:
        match = KEY_PATTERN.fullmatch(raw_key)
        if not match:
            return KeyAuthentication(None, "invalid_api_key")
        key_id = match.group(1)
        index = self.client.collection("gateway_key_index").document(key_id).get()
        if not index.exists:
            return KeyAuthentication(None, "invalid_api_key")
        tenant_id = str((index.to_dict() or {}).get("tenant_id", ""))
        if not tenant_id:
            return KeyAuthentication(None, "invalid_api_key")
        reference = self._key_reference(tenant_id, key_id)
        snapshot = reference.get()
        if not snapshot.exists:
            return KeyAuthentication(None, "invalid_api_key")
        document = snapshot.to_dict() or {}
        expected = str(document.get("digest", ""))
        if not expected or not hmac.compare_digest(expected, self._digest(raw_key)):
            return KeyAuthentication(None, "invalid_api_key")
        now = self._clock()
        status = document.get("status")
        overlap_expires_at = document.get("overlap_expires_at")
        status_valid = status == "active" or (
            status == "rotating" and overlap_expires_at is not None and now < overlap_expires_at
        )
        scopes = tuple(document.get("scopes", []))
        if not status_valid or now >= document["expires_at"] or required_scope not in scopes and "*" not in scopes:
            return KeyAuthentication(None, "key_forbidden")
        last_used_at = document.get("last_used_at")
        if last_used_at is None or now - last_used_at >= timedelta(minutes=5):
            try:
                reference.update({"last_used_at": now})
            except GoogleAPICallError:
                pass
        return KeyAuthentication(KeyPrincipal(key_id, tenant_id, scopes), "ok")

    def key_document(self, tenant_id: str, key_id: str) -> dict[str, Any] | None:
        snapshot = self._key_reference(tenant_id, key_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def _new_key(
        self,
        tenant_id: str,
        scopes: tuple[str, ...],
        expires_at: datetime,
        now: datetime,
        replaces: str | None,
    ) -> tuple[IssuedKey, dict[str, Any], dict[str, str]]:
        key_id = secrets.token_hex(8)
        raw_key = f"gw_{key_id}_{secrets.token_urlsafe(32)}"
        issued = IssuedKey(key_id, raw_key, tenant_id, scopes, expires_at)
        document = {
            "key_id": key_id,
            "tenant_id": tenant_id,
            "key_prefix": f"gw_{key_id}",
            "digest": self._digest(raw_key),
            "scopes": list(scopes),
            "status": "active",
            "expires_at": expires_at,
            "created_at": now,
            "last_used_at": None,
            "overlap_expires_at": None,
            "revoked_at": None,
            "replaces": replaces,
            "replaced_by": None,
        }
        return issued, document, {"tenant_id": tenant_id}

    def _digest(self, raw_key: str) -> str:
        return hmac.new(self._pepper, raw_key.encode(), hashlib.sha256).hexdigest()

    def _key_reference(self, tenant_id: str, key_id: str):
        return (
            self.client.collection("tenants")
            .document(tenant_id)
            .collection("gateway_keys")
            .document(key_id)
        )

    @staticmethod
    def _validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(scopes)))
        if not normalized or any(scope != "*" and not SCOPE_PATTERN.fullmatch(scope) for scope in normalized):
            raise ValueError("at least one valid key scope is required")
        return normalized

    @staticmethod
    def _validate_tenant(tenant_id: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", tenant_id):
            raise ValueError("invalid tenant ID")

    @staticmethod
    def _require_owner(actor: UserPrincipal) -> None:
        if actor.role != "owner":
            raise AuthorizationError("owner role required")
