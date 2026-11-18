"""Authenticated encryption for the public repository's cached listing history."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import ConfigurationError, ensure_home


def cipher(key: str):
    from cryptography.fernet import Fernet
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise ConfigurationError("The cloud state encryption key is missing or invalid.") from None


def restore_state(encrypted: Path, home: Path, key: str) -> bool:
    from cryptography.fernet import InvalidToken
    decoder = cipher(key)
    ensure_home(home)
    if not encrypted.is_file():
        return False
    if encrypted.stat().st_size > 20_000_000:
        raise ConfigurationError("The cloud state cache is unexpectedly large; no website access is allowed.")
    try:
        data = decoder.decrypt(encrypted.read_bytes())
    except InvalidToken:
        raise ConfigurationError("The cloud state cache could not be authenticated; no website access is allowed.") from None
    if not data.startswith(b"SQLite format 3\x00"):
        raise ConfigurationError("The cloud state cache is not a valid listing database.")
    path = home / "state.db"
    path.write_bytes(data)
    path.chmod(0o600)
    return True


def seal_state(home: Path, encrypted: Path, key: str) -> None:
    encoder = cipher(key)
    data = encoder.encrypt((home / "state.db").read_bytes())
    with NamedTemporaryFile("wb", dir=encrypted.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            temp.chmod(0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temp.replace(encrypted)
        finally:
            temp.unlink(missing_ok=True)

