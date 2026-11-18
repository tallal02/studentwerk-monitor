"""Local settings; secret values never belong in source files or logs."""

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

DEFAULT_HOME = Path.home() / "Library/Application Support/StudentWerk Monitor"
CHANNELS = ("telegram", "gmail")


class ConfigurationError(Exception):
    pass


def ensure_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)


def save_json(path: Path, value: dict) -> None:
    ensure_home(path.parent)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as stream:
        temp = Path(stream.name)
        try:
            os.chmod(temp, 0o600)
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ConfigurationError(f"Cannot read {path.name}; run setup again.") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"Invalid {path.name}; run setup again.")
    return value


def notification_config(home: Path) -> dict:
    settings = load_json(home / "config.json")
    secrets = load_json(home / "credentials.json")
    config = {**settings, **secrets}
    required = ("telegram_token", "telegram_chat_id", "gmail_address", "gmail_recipient", "gmail_app_password")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ConfigurationError("Notification setup is incomplete. Run the setup command first.")
    for key in required:
        if not isinstance(config[key], str) or "\n" in config[key] or "\r" in config[key]:
            raise ConfigurationError("Invalid notification configuration; run setup again.")
    return config


def config_fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

