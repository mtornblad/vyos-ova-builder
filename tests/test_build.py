from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import build as build_module  # noqa: E402
from project_config import load_config  # noqa: E402


class BuildTests(unittest.TestCase):
    def test_source_url_user_information_is_removed(self) -> None:
        value = build_module.safe_location(
            "https://builder:private@example.invalid/org/repo.git?token=secret"
        )
        self.assertEqual(value, "https://example.invalid/org/repo.git")
        self.assertNotIn("private", value)
        query_only = build_module.safe_location(
            "https://example.invalid/org/repo.git?token=private"
        )
        self.assertEqual(query_only, "https://example.invalid/org/repo.git")

    def test_customization_is_confined_to_disposable_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            checkout = Path(temporary_name) / "checkout"
            (checkout / "data" / "build-flavors").mkdir(parents=True)
            (checkout / "data" / "live-build-config" / "includes.chroot").mkdir(parents=True)
            config = copy.deepcopy(load_config(environ={}, include_default_local=False))
            build_module.customize_source(checkout, config)

            init_script = (
                checkout
                / "data/live-build-config/includes.chroot/usr/local/sbin/vyos-vapp-init"
            )
            service_link = (
                checkout
                / "data/live-build-config/includes.chroot/etc/systemd/system/"
                "multi-user.target.wants/vapp-init.service"
            )
            self.assertTrue(init_script.is_file())
            self.assertTrue(init_script.stat().st_mode & 0o100)
            self.assertTrue(service_link.is_symlink())
            self.assertEqual(service_link.readlink(), Path("../vapp-init.service"))

    def test_owned_directory_guard_rejects_root_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            with self.assertRaisesRegex(build_module.BuildError, "outside"):
                build_module.reset_owned_directory(root, root)

    def test_build_manifest_excludes_upload_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            ova = temporary / "vyos.ova"
            ova.write_bytes(b"test ova")
            config = copy.deepcopy(load_config(environ={}, include_default_local=False))
            config["upload"]["password"] = "must-not-appear"
            manifest_path = build_module.write_build_manifest(
                config,
                source_commit="a" * 40,
                source="<local checkout>",
                ova_path=ova,
            )
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertNotIn("upload", manifest["configuration"])
            self.assertNotIn("must-not-appear", manifest_text)

    def test_source_checkout_is_disposable_and_refreshes_from_local_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source"
            subprocess.run(
                ["git", "init", "--initial-branch", "rolling", str(source)], check=True
            )
            (source / "version.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "version.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Builder Test",
                    "-c",
                    "user.email=builder@example.invalid",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=source,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            config = copy.deepcopy(load_config(environ={}, include_default_local=False))
            config["source"]["directory"] = str(source)
            config["paths"]["artifacts"] = str(temporary / "artifacts")

            checkout, first_commit = build_module.prepare_source(config)
            self.assertEqual((checkout / "version.txt").read_text(encoding="utf-8"), "one\n")

            (source / "version.txt").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "add", "version.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Builder Test",
                    "-c",
                    "user.email=builder@example.invalid",
                    "commit",
                    "-m",
                    "second",
                ],
                cwd=source,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            refreshed_checkout, second_commit = build_module.prepare_source(config)
            self.assertNotEqual(first_commit, second_commit)
            self.assertEqual(
                (refreshed_checkout / "version.txt").read_text(encoding="utf-8"), "two\n"
            )

    def test_local_source_uses_detached_head_instead_of_named_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source"
            subprocess.run(["git", "init", "--initial-branch", "rolling", str(source)], check=True)
            (source / "version.txt").write_text("pinned\n", encoding="utf-8")
            subprocess.run(["git", "add", "version.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Builder Test",
                    "-c",
                    "user.email=builder@example.invalid",
                    "commit",
                    "-m",
                    "pinned",
                ],
                cwd=source,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            pinned_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            (source / "version.txt").write_text("branch tip\n", encoding="utf-8")
            subprocess.run(["git", "add", "version.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Builder Test",
                    "-c",
                    "user.email=builder@example.invalid",
                    "commit",
                    "-m",
                    "branch tip",
                ],
                cwd=source,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "checkout", "--detach", pinned_commit], cwd=source, check=True
            )

            config = copy.deepcopy(load_config(environ={}, include_default_local=False))
            config["source"]["directory"] = str(source)
            config["paths"]["artifacts"] = str(temporary / "artifacts")

            checkout, selected_commit = build_module.prepare_source(config)

            self.assertEqual(selected_commit, pinned_commit)
            self.assertEqual((checkout / "version.txt").read_text(encoding="utf-8"), "pinned\n")


if __name__ == "__main__":
    unittest.main()
