# Third-Party Components

SiteFlow's original code is licensed under MIT (see LICENSE). This does not
relicense dependencies, browser binaries, fonts, or external container images.

- CloakBrowser Manager is pulled directly from its official image repository by
  the deploying user. SiteFlow's worker image contains no browser binary, Manager,
  KasmVNC server or bundled fonts. The Manager/browser and its fonts remain subject
  to their upstream licenses, including restrictions on browser redistribution
  and providing services to third parties. Keyless is not a redistribution grant.
- The MIT-licensed CloakBrowser Python wrapper is installed as a dependency of
  the Manager adapter for remote input humanization; SiteFlow does not vendor or
  modify it. Its browser downloader is not called by the worker.
- Playwright, noVNC, Caddy, Bitwarden CLI, ddddocr and other dependencies retain
  their respective licenses and notices. Consult their installed distributions
  and upstream projects for terms.

External images are not rebuilt, retagged or published by SiteFlow's workflows.

CFbox is pulled directly from `ghcr.io/hyuan280/cfbox`. Its code and model license
notices (including AGPL-3.0/Ultralytics terms) apply independently; SiteFlow's MIT
license does not relicense CFbox or its models.
