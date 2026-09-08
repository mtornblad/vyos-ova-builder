#!/usr/bin/env python3
"""Resolve a VMware OVF network to its Linux interface by MAC address."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")


class InterfaceResolutionError(ValueError):
    """Raised when an OVF network cannot be mapped unambiguously."""


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def attribute(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if local_name(key) == name:
            return value
    return ""


def normalize_mac(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", value).lower()
    if len(normalized) != 12:
        raise InterfaceResolutionError("OVF adapter contains an invalid MAC address")
    return normalized


def network_matches(requested: str, actual: str) -> bool:
    # CCI can append an underscore and deployment-specific suffix to the
    # Kubernetes Subnet name in the OVF EthernetAdapterSection.
    return actual == requested or actual.startswith(f"{requested}_")


def resolve_interface(
    ovf_environment: str,
    requested_network: str,
    sys_class_net: Path = Path("/sys/class/net"),
) -> str:
    if not requested_network:
        raise InterfaceResolutionError("network name must not be empty")

    try:
        root = ET.fromstring(ovf_environment)
    except ET.ParseError as error:
        raise InterfaceResolutionError("VMware Tools returned invalid OVF XML") from error

    exact_adapter_macs = []
    suffixed_adapter_macs = []
    for element in root.iter():
        if local_name(element.tag) != "Adapter":
            continue
        actual_network = attribute(element, "network")
        if actual_network == requested_network:
            exact_adapter_macs.append(normalize_mac(attribute(element, "mac")))
        elif network_matches(requested_network, actual_network):
            suffixed_adapter_macs.append(normalize_mac(attribute(element, "mac")))

    adapter_macs = exact_adapter_macs or suffixed_adapter_macs

    if not adapter_macs:
        raise InterfaceResolutionError(
            f"OVF environment contains no adapter for network {requested_network!r}"
        )
    if len(adapter_macs) != 1:
        raise InterfaceResolutionError(
            f"OVF network {requested_network!r} matches more than one adapter"
        )

    matching_interfaces = []
    for candidate in sorted(sys_class_net.iterdir()):
        address_path = candidate / "address"
        try:
            candidate_mac = normalize_mac(address_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, InterfaceResolutionError, OSError, UnicodeError):
            continue
        if candidate_mac == adapter_macs[0]:
            matching_interfaces.append(candidate.name)

    if not matching_interfaces:
        raise InterfaceResolutionError(
            f"no Linux interface has the MAC address for OVF network {requested_network!r}"
        )
    if len(matching_interfaces) != 1:
        raise InterfaceResolutionError(
            f"OVF network {requested_network!r} maps to more than one Linux interface"
        )

    interface = matching_interfaces[0]
    if not INTERFACE_NAME.fullmatch(interface):
        raise InterfaceResolutionError("resolved Linux interface name is unsafe")
    return interface


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("network", help="OVF network name or its unsuffixed CCI name")
    parser.add_argument(
        "--sys-class-net",
        type=Path,
        default=Path("/sys/class/net"),
        help=argparse.SUPPRESS,
    )
    arguments = parser.parse_args(argv)

    try:
        interface = resolve_interface(
            sys.stdin.read(), arguments.network, arguments.sys_class_net
        )
    except (InterfaceResolutionError, OSError, UnicodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(interface)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
