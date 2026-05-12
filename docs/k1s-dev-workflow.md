# WorkerBee k1s Development Workflow

This workflow is for developing k1s with WorkerBee's nested profile harness. It
assumes the normal development checkout layout:

```text
k1s-wt/
  k1s-workerbee/
  k1s/
```

Run the WorkerBee control loop from `k1s-workerbee`, and use the sibling
`../k1s` checkout as the k1s source under test. WorkerBee mounts that checkout
read-only into profile containers and runs k1s from `/workspace/src`, so profile
restarts pick up k1s source changes without copying code into WorkerBee state.

## Operating Model

Use one agent from the `k1s-workerbee` repo root by default. That agent can edit
WorkerBee code in the current repo, edit k1s code under `../k1s`, and run local
tests in either checkout.

Use more than one agent only when the work is separable:

- One agent owns k1s behavior changes while another owns WorkerBee docs or
  harness changes.
- Each agent uses a distinct WorkerBee `project` value, or exactly one agent
  owns the shared profile lifecycle for a project such as `k1s-dev`.
- Agents do not concurrently purge, restart, or redeploy the same WorkerBee
  profile unless they have explicitly coordinated.

The risk in multi-agent work is shared runtime state, not filesystem access.
WorkerBee profile state, generated helper scripts, Caddy routes, and containerd
namespaces are project-scoped, so project names are the isolation boundary.

## Setup

Create or refresh the WorkerBee editable environment from `k1s-workerbee`:

```bash
scripts/build_wheelhouse.sh --k1s-root ../k1s --out dist/workerbee-wheelhouse
uv venv .venv
uv pip install -e '.[dev]' --find-links dist/workerbee-wheelhouse
```

Start the direct-containerd MCP daemon with the repo helper:

```bash
scripts/dev/wb-containerd mcp-restart
scripts/dev/wb-containerd mcp-status
```

For nested k1s profile work, use a short explicit stable project and k1s root.
Keep the project around 12 characters or fewer, such as `wb014` or `k1sdev`,
so generated profile DNS names and container labels stay valid:

```bash
scripts/dev/wb-containerd --project wb014 profile start \
  --profile k1s-dev-min-sqlite \
  --k1s-root ../k1s
```

For installed WorkerBee, the equivalent explicit form is:

```bash
workerbee --runtime containerd --containerd-privilege sudo-helper \
  --project wb014 profile start \
  --profile k1s-dev-min-sqlite \
  --k1s-root ../k1s
```

You can set `WORKERBEE_K1S_ROOT=/absolute/path/to/k1s` instead of passing
`--k1s-root`, but the project value should still be short and explicit for
shared dev lanes.

## Profile Selection

Use the smallest profile that exercises the behavior under test:

- `k1s-dev-min-sqlite`: fastest single-controller loop for controller, API
  shim, dashboard, and manifest behavior.
- `k1s-dev-etcd-labs`: single-controller loop with etcd state for persistence
  and state-backend behavior.
- `k1s-single-etcd-containerd`: etcd plus direct containerd workloads for
  runtime integration behavior.
- `k1s-ha-min`: three controllers, shared etcd, and NATS for HA, leadership,
  transport, and profile-workload validation.

Native k1s `make` profiles in `../k1s` remain useful for k1s-only validation,
especially strict CRI and core/edge lanes. WorkerBee profiles complement those
lanes by testing k1s through WorkerBee's staging, ingress, logs, probes, and
artifact export paths.

## Development Loop

For k1s behavior work:

```bash
cd ../k1s
python -m pytest tests/unit -q
cd ../k1s-workerbee
scripts/dev/wb-containerd --project wb014 profile stop
scripts/dev/wb-containerd --project wb014 profile start \
  --profile k1s-dev-min-sqlite \
  --k1s-root ../k1s
```

For staged workload validation through the nested profile:

```bash
scripts/dev/wb-containerd --project wb014 manifest prepare \
  --name realtime \
  --template realtime-web-db

scripts/dev/wb-containerd --project wb014 manifest deploy-local \
  --target profile \
  --profile k1s-ha-min \
  --k1s-root ../k1s \
  --stage realtime

scripts/dev/wb-containerd --project wb014 profile status --k1s-root ../k1s
scripts/dev/wb-containerd --project wb014 logs \
  --target profile \
  --profile k1s-ha-min \
  --app frontend
```

For one-command end-to-end validation:

```bash
scripts/dev/wb-containerd --project wb014 validate \
  --scenario profile-workload \
  --profile k1s-ha-min \
  --k1s-root ../k1s
```

That validation builds the bundled realtime image contexts, starts the selected
profile, deploys native k1s manifests into it, probes HTTPS app ingress,
verifies WebSocket behavior, checks workload status, and exports k1s,
Kubernetes, and Helm artifacts.

## Restart And State Rules

- After k1s source changes, restart the profile so controller and API shim
  Python processes reload `/workspace/src`.
- After WorkerBee source changes, restart MCP with
  `scripts/dev/wb-containerd mcp-restart`.
- After changing WorkerBee's generated helper bridge behavior, purge the profile
  because existing `workerbee-nerdctl` bridge scripts in WorkerBee state are not
  rewritten once created:

  ```bash
  scripts/dev/wb-containerd --project wb014 profile stop --purge
  ```

- Keep generated stages, helper scripts, profile databases, logs, and exported
  bundles in WorkerBee state unless the task explicitly asks for repo artifacts.
- Treat generated WorkerBee helper scripts as harness artifacts, not k1s source
  monkeypatches. They redirect containerd operations into WorkerBee-scoped
  namespaces and state-local CNI/data roots.

## Validation Ladder

Use this order when a change crosses the k1s and WorkerBee boundary:

1. k1s unit/static tests in `../k1s` for the touched subsystem.
2. WorkerBee unit tests in `k1s-workerbee` for changed profile, manifest, or
   runbook behavior.
3. `k1s-dev-min-sqlite` or `k1s-dev-etcd-labs` profile start/status for a fast
   control-plane smoke.
4. `k1s-single-etcd-containerd` when direct containerd workload behavior is in
   scope.
5. `profile-workload` against `k1s-ha-min` before treating the nested workflow
   as integrated.

Use native k1s strict CRI or core/edge profiles in `../k1s` when the change is
specifically about host CRI behavior, managed registry behavior, or multi-site
k1s topology. Use WorkerBee profiles when the question is whether WorkerBee can
stage, launch, inspect, probe, and export the k1s development runtime safely.
