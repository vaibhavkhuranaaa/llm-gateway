# Private LLM Gateway Workbench

A private release candidate for an OpenAI-compatible chat gateway. It combines deterministic routing, ordered fallback, tenant controls, metadata-only accounting, and a small React console built around committed synthetic scenarios.

The repository remains private while live-provider validation and the final case study are incomplete. The hosted simulator release is a separate, older revision; this source tree has not been deployed.

## What it demonstrates

- OpenAI-shaped chat completions with JSON and SSE responses
- tool calls and strict structured output
- request-stable weighted routing with pre-stream fallback
- OpenAI, Anthropic, and deterministic simulator adapters
- gateway keys, IAP identity, tenant roles, quotas, budgets, and bounded live sessions
- metadata-only receipts, token accounting, and cost reconciliation
- a server-mediated console that never exposes gateway credentials or arbitrary prompt entry
- fail-closed live configuration and lazy credential resolution

Hosted traffic is limited to the synthetic fixtures in `scenarios/catalog.json`. Prompt and response text is transient and is not written to Firestore, logs, traces, metrics, browser storage, or test artifacts.

## Architecture

The console, data plane, and provider simulator run as separate services. Firestore holds identity, policy, routing, quota, budget, and receipt metadata. Live provider targets remain disabled unless an owner opens a bounded session and every admission check passes.

See [architecture](docs/architecture.md), [API contract](docs/api-contract.md), [data policy](docs/data-policy.md), and [operations](docs/operations.md) for the boundaries.

## Local verification

Python 3.12, Node 26, Java, the Google Cloud SDK, and `uv` are required.

```bash
uv sync --frozen --all-groups
gcloud beta emulators firestore start \
  --host-port=127.0.0.1:8787 \
  --project=demo-private-gateway
FIRESTORE_EMULATOR_HOST=127.0.0.1:8787 uv run pytest
```

In another shell:

```bash
cd console
npm ci
npm run build
npm run test:e2e
```

The local load harness refuses to run without the Firestore emulator:

```bash
FIRESTORE_EMULATOR_HOST=127.0.0.1:8787 \
  uv run python scripts/run_load_test.py --help
```

The release check currently covers 51 Python tests, the production console build, and 13 Playwright cases with five intentional cross-project skips. Current Python and Node locks have no known dependency advisories. Detailed limits and measured results are in [evaluation](docs/evaluation.md).

## Current boundary

Live-provider credentials, outbound networking, deployment, spend, and publication are outside this release. [Roadmap](docs/roadmap.md) records the remaining validation and publication blockers.

Licensed under the [MIT License](LICENSE).
