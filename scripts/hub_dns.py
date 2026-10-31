"""A DNS answerer for the hub's own domain only: <domain> and *.<domain> are the hub's address, nothing else.

The system routes just that domain here (systemd-resolved through a NetworkManager link, /etc/resolver on macOS, an
NRPT rule on Windows; see installers/local_dns.sh and docs/RUNBOOK.md §8), so claude.cortex.local reaches the hub with
no hosts entries and every new agent's host works at once. Any other name is refused, so it never answers for the
rest of the world. Standard library only.
"""
import ipaddress
import socket
import struct
import threading

TTL = 60
A, AAAA, ANY = 1, 28, 255


def _qname(packet: bytes, i: int) -> tuple[str, int]:
    labels = []
    while True:
        n = packet[i]
        if n == 0:
            return ".".join(labels), i + 1
        if n & 0xC0 or len(labels) > 127:   # no compression in a question
            raise ValueError("bad name")
        labels.append(packet[i + 1:i + 1 + n].decode("ascii").lower())
        i += 1 + n


def answer(query: bytes, zones: dict[str, str]) -> bytes | None:
    """The reply to one query packet ({domain: address}), or None for a packet that is not a standard query."""
    if len(query) < 12:
        return None
    qid, flags, qd = struct.unpack("!HHH", query[:6])
    if flags & 0x8000 or (flags >> 11) & 0xF or qd != 1:   # a reply, not QUERY, or not one question
        return None
    try:
        name, end = _qname(query, 12)
        qtype, qclass = struct.unpack("!HH", query[end:end + 4])
    except (ValueError, IndexError, UnicodeDecodeError, struct.error):
        return None
    question = query[12:end + 4]
    rd = flags & 0x0100
    zone = next((d for d in sorted(zones, key=len, reverse=True) if name == d or name.endswith("." + d)), None)
    if zone is None:
        return struct.pack("!HHHHHH", qid, 0x8000 | rd | 5, 1, 0, 0, 0) + question   # REFUSED
    ip = ipaddress.ip_address(zones[zone])
    want = {A: 4, AAAA: 6, ANY: ip.version}.get(qtype)
    records = b""
    if qclass == 1 and want == ip.version:
        records = struct.pack("!HHHIH", 0xC00C, A if ip.version == 4 else AAAA, 1, TTL, len(ip.packed)) + ip.packed
    # authoritative; a name of ours with no record of that type is NOERROR with no answer
    return struct.pack("!HHHHHH", qid, 0x8400 | rd, 1, 1 if records else 0, 0, 0) + question + records


def serve(host: str, port: int, zones: dict[str, str]) -> threading.Thread:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))

    def loop():
        while True:
            data, peer = sock.recvfrom(512)
            reply = answer(data, zones)
            if reply:
                sock.sendto(reply, peer)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
