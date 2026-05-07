# K1S WorkerBee MCP

WorkerBee is a local MCP workbench for running lightweight k1s stacks while agents build and test cloud-native applications.

The current POC can use an installed `k1s-workerbee-runtime` wheel or a sibling `../k1s` checkout without modifying k1s. It starts host-process controllers and API shims, exposes project dashboards, provides a global MCP dashboard, deploys a representative native k1s app stack, exposes app ingress through local HTTPS, and exports Kubernetes YAML artifacts.

## Quickstart

Install from a local wheelhouse:

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

Run the MCP server:

```bash
workerbee mcp serve
```

The MCP daemon is intentionally shared. Multiple coding agents can connect to the same local MCP server URL and operate on separate project scopes by passing distinct `project` values to WorkerBee tools. The daemon stores those projects under a global state root, defaults to `WORKERBEE_HOME`, then `$XDG_DATA_HOME/workerbee`, then `~/.local/share/workerbee`, and exposes a global dashboard as soon as MCP starts.

Useful daemon commands:

```bash
workerbee projects
workerbee global-dashboard
workerbee ingress status
workerbee trust status
```

WorkerBee prefers an installed `k1s-workerbee-runtime` package. For source development it
falls back to a sibling k1s checkout at `../k1s`. Override with
`WORKERBEE_K1S_ROOT=/path/to/k1s`.

`workerbee start` prints the project k1s dashboard URL immediately. The default local URL is
`http://127.0.0.1:19108/dashboard` when that port is free. `workerbee mcp serve` additionally prints the global dashboard URL, normally `https://dashboard.workerbee.localhost:19443/`.

When a project deploys app ingress through the MCP daemon, WorkerBee scopes hosts under the project name, for example `https://app.default.workerbee.localhost:19443/` and `https://api.default.workerbee.localhost:19443/`. Caddy terminates TLS with its local internal CA. WorkerBee never installs that CA implicitly; run `workerbee trust install` only when you explicitly want the local CA added to system/user trust stores.

The MCP SDK is installed by the package dependency. In a source checkout, build the
wheelhouse first or provide equivalent dependency links before running `workerbee mcp serve`.
