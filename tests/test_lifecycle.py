"""Offline Discord registration/lifecycle checks; no login or paid API traffic."""
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from main import ChronicleBot
from nanny.config import Config


@pytest.mark.asyncio
async def test_offline_bot_registers_commands_and_closes(tmp_path):
    config = Config("fake-token", Fernet.generate_key().decode(), str(tmp_path / "bot.db"))
    bot = ChronicleBot(config)
    bot.tree.sync = AsyncMock()
    try:
        await bot.setup_hook()
        payloads = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
        names = {payload["name"] for payload in payloads}
        assert all(len(payload["description"]) <= 100 for payload in payloads)
        assert {"setup", "model", "session", "journal", "imagine", "roll", "export"} <= names
        assert bot.intents.message_content
        assert not bot.intents.members
        assert not bot.intents.presences
        assert not bot.allowed_mentions.everyone
        bot.tree.sync.assert_awaited_once_with()
    finally:
        await bot.close()
    # Shutdown must be idempotent, including partial-startup cleanup.
    await bot.close()


@pytest.mark.asyncio
async def test_test_guild_registration(tmp_path):
    config = Config("fake-token", Fernet.generate_key().decode(), str(tmp_path / "bot.db"), 123)
    bot = ChronicleBot(config)
    bot.tree.sync = AsyncMock()
    try:
        await bot.setup_hook()
        assert bot.tree.sync.await_args.kwargs["guild"].id == 123
    finally:
        await bot.close()
