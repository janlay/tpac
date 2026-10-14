"""DTLS server."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DTLS_VERSION = b"\xfe\xff"
CONTENT_CHANGE_CIPHER_SPEC = 20
CONTENT_ALERT = 21
CONTENT_HANDSHAKE = 22
CONTENT_APPLICATION_DATA = 23

HANDSHAKE_CLIENT_HELLO = 1
HANDSHAKE_SERVER_HELLO = 2
HANDSHAKE_HELLO_VERIFY_REQUEST = 3
HANDSHAKE_CERTIFICATE = 11
HANDSHAKE_SERVER_KEY_EXCHANGE = 12
HANDSHAKE_SERVER_HELLO_DONE = 14
HANDSHAKE_CLIENT_KEY_EXCHANGE = 16
HANDSHAKE_FINISHED = 20

COOKIE_SIZE = 16
COOKIE_SEED = os.urandom(16)

LOG = logging.getLogger("tpac.dtls")

CIPHER_SUITE = 0x0035
MAC_SIZE = 20
KEY_SIZE = 32
BLOCK_SIZE = 16
FIXED_IV_SIZE = BLOCK_SIZE


_MD5_SHIFTS = [7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4
_MD5_TABLE = [
    int(abs(__import__("math").sin(i + 1)) * (1 << 32)) & 0xFFFFFFFF for i in range(64)
]


def _md5_compress(state, block: bytes) -> None:
    a, b, c, d = state
    words = struct.unpack("<16I", block)
    for i in range(64):
        if i < 16:
            f, g = (b & c) | (~b & d), i
        elif i < 32:
            f, g = (d & b) | (~d & c), (5 * i + 1) % 16
        elif i < 48:
            f, g = b ^ c ^ d, (3 * i + 5) % 16
        else:
            f, g = c ^ (b | (~d & 0xFFFFFFFF)), (7 * i) % 16
        f = (f + a + _MD5_TABLE[i] + words[g]) & 0xFFFFFFFF
        a, d, c, b = d, c, b, (b + ((f << _MD5_SHIFTS[i]) | (f >> (32 - _MD5_SHIFTS[i])))) & 0xFFFFFFFF
    state[0] = (state[0] + a) & 0xFFFFFFFF
    state[1] = (state[1] + b) & 0xFFFFFFFF
    state[2] = (state[2] + c) & 0xFFFFFFFF
    state[3] = (state[3] + d) & 0xFFFFFFFF


def md5(data: bytes) -> bytes:
    state = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
    message = bytearray(data)
    bit_length = (8 * len(message)) & 0xFFFFFFFFFFFFFFFF
    message.append(0x80)
    while len(message) % 64 != 56:
        message.append(0)
    message += struct.pack("<Q", bit_length)
    for offset in range(0, len(message), 64):
        _md5_compress(state, bytes(message[offset : offset + 64]))
    return struct.pack("<4I", *state)


def _p_hash(secret: bytes, seed: bytes, length: int) -> bytes:
    output = b""
    a = seed
    while len(output) < length:
        a = hmac.new(secret, a, hashlib.sha1).digest()
        output += hmac.new(secret, a + seed, hashlib.sha1).digest()
    return output[:length]


def _hmac_md5(key: bytes, data: bytes) -> bytes:
    block = 64
    if len(key) > block:
        key = md5(key)
    key = key.ljust(block, b"\0")
    inner = md5(bytes(x ^ 0x36 for x in key) + data)
    return md5(bytes(x ^ 0x5C for x in key) + inner)


def _prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    half = (len(secret) + 1) // 2
    md5_secret, sha_secret = secret[:half], secret[half:]
    md5_part = b""
    a = label + seed
    while len(md5_part) < length:
        a = _hmac_md5(md5_secret, a)
        md5_part += _hmac_md5(md5_secret, a + label + seed)
    sha_part = _p_hash(sha_secret, label + seed, length)
    return bytes(x ^ y for x, y in zip(md5_part[:length], sha_part[:length]))


def cookie_for(peer: tuple[str, int]) -> bytes:
    return hashlib.sha256(f"{peer[0]}:{peer[1]}".encode() + COOKIE_SEED).digest()[:COOKIE_SIZE]


def handshake_message(kind: int, body: bytes, sequence: int) -> bytes:
    return (
        bytes((kind,))
        + len(body).to_bytes(3, "big")
        + sequence.to_bytes(2, "big")
        + b"\x00\x00\x00"
        + len(body).to_bytes(3, "big")
        + body
    )


def hello_verify_request(cookie: bytes) -> bytes:
    body = DTLS_VERSION + bytes((len(cookie),)) + cookie
    return handshake_message(HANDSHAKE_HELLO_VERIFY_REQUEST, body, 0)


def client_hello_body(record: bytes) -> bytes | None:
    if len(record) < 26 or record[0] != CONTENT_HANDSHAKE:
        return None
    body = record[13:]
    if len(body) < 12 or body[0] != HANDSHAKE_CLIENT_HELLO:
        return None
    return body[12:]


def client_hello_cookie(record: bytes) -> bytes:
    body = client_hello_body(record)
    if body is None or len(body) < 36:
        return b""
    offset = 36 + body[34]
    length = body[offset - 1]
    return body[offset : offset + length]


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _hmac_sha1(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha1).digest()


def _record_mac(mac_key: bytes, sequence: int, content_type: int, length: int, plaintext: bytes) -> bytes:
    header = (
        sequence.to_bytes(8, "big")
        + bytes((content_type,))
        + b"\xfe\xff"
        + len(plaintext).to_bytes(2, "big")
    )
    return _hmac_sha1(mac_key, header + plaintext)


class TlsError(Exception):
    pass


class DtlsServer:

    def __init__(self, certificate_pem: bytes, key_pem: bytes, peer: tuple[str, int]) -> None:
        self.peer = peer
        self.certificate_pem = certificate_pem
        self.key = serialization.load_pem_private_key(key_pem, password=None)
        self.certificate_der = (
            serialization.load_pem_public_key(certificate_pem) if False else self._certificate_der(certificate_pem)
        )
        self.state = "start"
        self.client_random = b""
        self.server_random = os.urandom(32)
        self.session_id = os.urandom(32)
        self.master_secret = b""
        self.client_keys = (b"", b"", b"")
        self.server_keys = (b"", b"", b"")
        self.client_sequence = 0
        self.server_sequence = 0
        self.server_epoch = 0
        self.handshake_log = b""
        self.client_finished_verified = False
        self.connected = False
        self.cipher_active = False
        self.last_alert: tuple[int, int] | None = None
        self.alert_bytes = ""
        self.pending: list[bytes] = []

    @staticmethod
    def _certificate_der(certificate_pem: bytes) -> bytes:
        from cryptography import x509

        return x509.load_pem_x509_certificate(certificate_pem).public_bytes(
            __import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER
        )


    def _record(self, content_type: int, payload: bytes, encrypt: bool = False) -> bytes:
        if encrypt:
            mac = _record_mac(
                self.server_keys[0],
                (self.server_epoch << 48) | self.server_sequence,
                content_type,
                len(payload),
                payload,
            )
            padded = payload + mac
            pad = BLOCK_SIZE - (len(padded) % BLOCK_SIZE) - 1
            padded += bytes((pad,)) * (pad + 1)
            iv = os.urandom(BLOCK_SIZE)
            body = iv + _aes_cbc_encrypt(self.server_keys[1], iv, padded)
        else:
            body = payload
        record = (
            bytes((content_type,))
            + b"\xfe\xff"
            + self.server_epoch.to_bytes(2, "big")
            + self.server_sequence.to_bytes(6, "big")
            + len(body).to_bytes(2, "big")
            + body
        )
        self.server_sequence += 1
        return record

    def _handshake(self, kind: int, body: bytes, encrypt: bool = False) -> bytes:
        message = handshake_message(kind, body, self._next_handshake_sequence())
        if not encrypt:
            self.handshake_log += message
        return self._record(CONTENT_HANDSHAKE, message, encrypt=encrypt)

    def _next_handshake_sequence(self) -> int:
        if not hasattr(self, "_handshake_sequence"):
            self._handshake_sequence = 0
        value = self._handshake_sequence
        self._handshake_sequence += 1
        return value


    def handle(self, record: bytes) -> list[bytes]:
        if record[0] == CONTENT_ALERT:
            level = record[13] if len(record) > 13 else None
            description = record[14] if len(record) > 14 else None
            self.alert_bytes = record[13:15].hex()
            self.state = "closed"
            self.last_alert = (level, description)
            return []
        if self.state == "start":
            return self._handle_client_hello(record, expect_cookie=False)
        if self.state == "cookie-sent":
            return self._handle_client_hello(record, expect_cookie=True)
        if record[0] == CONTENT_CHANGE_CIPHER_SPEC:
            self.cipher_active = True
            return []
        if self.cipher_active:
            return self._handle_encrypted(record)
        if record[0] == CONTENT_HANDSHAKE:
            return self._handle_handshake(record)
        return []

    def _handle_client_hello(self, record: bytes, expect_cookie: bool) -> list[bytes]:
        body = client_hello_body(record)
        if body is None:
            return []
        if len(body) < 35:
            raise TlsError("short ClientHello")
        self.client_random = body[2:34]
        cookie = client_hello_cookie(record)
        if not cookie:
            self.state = "cookie-sent"
            return [self._record(CONTENT_HANDSHAKE, hello_verify_request(cookie_for(self.peer)))]
        if cookie != cookie_for(self.peer):
            self.state = "start"
            return [self._record(CONTENT_HANDSHAKE, hello_verify_request(cookie_for(self.peer)))]
        self.state = "hello-sent"
        self.handshake_log = record[13:]
        self._handshake_sequence = 1
        return self._build_server_flight()

    def _build_server_flight(self) -> list[bytes]:
        server_hello = (
            DTLS_VERSION
            + self.server_random
            + bytes((len(self.session_id),))
            + self.session_id
            + struct.pack(">H", CIPHER_SUITE)
            + b"\x00"
        )
        certificate = self.certificate_der
        certificate_body = (
            (len(certificate) + 3).to_bytes(3, "big")
            + len(certificate).to_bytes(3, "big")
            + certificate
        )
        return [
            self._handshake(HANDSHAKE_SERVER_HELLO, server_hello),
            self._handshake(HANDSHAKE_CERTIFICATE, certificate_body),
            self._handshake(HANDSHAKE_SERVER_HELLO_DONE, b""),
        ]

    def _handle_encrypted(self, record: bytes) -> list[bytes]:
        content_type, content = self._decrypt(record)
        if content_type == CONTENT_HANDSHAKE:
            return self._handle_handshake(content, offset=0)
        if content_type == CONTENT_APPLICATION_DATA:
            self.pending.append(content)
        return []

    def _handle_handshake(self, record: bytes, offset: int = 13) -> list[bytes]:
        message = record[offset:]
        kind = message[0]
        length = int.from_bytes(message[1:4], "big")
        body = message[12 : 12 + length]
        if kind == HANDSHAKE_CLIENT_KEY_EXCHANGE:
            self._client_key_exchange(body)
            self.handshake_log += message[:12] + body
            return []
        if kind == HANDSHAKE_FINISHED:
            self._verify_finished(body)
            self.handshake_log += message[:12] + body
            self.connected = True
            return []
        raise TlsError(f"unexpected handshake type {kind}")

    def _client_key_exchange(self, body: bytes) -> None:
        if len(body) < 2:
            raise TlsError("short ClientKeyExchange")
        size = int.from_bytes(body[:2], "big")
        encrypted = body[2 : 2 + size]
        premaster = self.key.decrypt(encrypted, padding.PKCS1v15())
        LOG.info(
            "CKE: premaster=%d bytes head=%s",
            len(premaster),
            premaster[:4].hex(),
        )

        self.master_secret = _prf(
            premaster, b"master secret", self.client_random + self.server_random, 48
        )
        key_block = _prf(
            self.master_secret,
            b"key expansion",
            self.server_random + self.client_random,
            MAC_SIZE * 2 + KEY_SIZE * 2 + FIXED_IV_SIZE * 2,
        )
        client_mac = key_block[0:MAC_SIZE]
        server_mac = key_block[MAC_SIZE : MAC_SIZE * 2]
        key_at = MAC_SIZE * 2
        client_key = key_block[key_at : key_at + KEY_SIZE]
        server_key = key_block[key_at + KEY_SIZE : key_at + KEY_SIZE * 2]
        iv_at = key_at + KEY_SIZE * 2
        client_iv = key_block[iv_at : iv_at + FIXED_IV_SIZE]
        server_iv = key_block[iv_at + FIXED_IV_SIZE : iv_at + FIXED_IV_SIZE * 2]
        self.client_keys = (client_mac, client_key, client_iv)
        self.server_keys = (server_mac, server_key, server_iv)

    def _decrypt(self, record: bytes) -> tuple[int, bytes]:
        iv = record[13:13 + BLOCK_SIZE]
        body = record[13 + BLOCK_SIZE :]
        plaintext = _aes_cbc_decrypt(self.client_keys[1], iv, body)
        pad = plaintext[-1]
        plaintext = plaintext[: len(plaintext) - pad - 1]
        mac = plaintext[-MAC_SIZE:]
        content = plaintext[:-MAC_SIZE]
        epoch = int.from_bytes(record[3:5], "big")
        sequence = int.from_bytes(record[5:11], "big")
        expected = _record_mac(
            self.client_keys[0], (epoch << 48) | sequence, record[0], len(content), content
        )
        if mac != expected:
            raise TlsError(
                f"record MAC mismatch seq={sequence} type={record[0]} "
                f"head={content[:8].hex()} got={mac[:6].hex()} want={expected[:6].hex()}"
            )
        return record[0], content

    def _verify_finished(self, body: bytes) -> None:
        expected = _prf(
            self.master_secret,
            b"client finished",
            md5(self.handshake_log) + hashlib.sha1(self.handshake_log).digest(),
            12,
        )
        if body != expected:
            raise TlsError("client Finished did not verify")
        self.client_finished_verified = True

    def finish(self) -> list[bytes]:
        records = [self._record(CONTENT_CHANGE_CIPHER_SPEC, b"\x01")]
        self.server_epoch = 1
        self.server_sequence = 0
        verify = _prf(
            self.master_secret,
            b"server finished",
            md5(self.handshake_log) + hashlib.sha1(self.handshake_log).digest(),
            12,
        )
        records.append(self._record(CONTENT_HANDSHAKE, handshake_message(HANDSHAKE_FINISHED, verify, self._next_handshake_sequence()), encrypt=True))
        self.handshake_log += handshake_message(HANDSHAKE_FINISHED, verify, self._handshake_sequence - 1)
        self.state = "connected"
        return records

    def application_record(self, payload: bytes) -> bytes:
        return self._record(CONTENT_APPLICATION_DATA, payload, encrypt=True)

    def close_notify(self) -> bytes:
        return self._record(CONTENT_ALERT, b"\x01\x00", encrypt=True)

    def take_plaintext(self) -> list[bytes]:
        pending, self.pending = self.pending, []
        return pending
