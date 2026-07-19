"""Local network interface discovery for shareable stream URLs."""

from __future__ import annotations

import socket
from ipaddress import ip_address


def get_local_ips() -> list[str]:
    """Return non-loopback IPv4 addresses currently assigned to this machine."""
    ips: set[str] = set()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            primary = sock.getsockname()[0]
            if _is_usable_lan(primary):
                ips.add(primary)
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if _is_usable_lan(addr):
                ips.add(addr)
    except OSError:
        pass

    # Fallback: bind and inspect common hostname resolution
    try:
        for info in socket.getaddrinfo(None, 0, socket.AF_INET, socket.SOCK_DGRAM):
            addr = info[4][0]
            if _is_usable_lan(addr):
                ips.add(addr)
    except OSError:
        pass

    ordered = sorted(ips, key=_sort_key)
    return ordered or ["127.0.0.1"]


def _is_usable_lan(addr: str) -> bool:
    try:
        ip = ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    return ip.version == 4


def _sort_key(addr: str) -> tuple[int, str]:
    # Prefer typical home/office private ranges first
    if addr.startswith("192.168."):
        return (0, addr)
    if addr.startswith("10."):
        return (1, addr)
    if addr.startswith("172."):
        return (2, addr)
    return (3, addr)


def primary_ip() -> str:
    ips = get_local_ips()
    return ips[0]
