"""Firestore admission, bounded live sessions, and metadata-only observability."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore

from services.identity import AuthorizationError, UserPrincipal
from services.routing import Target


LIVE_DURATION = timedelta(minutes=30)
LIVE_REQUEST_LIMIT = 20
LIVE_SPEND_LIMIT_MICROS = 1_000_000
RECEIPT_TTL = timedelta(days=7)
RESERVATION_TTL = timedelta(days=1)
LIVE_RECORD_TTL = timedelta(days=1)
USAGE_BUCKET_TTL = timedelta(days=35)
PRICE_TABLE_MAX_AGE = timedelta(days=30)
GOVERNANCE_TRANSACTION_MAX_ATTEMPTS = 10


class GovernanceError(Exception):
    def __init__(self, code: str, status: int = 429) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Reservation:
    request_id: str
    tenant_id: str
    key_id: str
    trace_id: str
    window_id: str
    token_reservation: int
    cost_reservation_micros: int
    live_request_reservation: int
    live_session_id: str | None
    bucket_shard: int = 0


@dataclass(frozen=True)
class LiveSession:
    session_id: str
    tenant_id: str
    owner_email: str
    state: str
    armed_at: datetime
    expires_at: datetime
    request_limit: int
    requests_charged: int
    spend_limit_micros: int
    spend_charged_micros: int
    reserved_spend_micros: int
    reconciled_spend_micros: int


@dataclass(frozen=True)
class ReceiptMetadata:
    request_id: str
    trace_id: str
    tenant_id: str
    key_id: str
    scenario_id: str
    route: str
    provider: str
    provider_model: str
    attempted_targets: tuple[str, ...]
    outcome: str
    http_status: int
    input_characters: int
    output_characters: int
    tool_count: int
    response_format: str
    prompt_tokens: int
    completion_tokens: int
    cached_input_tokens: int
    latency_ms: int
    recorded_at: datetime


class MetadataTelemetry:
    """Bounded local views that mimic allowlisted log, trace, metric, and cost exports."""

    def __init__(
        self,
        maxlen: int = 500,
        *,
        exporter: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.logs: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.traces: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.costs: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.metrics: Counter[tuple[str, str, str]] = Counter()
        self._exporter = exporter or (
            self._export_cloud_log if os.getenv("METADATA_LOG_EXPORT") == "1" else None
        )

    def emit(self, receipt: ReceiptMetadata, cost_micros: int, accounting_status: str) -> None:
        correlation = {
            "request_id": receipt.request_id,
            "trace_id": receipt.trace_id,
            "tenant_id": receipt.tenant_id,
        }
        self.logs.append(
            {
                **correlation,
                "provider": receipt.provider,
                "model": receipt.provider_model,
                "outcome": receipt.outcome,
                "http_status": receipt.http_status,
                "attempts": len(receipt.attempted_targets),
            }
        )
        self.traces.append(
            {
                **correlation,
                "route": receipt.route,
                "latency_ms": receipt.latency_ms,
                "attempted_targets": list(receipt.attempted_targets),
            }
        )
        self.costs.append(
            {
                **correlation,
                "provider": receipt.provider,
                "model": receipt.provider_model,
                "cost_usd_micros": cost_micros,
                "accounting_status": accounting_status,
            }
        )
        self.metrics[(receipt.outcome, receipt.provider, accounting_status)] += 1
        if self._exporter is not None:
            project = os.getenv("GCP_PROJECT_ID", "")
            self._exporter(
                {
                    "severity": "INFO",
                    "message": "gateway_request",
                    "request_id": receipt.request_id,
                    "trace_id": receipt.trace_id,
                    "tenant_id": receipt.tenant_id,
                    "route": receipt.route,
                    "provider": receipt.provider,
                    "model": receipt.provider_model,
                    "outcome": receipt.outcome,
                    "http_status": receipt.http_status,
                    "attempts": len(receipt.attempted_targets),
                    "latency_ms": receipt.latency_ms,
                    "cost_usd_micros": cost_micros,
                    "accounting_status": accounting_status,
                    **(
                        {"logging.googleapis.com/trace": f"projects/{project}/traces/{receipt.trace_id}"}
                        if project
                        else {}
                    ),
                }
            )

    @staticmethod
    def _export_cloud_log(event: dict[str, Any]) -> None:
        print(json.dumps(event, separators=(",", ":"), sort_keys=True), flush=True)


class GovernanceStore:
    def __init__(
        self,
        client: firestore.Client,
        *,
        clock: Callable[[], datetime] | None = None,
        telemetry: MetadataTelemetry | None = None,
    ) -> None:
        self.client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self.telemetry = telemetry or MetadataTelemetry()

    def configure_policy(
        self,
        actor: UserPrincipal,
        *,
        request_limit: int,
        token_limit: int,
        budget_usd_micros: int,
        price_table_version: str,
        prices: Mapping[str, Mapping[str, int]],
        expected_version: str | None = None,
        usage_bucket_shards: int | None = None,
    ) -> str:
        self._require_owner(actor)
        if (
            min(request_limit, token_limit, budget_usd_micros) < 1
            or not price_table_version
            or (usage_bucket_shards is not None and not 1 <= usage_bucket_shards <= 64)
        ):
            raise ValueError("positive policy limits and price-table version are required")
        normalized_prices: dict[str, dict[str, int]] = {}
        for key, rates in prices.items():
            if not {"input", "output"}.issubset(rates) or not set(rates).issubset(
                {"input", "output", "cached_input"}
            ) or min(rates.values()) < 0:
                raise ValueError("price entries require non-negative input and output rates")
            normalized_prices[key] = {
                "input": int(rates["input"]),
                "output": int(rates["output"]),
                "cached_input": int(rates.get("cached_input", rates["input"])),
            }
        reference = self._policy_reference(actor.tenant_id)
        transaction = self.client.transaction()
        version = secrets.token_hex(8)

        @firestore.transactional
        def update(transaction: firestore.Transaction) -> GovernanceError | None:
            snapshot = reference.get(transaction=transaction)
            current = snapshot.to_dict() or {}
            if snapshot.exists and expected_version != current.get("version"):
                return GovernanceError("policy_version_conflict", 409)
            next_usage_bucket_shards = (
                usage_bucket_shards
                if usage_bucket_shards is not None
                else int(current.get("usage_bucket_shards", 1))
            )
            transaction.set(
                reference,
                {
                    "version": version,
                    "request_limit": request_limit,
                    "token_limit": token_limit,
                    "budget_usd_micros": budget_usd_micros,
                    "price_table_version": price_table_version,
                    "usage_bucket_shards": next_usage_bucket_shards,
                    "prices": normalized_prices,
                    "window": "utc_hour",
                    "updated_by": actor.email,
                    "updated_at": self._clock(),
                },
            )
            return None

        result = update(transaction)
        if result is not None:
            raise result
        return version

    def reserve(
        self,
        *,
        tenant_id: str,
        key_id: str,
        request_id: str,
        trace_id: str,
        targets: Sequence[Target],
        input_characters: int,
        max_output_tokens: int,
        required_price_table_version: str | None = None,
    ) -> Reservation:
        now = self._clock()
        window_id = now.strftime("%Y%m%d%H")
        policy_reference = self._policy_reference(tenant_id)
        policy_hint = policy_reference.get()
        if not policy_hint.exists:
            raise GovernanceError("policy_unavailable", 503)
        policy_hint_document = policy_hint.to_dict() or {}
        bucket_shards = int(policy_hint_document.get("usage_bucket_shards", 1))
        bucket_shard = int(hashlib.sha256(request_id.encode()).hexdigest(), 16) % bucket_shards
        bucket_reference = self._bucket_reference(tenant_id, window_id, bucket_shard)
        reservation_reference = self._reservation_reference(tenant_id, request_id)
        live_reference = self._live_reference(tenant_id)
        live_targets = [target for target in targets if target.provider != "simulator"]
        transaction = self.client.transaction(max_attempts=GOVERNANCE_TRANSACTION_MAX_ATTEMPTS)

        @firestore.transactional
        def admit(transaction: firestore.Transaction) -> Reservation | GovernanceError:
            policy_snapshot = policy_reference.get(transaction=transaction)
            reservation_snapshot = reservation_reference.get(transaction=transaction)
            bucket_snapshot = bucket_reference.get(transaction=transaction)
            live_snapshot = live_reference.get(transaction=transaction) if live_targets else None
            if not policy_snapshot.exists:
                return GovernanceError("policy_unavailable", 503)
            policy = policy_snapshot.to_dict() or {}
            if (
                required_price_table_version is not None
                and policy.get("price_table_version") != required_price_table_version
            ):
                return GovernanceError("price_unavailable", 503)
            if int(policy.get("usage_bucket_shards", 1)) != bucket_shards:
                return GovernanceError("policy_version_conflict", 409)
            if reservation_snapshot.exists:
                existing = reservation_snapshot.to_dict() or {}
                if existing.get("key_id") != key_id or existing.get("trace_id") != trace_id:
                    return GovernanceError("reservation_conflict", 409)
                return self._reservation_from_document(existing)

            token_reservation = math.ceil(input_characters / 4) + max_output_tokens
            try:
                cost_reservation = sum(
                    self._cost_micros(policy, target, math.ceil(input_characters / 4), max_output_tokens)
                    for target in live_targets
                )
            except KeyError:
                return GovernanceError("price_unavailable", 503)
            bucket = bucket_snapshot.to_dict() or {}
            request_charged = int(bucket.get("request_charged", 0))
            token_charged = int(bucket.get("token_charged", 0))
            cost_charged = int(bucket.get("cost_charged_micros", 0))
            request_limit = self._partition_limit(int(policy["request_limit"]), bucket_shard, bucket_shards)
            token_limit = self._partition_limit(int(policy["token_limit"]), bucket_shard, bucket_shards)
            budget_limit = self._partition_limit(
                int(policy["budget_usd_micros"]), bucket_shard, bucket_shards
            )
            if request_charged + 1 > request_limit:
                return GovernanceError("quota_exceeded")
            if token_charged + token_reservation > token_limit:
                return GovernanceError("token_quota_exceeded")
            if cost_charged + cost_reservation > budget_limit:
                return GovernanceError("budget_exceeded")

            session_id: str | None = None
            live = (live_snapshot.to_dict() or {}) if live_snapshot is not None else {}
            if live_targets:
                if live_snapshot is None or not live_snapshot.exists or live.get("state") != "active":
                    return GovernanceError("live_provider_disabled", 503)
                if now >= live["expires_at"]:
                    transaction.update(live_reference, {"state": "expired", "closed_at": now})
                    return GovernanceError("live_provider_disabled", 503)
                live_requests = int(live.get("requests_charged", 0)) + len(live_targets)
                live_spend = int(live.get("spend_charged_micros", 0)) + cost_reservation
                if live_requests > LIVE_REQUEST_LIMIT or live_spend > LIVE_SPEND_LIMIT_MICROS:
                    return GovernanceError("live_session_limit", 429)
                session_id = str(live["session_id"])
                live_update = {
                    "requests_charged": live_requests,
                    "spend_charged_micros": live_spend,
                    "reserved_spend_micros": int(live.get("reserved_spend_micros", 0))
                    + cost_reservation,
                    "updated_at": now,
                }
                if live_requests == LIVE_REQUEST_LIMIT or live_spend == LIVE_SPEND_LIMIT_MICROS:
                    live_update.update({"state": "cap_reached", "closed_at": now})
                transaction.update(live_reference, live_update)

            reservation = Reservation(
                request_id,
                tenant_id,
                key_id,
                trace_id,
                window_id,
                token_reservation,
                cost_reservation,
                len(live_targets),
                session_id,
                bucket_shard,
            )
            transaction.set(
                bucket_reference,
                {
                    "window_id": window_id,
                    "bucket_shard": bucket_shard,
                    "bucket_shards": bucket_shards,
                    "request_charged": request_charged + 1,
                    "token_charged": token_charged + token_reservation,
                    "cost_charged_micros": cost_charged + cost_reservation,
                    "reserved_tokens": int(bucket.get("reserved_tokens", 0)) + token_reservation,
                    "reserved_cost_micros": int(bucket.get("reserved_cost_micros", 0)) + cost_reservation,
                    "updated_at": now,
                    "delete_at": now + USAGE_BUCKET_TTL,
                },
                merge=True,
            )
            transaction.create(
                reservation_reference,
                {
                    **asdict(reservation),
                    "status": "reserved",
                    "created_at": now,
                    "expires_at": now + RESERVATION_TTL,
                },
            )
            return reservation

        result = admit(transaction)
        if isinstance(result, GovernanceError):
            raise result
        return result

    def reconcile(
        self,
        reservation: Reservation,
        receipt: ReceiptMetadata,
        *,
        selected_target: Target | None,
        live_attempts: int,
        terminal_certain: bool,
    ) -> dict[str, Any]:
        now = self._clock()
        reservation_reference = self._reservation_reference(reservation.tenant_id, reservation.request_id)
        bucket_reference = self._bucket_reference(
            reservation.tenant_id, reservation.window_id, reservation.bucket_shard
        )
        policy_reference = self._policy_reference(reservation.tenant_id)
        receipt_reference = self._receipt_reference(reservation.tenant_id, reservation.request_id)
        live_reference = self._live_reference(reservation.tenant_id)
        transaction = self.client.transaction(max_attempts=GOVERNANCE_TRANSACTION_MAX_ATTEMPTS)

        @firestore.transactional
        def finish(transaction: firestore.Transaction) -> tuple[dict[str, Any], bool]:
            reservation_snapshot = reservation_reference.get(transaction=transaction)
            bucket_snapshot = bucket_reference.get(transaction=transaction)
            policy_snapshot = policy_reference.get(transaction=transaction)
            live_snapshot = (
                live_reference.get(transaction=transaction)
                if reservation.live_session_id is not None
                else None
            )
            receipt_snapshot = receipt_reference.get(transaction=transaction)
            document = reservation_snapshot.to_dict() or {}
            if not reservation_snapshot.exists:
                raise GovernanceError("reservation_missing", 503)
            if receipt_snapshot.exists:
                return receipt_snapshot.to_dict() or {}, False
            bucket = bucket_snapshot.to_dict() or {}
            policy = policy_snapshot.to_dict() or {}
            actual_tokens = receipt.prompt_tokens + receipt.completion_tokens
            actual_cost = (
                self._cost_micros(
                    policy,
                    selected_target,
                    receipt.prompt_tokens,
                    receipt.completion_tokens,
                    receipt.cached_input_tokens,
                )
                if selected_target is not None and selected_target.provider != "simulator"
                else 0
            )
            accounting_status = "reconciled" if terminal_certain else "reserved_uncertain"
            if terminal_certain:
                transaction.update(
                    bucket_reference,
                    {
                        "token_charged": int(bucket.get("token_charged", 0)) - reservation.token_reservation + actual_tokens,
                        "cost_charged_micros": int(bucket.get("cost_charged_micros", 0))
                        - reservation.cost_reservation_micros
                        + actual_cost,
                        "reserved_tokens": int(bucket.get("reserved_tokens", 0)) - reservation.token_reservation,
                        "reserved_cost_micros": int(bucket.get("reserved_cost_micros", 0))
                        - reservation.cost_reservation_micros,
                        "reconciled_tokens": int(bucket.get("reconciled_tokens", 0)) + actual_tokens,
                        "reconciled_cost_micros": int(bucket.get("reconciled_cost_micros", 0)) + actual_cost,
                        "updated_at": now,
                    },
                )
                if reservation.live_session_id and live_snapshot is not None and live_snapshot.exists:
                    live = live_snapshot.to_dict() or {}
                    if live.get("session_id") == reservation.live_session_id:
                        transaction.update(
                            live_reference,
                            {
                                "requests_charged": int(live.get("requests_charged", 0))
                                - reservation.live_request_reservation
                                + live_attempts,
                                "spend_charged_micros": int(live.get("spend_charged_micros", 0))
                                - reservation.cost_reservation_micros
                                + actual_cost,
                                "reserved_spend_micros": int(live.get("reserved_spend_micros", 0))
                                - reservation.cost_reservation_micros,
                                "reconciled_spend_micros": int(live.get("reconciled_spend_micros", 0))
                                + actual_cost,
                                "updated_at": now,
                            },
                        )
            receipt_document = {
                **asdict(receipt),
                "attempted_targets": list(receipt.attempted_targets),
                "cost_usd_micros": actual_cost if terminal_certain else reservation.cost_reservation_micros,
                "price_table_version": policy.get("price_table_version"),
                "accounting_status": accounting_status,
                "live_session_id": reservation.live_session_id,
                "expires_at": now + RECEIPT_TTL,
            }
            transaction.create(receipt_reference, receipt_document)
            transaction.update(
                reservation_reference,
                {
                    "status": accounting_status,
                    "actual_tokens": actual_tokens if terminal_certain else None,
                    "actual_cost_micros": actual_cost if terminal_certain else None,
                    "reconciled_at": now,
                },
            )
            return receipt_document, True

        document, created = finish(transaction)
        if created:
            self.telemetry.emit(receipt, int(document["cost_usd_micros"]), str(document["accounting_status"]))
        return document

    def arm_live_session(self, actor: UserPrincipal) -> LiveSession:
        self._require_owner(actor)
        now = self._clock()
        reference = self._live_reference(actor.tenant_id)
        policy_reference = self._policy_reference(actor.tenant_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def arm(transaction: firestore.Transaction) -> LiveSession | GovernanceError:
            snapshot = reference.get(transaction=transaction)
            policy_snapshot = policy_reference.get(transaction=transaction)
            current = snapshot.to_dict() or {}
            if snapshot.exists and current.get("state") == "active" and now < current["expires_at"]:
                return GovernanceError("live_session_active", 409)
            policy = policy_snapshot.to_dict() or {}
            prices = policy.get("prices") or {}
            updated_at = policy.get("updated_at")
            providers = {str(key).split(":", 1)[0] for key in prices}
            if (
                not policy_snapshot.exists
                or not policy.get("price_table_version")
                or not isinstance(updated_at, datetime)
                or now - updated_at > PRICE_TABLE_MAX_AGE
                or not {"openai", "anthropic"}.issubset(providers)
            ):
                return GovernanceError("price_unavailable", 503)
            session = LiveSession(
                session_id=secrets.token_hex(12),
                tenant_id=actor.tenant_id,
                owner_email=actor.email,
                state="active",
                armed_at=now,
                expires_at=now + LIVE_DURATION,
                request_limit=LIVE_REQUEST_LIMIT,
                requests_charged=0,
                spend_limit_micros=LIVE_SPEND_LIMIT_MICROS,
                spend_charged_micros=0,
                reserved_spend_micros=0,
                reconciled_spend_micros=0,
            )
            transaction.set(
                reference,
                {
                    **asdict(session),
                    "closed_at": None,
                    "updated_at": now,
                    "delete_at": now + LIVE_RECORD_TTL,
                },
            )
            return session

        result = arm(transaction)
        if isinstance(result, GovernanceError):
            raise result
        return result

    def stop_live_session(self, actor: UserPrincipal) -> None:
        self._require_owner(actor)
        reference = self._live_reference(actor.tenant_id)
        transaction = self.client.transaction()
        now = self._clock()

        @firestore.transactional
        def stop(transaction: firestore.Transaction) -> GovernanceError | None:
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists or (snapshot.to_dict() or {}).get("state") != "active":
                return GovernanceError("live_session_inactive", 409)
            transaction.update(reference, {"state": "stopped", "closed_at": now, "updated_at": now})
            return None

        result = stop(transaction)
        if result is not None:
            raise result

    def status(self, tenant_id: str) -> dict[str, Any]:
        policy = self._policy_reference(tenant_id).get()
        live = self._live_reference(tenant_id).get()
        policy_document = policy.to_dict() if policy.exists else None
        live_document = live.to_dict() if live.exists else None
        if policy_document is not None:
            policy_document.pop("updated_by", None)
        if live_document is not None:
            live_document.pop("owner_email", None)
            live_document.setdefault("reserved_spend_micros", 0)
            live_document.setdefault("reconciled_spend_micros", 0)
        return {
            "policy": policy_document,
            "live_session": live_document,
        }

    def eligible_live_target_ids(
        self,
        tenant_id: str,
        targets: Sequence[Target],
        *,
        required_price_table_version: str | None = None,
    ) -> set[str]:
        now = self._clock()
        policy_snapshot = self._policy_reference(tenant_id).get()
        live_snapshot = self._live_reference(tenant_id).get()
        policy = policy_snapshot.to_dict() or {}
        live = live_snapshot.to_dict() or {}
        if (
            not policy_snapshot.exists
            or not live_snapshot.exists
            or live.get("state") != "active"
            or now >= live.get("expires_at", now)
            or (
                required_price_table_version is not None
                and policy.get("price_table_version") != required_price_table_version
            )
        ):
            return set()
        prices = policy.get("prices") or {}
        return {
            target.id
            for target in targets
            if target.provider != "simulator" and f"{target.provider}:{target.model}" in prices
        }

    def bucket(self, tenant_id: str, window_id: str) -> dict[str, Any] | None:
        policy = self._policy_reference(tenant_id).get().to_dict() or {}
        bucket_shards = int(policy.get("usage_bucket_shards", 1))
        documents = [
            snapshot.to_dict() or {}
            for snapshot in (
                self._bucket_reference(tenant_id, window_id, shard).get()
                for shard in range(bucket_shards)
            )
            if snapshot.exists
        ]
        if not documents:
            return None
        totals: dict[str, Any] = {"window_id": window_id, "bucket_shards": bucket_shards}
        for field in (
            "request_charged",
            "token_charged",
            "cost_charged_micros",
            "reserved_tokens",
            "reserved_cost_micros",
            "reconciled_tokens",
            "reconciled_cost_micros",
        ):
            totals[field] = sum(int(document.get(field, 0)) for document in documents)
        totals["updated_at"] = max(
            (document.get("updated_at") for document in documents if document.get("updated_at")),
            default=None,
        )
        totals["delete_at"] = max(
            (document.get("delete_at") for document in documents if document.get("delete_at")),
            default=None,
        )
        return totals

    def receipt(self, tenant_id: str, request_id: str) -> dict[str, Any] | None:
        snapshot = self._receipt_reference(tenant_id, request_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    @staticmethod
    def _cost_micros(
        policy: Mapping[str, Any],
        target: Target,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> int:
        rates = policy["prices"][f"{target.provider}:{target.model}"]
        cached = min(input_tokens, cached_input_tokens)
        uncached = input_tokens - cached
        return math.ceil(
            (
                uncached * int(rates["input"])
                + cached * int(rates.get("cached_input", rates["input"]))
                + output_tokens * int(rates["output"])
            )
            / 1_000_000
        )

    @staticmethod
    def _reservation_from_document(document: Mapping[str, Any]) -> Reservation:
        return Reservation(
            request_id=str(document["request_id"]),
            tenant_id=str(document["tenant_id"]),
            key_id=str(document["key_id"]),
            trace_id=str(document["trace_id"]),
            window_id=str(document["window_id"]),
            token_reservation=int(document["token_reservation"]),
            cost_reservation_micros=int(document["cost_reservation_micros"]),
            live_request_reservation=int(document["live_request_reservation"]),
            live_session_id=document.get("live_session_id"),
            bucket_shard=int(document.get("bucket_shard", 0)),
        )

    @staticmethod
    def _partition_limit(total: int, shard: int, shards: int) -> int:
        quotient, remainder = divmod(total, shards)
        return quotient + (1 if shard < remainder else 0)

    def _policy_reference(self, tenant_id: str):
        return self.client.collection("tenants").document(tenant_id).collection("policies").document("current")

    def _bucket_reference(self, tenant_id: str, window_id: str, shard: int = 0):
        suffix = "" if shard == 0 else f"-shard-{shard:02d}"
        return (
            self.client.collection("tenants")
            .document(tenant_id)
            .collection("usage_buckets")
            .document(window_id + suffix)
        )

    def _reservation_reference(self, tenant_id: str, request_id: str):
        return self.client.collection("tenants").document(tenant_id).collection("reservations").document(request_id)

    def _live_reference(self, tenant_id: str):
        return self.client.collection("tenants").document(tenant_id).collection("live_sessions").document("active")

    def _receipt_reference(self, tenant_id: str, request_id: str):
        return self.client.collection("tenants").document(tenant_id).collection("receipts").document(request_id)

    @staticmethod
    def _require_owner(actor: UserPrincipal) -> None:
        if actor.role != "owner":
            raise AuthorizationError("owner role required")
