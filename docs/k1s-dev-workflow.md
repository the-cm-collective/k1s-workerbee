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

When rebuilding WorkerBee itself and restarting the normal MCP daemon, use the
clean stop, rebuild, force-reinstall, sudo-refresh, start sequence:

```bash
workerbee mcp stop
scripts/build_wheelhouse.sh --k1s-root ../k1s --out dist/workerbee-wheelhouse
uv pip install --python .venv -e '.[dev]' --find-links dist/workerbee-wheelhouse --force-reinstall
sudo -v && workerbee mcp start
```

Stopping first prevents the daemon from serving old in-process code. The
`--force-reinstall` install refreshes WorkerBee against the rebuilt local
wheelhouse, and `sudo -v` refreshes credentials before direct-containerd
`sudo-helper` startup.

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
- `k1s-edge-link`: advanced external-core lane that runs a WorkerBee-scoped
  edge gateway and edge node against an already running k1s core.

Native k1s `make` profiles in `../k1s` remain useful for k1s-only validation,
especially strict CRI and core/edge lanes. WorkerBee profiles complement those
lanes by testing k1s through WorkerBee's staging, ingress, logs, probes, and
artifact export paths.

## External Edge Link

Use `edge-link` when the local host needs to connect to an external k1s core as
a short-lived edge site. The current dev target is the sibling `../k1s`
MicroK8s HA stack:

```bash
scripts/dev/wb-containerd --project wb014 edge-link start \
  --k1s-root ../k1s \
  --from-microk8s \
  --release k1s-dev-a \
  --namespace k1s-dev-a \
  --site-id workerbee-edge \
  --node-id workerbee-edge-node
```

The same runner is also exposed as the advanced profile `k1s-edge-link`:

```bash
scripts/dev/wb-containerd --project wb014 profile start \
  --profile k1s-edge-link \
  --k1s-root ../k1s \
  --from-microk8s \
  --release k1s-dev-a \
  --namespace k1s-dev-a
```

Edge-link uses WorkerBee's direct-containerd namespace, data root, and CNI
paths. It does not use the MicroK8s containerd socket or MicroK8s CNI paths.
The node agent is published on a LAN-reachable host port so the external core
can heartbeat back to the local host. Pass `--advertise-host` if WorkerBee
cannot infer the host address.

The external core must be configured to accept the chosen site ID. For the
MicroK8s dev stack, add the WorkerBee site to the Helm release before starting
the link:

```bash
helm -n k1s-dev-a upgrade k1s-dev-a ../k1s/ops/helm/k1s-core-ha \
  --reuse-values \
  --set 'controller.siteIds[0]=host-a' \
  --set 'controller.siteIds[1]=workerbee-edge'
```

On hosts where the MicroK8s API restarts during this path, wait for all
`k1s-dev-a` pods and `http://k1s-dev-a.core.home.arpa:9110/healthz` to become
healthy again before judging the link. `edge-link start --timeout` waits for a
fresh post-start node heartbeat so short restart windows do not produce a false
success.

Validation includes node heartbeat and GPU advertisement. When NVIDIA tooling
is present, `edge-link validate` runs a real `runtimeClassName: nvidia` smoke:

```bash
scripts/dev/wb-containerd --project wb014 edge-link validate \
  --k1s-root ../k1s \
  --from-microk8s \
  --release k1s-dev-a \
  --namespace k1s-dev-a
```

### AI Max Edge Cell Simulation

The default `k1s-edge-link` path remains the legacy one-node simulation: one
gateway component and one node-agent component using `--node-id` as the gateway
compute node.

To simulate the corrected AI Max edge-cell contract locally, opt in with three
additional cell nodes:

```bash
scripts/dev/wb-containerd --project wb014 edge-link start \
  --k1s-root ../k1s \
  --from-microk8s \
  --release k1s-dev-a \
  --namespace k1s-dev-a \
  --site-id workerbee-edge \
  --node-id workerbee-edge-node \
  --cell-node-count 3
```

The resulting WorkerBee state exposes an `edge_cell_contract` block with:

- `profile: ai-max-edge-cell-v1`
- `size: 4`
- `fabric_cell_count: 1`
- `fabric_size: 4`
- `gateway_discovery.mode: lan-local`
- `lan_scope: workerbee-lan`
- `gateway_node_id: workerbee-edge-node`
- three deterministic cell node IDs:
  `workerbee-edge-node-cell-1` through `workerbee-edge-node-cell-3`
- `compute_node_ids` containing all four nodes
- per-member labels with `role` set to `gateway` or `cell-node` and
  `compute_eligible: true`

The simulation starts one gateway component, one gateway node-agent component,
and three additional node-agent components. The legacy `node_id` and
`agent_endpoint` fields still refer to the gateway compute node so existing
edge-link consumers continue to work.

To simulate a local LAN fabric without requiring a real LAN, keep the same
four-node cell shape and add a supported fabric size:

```bash
scripts/dev/wb-containerd --project wb014 edge-link start \
  --k1s-root ../k1s \
  --from-microk8s \
  --release k1s-dev-a \
  --namespace k1s-dev-a \
  --site-id workerbee-edge \
  --node-id workerbee-edge-node \
  --cell-node-count 3 \
  --fabric-cell-count 4 \
  --lan-scope floor-a
```

`--fabric-cell-count` accepts `1`, `2`, `4`, or `8`. WorkerBee records
deterministic peer gateway IDs, per-cell node IDs, `gateway_peer_ids`, and
`compute_node_ids` across the simulated fabric. The simulator metadata is a
contract fixture for k1s discovery and scheduling tests; it does not perform
real LAN discovery or mutate an external cluster by itself.

Stop and optionally purge the short-lived link when the test is complete:

```bash
scripts/dev/wb-containerd --project wb014 edge-link stop --purge
```

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
- After WorkerBee source changes that are only interpreted from the editable
  checkout, restart MCP with `scripts/dev/wb-containerd mcp-restart`.
- After rebuilding WorkerBee packages or the local k1s runtime wheelhouse, use
  the clean MCP stop, wheelhouse build, `uv pip install --force-reinstall`, and
  `sudo -v && workerbee mcp start` sequence from Setup.
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
