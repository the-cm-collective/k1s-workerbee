# K1S WorkerBee MCP

WorkerBee is a local MCP workbench for running a lightweight k1s stack while an agent builds and tests cloud-native applications.

The current POC wraps a sibling `../k1s` checkout without modifying it. It starts a host-process controller and API shim, exposes the k1s dashboard immediately, deploys a representative native k1s app stack, and exports Kubernetes YAML artifacts.

## Quickstart

```bash
python -m pip install -e .[dev]
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

By default WorkerBee looks for k1s at `../k1s`. Override with `WORKERBEE_K1S_ROOT=/path/to/k1s`.

`workerbee start` prints the dashboard URL immediately. The default local URL is
`http://127.0.0.1:19108/dashboard` when that port is free.

The MCP SDK is installed by the package dependency. In a fresh environment, use
`python -m pip install -e .` before running `workerbee mcp serve`.
