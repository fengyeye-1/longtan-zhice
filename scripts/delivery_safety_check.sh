#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-.}"
DIR="$(cd "$(dirname "$0")" && pwd)"
# Prefer Python checker (no silent fallback to 0)
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$DIR/delivery_safety_check.py" "$ROOT"
fi
if command -v python >/dev/null 2>&1; then
  exec python "$DIR/delivery_safety_check.py" "$ROOT"
fi
echo "FAIL: python3/python required for delivery safety check (sqlite3 CLI fallback removed)"
exit 1
