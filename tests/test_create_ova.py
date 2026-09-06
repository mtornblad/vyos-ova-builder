from __future__ import annotations

import copy
import json
import sys
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import create_ova as ova_module  # noqa: E402
from project_config import load_config  # noqa: E402


OVF = "http://schemas.dmtf.org/ovf/envelope/1"


def minimal_ovf() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ovf:Envelope xmlns:ovf="{OVF}">
  <ovf:VirtualSystem ovf:id="VyOS">
    <ovf:Info>Virtual system</ovf:Info>
    <ovf:VirtualHardwareSection>
      <ovf:Info>Virtual hardware</ovf:Info>
    </ovf:VirtualHardwareSection>
  </ovf:VirtualSystem>
</ovf:Envelope>
"""


class CreateOvaTests(unittest.TestCase):
    def test_template_injection_adds_transport_and_empty_secret_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            ovf_path = Path(temporary_name) / "VyOS.ovf"
            ovf_path.write_text(minimal_ovf(), encoding="utf-8")
            template = ova_module.load_vapp_template(
                PROJECT_ROOT / "templates" / "vapp-properties.json"
            )
            ova_module.inject_vapp_properties(ovf_path, template)
            root = ET.parse(ovf_path).getroot()

        hardware = root.find(f".//{{{OVF}}}VirtualHardwareSection")
        self.assertEqual(hardware.get(f"{{{OVF}}}transport"), "com.vmware.guestInfo")
        properties = root.findall(f".//{{{OVF}}}ProductSection/{{{OVF}}}Property")
        self.assertGreater(len(properties), 5)
        secret_properties = [
            item for item in properties if item.get(f"{{{OVF}}}password") == "true"
        ]
        self.assertTrue(secret_properties)
        self.assertTrue(all(item.get(f"{{{OVF}}}value") == "" for item in secret_properties))

    def test_password_default_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            path = Path(temporary_name) / "properties.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "vyos.ova.builder.vapp-properties/v1",
                        "properties": [
                            {"key": "password", "password": True, "value": "known-secret"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ova_module.OvaBuildError, "empty default"):
                ova_module.load_vapp_template(path)

    def test_vmx_contains_requested_number_of_nics_and_escapes_values(self) -> None:
        config = copy.deepcopy(load_config(environ={}))
        config["appliance"]["network_adapters"] = 3
        config["appliance"]["display_name"] = 'VyOS "Lab"'
        vmx = ova_module.render_vmx(config, "disk.vmdk")
        self.assertEqual(vmx.count('.present = "TRUE"'), 5)
        self.assertIn('displayName = "VyOS \\"Lab\\""', vmx)
        self.assertIn('ethernet2.virtualDev = "vmxnet3"', vmx)

    def test_complete_ova_packaging_uses_only_ovftool_output_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            input_disk = temporary / "input.vmdk"
            input_disk.write_bytes(b"input-vmdk")
            config = copy.deepcopy(load_config(environ={}))
            config["paths"]["artifacts"] = str(temporary / "artifacts")
            config["vapp"]["properties_file"] = str(
                PROJECT_ROOT / "templates" / "vapp-properties.json"
            )

            def fake_ovftool(command: list[str], *, env: dict[str, str], check: bool) -> None:
                self.assertTrue(check)
                self.assertEqual(env["LC_ALL"], "C")
                output_ovf = Path(command[-1])
                output_ovf.write_text(minimal_ovf(), encoding="utf-8")
                (output_ovf.parent / "VyOS-disk1.vmdk").write_bytes(b"converted-vmdk")

            with mock.patch.object(ova_module.shutil, "which", return_value="/opt/ovftool"), mock.patch.object(
                ova_module.subprocess, "run", side_effect=fake_ovftool
            ):
                output = ova_module.create_ova(input_disk, config)

            with tarfile.open(output, "r") as archive:
                names = archive.getnames()
                self.assertEqual(names, ["VyOS.ovf", "VyOS.mf", "VyOS-disk1.vmdk"])
                manifest = archive.extractfile("VyOS.mf").read().decode("utf-8")
            self.assertIn("SHA256(VyOS.ovf)=", manifest)
            self.assertIn("SHA256(VyOS-disk1.vmdk)=", manifest)
            self.assertNotIn("input.vmdk", names)


if __name__ == "__main__":
    unittest.main()
