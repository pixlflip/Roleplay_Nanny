"""Small, durable SQLite storage for private roleplay campaigns.

All public operations are synchronous and protected by a reentrant lock. Keep
network requests outside these operations. API keys are encrypted at rest; the
Fernet key must be supplied by the operator and retained across restarts.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_MODEL = "openai/gpt-4o-mini"
ENTRY_KINDS = frozenset({"fact", "item", "quest", "note"})
SCHEMA_VERSION = 3
MAX_MESSAGE_LENGTH = 32_000
_LOGGER = logging.getLogger(__name__)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


# Ordered, additive migrations. Never touch the old bot's database.db.
_MIGRATIONS = {
    1: (
        """CREATE TABLE user_settings (
            user_id INTEGER PRIMARY KEY,
            api_key_encrypted BLOB,
            model TEXT NOT NULL DEFAULT 'openai/gpt-4o-mini',
            image_model TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            persona TEXT NOT NULL,
            scenario TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            kind TEXT NOT NULL CHECK (kind IN ('fact', 'item', 'quest', 'note')),
            name TEXT NOT NULL COLLATE NOCASE,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (session_id, kind, name)
        )""",
        """CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            request_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (session_id, request_id)
        )""",
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            turn_id INTEGER NOT NULL REFERENCES turns(id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (turn_id, role)
        )""",
    ),
    2: (
        """CREATE UNIQUE INDEX one_active_channel
           ON sessions(channel_id) WHERE archived = 0""",
        "CREATE INDEX owner_sessions ON sessions(owner_id, guild_id, id)",
        "CREATE INDEX session_messages ON messages(session_id, id)",
        "CREATE INDEX session_entries ON entries(session_id, kind, id)",
    ),
    3: (
        """ALTER TABLE turns ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'
           CHECK (status IN ('pending', 'failed', 'complete'))""",
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _text(value: str, label: str, maximum: int, *, empty: bool = False,
          strip: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    if strip:
        value = value.strip()
    if len(value) > maximum or (not empty and not value.strip()) or "\x00" in value:
        minimum = 0 if empty else 1
        raise ValueError(f"{label} must contain {minimum}–{maximum:,} characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain valid Unicode text.") from exc
    return value


def _request_id(value: str | int) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(_identifier(value, "Request ID"))
    return _text(value, "Request ID", 128, strip=True)


class Store:
    """Owner-scoped storage; use a new database path such as data/chronicle.db.

    ``get_session(id)`` without an owner is an internal lookup only. User-facing
    code should always pass the acting user's ID. All mutation APIs require it.
    """

    def __init__(self, path: str | Path, encryption_key: str | bytes):
        if not isinstance(path, (str, Path)) or not str(path):
            raise ValueError("Database path must be a nonempty path.")
        try:
            key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
            self._fernet = Fernet(key)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("ENCRYPTION_KEY must be a valid Fernet key.") from exc
        self._lock = RLock()
        self._closed = False
        path_string = str(path)
        if path_string != ":memory:":
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Avoid a window in which a newly created credentials DB is public.
            try:
                descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self._connection = sqlite3.connect(
            path_string, timeout=10, check_same_thread=False, isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._migrate()
            if path_string != ":memory:" and os.name == "posix" and target.stat().st_mode & 0o077:
                _LOGGER.warning(
                    "Existing roleplay database permissions allow access by other local users. "
                    "Restrict the database and its directory to the bot's operating-system account."
                )
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("The store is closed.")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._check_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _migrate(self) -> None:
        with self._transaction():
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Database schema is newer than this bot. Upgrade the bot before opening it.")
            if version == 0:
                tables = self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if tables:
                    raise RuntimeError(
                        "Unrecognized database. Use a new data/chronicle.db path; preserve the legacy database."
                    )
            for next_version in range(version + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[next_version]:
                    self._connection.execute(statement)
                self._connection.execute(f"PRAGMA user_version = {next_version}")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> Store:
        self._check_open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def set_settings(self, user_id: int, api_key: str | None = None,
                     model: str | None = None, image_model: str | None = None) -> dict[str, Any]:
        """Update supplied fields only. Use forget_key() to remove a saved key."""
        _identifier(user_id, "User ID")
        encrypted = None
        if api_key is not None:
            api_key = _text(api_key, "API key", 512, strip=True)
            if not api_key.isascii() or any(ord(character) < 33 or ord(character) == 127 for character in api_key):
                raise ValueError("API key must contain printable ASCII without whitespace.")
            encrypted = self._fernet.encrypt(api_key.encode("utf-8"))
        if model is not None:
            model = _text(model, "Model", 200, strip=True)
            if not _MODEL_ID.fullmatch(model):
                raise ValueError("Choose a valid OpenRouter model ID.")
        if image_model is not None:
            image_model = _text(image_model, "Image model", 200, empty=True, strip=True)
            if image_model and not _MODEL_ID.fullmatch(image_model):
                raise ValueError("Choose a valid OpenRouter image model ID.")
        with self._transaction():
            self._connection.execute(
                """INSERT INTO user_settings(user_id, api_key_encrypted, model, image_model, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     api_key_encrypted = COALESCE(excluded.api_key_encrypted, user_settings.api_key_encrypted),
                     model = CASE WHEN ? IS NULL THEN user_settings.model ELSE excluded.model END,
                     image_model = CASE WHEN ? IS NULL THEN user_settings.image_model ELSE excluded.image_model END,
                     updated_at = excluded.updated_at""",
                (user_id, encrypted, model or DEFAULT_MODEL, image_model or "", _now(), model, image_model),
            )
            result = self.get_settings(user_id)
            assert result is not None
            return result

    def get_settings(self, user_id: int) -> dict[str, Any] | None:
        _identifier(user_id, "User ID")
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                "SELECT * FROM user_settings WHERE user_id = ?", (user_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            ciphertext = result.pop("api_key_encrypted")
            result["api_key"] = None
            if ciphertext is not None:
                try:
                    result["api_key"] = self._fernet.decrypt(ciphertext).decode("utf-8")
                except (InvalidToken, UnicodeError) as exc:
                    raise ValueError(
                        "The saved API key cannot be decrypted. Restore the original ENCRYPTION_KEY "
                        "or save your API key again."
                    ) from exc
            return result

    def forget_key(self, user_id: int) -> None:
        _identifier(user_id, "User ID")
        with self._transaction():
            self._connection.execute(
                "UPDATE user_settings SET api_key_encrypted = NULL, updated_at = ? WHERE user_id = ?",
                (_now(), user_id),
            )

    @staticmethod
    def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["archived"] = bool(result["archived"])
        return result

    def create_session(self, owner_id: int, guild_id: int, channel_id: int,
                       title: str, persona: str, scenario: str) -> dict[str, Any]:
        _identifier(owner_id, "Owner ID")
        _identifier(guild_id, "Guild ID")
        _identifier(channel_id, "Channel ID")
        title = _text(title, "Title", 100, strip=True)
        persona = _text(persona, "Persona", 2_000)
        scenario = _text(scenario, "Scenario", 8_000)
        with self._transaction():
            now = _now()
            try:
                cursor = self._connection.execute(
                    """INSERT INTO sessions(owner_id, guild_id, channel_id, title, persona, scenario,
                                             created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (owner_id, guild_id, channel_id, title, persona, scenario, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("This channel already has an active session.") from exc
            return self.get_session(cursor.lastrowid, owner_id)

    def get_session(self, session_id: int, owner_id: int | None = None) -> dict[str, Any]:
        _identifier(session_id, "Session ID")
        if owner_id is not None:
            _identifier(owner_id, "Owner ID")
        with self._lock:
            self._check_open()
            row = self._connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None or (owner_id is not None and row["owner_id"] != owner_id):
                raise ValueError("Session not found or you do not own it.")
            return self._session_dict(row)

    def _owned_session(self, session_id: int, owner_id: int) -> dict[str, Any]:
        _identifier(owner_id, "Owner ID")
        return self.get_session(session_id, owner_id)

    def _writable_session(self, session_id: int, owner_id: int) -> dict[str, Any]:
        session = self._owned_session(session_id, owner_id)
        if session["archived"]:
            raise ValueError("This session is archived. Resume it before making changes.")
        return session

    def get_channel_session(self, channel_id: int, owner_id: int) -> dict[str, Any] | None:
        _identifier(channel_id, "Channel ID")
        _identifier(owner_id, "Owner ID")
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE channel_id = ? AND owner_id = ? AND archived = 0",
                (channel_id, owner_id),
            ).fetchone()
            return self._session_dict(row) if row else None

    def list_sessions(self, owner_id: int, guild_id: int) -> list[dict[str, Any]]:
        _identifier(owner_id, "Owner ID")
        _identifier(guild_id, "Guild ID")
        with self._lock:
            self._check_open()
            return [self._session_dict(row) for row in self._connection.execute(
                "SELECT * FROM sessions WHERE owner_id = ? AND guild_id = ? ORDER BY id DESC",
                (owner_id, guild_id),
            )]

    def set_archived(self, session_id: int, owner_id: int, archived: bool) -> dict[str, Any]:
        if not isinstance(archived, bool):
            raise ValueError("Archived must be true or false.")
        with self._transaction():
            self._owned_session(session_id, owner_id)
            try:
                self._connection.execute(
                    "UPDATE sessions SET archived = ?, updated_at = ? WHERE id = ?",
                    (int(archived), _now(), session_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("This channel already has another active session. Archive it first.") from exc
            return self._owned_session(session_id, owner_id)

    def add_entry(self, session_id: int, owner_id: int, kind: str,
                  name: str, content: str) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in ENTRY_KINDS:
            raise ValueError("Entry kind must be fact, item, quest, or note.")
        name = _text(name, "Entry name", 100, strip=True)
        content = _text(content, "Entry content", 8_000)
        with self._transaction():
            self._writable_session(session_id, owner_id)
            now = _now()
            self._connection.execute(
                """INSERT INTO entries(session_id, kind, name, content, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, kind, name) DO UPDATE SET
                     name = excluded.name, content = excluded.content, updated_at = excluded.updated_at""",
                (session_id, kind, name, content, now, now),
            )
            self._connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            row = self._connection.execute(
                "SELECT * FROM entries WHERE session_id = ? AND kind = ? AND name = ?",
                (session_id, kind, name),
            ).fetchone()
            return dict(row)

    def delete_entry(self, session_id: int, owner_id: int, entry_id: int) -> None:
        _identifier(entry_id, "Entry ID")
        with self._transaction():
            self._writable_session(session_id, owner_id)
            cursor = self._connection.execute(
                "DELETE FROM entries WHERE id = ? AND session_id = ?", (entry_id, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Entry not found in this session.")
            self._connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))

    def update_entry(self, session_id: int, owner_id: int, entry_id: int,
                     name: str, content: str) -> dict[str, Any]:
        _identifier(entry_id, "Entry ID")
        name = _text(name, "Entry name", 100, strip=True)
        content = _text(content, "Entry content", 8_000)
        with self._transaction():
            self._writable_session(session_id, owner_id)
            now = _now()
            try:
                cursor = self._connection.execute(
                    "UPDATE entries SET name = ?, content = ?, updated_at = ? WHERE id = ? AND session_id = ?",
                    (name, content, now, entry_id, session_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("An entry of this kind already has that name.") from exc
            if cursor.rowcount != 1:
                raise ValueError("Entry not found in this session.")
            self._connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            return dict(self._connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone())

    def list_entries(self, session_id: int, owner_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is not None and (not isinstance(kind, str) or kind not in ENTRY_KINDS):
            raise ValueError("Entry kind must be fact, item, quest, or note.")
        with self._lock:
            self._owned_session(session_id, owner_id)
            return [dict(row) for row in self._connection.execute(
                "SELECT * FROM entries WHERE session_id = ? AND (? IS NULL OR kind = ?) ORDER BY id",
                (session_id, kind, kind),
            )]

    def history(self, session_id: int, owner_id: int, limit: int = 30) -> list[dict[str, str]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("History limit must be an integer between 1 and 100.")
        with self._lock:
            self._owned_session(session_id, owner_id)
            rows = self._connection.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def append_turn(self, session_id: int, owner_id: int, user_content: str,
                    assistant_content: str, request_id: str | int) -> bool:
        """Atomically save both messages; replaying a request returns False."""
        user_content = _text(user_content, "User message", MAX_MESSAGE_LENGTH)
        assistant_content = _text(assistant_content, "Assistant message", MAX_MESSAGE_LENGTH)
        request_id = _request_id(request_id)
        with self._transaction():
            self._owned_session(session_id, owner_id)
            # A replay is harmless even if the session was archived meanwhile.
            existing = self._connection.execute(
                "SELECT id, status FROM turns WHERE session_id = ? AND request_id = ?", (session_id, request_id),
            ).fetchone()
            if existing and existing["status"] == "complete":
                return False
            if existing and existing["status"] == "failed":
                raise ValueError("This request already failed. Send a new message to try again.")
            self._writable_session(session_id, owner_id)
            now = _now()
            if existing:
                turn_id = existing["id"]
                self._connection.execute("UPDATE turns SET status = 'complete' WHERE id = ?", (turn_id,))
            else:
                cursor = self._connection.execute(
                    "INSERT INTO turns(session_id, request_id, created_at, status) VALUES (?, ?, ?, 'complete')",
                    (session_id, request_id, now),
                )
                turn_id = cursor.lastrowid
            self._connection.executemany(
                "INSERT INTO messages(session_id, turn_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                [(session_id, turn_id, "user", user_content, now),
                 (session_id, turn_id, "assistant", assistant_content, now)],
            )
            self._connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            return True

    def get_turn(self, session_id: int, owner_id: int, request_id: str | int) -> str | None:
        """Read a completed request without exposing another owner's history."""
        request_id = _request_id(request_id)
        with self._lock:
            self._owned_session(session_id, owner_id)
            row = self._connection.execute(
                """SELECT messages.content FROM turns JOIN messages ON messages.turn_id = turns.id
                   WHERE turns.session_id = ? AND turns.request_id = ? AND turns.status = 'complete'
                   AND messages.role = 'assistant'""", (session_id, request_id),
            ).fetchone()
            return row["content"] if row else None

    def claim_turn(self, session_id: int, owner_id: int, request_id: str | int) -> bool:
        """Persist a request before a paid call; never reclaim uncertain calls.

        A pending record surviving a crash deliberately blocks the same request.
        The user can send a new message when they want another attempt.
        """
        request_id = _request_id(request_id)
        with self._transaction():
            self._writable_session(session_id, owner_id)
            cursor = self._connection.execute(
                """INSERT INTO turns(session_id, request_id, created_at, status)
                   VALUES (?, ?, ?, 'pending') ON CONFLICT(session_id, request_id) DO NOTHING""",
                (session_id, request_id, _now()),
            )
            return cursor.rowcount == 1

    def fail_turn(self, session_id: int, owner_id: int, request_id: str | int) -> None:
        """Retain failed request IDs so a retry cannot silently incur a second charge."""
        request_id = _request_id(request_id)
        with self._transaction():
            self._owned_session(session_id, owner_id)
            self._connection.execute(
                "UPDATE turns SET status = 'failed' WHERE session_id = ? AND request_id = ? AND status = 'pending'",
                (session_id, request_id),
            )

    def edit_last_reply(self, session_id: int, owner_id: int, content: str) -> None:
        """Replace the latest saved assistant response, retaining the user turn."""
        content = _text(content, "Assistant message", MAX_MESSAGE_LENGTH)
        with self._transaction():
            self._writable_session(session_id, owner_id)
            row = self._connection.execute(
                "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError("There is no assistant reply to edit yet.")
            self._connection.execute("UPDATE messages SET content = ? WHERE id = ?", (content, row["id"]))
            self._connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))

    def export_session(self, session_id: int, owner_id: int) -> dict[str, Any]:
        """Export the complete campaign and transcript, never account settings."""
        # A transaction makes the export a consistent snapshot across connections.
        with self._transaction():
            session = self._owned_session(session_id, owner_id)
            entries = self.list_entries(session_id, owner_id)
            messages = [dict(row) for row in self._connection.execute(
                "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id", (session_id,),
            )]
            return {"format_version": 1, "session": session, "entries": entries, "messages": messages}
