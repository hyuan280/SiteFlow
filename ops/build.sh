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
declare -A versions commits
for service in api worker web bitwarden; do
  info="$(bash "$ROOT/ops/build-info.sh" "$service")"
  read -r version commit <<< "$info"
  versions[$service]="$version"
  commits[$service]="$commit"
done

cd "$ROOT/frontend"

npm ci --no-audit --no-fund
SITEFLOW_BUILD_VERSION="${versions[web]}" SITEFLOW_BUILD_COMMIT="${commits[web]}" npm run build

cd "$ROOT"
for service in api worker web bitwarden; do
  docker build "${build_args[@]}" \
    --build-arg "SITEFLOW_BUILD_VERSION=${versions[$service]}" \
    --build-arg "SITEFLOW_BUILD_COMMIT=${commits[$service]}" \
    -f "ops/Dockerfile.$service" -t "siteflow-$service" .
done
