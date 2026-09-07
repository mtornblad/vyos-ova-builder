#!/usr/bin/env python3
"""Load, merge, validate, and safely display builder configuration."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.json"
DEFAULT_LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "local.json"
CONFIG_SCHEMA = "vyos.ova.builder.config/v1"


class ConfigurationError(ValueError):
    """Raised when builder configuration is invalid."""


def parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Expected a boolean value, got {value!r}")


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


ENV_OVERRIDES: Sequence[tuple[str, tuple[str, ...], Callable[[str], Any]]] = (
    # Standard govc variables are accepted as convenient aliases.
    ("GOVC_URL", ("upload", "vcenter_url"), str),
    ("GOVC_USERNAME", ("upload", "username"), str),
    ("GOVC_PASSWORD", ("upload", "password"), str),
    ("GOVC_INSECURE", ("upload", "insecure"), parse_bool),
    # Project-prefixed variables take precedence over aliases above.
    ("VYOS_OVA_SOURCE_REPOSITORY", ("source", "repository"), str),
    ("VYOS_OVA_SOURCE_BRANCH", ("source", "branch"), str),
    ("VYOS_OVA_SOURCE_REVISION", ("source", "revision"), str),
    ("VYOS_OVA_SOURCE_DIRECTORY", ("source", "directory"), str),
    ("VYOS_OVA_BUILD_ARCHITECTURE", ("build", "architecture"), str),
    ("VYOS_OVA_BUILD_BY", ("build", "build_by"), str),
    ("VYOS_OVA_BUILD_TYPE", ("build", "build_type"), str),
    ("VYOS_OVA_BUILD_FLAVOR", ("build", "flavor"), str),
    ("VYOS_OVA_DOCKER_BASE_IMAGE", ("build", "docker_base_image"), str),
    ("VYOS_OVA_DOCKER_IMAGE", ("build", "docker_image"), str),
    ("VYOS_OVA_CUSTOM_PACKAGES", ("build", "custom_packages"), parse_csv),
    ("VYOS_OVA_NAME", ("appliance", "ova_name"), str),
    ("VYOS_OVA_DISPLAY_NAME", ("appliance", "display_name"), str),
    ("VYOS_OVA_CPUS", ("appliance", "cpus"), int),
    ("VYOS_OVA_MEMORY_MB", ("appliance", "memory_mb"), int),
    ("VYOS_OVA_NETWORK_ADAPTERS", ("appliance", "network_adapters"), int),
    ("VYOS_OVA_NETWORK_NAME", ("appliance", "network_name"), str),
    ("VYOS_OVA_ARTIFACTS_DIR", ("paths", "artifacts"), str),
    ("VYOS_OVA_VCENTER_URL", ("upload", "vcenter_url"), str),
    ("VYOS_OVA_VCENTER_USERNAME", ("upload", "username"), str),
    ("VYOS_OVA_VCENTER_PASSWORD", ("upload", "password"), str),
    ("VYOS_OVA_VCENTER_INSECURE", ("upload", "insecure"), parse_bool),
    ("VYOS_OVA_CONTENT_LIBRARY", ("upload", "content_library"), str),
    ("VYOS_OVA_TEMPLATE_NAME", ("upload", "template_name"), str),
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as error:
        raise ConfigurationError(f"Configuration file does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration root must be an object: {path}")
    return value


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _set_nested(config: MutableMapping[str, Any], path: Sequence[str], value: Any) -> None:
    target: MutableMapping[str, Any] = config
    for key in path[:-1]:
        child = target.get(key)
        if not isinstance(child, MutableMapping):
            child = {}
            target[key] = child
        target = child
    target[path[-1]] = value


def _resolve_config_path(value: str | os.PathLike[str], project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
    defaults_path: Path | None = None,
    include_default_local: bool = True,
) -> dict[str, Any]:
    """Load defaults, optional local JSON, and environment overrides.

    ``include_default_local=False`` skips the implicit ``config/local.json``.
    Explicitly selected configuration files are still loaded. This keeps unit
    tests deterministic without changing normal command-line behavior.
    """

    environment = os.environ if environ is None else environ
    defaults = _read_json(defaults_path or project_root / "config" / "defaults.json")
    if defaults.get("schema") != CONFIG_SCHEMA:
        raise ConfigurationError("Unsupported schema in default configuration")

    selected_path: Path | None
    explicitly_selected = config_path is not None or bool(environment.get("VYOS_OVA_CONFIG_FILE"))
    selected_value = config_path or environment.get("VYOS_OVA_CONFIG_FILE")
    if selected_value:
        selected_path = _resolve_config_path(selected_value, project_root)
    elif include_default_local:
        selected_path = project_root / "config" / "local.json"
    else:
        selected_path = None

    merged = defaults
    if selected_path is not None and selected_path.exists():
        local = _read_json(selected_path)
        schema = local.get("schema")
        if schema not in {None, CONFIG_SCHEMA}:
            raise ConfigurationError(f"Unsupported schema in {selected_path}")
        merged = deep_merge(merged, local)
    elif selected_path is not None and explicitly_selected:
        raise ConfigurationError(f"Selected local configuration does not exist: {selected_path}")

    for variable, path, converter in ENV_OVERRIDES:
        if variable in environment:
            try:
                value = converter(environment[variable])
            except (TypeError, ValueError) as error:
                raise ConfigurationError(f"Invalid value for {variable}: {error}") from error
            _set_nested(merged, path, value)

    validate_config(merged)
    return merged


def _require_mapping(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key)
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{key} must be a JSON object")
    return value


def _reject_unknown_keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"Unknown {context} setting(s): {', '.join(unknown)}")


def validate_config(config: Mapping[str, Any], *, require_upload: bool = False) -> None:
    if config.get("schema") != CONFIG_SCHEMA:
        raise ConfigurationError(f"schema must be {CONFIG_SCHEMA}")

    source = _require_mapping(config, "source")
    build = _require_mapping(config, "build")
    appliance = _require_mapping(config, "appliance")
    _require_mapping(config, "vapp")
    upload = _require_mapping(config, "upload")
    paths = _require_mapping(config, "paths")

    _reject_unknown_keys(
        config,
        {"schema", "source", "build", "appliance", "vapp", "upload", "paths"},
        "top-level",
    )
    _reject_unknown_keys(source, {"repository", "branch", "revision", "directory"}, "source")
    _reject_unknown_keys(
        build,
        {
            "architecture",
            "build_by",
            "build_type",
            "flavor",
            "docker_base_image",
            "docker_image",
            "custom_packages",
        },
        "build",
    )
    _reject_unknown_keys(
        appliance,
        {
            "ova_name",
            "display_name",
            "guest_os",
            "hardware_version",
            "cpus",
            "memory_mb",
            "disk_controller",
            "network_adapters",
            "network_name",
        },
        "appliance",
    )
    _reject_unknown_keys(config["vapp"], {"properties_file"}, "vapp")
    _reject_unknown_keys(
        upload,
        {
            "vcenter_url",
            "username",
            "password",
            "insecure",
            "content_library",
            "template_name",
        },
        "upload",
    )
    _reject_unknown_keys(paths, {"artifacts"}, "paths")

    if not str(source.get("directory", "")).strip() and not str(source.get("repository", "")).strip():
        raise ConfigurationError("source.repository or source.directory is required")
    if not str(source.get("revision", "")).strip() and not str(source.get("branch", "")).strip():
        raise ConfigurationError("source.branch or source.revision is required")
    if build.get("architecture") != "amd64":
        raise ConfigurationError("build.architecture must be amd64 with the currently pinned dependencies")
    if build.get("build_type") not in {"development", "release"}:
        raise ConfigurationError("build.build_type must be development or release")
    if not isinstance(build.get("custom_packages"), list):
        raise ConfigurationError("build.custom_packages must be an array")
    for key in ("build_by", "flavor", "docker_base_image", "docker_image"):
        if not str(build.get(key, "")).strip():
            raise ConfigurationError(f"build.{key} is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(build.get("flavor", ""))):
        raise ConfigurationError("build.flavor must be a simple filename-safe name")
    for package in build.get("custom_packages", []):
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", package):
            raise ConfigurationError(f"Invalid custom package name: {package!r}")

    for key in ("branch", "revision"):
        value = str(source.get(key, "")).strip()
        if value.startswith("-") or any(character in value for character in ("\n", "\r", "\x00")):
            raise ConfigurationError(f"source.{key} contains invalid characters")

    for key in ("cpus", "memory_mb", "hardware_version", "network_adapters"):
        value = appliance.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigurationError(f"appliance.{key} must be a positive integer")
    ova_name = str(appliance.get("ova_name", ""))
    if not ova_name.endswith(".ova") or Path(ova_name).name != ova_name:
        raise ConfigurationError("appliance.ova_name must be a filename ending in .ova")
    if not str(paths.get("artifacts", "")).strip():
        raise ConfigurationError("paths.artifacts is required")
    if not str(config["vapp"].get("properties_file", "")).strip():
        raise ConfigurationError("vapp.properties_file is required")
    for key in ("display_name", "guest_os", "disk_controller", "network_name"):
        value = str(appliance.get(key, ""))
        if not value.strip() or any(character in value for character in ("\n", "\r", "\x00")):
            raise ConfigurationError(f"appliance.{key} is required and must be a single line")
    if not isinstance(upload.get("insecure"), bool):
        raise ConfigurationError("upload.insecure must be a boolean")

    if require_upload:
        for key in ("vcenter_url", "username", "password", "content_library", "template_name"):
            if not str(upload.get(key, "")).strip():
                raise ConfigurationError(f"upload.{key} is required for upload")


def resolve_path(value: str | os.PathLike[str], *, project_root: Path = PROJECT_ROOT) -> Path:
    return _resolve_config_path(value, project_root).resolve()


def artifact_paths(config: Mapping[str, Any], *, project_root: Path = PROJECT_ROOT) -> dict[str, Path]:
    root = resolve_path(str(config["paths"]["artifacts"]), project_root=project_root)
    return {
        "root": root,
        "downloads": root / "downloads",
        "sources": root / "sources",
        "work": root / "work",
        "builds": root / "builds",
        "logs": root / "logs",
    }


def redacted(value: Any) -> Any:
    secret_fragments = ("password", "secret", "token", "api_key", "private_key")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            result[str(key)] = "<redacted>" if any(part in normalized for part in secret_fragments) else redacted(item)
        return result
    if isinstance(value, list):
        return [redacted(item) for item in value]
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return "<redacted-url>"
        if parsed.username is not None or parsed.query or parsed.fragment:
            try:
                hostname = parsed.hostname or ""
                if parsed.port is not None:
                    hostname = f"{hostname}:{parsed.port}"
            except ValueError:
                return "<redacted-url>"
            return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    return copy.deepcopy(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Optional local JSON configuration file")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="Validate effective configuration")
    validate_parser.add_argument("--upload", action="store_true", help="Require upload settings")
    subparsers.add_parser("show", help="Print effective configuration with secrets redacted")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        if args.command == "validate":
            validate_config(config, require_upload=args.upload)
            print("Configuration is valid.")
        else:
            print(json.dumps(redacted(config), indent=2, sort_keys=True))
    except ConfigurationError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
