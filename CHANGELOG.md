# Changelog

All notable WorkerBee changes are documented here. Earlier entries are best
effort summaries reconstructed from release notes and git history.

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
