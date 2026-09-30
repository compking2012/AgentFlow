"""Owner-pinned enrollment, node client certificates and scoped attempt tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from agentflow.common import DomainError, canonical_json


def fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def public_key_fingerprint(key) -> str:
    return fingerprint(key.public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo))


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise DomainError("unsafe_key_path", "Key paths must not be symbolic links", 500)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


class NodeCertificateAuthority:
    def __init__(self, directory: Path, server_hostname: str = "localhost"):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise DomainError("unsafe_key_path", "PKI directory must be a real protected directory", 500)
        if os.name != "nt":
            directory.chmod(0o700)
        key_path, cert_path = directory / "ca.key", directory / "ca.pem"
        if key_path.exists() != cert_path.exists():
            raise DomainError("incomplete_pki", "CA key/certificate state requires operator recovery", 500)
        if not key_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AgentFlow local node CA")])
            now = datetime.now(UTC)
            cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                    .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                                key_encipherment=False, data_encipherment=False,
                                                key_agreement=False, key_cert_sign=True, crl_sign=True,
                                                encipher_only=None, decipher_only=None), critical=True)
                    .sign(key, hashes.SHA256()))
            _private_write(key_path, key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
            _private_write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
        self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        self.certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        secret_path = directory / "token.key"
        if not secret_path.exists():
            _private_write(secret_path, secrets.token_bytes(32))
        self.token_key = secret_path.read_bytes()
        if len(self.token_key) != 32:
            raise DomainError("invalid_pki", "Invalid local token key", 500)
        self.server_cert_path, self.server_key_path = directory / "server.pem", directory / "server.key"
        if self.server_cert_path.exists() != self.server_key_path.exists():
            raise DomainError("incomplete_pki", "Gateway certificate/key state requires recovery", 500)
        if not self.server_cert_path.exists():
            server_key = ec.generate_private_key(ec.SECP256R1())
            try:
                host = x509.IPAddress(ipaddress.ip_address(server_hostname))
            except ValueError:
                host = x509.DNSName(server_hostname)
            cert = self._issue(server_key.public_key(), "AgentFlow execution gateway",
                               [host], [ExtendedKeyUsageOID.SERVER_AUTH], days=365)
            _private_write(self.server_key_path, server_key.private_bytes(serialization.Encoding.PEM,
                                                                          serialization.PrivateFormat.PKCS8,
                                                                          serialization.NoEncryption()))
            _private_write(self.server_cert_path, cert.public_bytes(serialization.Encoding.PEM))
        self.server_certificate = x509.load_pem_x509_certificate(self.server_cert_path.read_bytes())

    @property
    def controller_fingerprint(self) -> str:
        return fingerprint(self.server_certificate.public_bytes(serialization.Encoding.DER))

    def configure_primary_server_hostname(self, server_hostname: str) -> bool:
        """Explicit offline gateway configuration; never changes the local listener's leaf or CA."""
        if self.server_cert_path != self.directory / "server.pem" or self.server_key_path != self.directory / "server.key":
            raise DomainError("primary_gateway_required", "Only the primary gateway certificate can be configured here")
        if self.server_cert_path.is_symlink() or self.server_key_path.is_symlink():
            raise DomainError("unsafe_key_path", "Gateway key/certificate paths must be regular private files", 500)
        now = datetime.now(UTC)
        if not self.certificate.not_valid_before_utc <= now <= self.certificate.not_valid_after_utc:
            raise DomainError("controller_ca_expired", "Controller CA requires explicit recovery before gateway configuration")
        try:
            address = ipaddress.ip_address(server_hostname)
            requested = x509.IPAddress(address)
        except ValueError:
            requested = x509.DNSName(server_hostname.lower())
        key = serialization.load_pem_private_key(self.server_key_path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise DomainError("invalid_pki", "The primary gateway key must retain its supported P-256 identity")
        matches = False
        try:
            certificate = self.server_certificate
            certificate.verify_directly_issued_by(self.certificate)
            names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            matches = (requested in names and certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc
                       and public_key_fingerprint(certificate.public_key()) == public_key_fingerprint(key.public_key()))
        except (ValueError, x509.ExtensionNotFound):
            pass
        if matches:
            return False
        certificate = self._issue(key.public_key(), "AgentFlow execution gateway", [requested],
                                  [ExtendedKeyUsageOID.SERVER_AUTH], days=365)
        temporary = self.directory / (".server-" + secrets.token_hex(12) + ".pem")
        try:
            _private_write(temporary, certificate.public_bytes(serialization.Encoding.PEM))
            os.replace(temporary, self.server_cert_path)
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        self.server_certificate = certificate
        return True

    @property
    def ca_pem(self) -> str:
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode()

    def _issue(self, public_key, common_name: str, names: list, usages: list, days: int = 30):
        now = datetime.now(UTC)
        return (x509.CertificateBuilder().subject_name(x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
                .issuer_name(self.certificate.subject).public_key(public_key)
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=2))
                .not_valid_after(now + timedelta(days=days))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage(usages), critical=True)
                .add_extension(x509.SubjectAlternativeName(names), critical=False)
                .sign(self.key, hashes.SHA256()))

    def parse_csr(self, pem: str):
        try:
            csr = x509.load_pem_x509_csr(pem.encode())
        except ValueError as exc:
            raise DomainError("invalid_csr", "CSR is malformed", 422) from exc
        if not csr.is_signature_valid:
            raise DomainError("invalid_csr", "CSR proof of possession is invalid", 403)
        key = csr.public_key()
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
            raise DomainError("unsupported_node_key", "Node key must use ECDSA P-256", 422)
        return csr

    def issue_node(self, node_id: str, csr) -> x509.Certificate:
        # Never copy user-supplied CSR subject, SAN or CA attributes.
        return self._issue(csr.public_key(), node_id, [x509.UniformResourceIdentifier(f"urn:agentflow:node:{node_id}")],
                           [ExtendedKeyUsageOID.CLIENT_AUTH])

    def verify_node_certificate(self, der: bytes) -> tuple[str, x509.Certificate]:
        try:
            cert = x509.load_der_x509_certificate(der)
            cert.verify_directly_issued_by(self.certificate)
            now = datetime.now(UTC)
            if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
                raise ValueError("expired certificate")
            if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise ValueError("CA certificate is not a node identity")
            if ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
                raise ValueError("client authentication usage missing")
            uris = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                x509.UniformResourceIdentifier)
            if len(uris) != 1 or not uris[0].startswith("urn:agentflow:node:"):
                raise ValueError("node identity URI missing")
            return uris[0].split(":")[-1], cert
        except Exception as exc:
            raise DomainError("invalid_node_certificate", "Certificate is invalid for this execution gateway", 401) from exc

    def pairing_code(self, pairing_id: str) -> str:
        return hmac.new(self.token_key, b"pairing:" + pairing_id.encode(), hashlib.sha256).hexdigest()

    def attempt_token(self, payload: dict) -> str:
        raw = canonical_json({**payload, "aud": "agentflow_attempt"}).encode()
        body = base64.urlsafe_b64encode(raw).rstrip(b"=")
        signature = hmac.new(self.token_key, body, hashlib.sha256).digest()
        return (body + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()

    def verify_attempt_token(self, token: str, expected: dict) -> dict:
        try:
            body, signature = token.encode().split(b".")
            expected_sig = hmac.new(self.token_key, body, hashlib.sha256).digest()
            if not hmac.compare_digest(base64.urlsafe_b64decode(signature + b"=" * (-len(signature) % 4)), expected_sig):
                raise ValueError("invalid signature")
            payload = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
            if payload.get("aud") != "agentflow_attempt" or any(payload.get(k) != v for k, v in expected.items()):
                raise ValueError("token scope mismatch")
            if datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC):
                raise ValueError("token expired")
            return payload
        except Exception as exc:
            raise DomainError("invalid_attempt_token", "Attempt token is expired or outside its scope", 401) from exc

    def server_context(self, *, require_client_certificate: bool = True) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.server_cert_path), str(self.server_key_path))
        context.load_verify_locations(cadata=self.ca_pem)
        context.verify_mode = ssl.CERT_REQUIRED if require_client_certificate else ssl.CERT_OPTIONAL
        return context


def create_node_key_and_csr(directory: Path, node_label: str) -> tuple[str, str]:
    """Generate the private key locally; return CSR and public-key fingerprint only."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "node.key"
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        _private_write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_label[:160])]))
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode(), public_key_fingerprint(key.public_key())


def sign_receipt(private_key_path: Path, payload: dict) -> str:
    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    return base64.b64encode(key.sign(canonical_json(payload).encode(), ec.ECDSA(hashes.SHA256()))).decode()


def verify_receipt(certificate_pem: str, payload: dict, signature: str) -> None:
    try:
        cert = x509.load_pem_x509_certificate(certificate_pem.encode())
        cert.public_key().verify(base64.b64decode(signature, validate=True), canonical_json(payload).encode(),
                                 ec.ECDSA(hashes.SHA256()))
    except Exception as exc:
        raise DomainError("invalid_cleanup_signature", "Cleanup receipt is not signed by the assigned node", 403) from exc
