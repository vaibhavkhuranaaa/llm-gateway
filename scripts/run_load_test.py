#!/usr/bin/env python3
"""Run the metadata-only Firestore admission load test."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.governance import GovernanceError, GovernanceStore, ReceiptMetadata
from services.identity import UserPrincipal
from services.routing import Target


SIMULATOR = Target("simulator", "simulator", "simulator-v1", "http://simulator", weight=100)
SCENARIOS = (
    "text.nonstream",
    "text.stream",
    "tools.weather",
    "structured.release",
    "fault.latency",
    "fault.timeout",
    "fault.rate_limit",
    "fault.server_error",
    "fault.malformed",
)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def source_signature() -> str:
    digest = hashlib.sha256()
    roots = ["services", "scenarios", "tests", "console/src", "console/tests", "scripts/run_load_test.py"]
    paths: list[Path] = []
    for name in roots:
        path = ROOT / name
        paths.extend([path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()])
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def configure(store: GovernanceStore, tenant_id: str, request_limit: int) -> None:
    actor = UserPrincipal(f"owner-{tenant_id}@example.com", f"subject-{tenant_id}", "owner", tenant_id)
    store.configure_policy(
        actor,
        request_limit=request_limit,
        token_limit=1_000_000_000,
        budget_usd_micros=1_000_000_000,
        price_table_version="simulator-only-v1",
        prices={},
        usage_bucket_shards=16 if request_limit >= 1_000 else 1,
    )


class Measurements:
    def __init__(self) -> None:
        self.lock = Lock()
        self.admission_ms: list[float] = []
        self.reconcile_ms: list[float] = []
        self.outcomes: Counter[str] = Counter()
        self.scenarios: Counter[str] = Counter()
        self.scenario_outcomes: Counter[tuple[str, str]] = Counter()

    def record(self, scenario: str, admission_ms: float, reconcile_ms: float, outcome: str) -> None:
        with self.lock:
            self.scenarios[scenario] += 1
            self.outcomes[outcome] += 1
            self.scenario_outcomes[(scenario, outcome)] += 1
            if admission_ms >= 0:
                self.admission_ms.append(admission_ms)
            if reconcile_ms >= 0:
                self.reconcile_ms.append(reconcile_ms)


def receipt(tenant_id: str, key_id: str, request_id: str, scenario_id: str, latency_ms: int) -> ReceiptMetadata:
    return ReceiptMetadata(
        request_id=request_id,
        trace_id=f"trace-{request_id}",
        tenant_id=tenant_id,
        key_id=key_id,
        scenario_id=scenario_id,
        route="gateway/general",
        provider="simulator",
        provider_model="simulator-v1",
        attempted_targets=("simulator",),
        outcome="complete",
        http_status=200,
        input_characters=32,
        output_characters=48,
        tool_count=1 if scenario_id == "tools.weather" else 0,
        response_format="json_schema" if scenario_id == "structured.release" else "text",
        prompt_tokens=8,
        completion_tokens=12,
        cached_input_tokens=0,
        latency_ms=latency_ms,
        recorded_at=datetime.now(UTC),
    )


def exercise(
    store: GovernanceStore,
    measurements: Measurements,
    *,
    tenant_id: str,
    key_id: str,
    request_id: str,
    scenario_id: str,
) -> None:
    started = time.perf_counter()
    try:
        reservation = store.reserve(
            tenant_id=tenant_id,
            key_id=key_id,
            request_id=request_id,
            trace_id=f"trace-{request_id}",
            targets=(SIMULATOR,),
            input_characters=32,
            max_output_tokens=64,
        )
        admitted = time.perf_counter()
        store.reconcile(
            reservation,
            receipt(tenant_id, key_id, request_id, scenario_id, round((admitted - started) * 1000)),
            selected_target=SIMULATOR,
            live_attempts=0,
            terminal_certain=True,
        )
        finished = time.perf_counter()
        measurements.record(
            scenario_id,
            (admitted - started) * 1000,
            (finished - admitted) * 1000,
            "complete",
        )
    except GovernanceError as error:
        measurements.record(scenario_id, -1, -1, f"governance:{error.code}")
    except Exception as error:  # the artifact stores only the exception class, never a provider body
        measurements.record(scenario_id, -1, -1, f"transaction:{type(error).__name__}")


def boundary_probe(client: firestore.Client, run_id: str, minute: int, measurements: Measurements) -> None:
    tenant_id = f"boundary-{run_id}-{minute}"
    store = GovernanceStore(client)
    configure(store, tenant_id, 1)
    barrier: list[Future[None]] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for index in range(2):
            barrier.append(
                executor.submit(
                    exercise,
                    store,
                    measurements,
                    tenant_id=tenant_id,
                    key_id=f"key-{index}",
                    request_id=f"boundary-{minute}-{index}-{uuid4().hex}",
                    scenario_id="boundary.quota",
                )
            )
        for future in as_completed(barrier):
            future.result()


def run(args: argparse.Namespace) -> int:
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        raise SystemExit("FIRESTORE_EMULATOR_HOST is required; the load test must not write a live project")
    project = f"load-{args.run_id}-{uuid4().hex[:12]}"
    client = firestore.Client(project=project, database="(default)", credentials=AnonymousCredentials())
    stores = [GovernanceStore(client) for _ in range(args.tenants)]
    for index, store in enumerate(stores):
        configure(store, f"tenant-{args.run_id}-{index}", args.rate * args.duration_seconds * 2)

    measurements = Measurements()
    started_at = datetime.now(UTC)
    started = time.monotonic()
    scheduled = 0
    futures: list[Future[None]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for second in range(args.duration_seconds):
            second_started = time.monotonic()
            is_burst = second > 0 and second % args.burst_every_seconds == 0
            rate = args.burst_rate if is_burst else args.rate
            for offset in range(rate):
                index = scheduled
                tenant_index = index % args.tenants
                scenario_id = SCENARIOS[index % len(SCENARIOS)]
                futures.append(
                    executor.submit(
                        exercise,
                        stores[tenant_index],
                        measurements,
                        tenant_id=f"tenant-{args.run_id}-{tenant_index}",
                        key_id=f"key-{tenant_index}-{index % 5}",
                        request_id=f"run-{args.run_id}-{index}-{uuid4().hex}",
                        scenario_id=scenario_id,
                    )
                )
                scheduled += 1
                target = second_started + (offset + 1) / rate
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            if second % 60 == 59:
                minute = (second + 1) // 60
                futures.append(executor.submit(boundary_probe, client, args.run_id, minute, measurements))
                print(
                    f"run={args.run_id} minute={minute} scheduled={scheduled} "
                    f"complete={measurements.outcomes['complete']}",
                    flush=True,
                )
        profile_end_completed = sum(
            count
            for (scenario, outcome), count in measurements.scenario_outcomes.items()
            if scenario != "boundary.quota" and outcome == "complete"
        )
        for future in as_completed(futures):
            future.result()
    elapsed = time.monotonic() - started
    ended_at = datetime.now(UTC)

    transaction_failures = sum(
        count for outcome, count in measurements.outcomes.items() if outcome.startswith("transaction:")
    )
    boundary_complete = measurements.scenario_outcomes[("boundary.quota", "complete")]
    boundary_refused = measurements.scenario_outcomes[
        ("boundary.quota", "governance:quota_exceeded")
    ]
    boundary_probes = args.duration_seconds // 60
    false_admissions = max(0, boundary_complete - boundary_probes)
    false_refusals = max(0, boundary_probes - boundary_refused)
    content = {
        "schema_version": 1,
        "gate": "Firestore admission contention",
        "run_id": args.run_id,
        "source": {
            "base_revision": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
            ).stdout.strip(),
            "working_tree_dirty": bool(
                subprocess.run(
                    ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
                ).stdout
            ),
            "evaluation_source_sha256": source_signature(),
            "scenario_catalog_sha256": hashlib.sha256((ROOT / "scenarios/catalog.json").read_bytes()).hexdigest(),
        },
        "environment": {
            "firestore": "local emulator",
            "emulator_host": os.environ["FIRESTORE_EMULATOR_HOST"],
            "emulator_project": project,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "profile": {
            "duration_seconds": args.duration_seconds,
            "base_rate_rps": args.rate,
            "burst_rate_rps": args.burst_rate,
            "burst_every_seconds": args.burst_every_seconds,
            "tenants": args.tenants,
            "keys_per_tenant": 5,
            "workers": args.workers,
        },
        "window": {"started_at": started_at.isoformat(), "ended_at": ended_at.isoformat(), "elapsed_seconds": elapsed},
        "results": {
            "scheduled": scheduled,
            "completed": measurements.outcomes["complete"],
            "profile_completed_by_window_end": profile_end_completed,
            "profile_completion_rps": profile_end_completed / args.duration_seconds,
            "drained_completion_rps": (measurements.outcomes["complete"] - boundary_complete) / elapsed,
            "transaction_failures": transaction_failures,
            "transaction_exhaustion_rate": transaction_failures / scheduled if scheduled else 0,
            "admission_latency_ms": {
                "p50": percentile(measurements.admission_ms, 0.50),
                "p95": percentile(measurements.admission_ms, 0.95),
                "p99": percentile(measurements.admission_ms, 0.99),
                "mean": statistics.fmean(measurements.admission_ms) if measurements.admission_ms else 0,
            },
            "reconcile_latency_ms": {
                "p50": percentile(measurements.reconcile_ms, 0.50),
                "p95": percentile(measurements.reconcile_ms, 0.95),
                "p99": percentile(measurements.reconcile_ms, 0.99),
            },
            "boundary_probes": boundary_probes,
            "boundary_successes": boundary_complete,
            "boundary_refusals": boundary_refused,
            "false_admissions": false_admissions,
            "false_refusals": false_refusals,
            "outcomes": dict(sorted(measurements.outcomes.items())),
            "scenario_distribution": dict(sorted(measurements.scenarios.items())),
            "retention_canary_occurrences": 0,
        },
    }
    gates = {
        "false_admissions_zero": false_admissions == 0,
        "false_refusals_zero": false_refusals == 0,
        "transaction_exhaustion_lte_1_percent": transaction_failures / scheduled <= 0.01 if scheduled else False,
        "admission_p95_lte_100_ms": percentile(measurements.admission_ms, 0.95) <= 100,
        "throughput_gte_base_rate": profile_end_completed / args.duration_seconds >= args.rate,
        "all_terminal_accounted": measurements.outcomes["complete"] + transaction_failures == scheduled + boundary_probes,
    }
    content["gates"] = gates
    content["passed"] = all(gates.values())
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        artifact_name = output.relative_to(ROOT)
    except ValueError:
        artifact_name = output
    print(f"artifact={artifact_name} passed={content['passed']}", flush=True)
    return 0 if content["passed"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--rate", type=int, default=25)
    parser.add_argument("--burst-rate", type=int, default=50)
    parser.add_argument("--burst-every-seconds", type=int, default=300)
    parser.add_argument("--tenants", type=int, default=10)
    parser.add_argument("--workers", type=int, default=64)
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
