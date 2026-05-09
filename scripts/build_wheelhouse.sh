#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K1S_ROOT="${K1S_ROOT:-${ROOT_DIR}/../k1s}"
OUT_DIR="${ROOT_DIR}/dist/workerbee-wheelhouse"
PYTHON_BIN="${PYTHON_BIN:-python}"

usage() {
  cat <<'EOF'
usage: scripts/build_wheelhouse.sh [--k1s-root PATH] [--out PATH] [--python PYTHON]

Build a local wheelhouse containing:
  - k1s-workerbee-runtime
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
rm -rf "$OUT_DIR" "${OUT_PARENT}/workerbee-wheelhouse.tar.gz"
mkdir -p "$OUT_DIR"

"${PYTHON_BIN}" "${K1S_ROOT}/scripts/build_workerbee_runtime_wheel.py" --out "$OUT_DIR"
"${PYTHON_BIN}" -m pip wheel --find-links "$OUT_DIR" -w "$OUT_DIR" "$ROOT_DIR"

cat >"${OUT_DIR}/INSTALL.txt" <<EOF
Install WorkerBee from this wheelhouse:

  python -m pip install --no-index --find-links ${OUT_DIR} k1s-workerbee

Runtime requirements not bundled in wheels:
  - Podman or Docker available on PATH
EOF

echo "wheelhouse: ${OUT_DIR}"
tar -C "$(dirname "$OUT_DIR")" -czf "$(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz" "$(basename "$OUT_DIR")"
echo "archive: $(dirname "$OUT_DIR")/workerbee-wheelhouse.tar.gz"
