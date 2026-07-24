# WorkerBee Complementary Architecture

This document records the WBRR contracts that make WorkerBee complementary to
k1s rather than a second control plane. WorkerBee owns project-scoped build,
validation, probe, export, and evidence loops. k1s remains the runtime/control
surface for workloads and environments.

## WBRR-01 Application Profile Contract

The application profile contract is a repo-readable description of an app
topology before a target is selected. It is intentionally data-only: loading or
validating a profile does not start WorkerBee, deploy a manifest, create secrets,
or mutate k1s or target runtime state.

Profile documents use:

- `api_version: workerbee.application-profile/v1`
- `kind: WorkerBeeApplicationProfile`
- `metadata.name` for a stable profile identifier
- `spec.services[]` for named service roles, images, ports, protocols,
  dependencies, secret references, probes, and GPU hints
- `spec.export` for k1s/Kubernetes/Helm handoff behavior

The profile fixture at
[`examples/complementary-architecture/application-profile.json`](../examples/complementary-architecture/application-profile.json)
shows a generic API, worker, and store topology. Secret entries are references
only; raw secret values are invalid. GPU hints are advisory metadata and do not
request GPUs by themselves. Export behavior states which formats are expected
and explicitly records that secret values are not emitted.

This profile layer gives agents and operators a stable vocabulary for app shape
without embedding product-specific commercial service, marketplace, or
partner-proof concepts in core WorkerBee abstractions.

## WBRR-02 Target Manager Contract

The target manager contract records where WorkerBee is allowed to act and where
it must remain read-only. The fixture at
[`examples/complementary-architecture/target-manager.json`](../examples/complementary-architecture/target-manager.json)
defines the required target lanes:

- `local-dev`: project-scoped WorkerBee state, local images, local manifests,
  and Caddy routes.
- `local-validation`: direct-containerd profile validation under WorkerBee
  state-hash namespaces.
- `remote-k1s`: explicit remote k1s namespace/workload apply boundaries.
- `openstack-k1s-env`: OpenStack-compatible k1s environment actions through
  explicit environment, namespace, workload-prefix, and redacted-ledger
  boundaries. In OSS this is a contract lane; concrete product operators live
  outside core WorkerBee.
- `export-only`: artifact generation with no live target mutation.

Each target records `allowed_mutations`, `forbidden_mutations`,
`requires_confirmation`, and `evidence_surfaces`. The contract deliberately
keeps cluster-scoped webhooks, CRDs, global scheduler mutation, host networking
changes, router/DNS/firewall changes, and unrelated runtime state outside the
default WorkerBee target set.

## WBRR-03 Read-Only MCP Resource Contract

WorkerBee MCP keeps mutation behavior behind bounded tools, but clients also
need stable read-only context. The WBRR resource contract defines these
resource families:

- `projects`
- `status`
- `routes`
- `security`
- `trace`
- `profiles`
- `targets`

The static catalog is available to MCP clients at
`workerbee://contracts/wbrr/resources/v1` and is mirrored in
[`examples/complementary-architecture/mcp-resource-contracts.json`](../examples/complementary-architecture/mcp-resource-contracts.json).
Every resource record must set `read_only: true` and point to the bounded
WorkerBee tools that perform related mutations. This keeps read paths stable
without expanding the mutation surface.

## WBRR-04 Operation Trace Contract

The operation trace contract records a per-project timeline across the
WorkerBee lifecycle:

- `session`
- `build`
- `manifest`
- `validation`
- `deploy`
- `route`
- `probe`
- `security`
- `export`
- `promotion`

The fixture at
[`examples/complementary-architecture/operation-trace.json`](../examples/complementary-architecture/operation-trace.json)
uses one event per phase. Events carry an `id`, `phase`, `event`, `status`,
`timestamp`, `target`, `mutation` boolean, `evidence_refs`, and redaction
metadata. Trace records may describe mutating events, but the trace itself is an
evidence artifact and must not retain raw secret values.

## WBRR-05 Runtime Probe Pack Contract

The runtime probe pack contract describes validation coverage that WorkerBee can
attach to a profile, target, deployment, or proof surface. The fixture at
[`examples/complementary-architecture/runtime-probe-pack.json`](../examples/complementary-architecture/runtime-probe-pack.json)
defines required probe categories:

- WebSocket
- callback
- queue
- long-running job
- model endpoint
- GPU availability
- auth-negative

Probe pack entries are non-mutating by default. They record target, timeout, and
redacted evidence fields so the same coverage can be rendered in CLI, MCP,
dashboard, and report contexts. The auth-negative probe must declare denial
expectations because successful denial is the desired outcome.

## WBRR-06 Generic AI Runtime Sample Profile

The generic AI runtime sample at
[`examples/complementary-architecture/generic-ai-runtime-profile.json`](../examples/complementary-architecture/generic-ai-runtime-profile.json)
uses the same application profile API from WBRR-01. It models an `ai-router`,
`model-runtime`, `vector-store`, and `queue` without app-specific branding in
core abstractions. The model runtime includes advisory GPU hints and model/GPU
probe metadata, but the profile itself does not allocate GPUs or deploy a model.

The sample keeps WorkerBee's AI support reusable: branded demos can consume the
profile contract, but the contract names generic roles and evidence fields.

## WBRR-07 Static Proof Surface

The static proof surface at
[`examples/complementary-architecture/proof-surface.html`](../examples/complementary-architecture/proof-surface.html)
and the companion
[`examples/complementary-architecture/proof-summary.json`](../examples/complementary-architecture/proof-summary.json)
show the complementary architecture without requiring a live dashboard update.
The proof page covers:

- target manager boundaries
- profile topology
- operation trace
- runtime probe results
- advisory security findings
- export and promotion state

This is intentionally a static source fixture. It is suitable for docs,
dashboard embedding, or partner proof packages, but it performs no deployment,
cluster mutation, or runtime probing by itself.
