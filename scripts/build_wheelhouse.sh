#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K1S_ROOT="${K1S_ROOT:-${ROOT_DIR}/../k1s}"
OUT_DIR="${ROOT_DIR}/dist/workerbee-wheelhouse"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
DEPENDENCY_INDEX_URL="${WORKERBEE_DEPENDENCY_INDEX_URL:-https://pypi.org/simple}"

usage() {
  cat <<'EOF'
usage: scripts/build_wheelhouse.sh [--k1s-root PATH] [--out PATH] [--python PYTHON]
                                     [--dependency-index-url URL]

Build a local wheelhouse containing:
  - k1s-workerbee-runtime==0.1.7 (frozen legacy provider lane)
  - k1s-workerbee

Install with:
  python -m pip install --no-index --find-links <wheelhouse> k1s-workerbee
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --k1s-root)
      K1S_ROOT="$2"
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
    --dependency-index-url)
      DEPENDENCY_INDEX_URL="$2"
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

K1S_ROOT="$(cd "$K1S_ROOT" && pwd)"
OUT_PARENT="$(mkdir -p "$(dirname "$OUT_DIR")" && cd "$(dirname "$OUT_DIR")" && pwd)"
OUT_DIR="${OUT_PARENT}/$(basename "$OUT_DIR")"
rm -rf "$OUT_DIR" "${OUT_PARENT}/workerbee-wheelhouse.tar.gz" "${ROOT_DIR}/build"
mkdir -p "$OUT_DIR"

"${PYTHON_BIN}" "${K1S_ROOT}/scripts/build_workerbee_runtime_wheel.py" --out "$OUT_DIR"
RUNTIME_WHEELS=("$OUT_DIR"/k1s_workerbee_runtime-*.whl)
if [[ ${#RUNTIME_WHEELS[@]} -ne 1 || ! -f "${RUNTIME_WHEELS[0]}" ]]; then
  echo "expected exactly one frozen legacy runtime wheel in $OUT_DIR" >&2
  exit 1
fi
"${PYTHON_BIN}" -m pip wheel \
  --index-url "$DEPENDENCY_INDEX_URL" \
  --wheel-dir "$OUT_DIR" \
  "${RUNTIME_WHEELS[0]}" \
  "mcp>=1.8,<2"
"${PYTHON_BIN}" -m pip wheel \
  --no-index \
  --no-build-isolation \
  --no-deps \
  --find-links "$OUT_DIR" \
  -w "$OUT_DIR" \
  "$ROOT_DIR"

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
  "k1s-workerbee==0.1.7"
rm -rf "$VERIFY_DIR"

cat >"${OUT_DIR}/INSTALL.txt" <<EOF
Install WorkerBee from this wheelhouse:

  python -m pip install --no-index --find-links ${OUT_DIR} k1s-workerbee

Runtime requirements not bundled in wheels:
  - Podman or Docker available on PATH
EOF

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  write-manifest \
  --output "${OUT_DIR}/RELEASE-MANIFEST.json" \
  "$OUT_DIR"/*.whl "$OUT_DIR"/INSTALL.txt

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
  write-sha256s \
  --output "$(dirname "$OUT_DIR")/SHA256SUMS" \
  "$(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz" \
  "${ROOT_DIR}/scripts/install-workerbee.sh"
echo "archive: $(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz"
echo "checksums: $(dirname "$OUT_DIR")/SHA256SUMS"
