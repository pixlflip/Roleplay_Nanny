"""Validated deployment settings; importing the application never starts the bot."""
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    encryption_key: str = field(repr=False)
    database: str = "data/chronicle.db"
    guild_id: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        token = env.get("DISCORD_TOKEN", "").strip()
        key = env.get("NANNY_ENCRYPTION_KEY", "").strip()
        if not token:
            raise ValueError("Set DISCORD_TOKEN in your environment or .env file.")
        if not key:
            raise ValueError("Set a stable NANNY_ENCRYPTION_KEY before starting Chronicle.")
        try:
            Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError) as from_error:
            raise ValueError("NANNY_ENCRYPTION_KEY must be a valid Fernet key.") from from_error
        raw_guild = env.get("DISCORD_GUILD_ID", "").strip()
        if raw_guild and (not raw_guild.isdecimal() or int(raw_guild) <= 0):
            raise ValueError("DISCORD_GUILD_ID must be a positive server ID or blank.")
        path = env.get("NANNY_DATABASE", "data/chronicle.db").strip()
        if not path or Path(path).name == "database.db":
            raise ValueError("Use a new database path, not the legacy database.db.")
        return cls(token, key, path, int(raw_guild) if raw_guild else None)
