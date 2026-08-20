# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| Current `main` branch | Yes |
| Historical branches and deployed revisions | No |

The current source is a private release candidate. Support does not imply that this revision is deployed or production-ready.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/vaibhavkhuranaaa/llm-gateway/security/advisories/new).

Include:

- the affected component or endpoint;
- the security impact and required preconditions;
- concise reproduction steps or a minimal local proof;
- suggested remediation, if known.

Do not include credentials, authorization headers, provider content, personal data, or production logs. Reports should receive an acknowledgment within three business days and an initial status within seven business days.

## Security boundaries

Relevant reports include:

- authentication or role bypass;
- cross-tenant access or mutation;
- gateway-key or provider-secret exposure;
- bypass of the committed-scenario restriction;
- server-side request forgery or provider-origin expansion;
- quota, budget, or live-session over-admission;
- durable retention of prompt or response content;
- unsafe tool execution;
- cross-site scripting or CSRF bypass;
- vulnerable dependencies or container packages with a reachable impact.

Provider availability, model quality, unsupported API fields, social engineering, and volumetric denial of service without a control bypass are outside the project’s security scope.

## Security invariants

- Hosted traffic is limited to committed synthetic scenarios.
- Prompt and response text is excluded from durable project-controlled storage.
- Identity, policy, quota, budget, pricing, and session checks complete before live-provider access.
- Provider origins are fixed and redirects cannot expand the allowlist.
- Streaming output from different providers is never combined.
- Live sessions stop at the first of 30 minutes, 20 attempts, or USD 1 of reserved spend.
- Provider-returned tools, commands, code, and URLs are never executed.

## Safe research

Use the local Firestore emulator and synthetic provider transports. Do not test deployed endpoints, cloud resources, provider accounts, or third-party systems without separate written authorization. Avoid actions that could incur cost, affect availability, or retain prohibited content.

## Disclosure

Allow reasonable time for investigation and remediation before public disclosure. Coordinate publication through the private advisory. Never copy exposed secrets or prohibited content into issues, reports, screenshots, or test artifacts.
