from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
            config_parser = (
                checkout
                / "data/live-build-config/includes.chroot/usr/local/libexec/"
                "vyos-ova-parse-config"
            )
            interface_resolver = (
                checkout
                / "data/live-build-config/includes.chroot/usr/local/libexec/"
                "vyos-ova-resolve-interface"
            )
            postconfig_script = (
                checkout
                / "data/live-build-config/includes.chroot/opt/vyatta/etc/config/scripts/"
                "vyos-postconfig-bootup.script"
            )
            legacy_service = (
                checkout
                / "data/live-build-config/includes.chroot/etc/systemd/system/vapp-init.service"
            )
            self.assertTrue(init_script.is_file())
            self.assertTrue(init_script.stat().st_mode & 0o100)
            self.assertTrue(config_parser.is_file())
            self.assertTrue(config_parser.stat().st_mode & 0o100)
            self.assertTrue(interface_resolver.is_file())
            self.assertTrue(interface_resolver.stat().st_mode & 0o100)
            self.assertTrue(postconfig_script.is_file())
            self.assertTrue(postconfig_script.stat().st_mode & 0o100)
            self.assertFalse(legacy_service.exists())

    def test_container_context_includes_ownership_cleanup_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            syft_package = temporary / "syft.deb"
            syft_package.write_bytes(b"test package")
            config = copy.deepcopy(load_config(environ={}, include_default_local=False))
            config["paths"]["artifacts"] = str(temporary / "artifacts")

            with mock.patch.object(build_module, "run"):
                build_module.build_container_image(config, syft_package)

            context = temporary / "artifacts" / "work" / "docker-context"
            wrapper = context / "run-build.sh"
            dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertTrue(wrapper.is_file())
            self.assertTrue(wrapper.stat().st_mode & 0o100)
            self.assertIn("COPY run-build.sh /usr/local/bin/vyos-ova-run-build", dockerfile)

    def test_privileged_build_restores_host_uid_and_gid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            checkout = Path(temporary_name) / "checkout"
            (checkout / "build").mkdir(parents=True)
            expected_vmdk = checkout / "build" / "vyos.vmdk"
            expected_vmdk.write_bytes(b"vmdk")
            config = copy.deepcopy(load_config(environ={}, include_default_local=False))

            with (
                mock.patch.object(build_module, "run") as run_mock,
                mock.patch.object(build_module.os, "getuid", return_value=1234),
                mock.patch.object(build_module.os, "getgid", return_value=5678),
            ):
                result = build_module.build_vmdk(checkout, config)

            command = run_mock.call_args.args[0]
            image_index = command.index(str(config["build"]["docker_image"]))
            self.assertEqual(
                command[image_index + 1 : image_index + 5],
                [
                    "/usr/local/bin/vyos-ova-run-build",
                    "1234",
                    "5678",
                    "./build-vyos-image",
                ],
            )
            self.assertEqual(result, expected_vmdk)

    def test_permission_repair_is_confined_to_builder_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            work_root = Path(temporary_name) / "work"
            checkout = work_root / "vyos-build"
            checkout.mkdir(parents=True)

            with mock.patch.object(build_module, "run") as run_mock:
                build_module.repair_directory_ownership(
                    checkout,
                    work_root,
                    "builder:test",
                    uid=1234,
                    gid=5678,
                )

            command = run_mock.call_args.args[0]
            self.assertEqual(command[:3], ["docker", "run", "--rm"])
            self.assertIn(f"{checkout.resolve()}:/builder-work", command)
            self.assertIn("builder:test", command)
            self.assertIn("1234:5678", command)
            self.assertEqual(
                (checkout / build_module.OWNERSHIP_MARKER_NAME).read_text(
                    encoding="utf-8"
                ),
                "1234:5678\n",
            )

            with self.assertRaisesRegex(build_module.BuildError, "outside"):
                build_module.repair_directory_ownership(
                    work_root,
                    work_root,
                    "builder:test",
                    uid=1234,
                    gid=5678,
                )

    def test_foreign_checkout_is_repaired_when_cleanup_marker_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            work_root = Path(temporary_name) / "work"
            checkout = work_root / "vyos-build"
            checkout.mkdir(parents=True)

            with (
                mock.patch.object(build_module.os, "getuid", return_value=1234),
                mock.patch.object(build_module.os, "getgid", return_value=5678),
                mock.patch.object(
                    build_module,
                    "directory_has_foreign_owner",
                    return_value=True,
                ),
                mock.patch.object(build_module, "repair_directory_ownership") as repair_mock,
            ):
                build_module.ensure_directory_ownership(
                    checkout,
                    work_root,
                    "builder:test",
                )

            repair_mock.assert_called_once_with(
                checkout.resolve(),
                work_root,
                "builder:test",
                uid=1234,
                gid=5678,
            )

    def test_valid_cleanup_marker_skips_recursive_ownership_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            work_root = Path(temporary_name) / "work"
            checkout = work_root / "vyos-build"
            checkout.mkdir(parents=True)
            (checkout / build_module.OWNERSHIP_MARKER_NAME).write_text(
                "1234:5678\n", encoding="utf-8"
            )

            with (
                mock.patch.object(build_module.os, "getuid", return_value=1234),
                mock.patch.object(build_module.os, "getgid", return_value=5678),
                mock.patch.object(build_module, "directory_has_foreign_owner") as scan_mock,
            ):
                build_module.ensure_directory_ownership(
                    checkout,
                    work_root,
                    "builder:test",
                )

            scan_mock.assert_not_called()

    def test_cleanup_wrapper_marks_only_successful_ownership_repair(self) -> None:
        wrapper = (PROJECT_ROOT / "docker" / "run-build.sh").read_text(encoding="utf-8")
        self.assertIn("trap cleanup EXIT", wrapper)
        self.assertIn('find "$WORKSPACE" -xdev', wrapper)
        self.assertIn('rm -f -- "$OWNERSHIP_MARKER"', wrapper)
        self.assertIn('sudo -- "$@"', wrapper)

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
