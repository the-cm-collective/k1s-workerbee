#!/usr/bin/env bash
set -euo pipefail

# Canonicalize generated wheel, manifest, and archive modes across callers.
# Release artifacts are public; a caller's restrictive umask must not change
# their exact bytes.
umask 022

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/dist/workerbee-wheelhouse"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
K1S_CORE_WHEEL="${WORKERBEE_K1S_CORE_WHEEL:-}"
WORKERBEE_WHEEL="${WORKERBEE_RELEASE_WHEEL:-}"
DEPENDENCY_WHEELHOUSE="${WORKERBEE_DEPENDENCY_WHEELHOUSE:-}"

usage() {
  cat <<'EOF'
usage: scripts/build_wheelhouse.sh --k1s-core-wheel PATH
                                    [--workerbee-wheel PATH]
                                    --dependency-wheelhouse PATH
                                    [--out PATH] [--python PYTHON]

Build a local wheelhouse containing:
  - the exact release-contract k1s-core==1.0.0rc2 wheel
  - k1s-workerbee

All transitive wheels must already be present in --dependency-wheelhouse.
Pass --workerbee-wheel to copy the already-audited release candidate into the
wheelhouse byte-for-byte. When omitted, the script builds a development wheel
from the current checkout.
The command never resolves from a package index and rejects any legacy or
alternate runtime-provider wheel in the dependency input.

Install with:
  python -m pip install --no-index --find-links <wheelhouse> k1s-workerbee
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --k1s-core-wheel)
      K1S_CORE_WHEEL="$2"
      shift 2
      ;;
    --dependency-wheelhouse)
      DEPENDENCY_WHEELHOUSE="$2"
      shift 2
      ;;
    --workerbee-wheel)
      WORKERBEE_WHEEL="$2"
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$K1S_CORE_WHEEL" || ! -f "$K1S_CORE_WHEEL" ]]; then
  echo "--k1s-core-wheel must name the accepted local wheel" >&2
  exit 2
fi
if [[ -z "$DEPENDENCY_WHEELHOUSE" || ! -d "$DEPENDENCY_WHEELHOUSE" ]]; then
  echo "--dependency-wheelhouse must name a local wheel-only dependency directory" >&2
  exit 2
fi
if [[ -n "$WORKERBEE_WHEEL" && ! -f "$WORKERBEE_WHEEL" ]]; then
  echo "--workerbee-wheel must name the accepted local WorkerBee wheel" >&2
  exit 2
fi
K1S_CORE_WHEEL="$(cd "$(dirname "$K1S_CORE_WHEEL")" && pwd)/$(basename "$K1S_CORE_WHEEL")"
if [[ -n "$WORKERBEE_WHEEL" ]]; then
  WORKERBEE_WHEEL="$(cd "$(dirname "$WORKERBEE_WHEEL")" && pwd)/$(basename "$WORKERBEE_WHEEL")"
fi
DEPENDENCY_WHEELHOUSE="$(cd "$DEPENDENCY_WHEELHOUSE" && pwd)"
OUT_PARENT="$(mkdir -p "$(dirname "$OUT_DIR")" && cd "$(dirname "$OUT_DIR")" && pwd)"
OUT_DIR="${OUT_PARENT}/$(basename "$OUT_DIR")"
rm -rf "$OUT_DIR" "${OUT_PARENT}/workerbee-wheelhouse.tar.gz" "${ROOT_DIR}/build"
mkdir -p "$OUT_DIR"

for forbidden in \
  "$DEPENDENCY_WHEELHOUSE"/k1s_core-*.whl \
  "$DEPENDENCY_WHEELHOUSE"/k1s_workerbee-*.whl \
  "$DEPENDENCY_WHEELHOUSE"/k1s_workerbee_runtime-*.whl; do
  if [[ -e "$forbidden" ]]; then
    echo "dependency input must not supply a runtime provider: $forbidden" >&2
    exit 1
  fi
done

export PIP_NO_INDEX=1
export PIP_CONFIG_FILE=/dev/null
export PYTHONDONTWRITEBYTECODE=1
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  verify-runtime-artifact \
  --artifact "$K1S_CORE_WHEEL"
"${PYTHON_BIN}" -m pip download \
  --no-index \
  --only-binary=:all: \
  --find-links "$DEPENDENCY_WHEELHOUSE" \
  --dest "$OUT_DIR" \
  "$K1S_CORE_WHEEL" \
  "mcp>=1.8,<2"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  verify-runtime-artifact \
  --artifact "$OUT_DIR/$(basename "$K1S_CORE_WHEEL")"
if [[ -n "$WORKERBEE_WHEEL" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" audit "$WORKERBEE_WHEEL"
  cp "$WORKERBEE_WHEEL" "$OUT_DIR/"
else
  "${PYTHON_BIN}" -m pip wheel \
    --no-index \
    --no-build-isolation \
    --no-deps \
    --find-links "$OUT_DIR" \
    -w "$OUT_DIR" \
    "$ROOT_DIR"
fi

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  verify-wheelhouse \
  --wheelhouse "$OUT_DIR"
VERIFY_DIR="${OUT_PARENT}/.workerbee-wheelhouse-verify"
rm -rf "$VERIFY_DIR"
"${PYTHON_BIN}" -m pip install \
  --ignore-installed \
  --no-compile \
  --no-index \
  --find-links "$OUT_DIR" \
  --target "$VERIFY_DIR" \
  "k1s-workerbee==1.0.0rc2"
rm -rf "$VERIFY_DIR"

cat >"${OUT_DIR}/INSTALL.txt" <<EOF
Install WorkerBee from this wheelhouse:

  python -m pip install --no-index --find-links <wheelhouse> k1s-workerbee

Accepted runtime provider:
  k1s-core==1.0.0rc2

Runtime requirements not bundled in wheels:
  - Podman or Docker available on PATH
EOF

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  write-sbom \
  --output "${OUT_DIR}/SBOM.cdx.json" \
  "$OUT_DIR"/*.whl
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  write-manifest \
  --output "${OUT_DIR}/RELEASE-MANIFEST.json" \
  "$OUT_DIR"/*.whl "$OUT_DIR"/INSTALL.txt "$OUT_DIR"/SBOM.cdx.json
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  verify-manifest \
  --manifest "${OUT_DIR}/RELEASE-MANIFEST.json" \
  --directory "$OUT_DIR"

echo "wheelhouse: ${OUT_DIR}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT_DIR" log -1 --format=%ct)}"
export SOURCE_DATE_EPOCH
tar \
  --sort=name \
  --mtime="@${SOURCE_DATE_EPOCH}" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  -C "$(dirname "$OUT_DIR")" \
  -cf - \
  "$(basename "$OUT_DIR")" \
  | gzip -n >"$(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  audit-archive \
  "$(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  write-sha256s \
  --output "$(dirname "$OUT_DIR")/SHA256SUMS" \
  "$(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz" \
  "${ROOT_DIR}/scripts/install-workerbee.sh"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  verify-sha256s \
  --checksums "$(dirname "$OUT_DIR")/SHA256SUMS" \
  "$(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz" \
  "${ROOT_DIR}/scripts/install-workerbee.sh"
echo "archive: $(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz"
echo "checksums: $(dirname "$OUT_DIR")/SHA256SUMS"
