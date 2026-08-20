# Evaluation

## Current release checks

The private-workbench source has passed:

| Area | Result | Scope |
| --- | --- | --- |
| Python | 51 tests passed | Firestore emulator, synthetic transports, no provider credentials |
| Console | Production build passed | Locked Node dependencies |
| Browser | 13 Playwright cases passed; five intentional cross-project skips | Desktop and mobile Chromium, keyboard, reflow, forced colors, reduced motion, browser storage |
| Dependencies | No known findings in the current Python and Node locks | Point-in-time advisory databases |
| Privacy | Content canaries absent from inspected metadata models and browser persistence | Project-controlled local destinations |
| Containers | Non-root service targets; release candidates have no high or critical findings | Point-in-time image scans |

The suite covers protocol normalization, streaming, tool calls, strict schemas, identity, roles, key lifecycle, deterministic routing, fallback, quota and budget races, conservative accounting, exact provider origins, simulator service identity, CSRF, browser security headers, and fail-closed live configuration.

## Scale evidence

The original single-document Firestore counter stalled under the target profile. The current 16-partition design completed two fresh local-emulator repetitions of 22,550 scheduled requests with zero transaction failures or incorrect admissions. Admission p95 was 7.194 ms and 6.516 ms.

Five later managed simulator-only runs tested the same profile. The first three exposed capacity limits. After the API ceiling was raised from three to six instances, two fresh repetitions passed at 25.043 and 25.047 admitted requests per second with zero platform refusals. These results describe the verified hosted simulator configuration.

## Privacy evidence

Synthetic canaries cover prompt, response, tool, schema, and provider-error content. The deployed simulator inspection found no canary or gateway-key occurrence in the queried Firestore documents, Cloud Logging entries, or trace lookups. No live provider was called.

## Limits

The evidence does not establish full production readiness, universal provider compatibility, zero vulnerabilities, Zero Data Retention, assistive-technology usability, or live-provider behavior. Live protocol, pricing, retention, accounting, fallback, teardown, and recovery remain blocked on the items in [roadmap](roadmap.md).
