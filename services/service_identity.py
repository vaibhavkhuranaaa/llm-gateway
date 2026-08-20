"""Cloud Run service-to-service identity token boundary."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from google.auth import jwt
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token


GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class ServiceIdentityError(Exception):
    """The caller is not the configured Cloud Run service identity."""


class GoogleServiceIdentityVerifier:
    def __init__(
        self,
        audience: str,
        caller_email: str,
        *,
        token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
    ) -> None:
        if not audience or not caller_email:
            raise ValueError("service audience and caller email are required")
        self._audience = audience
        self._caller_email = caller_email.strip().lower()
        self._token_verifier = token_verifier or self._verify_google_token

    def verify(self, headers: Mapping[str, str]) -> None:
        authorization = next(
            (value for key, value in headers.items() if key.lower() == "authorization"),
            "",
        )
        if not authorization.startswith("Bearer "):
            raise ServiceIdentityError("missing service identity")
        token = authorization.removeprefix("Bearer ")
        try:
            claims = self._token_verifier(token, self._audience)
            issuer = str(claims.get("iss", ""))
            email = str(claims["email"]).strip().lower()
            audience = str(claims["aud"])
        except (KeyError, TypeError, ValueError) as error:
            raise ServiceIdentityError("invalid service identity") from error
        if (
            issuer not in GOOGLE_ISSUERS
            or not secrets.compare_digest(audience, self._audience)
            or not secrets.compare_digest(email, self._caller_email)
        ):
            raise ServiceIdentityError("service identity mismatch")

    @staticmethod
    def _verify_google_token(token: str, audience: str) -> Mapping[str, Any]:
        return id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=audience)


class GoogleIdentityTokenProvider:
    """Cache an ADC identity token until five minutes before expiration."""

    def __init__(
        self,
        audience: str,
        *,
        token_fetcher: Callable[[str], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not audience:
            raise ValueError("service audience is required")
        self._audience = audience
        self._token_fetcher = token_fetcher or self._fetch_google_token
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at - 300:
                return self._token
            token = self._token_fetcher(self._audience)
            claims = jwt.decode(token, verify=False)
            expires_at = float(claims.get("exp", 0))
            if not token or expires_at <= self._clock():
                raise ServiceIdentityError("identity token is missing or expired")
            self._token = token
            self._expires_at = expires_at
            return token

    @staticmethod
    def _fetch_google_token(audience: str) -> str:
        return id_token.fetch_id_token(GoogleAuthRequest(), audience)
