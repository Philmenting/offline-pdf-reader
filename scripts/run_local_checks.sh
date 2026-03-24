#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

STRICT_MODE="${VALIDATION_STRICT:-0}"

echo "[checks] Running syntax check..."
python3 -m py_compile app.py

echo "[checks] Running helper validation..."
VALIDATION_STRICT="$STRICT_MODE" python3 validate_helpers.py

echo "[checks] Running parser regression..."
VALIDATION_STRICT="$STRICT_MODE" python3 validate_parser_regression.py

echo "[checks] OK (VALIDATION_STRICT=${STRICT_MODE})"
