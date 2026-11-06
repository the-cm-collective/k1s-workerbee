# Native-direct interoperability

WorkerBee OSS operates directly against the trusted host checkout supplied to
`workerbee_v1_session_start(cwd=...)`. This is the normal interactive WorkerBee
path: it preserves the complete local MCP surface, including project lifecycle,
status, logs, exec, image build, manifest prepare/validate/deploy, and ingress.

This contract is separate from the strict controlled-run contract. A trusted
native binding is not an isolation appliance and cannot satisfy the
controlled-run capability gate.

## Orthogonal authority vocabulary

`workerbee_v1_capabilities.data.native_interoperability` exposes the source and
execution axes owned by WorkerBee while declaring, without selecting, the
independent Agent PBX access-profile authority:

| Axis | Value | OSS status | Owner |
| --- | --- | --- | --- |
| source | `native-direct` | supported | WorkerBee OSS |
| source | `snapshot-import` | unsupported | private platform-debug adapter |
| source | `managed-clone` | unsupported | private platform-debug adapter |
| source binding | `trusted-direct-bind` | unsupported | private platform-debug adapter |
| execution lane | `native-local` | supported | WorkerBee OSS |
| execution lane | `strict-appliance` | unsupported | private platform-debug adapter |

Access profiles are a separate Agent PBX-owned axis. WorkerBee neither claims
nor selects profiles such as `local-deploy` or `trusted-workbench`; the
descriptor records `owner="agent-pbx"`, `workerbee_selects=false`, and
`selected=null` only.

The OSS default binding is therefore:

```json
{
  "source_mode": "native-direct",
  "execution_lane": "native-local"
}
```

Private source and execution modes are recognized so Agent PBX and adapters can
negotiate a stable vocabulary. Their presence in the capability schema is not
an availability claim: each is marked `supported=false`. OSS contains no
snapshot importer, managed-clone controller, trusted-direct-bind helper,
private namespace launcher, PaaS scheduler, or strict-appliance implementation.

## Session binding receipt

The existing `workerbee_v1_session_start` response includes a
`source_binding` object with schema `workerbee.native-direct-binding/v1`. It is
deterministic from the existing session project identity and contains only:

- the source mode and execution-lane identities;
- the WorkerBee project/workspace identity;
- SHA-256 identities for the resolved checkout and Git root, while referring to
  the existing `cwd` and `git_root` response fields for canonical paths;
- the Git branch already present in the session identity;
- the helper identity and native-interoperability capability digest;
- references to existing response evidence fields; and
- explicit `snapshot_imported=false`, `managed_clone_created=false`,
  `trusted_direct_bind_created=false`, and `controlled_run_appliance=false`
  invariants.

The receipt deliberately has no cleanup or residue fields. A native-direct
binding does not create a snapshot, clone, source-binding helper, namespace, or
appliance that needs a separate cleanup lifecycle. It also does not accept
commands or environment values, does not contain or select an access profile,
and does not introduce a new MCP tool.

## Controlled-run boundary

The five controlled-run tools remain default-disabled and conditionally
registered. Their fixed-recipe validation, adapter capability checks, immutable
export attestation, idempotency, limits, evidence, recovery, and zero-residue
cleanup requirements are unchanged.

In particular:

- a native-direct receipt never advertises a strict adapter capability;
- `native-direct` never claims `trusted-direct-bind` or an Agent PBX access
  profile;
- the ordinary `workerbee_v1_exec` tool remains available under its existing
  native local project policy and is not accepted as controlled-run input;
- the controlled-run `argv` and environment prohibitions remain unchanged; and
- enabling native/direct interoperability does not advertise any controlled-run
  tool.

## Private platform-debug carry-forward

Private platform-debug remains responsible for any snapshot-import,
managed-clone, trusted-direct-bind, or strict-appliance path. An adapter
implementing those modes must supply its own source import/clone provenance,
network and namespace enforcement, empty-environment construction, exact
tool/runtime/profile pins,
bounded execution, durable recovery, evidence, and cleanup receipt. It must
enable the existing controlled-run gate only after every required strict
capability and fixed recipe is present.

No private launcher behavior should be copied into WorkerBee OSS. Agent PBX
should select the OSS native binding for trusted interactive work and select a
verified private adapter when immutable snapshot or strict-appliance semantics
are required.
