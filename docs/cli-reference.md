# WorkerBee CLI reference

The `k1s-workerbee` distribution installs the `workerbee` command. The CLI and
the `workerbee_v1_*` MCP tools operate the same project-scoped WorkerBee model,
but they are separate interfaces. CLI commands do not impersonate an MCP
session, and MCP tool availability remains capability and policy driven.

Run `workerbee --help` or `workerbee <command> --help` for the installed
argument contract. Use `workerbee doctor` before runtime work.

## Global project and runtime selection

Common global options select the project name/cwd, runtime, state root, and
JSON output. `workerbee config show|path|set|clear` manages user-local defaults.
Explicit command-line values take precedence over configured defaults.

The normal OSS v1 lane binds the exact caller-supplied checkout as
`source_mode="native-direct"` with `execution_lane="native-local"`. Podman or
Docker is the ordinary app-workbench runtime. Direct containerd is an advanced
k1s-development lane with explicit privilege, namespace, and cleanup
boundaries; selecting it does not grant ambient host ownership.

## Setup and lifecycle

| Command | Purpose |
| --- | --- |
| `workerbee config show|path|set|clear` | Inspect or update local CLI defaults. |
| `workerbee agent instructions` | Print the generic WorkerBee `AGENTS.md` guidance. |
| `workerbee agent install` | Check or install the managed guidance block. |
| `workerbee doctor` | Report prerequisites, provider ownership, runtime readiness, and bounded diagnostics. |
| `workerbee start` | Start the selected project workbench. |
| `workerbee stop` | Stop it; optional purge behavior remains explicit. |
| `workerbee status` | Show selected-project stack status. |
| `workerbee reset` | Reset selected-project workloads and WorkerBee-owned artifacts. |
| `workerbee cleanup` | Inspect or explicitly remove stale WorkerBee-owned runtime resources. |

## Shared daemon and project control

| Command | Purpose |
| --- | --- |
| `workerbee mcp start|status|restart|stop` | Manage the background Streamable HTTP MCP daemon. |
| `workerbee mcp serve` | Run the MCP server in the foreground for debugging. |
| `workerbee projects` | List projects known to the daemon state root. |
| `workerbee project status` | Inspect one project's mode and runtime state. |
| `workerbee project mode lazy|start|stop` | Persist the selected project lifecycle policy. |
| `workerbee global-dashboard` | Show global dashboard status. |
| `workerbee ingress status` | Inspect global Caddy/DNS/CA state. |
| `workerbee ingress ca` | Export the generated public CA certificate. |
| `workerbee trust status|install|uninstall` | Inspect or explicitly change local CA trust. |
| `workerbee tls-info` | Show local API-shim TLS paths. |

MCP binds are loopback-only by default. Non-loopback MCP and LAN application
ingress require separate explicit flags. LAN ingress does not imply remote MCP
authorization.

## Project runbooks

```text
workerbee runbook status
workerbee runbook update
workerbee runbook export
workerbee runbook import
```

Runbooks are project-scoped, bounded operational context. Update/import paths
screen for secret-like content and preserve WorkerBee state/repository
ownership boundaries.

## Build, deploy, inspect, and export

| Command | Purpose |
| --- | --- |
| `workerbee build-image CONTEXT --tag TAG` | Build a local image with the selected runtime. |
| `workerbee manifest prepare` | Create an editable named stage from native k1s or practical Kubernetes input. |
| `workerbee manifest validate` | Validate staged files without deploying them. |
| `workerbee manifest deploy-local` | Deploy a stage to the local WorkerBee or selected profile target. |
| `workerbee manifest deploy-k1s` | Deploy a stage to an explicit remote k1s API target. |
| `workerbee deploy` | Apply a native k1s manifest directly. |
| `workerbee deploy-poc` | Build and deploy the representative application stack. |
| `workerbee poc-status` | Inspect that representative stack through the native k1s API. |
| `workerbee logs APP` | Read bounded recent app logs. |
| `workerbee exec APP -- COMMAND` | Run a bounded command in a selected app container. |
| `workerbee apishim-smoke` | Inspect representative objects through the API shim. |
| `workerbee validate` | Run an explicit WorkerBee validation scenario. |
| `workerbee bundle export` | Export native k1s, Kubernetes, or Helm handoff artifacts. |
| `workerbee export-k8s` | Export the representative POC manifests as Kubernetes YAML. |

`manifest deploy-local --target profile` requires a compatible running profile
and the background MCP daemon because the global ingress edge owns profile
dashboard, API, and app routes. `manifest deploy-k1s` fails closed on native
remote secret references unless the operator explicitly allows that boundary.

## Profiles and edge links

```text
workerbee profile list
workerbee profile start PROFILE
workerbee profile status PROFILE
workerbee profile stop PROFILE

workerbee edge-link start
workerbee edge-link status
workerbee edge-link validate
workerbee edge-link stop
```

Profiles are advanced containerized k1s development environments. Edge links
are explicit, short-lived external gateway/node validation sessions. Neither
surface silently promotes a normal app project to host-wide authority.

## K1s profile evidence

```text
workerbee k1s-evidence list
workerbee k1s-evidence doctor PROFILE
workerbee k1s-evidence plan PROFILE
workerbee k1s-evidence status PROFILE
workerbee k1s-evidence logs PROFILE --component COMPONENT
```

This is a read-only, local-state, non-networked evidence surface backed by the
one exact `k1s-core` runtime selected by WorkerBee. It is distinct from
`workerbee profile`, which manages advanced direct-containerd development
profiles. The evidence command never accepts a K1s root, executable,
environment, command, endpoint, credential, runtime socket, or raw run ID.
Results omit raw source JSON, log content, stderr, and filesystem paths.

The MCP equivalent is `workerbee_v1_k1s_profile_evidence(operation, profile,
component)`. Its public operation list is exactly `list`, `doctor`, `plan`,
`status`, and `logs`. Bootstrap creation is not exposed through either CLI or
MCP; WorkerBee may only apply its internal pure adapter to an already-selected
public bootstrap projection during a separately authorized qualification flow.

## Security and privilege controls

| Command | Purpose |
| --- | --- |
| `workerbee security assess` | Run advisory checks against a stage/export and optional local ingress. |
| `workerbee security review-project` | Review the latest deployed project and write a bounded report. |
| `workerbee containerd-privilege status` | Show direct-containerd privilege posture. |
| `workerbee containerd-privilege stop-helper` | Stop the state-scoped root helper. |
| `workerbee containerd-privilege revoke-acl-leases` | Revoke tracked direct-containerd ACL leases. |

Security assessment is advisory. It does not make a manifest safe by itself.
Direct-containerd privilege is never the default app lane and does not grant
ownership of unrelated containerd namespaces, MicroK8s, Docker, or other host
container services.

## v1 stability boundary

The public distribution/import/CLI identity, `workerbee.mcp/v1` envelope, and
v1-only MCP tool names are compatibility contracts. Individual runtime,
profile, ingress, controlled-run, and platform-adapter capabilities remain
subject to their advertised policy and evidence status. See the
[WorkerBee v1 public contract](v1-contract.md),
[native-direct interoperability](native-direct-interoperability.md), and
[release foundation](release-foundation.md) before building automation around
experimental surfaces.
