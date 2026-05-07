# WorkerBee POC Scope

## Implemented POC

The POC starts a local, single-node k1s stack from a sibling `../k1s` checkout without modifying k1s. It runs the controller and Kubernetes API shim as host processes, stores state in WorkerBee-local SQLite files, and exposes the k1s dashboard as soon as the stack starts.

The representative app stack is deployed through native k1s manifests, not Helm. It includes:

- `store`: stateful HTTP key/value service with seeded data, service port, health checks, emptyDir, and delete-retention storage.
- `api`: backend service that calls `store`, consumes config and secret projections, exposes health and app-check endpoints, declares ingress, and uses resource/security settings.
- `frontend`: HTTP frontend that calls `api`, exposes health and UI endpoints, and declares ingress paths.

The POC flow builds local images with Podman, Docker, or explicit direct containerd, applies the native k1s manifests, queries native k1s status, fetches logs, validates the live app chain, checks API shim pod visibility, and exports Kubernetes YAML artifacts.

## Extended MCP/Ingress Scope

WorkerBee now has the planned shared MCP shape:

- A single background local MCP daemon can serve multiple clients. Each tool accepts a `project` value, allowing two Codex sessions in different working trees to share one MCP daemon while keeping k1s state, runtime networks, generated manifests, and app ingress separated by project name.
- Agents bootstrap each repo with `workerbee_v1_session_start`, which derives a stable project id from the Git repo name, branch, and cwd hash, persists that cwd for later tool calls, and returns the cloud-native build/deploy/test/export runbook.
- Per-project mode is persisted as `lazy`, `start`, or `stop`. `lazy` is the default, `start` launches the stack during bootstrap, and `stop` provides a persistent user-controlled off switch that returns `PROJECT_STOPPED` for runtime operations.
- `workerbee mcp start` starts the background daemon, `workerbee mcp stop` stops it, and `workerbee mcp serve` remains the foreground/debug path.
- The MCP daemon starts a global dashboard immediately and prints the URL before serving MCP traffic. The dashboard lists all known project-scoped stacks under the daemon state root, including branch metadata when available.
- A global Caddy edge is started for local HTTPS. Project app hosts are scoped as `app.<project>.workerbee.localhost` and `api.<project>.workerbee.localhost`, with Caddy using its internal local CA.
- Native k1s manifest deploys and the POC stack return browser-ready ingress URLs when the MCP daemon provides ingress configuration.
- `workerbee_v1_ingress_probe` gives agents a Caddy-CA-aware HTTPS probe restricted to WorkerBee-managed localhost hosts, so TLS trust setup is not required for automated smoke checks.
- CA trust remains explicit. `workerbee trust status` reports the generated Caddy CA path, and `workerbee trust install` performs the OS/user trust-store install only when requested.
- The wheelhouse flow can package `k1s-workerbee` plus `k1s-workerbee-runtime`, so users only need the WorkerBee wheelhouse and a supported container runtime.

## v0.1 Scope

The immediate v0.1 hardening work should turn the POC into a reliable distributable local agent workbench:

- MCP contract: register v1-only `workerbee_v1_*` tool names, stable result envelopes, stable errors, version/capabilities reporting, and dashboard URL notification semantics.
- Runtime support: harden Podman rootless, Podman rootful, Docker Linux, Docker Desktop, and explicit direct containerd behavior, especially Caddy host reachability and port cleanup.
- Lifecycle safety: prevent two independent MCP daemons from mutating the same state root, clean stale processes, avoid port drift, support purge/reset, and never expose bearer tokens in tool results.
- Deployment inputs: support staged native k1s manifests as the primary path, practical Kubernetes YAML apply through `ae apply --k8s`, image build contexts, and simple generated app templates. Kubernetes input is intentionally limited to one workload plus optional Service/Ingress per file for v0.1; native k1s input is required for native k1s bundle export.
- Observability: defer richer resource summaries/events/ingress health to k1s-side work and track it through `docs/rfcs/k1s-workerbee-observability.md`.
- TLS/dev CA: complete guided trust-store handling across NixOS, Debian/Fedora, macOS, Windows, Firefox/NSS, and containerized browser cases.
- Artifact handoff: export native k1s bundles, Kubernetes YAML, Helm chart skeletons, and image metadata suitable for registry handoff.
- Packaging: publish repeatable wheels/wheelhouses, ship a one-line installer, document active-venv versus standalone install behavior, and provide source-development fallbacks without requiring k1s project edits.

## Later Phase

After v0.1, the larger product scope is to make WorkerBee an agent-native cloud-native simulator:

- Multi-stack test scenarios with seeded databases, queues, object stores, and failure injection.
- Reproducible ephemeral environments per agent task, branch, or namespace.
- Policy/safety controls for image builds, host mounts, network egress, and command execution.
- DNS options beyond `*.localhost`, including private dev domains, wildcard local DNS, and team-shared smoke-test tunnels where explicitly enabled.
- Stronger global dashboard UX with resource graphs, logs, events, app links, and per-project cleanup controls.
- MCP server auth and explicit network exposure modes for cases where the server is not bound only to loopback.
- CI mode for headless POC validation and artifact export.
- Plugin-like extension points for project-specific app stacks and verification recipes.
