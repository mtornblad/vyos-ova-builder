#!/usr/bin/env python3
"""Build a VyOS VMDK in Docker and package it as a VMware OVA."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from create_ova import OvaBuildError, create_ova, sha256_file
from download_dependencies import DependencyError, ensure_dependencies
from project_config import PROJECT_ROOT, artifact_paths, load_config, redacted, resolve_path


class BuildError(RuntimeError):
    """Raised when source preparation or the VyOS build fails."""


def safe_location(location: str) -> str:
    """Remove URL user information before logging or writing manifests."""

    try:
        parsed = urlsplit(location)
    except ValueError:
        return "<configured source>"
    if not parsed.scheme:
        return location
    if parsed.username is None and not parsed.query and not parsed.fragment:
        return location
    try:
        hostname = parsed.hostname or ""
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
    except ValueError:
        return "<configured source>"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def run(
    command: Sequence[str], *, cwd: Path | None = None, display: Sequence[str] | None = None
) -> None:
    printable = " ".join(display or command)
    print(f"+ {printable}", flush=True)
    subprocess.run(list(command), cwd=cwd, check=True)


def capture(command: Sequence[str], *, cwd: Path | None = None) -> str:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def require_command(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise BuildError(f"Required command was not found in PATH: {name}")
    return executable


def reset_owned_directory(path: Path, owned_root: Path) -> None:
    resolved = path.resolve()
    resolved_root = owned_root.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise BuildError(f"Refusing to reset directory outside the builder work area: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def source_location(config: Mapping[str, Any]) -> str:
    configured_directory = str(config["source"].get("directory", "")).strip()
    if configured_directory:
        directory = resolve_path(configured_directory)
        if not directory.is_dir():
            raise BuildError(f"Configured VyOS source directory does not exist: {directory}")
        if not (directory / ".git").exists():
            raise BuildError(f"Configured VyOS source directory is not a Git checkout: {directory}")
        return str(directory)
    return str(config["source"]["repository"])


def prepare_source(config: Mapping[str, Any]) -> tuple[Path, str]:
    paths = artifact_paths(config)
    mirror = paths["sources"] / "vyos-build.git"
    checkout = paths["work"] / "vyos-build"
    location = source_location(config)
    paths["sources"].mkdir(parents=True, exist_ok=True)
    paths["work"].mkdir(parents=True, exist_ok=True)

    if mirror.exists():
        if not (mirror / "HEAD").is_file():
            raise BuildError(f"Source cache is not a Git mirror: {mirror}")
        run(
            ["git", "--git-dir", str(mirror), "remote", "set-url", "origin", location],
            display=["git", "--git-dir", str(mirror), "remote", "set-url", "origin", "<source>"],
        )
        run(["git", "--git-dir", str(mirror), "remote", "update", "--prune"])
    else:
        run(
            ["git", "clone", "--mirror", location, str(mirror)],
            display=["git", "clone", "--mirror", "<source>", str(mirror)],
        )

    revision = str(config["source"].get("revision", "")).strip()
    branch = str(config["source"].get("branch", "")).strip()
    reference = revision or f"refs/heads/{branch}"
    try:
        commit = capture(
            ["git", "--git-dir", str(mirror), "rev-parse", "--verify", f"{reference}^{{commit}}"]
        )
    except subprocess.CalledProcessError as error:
        raise BuildError(f"Unable to resolve VyOS source revision {reference!r}") from error

    reset_owned_directory(checkout, paths["work"])
    # reset_owned_directory creates the target, while git clone requires it to be absent.
    checkout.rmdir()
    run(["git", "clone", "--no-checkout", str(mirror), str(checkout)])
    run(["git", "checkout", "--detach", commit], cwd=checkout)
    return checkout, commit


def customize_source(checkout: Path, config: Mapping[str, Any]) -> None:
    flavor_name = str(config["build"]["flavor"])
    flavor_source = PROJECT_ROOT / "templates" / "vmware-vapp.toml"
    flavor_target = checkout / "data" / "build-flavors" / f"{flavor_name}.toml"
    include_root = checkout / "data" / "live-build-config" / "includes.chroot"
    script_target = include_root / "usr" / "local" / "sbin" / "vyos-vapp-init"
    service_target = include_root / "etc" / "systemd" / "system" / "vapp-init.service"
    wants_target = include_root / "etc" / "systemd" / "system" / "multi-user.target.wants"

    flavor_target.parent.mkdir(parents=True, exist_ok=True)
    script_target.parent.mkdir(parents=True, exist_ok=True)
    service_target.parent.mkdir(parents=True, exist_ok=True)
    wants_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(flavor_source, flavor_target)
    shutil.copy2(PROJECT_ROOT / "files" / "vapp-init.sh", script_target)
    shutil.copy2(PROJECT_ROOT / "files" / "vapp-init.service", service_target)
    script_target.chmod(0o755)

    service_link = wants_target / "vapp-init.service"
    service_link.unlink(missing_ok=True)
    service_link.symlink_to("../vapp-init.service")


def build_container_image(config: Mapping[str, Any], syft_package: Path) -> None:
    paths = artifact_paths(config)
    context = paths["work"] / "docker-context"
    reset_owned_directory(context, paths["work"])
    shutil.copy2(PROJECT_ROOT / "docker" / "Dockerfile", context / "Dockerfile")
    shutil.copy2(syft_package, context / "syft.deb")
    run(
        [
            "docker",
            "build",
            "--build-arg",
            f"BASE_IMAGE={config['build']['docker_base_image']}",
            "--tag",
            str(config["build"]["docker_image"]),
            str(context),
        ]
    )


def build_vmdk(checkout: Path, config: Mapping[str, Any]) -> Path:
    build = config["build"]
    command = [
        "sudo",
        "./build-vyos-image",
        str(build["flavor"]),
        "--architecture",
        str(build["architecture"]),
        "--build-by",
        str(build["build_by"]),
        "--build-type",
        str(build["build_type"]),
    ]
    for package in build["custom_packages"]:
        command.extend(["--custom-package", str(package)])

    run(
        [
            "docker",
            "run",
            "--rm",
            "--privileged",
            "--volume",
            f"{checkout}:/vyos",
            "--workdir",
            "/vyos",
            str(build["docker_image"]),
            *command,
        ]
    )

    candidates = sorted((checkout / "build").glob("*.vmdk"))
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise BuildError(f"Expected exactly one generated VMDK; found {names}")
    return candidates[0]


def write_build_manifest(
    config: Mapping[str, Any], *, source_commit: str, source: str, ova_path: Path
) -> Path:
    reproducible_configuration = {
        key: config[key] for key in ("build", "appliance", "vapp")
    }
    manifest = {
        "schema": "vyos.ova.builder.build/v1",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {"location": source, "commit": source_commit},
        "configuration": redacted(reproducible_configuration),
        "artifact": {
            "filename": ova_path.name,
            "size_bytes": ova_path.stat().st_size,
            "sha256": sha256_file(ova_path),
        },
    }
    destination = ova_path.with_suffix(".build.json")
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def build(config: Mapping[str, Any]) -> tuple[Path, Path]:
    require_command("git")
    require_command("docker")
    require_command("ovftool")
    dependencies = ensure_dependencies(config)
    syft_package = dependencies.get("syft")
    if not syft_package:
        raise BuildError("The dependency BOM does not provide syft")

    location = source_location(config)
    source_description = (
        "<local checkout>" if str(config["source"].get("directory", "")).strip() else safe_location(location)
    )
    checkout, commit = prepare_source(config)
    customize_source(checkout, config)
    build_container_image(config, syft_package)
    vmdk = build_vmdk(checkout, config)
    ova = create_ova(vmdk, config)
    manifest = write_build_manifest(
        config,
        source_commit=commit,
        source=source_description,
        ova_path=ova,
    )
    return ova, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Optional local JSON configuration file")
    args = parser.parse_args()
    try:
        ova, manifest = build(load_config(args.config))
    except (
        BuildError,
        DependencyError,
        OvaBuildError,
        OSError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Created {ova}")
    print(f"Build manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
