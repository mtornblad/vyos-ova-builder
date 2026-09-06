# Artifacts

This directory is created and populated by the builder. Binary payloads are
excluded from Git.

- `downloads/` contains verified external dependencies.
- `sources/` contains disposable source checkouts.
- `work/` contains temporary Docker and OVA assembly state.
- `builds/` contains generated OVA files and redacted build manifests.
- `logs/` is reserved for build logs.

Dependency versions, source URLs, and checksums are tracked in
`bom/vyos-ova-builder-bom.json`.
