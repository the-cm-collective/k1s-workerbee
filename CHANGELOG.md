# Changelog

All notable WorkerBee changes are documented here. Earlier entries are best
effort summaries reconstructed from release notes and git history.

## v1.0.1 - 2026-11-12

- Added public PyPI, repository, documentation, issue, and changelog links to
  the package metadata.
- Added PyPI version and Python support badges to the packaged README.
- Retained the exact `k1s-core==1.0.0` runtime provider and all v1.0.0 runtime,
  MCP, installer, wheelhouse, and compatibility behavior unchanged.

## v1.0.0 - 2026-11-11

- Promoted the qualified WorkerBee package, CLI, MCP, runtime-provider,
  installer, wheelhouse, profile-evidence, and public documentation contracts
  to the stable `1.0.0` identity.
- Bound the release to the published exact-byte `k1s-core==1.0.0` provider from
  source commit `047c601c0c8426f1dc4c6da579a80ccd175c55cb`; the accepted wheel is
  `1057377` bytes with SHA256
  `1208552881446eb7b4fdefd27a22ca2f79f70fb851445bebf0a17273a507724f`.
- Canonicalized wheel member order, timestamps, modes, and RECORD content, and
  canonicalized sdist member modes so equivalent trusted-workstation source
  modes produce byte-identical publication artifacts.
- Switched README images to PyPI-portable Markdown with absolute public URLs.

## v1.0.0rc2 - 2026-11-11

- Rebound WorkerBee to the published exact-byte `k1s-core==1.0.0rc2`
  provider from source commit `4d91576326105fbe8a62c1dc5c01c6d5cfdca460`.
- Recorded the provider wheel filename, size, and SHA256 served identically by
  PyPI and TestPyPI, and opened the fail-closed WorkerBee publication gate.
- Advanced the package, wheelhouse, installed-consumer qualification, and
  public documentation contracts to `1.0.0rc2` while preserving RC1 as an
  immutable local review checkpoint.
- Canonicalized release-builder permissions across caller umasks and allowed
  the sealed wheelhouse to reuse the audited WorkerBee candidate byte-for-byte.

## v1.0.0rc1 - 2026-11-11

- Rebound the release contract to the sealed exact-byte
  `k1s-core==1.0.0rc1` provider while keeping package publication fail-closed
  until that provider has separate published-index authority.
- Added the bounded, metadata-safe K1s profile-evidence consumer with an
  unambiguous `workerbee k1s-evidence` CLI, read-only MCP projection, exact
  same-runtime provider binding, and versioned capability readback.
- Previously rebound the active runtime-provider contract from the temporary
  `k1s-core==0.1.8a1` candidate to the published exact-byte
  `k1s-core==1.0.0a3` artifact while preserving the `ae` compatibility imports.
- Added exact provider filename, size, and SHA256 verification; mixed-provider
  rejection; and local-only, no-index wheelhouse construction.
- Added the public v1 MCP, readiness, controlled-run, native-direct,
  deterministic release, transactional installer, and rollback contracts.
- Added a versioned public K1s consumer descriptor for the portable profile
  catalog, bootstrap projection boundary, strict-CRI transaction boundary, and
  explicit advanced `ae` fallback rule.
- Added deterministic installed-artifact qualification that performs two fresh
  strict no-index installs outside both source checkouts and proves exact
  package, entry-point, provider, and source-isolation identity without starting
  profiles, providers, or workloads.
- Aligned the public direct-pip, transactional installer, release, wiki, and
  evidence-level guidance. Runtime- and host-dependent D2/D3 scenarios remain
  explicitly deferred rather than being described as live-qualified.

## v0.1.7 - 2026-08-27

- Added read-only MCP resource contracts and complementary architecture fixtures
  so agent harnesses can discover WorkerBee runbooks, runtime state, and bounded
  mutation surfaces without private product code.
- Hardened WorkerBee image build context ignores to reduce accidental inclusion
  of local state, caches, secrets, and generated artifacts.
- Fixed DNS forwarding fallback behavior so later positive upstream responses
  can recover from router NXDOMAIN, empty-answer, or transient upstream errors.
- Hardened ingress readiness and dashboard health behavior, including bounded
  Caddy reloads, transient TLS health-probe retries, safe dashboard `/healthz`
  handling when the dashboard backend is disabled, and non-destructive Caddy CA
  mismatch recovery.
- Added a deploy readiness timeout override for local manifest deployment tools
  so slower workloads can be validated without changing global timeout behavior.
- Published as a stable release pinned to
  `k1s-workerbee-runtime==0.1.7`.

## v0.1.6 - 2026-07-24

- Stable release of the v0.1.6 line aligned with k1s v0.1.6.
- Promotes the v0.1.6.dev1 through v0.1.6.dev3 development channel into the
  stable WorkerBee install path.
- Includes external k1s edge-link start/status/validate/stop workflows,
  direct-containerd profile hardening, per-project runbooks, AI fabric lab
  validation, AI Max / Strix Halo edge-cell simulation, public-safe OpenStack
  Lite contract fixtures, namespaced RBAC validation, and bundle export
  hardening.
- Published as a stable release pinned to `k1s-workerbee-runtime==0.1.6`.

## v0.1.6.dev3 - 2026-07-23

- Added AI Max edge-cell simulation coverage for multi-cell fabric behavior,
  installer artifact signing, installer assurance, boot evidence, quarantine,
  autonomy state, disconnected operation, HA recovery, governance evidence, and
  readiness bundle export.
- Added OpenStack Lite public-safe contracts for route parity, dashboard
  fixtures, disabled routes, redacted refresh, dossier reporting, and target
  import dry-run validation.
- Added the OSS/private alignment ledger and backported generic bundle export
  hardening needed by public WorkerBee users.
- Improved edge-link and direct-containerd behavior around the corrected
  4-node AI Max cell model, service alias refresh, provider readiness, and
  stale endpoint avoidance.
- Improved API shim route preference, loopback CLI calls, namespaced RBAC
  validation, containerd namespace boundaries, and native manifest export
  security intent preservation.
- Fixed duplicate ingress route reconciliation, API shim RBAC role evaluation,
  local image warning coverage for loopback registry references, and direct
  containerd alias refresh for referenced providers.
- Published as a development-channel candidate pinned to
  `k1s-workerbee-runtime==0.1.6.dev3`.

## v0.1.6.dev2 - 2026-06-19

- Added the AI fabric lab validation package with native k1s stages, model
  lanes, LoRA adapter smoke coverage, retrieval evidence, DAS advisory
  scenarios, Hyperon sidecar scaffolding, and retained validation reports.
- Added per-project runbooks so Codex agents and operators can preserve and
  refine proven WorkerBee bring-up, deploy, repair, and validation paths.
- Added WorkerBee image hardening build metadata for minimal/hardened image
  review without changing existing repo Dockerfiles by default.
- Added workload restart support for local development images that reuse stable
  `:dev` tags.
- Improved direct-containerd and profile workflows with runtime preference,
  profile ingress/log hardening, exposed profile workload ingress, and cleanup
  guidance.
- Fixed stale project ingress, persisted ingress CA state across restarts,
  wildcard listener detection during port allocation, project port collision
  handling, containerd MCP log fallback, and WorkerBee-owned Podman CNI
  normalization.
- Fixed direct-containerd profile workload validation so containerized k1s
  controllers always have a live WorkerBee helper socket for image inspection
  and runtime operations.
- Fixed profile workload HTTPS validation after ingress restarts or cleanup by
  repairing global ingress metadata on reload and live daemon status checks.
- Improved profile workload deploy validation by retrying transient remote
  `apply` failures from container runtime startup races.
- Published as a development-channel candidate pinned to
  `k1s-workerbee-runtime==0.1.6.dev2`.

## v0.1.6.dev1 - 2026-05-31

- Added the k1s edge-link runner and HA-aware external-core targets.
- Added MCP/API support for edge-link validation, start, status, and stop
  workflows.
- Improved global dashboard load behavior for large local WorkerBee project
  sets by avoiding eager k1s runtime resolution during project listing.
- Changed the global dashboard shell to load first and refresh project data
  asynchronously with single-flight polling.
- Fixed stale generated dashboard routes for stopped profile and project
  dashboards.
- Published as a development-channel build pinned to
  `k1s-workerbee-runtime==0.1.6.dev1`.

## v0.1.4 - 2026-05-12

- Added explicit LAN ingress DNS forwarding so local devices can resolve
  WorkerBee app domains without changing router DNS.
- Added global dashboard DNS and ingress state, including LAN mode details and
  device DNS guidance.
- Added CA export and trust guidance across CLI, MCP status, trust status, and
  the global dashboard.
- Added `workerbee ingress ca --output workerbee-ca.crt` and
  `workerbee_v1_ingress_status` for discovering ingress, DNS, CA readiness, and
  command guidance.
- Fixed WorkerBee ingress upstreams to prefer host ports where appropriate.
- Expanded README and wiki-oriented docs for LAN ingress, DNS, and CA workflows.

## v0.1.3 - 2026-05-12

- Reduced wheelhouse and install footprint for release artifacts.
- Fixed dashboard-origin proxying for API shim streams.
- Fixed containerd helper exec streaming over PTY.

## v0.1.2 - 2026-05-11

- Hardened local secret and token state with owner-only atomic writes.
- Added secure-by-default MCP remote bind guard with explicit opt-in.
- Added dashboard response security headers and content security policy.
- Hardened direct containerd and k1s live deploy flows against shared
  MicroK8s/containerd conflicts.
- Added remote k1s live deploy integration coverage and secret policy status
  tooling.
- Clarified WorkerBee install methods in documentation.

## v0.1.1 - 2026-05-09

- Added WorkerBee security review workflow.
- Bumped WorkerBee release metadata to v0.1.1.

## v0.1.0 - 2026-05-09

- Initial WorkerBee v0.1 release.
- Shipped the local MCP workbench, project-scoped k1s app workflow, dashboard,
  local HTTPS ingress, installer, and wheelhouse release assets.
