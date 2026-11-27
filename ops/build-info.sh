#!/usr/bin/env bash
# Print the component version and full commit, separated by a space.
set -euo pipefail

component="${1:-}"
case "$component" in
  api|worker|web|bitwarden) ;;
  *) printf 'Usage: %s {api|worker|web|bitwarden}\n' "$0" >&2; exit 1 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
commit="$(git rev-parse --verify HEAD 2>/dev/null || true)"
tag="$(git describe --tags --abbrev=0 --match "$component-v[0-9]*" HEAD 2>/dev/null || true)"
version="${tag#"$component"-}"
version="${version:-v0.0.0}"

if [[ -z "$tag" || "$(git rev-parse "refs/tags/$tag^{commit}")" != "$commit" ||
      -n "$(git status --porcelain)" ]]; then
  version="$version-dev"
fi

printf '%s %s\n' "$version" "$commit"
