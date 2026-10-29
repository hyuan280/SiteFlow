# Worker Build Resources

Run `bash ops/download-worker-resources.sh` before building the worker directly.
`bash ops/build.sh` runs this preparation automatically. The host needs curl and
sha256sum. Downloads are resumable and completed files are reused.

The build script calls `docker build` directly and loads the four runtime images
into Docker. Use `bash ops/build.sh --host` to enable host networking for build
steps with explicit `network.host` permission. No Compose override is needed;
without `--host`, Docker uses its default build network. Container runtime
networking is unchanged. Docker with Buildx/BuildKit support is required.

Assets are cached in `.build-resources/worker/amd64/` (or `arm64/`), excluded
from Git. Pass `amd64` or `arm64` to the download script for a different target.
The cache must match the Docker build target architecture.

- CloakBrowser SDK: 0.6.3; keyless Chromium: 146.0.7680.177.5.
- Browser archive: official CloakHQ/cloakbrowser GitHub release, cached as
  `browser.tar.gz`, alongside `SHA256SUMS` and `SHA256SUMS.sig`.
- KasmVNC: 1.3.3 Bookworm package, cached as `kasmvnc.deb`.
- Microsoft core fonts: the eleven original SourceForge `corefonts` installers,
  checked against `ops/corefonts.sha256` (Debian msttcorefonts checksums).
  Font license notices are retained in `/usr/share/doc/siteflow-corefonts`.

BuildKit mounts these files temporarily rather than adding archives to image
layers. Browser installation runs without network access and verifies the
official signed manifest, exact version and archive checksum. KasmVNC is fetched
over HTTPS; its locally recorded checksum detects subsequent corruption, but is
not an upstream authenticity signature.

The resulting image contains one browser version and uses its explicit binary
path. Missing binaries fail rather than trigger runtime downloads. Desktop PNG
capture uses mss with a per-capture X connection; FFmpeg and wget are not installed.

Only apt and pip still require network access during the image build. Keyless
operation does not grant redistribution rights: check upstream binary licensing
before publishing an image containing the browser to third parties.

## Manager Parity

The worker retains its independent Bookworm runtime and pinned keyless browser;
it does not use the Manager service or import Manager profiles. Font families
match the Manager baseline: Microsoft core fonts, Liberation, Noto Color Emoji,
FreeFont, Unifont, IPA Gothic, WenQuanYi Zen Hei and Loma. Noto CJK from the initial
slim worker is replaced by that baseline; X11 base/scalable fonts are also
included. Package versions can differ across
Debian releases, so this is not a byte-identical operating system.

Browser options match the working Manager profile's Windows persona, Intel GPU,
1920x1080 screen, 1920x947 viewport, Asia/Shanghai timezone, zh-CN locale and
default humanized input. Existing worker profile seeds and storage are retained.
Humanized input and reads deliberately add pauses; multi-action plugin steps may
need a larger step timeout. The isolated demo uses 60 seconds for these RPCs.
The Manager's third-party-cookie flag is not added: upstream documents it as
Chromium 148+, so it has no supported effect on the pinned Chromium 146 build.

This baseline is not the full Windows OS font set. Both Manager and worker can
still warn about missing Segoe UI, Calibri, Marlett, MS UI Gothic, Franklin Gothic
and Consolas. Use appropriately licensed Windows fonts to complete that set;
the warning is not suppressed. Font installation does not guarantee acceptance
by a site's server-side CAPTCHA verification.
