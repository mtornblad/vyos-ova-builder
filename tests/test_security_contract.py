from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SecurityContractTests(unittest.TestCase):
    def test_first_boot_script_does_not_use_shell_eval(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertNotIn("eval ", script)
        self.assertNotIn("DEFAULT_API", script)
        self.assertIn("set -o errexit", script)

    def test_every_password_property_has_an_empty_default(self) -> None:
        template = json.loads(
            (PROJECT_ROOT / "templates" / "vapp-properties.json").read_text(encoding="utf-8")
        )
        secret_properties = [item for item in template["properties"] if item.get("password")]
        ssh_property = next(item for item in template["properties"] if item["key"] == "enable_ssh")
        self.assertTrue(secret_properties)
        self.assertTrue(all(item.get("value", "") == "" for item in secret_properties))
        self.assertEqual(ssh_property["value"], "false")


if __name__ == "__main__":
    unittest.main()
