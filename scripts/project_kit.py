#!/usr/bin/env python3
"""Validate and fingerprint the private gateway project."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GRAPH_SIGNATURE = ROOT / ".project" / "graph-source.sha256"
REQUIRED_FILES = {
    ".env.example",
    ".dockerignore",
    "AGENTS.md",
    "DESIGN.md",
    "Dockerfile",
    "PRODUCT.md",
    "PROJECT.md",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".project/approvals.yml",
    ".project/artifacts/m7/container-console.sarif.json",
    ".project/artifacts/m7/container-data-plane.sarif.json",
    ".project/artifacts/m7/container-provider-simulator.sarif.json",
    ".project/artifacts/m7/firestore-load-mitigated-run-1.json",
    ".project/artifacts/m7/firestore-load-mitigated-run-2.json",
    ".project/artifacts/m7/firestore-load-pre-mitigation-failure.json",
    ".project/artifacts/m7/python-dependency-audit.json",
    ".project/artifacts/m7/verification-summary.json",
    ".project/architecture.md",
    ".project/evidence.yml",
    ".project/handoff.md",
    ".project/milestones.yml",
    ".project/state.md",
    "architecture/system.mmd",
    "docs/api-contract.md",
    "docs/data-policy.md",
    "docs/evaluation.md",
    "docs/m9-activation-plan.md",
    "docs/operations.md",
    "docs/security.md",
    "console/index.html",
    "console/package-lock.json",
    "console/package.json",
    "console/playwright.config.ts",
    "console/src/App.tsx",
    "console/src/main.tsx",
    "console/src/styles.css",
    "console/tests/workbench.spec.ts",
    "console/tsconfig.app.json",
    "console/tsconfig.json",
    "console/tsconfig.node.json",
    "console/vite.config.ts",
    "scripts/project_kit.py",
    "scripts/run_m7_load.py",
    "scenarios/catalog.json",
    "services/catalog.py",
    "services/__init__.py",
    "services/data_plane.py",
    "services/control_plane.py",
    "services/governance.py",
    "services/identity.py",
    "services/providers.py",
    "services/provider_simulator.py",
    "services/routing.py",
    "services/service_identity.py",
    "tests/test_m2_protocol.py",
    "tests/test_m3_identity.py",
    "tests/test_m4_routing_adapters.py",
    "tests/test_m5_governance.py",
    "tests/test_m6_workbench.py",
    "tests/test_m7_security_privacy.py",
}
EXPECTED_DOCS = {
    "api-contract.md",
    "data-policy.md",
    "evaluation.md",
    "m9-activation-plan.md",
    "operations.md",
    "security.md",
}
LEGACY_FILES = {".github/workflows/quality.yml", "compose.yaml", "requirements.txt"}
LEGACY_DIRECTORIES = {"app", "evaluation"}


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = {
        ROOT / name
        for name in result.stdout.split("\0")
        if name and name != ".project/graph-source.sha256"
    }
    return sorted((path for path in files if path.is_file()), key=lambda path: path.relative_to(ROOT).as_posix())


def source_signature() -> str:
    digest = hashlib.sha256()
    for path in repository_files():
        relative = path.relative_to(ROOT).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def milestone_status(markdown: str, milestone_id: str) -> str:
    match = re.search(
        rf"(?ms)^  - id: {re.escape(milestone_id)}\s*$"
        rf"(?:(?!^  - id: ).)*?^    status: ([a-z_-]+)\s*$",
        markdown,
    )
    if not match:
        raise ValueError(f"Missing status for {milestone_id}")
    return match.group(1)


def validate_portfolio_lifecycle(milestones: str) -> None:
    case_study = ROOT / "CASE-STUDY.md"
    portfolio = ROOT / "portfolio" / "project.json"
    m10_complete = milestone_status(milestones, "M10") == "complete"
    if not m10_complete:
        premature = [path.relative_to(ROOT).as_posix() for path in (case_study, portfolio) if path.exists()]
        if premature:
            raise ValueError("M10-only publication records exist early: " + ", ".join(premature))
        return
    missing = [path.relative_to(ROOT).as_posix() for path in (case_study, portfolio) if not path.is_file()]
    if missing:
        raise ValueError("M10 requires publication records: " + ", ".join(missing))
    manifest = json.loads(portfolio.read_text(encoding="utf-8"))
    for field in ("slug", "title", "githubUrl", "deployment", "evidence"):
        if not manifest.get(field):
            raise ValueError(f"portfolio/project.json is missing {field}")


def check(_: argparse.Namespace) -> None:
    missing = sorted(path for path in REQUIRED_FILES if not (ROOT / path).is_file())
    if missing:
        raise ValueError("Missing project files: " + ", ".join(missing))

    stale = sorted(path for path in LEGACY_FILES if (ROOT / path).exists())
    for directory in LEGACY_DIRECTORIES:
        root = ROOT / directory
        if root.exists() and any(path.is_file() for path in root.rglob("*")):
            stale.append(directory + "/")
    if stale:
        raise ValueError("Legacy reset artifacts remain: " + ", ".join(stale))

    docs = {path.relative_to(ROOT / "docs").as_posix() for path in (ROOT / "docs").rglob("*") if path.is_file()}
    if docs != EXPECTED_DOCS:
        raise ValueError(f"docs/ must contain exactly {sorted(EXPECTED_DOCS)}; found {sorted(docs)}")

    milestones = (ROOT / ".project" / "milestones.yml").read_text(encoding="utf-8")
    statuses = [milestone_status(milestones, f"M{number}") for number in range(11)]
    if any(status not in {"complete", "pending"} for status in statuses):
        raise ValueError("Milestone statuses must be complete or pending")
    first_pending = next((index for index, status in enumerate(statuses) if status == "pending"), len(statuses))
    if any(status == "complete" for status in statuses[first_pending:]):
        raise ValueError("Milestones must complete in order")
    validate_portfolio_lifecycle(milestones)

    if not GRAPH_SIGNATURE.is_file():
        raise ValueError("Missing .project/graph-source.sha256; run graph-sync")
    recorded = GRAPH_SIGNATURE.read_text(encoding="utf-8").strip()
    current = source_signature()
    if recorded != current:
        raise ValueError("Project graph signature is stale; run graph-sync")
    print("Project records: pass")


def graph_sync(_: argparse.Namespace) -> None:
    GRAPH_SIGNATURE.write_text(source_signature() + "\n", encoding="utf-8")
    print(f"Project graph signature: {GRAPH_SIGNATURE.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="project-kit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.set_defaults(handler=check)
    graph_parser = subparsers.add_parser("graph-sync")
    graph_parser.set_defaults(handler=graph_sync)
    args = parser.parse_args()
    try:
        args.handler(args)
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"project-kit: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
