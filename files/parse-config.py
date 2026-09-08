#!/usr/bin/env python3
"""Parse supplemental VyOS configuration without evaluating shell code.

Input is UTF-8 text containing one VyOS ``set`` or ``delete`` command per
line. Output is one tab-separated line per command where every argument is
individually Base64 encoded. This gives the vApp initialization shell a safe,
unambiguous argv representation without using ``eval``.
"""

from __future__ import annotations

import base64
import shlex
import sys
from collections.abc import Iterable, Iterator


ALLOWED_COMMANDS = frozenset({"delete", "set"})


class ConfigurationParseError(ValueError):
    """Raised when supplemental configuration violates the input contract."""


def parse_commands(lines: Iterable[str]) -> Iterator[list[str]]:
    for line_number, line in enumerate(lines, start=1):
        try:
            arguments = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise ConfigurationParseError(
                f"line {line_number}: invalid quoting"
            ) from error

        if not arguments:
            continue
        if arguments[0] not in ALLOWED_COMMANDS:
            raise ConfigurationParseError(
                f"line {line_number}: only set and delete commands are allowed"
            )
        if len(arguments) < 2:
            raise ConfigurationParseError(
                f"line {line_number}: command requires a configuration path"
            )
        if any(
            any(ord(character) < 32 or ord(character) == 127 for character in item)
            for item in arguments
        ):
            raise ConfigurationParseError(
                f"line {line_number}: control characters are not allowed"
            )
        yield arguments


def encode_command(arguments: list[str]) -> str:
    return "\t".join(
        "x" + base64.b64encode(argument.encode("utf-8")).decode("ascii")
        for argument in arguments
    )


def main() -> int:
    try:
        for command in parse_commands(sys.stdin):
            print(encode_command(command))
    except (ConfigurationParseError, UnicodeError) as error:
        print(f"ERROR: supplemental configuration {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
