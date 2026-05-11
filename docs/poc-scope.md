# WorkerBee v0.1 Scope

## Implemented Baseline

WorkerBee now has two validation paths:

- The default app-stack path starts a local, single-node k1s workbench from an
  installed `k1s-workerbee-runtime` package or a sibling `../k1s` checkout
  without modifying k1s.
- The advanced profile path runs containerized k1s controller/runtime profiles
  in explicit direct containerd mode for k1s development.

The representative app stack is deployed through native k1s manifests, not Helm. It includes:

- `store`: stateful HTTP key/value service with seeded data, service port, health checks, emptyDir, and delete-retention storage.
- `api`: backend service that calls `store`, consumes config and secret projections, exposes health and app-check endpoints, declares ingress, and uses resource/security settings.
- `frontend`: HTTP frontend that calls `api`, exposes health and UI endpoints, and declares ingress paths.

The default flow builds local images with Podman, Docker, or explicit direct
containerd, applies native k1s manifests, queries native k1s status, fetches
logs, validates the live app chain, checks API shim pod visibility, and exports
handoff artifacts.

An advanced direct containerd profile path now covers k1s development cases where
the app under test is a k1s control-plane stack rather than an ordinary workload.
Those profiles containerize every k1s subsystem: controllers, API shim,
dashboard, etcd, NATS, and direct containerd workload runtime support. This path
is intentionally not available on Podman or Docker because it validates k1s
against host containerd semantics while preserving WorkerBee state-hash namespace
boundaries.

## Extended MCP/Ingress Scope

WorkerBee now has the planned shared MCP shape:

- A single background local MCP daemon can serve multiple clients. Each tool accepts a `project` value, allowing two Codex sessions in different working trees to share one MCP daemon while keeping k1s state, runtime networks, generated manifests, and app ingress separated by project name.
- Agents bootstrap each repo with `workerbee_v1_session_start`, which derives a stable project id from the Git repo name, branch, and cwd hash, persists that cwd for later tool calls, and returns the cloud-native build/deploy/test/export runbook.
- Per-project mode is persisted as `lazy`, `start`, or `stop`. `lazy` is the default, `start` launches the stack during bootstrap, and `stop` provides a persistent user-controlled off switch that returns `PROJECT_STOPPED` for runtime operations.
- `workerbee mcp start` starts the background daemon, `workerbee mcp stop` stops the daemon plus global dashboard/Caddy ingress, and `workerbee mcp serve` remains the foreground/debug path. MCP port conflicts fail fast instead of silently selecting another port.
- The MCP daemon starts a global dashboard immediately and prints the URL before serving MCP traffic. The dashboard lists all known project-scoped stacks under the daemon state root, including branch metadata when available.
- A global Caddy edge is started for local HTTPS. Project app hosts are scoped as `app.<project>.workerbee.localhost` and `api.<project>.workerbee.localhost`, with Caddy using its internal local CA.
- Native k1s manifest deploys and the default app stack return browser-ready
  ingress URLs when the MCP daemon provides ingress configuration.
- Direct containerd k1s profiles publish controller and API ingress under the
  project namespace. The canonical controller host is
  `https://k1s.<project>.workerbee.localhost:19443/`; the API shim host is
  `https://k1s-api.<project>.workerbee.localhost:19443/`.
- Profile workload deploys require the background MCP daemon because profile
  dashboard/API/app ingress is owned by the global Caddy edge. They apply staged
  native k1s manifests through the project-scoped profile API using
  WorkerBee-owned internal tokens and Caddy CA trust. The target is selected
  with `manifest deploy-local --target profile` or MCP
  `workerbee_v1_manifest_deploy_local(target="profile")`.
- `workerbee_v1_profile_workload_validate` builds a bundled realtime
  frontend/backend/db stack, deploys it into the selected k1s profile, validates
  dashboard/docs/API health, probes HTTPS ingress, verifies a WebSocket round
  trip, and exports k1s/Kubernetes/Helm artifacts.
- `workerbee_v1_ingress_probe` gives agents a Caddy-CA-aware HTTPS probe restricted to WorkerBee-managed localhost hosts, so TLS trust setup is not required for automated smoke checks.
- CA trust remains explicit. `workerbee trust status` reports the generated Caddy CA path, and `workerbee trust install` performs the OS/user trust-store install only when requested.
- The wheelhouse flow can package `k1s-workerbee` plus `k1s-workerbee-runtime`, so users only need the WorkerBee wheelhouse and a supported container runtime.

## v0.1 Capability Baseline

The v0.1 baseline is a distributable local agent workbench with these core
capabilities:

- MCP contract: register v1-only `workerbee_v1_*` tool names, stable result envelopes, stable errors, version/capabilities reporting, and dashboard URL notification semantics.
- Runtime support: harden Podman rootless, Podman rootful, Docker Linux, Docker Desktop, and explicit direct containerd behavior, especially Caddy host reachability and port cleanup.
- Direct containerd safety: use WorkerBee state-hash namespaces, state-local CNI config paths, explicit `--runtime containerd` privilege handling, a state-scoped sudo root helper when unprivileged `nerdctl` cannot reach system containerd, and cleanup boundaries that never target reserved namespaces such as `ae`, `k8s.io`, `moby`, or `default`.
- Lifecycle safety: prevent two independent MCP daemons from mutating the same state root, clean stale processes, avoid port drift, support purge/reset, and never expose bearer tokens in tool results.
- Deployment inputs: support staged native k1s manifests as the primary path, practical Kubernetes YAML apply through `ae apply --k8s`, image build contexts, and simple generated app templates. Kubernetes input is intentionally limited to one workload plus optional Service/Ingress per file for v0.1; native k1s input is required for native k1s bundle export.
- Secret handling: keep generated local-stack secrets SOPS/age-encrypted by default, require an explicit plaintext escape hatch for local runs, block unsafe remote native `secretRefs` unless explicitly allowed, and avoid emitting Secret values in Kubernetes/Helm exports.
- Observability: defer richer resource summaries/events/ingress health to k1s-side work and track it through `docs/rfcs/k1s-workerbee-observability.md`.
- TLS/dev CA: complete guided trust-store handling across NixOS, Debian/Fedora, macOS, Windows, Firefox/NSS, and containerized browser cases.
- Artifact handoff: export native k1s bundles, Kubernetes YAML, Helm chart skeletons, and image metadata suitable for registry handoff.
- Packaging: publish repeatable wheels/wheelhouses, ship a one-line installer, document active-venv versus standalone install behavior, provide source-development fallbacks without requiring k1s project edits, and include best-effort macOS install/trust guidance until macOS validation is complete.

## Remaining v0.1 Validation

- Complete host matrix smoke checks for rootless/rootful Podman, Docker Linux,
  Docker Desktop, and direct containerd.
- Validate macOS and Windows trust-store guidance on real hosts.
- Keep richer resource summaries, events, and ingress health on the k1s-side RFC
  track instead of duplicating k1s observability in WorkerBee.

## Later Phase

After v0.1, the larger product scope is to make WorkerBee an agent-native cloud-native simulator:

- Multi-stack test scenarios with seeded databases, queues, object stores, and failure injection.
- Reproducible ephemeral environments per agent task, branch, or namespace.
- Policy/safety controls for image builds, host mounts, network egress, and command execution.
- DNS options beyond `*.localhost`, including private dev domains, wildcard local DNS, and team-shared smoke-test tunnels where explicitly enabled.
- Stronger global dashboard UX with resource graphs, logs, events, app links, and per-project cleanup controls.
- MCP server auth and explicit network exposure modes for cases where the server is not bound only to loopback.
- CI mode for headless validation and artifact export.
- Plugin-like extension points for project-specific app stacks and verification recipes.
