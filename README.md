# K1S WorkerBee MCP

WorkerBee is a local MCP workbench for running lightweight k1s stacks while agents build and test cloud-native applications.

The current POC can use an installed `k1s-workerbee-runtime` wheel or a sibling `../k1s` checkout without modifying k1s. It starts host-process controllers and API shims, exposes project dashboards, provides a global MCP dashboard, deploys a representative native k1s app stack, exposes app ingress through local HTTPS, and exports Kubernetes YAML artifacts.

## Quickstart

Install WorkerBee:

```bash
curl -fsSL https://github.com/the-cm-collective/k1s-workerbee/releases/latest/download/install-workerbee.sh | sh
```

The installer uses the currently active Python virtual environment when `VIRTUAL_ENV` is set. If no venv is active, it creates a standalone WorkerBee venv under `${XDG_DATA_HOME:-~/.local/share}/workerbee/venv` and writes a `workerbee` wrapper to `~/.local/bin`. If that directory is not on `PATH`, the installer prints the exact `export PATH=...` line to add.

WorkerBee does not install Podman or Docker. It detects the runtime and prints guidance when neither is available.

Run the MCP server:

```bash
workerbee mcp serve
```

The MCP daemon prints the global dashboard URL, normally
`https://dashboard.workerbee.localhost:19443/`.

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

Useful daemon commands:

```bash
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
`http://127.0.0.1:19108/dashboard` when that port is free. `workerbee mcp serve` additionally prints the global dashboard URL, normally `https://dashboard.workerbee.localhost:19443/`.

When a project deploys app ingress through the MCP daemon, WorkerBee scopes hosts under the project name, for example `https://app.default.workerbee.localhost:19443/` and `https://api.default.workerbee.localhost:19443/`. Caddy terminates TLS with its local internal CA. WorkerBee never installs that CA implicitly; run `workerbee trust install` only when you explicitly want the local CA added to system/user trust stores.

The MCP SDK is installed by the package dependency. In a source checkout, build the
wheelhouse first or provide equivalent dependency links before running `workerbee mcp serve`.

Uninstall standalone WorkerBee:

```bash
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/workerbee/venv" "$HOME/.local/bin/workerbee"
```

If installed into an active venv, uninstall with `python -m pip uninstall k1s-workerbee`.
