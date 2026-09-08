from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "files" / "resolve_interface.py"
SPEC = importlib.util.spec_from_file_location("resolve_interface", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


def ovf_environment(*adapters: tuple[str, str]) -> str:
    adapter_xml = "\n".join(
        f'<ve:Adapter ve:mac="{mac}" ve:network="{network}" ve:unitNumber="7"/>'
        for network, mac in adapters
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Environment xmlns="http://schemas.dmtf.org/ovf/environment/1"
             xmlns:ve="http://www.vmware.com/schema/ovfenv">
  <ve:EthernetAdapterSection>
    {adapter_xml}
  </ve:EthernetAdapterSection>
</Environment>
"""


def add_interface(root: Path, name: str, mac: str) -> None:
    interface = root / name
    interface.mkdir()
    (interface / "address").write_text(f"{mac}\n", encoding="ascii")


class ResolveInterfaceTests(unittest.TestCase):
    def test_maps_swapped_interfaces_by_ovf_mac(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            sys_class_net = Path(temporary_name)
            add_interface(sys_class_net, "eth0", "00:50:56:aa:00:02")
            add_interface(sys_class_net, "eth1", "00:50:56:aa:00:01")
            environment = ovf_environment(
                ("uplink_ab123", "00:50:56:AA:00:01"),
                ("trunk_ab123", "00:50:56:AA:00:02"),
            )

            self.assertEqual(
                resolver.resolve_interface(environment, "uplink", sys_class_net),
                "eth1",
            )
            self.assertEqual(
                resolver.resolve_interface(environment, "trunk", sys_class_net),
                "eth0",
            )

    def test_accepts_an_exact_ovf_network_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            sys_class_net = Path(temporary_name)
            add_interface(sys_class_net, "ens192", "00:50:56:aa:00:03")
            environment = ovf_environment(("management", "00:50:56:AA:00:03"))

            self.assertEqual(
                resolver.resolve_interface(environment, "management", sys_class_net),
                "ens192",
            )

    def test_prefers_an_exact_match_over_a_suffixed_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            sys_class_net = Path(temporary_name)
            add_interface(sys_class_net, "eth0", "00:50:56:aa:00:01")
            add_interface(sys_class_net, "eth1", "00:50:56:aa:00:02")
            environment = ovf_environment(
                ("uplink", "00:50:56:AA:00:01"),
                ("uplink_backup", "00:50:56:AA:00:02"),
            )

            self.assertEqual(
                resolver.resolve_interface(environment, "uplink", sys_class_net),
                "eth0",
            )

    def test_rejects_ambiguous_network_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            sys_class_net = Path(temporary_name)
            add_interface(sys_class_net, "eth0", "00:50:56:aa:00:01")
            add_interface(sys_class_net, "eth1", "00:50:56:aa:00:02")
            environment = ovf_environment(
                ("uplink_one", "00:50:56:AA:00:01"),
                ("uplink_two", "00:50:56:AA:00:02"),
            )

            with self.assertRaisesRegex(
                resolver.InterfaceResolutionError, "more than one adapter"
            ):
                resolver.resolve_interface(environment, "uplink", sys_class_net)

    def test_rejects_an_adapter_missing_from_linux(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            environment = ovf_environment(("uplink", "00:50:56:AA:00:01"))

            with self.assertRaisesRegex(
                resolver.InterfaceResolutionError, "no Linux interface"
            ):
                resolver.resolve_interface(
                    environment, "uplink", Path(temporary_name)
                )


if __name__ == "__main__":
    unittest.main()
