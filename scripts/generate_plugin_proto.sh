#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
"$PYTHON" -m grpc_tools.protoc \
  -I proto \
  --python_out=proto \
  --grpc_python_out=proto \
  proto/siteflow_proto/plugin.proto
