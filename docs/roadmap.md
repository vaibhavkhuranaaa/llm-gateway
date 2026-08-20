# Roadmap

## Live-provider validation

The implementation is locally complete, but external validation remains blocked on:

- confirmed ownership and access for the exact provider accounts, projects, and workspaces;
- same-session review of model availability, prices, limits, retention, and training controls;
- exact authorization for immutable images, narrowly scoped secrets and IAM, temporary outbound networking, a 14-attempt synthetic rehearsal, and a fixed spend ceiling;
- live verification of protocol shapes, fallback, accounting, privacy boundaries, and role restrictions;
- revocation and deletion of temporary credentials and network access, followed by proof that simulator-only recovery still works.

No provider authentication, paid request, or live-provider teardown is authorized by this release.

## Future case study

The source release and simulator deployment do not establish live-provider behavior. A later case study remains blocked on:

- a case study and portfolio manifest derived only from verified live evidence;
- same-session provider and cloud evidence;
- final reconciliation of protocol, accounting, privacy, fallback, teardown, and recovery results.

Until those checks pass, no live-provider or production-readiness claim should be made. The private workbench remains simulator-only.
