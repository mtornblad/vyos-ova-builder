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
            "config_blob",
        ):
            self.assertNotIn(legacy_name, script)
        self.assertNotRegex(script, r"get_ovf_property (?:ssh|rest)\)")

    def test_management_services_share_listen_address_behavior(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertIn("set service ssh port 22", script)
        self.assertIn("set service https api rest", script)
        self.assertNotIn("set service ssh listen-address", script)
        self.assertNotIn("set service https listen-address", script)

    def test_network_interfaces_are_resolved_before_configuration(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        self.assertIn('get_ovf_property management_network', script)
        self.assertIn('get_ovf_property trunk_network', script)
        self.assertIn('readonly DEFAULT_MANAGEMENT_INTERFACE="eth0"', script)
        self.assertNotIn('readonly BASE_INTERFACE=', script)
        self.assertIn('"$INTERFACE_RESOLVER" "$network_name"', script)
        self.assertIn('decoded_argument="$MANAGEMENT_INTERFACE"', script)
        self.assertIn('decoded_argument="$TRUNK_INTERFACE"', script)
        self.assertLess(
            script.index('resolve_network_interface management_network'),
            script.index('apply_extra_config "$CONFIG_BASE64_VALUE"'),
        )

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

    def test_first_boot_preserves_interactive_commit_permissions(self) -> None:
        script = (PROJECT_ROOT / "files" / "vapp-init.sh").read_text(encoding="utf-8")
        invocation = "\nnormalize_config_archive_permissions\n"
        self.assertIn(
            'readonly CONFIG_ARCHIVE_DIR="/opt/vyatta/etc/config/archive"',
            script,
        )
        self.assertIn('chown root:vyattacfg "$CONFIG_ARCHIVE_DIR"', script)
        self.assertIn('chmod 2775 "$CONFIG_ARCHIVE_DIR"', script)
        self.assertIn('chown root:vyattacfg "$COMMIT_LOG_FILE"', script)
        self.assertIn('chmod 0664 "$COMMIT_LOG_FILE"', script)
        self.assertEqual(script.count(invocation), 2)

        first_repair = script.index(invocation)
        second_repair = script.index(invocation, first_repair + len(invocation))
        self.assertLess(first_repair, script.index('commit || fail'))
        self.assertGreater(second_repair, script.index('save || fail'))
        self.assertNotIn("chown -R", script)
        self.assertNotIn("chmod -R", script)

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
                "enable_ssh",
                "ssh_authorized_key",
                "enable_rest",
                "rest_api_key",
                "management_network",
                "trunk_network",
                "config_base64",
            },
        )
        secret_properties = [item for item in template["properties"] if item.get("password")]
        ssh_property = next(
            item for item in template["properties"] if item["key"] == "enable_ssh"
        )
        rest_property = next(
            item for item in template["properties"] if item["key"] == "enable_rest"
        )
        self.assertTrue(secret_properties)
        self.assertTrue(all(item.get("value", "") == "" for item in secret_properties))
        self.assertEqual(ssh_property["value"], "false")
        self.assertEqual(rest_property["value"], "false")


if __name__ == "__main__":
    unittest.main()
