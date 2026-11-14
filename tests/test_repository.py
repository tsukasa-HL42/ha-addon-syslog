"""
Checks on the repository itself, not on the forwarder.

This repository is public. Examples and test data must not carry addresses of
a real network: only loopback, the ranges reserved for documentation
(RFC 5737) and the internal network every Home Assistant OS install uses for
its containers are accepted, written forward or as a reverse lookup name.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ipaddress
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
SKIPPED_DIRS = {".git", "__pycache__", ".venv", "node_modules"}

ALLOWED = [ipaddress.ip_network(net) for net in (
    "0.0.0.0/32",
    "127.0.0.0/8",
    "192.0.2.0/24",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "172.30.32.0/23",
)]

REVERSE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){1,4})in-addr\.arpa")
FORWARD = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def allowed(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True  # not an address (a version number, for example)
    return any(parsed in net for net in ALLOWED)


def addresses(text: str):
    """Yield (written form, address) for every IPv4 address in the text."""
    for match in REVERSE.finditer(text):
        octets = match.group(1).rstrip(".").split(".")[::-1]
        yield match.group(0), ".".join(octets + ["0"] * (4 - len(octets)))
    for match in FORWARD.finditer(REVERSE.sub("", text)):
        yield match.group(0), match.group(0)


class AddressTests(unittest.TestCase):
    def test_reader_sees_both_forms(self):
        found = dict(addresses("x 198.51.100.7 y 9.2.0.192.in-addr.arpa. z"))
        self.assertEqual(
            found,
            {"198.51.100.7": "198.51.100.7",
             "9.2.0.192.in-addr.arpa": "192.0.2.9"},
        )
        self.assertFalse(allowed(".".join(["198", "18", "0", "1"])))

    def test_no_real_network_address(self):
        offenders = []
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS]
            for name in files:
                path = os.path.join(folder, name)
                try:
                    with open(path, encoding="utf-8") as handle:
                        lines = handle.read().splitlines()
                except (UnicodeDecodeError, OSError):
                    continue  # binary file
                for number, line in enumerate(lines, 1):
                    for written, address in addresses(line):
                        if not allowed(address):
                            offenders.append(
                                f"{os.path.relpath(path, ROOT)}:{number}: "
                                f"{written}"
                            )
        self.assertEqual(offenders, [], "addresses outside the accepted ranges")


if __name__ == "__main__":
    unittest.main()
