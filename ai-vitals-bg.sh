#!/usr/bin/env bash
# AI Vitals - Linux/macOS Background Launcher
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_PY="${SCRIPT_DIR}/ai_vitals.py"

echo "[AI Vitals] バックグラウンド起動中..."
python3 "${TARGET_PY}" -b "$@"
