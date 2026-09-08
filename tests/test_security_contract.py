from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SecurityContractTests(unittest.TestCase):
    def test_first_boot_script_does_not_use_shell_eval(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertNotIn("eval ", script)
        self.assertNotIn("DEFAULT_API", script)
        self.assertNotIn("set -o errexit", script)
        self.assertIn("builtin set -o nounset", script)
        self.assertIn("builtin set -o pipefail", script)
        self.assertLess(
            script.index('source "$SCRIPT_TEMPLATE"'),
            script.index("builtin set -o nounset"),
        )
        for legacy_name in (
            "mgmt_ip",
            "mgmt_mask",
            "mgmt_gw",
            "enable_rest",
            "config_blob",
        ):
            self.assertNotIn(legacy_name, script)

    def test_management_services_share_listen_address_behavior(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertIn("set service ssh port 22", script)
        self.assertIn("set service https api rest", script)
        self.assertNotIn("set service ssh listen-address", script)
        self.assertNotIn("set service https listen-address", script)

    def test_first_boot_script_does_not_log_supplemental_config_or_rest_key(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertNotRegex(script, r'log\s+.*\$CONFIG_BASE64_VALUE')
        self.assertNotRegex(script, r'log\s+.*\$REST_API_KEY_VALUE')
        self.assertLess(
            script.index('apply_extra_config "$CONFIG_BASE64_VALUE"'),
            script.index('set system host-name "$HOSTNAME_VALUE"'),
        )

    def test_first_boot_script_does_not_wait_for_its_parent_service(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertNotIn("systemctl", script)
        self.assertNotRegex(script, r"(?m)^\s*sleep\s")
        self.assertNotIn("while cli-shell-api inSession", script)

    def test_first_boot_script_selects_the_vyattacfg_primary_group(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertIn('exec /usr/bin/sg vyattacfg -c "/bin/vbash ${SCRIPT_PATH}"', script)
        self.assertIn('SET_COMMAND_TYPE="$(type -t set || true)"', script)

    def test_first_boot_script_closes_the_configuration_session(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn("cli-shell-api teardownSession", script)
        self.assertIn("builtin exit 1", script)
        self.assertIn("builtin exit 0", script)
        self.assertIsNone(re.search(r"(?m)^\s*exit\s+[01]\s*$", script))

    def test_postconfig_hook_contains_failure_and_allows_vyos_boot(self) -> None:
        hook = (PROJECT_ROOT / "files" / "vyos-postconfig-bootup.script").read_text(
            encoding="utf-8"
        )
        self.assertIn("if ! /usr/local/sbin/vyos-vapp-init; then", hook)
        self.assertIn("retried on the next boot", hook)
        self.assertRegex(hook, r"(?m)^builtin exit 0$")
        self.assertFalse((PROJECT_ROOT / "files" / "vapp-init.service").exists())

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
                "ssh_authorized_key",
                "rest",
                "rest_api_key",
                "config_base64",
            },
        )
        secret_properties = [item for item in template["properties"] if item.get("password")]
        ssh_property = next(item for item in template["properties"] if item["key"] == "ssh")
        rest_property = next(item for item in template["properties"] if item["key"] == "rest")
        self.assertTrue(secret_properties)
        self.assertTrue(all(item.get("value", "") == "" for item in secret_properties))
        self.assertEqual(ssh_property["value"], "false")
        self.assertEqual(rest_property["value"], "false")


if __name__ == "__main__":
    unittest.main()
