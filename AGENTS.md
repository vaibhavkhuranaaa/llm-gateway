# Project delivery rules

## Read first

Read `PROJECT.md`, `.project/architecture.md`, `.project/milestones.yml`, `.project/state.md`, and `.project/handoff.md` before editing. Complete only the first unblocked milestone.

## Rules

- Preserve unrelated work and never overwrite existing files without instruction.
- Hosted traffic uses only committed project-authored synthetic prompts and simulator responses. Never retain prompt or response text.
- Use the smallest credible design; remove stale code and unjustified abstractions.
- Apply `DESIGN.md` to all user-facing work.
- Keep secrets outside source; commit variable names only in `.env.example`.
- Use conventional commits and configured human Git identity. Never add AI/model author or co-author trailers.
- Do not put AI/model names in Git branch names.
- Do not create paid resources, change public visibility, deploy, roll back, or publish without explicit human approval recorded in `.project/approvals.yml`.
- Before every external-state action (GitHub administration or publishing, provider access, data acquisition or publication, cloud deployment, public exposure, billing, or deletion), complete an external-action preflight: (1) confirm the matching recorded approval and its scope, (2) verify the required CLI or application authentication, (3) confirm the exact target account, project, repository, owner/name, region, visibility, and cost controls as applicable, and (4) stop and ask for approval or authentication if any item is missing or differs. Never treat a previous session's authentication as evidence for the current action.
- Before accepting any non-synthetic data, record its source URL, license, version or snapshot, intended fields, and prohibited fields. Do not ingest user conversations, customer data, secrets, personal data, or sensitive data into the demo.
- Keep the repository and GitHub release record clean: remove or update stale project records when a decision changes; do not create placeholder, duplicate, or superseded status documents. Check local Git status and the relevant GitHub target before publication or deployment.
- Update architecture, evidence, state, and handoff when verified facts change.
- Run `project-kit check` before claiming a milestone is complete.
