"""Find LG webOS TVs on the local network without being told an IP.

1. The TV already paired (if it still answers).
2. SSDP: webOS TVs answer M-SEARCH for their second-screen service.
3. A sweep of the PC's own /24 subnets for the webOS ports 3001/3000.
Every candidate is then confirmed with the author's helper (probe).
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor

SSDP_TARGETS = ("urn:lge-com:service:webos-second-screen:1", "urn:dial-multiscreen-org:service:dial:1")


def port_open(ip: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def ssdp(timeout: float = 2.5) -> list[str]:
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.3)
            for target in SSDP_TARGETS:
                message = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                           f"MX: 2\r\nST: {target}\r\n\r\n").encode()
                sock.sendto(message, ("239.255.255.250", 1900))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, (ip, _port) = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                if re.search(r"webos|lge|LG", data.decode("latin-1", errors="replace")) and ip not in found:
                    found.append(ip)
    except OSError:
        pass            # multicast unavailable here: the subnet sweep still finds the TV
    return found


def private_networks(addresses) -> list[ipaddress.IPv4Network]:
    """The /24 home networks among this PC's IPv4 addresses."""
    networks: list[ipaddress.IPv4Network] = []
    for address in addresses:
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and not ip.is_link_local:
            network = ipaddress.IPv4Network(f"{address}/24", strict=False)
            if network not in networks:
                networks.append(network)
    return networks


def local_networks() -> list[ipaddress.IPv4Network]:
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))      # no packet is sent; picks the LAN interface
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass            # no default route; the host name lookup below may still find one
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass            # no name resolution; use what the route lookup found
    return private_networks(sorted(addresses))


def sweep(networks) -> list[str]:
    hosts = [str(h) for n in networks for h in n.hosts()]
    with ThreadPoolExecutor(max_workers=128) as pool:
        hits = pool.map(lambda ip: ip if port_open(ip, 3001) or port_open(ip, 3000) else None, hosts)
    return [ip for ip in hits if ip]


def discover(probe, known_ip: str = "", say=print) -> list[dict]:
    """Return confirmed LG TVs as probe results (ip, model_name, ...)."""
    tvs: list[dict] = []
    seen: set[str] = set()

    def confirm(candidates):
        for ip in candidates:
            if ip in seen:
                continue
            seen.add(ip)
            result = probe(ip)
            if result.get("status") == "ok" and result.get("is_lg_tv", True):
                result.setdefault("ip", ip)
                tvs.append(result)

    if known_ip and (port_open(known_ip, 3001) or port_open(known_ip, 3000)):
        confirm([known_ip])
        if tvs:
            return tvs
    say("Looking for the LG TV on the network ...")
    confirm(ssdp())
    if not tvs:
        networks = local_networks()
        if not networks:
            say("This PC has no home-network (private) address, so the TV cannot be searched for.")
        confirm(sweep(networks))
    return tvs
