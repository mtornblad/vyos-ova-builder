# VyOS OVA Builder

Builds a VyOS VMware OVA with vApp properties for safe first-boot
configuration. Despite the original archive name, this project does not use
Packer, so `vyos-ova-builder` is the intentional name.

The builder keeps two responsibilities separate:

- `vyos-build` is an upstream or forked VyOS source dependency.
- `vyos-ova-builder` creates a disposable checkout, injects the VMware guest
  integration, builds a VMDK in Docker, and packages the OVA.

It never modifies or resets the user-managed `vyos-build` checkout.

## Migration from the original archive

| Original | Refactored project |
| --- | --- |
| `vyos-packer` | `vyos-ova-builder` |
| `build-vyos-docker.sh` plus `create-ova.sh` | End-to-end `build.sh` |
| `upload-ova.sh` | Configuration-driven `upload.sh` |
| Mutable nested `vyos-build/` clone | Builder-owned mirror and disposable checkout under artifacts |
| Committed Syft `.deb` | Versioned, checksum-pinned BOM download |
| Hard-coded build and vCenter values | Defaults, ignored local JSON, then environment |
| Root-level OVA and temporary files | Structured ignored artifact directory |

The refactored project intentionally does not retain aliases for the original
property names. It publishes the same `guestinfo.*` convention used by the
other appliance builders in the lab.

## Configuration model

Effective configuration is merged in this order, with the last source winning:

1. Committed generic defaults in `config/defaults.json`.
2. Optional private settings in ignored `config/local.json`, or the file named
   by `VYOS_OVA_CONFIG_FILE`.
3. Environment variables.

Create a private configuration for the umbrella repository:

```bash
cp config/local.example.json config/local.json
${EDITOR:-vi} config/local.json
python3 scripts/project_config.py validate
```

The example assumes this layout:

```text
nested-vcf-lab/
├── artifacts/vyos/
└── components/
    ├── vyos-build/
    └── vyos-ova-builder/
```

All values in `config/local.json` are ignored by Git. Passwords and API keys
should preferably be supplied by the process environment or a CI secret store.
The effective configuration can be inspected safely with `make show-config`;
secret fields are redacted.

### Environment variables

| Variable | Configuration value |
| --- | --- |
| `VYOS_OVA_CONFIG_FILE` | Alternate private JSON file |
| `VYOS_OVA_SOURCE_REPOSITORY` | `source.repository` |
| `VYOS_OVA_SOURCE_DIRECTORY` | `source.directory` |
| `VYOS_OVA_SOURCE_BRANCH` | `source.branch` |
| `VYOS_OVA_SOURCE_REVISION` | `source.revision` |
| `VYOS_OVA_BUILD_ARCHITECTURE` | `build.architecture` |
| `VYOS_OVA_BUILD_BY` | `build.build_by` |
| `VYOS_OVA_BUILD_TYPE` | `build.build_type` |
| `VYOS_OVA_BUILD_FLAVOR` | `build.flavor` |
| `VYOS_OVA_DOCKER_BASE_IMAGE` | `build.docker_base_image` |
| `VYOS_OVA_DOCKER_IMAGE` | `build.docker_image` |
| `VYOS_OVA_CUSTOM_PACKAGES` | Comma-separated `build.custom_packages` |
| `VYOS_OVA_NAME` | `appliance.ova_name` |
| `VYOS_OVA_DISPLAY_NAME` | `appliance.display_name` |
| `VYOS_OVA_CPUS` | `appliance.cpus` |
| `VYOS_OVA_MEMORY_MB` | `appliance.memory_mb` |
| `VYOS_OVA_NETWORK_ADAPTERS` | `appliance.network_adapters` |
| `VYOS_OVA_NETWORK_NAME` | `appliance.network_name` |
| `VYOS_OVA_ARTIFACTS_DIR` | `paths.artifacts` |
| `VYOS_OVA_VCENTER_URL` | `upload.vcenter_url` |
| `VYOS_OVA_VCENTER_USERNAME` | `upload.username` |
| `VYOS_OVA_VCENTER_PASSWORD` | `upload.password` |
| `VYOS_OVA_VCENTER_INSECURE` | `upload.insecure` |
| `VYOS_OVA_CONTENT_LIBRARY` | `upload.content_library` |
| `VYOS_OVA_TEMPLATE_NAME` | `upload.template_name` |

The standard `GOVC_URL`, `GOVC_USERNAME`, `GOVC_PASSWORD`, and
`GOVC_INSECURE` variables are accepted as upload aliases. A project-prefixed
variable wins when both forms are set.

For example, a build can use the umbrella repository without a local file:

```bash
VYOS_OVA_SOURCE_DIRECTORY=../vyos-build \
VYOS_OVA_ARTIFACTS_DIR=../../artifacts/vyos \
VYOS_OVA_BUILD_BY=builder@example.invalid \
./build.sh
```

Only committed revisions from a configured local source checkout are used.
For `source.directory`, the default is that checkout's exact `HEAD`, including
a detached submodule HEAD; uncommitted files are ignored. Set
`VYOS_OVA_SOURCE_REVISION` to select another committed revision explicitly.
`source.branch` is used when cloning `source.repository` directly.
The currently pinned Syft package is for `amd64`; other architectures are
rejected until a matching package and checksum are added to the BOM.

## Build and upload

Prerequisites are Python 3.11 or later, Git, Docker, VMware OVF Tool, and the
privileges required to run the VyOS build container with `--privileged`.
`govc` is only required for upload.

```bash
make validate
make test
make dependencies
make build
make upload
```

`build.sh` performs the complete flow. It downloads checksum-pinned
dependencies, refreshes a Git mirror below the artifact directory, creates a
disposable checkout, builds the VMDK, then creates the OVA and a redacted build
manifest. `upload.sh` imports that OVA into the configured vCenter Content
Library.

The privileged VyOS build runs through a container wrapper that records the
host UID/GID and restores ownership of the mounted disposable checkout before
the container exits, including when the build command fails. The wrapper
removes an ownership marker before starting and recreates it only after a
successful cleanup. If an older or interrupted build left foreign-owned files,
the next build repairs that checkout with container root before replacing it.
No host-side `sudo` cleanup should be needed during normal operation.

Downloaded and generated data is kept outside Git:

| Directory | Content |
| --- | --- |
| `artifacts/downloads/` | Checksum-verified external packages |
| `artifacts/sources/` | Builder-owned Git mirrors |
| `artifacts/work/` | Disposable checkout and packaging workspace |
| `artifacts/builds/` | OVA and redacted build manifest |
| `artifacts/logs/` | Reserved for logs |

The Syft package is no longer committed as a binary. Its version, URL, and
SHA-256 are pinned in `bom/vyos-ova-builder-bom.json`.

## vApp properties

The property definitions are maintained in `templates/vapp-properties.json`.
Password properties have deliberately empty OVA defaults.

| OVF environment key | Purpose | Default |
| --- | --- | --- |
| `guestinfo.hostname` | VyOS hostname | `vyos` |
| `guestinfo.password` | Password for the `vyos` user | empty, masked |
| `guestinfo.ipaddress` | Static address; empty or `dhcp` selects DHCP | empty |
| `guestinfo.netmask` | Prefix length or dotted netmask | `24` |
| `guestinfo.gateway` | Optional default gateway | empty |
| `guestinfo.dns` | Comma- or space-separated DNS servers | empty |
| `guestinfo.domain` | DNS domain | empty |
| `guestinfo.ntp` | Comma- or space-separated NTP servers | empty |
| `guestinfo.vlan` | Optional VLAN ID on the resolved management interface | empty |
| `guestinfo.enable_ssh` | Enable SSH | `false` |
| `guestinfo.ssh_authorized_key` | OpenSSH public key for the `vyos` user | empty |
| `guestinfo.enable_rest` | Enable the REST API over HTTPS | `false` |
| `guestinfo.rest_api_key` | Full-access REST API key | empty, masked |
| `guestinfo.management_network` | OVF network used to discover the management interface by MAC; empty falls back to `eth0` | empty |
| `guestinfo.trunk_network` | OVF network used to discover the supplemental-configuration trunk interface by MAC | empty |
| `guestinfo.config_base64` | Base64-encoded supplemental `set`/`delete` commands | empty |

SSH and REST both retain VyOS's default listen-address behavior. To restrict
either service to selected addresses, provide the corresponding `service ssh`
or `service https` commands in `guestinfo.config_base64`.

Supplemental configuration is decoded as UTF-8 and parsed without shell
evaluation. Blank lines and comments are allowed, but every command must begin
with `set` or `delete`. It is applied before the dedicated vApp properties, so
the stable properties above take precedence when both configure the same path.
The worker then performs one atomic `commit` and `save`. Base64 is transport
encoding, not encryption; place secrets in dedicated masked properties instead
of the supplemental configuration whenever possible.

When the two network properties are provided, the worker reads the OVF
`EthernetAdapterSection` and maps each network's MAC address to the actual Linux
interface. Supplemental commands may use the standalone tokens
`__MANAGEMENT_INTERFACE__` and `__TRUNK_INTERFACE__`; they are replaced with
the resolved names after parsing and are never evaluated as shell code. This
avoids relying on VMware NIC enumeration order.

### First-boot execution

The image contains `/usr/local/sbin/vyos-vapp-init` and seeds
`/config/scripts/vyos-postconfig-bootup.script` through VyOS's default
configuration skeleton. VyOS invokes that hook after the saved configuration
has been applied. The hook calls the worker directly; no additional systemd
unit is installed and the worker does not wait for `vyos-router.service` or a
second configuration session.

On success, the worker commits and saves the generated configuration and then
creates `/opt/vyos-ova-builder/vapp-configured`. Later boots are no-ops while
that marker exists. On failure, the hook logs the error without failing the
VyOS boot, leaves the marker absent, and retries on the next boot. Logs use the
`vyos-vapp-init` syslog tag.

Useful guest-side checks are:

```bash
vmtoolsd --cmd 'info-get guestinfo.ovfEnv'
sudo grep -F 'vyos-vapp-init' /var/log/messages
sudo test -e /opt/vyos-ova-builder/vapp-configured
```

## Security

- No vCenter or VyOS password is committed or built into the OVA.
- Secret configuration is redacted from diagnostics and manifests.
- Decoded supplemental commands and REST API keys are never written to logs.
- `config/local.json`, environment files, downloaded packages, and built images
  are excluded by `.gitignore`.
- Treat an OVA instantiated with deployment secrets as sensitive even though
  the committed OVA template itself contains no secret defaults.
