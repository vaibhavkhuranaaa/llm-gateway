"""Fail-closed route loading and just-in-time provider credential resolution."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore, secretmanager

from services.routing import RoutePolicy, Target


LIVE_PRICE_TABLE_VERSION = "live-2026-08-10-direct-v1"
LIVE_ROUTE_DOCUMENT = "gateway-general"
LIVE_ROUTE_MAX_AGE = timedelta(days=30)
LIVE_PROVIDER_SPECS = {
    "openai": {
        "model": "gpt-5-mini-2025-08-07",
        "origin": "https://api.openai.com",
        "secret": "openai-api-key",
    },
    "anthropic": {
        "model": "claude-haiku-4-5-20251001",
        "origin": "https://api.anthropic.com",
        "secret": "anthropic-api-key",
    },
}


class CredentialResolutionError(Exception):
    """A live credential was unavailable without exposing provider or secret details."""


class FirestoreRouteLoader:
    def __init__(
        self,
        client: firestore.Client,
        *,
        clock: Callable[[], datetime] | None = None,
        simulator_url: str | None = None,
    ) -> None:
        self.client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._simulator_url = simulator_url or os.getenv("PROVIDER_SIMULATOR_URL", "http://provider-simulator")

    def load(self, tenant_id: str) -> RoutePolicy | None:
        snapshot = (
            self.client.collection("tenants")
            .document(tenant_id)
            .collection("routes")
            .document(LIVE_ROUTE_DOCUMENT)
            .get()
        )
        if not snapshot.exists:
            return None
        try:
            return self._parse(snapshot.to_dict() or {})
        except (KeyError, TypeError, ValueError):
            return None

    def _parse(self, document: Mapping[str, Any]) -> RoutePolicy | None:
        now = self._clock()
        updated_at = document["updated_at"]
        if (
            document.get("enabled") is not True
            or document.get("alias") != "gateway/general"
            or document.get("price_table_version") != LIVE_PRICE_TABLE_VERSION
            or not isinstance(updated_at, datetime)
            or updated_at > now + timedelta(minutes=5)
            or now - updated_at > LIVE_ROUTE_MAX_AGE
        ):
            return None

        raw_targets = document["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            return None
        targets = tuple(self._target(item) for item in raw_targets)
        return RoutePolicy(
            alias="gateway/general",
            version=str(document["version"]),
            targets=targets,
            price_table_version=LIVE_PRICE_TABLE_VERSION,
        )

    def _target(self, item: Mapping[str, Any]) -> Target:
        provider = str(item["provider"])
        if provider == "simulator":
            model = "simulator-v1"
            base_url = self._simulator_url
        else:
            specification = LIVE_PROVIDER_SPECS[provider]
            model = str(specification["model"])
            base_url = str(specification["origin"])
            if item.get("model") != model or item.get("origin") != provider:
                raise ValueError("unapproved provider target")
        target_id = str(item["id"])
        if target_id != provider:
            raise ValueError("target IDs are fixed to provider IDs")
        weight = item.get("weight", 0)
        enabled = item.get("enabled", True)
        fallbacks = item.get("fallbacks", [])
        if (
            not isinstance(weight, int)
            or isinstance(weight, bool)
            or weight < 0
            or weight > 10_000
            or not isinstance(enabled, bool)
            or not isinstance(fallbacks, list)
            or any(not isinstance(value, str) for value in fallbacks)
        ):
            raise ValueError("invalid route target")
        return Target(
            id=target_id,
            provider=provider,
            model=model,
            base_url=base_url,
            weight=weight,
            fallbacks=tuple(fallbacks),
            enabled=enabled,
        )


class SecretManagerCredentialResolver:
    def __init__(
        self,
        project_id: str,
        *,
        client: Any | None = None,
        region: str = "us-central1",
    ) -> None:
        if not project_id or region != "us-central1":
            raise ValueError("exact project and region are required")
        self.project_id = project_id
        self.region = region
        self._client = client

    @classmethod
    def from_environment(cls) -> "SecretManagerCredentialResolver | None":
        project_id = os.getenv("GCP_PROJECT_ID")
        return cls(project_id) if project_id else None

    def __call__(self, target: Target) -> str:
        try:
            specification = LIVE_PROVIDER_SPECS[target.provider]
            if target.model != specification["model"] or target.base_url != specification["origin"]:
                raise CredentialResolutionError("live credential unavailable")
            name = (
                f"projects/{self.project_id}/locations/{self.region}/secrets/"
                f"{specification['secret']}/versions/latest"
            )
            if self._client is None:
                self._client = secretmanager.SecretManagerServiceClient(
                    client_options={"api_endpoint": f"secretmanager.{self.region}.rep.googleapis.com"}
                )
            client = self._client
            response = client.access_secret_version(request={"name": name})
            value = response.payload.data.decode("utf-8").strip()
            if not value:
                raise CredentialResolutionError("live credential unavailable")
            return value
        except CredentialResolutionError:
            raise
        except Exception as error:
            raise CredentialResolutionError("live credential unavailable") from error


def public_live_configuration() -> dict[str, Any]:
    return {
        "proposed_targets": [
            {"provider": provider, "model": specification["model"]}
            for provider, specification in LIVE_PROVIDER_SPECS.items()
        ],
        "provider_processing_notice": (
            "If live mode is later approved and armed, committed synthetic content is processed by the selected "
            "provider. Default API retention may be up to 30 days; exact account settings require same-session "
            "verification. Zero Data Retention is not claimed."
        ),
    }
