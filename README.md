# K1S WorkerBee MCP

WorkerBee is a local MCP workbench for running lightweight k1s stacks while agents build and test cloud-native applications.

The current POC can use an installed `k1s-workerbee-runtime` wheel or a sibling `../k1s` checkout without modifying k1s. The default app-stack workflow starts a lightweight local k1s workbench, exposes project dashboards, provides a global MCP dashboard, deploys a representative native k1s app stack, exposes app ingress through local HTTPS, and exports Kubernetes YAML artifacts. Advanced k1s profile workflows are direct-containerd only and containerize every k1s profile component.

## Quickstart

Install WorkerBee:

```bash
curl -fsSL https://github.com/the-cm-collective/k1s-workerbee/releases/latest/download/install-workerbee.sh | sh
```

The installer uses the currently active Python virtual environment when `VIRTUAL_ENV` is set. If no venv is active, it creates a standalone WorkerBee venv under `${XDG_DATA_HOME:-~/.local/share}/workerbee/venv` and writes a `workerbee` wrapper to `~/.local/bin`. If that directory is not on `PATH`, the installer prints the exact `export PATH=...` line to add.

WorkerBee does not install container runtimes. It uses Podman or Docker for the default workflow and supports explicit direct containerd development with `nerdctl`.

Start the background MCP daemon:

```bash
workerbee mcp start
```

The command prints the local MCP URL and the global dashboard URL, normally
`https://dashboard.workerbee.localhost:19443/`. Use `workerbee mcp status`,
`workerbee mcp restart`, and `workerbee mcp stop` for lifecycle management. Use
`workerbee mcp serve` only when you want a foreground/debug process. If the
requested MCP port is already in use, `workerbee mcp start` fails fast; stop the
owning process or pass `--port`.

Connect Codex to the shared local MCP server:

```bash
codex mcp add workerbee --url http://127.0.0.1:8765/mcp
codex mcp list
```

For cloud-native repos, add this project instruction to `AGENTS.md`:

```markdown
When a task involves containers, services, manifests, ingress, databases, queues,
or integration behavior, call WorkerBee MCP `workerbee_v1_session_start` with
the absolute repo cwd and task goal. Use the returned `project` for every
WorkerBee tool call. Use the local shell for repo edits and ordinary tests, and
use WorkerBee MCP for local image builds, manifest staging/deploy, status, logs,
HTTPS ingress probes, dashboard URLs, cleanup, and artifact export.
```

For local release testing against the internal Gitea release assets:

```bash
WORKERBEE_INSTALL_BASE_URL=https://gitea.core.home.arpa/m4xx3d0ut/k1s-workerbee/releases/download/v0.1.0 \
  sh -c "$(curl -fsSL https://gitea.core.home.arpa/m4xx3d0ut/k1s-workerbee/releases/download/v0.1.0/install-workerbee.sh)"
```

Build and install from a local wheelhouse:

```bash
scripts/build_wheelhouse.sh --k1s-root ../k1s --out dist/workerbee-wheelhouse
python -m venv .venv
. .venv/bin/activate
python -m pip install --no-index --find-links dist/workerbee-wheelhouse k1s-workerbee
workerbee doctor
```

Source checkout workflow:

```bash
python -m pip install -e .[dev] --find-links dist/workerbee-wheelhouse
workerbee doctor
workerbee start
workerbee deploy-poc
workerbee poc-status
workerbee logs api
workerbee export-k8s
workerbee stop --purge
```

The MCP daemon is intentionally shared. Multiple coding agents can connect to the same local MCP server URL and operate on separate project scopes by passing distinct `project` values to WorkerBee tools. The daemon stores those projects under a global state root, defaults to `WORKERBEE_HOME`, then `$XDG_DATA_HOME/workerbee`, then `~/.local/share/workerbee`, and exposes a global dashboard as soon as MCP starts.

Agents should start each repo session with `workerbee_v1_session_start`. WorkerBee derives a stable project id from the Git repository name plus the current branch plus a cwd hash, persists the cwd for that project, and returns dashboard URLs plus the cloud-native runbook. This lets two Codex sessions share one MCP daemon while still building and deploying against the correct checkout and branch. Outside Git, WorkerBee uses the cwd basename plus the cwd hash. Explicit `--project` or MCP `project` values override this derived identity.

For example, in `~/git/k1s` on branch `dev`, WorkerBee derives a project similar to `k1s-dev-190438baf7`. App ingress is scoped under that project:

```text
https://app.k1s-dev-190438baf7.workerbee.localhost:19443/
https://api.k1s-dev-190438baf7.workerbee.localhost:19443/
```

Project mode controls whether WorkerBee starts for a repo:

```bash
workerbee project status
workerbee project mode lazy
workerbee project mode start --open
workerbee project mode stop
```

`lazy` is the default and starts the k1s stack only when deploy/start is requested. `start` starts immediately and persists for future Codex sessions. `stop` stops the project and makes WorkerBee MCP return `PROJECT_STOPPED` for start/deploy/runtime operations until the mode is changed.

Useful daemon commands:

```bash
workerbee mcp status
workerbee mcp stop
workerbee projects
workerbee global-dashboard
workerbee ingress status
workerbee trust status
```

`workerbee mcp stop` stops the background MCP daemon and its global
dashboard/Caddy ingress container. Project stacks remain controlled with
`workerbee stop`, `workerbee project mode stop`, or the MCP project stop/reset
tools.

The global dashboard also provides token-protected local controls to start, stop,
or delete one project, selected projects, or all known projects. Start can bring
a saved project stack back up outside the original agent session. Delete means
stop, purge project runtime/state, unregister the project from the global
dashboard, and resync WorkerBee Caddy imports. The dashboard can also request
MCP shutdown or an in-place MCP reboot.

Staged deployment workflow:

```bash
workerbee manifest prepare --name demo --template frontend-api-store
workerbee manifest validate --stage ~/.local/share/workerbee/projects/default/artifacts/staged/demo
workerbee manifest deploy-local --stage ~/.local/share/workerbee/projects/default/artifacts/staged/demo
workerbee bundle export --stage ~/.local/share/workerbee/projects/default/artifacts/staged/demo --format k1s
```

`manifest prepare --source <file-or-dir>` can stage native k1s YAML or practical Kubernetes YAML. Kubernetes input is applied through the k1s shim `ae apply --k8s` path and should keep exactly one workload plus matching Service/Ingress documents per file. Native k1s manifests are the required input when exporting a native k1s bundle; Kubernetes input can be exported as Kubernetes YAML or a Helm skeleton.

WorkerBee prefers an installed `k1s-workerbee-runtime` package. For source development it
falls back to a sibling k1s checkout at `../k1s`. Override with
`WORKERBEE_K1S_ROOT=/path/to/k1s`.

`workerbee start` prints the project k1s dashboard URL immediately. The default local URL is
`http://127.0.0.1:19108/dashboard` when that port is free. `workerbee mcp start` additionally prints the global dashboard URL, normally `https://dashboard.workerbee.localhost:19443/`.

When a project deploys app ingress through the MCP daemon, WorkerBee scopes hosts under the project name, for example `https://app.default.workerbee.localhost:19443/` and `https://api.default.workerbee.localhost:19443/`. Caddy terminates TLS with its local internal CA. WorkerBee never installs that CA implicitly; run `workerbee trust install` only when you explicitly want the local CA added to system/user trust stores.

The MCP SDK is installed by the package dependency. In a source checkout, build the
wheelhouse first or provide equivalent dependency links before running `workerbee mcp start`.

For container-orchestration development where Podman or Docker would interfere with the system under test, run WorkerBee against direct containerd explicitly:

```bash
workerbee --runtime containerd --containerd-privilege sudo-helper mcp start
```

Containerized k1s profiles are only available in this direct-containerd mode. They are intended for advanced k1s development, not ordinary app-stack use. WorkerBee currently ships these built-in profiles:

```text
k1s-dev-min-sqlite          1 controller + API shim/dashboard + sqlite
k1s-dev-etcd-labs           1 controller + API shim/dashboard + etcd
k1s-single-etcd-containerd  1 controller + API shim/dashboard + etcd + direct containerd workloads
k1s-ha-min                  3 controllers + API shim/dashboard + shared etcd + shared NATS
```

No k1s profile starts a k1s controller, API shim, etcd, NATS, or dashboard as a host process. Use:

```bash
workerbee --runtime containerd profile list
workerbee --runtime containerd --project k1s-dev profile start --profile k1s-ha-min --k1s-root ../k1s
workerbee --runtime containerd --project k1s-dev profile status
workerbee --runtime containerd --project k1s-dev validate --scenario k1s-profile --profile k1s-ha-min --k1s-root ../k1s
workerbee --runtime containerd --project k1s-dev profile stop --purge
```

When run through the MCP daemon, profile controller/API ingress is published under the project namespace, for example `https://k1s.k1s-dev.workerbee.localhost:19443/dashboard` and `https://k1s-api.k1s-dev.workerbee.localhost:19443/`.

The canonical profile controller URL is now `https://k1s.<project>.workerbee.localhost:19443/`.
It exposes dashboard and docs paths such as:

```text
https://k1s.k1s-dev.workerbee.localhost:19443/dashboard
https://k1s.k1s-dev.workerbee.localhost:19443/docs
https://k1s.k1s-dev.workerbee.localhost:19443/redoc
https://k1s-api.k1s-dev.workerbee.localhost:19443/
```

`k1s-dash.<project>.workerbee.localhost` remains a compatibility alias for the
dashboard. WorkerBee profile workload operations use the project profile's
internal admin token and WorkerBee's generated CA to call the exposed profile API;
tokens are not returned in MCP or CLI results.

Profile workload deploy/status/log/validate operations require the background
MCP daemon for project-scoped Caddy ingress. To exercise the
app-engine-in-app-engine path, start MCP, stage a realtime frontend/backend/db
bundle, and deploy it into a running profile:

```bash
workerbee --runtime containerd --containerd-privilege sudo-helper mcp start
workerbee --runtime containerd --project k1s-dev profile start --profile k1s-ha-min --k1s-root ../k1s
workerbee --runtime containerd --project k1s-dev manifest prepare --name realtime --template realtime-web-db
workerbee --runtime containerd --project k1s-dev manifest deploy-local \
  --target profile \
  --profile k1s-ha-min \
  --stage .workerbee/k1s-dev/artifacts/staged/realtime
workerbee --runtime containerd --project k1s-dev profile status
```

The MCP equivalents are `workerbee_v1_manifest_prepare`,
`workerbee_v1_manifest_deploy_local(target="profile")`,
`workerbee_v1_profile_workload_status`, and `workerbee_v1_logs(target="profile")`.
For a single end-to-end validation, use:

```bash
workerbee --runtime containerd --project k1s-dev validate \
  --scenario profile-workload \
  --profile k1s-ha-min \
  --k1s-root ../k1s
```

That validation builds the bundled realtime image contexts, starts the requested
k1s profile, deploys native k1s manifests into the profile through the
project-scoped API, checks dashboard/docs/API health, probes HTTPS app ingress,
verifies a WebSocket echo path, collects workload status, and exports k1s,
Kubernetes, and Helm handoff artifacts.

This path uses `nerdctl` against the configured containerd socket, but scopes WorkerBee work into state-root-hashed namespaces such as `workerbee-<state-hash>-system` and `workerbee-<state-hash>-<project>`. It also uses state-local nerdctl data roots, state-local CNI config directories, and state-hash-scoped project networks. WorkerBee must never target reserved namespaces such as `ae`, `k8s.io`, `moby`, or `default`; those may belong to a real k1s/Kubernetes runtime on the same host.

When `--runtime containerd` is explicitly selected, WorkerBee MCP defaults to `--containerd-privilege auto`. Auto first tries unprivileged `nerdctl`; if that fails, WorkerBee prompts once with `sudo` and starts a state-scoped root helper. The helper exposes a WorkerBee-owned Unix socket and a generated `nerdctl` wrapper under the WorkerBee state root, validates every command, and only allows WorkerBee state-hash namespaces plus state-local data/CNI paths. This never runs for `--runtime auto`, Docker, or Podman. Use `--containerd-privilege unprivileged` if you preconfigured rootless/system access yourself.

Inspect or stop the helper with:

```bash
workerbee --runtime containerd containerd-privilege status
workerbee --runtime containerd containerd-privilege stop-helper
```

Shared host containerd is still a privileged development mode. WorkerBee avoids broad prune operations and cleanup is constrained to WorkerBee state-hash namespaces, but Podman or Docker remains the safer default for ordinary users.

Other MCP clients can connect to the same Streamable HTTP endpoint if they support HTTP MCP. For example, Claude Code documents `claude mcp add --transport http workerbee http://127.0.0.1:8765/mcp`; Codex is the tested target for WorkerBee v0.1.

Uninstall standalone WorkerBee:

```bash
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/workerbee/venv" "$HOME/.local/bin/workerbee"
```

If installed into an active venv, uninstall with `python -m pip uninstall k1s-workerbee`.

## macOS Notes

macOS support is best effort for v0.1 until validated on physical hosts. Install
Python 3.11 or newer, use Docker Desktop or Podman, run the one-line installer,
then verify with `workerbee doctor`. To trust the local WorkerBee Caddy CA after
`workerbee mcp start`, run:

```bash
workerbee trust install --target system
```
