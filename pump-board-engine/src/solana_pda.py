"""Pure-Python Solana address helpers: base58 and associated token account (ATA) derivation.

Used to locate a coin creator's own token account so a paper worker can read
the creator's balance (a developer dump) from public account data. No signing,
no keys, no network. Mirrors Solana's find_program_address: seeds + bump +
program id + "ProgramDerivedAddress", SHA-256, first bump whose hash is not a
valid ed25519 point.
"""
from __future__ import annotations

import hashlib

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}

ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# ed25519 field constants
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def b58decode(value: str) -> bytes:
    num = 0
    for char in value:
        if char not in _INDEX:
            raise ValueError("invalid base58 character")
        num = num * 58 + _INDEX[char]
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _ALPHABET[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


def pubkey_bytes(value: str) -> bytes:
    raw = b58decode(value)
    if len(raw) != 32:
        raise ValueError("public key must decode to 32 bytes")
    return raw


def is_on_curve(raw: bytes) -> bool:
    """True when 32 bytes decompress to a valid ed25519 point."""
    if len(raw) != 32:
        return False
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    sign = raw[31] >> 7
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    # x = sqrt(u / v) using the RFC 8032 method
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P)) % _P
    vx2 = v * x * x % _P
    if vx2 == u % _P:
        pass
    elif vx2 == (-u) % _P:
        x = x * _SQRT_M1 % _P
    else:
        return False
    if x == 0 and sign == 1:
        return False
    return True


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    program = pubkey_bytes(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("no viable program address")


def associated_token_address(owner: str, mint: str, token_program: str = TOKEN_PROGRAM_ID) -> str:
    """The canonical associated token account of ``owner`` for ``mint``."""
    if token_program not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        raise ValueError("unsupported token program")
    address, _ = find_program_address([pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)], ATA_PROGRAM_ID)
    return address


def token_account_amount(data: bytes) -> int | None:
    """Raw amount of an SPL / Token-2022 account (bytes 64..72). None when unreadable."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 72:
        return None
    return int.from_bytes(bytes(data[64:72]), "little")
