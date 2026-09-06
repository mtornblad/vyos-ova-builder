#!/usr/bin/env python3
"""Create a VMware OVA and inject configurable vApp properties."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

from project_config import PROJECT_ROOT, artifact_paths, load_config, resolve_path


OVF_NAMESPACE = "http://schemas.dmtf.org/ovf/envelope/1"
RASD_NAMESPACE = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
VMW_NAMESPACE = "http://www.vmware.com/schema/ovf"
VSSD_NAMESPACE = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"

for prefix, namespace in (
    ("ovf", OVF_NAMESPACE),
    ("rasd", RASD_NAMESPACE),
    ("vmw", VMW_NAMESPACE),
    ("vssd", VSSD_NAMESPACE),
):
    ET.register_namespace(prefix, namespace)


class OvaBuildError(RuntimeError):
    """Raised when OVA assembly fails."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_vapp_template(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            template = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise OvaBuildError(f"Unable to read vApp property template {path}: {error}") from error
    if template.get("schema") != "vyos.ova.builder.vapp-properties/v1":
        raise OvaBuildError(f"Unsupported vApp property schema in {path}")
    properties = template.get("properties")
    if not isinstance(properties, list) or not properties:
        raise OvaBuildError("vApp property template must contain properties")
    seen: set[str] = set()
    for property_definition in properties:
        if not isinstance(property_definition, dict):
            raise OvaBuildError("Every vApp property must be an object")
        key = str(property_definition.get("key", "")).strip()
        if not key or key in seen:
            raise OvaBuildError(f"Invalid or duplicate vApp property key: {key!r}")
        seen.add(key)
        if property_definition.get("password") and str(property_definition.get("value", "")):
            raise OvaBuildError(f"Password property {key!r} must have an empty default")
    return template


def inject_vapp_properties(ovf_path: Path, template: Mapping[str, Any]) -> None:
    try:
        tree = ET.parse(ovf_path)
    except (OSError, ET.ParseError) as error:
        raise OvaBuildError(f"Unable to parse OVF {ovf_path}: {error}") from error
    root = tree.getroot()
    virtual_hardware = root.find(f".//{{{OVF_NAMESPACE}}}VirtualHardwareSection")
    virtual_system = root.find(f".//{{{OVF_NAMESPACE}}}VirtualSystem")
    if virtual_hardware is None or virtual_system is None:
        raise OvaBuildError("OVF does not contain the expected VirtualSystem and VirtualHardwareSection")

    virtual_hardware.set(f"{{{OVF_NAMESPACE}}}transport", "com.vmware.guestInfo")

    product_class = str(template.get("class", "vapp"))
    for existing in list(virtual_system.findall(f"{{{OVF_NAMESPACE}}}ProductSection")):
        if existing.get(f"{{{OVF_NAMESPACE}}}class", "") == product_class:
            virtual_system.remove(existing)

    product = ET.Element(f"{{{OVF_NAMESPACE}}}ProductSection")
    product.set(f"{{{OVF_NAMESPACE}}}class", product_class)
    product.set(f"{{{OVF_NAMESPACE}}}required", "false")
    info = ET.SubElement(product, f"{{{OVF_NAMESPACE}}}Info")
    info.text = str(template.get("info", "VyOS first-boot configuration"))
    product_name = ET.SubElement(product, f"{{{OVF_NAMESPACE}}}Product")
    product_name.text = str(template.get("product", "VyOS Router"))

    for definition in template["properties"]:
        prop = ET.SubElement(product, f"{{{OVF_NAMESPACE}}}Property")
        prop.set(f"{{{OVF_NAMESPACE}}}key", str(definition["key"]))
        prop.set(f"{{{OVF_NAMESPACE}}}type", str(definition.get("type", "string")))
        prop.set(f"{{{OVF_NAMESPACE}}}userConfigurable", "true")
        prop.set(f"{{{OVF_NAMESPACE}}}value", str(definition.get("value", "")))
        if definition.get("password"):
            prop.set(f"{{{OVF_NAMESPACE}}}password", "true")
        label = ET.SubElement(prop, f"{{{OVF_NAMESPACE}}}Label")
        label.text = str(definition.get("label", definition["key"]))
        description = ET.SubElement(prop, f"{{{OVF_NAMESPACE}}}Description")
        description.text = str(definition.get("description", ""))

    hardware_index = list(virtual_system).index(virtual_hardware)
    virtual_system.insert(hardware_index, product)
    tree.write(ovf_path, encoding="utf-8", xml_declaration=True)


def render_vmx(config: Mapping[str, Any], disk_name: str) -> str:
    appliance = config["appliance"]

    def quote(value: object) -> str:
        text = str(value)
        if "\n" in text or "\r" in text:
            raise OvaBuildError("VMX values must not contain line breaks")
        return text.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        '.encoding = "UTF-8"',
        'config.version = "8"',
        f'virtualHW.version = "{appliance["hardware_version"]}"',
        f'displayName = "{quote(appliance["display_name"])}"',
        f'guestOS = "{quote(appliance["guest_os"])}"',
        f'numvcpus = "{appliance["cpus"]}"',
        f'memsize = "{appliance["memory_mb"]}"',
        'scsi0.present = "TRUE"',
        f'scsi0.virtualDev = "{quote(appliance["disk_controller"])}"',
        'scsi0:0.present = "TRUE"',
        f'scsi0:0.fileName = "{quote(disk_name)}"',
    ]
    for index in range(int(appliance["network_adapters"])):
        lines.extend(
            [
                f'ethernet{index}.present = "TRUE"',
                f'ethernet{index}.virtualDev = "vmxnet3"',
                f'ethernet{index}.networkName = "{quote(appliance["network_name"])}"',
                f'ethernet{index}.addressType = "generated"',
            ]
        )
    return "\n".join(lines) + "\n"


def write_manifest(directory: Path, ovf_path: Path, disk_paths: Sequence[Path]) -> Path:
    manifest_path = directory / f"{ovf_path.stem}.mf"
    members = [ovf_path, *sorted(disk_paths, key=lambda item: item.name)]
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        for member in members:
            handle.write(f"SHA256({member.name})= {sha256_file(member)}\n")
    return manifest_path


def create_ova(vmdk_path: Path, config: Mapping[str, Any]) -> Path:
    if not vmdk_path.is_file():
        raise OvaBuildError(f"VMDK does not exist: {vmdk_path}")
    ovftool = shutil.which("ovftool")
    if not ovftool:
        raise OvaBuildError("ovftool is required but was not found in PATH")

    paths = artifact_paths(config)
    work_root = paths["work"]
    build_root = paths["builds"]
    work_root.mkdir(parents=True, exist_ok=True)
    build_root.mkdir(parents=True, exist_ok=True)

    properties_path = resolve_path(str(config["vapp"]["properties_file"]))
    properties = load_vapp_template(properties_path)
    output_path = build_root / str(config["appliance"]["ova_name"])

    with tempfile.TemporaryDirectory(prefix="ova-", dir=work_root) as temporary_name:
        temporary = Path(temporary_name)
        source_directory = temporary / "source"
        package_directory = temporary / "package"
        source_directory.mkdir()
        package_directory.mkdir()
        local_disk = source_directory / "disk.vmdk"
        vmx_path = source_directory / "VyOS.vmx"
        ovf_path = package_directory / "VyOS.ovf"
        shutil.copy2(vmdk_path, local_disk)
        vmx_path.write_text(render_vmx(config, local_disk.name), encoding="utf-8")

        ovftool_environment = os.environ.copy()
        ovftool_environment["LC_ALL"] = "C"
        subprocess.run(
            [ovftool, "--acceptAllEulas", str(vmx_path), str(ovf_path)],
            env=ovftool_environment,
            check=True,
        )
        inject_vapp_properties(ovf_path, properties)
        disk_paths = list(package_directory.glob("*.vmdk"))
        if not disk_paths:
            raise OvaBuildError("ovftool did not produce a VMDK")
        manifest_path = write_manifest(package_directory, ovf_path, disk_paths)

        staged_output = temporary / output_path.name
        with tarfile.open(staged_output, mode="w") as archive:
            archive.add(ovf_path, arcname=ovf_path.name)
            archive.add(manifest_path, arcname=manifest_path.name)
            for disk in sorted(disk_paths, key=lambda item: item.name):
                archive.add(disk, arcname=disk.name)
        pending_output = output_path.with_suffix(output_path.suffix + ".part")
        os.replace(staged_output, pending_output)
        os.replace(pending_output, output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vmdk", type=Path, help="VyOS VMDK generated by vyos-build")
    parser.add_argument("--config", help="Optional local JSON configuration file")
    args = parser.parse_args()
    try:
        output = create_ova(args.vmdk.resolve(), load_config(args.config))
    except (OvaBuildError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Created {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
