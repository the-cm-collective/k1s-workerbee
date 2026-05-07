# WorkerBee POC Scope

## Implemented POC

The POC starts a local, single-node k1s stack from a sibling `../k1s` checkout without modifying k1s. It runs the controller and Kubernetes API shim as host processes, stores state in WorkerBee-local SQLite files, and exposes the k1s dashboard as soon as the stack starts.

The representative app stack is deployed through native k1s manifests, not Helm. It includes:

- `store`: stateful HTTP key/value service with seeded data, service port, health checks, emptyDir, and delete-retention storage.
- `api`: backend service that calls `store`, consumes config and secret projections, exposes health and app-check endpoints, declares ingress, and uses resource/security settings.
- `frontend`: HTTP frontend that calls `api`, exposes health and UI endpoints, and declares ingress paths.

The POC flow builds local images with Podman or Docker, applies the native k1s manifests, queries native k1s status, fetches logs, validates the live app chain, checks API shim pod visibility, and exports Kubernetes YAML artifacts.

## v0.1 Scope

The next phase should turn the POC into a distributable local agent workbench:

- Package install path: publish a Python package with console scripts, clear k1s checkout resolution, and repeatable dependency installation.
- MCP contract: stabilize tool names, result schemas, error payloads, and dashboard URL notification semantics.
- Runtime support: harden Podman rootless, Podman rootful, Docker Linux, and Docker Desktop behavior.
- Lifecycle safety: isolate projects, clean stale processes, avoid port drift, support purge/reset, and never expose bearer tokens in tool results.
- Deployment inputs: support native k1s manifests, Kubernetes YAML through the shim/apply path where feasible, image build contexts, and simple generated app templates.
- Observability: provide status, logs, events, exported artifacts, dashboard links, and structured validation results as first-class MCP tools.
- TLS/dev CA: expose generated CA bundle paths, add guided trust-store installation commands by OS, and keep local plaintext modes explicit.
- Artifact handoff: export Kubernetes YAML, Helm chart skeletons where useful, and image metadata suitable for pushing to an external registry.

## Later Phase

After v0.1, the larger product scope is to make WorkerBee an agent-native cloud-native simulator:

- Multi-stack test scenarios with seeded databases, queues, object stores, and failure injection.
- Reproducible ephemeral environments per agent task, branch, or namespace.
- Policy/safety controls for image builds, host mounts, network egress, and command execution.
- Optional Caddy ingress and local DNS integration for browser-realistic testing.
- CI mode for headless POC validation and artifact export.
- Plugin-like extension points for project-specific app stacks and verification recipes.
