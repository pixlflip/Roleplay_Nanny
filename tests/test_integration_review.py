"""Independent real-storage/engine regression checks; no external traffic."""

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import discord
import pytest

from nanny.engine import EngineError, StoryEngine
from nanny.discord_app import ChronicleCog, Dashboard, EntryModal, JournalView, ReplyModal
from nanny.store import Store


class ControlledProvider:
    def __init__(self, *, fail=False, hold=False):
        self.calls = []
        self.fail = fail
        self.hold = hold
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, key, model, messages):
        self.calls.append((key, model, messages))
        self.started.set()
        if self.hold:
            await self.release.wait()
        if self.fail:
            raise EngineError("An intentionally safe provider failure.")
        return "The lantern illuminates a silver door."


def make_campaign(store):
    store.set_settings(111, api_key="fake-owner-private-key", model="test/story")
    return store.create_session(111, 222, 333, "The lantern", "A patient narrator", "A locked tower")


def fake_interaction(owner_id=111):
    state = {"done": False}

    async def defer(**kwargs):
        state["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=owner_id), guild_id=222, channel_id=333,
        guild=SimpleNamespace(id=222), is_expired=lambda: False,
        response=SimpleNamespace(
            is_done=lambda: state["done"], defer=defer,
            send_message=AsyncMock(), edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_repeated_real_store_turn_survives_engine_and_database_restart(tmp_path):
    path = tmp_path / "story.db"
    key = Fernet.generate_key()
    store = Store(path, key)
    campaign = make_campaign(store)
    first_provider = ControlledProvider()
    try:
        first = await StoryEngine(store, first_provider, cooldown_seconds=0).turn(
            campaign["id"], 111, "I light the lantern.", "discord-message-1"
        )
    finally:
        store.close()

    store = Store(path, key)
    second_provider = ControlledProvider()
    try:
        second = await StoryEngine(store, second_provider, cooldown_seconds=0).turn(
            campaign["id"], 111, "I light the lantern.", "discord-message-1"
        )
        assert second == first
        assert len(first_provider.calls) == 1
        assert second_provider.calls == []
        assert len(store.history(campaign["id"], 111)) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_paid_request_cannot_replay_after_restart(tmp_path):
    path = tmp_path / "story.db"
    key = Fernet.generate_key()
    store = Store(path, key)
    campaign = make_campaign(store)
    provider = ControlledProvider(fail=True)
    try:
        with pytest.raises(EngineError):
            await StoryEngine(store, provider, cooldown_seconds=0).turn(
                campaign["id"], 111, "Open the door.", "failed-message"
            )
    finally:
        store.close()

    store = Store(path, key)
    replacement = ControlledProvider()
    try:
        with pytest.raises(EngineError) as caught:
            await StoryEngine(store, replacement, cooldown_seconds=0).turn(
                campaign["id"], 111, "Open the door.", "failed-message"
            )
        assert caught.value.code == "request_already_attempted"
        assert replacement.calls == []
        assert store.history(campaign["id"], 111) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancelled_paid_request_stays_claimed(tmp_path):
    store = Store(tmp_path / "story.db", Fernet.generate_key())
    campaign = make_campaign(store)
    provider = ControlledProvider(hold=True)
    engine = StoryEngine(store, provider, cooldown_seconds=0)
    task = asyncio.create_task(engine.turn(campaign["id"], 111, "Open the door.", "cancelled"))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        provider.release.set()
        with pytest.raises(EngineError) as caught:
            await engine.turn(campaign["id"], 111, "Open the door.", "cancelled")
        assert caught.value.code == "request_already_attempted"
        assert len(provider.calls) == 1
        assert store.history(campaign["id"], 111) == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
async def test_independent_engines_cannot_bill_same_request_twice(tmp_path):
    path = tmp_path / "story.db"
    key = Fernet.generate_key()
    store1 = Store(path, key)
    campaign = make_campaign(store1)
    store2 = Store(path, key)
    provider = ControlledProvider(hold=True)
    first = StoryEngine(store1, provider, cooldown_seconds=0)
    second = StoryEngine(store2, provider, cooldown_seconds=0)
    task = asyncio.create_task(first.turn(campaign["id"], 111, "Look around.", "same-message"))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        with pytest.raises(EngineError) as caught:
            await second.turn(campaign["id"], 111, "Look around.", "same-message")
        assert caught.value.code == "request_already_attempted"
        provider.release.set()
        await task
        assert len(provider.calls) == 1
        assert len(store2.history(campaign["id"], 111)) == 2
    finally:
        provider.release.set()
        await asyncio.gather(task, return_exceptions=True)
        store1.close()
        store2.close()


@pytest.mark.asyncio
async def test_owner_is_checked_before_provider_and_keys_never_enter_context(tmp_path):
    store = Store(tmp_path / "story.db", Fernet.generate_key())
    campaign = make_campaign(store)
    store.set_settings(999, api_key="other-player-private-key", model="test/story")
    provider = ControlledProvider()
    engine = StoryEngine(store, provider, cooldown_seconds=0)
    try:
        store.add_entry(campaign["id"], 111, "fact", "The key", "A brass key opens the silver door.")
        with pytest.raises((ValueError, EngineError)):
            await engine.turn(campaign["id"], 999, "Read another story.", "attacker-message")
        assert provider.calls == []

        await engine.turn(campaign["id"], 111, "Try the brass key.", "owner-message")
        context = json.dumps(provider.calls[0][2])
        assert "A brass key opens the silver door." in context
        assert "fake-owner-private-key" not in context
        assert "other-player-private-key" not in context
        exported = json.dumps(store.export_session(campaign["id"], 111))
        assert "fake-owner-private-key" not in exported
        assert "other-player-private-key" not in exported
        assert "api_key" not in exported
    finally:
        store.close()


@pytest.mark.asyncio
async def test_real_store_ui_views_resolve_strict_integer_session_ids(tmp_path):
    store = Store(tmp_path / "story.db", Fernet.generate_key())
    campaign = make_campaign(store)
    try:
        cog = ChronicleCog(SimpleNamespace(), store, SimpleNamespace(), SimpleNamespace())
        dashboard = Dashboard(cog, 111, str(campaign["id"]))
        embed = dashboard.embed(campaign)
        assert "The lantern" in embed.title
        journal = JournalView(cog, 111, str(campaign["id"]), "fact")
        assert journal.entries == []
        assert cog.owned_session(str(campaign["id"]), 111)["id"] == campaign["id"]
        assert cog.owned_session(str(campaign["id"]), 999) is None
        dashboard.stop()
        journal.stop()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_real_store_message_handler_generates_once_for_owner(tmp_path):
    store = Store(tmp_path / "story.db", Fernet.generate_key())
    make_campaign(store)
    provider = ControlledProvider()
    engine = StoryEngine(store, provider, cooldown_seconds=0)
    cog = ChronicleCog(SimpleNamespace(), store, engine, provider)

    @asynccontextmanager
    async def typing():
        yield

    everyone, bot, owner = discord.Object(222), discord.Object(555), discord.Object(111)
    guild = SimpleNamespace(id=222, default_role=everyone, me=bot)
    channel = SimpleNamespace(
        id=333, guild=guild, send=AsyncMock(), typing=typing,
        overwrites={
            everyone: discord.PermissionOverwrite(view_channel=False),
            bot: discord.PermissionOverwrite(view_channel=True),
            owner: discord.PermissionOverwrite(view_channel=True),
        },
    )
    message = SimpleNamespace(
        id=444, author=SimpleNamespace(id=111, bot=False), webhook_id=None,
        guild=guild, channel=channel, content="I open the door.",
    )
    try:
        await cog.on_message(message)
        await cog.on_message(message)
        assert len(provider.calls) == 1
        channel.send.assert_awaited_once()
        assert "silver door" in channel.send.await_args.args[0]

        message.id = 445
        message.author.id = 999
        await cog.on_message(message)
        assert len(provider.calls) == 1
        assert channel.send.await_count == 1

        # A new public-role grant must stop the same real Engine before any billing.
        message.id = 446
        message.author.id = 111
        channel.overwrites[discord.Object(888)] = discord.PermissionOverwrite(view_channel=True)
        await cog.on_message(message)
        assert len(provider.calls) == 1
        assert "Narration is paused" in channel.send.await_args.args[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_real_store_entry_modal_adds_edits_and_rejects_expired_or_foreign_submit(tmp_path):
    store = Store(tmp_path / "story.db", Fernet.generate_key())
    campaign = make_campaign(store)
    cog = ChronicleCog(SimpleNamespace(), store, SimpleNamespace(), SimpleNamespace())
    try:
        modal = EntryModal(cog, 111, str(campaign["id"]), "item")
        modal.name._value = "Brass key"
        modal.content._value = "Opens the silver door."
        await modal.on_submit(fake_interaction())
        entry, = store.list_entries(campaign["id"], 111, "item")
        assert entry["content"] == "Opens the silver door."

        edit = EntryModal(cog, 111, str(campaign["id"]), "item", entry)
        edit.name._value = "Brass key"
        edit.content._value = "It glows faintly."
        await edit.on_submit(fake_interaction())
        assert store.list_entries(campaign["id"], 111, "item")[0]["content"] == "It glows faintly."

        for foreign in (True, False):
            rejected = EntryModal(cog, 111, str(campaign["id"]), "fact")
            rejected.name._value = "Unwanted change"
            rejected.content._value = "This must never be saved."
            if not foreign:
                rejected.expires_at = 0
            await rejected.on_submit(fake_interaction(999 if foreign else 111))
        assert store.list_entries(campaign["id"], 111, "fact") == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reply_correction_rejects_newer_turn_even_with_identical_response(tmp_path):
    store = Store(tmp_path / "story.db", Fernet.generate_key())
    campaign = make_campaign(store)
    cog = ChronicleCog(SimpleNamespace(), store, SimpleNamespace(), SimpleNamespace())
    try:
        store.append_turn(campaign["id"], 111, "Open the door.", "The door is locked.", "first")
        modal = ReplyModal(cog, 111, str(campaign["id"]))
        modal.content._value = "The first door was actually open."
        store.append_turn(campaign["id"], 111, "Try the second door.", "The door is locked.", "second")
        await modal.on_submit(fake_interaction())
        assert store.history(campaign["id"], 111)[-1]["content"] == "The door is locked."
    finally:
        store.close()
