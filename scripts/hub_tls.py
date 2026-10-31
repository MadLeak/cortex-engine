"""The Cortex hub's own certificate authority, made with the openssl command on first start.

The CA can sign only the hub's own domains and private addresses (an X.509 name constraint): cortex.local,
cortex.home, localhost and their subdomains, loopback and the RFC 1918 ranges. Trusting its ca.crt therefore trusts no
public site, even if its key leaked. Because the constraint does not name this machine's address or name, changing
them only renews the server certificate: the CA, and the trust each machine gave it, stay. The server certificate
lasts 397 days and is renewed on start when fewer than 30 remain, or at once when the names it needs change.
"""
import ipaddress
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

CA_DAYS = 3650
PRIVATE = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")   # the CA may sign these only
LEAF_DAYS = 397
RENEW_DAYS = 30
HOSTNAME = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")


def scope(address: str) -> str:
    """'localhost' or 'lan' for the address browsers reach the hub at; anything public is refused."""
    ip = ipaddress.ip_address(address)
    if ip.is_loopback:
        return "localhost"
    if ip.is_private and not (ip.is_unspecified or ip.is_multicast):
        return "lan"
    raise ValueError(f"{address} is neither loopback nor a private network address: the hub serves localhost or the local network only")


def names(domain: str, address: str) -> tuple[list[str], list[str]]:
    """The DNS names and IP addresses the certificate covers (and browsers may use)."""
    domain = domain.strip().lower().rstrip(".")
    if not HOSTNAME.fullmatch(domain):
        raise ValueError(f"{domain!r} is not a host name")
    dns, ips = [domain], [str(ipaddress.ip_address(address))]
    if "." in domain:   # every agent's panel at <agent>.<domain>; the name constraint on the domain covers them
        dns.append(f"*.{domain}")
    if scope(address) == "localhost":
        dns += [d for d in ("localhost",) if d not in dns]
        ips += [i for i in ("127.0.0.1", "::1") if i not in ips]
    return dns, ips


def _openssl(*args: str) -> str:
    r = subprocess.run(["openssl", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"openssl {args[0]} failed: {r.stderr.strip()}")
    return r.stdout


def ensure(tls: Path, dns: list[str], ips: list[str], zones: list[str]) -> tuple[Path, Path, list[str]]:
    """The server certificate chain and key under tls/, made or renewed as needed; returns what was done."""
    tls.mkdir(parents=True, exist_ok=True)
    os.chmod(tls, 0o700)
    ca_key, ca_crt, key, crt = tls / "ca.key", tls / "ca.crt", tls / "hub.key", tls / "hub.crt"
    want = {"dns": dns, "ips": ips, "ca": {"dns": zones, "nets": list(PRIVATE)}}   # zones: what the CA may sign
    try:
        have = json.loads((tls / "names.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        have = None
    done = []
    old = os.umask(0o077)
    try:
        same_ca = have is not None and have.get("ca") == want["ca"]
        if not same_ca or not (ca_key.is_file() and ca_crt.is_file()):
            nets = [ipaddress.ip_network(n) for n in PRIVATE]
            constraints = ",".join([f"permitted;DNS:{d}" for d in zones] +
                                   [f"permitted;IP:{n.network_address}/{n.netmask}" for n in nets])
            _openssl("req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                     "-keyout", str(ca_key), "-out", str(ca_crt), "-days", str(CA_DAYS), "-sha256",
                     "-subj", "/O=Cortex hub/CN=Cortex hub local CA",
                     "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                     "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                     "-addext", f"nameConstraints=critical,{constraints}")
            os.chmod(ca_crt, 0o644)
            crt.unlink(missing_ok=True)
            done.append(f"new local CA for {', '.join(zones)} and private addresses: trust its ca.crt once on each machine")
        fresh = have == want and crt.is_file() and key.is_file() and subprocess.run(
            ["openssl", "x509", "-checkend", str(RENEW_DAYS * 86400), "-noout", "-in", str(crt)], capture_output=True).returncode == 0
        if not fresh:
            csr, ext = tls / "hub.csr", tls / "hub.ext"
            ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                           "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
                           f"subjectAltName={','.join([f'DNS:{d}' for d in dns] + [f'IP:{i}' for i in ips])}\n", encoding="utf-8")
            _openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                     "-keyout", str(key), "-out", str(csr), "-subj", f"/O=Cortex hub/CN={dns[0]}")
            leaf = _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
                            "-set_serial", "0x" + secrets.token_hex(16), "-days", str(LEAF_DAYS), "-sha256", "-extfile", str(ext))
            crt.write_text(leaf + ca_crt.read_text(encoding="utf-8"), encoding="utf-8")   # the chain: server, then CA
            csr.unlink(missing_ok=True)
            ext.unlink(missing_ok=True)
            done.append(f"server certificate for {LEAF_DAYS} days")
        (tls / "names.json").write_text(json.dumps(want), encoding="utf-8")
    finally:
        os.umask(old)
    return crt, key, done
