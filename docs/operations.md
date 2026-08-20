# Operations

## Configuration

`.env.example` lists runtime variable names only. Secret values belong in the runtime secret store and must not be committed, passed on command lines, or included in diagnostics.

The applications fail closed when Firestore, gateway-key pepper, IAP audience, route, price, or live-session configuration is absent. Simulator mode does not require provider credentials.

## Local runtime

Run the Firestore emulator before the Python suite or local services. Use separate Uvicorn processes for:

```text
services.provider_simulator:app
services.data_plane:app
services.control_plane:app
```

The Dockerfile exposes `data-plane`, `provider-simulator`, and `console` targets. Each final image runs as UID and GID 65532 on port 8080.

```bash
docker build --target data-plane -t llm-gateway-data-plane:local .
docker build --target provider-simulator -t llm-gateway-simulator:local .
docker build --target console -t llm-gateway-console:local .
```

## Failure handling

| Failure | Behavior |
| --- | --- |
| Firestore unavailable or ambiguous | Refuse admission before provider access |
| Secret unavailable | Keep simulator eligible; disable the live target |
| Simulator unavailable | Apply pre-stream fallback or return a safe 503/504 |
| Provider timeout, 429, or 5xx | Apply the configured pre-stream fallback |
| Failure after streaming begins | End the stream without contacting another provider |
| Missing usage or client disconnect | Retain the conservative reservation |
| Identity or role failure | Refuse the console operation |
| Missing or stale price | Refuse live-session activation |
| Privacy canary retained | Stop traffic and follow the data-policy response |

## Backup and recovery

Export versioned metadata before destructive configuration changes. Gateway keys cannot be recovered from backups and must be rotated if key metadata or pepper integrity is uncertain.

Recovery order is identity and roles, route and policy configuration, key metadata, quota and budget state, then simulator traffic. Live sessions are never restored; they reopen as disabled.

## Deployment boundary

This source revision is not deployed. A source push must run CI only. Deployment, cloud configuration, provider access, network changes, credentials, and paid requests require a separate exact-target review and are intentionally outside this repository cleanup.
