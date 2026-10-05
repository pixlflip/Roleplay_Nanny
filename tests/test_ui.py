"""Offline interaction tests backed by the real strict SQLite Store.

No Discord connection, OpenRouter call, or genuine credential is used.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
import discord
import pytest

from nanny.discord_app import (
    ChronicleCog, Dashboard, DeleteEntryView, EntryModal, ImageApproval,
    JournalView, ModelModal, ModelView, OwnerView, ReplyModal,
    SessionLibrary, SessionModal, SetupModal, card, model_id, private, roll_dice,
    split_story,
)
from nanny.engine import EngineError
from nanny.store import Store


class Actor:
    def __init__(self, id, *, bot=False, permissions=None):
        self.id, self.bot = id, bot
        self.guild_permissions = permissions or discord.Permissions.all()


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class Channel:
    def __init__(self, guild, owner, id=30):
        self.id, self.guild = id, guild
        self.overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            owner: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True),
        }
        self.send = AsyncMock()
        self.delete = AsyncMock()

    def typing(self):
        return Typing()


class Interaction:
    def __init__(self, owner, guild, channel):
        self.user, self.guild, self.channel = owner, guild, channel
        self.guild_id, self.channel_id = guild.id, channel.id
        self.expired = False
        self.filesize_limit = 10_000_000
        self.sent = []
        self.done = False

        async def send(content=None, **kwargs):
            self.sent.append((content, kwargs))
            self.done = True

        async def defer(**kwargs):
            self.done = True

        self.response = SimpleNamespace(
            is_done=lambda: self.done,
            send_message=AsyncMock(side_effect=send),
            defer=AsyncMock(side_effect=defer),
            send_modal=AsyncMock(),
            edit_message=AsyncMock(),
        )
        self.followup = SimpleNamespace(send=AsyncMock(side_effect=send))
        self.edit_original_response = AsyncMock()

    def is_expired(self):
        return self.expired


@pytest.fixture
def world():
    store = Store(":memory:", Fernet.generate_key())
    owner, outsider = Actor(11), Actor(12)
    guild = SimpleNamespace(id=20, default_role=Actor(20), me=Actor(99, bot=True))
    channel = Channel(guild, owner)
    guild.get_channel = Mock(return_value=channel)
    guild.create_text_channel = AsyncMock(return_value=channel)
    engine = SimpleNamespace(turn=AsyncMock(return_value="A lantern flickers."), image=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfake"))
    cog = ChronicleCog(SimpleNamespace(), store, engine, SimpleNamespace())
    store.set_settings(owner.id, api_key="fake-test-key", model="openai/gpt-4o-mini", image_model="test/image")
    session = store.create_session(owner.id, guild.id, channel.id, "Lanterns", "A gentle narrator", "Mist covers the harbor.")
    yield SimpleNamespace(store=store, owner=owner, outsider=outsider, guild=guild, channel=channel, engine=engine, cog=cog, session=session, interaction=lambda actor=None: Interaction(actor or owner, guild, channel))
    store.close()


def message(world, *, author=None, content="I raise the lantern.", id=100, webhook_id=None):
    return SimpleNamespace(author=author or world.owner, guild=world.guild, channel=world.channel, id=id, content=content, webhook_id=webhook_id)


def set_value(field, value):
    field._value = value


@pytest.mark.asyncio
async def test_owner_views_reject_intruders_and_expiry(world):
    view = OwnerView(world.owner.id)
    other = world.interaction(world.outsider)
    assert not await view.interaction_check(other)
    assert other.sent[0][1]["ephemeral"]
    view.expires_at = 0
    own = world.interaction()
    assert not await view.authorize(own)
    assert "expired" in own.sent[0][0]


@pytest.mark.asyncio
async def test_timeout_disables_controls_even_if_message_gone(world):
    view = Dashboard(world.cog, world.owner.id, world.session["id"])
    view.origin = world.interaction()
    await view.on_timeout()
    assert all(item.disabled for item in view.children)
    assert view.is_finished()
    view.origin.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_interaction_never_falls_back_public(world):
    interaction = world.interaction()
    interaction.expired = True
    assert not await private(interaction, "private text")
    assert interaction.sent == []
    world.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_cancel_does_not_mutate_or_prefill_key(world):
    before = world.store.get_settings(world.owner.id)
    modal = SetupModal(world.cog, world.owner.id)
    assert not str(modal.api_key)
    assert "Discord" in modal.title
    await modal.on_timeout()
    assert world.store.get_settings(world.owner.id) == before


@pytest.mark.asyncio
async def test_setup_owner_and_once_only_submission(world):
    modal = SetupModal(world.cog, world.owner.id)
    set_value(modal.api_key, "replacement-fake-key")
    set_value(modal.text_model, "provider/model")
    set_value(modal.image_model, "")
    await modal.on_submit(world.interaction(world.outsider))
    assert world.store.get_settings(world.owner.id)["api_key"] == "fake-test-key"
    interaction = world.interaction()
    await modal.on_submit(interaction)
    assert world.store.get_settings(world.owner.id)["api_key"] == "replacement-fake-key"
    assert str(modal.api_key) == ""
    assert "replacement-fake-key" not in repr(interaction.sent)
    await modal.on_submit(world.interaction())
    assert world.store.get_settings(world.owner.id)["api_key"] == "replacement-fake-key"


@pytest.mark.asyncio
async def test_setup_blank_key_preserves_existing(world):
    modal = SetupModal(world.cog, world.owner.id)
    set_value(modal.api_key, "")
    set_value(modal.text_model, "provider/model")
    set_value(modal.image_model, "")
    await modal.on_submit(world.interaction())
    assert world.store.get_settings(world.owner.id)["api_key"] == "fake-test-key"


@pytest.mark.asyncio
async def test_setup_recovers_from_undecryptable_key(world):
    world.store._fernet = Fernet(Fernet.generate_key())
    modal = SetupModal(world.cog, world.owner.id)
    set_value(modal.api_key, "new-test-key")
    set_value(modal.text_model, "provider/model")
    set_value(modal.image_model, "")
    await modal.on_submit(world.interaction())
    assert world.store.get_settings(world.owner.id)["api_key"] == "new-test-key"


@pytest.mark.asyncio
async def test_model_view_and_modal_owner_guards(world):
    view = ModelView(world.cog, world.owner.id)
    view.selector._values = ["openrouter/auto"]
    await view.choose(world.interaction(world.outsider))
    assert world.store.get_settings(world.owner.id)["model"] == "openai/gpt-4o-mini"
    await view.choose(world.interaction())
    assert world.store.get_settings(world.owner.id)["model"] == "openrouter/auto"
    modal = ModelModal(world.cog, world.owner.id)
    set_value(modal.text_model, "provider/custom")
    set_value(modal.image_model, "test/image")
    await modal.on_submit(world.interaction())
    assert world.store.get_settings(world.owner.id)["model"] == "provider/custom"


@pytest.mark.asyncio
async def test_real_store_dashboard_and_all_journal_kinds(world):
    await world.cog.show_dashboard(world.interaction(), str(world.session["id"]))
    dashboard = Dashboard(world.cog, world.owner.id, str(world.session["id"]))
    assert len(dashboard.embed(world.session)) < 6000
    for kind in ("item", "fact", "quest", "note"):
        modal = EntryModal(world.cog, world.owner.id, str(world.session["id"]), kind)
        set_value(modal.name, "A name")
        set_value(modal.content, "A detail")
        interaction = world.interaction()
        await modal.on_submit(interaction)
        assert world.store.list_entries(world.session["id"], world.owner.id, kind)[0]["content"] == "A detail"
        assert interaction.sent[-1][1]["ephemeral"]


@pytest.mark.asyncio
async def test_entry_modal_cancel_unauthorized_and_repeat(world):
    modal = EntryModal(world.cog, world.owner.id, world.session["id"], "item")
    set_value(modal.name, "Lantern")
    set_value(modal.content, "Still lit")
    await modal.on_submit(world.interaction(world.outsider))
    assert not world.store.list_entries(world.session["id"], world.owner.id)
    await modal.on_submit(world.interaction())
    await modal.on_submit(world.interaction())
    assert len(world.store.list_entries(world.session["id"], world.owner.id)) == 1
    cancelled = EntryModal(world.cog, world.owner.id, world.session["id"], "fact")
    await cancelled.on_timeout()
    assert len(world.store.list_entries(world.session["id"], world.owner.id)) == 1


@pytest.mark.asyncio
async def test_journal_edit_rejects_stale_form(world):
    entry = world.store.add_entry(world.session["id"], world.owner.id, "item", "Lantern", "Unlit")
    modal = EntryModal(world.cog, world.owner.id, world.session["id"], "item", entry)
    set_value(modal.name, "Lantern")
    set_value(modal.content, "Old form")
    world.store.update_entry(world.session["id"], world.owner.id, entry["id"], "Lantern", "Newer edit")
    interaction = world.interaction()
    await modal.on_submit(interaction)
    assert "changed" in interaction.sent[-1][0]
    assert world.store.list_entries(world.session["id"], world.owner.id)[0]["content"] == "Newer edit"


@pytest.mark.asyncio
async def test_archived_entry_modal_does_not_write(world):
    modal = EntryModal(world.cog, world.owner.id, world.session["id"], "item")
    set_value(modal.name, "Lantern")
    set_value(modal.content, "Unlit")
    world.store.set_archived(world.session["id"], world.owner.id, True)
    await modal.on_submit(world.interaction())
    assert not world.store.list_entries(world.session["id"], world.owner.id)


@pytest.mark.asyncio
async def test_journal_pagination_and_embed_limits(world):
    for index in range(14):
        world.store.add_entry(world.session["id"], world.owner.id, "item", "N" * 90 + str(index), "X" * 2000)
    view = JournalView(world.cog, world.owner.id, world.session["id"], "item")
    assert len(view.embed()) < 6000
    assert len(view.selector.options) == 4
    for _ in range(20):
        await view.next_page.callback(world.interaction())
    assert view.page == 3
    assert view.next_page.disabled
    assert len(view.embed()) < 6000
    for _ in range(20):
        await view.previous.callback(world.interaction())
    assert view.page == 0


@pytest.mark.asyncio
async def test_delete_confirmation_once_and_cancel_race(world):
    entry = world.store.add_entry(world.session["id"], world.owner.id, "fact", "Moon", "Blue")
    view = DeleteEntryView(world.cog, world.owner.id, world.session["id"], entry)
    lock = world.cog.session_lock(world.session["id"])
    await lock.acquire()
    deleting = asyncio.create_task(view.confirm.callback(world.interaction()))
    await asyncio.sleep(0)
    cancel = world.interaction()
    await view.cancel.callback(cancel)
    assert "Kept" not in repr(cancel.sent)
    lock.release()
    await deleting
    await view.confirm.callback(world.interaction())
    assert not world.store.list_entries(world.session["id"], world.owner.id)


@pytest.mark.asyncio
async def test_delete_cancel_keeps_entry(world):
    entry = world.store.add_entry(world.session["id"], world.owner.id, "fact", "Moon", "Blue")
    view = DeleteEntryView(world.cog, world.owner.id, world.session["id"], entry)
    await view.cancel.callback(world.interaction())
    await view.confirm.callback(world.interaction())
    assert len(world.store.list_entries(world.session["id"], world.owner.id)) == 1


@pytest.mark.asyncio
async def test_library_dropdown_resolves_real_integer_ids(world):
    for index in range(28):
        world.store.create_session(world.owner.id, world.guild.id, 1000 + index, f"World {index}", "Guide", "Start")
    view = SessionLibrary(world.cog, world.owner.id, world.guild.id)
    assert len(view.selector.options) == 25
    await view.next_page.callback(world.interaction())
    assert len(view.selector.options) == 4
    view.selector._values = [str(world.session["id"])]
    interaction = world.interaction()
    await view.choose(interaction)
    assert isinstance(interaction.sent[-1][1]["view"], Dashboard)


@pytest.mark.asyncio
async def test_archive_stale_dashboard_idempotent_and_resume_from_channel(world):
    first = Dashboard(world.cog, world.owner.id, world.session["id"])
    stale = Dashboard(world.cog, world.owner.id, world.session["id"])
    first.embed(world.session)
    stale.embed(world.session)
    await first.archive.callback(world.interaction())
    await stale.archive.callback(world.interaction())
    assert world.store.get_session(world.session["id"], world.owner.id)["archived"]
    # get_channel_session intentionally excludes archived records, but UI can resume.
    interaction = world.interaction()
    await world.cog.session_resume.callback(world.cog, interaction, None)
    assert not world.store.get_session(world.session["id"], world.owner.id)["archived"]
    world.channel.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_waits_for_generation_and_delivery(world):
    started, finish = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        started.set()
        await finish.wait()
        return "The next chapter."

    world.engine.turn.side_effect = generate
    turn = asyncio.create_task(world.cog.on_message(message(world)))
    await started.wait()
    archive = asyncio.create_task(world.cog.change_archive(world.interaction(), str(world.session["id"]), True))
    await asyncio.sleep(0)
    assert not world.store.get_session(world.session["id"], world.owner.id)["archived"]
    finish.set()
    await asyncio.gather(turn, archive)
    assert world.channel.send.await_args_list[0].args[0] == "The next chapter."
    assert world.store.get_session(world.session["id"], world.owner.id)["archived"]
    await world.cog.on_message(message(world, id=101))
    assert world.engine.turn.await_count == 1


@pytest.mark.asyncio
async def test_owner_only_narration_and_duplicate_gateway_delivery(world):
    await world.cog.on_message(message(world, author=world.outsider))
    await world.cog.on_message(message(world, author=world.guild.me))
    await world.cog.on_message(message(world, webhook_id=9))
    assert world.engine.turn.await_count == 0
    await asyncio.gather(world.cog.on_message(message(world)), world.cog.on_message(message(world)))
    world.engine.turn.assert_awaited_once_with(world.session["id"], world.owner.id, "I raise the lantern.", request_id="100")
    assert world.channel.send.await_count == 1
    mentions = world.channel.send.await_args.kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles


@pytest.mark.asyncio
async def test_public_channel_fails_closed_before_api(world):
    world.channel.overwrites[world.guild.default_role].view_channel = True
    await world.cog.on_message(message(world))
    world.engine.turn.assert_not_awaited()
    assert "permissions changed" in world.channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_new_channel_member_fails_closed(world):
    world.channel.overwrites[world.outsider] = discord.PermissionOverwrite(view_channel=True)
    await world.cog.on_message(message(world))
    world.engine.turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_permissions_changed_mid_generation_suppresses_story(world):
    async def generate(*args, **kwargs):
        world.channel.overwrites[world.guild.default_role].view_channel = True
        return "Secret story text"
    world.engine.turn.side_effect = generate
    await world.cog.on_message(message(world))
    world.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_message_rejected_and_narrative_chunked(world):
    await world.cog.on_message(message(world, content="X" * 6001))
    world.engine.turn.assert_not_awaited()
    world.channel.send.reset_mock()
    world.engine.turn.return_value = "@everyone " + "X" * 8000
    await world.cog.on_message(message(world, id=101))
    assert world.channel.send.await_count >= 5
    assert all(len(call.args[0]) <= 1900 for call in world.channel.send.await_args_list)


@pytest.mark.asyncio
async def test_safe_engine_errors_and_unexpected_secret_redaction(world):
    world.engine.turn.side_effect = EngineError("Please wait 10 seconds.", code="cooldown")
    await world.cog.on_message(message(world))
    assert "10 seconds" in world.channel.send.await_args.args[0]
    world.engine.turn.side_effect = RuntimeError("fake-test-key")
    await world.cog.on_message(message(world, id=101))
    assert "fake-test-key" not in repr(world.channel.send.await_args_list)


@pytest.mark.asyncio
async def test_imagine_does_not_call_api_without_approval(world):
    interaction = world.interaction()
    await world.cog.imagine.callback(world.cog, interaction, "A moonlit harbor")
    world.engine.image.assert_not_awaited()
    view = interaction.sent[0][1]["view"]
    await view.cancel.callback(world.interaction())
    await view.approve.callback(world.interaction())
    world.engine.image.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_approval_owner_model_and_double_click(world):
    view = ImageApproval(world.cog, world.owner.id, "Moon", "test/image")
    await view.approve.callback(world.interaction(world.outsider))
    world.engine.image.assert_not_awaited()
    await asyncio.gather(view.approve.callback(world.interaction()), view.approve.callback(world.interaction()))
    world.engine.image.assert_awaited_once_with(world.owner.id, "Moon", model="test/image")
    second = ImageApproval(world.cog, world.owner.id, "Sun", "test/image")
    world.store.set_settings(world.owner.id, image_model="other/image")
    await second.approve.callback(world.interaction())
    assert world.engine.image.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("data", "extension"), [(b"\x89PNG\r\n\x1a\ntest", "png"), (b"\xff\xd8\xfftest", "jpg"), (b"GIF89atest", "gif"), (b"RIFF1234WEBPtest", "webp")])
async def test_image_attachments_match_raster_format(world, data, extension):
    world.engine.image.return_value = data
    view = ImageApproval(world.cog, world.owner.id, "Moon", "test/image")
    interaction = world.interaction()
    await view.approve.callback(interaction)
    assert interaction.sent[-1][1]["file"].filename.endswith(f".{extension}")
    assert interaction.sent[-1][1]["ephemeral"]


@pytest.mark.asyncio
async def test_export_private_without_keys_and_owner_only(world):
    world.store.add_entry(world.session["id"], world.owner.id, "item", "Lantern", "Warm")
    interaction = world.interaction()
    await world.cog.export_adventure(interaction, str(world.session["id"]))
    payload = interaction.sent[-1][1]
    data = payload["file"].fp.read()
    assert payload["ephemeral"]
    assert b"fake-test-key" not in data and b"api_key" not in data
    assert json.loads(data)["entries"][0]["name"] == "Lantern"
    outsider = world.interaction(world.outsider)
    await world.cog.export_adventure(outsider, str(world.session["id"]))
    assert all("file" not in sent[1] for sent in outsider.sent)


@pytest.mark.asyncio
async def test_reply_modal_updates_real_store_and_rejects_newer_turn(world):
    world.store.append_turn(world.session["id"], world.owner.id, "Old move", "Old reply", "200")
    modal = ReplyModal(world.cog, world.owner.id, world.session["id"])
    set_value(modal.content, "Corrected reply")
    await modal.on_submit(world.interaction())
    assert world.store.history(world.session["id"], world.owner.id)[-1]["content"] == "Corrected reply"
    stale = ReplyModal(world.cog, world.owner.id, world.session["id"])
    set_value(stale.content, "Stale correction")
    world.store.append_turn(world.session["id"], world.owner.id, "New move", "New reply", "201")
    await stale.on_submit(world.interaction())
    assert world.store.history(world.session["id"], world.owner.id)[-1]["content"] == "New reply"


@pytest.mark.asyncio
async def test_create_private_channel_permissions_and_no_paid_call(world):
    world.channel.id = 31
    interaction = world.interaction()
    await world.cog.create_adventure(interaction, "New World", "Guide", "Scenario")
    overwrites = world.guild.create_text_channel.await_args.kwargs["overwrites"]
    assert overwrites[world.guild.default_role].view_channel is False
    assert overwrites[world.owner].view_channel is True
    assert set(overwrites) == {world.guild.default_role, world.owner, world.guild.me}
    assert len(world.store.list_sessions(world.owner.id, world.guild.id)) == 2
    world.engine.turn.assert_not_awaited()
    world.engine.image.assert_not_awaited()
    assert isinstance(interaction.sent[-1][1]["view"], Dashboard)


@pytest.mark.asyncio
async def test_create_failure_cleans_channel(world, monkeypatch):
    monkeypatch.setattr(world.store, "create_session", Mock(side_effect=RuntimeError("DB unavailable fake-test-key")))
    interaction = world.interaction()
    await world.cog.create_adventure(interaction, "New", "Guide", "Scenario")
    world.channel.delete.assert_awaited_once()
    assert "fake-test-key" not in repr(interaction.sent)


@pytest.mark.asyncio
async def test_create_permission_and_limit_checks(world):
    world.guild.me.guild_permissions = discord.Permissions.none()
    await world.cog.create_adventure(world.interaction(), "New", "Guide", "Scenario")
    world.guild.create_text_channel.assert_not_awaited()
    world.guild.me.guild_permissions = discord.Permissions.all()
    for index in range(19):
        world.store.create_session(world.owner.id, world.guild.id, 1000 + index, f"World {index}", "Guide", "Start")
    await world.cog.create_adventure(world.interaction(), "New", "Guide", "Scenario")
    world.guild.create_text_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_modal_cancel_and_single_submission(world):
    world.channel.id = 31
    modal = SessionModal(world.cog, world.owner.id)
    set_value(modal.story_title, "A new world")
    set_value(modal.persona, "A guide")
    set_value(modal.scenario, "The door opens")
    await modal.on_submit(world.interaction(world.outsider))
    world.guild.create_text_channel.assert_not_awaited()
    await asyncio.gather(modal.on_submit(world.interaction()), modal.on_submit(world.interaction()))
    world.guild.create_text_channel.assert_awaited_once()
    unused = SessionModal(world.cog, world.owner.id)
    await unused.on_timeout()
    assert world.guild.create_text_channel.await_count == 1


@pytest.mark.asyncio
async def test_missing_channel_and_invalid_session_recovery(world):
    world.store.set_archived(world.session["id"], world.owner.id, True)
    world.guild.get_channel.return_value = None
    interaction = world.interaction()
    await world.cog.change_archive(interaction, str(world.session["id"]), False)
    assert "channel is missing" in interaction.sent[-1][0]
    assert world.store.get_session(world.session["id"], world.owner.id)["archived"]
    for value in ("nope", "-1", "0", "999999999999999999999999999999"):
        assert world.cog.owned_session(value, world.owner.id) is None


@pytest.mark.asyncio
async def test_discord_component_limits_and_registered_command_tree(world):
    modals = [SetupModal(world.cog, world.owner.id), ModelModal(world.cog, world.owner.id), SessionModal(world.cog, world.owner.id), EntryModal(world.cog, world.owner.id, world.session["id"], "item"), ReplyModal(world.cog, world.owner.id, world.session["id"])]
    for modal in modals:
        assert len(modal.title) <= 45
        assert len(modal.children) <= 5
        assert all(len(field.label) <= 45 and field.max_length <= 4000 for field in modal.children)
    assert {command.name for command in world.cog.__cog_app_commands__} == {"session", "journal", "help", "setup", "forget-key", "model", "imagine", "roll", "export"}
    interaction = world.interaction()
    await world.cog.help_command.callback(world.cog, interaction)
    assert len(interaction.sent[-1][1]["embed"]) < 6000


@pytest.mark.parametrize("notation", ["0d6", "21d6", "2d1", "1d1001", "1d6+1001", "1d6-1001", "drop table", "1000000d6", "1d6; exit"])
def test_dice_rejects_unbounded_or_invalid_input(notation):
    with pytest.raises(ValueError):
        roll_dice(notation)


def test_dice_bounds_and_text_utilities():
    values, total, text = roll_dice("20d1000-1000")
    assert len(values) == 20 and all(1 <= value <= 1000 for value in values)
    assert total == sum(values) - 1000 and text == "20d1000-1000"
    assert all(len(part) <= 1900 for part in split_story("x" * 10_000))
    assert len(card("x" * 999, "x" * 9000)) < 6000
    assert model_id("provider/model-name:free")
    for value in ("bad model", "provider/+bad", "bad\nmodel", "x" * 201):
        with pytest.raises(ValueError):
            model_id(value)


@pytest.mark.asyncio
async def test_expired_interaction_cannot_mutate_models_or_submit_key(world):
    view = ModelView(world.cog, world.owner.id)
    view.selector._values = ["openrouter/auto"]
    interaction = world.interaction()
    interaction.expired = True
    await view.choose(interaction)
    assert world.store.get_settings(world.owner.id)["model"] == "openai/gpt-4o-mini"
    modal = SetupModal(world.cog, world.owner.id)
    set_value(modal.api_key, "new-key")
    set_value(modal.text_model, "provider/model")
    set_value(modal.image_model, "")
    await modal.on_submit(interaction)
    assert world.store.get_settings(world.owner.id)["api_key"] == "fake-test-key"


@pytest.mark.asyncio
async def test_timed_out_modal_cannot_save(world):
    modal = EntryModal(world.cog, world.owner.id, world.session["id"], "item")
    set_value(modal.name, "Lantern")
    set_value(modal.content, "Unlit")
    await modal.on_timeout()
    await modal.on_submit(world.interaction())
    assert not world.store.list_entries(world.session["id"], world.owner.id)


@pytest.mark.asyncio
async def test_complete_entry_reader_never_silently_truncates(world):
    content = "X" * 8000
    entry = world.store.add_entry(world.session["id"], world.owner.id, "note", "Long note", content)
    view = JournalView(world.cog, world.owner.id, world.session["id"], "note")
    view.selected_id = str(entry["id"])
    view.rebuild()
    interaction = world.interaction()
    await view.detail.callback(interaction)
    assert "".join(item[1]["embed"].description for item in interaction.sent) == content
    assert all(item[1]["ephemeral"] for item in interaction.sent)


@pytest.mark.asyncio
async def test_archived_sessions_do_not_consume_active_cap(world):
    for index in range(19):
        session = world.store.create_session(world.owner.id, world.guild.id, 1000 + index, f"World {index}", "Guide", "Start")
        world.store.set_archived(session["id"], world.owner.id, True)
    world.channel.id = 31
    await world.cog.create_adventure(world.interaction(), "New", "Guide", "Scenario")
    world.guild.create_text_channel.assert_awaited_once()
    assert len(world.store.list_sessions(world.owner.id, world.guild.id)) == 21


@pytest.mark.asyncio
async def test_presets_are_editable_and_do_not_create_until_submitted(world):
    for preset in ("cozy-fantasy", "neon-noir", "space-opera"):
        interaction = world.interaction()
        await world.cog.session_create.callback(world.cog, interaction, preset)
        modal = interaction.response.send_modal.await_args.args[0]
        assert modal.story_title.default and modal.persona.default and modal.scenario.default
    world.guild.create_text_channel.assert_not_awaited()
    world.engine.turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_account_errors_not_sent_after_privacy_change(world):
    async def generate(*args, **kwargs):
        world.channel.overwrites[world.guild.default_role].view_channel = True
        raise EngineError("Your balance is too low.", code="insufficient_funds")
    world.engine.turn.side_effect = generate
    await world.cog.on_message(message(world))
    world.channel.send.assert_not_awaited()
