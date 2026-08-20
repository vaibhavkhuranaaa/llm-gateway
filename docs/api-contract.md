# API contract

## Supported surface

The data plane exposes `POST /v1/chat/completions` with the following subset:

- non-streaming and SSE responses;
- text messages;
- tool definitions and tool-call results;
- strict JSON-schema response formats;
- normalized finish reasons and usage;
- gateway request and trace identifiers.

Embeddings, Responses API, images, audio, realtime, batches, file upload, and arbitrary remote tools are outside this release.

## Authentication and tenancy

Every application route except health requires a bearer gateway key. The stored record contains only a prefix and peppered HMAC digest. Keys are tenant-bound, scoped, expiring, revocable, and revealed once at issuance. Authentication compares digests in constant time and coalesces last-used writes.

Console routes also require a valid IAP identity and an active application role. The server enforces owner and demo-operator permissions independently of what the browser renders.

## Hosted request boundary

Hosted requests must match one committed scenario exactly after canonicalization. The console sends only the scenario ID; the control plane resolves the full payload on the server. Unknown or altered input is rejected before routing.

## Routing and adapters

Weighted selection is deterministic for a request ID. Each route has an explicit ordered fallback chain. Fallback is allowed only when the selected upstream fails before a response stream is exposed. Once streaming begins, a later provider failure produces one terminal gateway error and `[DONE]`; output from different providers is never joined.

Adapters normalize OpenAI chat, Anthropic messages, and the local simulator into the same contract. Unsupported semantic controls make a target ineligible rather than being silently discarded.

## Admission and accounting

Admission reserves requests, estimated input and output tokens, and the maximum possible route cost before provider access. The live-provider price table is versioned and must match the configured target. Terminal reported usage reconciles a certain single attempt. Missing usage, fallback, partial output, disconnect, or ambiguous completion retains the reservation as uncertain.

Hourly quota and budget envelopes can be partitioned across request-ID-selected Firestore documents. Each partition owns a disjoint integer share, so concurrent transactions cannot increase the configured global cap.

## Live-provider boundary

Live targets remain ineligible without an owner-opened session. A session closes at the first of 30 minutes, 20 provider attempts, or USD 1 of reserved spend. Secret resolution occurs only after all identity, scenario, policy, quota, budget, pricing, session, and attempt-reservation checks pass.

The planned live rehearsal is limited to the same committed non-streaming, SSE, tool, and strict-schema shapes. It does not change the public request or response contract. See [roadmap](roadmap.md).
