# K1S WorkerBee MCP

WorkerBee is a local MCP workbench for running lightweight k1s stacks while agents build and test cloud-native applications.

The current POC can use an installed `k1s-workerbee-runtime` wheel or a sibling `../k1s` checkout without modifying k1s. It starts host-process controllers and API shims, exposes project dashboards, provides a global MCP dashboard, deploys a representative native k1s app stack, exposes app ingress through local HTTPS, and exports Kubernetes YAML artifacts.

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
`workerbee mcp serve` only when you want a foreground/debug process.

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
workerbee --runtime containerd mcp start
```

This path uses `nerdctl` with WorkerBee-scoped containerd namespaces and state-local data roots. It is intended for development hosts where no other WorkerBee/k1s process is using the same direct containerd runtime at the same time.

Other MCP clients can connect to the same Streamable HTTP endpoint if they support HTTP MCP. For example, Claude Code documents `claude mcp add --transport http workerbee http://127.0.0.1:8765/mcp`; Codex is the tested target for WorkerBee v0.1.

Uninstall standalone WorkerBee:

```bash
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/workerbee/venv" "$HOME/.local/bin/workerbee"
```

If installed into an active venv, uninstall with `python -m pip uninstall k1s-workerbee`.
