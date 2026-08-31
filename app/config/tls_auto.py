"""Locally-signed TLS material for ``CREMIND_SSL=auto``.

Generates a small local CA once, then a server certificate signed by it, both
under ``<system dir>/tls/``. The CA exists so the trust decision is a *one-off*:
a bare self-signed server certificate has to be re-trusted on every machine
every time it is regenerated, whereas a certificate signed by a CA the device
already trusts is accepted for as long as that CA lives.

That indirection is the only way to make a generated certificate warning-free.
Browsers trust a certificate because it chains to a root in the *device's* trust
store, so something must be installed there by hand exactly once — see the HTTPS
section in CONTRIBUTING.md for the per-OS command. Nothing a server can do on
its own removes the warning; a certificate from a public CA (Let's Encrypt and
friends) is the alternative, and it needs a real domain name.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.utils import logger

# The CA outlives many leaf rotations — it is installed by hand, so asking the
# user to repeat that should be rare. The leaf stays under the 825-day maximum
# that Apple and Chrome enforce for server certificates.
_CA_DAYS = 3650
_LEAF_DAYS = 825
# Rotate a leaf before it actually expires, so a long-running server does not
# start serving an expired certificate between restarts.
_LEAF_RENEW_WITHIN_DAYS = 30


def tls_dir(system_dir: str) -> str:
    return os.path.join(system_dir, "tls")


def _paths(system_dir: str) -> dict[str, str]:
    d = tls_dir(system_dir)
    return {
        "dir": d,
        "ca_cert": os.path.join(d, "ca.pem"),
        "ca_key": os.path.join(d, "ca.key"),
        "cert": os.path.join(d, "cert.pem"),
        "key": os.path.join(d, "key.pem"),
    }


def ca_fingerprint_sha256(system_dir: str) -> str | None:
    """SHA-256 of the local CA certificate, or ``None`` if there isn't one.

    Colon-separated uppercase hex over the DER — the format certificate
    viewers show, so a user can compare what the Setup Wizard displays against
    what their browser and OS trust dialog show, and against
    ``cremind tls fingerprint``.
    """
    path = _paths(system_dir)["ca_cert"]
    try:
        with open(path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)
    ).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _write_private(path: str, data: bytes) -> None:
    """Write key material, readable by this user only where the OS allows it."""
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs don't map to POSIX modes
        pass


def _local_hostnames(extra: list[str] | None = None) -> tuple[list[str], list[str]]:
    """The names and IPs this server is plausibly reached by.

    ``extra`` (CREMIND_SSL_AUTO_HOSTS) covers the cases detection cannot know
    about — a LAN alias, a container name, a hostname resolved by other hosts.
    """
    names: list[str] = ["localhost"]
    ips: list[str] = ["127.0.0.1", "::1"]

    try:
        host = socket.gethostname()
        if host:
            names.append(host)
            # The FQDN is a different name to a certificate than the short one.
            fqdn = socket.getfqdn()
            if fqdn and fqdn != host:
                names.append(fqdn)
            for info in socket.getaddrinfo(host, None):
                addr = info[4][0]
                # Strip the scope id IPv6 link-local addresses carry ("%eth0"),
                # which is not part of the address itself.
                ips.append(addr.split("%")[0])
    except OSError as e:
        logger.debug(f"[tls] host detection best-effort failed: {e}")

    for item in extra or []:
        item = item.strip()
        if not item:
            continue
        try:
            ipaddress.ip_address(item)
        except ValueError:
            names.append(item)
        else:
            ips.append(item)

    # dict.fromkeys de-duplicates while keeping the order stable, so an
    # unchanged environment produces an identical SAN set and no rotation.
    return list(dict.fromkeys(names)), list(dict.fromkeys(ips))


def _san(names: list[str], ips: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    for ip in ips:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            logger.debug(f"[tls] skipping unparseable IP for SAN: {ip!r}")
    return x509.SubjectAlternativeName(entries)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _authority_key_id(
    ca_cert: x509.Certificate, ca_key: rsa.RSAPrivateKey
) -> x509.AuthorityKeyIdentifier:
    """Tie the leaf to its issuer the way verifiers expect.

    Derived from the CA's own Subject Key Identifier when it has one, so the
    two match exactly; a CA written before that extension was added still gets
    a usable value computed from its public key.
    """
    try:
        ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        return x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski)
    except x509.ExtensionNotFound:
        return x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())


def _load_ca(paths: dict[str, str]) -> tuple[x509.Certificate, rsa.RSAPrivateKey] | None:
    """The existing CA, or ``None`` if it is absent, unreadable, or expired."""
    if not (os.path.isfile(paths["ca_cert"]) and os.path.isfile(paths["ca_key"])):
        return None
    try:
        with open(paths["ca_cert"], "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        with open(paths["ca_key"], "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        if cert.not_valid_after_utc <= _utcnow():
            logger.warning("[tls] local CA has expired; generating a new one.")
            return None
        return cert, key  # type: ignore[return-value]
    except Exception as e:  # noqa: BLE001 - any unreadable CA is simply regenerated
        logger.warning(f"[tls] could not load the local CA ({e}); generating a new one.")
        return None


def _make_ca(paths: dict[str, str]) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Cremind Local CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cremind"),
        ]
    )
    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Backdated a little so a client whose clock trails ours still
        # accepts the certificate immediately after generation.
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    os.makedirs(paths["dir"], exist_ok=True)
    with open(paths["ca_cert"], "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(
        paths["ca_key"],
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    logger.info(f"[tls] generated a local CA at {paths['ca_cert']}")
    return cert, key


def _leaf_is_current(
    paths: dict[str, str],
    ca_cert: x509.Certificate,
    names: list[str],
    ips: list[str],
) -> bool:
    """Whether the stored leaf can be reused as-is."""
    if not (os.path.isfile(paths["cert"]) and os.path.isfile(paths["key"])):
        return False
    try:
        with open(paths["cert"], "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[tls] could not read the server certificate ({e}); regenerating.")
        return False

    if cert.issuer != ca_cert.subject:
        logger.info("[tls] server certificate was issued by a different CA; regenerating.")
        return False
    if cert.not_valid_after_utc <= _utcnow() + datetime.timedelta(days=_LEAF_RENEW_WITHIN_DAYS):
        logger.info("[tls] server certificate is expiring; regenerating.")
        return False

    # A leaf without this fails OpenSSL 3 verification, so a certificate
    # written before it was added must be reissued rather than kept forever.
    try:
        cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    except x509.ExtensionNotFound:
        logger.info("[tls] server certificate predates the issuer link; regenerating.")
        return False

    # Regenerate when this host answers to something the certificate omits —
    # e.g. the machine was renamed or CREMIND_SSL_AUTO_HOSTS grew an entry.
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    covered = set(san.get_values_for_type(x509.DNSName))
    covered |= {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    missing = ({*names} | {*ips}) - covered
    if missing:
        logger.info(f"[tls] server certificate is missing {sorted(missing)}; regenerating.")
        return False
    return True


def _make_leaf(
    paths: dict[str, str],
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    names: list[str],
    ips: list[str],
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_LEAF_DAYS))
        .add_extension(_san(names, ips), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        # Without this, OpenSSL 3's default verification rejects the chain
        # outright ("Missing Authority Key Identifier") — the certificate looks
        # fine but nothing will connect to it.
        .add_extension(_authority_key_id(ca_cert, ca_key), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    os.makedirs(paths["dir"], exist_ok=True)
    with open(paths["cert"], "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(
        paths["key"],
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    logger.info(f"[tls] issued a server certificate for {names + ips}")


def ensure_local_tls(system_dir: str, extra_hosts: list[str] | None = None) -> tuple[str, str]:
    """Return ``(certfile, keyfile)``, generating the CA and/or leaf if needed.

    Idempotent: with valid material already on disk this only reads it.
    """
    paths = _paths(system_dir)
    os.makedirs(paths["dir"], exist_ok=True)

    names, ips = _local_hostnames(extra_hosts)

    loaded = _load_ca(paths)
    if loaded is None:
        ca_cert, ca_key = _make_ca(paths)
        # A new CA invalidates any leaf signed by the old one.
        for stale in (paths["cert"], paths["key"]):
            if os.path.isfile(stale):
                os.remove(stale)
    else:
        ca_cert, ca_key = loaded

    if not _leaf_is_current(paths, ca_cert, names, ips):
        _make_leaf(paths, ca_cert, ca_key, names, ips)

    return paths["cert"], paths["key"]
