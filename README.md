# K1S WorkerBee MCP

WorkerBee is a local MCP workbench for running a lightweight k1s stack while an agent builds and tests cloud-native applications.

The current POC wraps a sibling `../k1s` checkout without modifying it. It starts a host-process controller and API shim, exposes the k1s dashboard immediately, deploys a representative native k1s app stack, and exports Kubernetes YAML artifacts.

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

WorkerBee prefers an installed `k1s-workerbee-runtime` package. For source development it
falls back to a sibling k1s checkout at `../k1s`. Override with
`WORKERBEE_K1S_ROOT=/path/to/k1s`.

`workerbee start` prints the dashboard URL immediately. The default local URL is
`http://127.0.0.1:19108/dashboard` when that port is free.

The MCP SDK is installed by the package dependency. In a source checkout, build the
wheelhouse first or provide equivalent dependency links before running `workerbee mcp serve`.
