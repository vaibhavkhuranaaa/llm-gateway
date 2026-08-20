# Security design

## Trust boundaries

- Cloud IAP authenticates invited users before the console; Firestore roles independently authorize application actions.
- Bearer gateway keys protect the data plane and are scoped to a tenant and operation set.
- The console resolves committed scenarios and attaches its gateway key on the server.
- The simulator requires the exact configured service identity in hosted environments.
- Live provider origins are fixed in code, and credentials are resolved only after admission during a bounded owner session.

## Identity and keys

IAP assertions require the configured audience and issuer. Roles use normalized exact email matches; domain-only authorization is not supported. Owner and demo-operator checks run on the server for every request.

Gateway keys contain at least 256 random bits. Storage is limited to a non-secret prefix and HMAC-SHA256 digest protected by a runtime pepper. Keys are shown once, compared in constant time, expiring, revocable, and safe for overlapping rotation. Failed authentication is bounded per process and always occurs before provider access.

## Provider boundary

Provider URLs are application-owned constants for the simulator, OpenAI, and Anthropic. User input cannot supply a URL. Redirects cannot expand the origin allowlist. Provider exceptions are mapped to safe codes before telemetry.

Live sessions end at the first of 30 minutes, 20 attempts, or USD 1 of reserved spend. Identity, hosted scenario, policy, quota, budget, price, session, and attempt reservation must all succeed before a provider credential is read. Missing or uncertain state fails closed.

## Content and browser controls

The gateway treats tools and provider output as untrusted data and never executes returned code, commands, or URLs. Structured output is validated against the selected schema. Content cannot influence authorization, database paths, route configuration, or telemetry labels.

Unsafe console methods require the same-origin CSRF marker. Responses include a restrictive content security policy, HSTS, MIME-sniffing protection, frame denial, referrer limits, and permissions policy. The browser keeps response content in component memory only.

## Supply chain

Python and Node dependencies are locked. CI runs tests, builds the console, checks both locks for known advisories, and uses immutable action revisions with read-only repository permissions. Runtime containers use non-root final stages.

See the root security policy for vulnerability reporting and supported versions.
