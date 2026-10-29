#!/usr/bin/env bash
# Cache fixed upstream assets on the host; completed downloads are reused.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
arch="${1:-$(uname -m)}"
case "$arch" in
  x86_64|amd64) arch=amd64; platform=x64 ;;
  aarch64|arm64) arch=arm64; platform=arm64 ;;
  *) printf 'Unsupported architecture: %s\n' "$arch" >&2; exit 1 ;;
esac
dest="$ROOT/.build-resources/worker/$arch"
mkdir -p "$dest"
fetch() {
  local url="$1" name="$2"
  if [[ ! -f "$dest/$name" ]]; then
    curl --fail --location --retry 5 --connect-timeout 20 \
      --continue-at - --output "$dest/$name.part" "$url"
    mv "$dest/$name.part" "$dest/$name"
  fi
}
browser=https://github.com/CloakHQ/cloakbrowser/releases/download/chromium-v146.0.7680.177.5
fetch "$browser/cloakbrowser-linux-$platform.tar.gz" browser.tar.gz
fetch "$browser/SHA256SUMS" SHA256SUMS
fetch "$browser/SHA256SUMS.sig" SHA256SUMS.sig
fetch "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_bookworm_1.3.3_$arch.deb" kasmvnc.deb
if [[ ! -f "$dest/kasmvnc.sha256" ]]; then
  (cd "$dest" && sha256sum kasmvnc.deb > kasmvnc.sha256)
fi
(cd "$dest" && sha256sum -c kasmvnc.sha256)
while read -r checksum filename; do
  fetch "https://downloads.sourceforge.net/project/corefonts/the%20fonts/final/$filename" "$filename"
done < "$ROOT/ops/corefonts.sha256"
(cd "$dest" && sha256sum -c "$ROOT/ops/corefonts.sha256")
printf 'Worker resources cached in %s\n' "$dest"
