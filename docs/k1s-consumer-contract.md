# K1s v1 consumer contract

WorkerBee consumes the installed public contract of the exact declared
`k1s-core` distribution. It does not copy K1s implementation, discover an
ambient checkout, or infer behavior from an uncommitted K1s tree. The current
release contract is `k1s-core==1.0.0rc1`; changing that identity is a separate
coordinated release decision.

The WorkerBee-owned descriptor is
`workerbee.k1s-v1-consumer-contract/v1` in
`src/workerbee/k1s_consumer_contract.py`. It records what WorkerBee may consume,
not what a particular runtime has completed. Its qualification posture remains
`source-validated/unqualified` until a separately isolated installed-artifact
or live lane supplies stronger evidence.

## Portable profile roles

The compact installed profile catalog has four portable roles. WorkerBee's
public v1 consumer coverage focuses on `core`, `edge-gateway`, and `node`; the
installed `local` role remains available for local composition.

| Role | Compact component contract | WorkerBee claim |
| --- | --- | --- |
| `core` | `controller` | Installed contract and metadata-safe evidence source; not proof of a live controller. |
| `edge-gateway` | `gateway` | Installed contract and metadata-safe evidence source; bootstrap authority remains separate. |
| `node` | `node-agent` | Installed contract and metadata-safe evidence source; strict-CRI host authority remains separate. |

`k1s profile list`, `doctor`, and `plan` are installed-contract inspection.
WorkerBee invokes `doctor` without the optional engine probe. Portable `status`
and bounded `logs` read exact owned local state. The profile-evidence adapter
projects only allowlisted metadata for `list`, `doctor`, `plan`, `status`, and
`logs`; it never returns raw logs, source JSON, credentials, endpoints, paths,
environment, executable identity, sockets, or raw run IDs.

These contracts establish command and schema compatibility. The descriptor
does not qualify live runtime behavior, transport, deployment readiness, or
production fitness.

## Bootstrap boundary

`k1s profile bootstrap create` is an explicit mutating CLI for `edge-gateway`
and `node`. WorkerBee does not expose bootstrap creation through its CLI or MCP
profile-evidence surface. The internal `bootstrap` adapter operation is
pure-projection-only: it may sanitize an already selected public bootstrap
projection supplied by an independently authorized workflow, but it cannot
create, discover, or persist bootstrap state.

## Strict-CRI host transaction boundary

`k1s profile host plan strict-cri` is a pure, digest-pinned planning surface.
It consumes explicit intent and facts and does not probe or mutate the host.
It remains a direct CLI contract and is not a WorkerBee profile-evidence MCP
operation.

`k1s profile host prepare strict-cri` and
`k1s profile host rollback strict-cri` are explicit mutating host transactions.
They require exact transaction and evidence identities, confirmation, and a
separately authorized pre-opened helper boundary. WorkerBee's public evidence
consumer neither launches that helper nor gains prepare, rollback, recovery,
or host-mutation authority from the existence of these commands.

## Advanced `ae` surface

The exact distribution also installs `ae`, `ae-gateway`, `ae-node`,
`ae-rotate-certs`, and `ae-worker-stub`. WorkerBee uses an advanced `ae` entry
point only when the compact `k1s` CLI has no equivalent and only through an
explicitly documented direct workflow. There is no ambient fallback from a
failed or unknown `k1s` command to `ae`, and these entry points are not exposed
through `workerbee_v1_k1s_profile_evidence`.

## Evidence levels

- **Installed contract** proves an exact packaged command, catalog, or schema
  is present.
- **Source validated** proves deterministic source or fixture behavior without
  claiming a live engine, provider, transport, or workload.
- **Installed-artifact qualified** additionally requires a clean environment
  outside both source checkouts with no index or source fallback.
- **Deferred live** identifies behavior that needs separately authorized
  runtime or host evidence.

Optional Agent PBX integration may consume the same bounded receipts, but it
does not change WorkerBee's authority. A campaign receipt, capability object,
or source-level test is never a substitute for the evidence level named by the
claim.
