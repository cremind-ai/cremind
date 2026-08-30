"""Generated TLS material for ``CREMIND_SSL=auto``.

The CA is the part that matters: it is installed into a device's trust store by
hand, so it has to survive leaf rotation. A bare self-signed certificate would
force that manual step again every time the certificate changed.
"""

from __future__ import annotations

import datetime

from cryptography import x509

from app.config.tls_auto import ensure_local_tls


def _load(path: str) -> x509.Certificate:
    with open(path, "rb") as fh:
        return x509.load_pem_x509_certificate(fh.read())


def test_it_generates_a_ca_and_a_server_certificate(tmp_path) -> None:
    cert_path, key_path = ensure_local_tls(str(tmp_path))
    tls = tmp_path / "tls"
    assert (tls / "ca.pem").is_file()
    assert (tls / "ca.key").is_file()
    assert cert_path == str(tls / "cert.pem")
    assert key_path == str(tls / "key.pem")


def test_the_server_certificate_is_signed_by_the_ca(tmp_path) -> None:
    """This is what makes trusting the CA once enough."""
    cert_path, _ = ensure_local_tls(str(tmp_path))
    leaf = _load(cert_path)
    ca = _load(str(tmp_path / "tls" / "ca.pem"))
    assert leaf.issuer == ca.subject
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False


def test_the_certificate_covers_localhost_and_loopback(tmp_path) -> None:
    """Browsers match the SAN, not the common name."""
    cert_path, _ = ensure_local_tls(str(tmp_path))
    san = _load(cert_path).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert "127.0.0.1" in {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}


def test_extra_hosts_are_included(tmp_path) -> None:
    """CREMIND_SSL_AUTO_HOSTS covers what detection cannot know — a LAN alias,
    a container name, whatever other hosts resolve this server as."""
    cert_path, _ = ensure_local_tls(str(tmp_path), ["cremind.lan", "10.0.0.5"])
    san = _load(cert_path).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert "cremind.lan" in san.get_values_for_type(x509.DNSName)
    assert "10.0.0.5" in {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}


def test_a_second_call_reuses_what_is_on_disk(tmp_path) -> None:
    """Boot must not mint a new certificate every restart — that would
    invalidate nothing, but it churns serials and defeats caching."""
    first_cert, _ = ensure_local_tls(str(tmp_path))
    first = _load(first_cert).serial_number
    ca_first = _load(str(tmp_path / "tls" / "ca.pem")).serial_number

    second_cert, _ = ensure_local_tls(str(tmp_path))
    assert _load(second_cert).serial_number == first
    assert _load(str(tmp_path / "tls" / "ca.pem")).serial_number == ca_first


def test_a_newly_requested_host_reissues_the_certificate(tmp_path) -> None:
    """Adding a name to CREMIND_SSL_AUTO_HOSTS has to take effect on restart,
    or the server keeps serving a certificate that does not match the URL."""
    ensure_local_tls(str(tmp_path))
    ca_before = _load(str(tmp_path / "tls" / "ca.pem")).serial_number

    cert_path, _ = ensure_local_tls(str(tmp_path), ["added-later.example"])
    san = _load(cert_path).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert "added-later.example" in san.get_values_for_type(x509.DNSName)
    # Reissued under the SAME CA, so the device's trust decision still holds.
    assert _load(str(tmp_path / "tls" / "ca.pem")).serial_number == ca_before


def test_a_lost_leaf_is_reissued_without_touching_the_ca(tmp_path) -> None:
    ensure_local_tls(str(tmp_path))
    ca_before = _load(str(tmp_path / "tls" / "ca.pem")).serial_number
    (tmp_path / "tls" / "cert.pem").unlink()

    ensure_local_tls(str(tmp_path))
    assert (tmp_path / "tls" / "cert.pem").is_file()
    assert _load(str(tmp_path / "tls" / "ca.pem")).serial_number == ca_before


def test_a_corrupt_ca_is_replaced_along_with_its_leaf(tmp_path) -> None:
    """A half-written or hand-edited CA must not wedge boot. The leaf goes with
    it — it can no longer chain to anything."""
    ensure_local_tls(str(tmp_path))
    (tmp_path / "tls" / "ca.pem").write_text("not a certificate")

    cert_path, _ = ensure_local_tls(str(tmp_path))
    ca = _load(str(tmp_path / "tls" / "ca.pem"))
    assert _load(cert_path).issuer == ca.subject


def test_the_leaf_carries_the_issuer_link_verifiers_require(tmp_path) -> None:
    """Regression: the first cut omitted the Authority Key Identifier, and
    OpenSSL 3 rejected the chain outright ("Missing Authority Key Identifier").
    The certificate looked correct in every other way, so nothing but an actual
    connection attempt revealed it."""
    cert_path, _ = ensure_local_tls(str(tmp_path))
    leaf = _load(cert_path)
    ca = _load(str(tmp_path / "tls" / "ca.pem"))

    aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert aki.key_identifier == ski.digest


def test_the_chain_actually_verifies_against_the_ca(tmp_path) -> None:
    """The end the user cares about: a client that trusts the CA accepts the
    server certificate for the hostname it is served on."""
    from cryptography.x509.verification import PolicyBuilder, Store

    cert_path, _ = ensure_local_tls(str(tmp_path))
    leaf = _load(cert_path)
    store = Store([_load(str(tmp_path / "tls" / "ca.pem"))])
    # Verification is time-sensitive; anchor it inside the certificate's window.
    verifier = (
        PolicyBuilder()
        .store(store)
        .time(datetime.datetime.now(datetime.timezone.utc))
        .build_server_verifier(x509.DNSName("localhost"))
    )
    chain = verifier.verify(leaf, [])
    assert chain, "the leaf should chain to the generated CA"


def test_the_certificate_is_valid_now_and_not_absurdly_long(tmp_path) -> None:
    """Backdated slightly so a client with a trailing clock still accepts it,
    and inside the 825-day cap browsers enforce on server certificates."""
    cert_path, _ = ensure_local_tls(str(tmp_path))
    leaf = _load(cert_path)
    now = datetime.datetime.now(datetime.timezone.utc)
    assert leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc
    assert (leaf.not_valid_after_utc - now).days <= 825
