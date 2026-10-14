"""Controller certificate and key."""
from __future__ import annotations

import base64
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

CERT_FILE = "tpac-ac.crt"
KEY_FILE = "tpac-ac.key"
SUBJECT = (("O", "Default Company Ltd"), ("L", "Default City"), ("C", "XX"))
VALID_FOR = datetime.timedelta(days=3650)


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _length(len(body)) + body


def _seq(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _oid(dotted: str) -> bytes:
    numbers = [int(x) for x in dotted.split(".")]
    body = bytes([40 * numbers[0] + numbers[1]])
    for number in numbers[2:]:
        chunk = [number & 0x7F]
        number >>= 7
        while number:
            chunk.insert(0, 0x80 | (number & 0x7F))
            number >>= 7
        body += bytes(chunk)
    return _tlv(0x06, body)


def _name() -> bytes:
    parts = []
    for attribute, value in SUBJECT:
        tag = 0x13 if attribute == "C" else 0x0C
        dotted = {"O": "2.5.4.10", "L": "2.5.4.7", "C": "2.5.4.6"}[attribute]
        parts.append(_tlv(0x31, _seq(_oid(dotted), _tlv(tag, value.encode()))))
    return _seq(*parts)


def _utc_time(moment: datetime.datetime) -> bytes:
    return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode())


def _extension(dotted: str, critical: bool, value: bytes) -> bytes:
    parts = [_oid(dotted)]
    if critical:
        parts.append(_tlv(0x01, b"\xff"))
    parts.append(_tlv(0x04, value))
    return _seq(*parts)


def _generate() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.asymmetric import padding

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key_id = hashes.Hash(hashes.SHA1())
    key_id.update(key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1))
    key_identifier = key_id.finalize()
    now = datetime.datetime.now(datetime.timezone.utc)
    algorithm = _seq(_oid("1.2.840.113549.1.1.5"), b"\x05\x00")
    extensions = _tlv(
        0xA3,
        _seq(
            _extension("2.5.29.19", True, _seq(_tlv(0x01, b"\xff"))),
            _extension("2.5.29.14", False, _tlv(0x04, key_identifier)),
            _extension("2.5.29.35", False, _seq(_tlv(0x80, key_identifier))),
        ),
    )
    serial = x509.random_serial_number()
    tbs = _seq(
        _tlv(0xA0, _tlv(0x02, b"\x02")),
        _tlv(0x02, serial.to_bytes((serial.bit_length() + 8) // 8, "big")),
        algorithm,
        _name(),
        _seq(_utc_time(now), _utc_time(now + VALID_FOR)),
        _name(),
        public_der,
        extensions,
    )
    signature = key.sign(tbs, padding.PKCS1v15(), hashes.SHA1())
    certificate = _seq(tbs, algorithm, _tlv(0x03, b"\x00" + signature))
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    cert_pem = (
        b"-----BEGIN CERTIFICATE-----\n"
        + base64.encodebytes(certificate)
        + b"-----END CERTIFICATE-----\n"
    )
    return cert_pem, key_pem


def ensure_certificate(config_dir: Path) -> tuple[Path, Path]:
    directory = config_dir / "tpac"
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    if not (cert_path.exists() and key_path.exists()):
        directory.mkdir(parents=True, exist_ok=True)
        cert_pem, key_pem = _generate()
        key_path.write_bytes(key_pem)
        key_path.chmod(0o600)
        cert_path.write_bytes(cert_pem)
    return cert_path, key_path
