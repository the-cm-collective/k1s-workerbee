# Agent PBX controlled runs

WorkerBee exposes a narrow, capability-gated MCP contract for an Agent PBX root
Operator to request one fixed-recipe local validation run. This is not a generic
command runner and it does not extend `workerbee_v1_exec`.

The OSS package supplies request validation, durable idempotency, bounded
receipts, state journaling, and the MCP lifecycle. The strict isolation
appliance is platform-specific. Consequently, controlled runs are disabled by
default and the five lifecycle tools are not advertised or registered by an OSS
daemon unless all of the following are true:

1. `WORKERBEE_CONTROLLED_RUN_MODE=enabled` is explicitly set for that daemon.
2. Exactly one `workerbee.controlled_run_adapter` Python entry point is installed.
3. The adapter attests every required strict isolation, pinning, resource,
   evidence, recovery, and cleanup capability.
4. The adapter exposes at least one fixed recipe with an exact pinned tool
   manifest.

An unavailable or incomplete adapter is reported in
`workerbee_v1_capabilities.data.controlled_runs`. Its tools remain absent from
`mcp_tools`, and direct service calls fail closed.

## MCP surface

The lifecycle consists of exactly these tools:

- `workerbee_v1_controlled_run_preflight`
- `workerbee_v1_controlled_run_start`
- `workerbee_v1_controlled_run_status`
- `workerbee_v1_controlled_run_cancel`
- `workerbee_v1_controlled_run_receipt`

There is no caller-supplied `argv`, shell, script, command, environment, or
package-install field. Preflight and start accept only the Agent PBX identity,
source, profile, recipe, and tool-manifest bindings. Any caller command or
environment field is rejected even when nested in the profile.

## Preflight and start request

Agent PBX sends the same payload to preflight and start:

```json
{
  "request_id": "wbrun-request-01",
  "idempotency_key": "wbrun-request-01",
  "request_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "root_operator_agent_id": "operator-8",
  "target_fork_agent_id": "operator-8-fork-k1s",
  "campaign_id": "campaign-rc1",
  "workspace_binding_id": "workerbee-binding-01",
  "profile": {
    "profile_id": "isolated-runtime-validation",
    "version": 1,
    "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "enabled": true,
    "backend": "workerbee-standard-workspace",
    "required_authority": "trusted-workbench",
    "allowed_workerbee_backends": ["platform_debug"],
    "allowed_projects": ["k1s-workerbee-oss-v1"],
    "source_mode": "immutable-commit-export",
    "network_mode": "private-loopback",
    "external_egress": "deny",
    "credentials": "none",
    "recipe_id": "k1s-rc1-runtime-parity",
    "tool_manifest_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "max_seconds": 1800,
    "max_memory_mib": 4096,
    "max_output_bytes": 16777216,
    "max_concurrency": 1,
    "approval_mode": "campaign_lease",
    "lease_max_uses": 4,
    "lease_ttl_seconds": 7200,
    "retention_seconds": 604800,
    "cleanup_required": true,
    "allow_host_fallback": false
  },
  "profile_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "baseline_commit": "1111111111111111111111111111111111111111",
  "candidate_commit": "2222222222222222222222222222222222222222",
  "recipe_id": "k1s-rc1-runtime-parity",
  "tool_manifest_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
```

The profile digest is recomputed from the exact normalized fields used by Agent
PBX; WorkerBee requires that result, the embedded profile `sha256`, the payload
`profile_sha256`, the fixed recipe profile digest, and the recipe tool-manifest
digest to agree exactly. Agent PBX's `request_sha256` also binds backend and
workspace authority fields that are intentionally not sent to WorkerBee.
WorkerBee therefore treats it as the opaque authority digest, echoes it exactly,
and separately hashes the complete canonical MCP payload. Durable idempotency
requires both digests to match. It does not reinterpret or silently substitute
any bound value.

### Successful preflight

Preflight echoes the three digests Agent PBX verifies:

```json
{
  "ok": true,
  "request_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "profile_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "tool_manifest_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "workspace_binding_id": "workerbee-binding-01",
  "recipe_id": "k1s-rc1-runtime-parity",
  "source": {
    "mode": "immutable-commit-export",
    "export_count": 1,
    "export_sha256": "...",
    "baseline_commit": "1111111111111111111111111111111111111111",
    "baseline_tree": "...",
    "candidate_commit": "2222222222222222222222222222222222222222",
    "candidate_tree": "...",
    "caller_worktree_read_only": true,
    "source_roots_read_only": true
  },
  "policy": {}
}
```

The single export is one immutable workspace bundle containing read-only
baseline and candidate committed-tree roots. The adapter must resolve and
record both tree object IDs and the export manifest SHA-256 without checking out
into or writing to the caller worktree.

### Successful start

```json
{
  "run_id": "crun-0123456789abcdef01234567",
  "workspace_id": "workerbee-space-01",
  "state": "starting",
  "status": "preparing",
  "idempotent_reuse": false
}
```

The same idempotency key and identical canonical request return the original
run and workspace. Reusing the key with a different `request_sha256` or a
different canonical payload fails with
`WORKERBEE_CONTROLLED_RUN_IDEMPOTENCY_CONFLICT`.

## Fixed policy

The OSS contract requires the strict adapter to enforce all of these properties
as one appliance operation:

- import one immutable committed-tree workspace bundle;
- keep the caller worktree and imported sources read-only;
- create a fresh owner-only mode-`0700` run root, scratch, and evidence area;
- allow loopback TCP and pathname `AF_UNIX` sockets;
- deny DNS, non-loopback interfaces, non-loopback routes, non-loopback
  connections, external egress, abstract Unix sockets, and host control sockets;
- create the child environment from an empty allowlist and deny ambient
  credentials, configuration, and caller environment values;
- execute only the selected versioned recipe with the exact pinned tool
  manifest;
- deny package installation, package indexes, runtime/provider/tool fallback,
  and silent substitutions;
- deny writes into the host repository;
- enforce wall-time, memory, output, concurrency, artifact-count,
  artifact-size, and receipt-size limits;
- collect bounded evidence and perform cleanup on success, failure, timeout,
  cancellation, and crash recovery;
- report cleanup residue, with success prohibited until cleanup is complete and
  residue is empty.

The OSS adapter seam treats each property as a required capability. One missing
capability makes the entire controlled-run surface unavailable.

## State model

WorkerBee accepts the Agent PBX-compatible states:

```text
provisioning -> starting -> canary -> running -> collecting -> cleaning
                                              -> recovery_required

terminal: succeeded | failed | canceled | cancelled
```

The adapter can omit states that do not apply to its internal implementation,
but it cannot return an unknown state. `state` always uses the lifecycle above;
the companion `status` bridges the current Agent PBX executor vocabulary:
`provisioning` and `starting` become `preparing`, `canary` becomes `ready`, and
`recovery_required` becomes `recovering`. All other values are unchanged. A
terminal backend result with incomplete cleanup is normalized to
`recovery_required`, not success. After restart, the daemon reconciles durable
nonterminal journals through the adapter recovery operation before the run can
become cleanly terminal.

## Status, cancellation, and receipt

Status accepts only `run_id` and returns both `state` and `status` for Agent PBX
compatibility:

```json
{
  "run_id": "crun-0123456789abcdef01234567",
  "workspace_id": "workerbee-space-01",
  "state": "collecting",
  "status": "collecting",
  "updated_at": 1793920000.0
}
```

Cancel accepts `run_id` and an optional bounded reason. It is idempotent. A
cancel request still requires evidence collection and cleanup. If cleanup is not
complete, cancel returns `recovery_required`.

Receipt is a read-only, bounded, recursively redacted view. It always contains
an evidence object and explicit cleanup result:

```json
{
  "api_version": "workerbee.controlled-run-receipt/v1",
  "run_id": "crun-0123456789abcdef01234567",
  "workspace_id": "workerbee-space-01",
  "state": "succeeded",
  "request_sha256": "...",
  "canonical_payload_sha256": "...",
  "profile_sha256": "...",
  "tool_manifest_sha256": "...",
  "evidence": {
    "artifact_manifest_sha256": "...",
    "stdout_sha256": "...",
    "stderr_sha256": "..."
  },
  "cleanup": {
    "complete": true,
    "residue": []
  },
  "success": true,
  "receipt_sha256": "..."
}
```

Sensitive key classes such as tokens, credentials, passwords, cookies,
authorization headers, and private keys are redacted. Oversized evidence is
replaced with its encoded size and SHA-256 rather than returned verbatim.
`success` is true only when the state is `succeeded`, cleanup is complete, and
the residue list is empty.

## Failure codes

Important fail-closed errors include:

- `WORKERBEE_CONTROLLED_RUN_DISABLED`
- `WORKERBEE_CONTROLLED_RUN_CAPABILITY_UNAVAILABLE`
- `WORKERBEE_CONTROLLED_RUN_REQUEST_INVALID`
- `WORKERBEE_CONTROLLED_RUN_CALLER_COMMAND_DENIED`
- `WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT`
- `WORKERBEE_CONTROLLED_RUN_RECIPE_DENIED`
- `WORKERBEE_CONTROLLED_RUN_TOOL_MANIFEST_DRIFT`
- `WORKERBEE_CONTROLLED_RUN_SOURCE_DRIFT`
- `WORKERBEE_CONTROLLED_RUN_IDEMPOTENCY_CONFLICT`
- `WORKERBEE_CONTROLLED_RUN_CONCURRENCY_LIMIT`
- `WORKERBEE_CONTROLLED_RUN_STATUS_INVALID`
- `WORKERBEE_CONTROLLED_RUN_RECEIPT_INVALID`
- `WORKERBEE_CONTROLLED_RUN_RECEIPT_LIMIT`
- `WORKERBEE_CONTROLLED_RUN_RECOVERY_INCOMPLETE`

Configuration or capability failures occur before the fixed recipe starts. No
fallback is silent. Private adapter exceptions are normalized to stable
`WORKERBEE_CONTROLLED_RUN_ADAPTER_ERROR` or
`WORKERBEE_CONTROLLED_RUN_ADAPTER_TIMEOUT` envelopes containing only the
operation and exception type; private exception messages are not returned.
Start, status, cancel, timeout, and restart failures durably record bounded
recovery evidence and a cleanup postcondition. Receipt reads remain read-only.

## Platform-debug carry-forward

The private platform-debug distribution must provide the implementation behind
the `workerbee.controlled_run_adapter` entry point. That adapter must provide:

1. The exact fixed recipe registry and pinned tool-manifest digests.
2. Git-object verification and one immutable baseline/candidate export bundle.
3. A private namespace or equivalent strict appliance with loopback-only IP,
   pathname Unix sockets, no DNS/default route/external egress, and denial of
   abstract or host control sockets.
4. Empty-environment construction, explicit non-secret allowlisting, and no
   ambient credential or configuration inheritance.
5. Tool, runtime, image, profile, recipe, and source digest enforcement with no
   index/install/fallback path.
6. Kernel or appliance-enforced wall-time, memory, output, concurrency, and
   artifact limits.
7. Durable workspace ownership and crash recovery compatible with the standard
   WorkerBee state root.
8. Bounded, redacted evidence plus an exact cleanup postcondition and residue
   inventory for every terminal path.

The adapter must attest all required capabilities before WorkerBee advertises
the lifecycle tools. The OSS layer does not turn declarations into private
enforcement and does not treat a conventional container/network setup as the
strict appliance.

## Operational boundary

Controlled-run support does not authorize publication or external mutation. It
must not push, tag, create a release, invoke TestPyPI/PyPI or Trusted Publisher,
change repository settings, use ambient credentials, deploy user-facing
services, or mutate external systems. Any recipe needing such behavior is not a
valid controlled-run recipe.

To roll back, remove the explicit enable flag or the strict adapter. WorkerBee
then omits all five tools from capability advertisement and MCP registration
while retaining bounded journals and receipts for operator review.
