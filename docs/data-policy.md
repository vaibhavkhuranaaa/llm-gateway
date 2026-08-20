# Data policy

## Allowed content

Hosted traffic may use only the fictional prompts, tools, schemas, and expected simulator responses in `scenarios/catalog.json`. Each scenario carries a fictional-data attestation and a checksum over its request and expected response.

The workbench does not accept customer conversations, production telemetry, personal or regulated data, credentials, proprietary material, third-party benchmarks, uploaded files, or arbitrary prompts. Provider-returned tool calls are data; the gateway never executes them.

## Content lifecycle

Prompt and response text may exist only in the committed catalog, process memory during a request, and the current browser component state. It is excluded from Firestore models, logs, traces, metrics, error reports, analytics, browser storage, snapshots, and generated artifacts.

The browser sends only a scenario ID to the control plane. Refresh, navigation, sign-out, or another run clears the rendered response.

## Retained metadata

Allowed metadata includes request, trace, tenant, key, scenario, policy, route, provider, model, attempt, outcome, timing, token, cost, quota, budget, and live-session identifiers or counters. Configuration records may also contain roles, key status, route weights, limits, prices, and expiry timestamps.

Raw gateway keys, authorization headers, provider credentials, request or response bodies, tool arguments, schemas, and content-derived hashes are prohibited in durable storage.

Receipts and reservations use seven-day expiry, usage buckets use 35-day expiry, and live-session records expire after 24 hours. Operational log retention is 30 days. Configuration remains until it is replaced or revoked.

## External providers

A live API provider is an external processor and may retain synthetic request or response content under its own account settings and terms. Before any live request, the owner must review the exact account, workspace or project, model availability, training controls, retention settings, prices, limits, and key permissions in the same session. Zero Data Retention is not assumed.

Live validation, if authorized later, remains limited to committed fixtures and metadata-only evidence. It does not permit provider-console conversations, files, batches, remote tools, prompt-caching configuration, or another dataset.

## Enforcement and response

Hosted requests are canonicalized and compared with the catalog before routing. Telemetry uses typed allowlists. Provider errors are normalized before logging, and canary tests check every project-controlled durable destination.

If prohibited content is retained, stop affected traffic, disable live targets, preserve only identifiers needed for investigation, restrict access, remove the content through an exact-target procedure, rotate exposed credentials, and rerun the privacy checks before restoration. Never copy leaked content into tickets or reports.
