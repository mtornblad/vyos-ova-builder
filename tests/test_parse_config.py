from __future__ import annotations

import base64
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARSER = PROJECT_ROOT / "files" / "parse-config.py"


def decode_commands(output: bytes) -> list[list[str]]:
    commands: list[list[str]] = []
    for line in output.splitlines():
        commands.append(
            [
                base64.b64decode(field.removeprefix(b"x"), validate=True).decode("utf-8")
                for field in line.split(b"\t")
            ]
        )
    return commands


class ParseConfigTests(unittest.TestCase):
    def run_parser(self, value: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(PARSER)],
            input=value.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_parses_set_delete_comments_and_quoted_values(self) -> None:
        result = self.run_parser(
            """
            # supplemental routing
            set interfaces ethernet eth1 address 172.16.100.1/24
            set system login banner pre-login "Managed lab router"
            delete interfaces ethernet eth2 address
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(
            decode_commands(result.stdout),
            [
                ["set", "interfaces", "ethernet", "eth1", "address", "172.16.100.1/24"],
                ["set", "system", "login", "banner", "pre-login", "Managed lab router"],
                ["delete", "interfaces", "ethernet", "eth2", "address"],
            ],
        )

    def test_shell_syntax_is_transported_as_literal_arguments(self) -> None:
        result = self.run_parser(
            'set system login banner pre-login "$(touch /tmp/must-not-run); value"\n'
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(
            decode_commands(result.stdout),
            [
                [
                    "set",
                    "system",
                    "login",
                    "banner",
                    "pre-login",
                    "$(touch /tmp/must-not-run); value",
                ]
            ],
        )

    def test_rejects_commands_outside_allowlist_without_echoing_input(self) -> None:
        secret = "do-not-repeat-this-secret"
        result = self.run_parser(f"run echo {secret}\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"only set and delete commands are allowed", result.stderr)
        self.assertNotIn(secret.encode(), result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_rejects_invalid_quoting(self) -> None:
        result = self.run_parser("set system host-name 'unterminated\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"invalid quoting", result.stderr)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
