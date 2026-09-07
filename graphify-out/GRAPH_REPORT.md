# Graph Report - /Users/vaibhavkhurana/Development/repos/AI/llm-gateway  (2026-08-10)

## Corpus Check
- 2 files · ~55,037 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 543 nodes · 1381 edges · 33 communities (25 shown, 8 thin omitted)
- Extraction: 79% EXTRACTED · 21% INFERRED · 0% AMBIGUOUS · INFERRED: 294 edges (avg confidence: 0.59)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Data Plane Protocol
- Control Plane Bootstrap
- Identity and Gateway Keys
- M8 Architecture Evidence
- Provider Adapters
- M9 Activation Controls
- Console Dependencies
- Managed Load Harness
- React Compiler Config
- Workbench Interface
- Local Load Evaluation
- Console Tooling Config
- Project Record Checker
- Privacy Inspector
- Project Delivery Rules
- Browser Verification
- Delivery Evidence Lifecycle
- M7 Console Scan
- M7 API Scan
- M7 Simulator Scan
- M8 API Scan
- M8 Console Scan
- M8 Simulator Scan
- TypeScript Project References
- Dependency Audit
- Services Package
- Project Manifest
- Filesystem Path Types
- Async HTTP Types
- Synchronous HTTP Types
- Error Types

## God Nodes (most connected - your core abstractions)
1. `GovernanceStore` - 67 edges
2. `FirestoreIdentityStore` - 53 edges
3. `UserPrincipal` - 47 edges
4. `Target` - 44 edges
5. `ScenarioCatalog` - 34 edges
6. `GovernanceError` - 30 edges
7. `create_app()` - 27 edges
8. `FailedAuthLimiter` - 24 edges
9. `AuthorizationError` - 24 edges
10. `RoutePolicy` - 24 edges

## Surprising Connections (you probably didn't know these)
- `Private LLM Gateway Workbench Page` --semantically_similar_to--> `Private LLM Gateway Workbench Repository`  [INFERRED] [semantically similar]
  console/index.html → README.md
- `External Action Preflight` --semantically_similar_to--> `External Action Preflight`  [INFERRED] [semantically similar]
  docs/operations.md → .project/approvals.yml
- `Measurements` --uses--> `GovernanceError`  [INFERRED]
  scripts/run_m7_load.py → services/governance.py
- `Measurements` --uses--> `GovernanceStore`  [INFERRED]
  scripts/run_m7_load.py → services/governance.py
- `Measurements` --uses--> `ReceiptMetadata`  [INFERRED]
  scripts/run_m7_load.py → services/governance.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **M8 Verified Closure State** — _project_handoff_m8_verified_state, _project_handoff_managed_load_evidence, _project_handoff_deployed_privacy_evidence, _project_state_current_m8_status, _project_state_m8_topology [INFERRED 0.95]
- **M9 Pending Approval Transition** — _project_handoff_m9_plan_prepared, _project_handoff_m9_next_action, _project_handoff_continuation_constraints, _project_state_m9_activation_plan, _project_state_m9_approval_blocker, _project_state_m9_next_action [INFERRED 0.95]
- **Private Workbench Deployment Boundary** — project_private_llm_gateway_workbench, _project_architecture_three_service_cloud_run_topology, docs_security_trust_boundaries, docs_operations_operating_modes [INFERRED 0.95]
- **Synthetic Metadata-Only Data Boundary** — project_hosted_data_boundary, docs_api_contract_hosted_prompt_enforcement, docs_data_policy_non_retention_boundary, docs_data_policy_metadata_only_retention, _project_evidence_m8_deployed_privacy [INFERRED 0.95]
- **M9 Controlled Activation Lifecycle** — docs_m9_activation_plan_exact_owner_approval, docs_m9_activation_plan_post_reservation_secret_resolution, docs_m9_activation_plan_temporary_outbound_network, docs_m9_activation_plan_bounded_fourteen_attempt_rehearsal, docs_m9_activation_plan_mandatory_teardown [EXTRACTED 1.00]

## Communities (33 total, 8 thin omitted)

### Community 0 - "Data Plane Protocol"
Cohesion: 0.06
Nodes (73): deque, model_validator, Request, Any, Path, Committed synthetic scenario catalog., Return display metadata without exposing committed prompt or response text., scenario_checksum() (+65 more)

### Community 1 - "Control Plane Bootstrap"
Cohesion: 0.06
Nodes (61): ArgumentParser, Headers, ensure_demo_operator(), ensure_policy(), existing_owner(), main(), parser(), create_app() (+53 more)

### Community 2 - "Identity and Gateway Keys"
Cohesion: 0.09
Nodes (23): FirestoreIdentityStore, IAPIdentity, IssuedKey, Any, Client, datetime, clock(), iap_headers() (+15 more)

### Community 3 - "M8 Architecture Evidence"
Cohesion: 0.05
Nodes (41): M8 Private Deployment Approval, Approved Synthetic Data Boundary, Firestore Contention Decision, Firestore-First Governance State, Owner-Armed Live Provider Control, Three-Service Cloud Run Topology, M7 Firestore Partitioning Evidence, M8 Managed Capacity Evidence (+33 more)

### Community 4 - "Provider Adapters"
Cohesion: 0.13
Nodes (35): KeyAuthentication, create_app(), _anthropic_finish_reason(), _anthropic_request(), build_request(), _chunk(), normalize_response(), normalized_stream() (+27 more)

### Community 5 - "M9 Activation Controls"
Cohesion: 0.08
Nodes (32): External Action Preflight, M9 Exact Owner Approval Gate, Proposed M9 Activation Architecture, Streaming Fallback Boundary, M8 Deployed Privacy Evidence, M9 Exact Next Action, M9 Plan Prepared, M9 Activation Plan State (+24 more)

### Community 6 - "Console Dependencies"
Cohesion: 0.07
Nodes (28): @axe-core/playwright, dependencies, react, react-dom, devDependencies, @axe-core/playwright, @playwright/test, @types/react (+20 more)

### Community 7 - "Managed Load Harness"
Cohesion: 0.19
Nodes (20): AsyncClient, Client, Counter, Exception, FirestoreIdentityStore, IssuedKey, boundary_probe(), cleanup() (+12 more)

### Community 8 - "React Compiler Config"
Cohesion: 0.08
Nodes (23): compilerOptions, allowJs, allowSyntheticDefaultImports, esModuleInterop, forceConsistentCasingInFileNames, isolatedModules, jsx, lib (+15 more)

### Community 9 - "Workbench Interface"
Cohesion: 0.14
Nodes (12): App(), dollars(), EMPTY_RUN, LiveSession, Operations, Policy, Receipt, Role (+4 more)

### Community 10 - "Local Load Evaluation"
Cohesion: 0.30
Nodes (11): boundary_probe(), configure(), exercise(), Measurements, parse_args(), percentile(), Client, Namespace (+3 more)

### Community 11 - "Console Tooling Config"
Cohesion: 0.20
Nodes (9): compilerOptions, composite, module, moduleResolution, outDir, skipLibCheck, include, playwright.config.ts (+1 more)

### Community 12 - "Project Record Checker"
Cohesion: 0.40
Nodes (9): Path, check(), graph_sync(), main(), milestone_status(), Namespace, repository_files(), source_signature() (+1 more)

### Community 13 - "Privacy Inspector"
Cohesion: 0.42
Nodes (8): contains_any(), field_paths(), gcloud_json(), parse_args(), Any, Namespace, run(), strings_under_content_keys()

### Community 14 - "Project Delivery Rules"
Cohesion: 0.60
Nodes (5): Copilot Project Delivery Adapter, External Action Preflight Rule, First Unblocked Milestone Rule, Project Delivery Rules, Project Delivery Adapter

### Community 15 - "Browser Verification"
Cohesion: 0.50
Nodes (4): metadataHeaders(), mockWorkbench(), policy, scenarios

### Community 16 - "Delivery Evidence Lifecycle"
Cohesion: 0.50
Nodes (4): Verified M0-M8 Delivery Evidence, Sequential M0-M10 Delivery Plan, Evidence-Backed Claims Rule, Ordered Evidence-Gated Delivery Rule

### Community 17 - "M7 Console Scan"
Cohesion: 0.50
Nodes (3): runs, $schema, version

### Community 18 - "M7 API Scan"
Cohesion: 0.50
Nodes (3): runs, $schema, version

### Community 19 - "M7 Simulator Scan"
Cohesion: 0.50
Nodes (3): runs, $schema, version

### Community 20 - "M8 API Scan"
Cohesion: 0.50
Nodes (3): runs, $schema, version

### Community 21 - "M8 Console Scan"
Cohesion: 0.50
Nodes (3): runs, $schema, version

### Community 22 - "M8 Simulator Scan"
Cohesion: 0.50
Nodes (3): runs, $schema, version

## Knowledge Gaps
- **97 isolated node(s):** `version`, `$schema`, `runs`, `version`, `$schema` (+92 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `GovernanceStore` connect `Control Plane Bootstrap` to `Data Plane Protocol`, `Local Load Evaluation`, `Identity and Gateway Keys`?**
  _High betweenness centrality (0.063) - this node is a cross-community bridge._
- **Why does `FirestoreIdentityStore` connect `Identity and Gateway Keys` to `Data Plane Protocol`, `Control Plane Bootstrap`?**
  _High betweenness centrality (0.044) - this node is a cross-community bridge._
- **Why does `Target` connect `Data Plane Protocol` to `Control Plane Bootstrap`, `Local Load Evaluation`, `Provider Adapters`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Are the 21 inferred relationships involving `GovernanceStore` (e.g. with `Measurements` and `InviteUserRequest`) actually correct?**
  _`GovernanceStore` has 21 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `FirestoreIdentityStore` (e.g. with `InviteUserRequest` and `IssueKeyRequest`) actually correct?**
  _`FirestoreIdentityStore` has 17 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `UserPrincipal` (e.g. with `Measurements` and `InviteUserRequest`) actually correct?**
  _`UserPrincipal` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 19 inferred relationships involving `Target` (e.g. with `Measurements` and `ChatRequest`) actually correct?**
  _`Target` has 19 INFERRED edges - model-reasoned connections that need verification._