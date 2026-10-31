"""Authenticated RPC configuration envelope; secrets exist only in memory."""
import os
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"PMRPC1\x00"
CONTEXT = b"pumpmi-cloud-worker-rpc/v1"
MAX_SIZE = 20000


def _key(database_url: str, salt: bytes) -> bytes:
    if not database_url:
        raise ValueError("Managed database runtime configuration is unavailable")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=CONTEXT).derive(database_url.encode())


def _validate(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or len(url) > 16384:
        raise ValueError("A valid protected HTTPS RPC endpoint is required")
    return url


def seal(url: str, database_url: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    encrypted = AESGCM(_key(database_url, salt)).encrypt(nonce, _validate(url).encode(), CONTEXT)
    return MAGIC + salt + nonce + encrypted


def unseal(blob: bytes, database_url: str) -> str:
    start = len(MAGIC)
    if not blob.startswith(MAGIC) or not start + 44 <= len(blob) <= MAX_SIZE:
        raise ValueError("Protected RPC credential format is invalid")
    salt, nonce = blob[start:start + 16], blob[start + 16:start + 28]
    plaintext = AESGCM(_key(database_url, salt)).decrypt(nonce, blob[start + 28:], CONTEXT)
    return _validate(plaintext.decode())
