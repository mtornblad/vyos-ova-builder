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
        for legacy_name in (
            "mgmt_ip",
            "mgmt_mask",
            "mgmt_gw",
            "enable_rest",
            "rest_api_key",
            "config_blob",
        ):
            self.assertNotIn(legacy_name, script)

    def test_every_password_property_has_an_empty_default(self) -> None:
        template = json.loads(
            (PROJECT_ROOT / "templates" / "vapp-properties.json").read_text(encoding="utf-8")
        )
        self.assertEqual(template["class"], "guestinfo")
        self.assertEqual(
            {item["key"] for item in template["properties"]},
            {
                "hostname",
                "password",
                "ipaddress",
                "netmask",
                "gateway",
                "dns",
                "domain",
                "ntp",
                "vlan",
                "ssh",
            },
        )
        secret_properties = [item for item in template["properties"] if item.get("password")]
        ssh_property = next(item for item in template["properties"] if item["key"] == "ssh")
        self.assertTrue(secret_properties)
        self.assertTrue(all(item.get("value", "") == "" for item in secret_properties))
        self.assertEqual(ssh_property["value"], "false")


if __name__ == "__main__":
    unittest.main()
