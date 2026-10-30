#!/usr/bin/env bash
# Build the SPA on the host, then build the runtime images.
set -euo pipefail

build_args=(--load)
case "${1:-}" in
  "") ;;
  --host) build_args+=(--network=host --allow=network.host) ;;
  *) printf 'Usage: %s [--host]\n' "$0" >&2; exit 1 ;;
esac
if (( $# > 1 )); then
  printf 'Usage: %s [--host]\n' "$0" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/frontend"

npm ci --no-audit --no-fund
npm run build

cd "$ROOT"
for service in api worker web bitwarden; do
  docker build "${build_args[@]}" -f "ops/Dockerfile.$service" -t "siteflow-$service" .
done
