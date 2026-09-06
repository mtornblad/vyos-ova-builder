#!/usr/bin/env python3
"""Download and verify external builder dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen

from project_config import PROJECT_ROOT, artifact_paths, load_config


BOM_PATH = PROJECT_ROOT / "bom" / "vyos-ova-builder-bom.json"


class DependencyError(RuntimeError):
    """Raised when the dependency BOM or a downloaded payload is invalid."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bom(path: Path = BOM_PATH) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            bom = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise DependencyError(f"Unable to read dependency BOM {path}: {error}") from error
    if bom.get("schema") != "vyos.ova.builder.bom/v1":
        raise DependencyError(f"Unsupported BOM schema in {path}")
    dependencies = bom.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise DependencyError(f"No dependencies declared in {path}")
    required = {"name", "version", "filename", "url", "sha256"}
    for dependency in dependencies:
        if not isinstance(dependency, dict) or not required <= dependency.keys():
            raise DependencyError(f"Malformed dependency entry in {path}")
        filename = str(dependency["filename"])
        checksum = str(dependency["sha256"])
        if Path(filename).name != filename:
            raise DependencyError(f"Dependency filename must not contain a path: {filename!r}")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            raise DependencyError(f"Dependency {dependency['name']!r} has an invalid SHA-256")
    return dependencies


def ensure_dependencies(
    config: Mapping[str, Any],
    *,
    force: bool = False,
    bom_path: Path = BOM_PATH,
) -> dict[str, Path]:
    download_dir = artifact_paths(config)["downloads"]
    download_dir.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, Path] = {}

    for dependency in load_bom(bom_path):
        name = str(dependency["name"])
        destination = download_dir / str(dependency["filename"])
        expected = str(dependency["sha256"]).lower()
        if destination.exists() and not force and sha256_file(destination) == expected:
            print(f"Dependency already verified: {destination.name}")
            resolved[name] = destination
            continue

        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.unlink(missing_ok=True)
        print(f"Downloading {name} {dependency['version']}...")
        request = Request(str(dependency["url"]), headers={"User-Agent": "vyos-ova-builder/1"})
        try:
            with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            actual = sha256_file(temporary)
            if actual != expected:
                raise DependencyError(
                    f"SHA-256 mismatch for {destination.name}: expected {expected}, got {actual}"
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        resolved[name] = destination
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Optional local JSON configuration file")
    parser.add_argument("--force", action="store_true", help="Download even when a verified file exists")
    args = parser.parse_args()
    try:
        ensure_dependencies(load_config(args.config), force=args.force)
    except (DependencyError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
