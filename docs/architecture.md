# Architecture

## Services

The workbench has three independently deployable processes:

- `control_plane` serves the React console, validates IAP identity, enforces application roles, and proxies committed scenarios without exposing its gateway key.
- `data_plane` implements the chat contract, authenticates gateway keys, performs admission, selects a route, calls one provider at a time, and reconciles metadata.
- `provider_simulator` returns deterministic responses for local and hosted demonstrations. Its invocation boundary requires an exact service identity in hosted environments.

Firestore stores users, roles, key digests, policies, routes, quota and budget counters, bounded live-session state, and metadata-only receipts. Prompt and response text has no durable model.

## Request path

1. The console accepts only a scenario ID from the browser.
2. The control plane resolves the corresponding committed fixture and attaches a server-held gateway key.
3. The data plane checks identity, tenant, scenario, policy, quota, budget, price, and live-session state before provider access.
4. Routing is stable for a request ID. Fallback is allowed only before a response stream is exposed.
5. Terminal usage reconciles the reservation. Uncertain outcomes retain the conservative charge.

## Runtime modes

- Local simulator: Firestore emulator and deterministic provider responses.
- Private workbench: simulator-only hosted traffic with invited console access.
- Live validation: implemented but disabled; it requires a bounded owner session, an eligible route, current pricing, and lazy secret resolution after admission.

## Failure posture

Missing identity, configuration, policy, pricing, quota, budget, or state fails closed. A live-provider failure can fall back only before streaming starts. Partial streams terminate without mixing providers. Simulator mode remains the recovery state.

The companion [Mermaid source](../architecture/system.mmd) shows the service boundaries.
