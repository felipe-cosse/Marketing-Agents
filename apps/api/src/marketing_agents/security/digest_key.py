"""Restart-stable local digest-key creation and database/key pairing checks."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


KEY_BYTES = 32
FINGERPRINT_DOMAIN = b"marketing-agents-digest-key-fingerprint-v1\x00"


class DigestKeyError(RuntimeError):
    """Raised when local key material is absent, unsafe, corrupt, or mismatched."""


@dataclass(frozen=True)
class DigestKey:
    _value: bytes

    def __post_init__(self) -> None:
        if len(self._value) != KEY_BYTES:
            raise ValueError("digest key must contain exactly 32 bytes")

    def bytes_for_digest(self) -> bytes:
        return self._value

    def __repr__(self) -> str:
        return "DigestKey([REDACTED])"


def digest_key_fingerprint(key: DigestKey) -> str:
    digest = hashlib.sha256(FINGERPRINT_DOMAIN + key.bytes_for_digest()).hexdigest()
    return f"digest-key-fingerprint-v1:{digest}"


def _decode_key(payload: bytes) -> DigestKey:
    try:
        value = base64.urlsafe_b64decode(payload.strip())
    except (ValueError, binascii.Error) as exc:
        raise DigestKeyError("digest key file is not valid base64") from exc
    if len(value) != KEY_BYTES:
        raise DigestKeyError("digest key file has an invalid length")
    return DigestKey(value)


def _read_existing(path: Path) -> DigestKey:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise DigestKeyError("digest key cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DigestKeyError("digest key path is not a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise DigestKeyError("digest key permissions must not grant group or other access")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(256)
            if stream.read(1):
                raise DigestKeyError("digest key file is unexpectedly large")
    finally:
        os.close(descriptor)
    return _decode_key(payload)


def _create_new(path: Path) -> DigestKey:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)) + b"\n"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_existing(path)
    except OSError as exc:
        raise DigestKeyError("digest key cannot be created safely") from exc
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass
    return _decode_key(encoded)


def load_or_create_digest_key(
    path: Path,
    *,
    persistent_state_exists: bool = False,
    expected_fingerprint: str | None = None,
) -> DigestKey:
    """Load a safe key or create one only when no durable state already exists."""
    path = path.expanduser()
    if path.is_symlink():
        raise DigestKeyError("digest key path cannot be a symlink")
    if path.exists():
        key = _read_existing(path)
    elif persistent_state_exists:
        raise DigestKeyError("digest key is missing for existing persistent state")
    else:
        key = _create_new(path)
    actual_fingerprint = digest_key_fingerprint(key)
    if expected_fingerprint is not None and expected_fingerprint != actual_fingerprint:
        raise DigestKeyError("digest key does not match the persistent-state fingerprint")
    return key
