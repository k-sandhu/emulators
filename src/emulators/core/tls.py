"""Local certificate authority for the HTTPS gateway.

Consumers that hard-code ``https://graph.microsoft.com`` can reach the emulators by
resolving vendor hostnames to the emulator (Docker network aliases or a hosts file) and
trusting this CA (``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` / ``NODE_EXTRA_CA_CERTS``).
``ensure_certs`` creates, once, a CA plus a server certificate whose SANs cover every
provider hostname (wildcards included) and ``localhost``.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Concrete names behind wildcard patterns (hosts files cannot hold wildcards).
WILDCARD_EXAMPLES: dict[str, list[str]] = {
    "*.sharepoint.com": ["halcyonadvisory.sharepoint.com", "halcyonadvisory-my.sharepoint.com"],
    "*.zoom.us": ["us06web.zoom.us", "ssrweb.zoom.us"],
    "*.my.salesforce.com": ["halcyonadvisory.my.salesforce.com"],
    "*.lightning.force.com": ["halcyonadvisory.lightning.force.com"],
    "*.my.salesforce-setup.com": ["halcyonadvisory.my.salesforce-setup.com"],
    "*.slack.com": ["halcyonadvisory.slack.com", "files.slack.com"],
    "*.boxcloud.com": ["dl.boxcloud.com", "public.boxcloud.com"],
    "*.dl.dropboxusercontent.com": ["uc.dl.dropboxusercontent.com"],
    "*.twilio.com": ["api.twilio.com"],
    "*.fbcdn.net": ["scontent.xx.fbcdn.net"],
}


def concrete_hosts(patterns: list[str]) -> list[str]:
    hosts: list[str] = []
    for pattern in patterns:
        if "*" in pattern:
            hosts.extend(WILDCARD_EXAMPLES.get(pattern, []))
        else:
            hosts.append(pattern)
    return sorted(dict.fromkeys(hosts))


@dataclass(frozen=True)
class CertPaths:
    directory: Path
    ca_cert: Path
    ca_key: Path
    server_cert: Path
    server_key: Path
    ca_bundle: Path


def _paths(directory: Path) -> CertPaths:
    return CertPaths(
        directory,
        directory / "ca.pem",
        directory / "ca-key.pem",
        directory / "server.pem",
        directory / "server-key.pem",
        directory / "ca-bundle.pem",
    )


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )


def ensure_certs(directory: Path, names: list[str], *, extra_ips: list[str] | None = None) -> CertPaths:
    """Create (or reuse) the CA and a server certificate covering ``names``."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = _paths(directory)
    now = dt.datetime.now(dt.UTC)
    wanted = sorted({*names, "localhost", "emulators", "host.docker.internal"})

    if paths.ca_cert.exists() and paths.ca_key.exists():
        ca_key = serialization.load_pem_private_key(paths.ca_key.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(paths.ca_cert.read_bytes())
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "emulators (local test CA)"),
                x509.NameAttribute(NameOID.COMMON_NAME, "emulators local CA"),
            ]
        )
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_key(paths.ca_key, ca_key)  # type: ignore[arg-type]
        paths.ca_cert.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    if paths.server_cert.exists() and paths.server_key.exists():
        existing = x509.load_pem_x509_certificate(paths.server_cert.read_bytes())
        try:
            sans = set(
                existing.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value.get_values_for_type(x509.DNSName)
            )
        except x509.ExtensionNotFound:
            sans = set()
        fresh = existing.not_valid_after_utc > now + dt.timedelta(days=30)
        if set(wanted) <= sans and fresh:
            _write_bundle(paths)
            return paths

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ips = [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]
    ips += [ipaddress.ip_address(ip) for ip in extra_ips or []]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "emulators gateway")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [*(x509.DNSName(n) for n in wanted), *(x509.IPAddress(ip) for ip in ips)]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),  # type: ignore[arg-type]
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    )
    _write_key(paths.server_key, key)
    paths.server_cert.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    _write_bundle(paths)
    return paths


def _write_bundle(paths: CertPaths) -> None:
    """System roots (certifi when available) + our CA, for SSL_CERT_FILE/REQUESTS_CA_BUNDLE."""
    parts = []
    try:
        import certifi

        parts.append(Path(certifi.where()).read_text(encoding="utf-8"))
    except ImportError:
        pass
    parts.append(paths.ca_cert.read_text(encoding="utf-8"))
    paths.ca_bundle.write_text("\n".join(parts), encoding="utf-8")
