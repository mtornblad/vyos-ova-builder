from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from project_config import (  # noqa: E402
    ConfigurationError,
    load_config,
    redacted,
    validate_config,
)


class ProjectConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self) -> None:
        config = load_config(environ={}, include_default_local=False)
        self.assertEqual(config["schema"], "vyos.ova.builder.config/v1")
        self.assertEqual(config["build"]["architecture"], "amd64")

    def test_implicit_local_config_can_be_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            project_root = Path(temporary_name)
            config_directory = project_root / "config"
            config_directory.mkdir()
            (config_directory / "defaults.json").write_text(
                (PROJECT_ROOT / "config" / "defaults.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (config_directory / "local.json").write_text(
                json.dumps({"upload": {"vcenter_url": "https://local.example.invalid"}}),
                encoding="utf-8",
            )

            with_local = load_config(environ={}, project_root=project_root)
            defaults_only = load_config(
                environ={},
                project_root=project_root,
                include_default_local=False,
            )

        self.assertEqual(with_local["upload"]["vcenter_url"], "https://local.example.invalid")
        self.assertEqual(defaults_only["upload"]["vcenter_url"], "")

    def test_precedence_is_defaults_then_local_then_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            local_path = Path(temporary_name) / "private.json"
            local_path.write_text(
                json.dumps(
                    {
                        "schema": "vyos.ova.builder.config/v1",
                        "build": {"build_by": "local@example.invalid"},
                        "upload": {"password": "local-secret"},
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(
                local_path,
                environ={
                    "VYOS_OVA_BUILD_BY": "environment@example.invalid",
                    "GOVC_PASSWORD": "alias-secret",
                    "VYOS_OVA_VCENTER_PASSWORD": "prefixed-secret",
                    "VYOS_OVA_CPUS": "4",
                },
            )

        self.assertEqual(config["build"]["build_by"], "environment@example.invalid")
        self.assertEqual(config["upload"]["password"], "prefixed-secret")
        self.assertEqual(config["appliance"]["cpus"], 4)

    def test_selected_missing_config_is_an_error(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "does not exist"):
            load_config("config/not-present.json", environ={})

    def test_secrets_are_redacted_recursively(self) -> None:
        value = {
            "password": "do-not-print",
            "nested": {
                "api_key": "also-private",
                "hostname": "vyos",
                "repository": "https://user:secret@example.invalid/repo.git?token=private",
            },
        }
        result = redacted(value)
        self.assertEqual(result["password"], "<redacted>")
        self.assertEqual(result["nested"]["api_key"], "<redacted>")
        self.assertEqual(result["nested"]["hostname"], "vyos")
        self.assertEqual(result["nested"]["repository"], "https://example.invalid/repo.git")
        self.assertNotIn("do-not-print", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_upload_validation_requires_all_target_values(self) -> None:
        config = load_config(environ={}, include_default_local=False)
        with self.assertRaisesRegex(ConfigurationError, "upload.vcenter_url"):
            validate_config(config, require_upload=True)

    def test_flavor_cannot_escape_build_flavor_directory(self) -> None:
        config = copy.deepcopy(load_config(environ={}, include_default_local=False))
        config["build"]["flavor"] = "../generic"
        with self.assertRaisesRegex(ConfigurationError, "filename-safe"):
            validate_config(config)

    def test_arm64_is_rejected_until_a_matching_dependency_is_pinned(self) -> None:
        config = copy.deepcopy(load_config(environ={}, include_default_local=False))
        config["build"]["architecture"] = "arm64"
        with self.assertRaisesRegex(ConfigurationError, "currently pinned"):
            validate_config(config)

    def test_unknown_private_setting_is_not_silently_ignored(self) -> None:
        config = copy.deepcopy(load_config(environ={}, include_default_local=False))
        config["upload"]["passwrod"] = "typo"
        with self.assertRaisesRegex(ConfigurationError, "Unknown upload"):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
