"""Install the cached upstream release, without making network requests."""
from pathlib import Path

from cloakbrowser.config import get_archive_name, get_binary_dir, get_binary_path
from cloakbrowser.download import (
    _extract_archive,
    _parse_checksums,
    _parse_manifest_version,
    _verify_checksum,
    _verify_signature,
)

root = Path('/tmp/browser-assets')
version = '146.0.7680.177.5'
manifest = (root / 'SHA256SUMS').read_bytes()
_verify_signature(manifest, (root / 'SHA256SUMS.sig').read_bytes())
if _parse_manifest_version(manifest.decode()) != version:
    raise RuntimeError('Browser release version mismatch')
_verify_checksum(root / 'browser.tar.gz', _parse_checksums(manifest.decode())[get_archive_name()])
binary_dir = get_binary_dir(version)
binary_dir.parent.mkdir(parents=True, exist_ok=True)
_extract_archive(root / 'browser.tar.gz', binary_dir, get_binary_path(version))
