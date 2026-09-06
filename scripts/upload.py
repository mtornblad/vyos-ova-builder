#!/usr/bin/env python3
"""Upload a generated VyOS OVA to a vCenter Content Library."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from project_config import ConfigurationError, artifact_paths, load_config, validate_config


class UploadError(RuntimeError):
    """Raised when an OVA cannot be uploaded."""


def upload(config: Mapping[str, Any], ova_path: Path | None = None) -> None:
    validate_config(config, require_upload=True)
    govc = shutil.which("govc")
    if not govc:
        raise UploadError("govc is required but was not found in PATH")

    if ova_path is None:
        ova_path = artifact_paths(config)["builds"] / str(config["appliance"]["ova_name"])
    ova_path = ova_path.expanduser().resolve()
    if not ova_path.is_file():
        raise UploadError(f"OVA does not exist: {ova_path}")

    upload_config = config["upload"]
    environment = os.environ.copy()
    environment.update(
        {
            "GOVC_URL": str(upload_config["vcenter_url"]),
            "GOVC_USERNAME": str(upload_config["username"]),
            "GOVC_PASSWORD": str(upload_config["password"]),
            "GOVC_INSECURE": "true" if upload_config["insecure"] else "false",
        }
    )
    print(
        f"Uploading {ova_path.name} to Content Library "
        f"{upload_config['content_library']!r} as {upload_config['template_name']!r}."
    )
    subprocess.run(
        [
            govc,
            "library.import",
            "-n",
            str(upload_config["template_name"]),
            str(upload_config["content_library"]),
            str(ova_path),
        ],
        env=environment,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Optional local JSON configuration file")
    parser.add_argument("--ova", type=Path, help="OVA to upload; defaults to the configured build output")
    args = parser.parse_args()
    try:
        upload(load_config(args.config), args.ova)
    except (ConfigurationError, UploadError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
