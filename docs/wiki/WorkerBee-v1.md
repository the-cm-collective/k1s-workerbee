# WorkerBee v1

WorkerBee is a local, project-scoped MCP workbench for building, validating,
deploying, inspecting, and exporting k1s-powered applications. This page is the
repository-hosted candidate for a public WorkerBee v1 wiki page. The normative
contract remains [the WorkerBee v1 public contract](../v1-contract.md).

## Release status

The stable release is `k1s-workerbee==1.0.0` with the exact published
dependency `k1s-core==1.0.0`. The release contract binds the provider wheel by
filename, size, and SHA-256 and requires deterministic artifact construction,
strict offline installation, and exactly one installed owner of the `ae`
package.

## Install choices

For a complete sealed wheelhouse, direct pip is the supported
environment-local path:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
PIP_NO_INDEX=1 PIP_CONFIG_FILE=/dev/null \
  python -m pip install --no-index \
  --find-links /path/to/workerbee-wheelhouse \
  'k1s-workerbee==1.0.0'
python -m pip check
workerbee doctor
```

Use `scripts/install-workerbee.sh` with the verified wheelhouse archive when
you need a standalone launcher, atomic activation, retention of the previous
environment, and `--rollback`. Neither path may combine `k1s-core` with the
legacy `k1s-workerbee-runtime` provider or fall back to a package index. See the
[release runbook](../releasing.md) for exact artifact and publishing gates.

## Public surface and authority

- **OSS native/direct:** `workerbee_v1_session_start(cwd=...)` binds the exact
  trusted checkout. The normal local build, deploy, ingress, logs, exec, status,
  security, export, and project lifecycle remain WorkerBee-owned.
- **K1s consumer:** `workerbee k1s-evidence` and
  `workerbee_v1_k1s_profile_evidence` expose bounded metadata for `list`,
  `doctor`, `plan`, `status`, and `logs`. Bootstrap creation and strict-CRI host
  mutation are not exposed by that surface.
- **Optional Agent PBX:** Agent PBX may compose public receipts and capability
  objects. It owns campaigns and access profiles; WorkerBee does not inject
  Agent PBX instructions into generic generated guidance.
- **Controlled runs:** the exact five-tool lifecycle is disabled by default.
  The OSS contract does not claim appliance enforcement or usability without
  an explicit policy and strict private adapter.
- **Private platform-debug:** snapshot import, managed clone,
  trusted-direct-bind, and strict appliance enforcement remain adapter-owned
  carry-forward. They are not implemented or advertised as available by OSS.

## Evidence boundary

The accepted C2 lane installed the exact WorkerBee and K1s release wheels twice from
one sealed 46-wheel set with `PIP_NO_INDEX=1`, `PIP_CONFIG_FILE=/dev/null`, and
explicit `--no-index`. Both runs occurred outside the source checkouts, proved
that neither checkout was importable, resolved package metadata and imports
inside each fresh environment, found only `k1s-core` as the `ae` owner, and
produced byte-identical canonical receipts. It exercised non-live
`list`/`doctor`/`plan` contracts and did not start a profile, provider, daemon,
service, socket, or workload.

That receipt does not establish live profile health, transport behavior,
strict-CRI host mutation, controlled-run appliance enforcement, deployment, or
production fitness. See the [K1s consumer contract](../k1s-consumer-contract.md)
for its exact command and artifact identities.

## Scenario status

| ID | Evidence | Status |
| --- | --- | --- |
| `WB-C2-INSTALL-001` | Exact installed package pair and non-live consumer commands | Installed-artifact qualified; no package/consumer defect. |
| `WB-D2-PROFILE-STATE-001` | Profile `status`/`logs` with owned local state | Deferred live pending terminal D0/D1 authority and an authorized local-state lane. |
| `WB-D2-STRICT-CRI-001` | Strict-CRI host transaction | Deferred live; K1s host authority and rollback evidence required. |
| `WB-D3-CONTROLLED-RUN-001` | Controlled-run appliance | Deferred private qualification; OSS provides the gated contract only. |
| `WB-D3-AGENT-PBX-001` | Optional Agent PBX composition | Deferred integration; Agent PBX remains the campaign/access-profile owner. |

These stable IDs are detailed in the [scenario matrix](../v1-contract.md#scenario-matrix).
A deferral is not a pass, and it is not silently converted into a defect. Any
new known package or consumer defect blocks readiness until repaired.

## More information

- [CLI reference](../cli-reference.md)
- [Controlled-run contract](../controlled-runs.md)
- [Native/direct interoperability](../native-direct-interoperability.md)
- [K1s consumer contract](../k1s-consumer-contract.md)
- [Release runbook](../releasing.md)
