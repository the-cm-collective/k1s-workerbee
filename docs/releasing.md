# WorkerBee Python release runbook

This repository publishes the `k1s-workerbee` distribution. Its import package
and command remain `workerbee`, and its k1s compatibility probe continues to
import `ae`.

## Current provider lane

Release 0.1.7 is deliberately frozen on the legacy requirement
`k1s-workerbee-runtime==0.1.7`. The release contract in `pyproject.toml`, the
runtime provider guard, artifact audit, wheelhouse audit, and tests all require
that to be the only installed distribution advertising the `ae` package.
The MCP SDK remains on `mcp>=1.8,<2` because WorkerBee's current server contract
uses the 1.x `FastMCP` API; a 2.x migration must be a separate compatibility
change rather than an implicit dependency upgrade.

The replacement k1s-core artifact is a separate release gate. Do not change the
provider lane until the accepted artifact supplies all of the following:

- the canonical distribution name and exact version;
- the exact wheel filename and supported Python/platform tags;
- the wheel SHA256 from the accepted build;
- evidence that the wheel provides the existing `ae`, `ae.apishim`, `ae.cli`,
  and `ae.controller` imports WorkerBee uses.

The integration point is `[project].dependencies` together with
`[tool.workerbee.release]` in `pyproject.toml` and the constants in
`src/workerbee/runtime_provider.py`. Change those in one reviewed commit, add
the accepted filename/SHA to the release validation contract, rebuild the
wheelhouse, and rerun every command below. Never leave both legacy and new
provider requirements active.

## Build and local validation

Use Python 3.11 or newer and build from a clean commit. The candidate builder
does not publish:

```bash
python3.11 -m pip install \
  --index-url https://pypi.org/simple \
  build==1.3.0 setuptools==80.9.0 twine==6.2.0 wheel==0.45.1
PYTHON_BIN=python3.11 scripts/build_release_candidate.sh --out dist/pypi
python3.11 scripts/audit_release_artifacts.py verify-manifest \
  --manifest dist/pypi/RELEASE-MANIFEST.json \
  --directory dist/pypi
```

For reproducibility, build twice from the same commit with the same
`SOURCE_DATE_EPOCH` and verify the first manifest against the second directory.
The wheel and sdist filenames, sizes, and SHA256 values must match exactly.

The wheelhouse builder materializes third-party dependency wheels from one
explicit index, builds the frozen legacy runtime wheel locally, builds WorkerBee without
dependency or build-isolation resolution, validates that only one wheel
provides `ae`, proves pip can resolve the complete wheelhouse with
`--no-index`, writes an internal manifest, and creates a normalized archive.
The installer requires the external `SHA256SUMS`, verifies the internal
manifest, and never falls back to an index.

## Staged trusted publishing

Both workflows are manual-only and default to validation without publication:

1. `release-testpypi.yml` builds the wheel and sdist twice, requires identical
   manifests, uploads one exact-byte candidate bundle, and publishes only when
   its `publish` input is explicitly true. The publish job uses the `testpypi`
   environment and OIDC trusted publishing.
2. `release-pypi.yml` accepts the successful TestPyPI workflow run ID. It never
   builds. It downloads that candidate, verifies the manifest and archive
   hygiene, downloads the TestPyPI files by their JSON API URLs, and compares
   exact SHA256 values. Production publication is separately gated by its
   `publish` input and the `pypi` environment, again using OIDC.

Do not add API tokens, index passwords, `skip-existing`, automatic tag/release
triggers, or a secondary dependency index. Repository environments and trusted
publisher configuration are operator-managed and outside this source change.

## Rollback

Before publication, discard the candidate bundle and fix the source; no package
index state has changed. After TestPyPI publication, keep production gated,
prepare a new version, and rerun the TestPyPI path. After PyPI publication,
Python package files are immutable: do not replace or delete them as a normal
rollback. Restore behavior with a new patch release that re-establishes the
last accepted provider contract, then promote that new exact-byte candidate.

If the future provider migration fails before publication, revert the single
integration commit so the dependency, release contract, and runtime guard all
return to `k1s-workerbee-runtime==0.1.7`. Confirm a clean no-index wheelhouse
install and the CLI/MCP smoke tests before considering another candidate.
