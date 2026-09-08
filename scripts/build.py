#!/usr/bin/env python3
"""Build a VyOS VMDK in Docker and package it as a VMware OVA."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import stat
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


OWNERSHIP_MARKER_NAME = ".vyos-ova-builder-owner"


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


def resolve_owned_child(path: Path, owned_root: Path) -> Path:
    resolved = path.resolve()
    resolved_root = owned_root.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise BuildError(f"Refusing to modify directory outside the builder work area: {resolved}")
    return resolved


def reset_owned_directory(path: Path, owned_root: Path) -> None:
    resolved = resolve_owned_child(path, owned_root)
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def directory_has_foreign_owner(path: Path, *, uid: int, gid: int) -> bool:
    """Return whether a tree contains entries not owned by the build user."""

    try:
        root_device = path.lstat().st_dev
    except OSError:
        return True
    pending = [path]
    while pending:
        candidate = pending.pop()
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if metadata.st_dev != root_device:
            continue
        if metadata.st_uid != uid or metadata.st_gid != gid:
            return True
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        try:
            with os.scandir(candidate) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        except OSError:
            return True
    return False


def repair_directory_ownership(
    path: Path,
    owned_root: Path,
    image: str,
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    """Use container root to return a disposable build tree to the host user."""

    resolved = resolve_owned_child(path, owned_root)
    host_uid = os.getuid() if uid is None else uid
    host_gid = os.getgid() if gid is None else gid
    print(f"Repairing container-created ownership below {resolved}", flush=True)
    run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "/usr/bin/find",
            "--volume",
            f"{resolved}:/builder-work",
            image,
            "/builder-work",
            "-xdev",
            "(",
            "!",
            "-uid",
            str(host_uid),
            "-o",
            "!",
            "-gid",
            str(host_gid),
            ")",
            "-exec",
            "/usr/bin/chown",
            "-h",
            "--",
            f"{host_uid}:{host_gid}",
            "{}",
            "+",
        ]
    )
    marker = resolved / OWNERSHIP_MARKER_NAME
    marker.unlink(missing_ok=True)
    marker.write_text(f"{host_uid}:{host_gid}\n", encoding="utf-8")


def ensure_directory_ownership(path: Path, owned_root: Path, image: str) -> None:
    """Repair an old or interrupted container build before host-side cleanup."""

    if not path.exists():
        return
    resolved = resolve_owned_child(path, owned_root)
    host_uid = os.getuid()
    host_gid = os.getgid()
    marker = resolved / OWNERSHIP_MARKER_NAME
    try:
        if marker.read_text(encoding="utf-8").strip() == f"{host_uid}:{host_gid}":
            return
    except OSError:
        pass
    if directory_has_foreign_owner(resolved, uid=host_uid, gid=host_gid):
        repair_directory_ownership(
            resolved,
            owned_root,
            image,
            uid=host_uid,
            gid=host_gid,
        )


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


def source_reference(config: Mapping[str, Any], location: str) -> str:
    """Select a source reference without losing a pinned local checkout.

    A local source directory is normally the ``vyos-build`` submodule from the
    umbrella repository.  Its checked-out HEAD is the umbrella's pin and may be
    detached, so resolve that commit in the source repository instead of
    assuming a local branch exists.  An explicitly configured revision still
    takes precedence.
    """

    revision = str(config["source"].get("revision", "")).strip()
    configured_directory = str(config["source"].get("directory", "")).strip()
    if configured_directory:
        requested = revision or "HEAD"
        try:
            return capture(
                ["git", "-C", location, "rev-parse", "--verify", f"{requested}^{{commit}}"]
            )
        except subprocess.CalledProcessError as error:
            raise BuildError(
                f"Unable to resolve local VyOS source revision {requested!r}"
            ) from error

    if revision:
        return revision
    return f"refs/heads/{str(config['source']['branch']).strip()}"


def prepare_source(config: Mapping[str, Any]) -> tuple[Path, str]:
    paths = artifact_paths(config)
    mirror = paths["sources"] / "vyos-build.git"
    checkout = paths["work"] / "vyos-build"
    location = source_location(config)
    reference = source_reference(config, location)
    paths["sources"].mkdir(parents=True, exist_ok=True)
    paths["work"].mkdir(parents=True, exist_ok=True)
    ensure_directory_ownership(
        checkout,
        paths["work"],
        str(config["build"]["docker_base_image"]),
    )

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
    parser_target = (
        include_root / "usr" / "local" / "libexec" / "vyos-ova-parse-config"
    )
    resolver_target = (
        include_root / "usr" / "local" / "libexec" / "vyos-ova-resolve-interface"
    )
    postconfig_target = (
        include_root
        / "opt"
        / "vyatta"
        / "etc"
        / "config"
        / "scripts"
        / "vyos-postconfig-bootup.script"
    )

    flavor_target.parent.mkdir(parents=True, exist_ok=True)
    script_target.parent.mkdir(parents=True, exist_ok=True)
    parser_target.parent.mkdir(parents=True, exist_ok=True)
    postconfig_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(flavor_source, flavor_target)
    shutil.copy2(PROJECT_ROOT / "files" / "vapp-init.sh", script_target)
    shutil.copy2(PROJECT_ROOT / "files" / "parse-config.py", parser_target)
    shutil.copy2(PROJECT_ROOT / "files" / "resolve_interface.py", resolver_target)
    shutil.copy2(
        PROJECT_ROOT / "files" / "vyos-postconfig-bootup.script",
        postconfig_target,
    )
    script_target.chmod(0o755)
    parser_target.chmod(0o755)
    resolver_target.chmod(0o755)
    postconfig_target.chmod(0o755)


def build_container_image(config: Mapping[str, Any], syft_package: Path) -> None:
    paths = artifact_paths(config)
    context = paths["work"] / "docker-context"
    reset_owned_directory(context, paths["work"])
    shutil.copy2(PROJECT_ROOT / "docker" / "Dockerfile", context / "Dockerfile")
    shutil.copy2(PROJECT_ROOT / "docker" / "run-build.sh", context / "run-build.sh")
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
            "/usr/local/bin/vyos-ova-run-build",
            str(os.getuid()),
            str(os.getgid()),
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
