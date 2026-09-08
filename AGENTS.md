# VyOS OVA Builder Agent Guidelines

Read this before changing the project. This repository builds a VMware OVA
from VyOS sources and adds first-boot configuration through vApp properties.

## Product shape

- `build.sh` is the supported end-to-end entry point.
- Python orchestration and validation live under `scripts/`.
- Guest files injected into VyOS live under `files/`.
- Generic, non-secret defaults live in `config/defaults.json`.
- `config/local.json` is private, optional, and must never be committed.
- Environment variables override both default and local configuration.
- Downloaded and generated payloads belong under `artifacts/` and must not be
  committed.

## Configuration rules

- Preserve the precedence order: defaults, local configuration, environment.
- Add every new environment override to the README and unit tests.
- Never place passwords, API keys, tokens, private keys, real infrastructure
  names, or private addressing in committed defaults or examples.
- Secret values must be redacted from diagnostic output and build manifests.
- vApp properties marked as passwords must have empty defaults in the OVA.

## Build rules

- Never modify or hard-reset a user-managed VyOS checkout.
- Customize only the disposable checkout under `artifacts/work/`.
- Privileged container builds must restore the disposable checkout to the host
  UID/GID before exiting, including on build failure.
- Pin downloaded dependencies by version and SHA-256 in
  `bom/vyos-ova-builder-bom.json`.
- Do not commit downloaded packages, ISO/OVA/OVF/VMDK files, logs, or caches.
- Quote shell paths and use `set -Eeuo pipefail` in ordinary Bash entry points.
- In VyOS configuration scripts, source `script-template` before shell options,
  use `builtin set` for shell options, and check VyOS commands explicitly.
- Do not log secrets or decoded vApp configuration commands.

## vApp initialization

- All VyOS changes must run through a single configuration session followed by
  `commit` and `save`.
- Start first-boot configuration from `vyos-postconfig-bootup.script`; do not
  wait for `vyos-router.service` from inside that hook.
- A failed initialization must not create the completion marker.
- Published vApp properties use the shared `guestinfo.*` naming convention.
- Do not add legacy property aliases unless a migration requirement is agreed.
- vApp values must be passed to VyOS CLI functions as quoted arguments and
  must never be evaluated as shell code.

## Testing

Run before committing:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
bash -n build.sh upload.sh docker/run-build.sh files/vapp-init.sh files/vyos-postconfig-bootup.script
```

An end-to-end image build additionally requires Docker, `ovftool`, network
access to configured dependencies, and privileges required by `vyos-build`.
