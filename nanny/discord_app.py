"""Chronicle's Discord interface (discord.py 2.6+).

All management interfaces are owner-bound and ephemeral. Only narration is posted
in the owner's dedicated channel. The story never supplies channel permissions.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import io
import json
import re
import secrets
import time
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from .engine import EngineError, MAX_INPUT_CHARS
from .store import DEFAULT_MODEL

VIOLET = 0x8B5CF6
GOLD = 0xE6BD69
PANEL_TTL = 300
PAGE_SIZE = 4
MODEL_CHOICES = (
    ("OpenRouter · automatic routing", "openrouter/auto"),
    ("Claude Sonnet 4", "anthropic/claude-sonnet-4"),
    ("GPT-4.1", "openai/gpt-4.1"),
    ("Gemini 2.5 Flash", "google/gemini-2.5-flash"),
)
KINDS = {"item": ("🎒", "Inventory"), "fact": ("📜", "World facts"), "quest": ("🧭", "Quests"), "note": ("📝", "Notes")}
NO_MENTIONS = discord.AllowedMentions.none()
STARTERS = {
    "cozy-fantasy": ("The Lanterns of Hollowmere", "A warm, whimsical fantasy storyteller. Offer meaningful choices and gentle mysteries.", "You inherit a tiny tea shop in a misty village where the lanterns remember stories. Tonight, one goes dark."),
    "neon-noir": ("Rain over Neon Harbor", "A cinematic noir narrator. Keep clues fair, characters layered, and the player's decisions consequential.", "Rain hisses against the neon outside your detective office. An android arrives with a photograph of a crime that has not happened yet."),
    "space-opera": ("Beyond the Quiet Stars", "An adventurous science-fiction game master. Balance wonder, crew relationships, and daring exploration.", "Your small salvage ship discovers a silent beacon beyond the mapped stars. It is broadcasting your captain's childhood lullaby."),
}


def clipped(value: Any, limit: int) -> str:
    value = str(value or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def card(title: str, description: str = "", *, gold: bool = False) -> discord.Embed:
    result = discord.Embed(title=clipped(title, 256), description=clipped(description, 4000), color=GOLD if gold else VIOLET)
    result.set_footer(text="CHRONICLE  ✦  Your world. Your choices.")
    return result


def model_id(value: str, *, optional: bool = False) -> str:
    value = value.strip()
    if optional and not value:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,199}", value):
        raise ValueError("Use a provider/model ID, up to 200 characters, without spaces.")
    return value


def required_text(value: str, name: str, maximum: int) -> str:
    value = value.strip()
    if not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must contain 1–{maximum} characters.")
    return value


def split_story(text: str, limit: int = 1900) -> list[str]:
    """Split on natural boundaries, including pathological unbroken model output."""
    text = str(text).strip()
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        end = max(text.rfind("\n", 0, limit + 1), text.rfind(" ", 0, limit + 1))
        if end < limit // 2:
            end = limit
        parts.append(text[:end])
        text = text[end:].lstrip()
    return parts or ["The storyteller returned an empty response. Try a different action."]


def roll_dice(notation: str) -> tuple[list[int], int, str]:
    match = re.fullmatch(r"\s*(\d{1,2})[dD](\d{1,4})([+-]\d{1,4})?\s*", notation)
    if not match:
        raise ValueError("Use NdS±M, for example 2d6+3. Limits: 1–20 dice, 2–1000 sides, ±1000 modifier.")
    count, sides = int(match[1]), int(match[2])
    modifier = int(match[3] or 0)
    if not 1 <= count <= 20 or not 2 <= sides <= 1000 or abs(modifier) > 1000:
        raise ValueError("Limits: 1–20 dice, 2–1000 sides, and a modifier from −1000 to +1000.")
    rolls = [secrets.randbelow(sides) + 1 for _ in range(count)]
    return rolls, sum(rolls) + modifier, f"{count}d{sides}" + (f"{modifier:+}" if modifier else "")


async def private(interaction: discord.Interaction, content: str | None = None, **kwargs: Any) -> bool:
    """Never fall back to a public reply when an interaction token has expired."""
    if interaction.is_expired():
        return False
    kwargs.update(ephemeral=True, allowed_mentions=NO_MENTIONS)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
        return True
    except (discord.NotFound, discord.Forbidden):
        return False


async def deferred(interaction: discord.Interaction) -> bool:
    if interaction.is_expired():
        return False
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        return True
    except (discord.NotFound, discord.Forbidden):
        return False


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float = PANEL_TTL):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.expires_at = time.monotonic() + timeout
        self.origin: discord.Interaction | None = None

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.is_expired():
            return False
        if interaction.user.id != self.owner_id:
            await private(interaction, "This panel belongs to another player. Open your own with /session library.")
            return False
        if time.monotonic() >= self.expires_at or self.is_finished():
            await private(interaction, "This panel has expired. Open a fresh panel with the same command.")
            return False
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.authorize(interaction)

    async def present(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        self.origin = interaction
        await private(interaction, embed=embed, view=self)

    async def refresh(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        if interaction.is_expired():
            return
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.response.edit_message(embed=embed, view=self, allowed_mentions=NO_MENTIONS)
        except (discord.NotFound, discord.Forbidden):
            self.stop()

    async def finish(self, interaction: discord.Interaction) -> bool:
        """Consume a one-shot panel and visibly disable its original controls."""
        self.stop()
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if interaction.is_expired():
            return False
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.edit_original_response(view=self, allowed_mentions=NO_MENTIONS)
            return True
        except discord.HTTPException:
            # Do not spend or mutate if we cannot acknowledge the click.
            return False

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.origin is not None and not self.origin.is_expired():
            try:
                await self.origin.edit_original_response(view=self)
            except discord.HTTPException:
                pass
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        # Do not send/log exception repr: API transports and validators can contain keys.
        await private(interaction, "That action couldn't be completed. Reopen the panel and try again.")


class OwnerModal(discord.ui.Modal):
    def __init__(self, owner_id: int, *, title: str):
        super().__init__(title=title, timeout=PANEL_TTL)
        self.owner_id = owner_id
        self.expires_at = time.monotonic() + PANEL_TTL
        self.submitted = False

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.is_expired():
            return False
        if interaction.user.id != self.owner_id:
            await private(interaction, "Only the player who opened this form can submit it.")
            return False
        if self.submitted or self.is_finished() or time.monotonic() >= self.expires_at:
            await private(interaction, "This form has expired or was already submitted. Open a new one.")
            return False
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.authorize(interaction)

    async def claim(self, interaction: discord.Interaction) -> bool:
        if not await self.authorize(interaction):
            return False
        self.submitted = True
        return True

    async def on_timeout(self) -> None:
        self.expires_at = 0
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await private(interaction, "This form couldn't be saved. No secret values are shown here. Please open it again.")


class SetupModal(OwnerModal):
    def __init__(self, cog: ChronicleCog, owner_id: int):
        super().__init__(owner_id, title="Setup · Discord transports your key")
        self.cog = cog
        settings = cog.form_settings(owner_id)
        self.api_key = discord.ui.TextInput(label="OpenRouter key (Discord receives this)", placeholder="Blank keeps your existing key. Never use chat.", required=False, max_length=512)
        self.text_model = discord.ui.TextInput(label="Story model · provider/model", default=settings.get("model") or DEFAULT_MODEL, max_length=200)
        self.image_model = discord.ui.TextInput(label="Image model · optional, blank disables", default=settings.get("image_model") or "", required=False, max_length=200)
        for item in (self.api_key, self.text_model, self.image_model):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.claim(interaction):
            return
        try:
            text_model = model_id(str(self.text_model))
            image_model = model_id(str(self.image_model), optional=True)
            key = str(self.api_key).strip()
            self.api_key._value = ""
            if key and (len(key) > 512 or not key.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in key)):
                raise ValueError("The key must be at most 512 printable ASCII characters without whitespace.")
        except ValueError as error:
            await private(interaction, str(error))
            return
        await deferred(interaction)
        try:
            self.cog.store.set_settings(self.owner_id, api_key=key or None, model=text_model, image_model=image_model)
        finally:
            # Do not keep a key in the modal after submission.
            self.api_key._value = ""
        await private(interaction, embed=card("✦ Your storyteller is ready", "Your settings were saved privately. Use /session create to begin.\n\nDiscord transports modal submissions. Your key is never echoed in chat or included in exports. /forget-key removes the stored key.\n\nStory turns use your OpenRouter balance. Images require a separate approval every time.", gold=True))


class ModelModal(OwnerModal):
    def __init__(self, cog: ChronicleCog, owner_id: int):
        super().__init__(owner_id, title="Choose your storyteller")
        self.cog = cog
        settings = cog.form_settings(owner_id)
        self.text_model = discord.ui.TextInput(label="Story model · provider/model", default=settings.get("model") or DEFAULT_MODEL, max_length=200)
        self.image_model = discord.ui.TextInput(label="Image model · blank disables images", default=settings.get("image_model") or "", max_length=200, required=False)
        self.add_item(self.text_model)
        self.add_item(self.image_model)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.claim(interaction):
            return
        try:
            story = model_id(str(self.text_model))
            image = model_id(str(self.image_model), optional=True)
        except ValueError as error:
            await private(interaction, str(error))
            return
        self.cog.store.set_settings(self.owner_id, model=story, image_model=image)
        await private(interaction, embed=card("✦ Models updated", f"Story: `{story}`\nImages: `{image or 'disabled'}`\n\nModel availability and provider charges vary. No API request was made."))


class ModelView(OwnerView):
    def __init__(self, cog: ChronicleCog, owner_id: int):
        super().__init__(owner_id)
        self.cog = cog
        choices = discord.ui.Select(placeholder="Choose a story model", options=[discord.SelectOption(label=name, value=value) for name, value in MODEL_CHOICES])
        choices.callback = self.choose
        self.add_item(choices)
        self.selector = choices

    async def choose(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        value = self.selector.values[0]
        if value not in {item[1] for item in MODEL_CHOICES}:
            await private(interaction, "Choose one of the listed models.")
            return
        self.cog.store.set_settings(self.owner_id, model=value)
        await self.refresh(interaction, self.embed())

    def embed(self) -> discord.Embed:
        settings = self.cog.store.get_settings(self.owner_id) or {}
        return card("✦ The storyteller's voice", f"Story: `{clipped(settings.get('model') or DEFAULT_MODEL, 200)}`\nImages: `{clipped(settings.get('image_model') or 'disabled', 200)}`\n\nChoose a model below, or enter any OpenRouter model ID. Presets are conveniences, not availability guarantees. Changing models keeps your story and journal. Provider charges vary.")

    @discord.ui.button(label="Custom model / images", emoji="✏️", style=discord.ButtonStyle.secondary, row=1)
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            await interaction.response.send_modal(ModelModal(self.cog, self.owner_id))


class SessionModal(OwnerModal):
    def __init__(self, cog: ChronicleCog, owner_id: int, preset: str = "custom"):
        super().__init__(owner_id, title="Begin a new chronicle")
        self.cog = cog
        starter = STARTERS.get(preset, ("", "", ""))
        self.story_title = discord.ui.TextInput(label="Adventure title", placeholder="The Lanterns of Hollowmere", default=starter[0], max_length=80)
        self.persona = discord.ui.TextInput(label="Storyteller persona", placeholder="A warm, atmospheric fantasy game master", default=starter[1], style=discord.TextStyle.paragraph, max_length=1000)
        self.scenario = discord.ui.TextInput(label="Opening scenario", placeholder="Who are you? Where does the story begin?", default=starter[2], style=discord.TextStyle.paragraph, max_length=3000)
        for item in (self.story_title, self.persona, self.scenario):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.claim(interaction):
            return
        try:
            title = required_text(str(self.story_title), "Title", 80)
            persona = required_text(str(self.persona), "Persona", 1000)
            scenario = required_text(str(self.scenario), "Scenario", 3000)
        except ValueError as error:
            await private(interaction, str(error))
            return
        await self.cog.create_adventure(interaction, title, persona, scenario)


class SessionLibrary(OwnerView):
    def __init__(self, cog: ChronicleCog, owner_id: int, guild_id: int):
        super().__init__(owner_id)
        self.cog, self.guild_id, self.page = cog, guild_id, 0
        self.selector = discord.ui.Select(placeholder="Open a chronicle", row=0)
        self.selector.callback = self.choose
        self.add_item(self.selector)
        self.rebuild()

    def rebuild(self) -> None:
        self.sessions = self.cog.store.list_sessions(self.owner_id, self.guild_id)
        self.page = min(self.page, max(0, (len(self.sessions) - 1) // 25))
        batch = self.sessions[self.page * 25:(self.page + 1) * 25]
        self.selector.options = [discord.SelectOption(label=clipped(s.get("title"), 90) or "Untitled", value=str(s["id"]), description=f"{'Archived' if s.get('archived') else 'Active'} · #{s['id']}", emoji="📚" if s.get("archived") else "📖") for s in batch] or [discord.SelectOption(label="No adventures yet", value="empty")]
        self.selector.disabled = not bool(batch)
        self.previous.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * 25 >= len(self.sessions)

    def embed(self) -> discord.Embed:
        return card("📚 Your chronicle library", f"**{len(self.sessions)} adventures** · Page {self.page + 1}/{max(1, (len(self.sessions) + 24) // 25)}\n\nEvery adventure has its own channel, memory, inventory, facts, and quests. Pick one below. Archived adventures can be resumed.\n\nUse /session create to start a new world. Server administrators can still see private channels.")

    async def choose(self, interaction: discord.Interaction) -> None:
        if await self.authorize(interaction):
            await self.cog.show_dashboard(interaction, self.selector.values[0])

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            self.page = max(0, self.page - 1)
            self.rebuild()
            await self.refresh(interaction, self.embed())

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            self.page += 1
            self.rebuild()
            await self.refresh(interaction, self.embed())


class Dashboard(OwnerView):
    def __init__(self, cog: ChronicleCog, owner_id: int, session_id: str):
        super().__init__(owner_id)
        self.cog, self.session_id = cog, int(session_id)
        self.busy = False
        self.target_archived = True

    def embed(self, session: dict[str, Any]) -> discord.Embed:
        counts = {kind: len(self.cog.store.list_entries(self.session_id, self.owner_id, kind)) for kind in KINDS}
        self.target_archived = not bool(session.get("archived"))
        self.archive.label = "Resume adventure" if session.get("archived") else "Archive adventure"
        embed = card(f"✦ {session.get('title', 'Chronicle')}", f"{'📚 Archived' if session.get('archived') else '🟢 Active'} · Chronicle #{self.session_id}\n<# {session['channel_id']}>".replace("<# ", "<#") + "\n\n" + clipped(session.get("scenario"), 1600), gold=True)
        embed.add_field(name="Storyteller", value=clipped(session.get("persona"), 500) or "Your game master", inline=False)
        for kind, (emoji, label) in KINDS.items():
            embed.add_field(name=f"{emoji} {label}", value=str(counts[kind]), inline=True)
        embed.add_field(name="Play your next move", value="Write in your adventure channel. Journal entries help preserve continuity. Archiving pauses narration and keeps everything.", inline=False)
        return embed

    async def open_journal(self, interaction: discord.Interaction, kind: str) -> None:
        if await self.authorize(interaction):
            await self.cog.show_journal(interaction, self.session_id, kind)

    @discord.ui.button(label="Inventory", emoji="🎒", style=discord.ButtonStyle.primary, row=0)
    async def inventory(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.open_journal(interaction, "item")

    @discord.ui.button(label="World facts", emoji="📜", style=discord.ButtonStyle.primary, row=0)
    async def facts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.open_journal(interaction, "fact")

    @discord.ui.button(label="Quests", emoji="🧭", style=discord.ButtonStyle.primary, row=0)
    async def quests(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.open_journal(interaction, "quest")

    @discord.ui.button(label="Notes", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def notes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.open_journal(interaction, "note")

    @discord.ui.button(label="Archive adventure", emoji="📚", style=discord.ButtonStyle.secondary, row=1)
    async def archive(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorize(interaction):
            return
        if self.busy:
            await private(interaction, "This adventure is already being updated.")
            return
        self.busy = True
        try:
            session = self.cog.owned_session(self.session_id, self.owner_id)
            if not session:
                await private(interaction, "That adventure is no longer available.")
                return
            # Capture desired state before yielding. Repeated clicks cannot undo it.
            desired = self.target_archived
            if not await self.finish(interaction):
                return
            await self.cog.change_archive(interaction, self.session_id, desired)
            self.stop()
        finally:
            self.busy = False

    @discord.ui.button(label="Export", emoji="💾", style=discord.ButtonStyle.secondary, row=1)
    async def export(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            await self.cog.export_adventure(interaction, self.session_id)


class EntryModal(OwnerModal):
    def __init__(self, cog: ChronicleCog, owner_id: int, session_id: str, kind: str, entry: dict[str, Any] | None = None):
        super().__init__(owner_id, title=f"{'Edit' if entry else 'Add'} · {KINDS[kind][1]}")
        self.cog, self.session_id, self.kind, self.entry = cog, int(session_id), kind, entry
        self.name = discord.ui.TextInput(label="Name", default=(entry or {}).get("name", ""), max_length=100)
        self.content = discord.ui.TextInput(label="Details", default=(entry or {}).get("content", ""), style=discord.TextStyle.paragraph, max_length=2000)
        self.add_item(self.name)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.claim(interaction):
            return
        try:
            name = required_text(str(self.name), "Name", 100)
            content = required_text(str(self.content), "Details", 2000)
        except ValueError as error:
            await private(interaction, str(error))
            return
        if not await deferred(interaction):
            return
        async with self.cog.session_lock(self.session_id):
            session = self.cog.owned_session(self.session_id, self.owner_id)
            if not session or session.get("archived"):
                await private(interaction, "This adventure is missing or archived. Resume it before changing its journal.")
                return
            if self.entry:
                entries = self.cog.store.list_entries(self.session_id, self.owner_id, self.kind)
                current = next((item for item in entries if str(item["id"]) == str(self.entry["id"])), None)
                if not current:
                    await private(interaction, "This entry was removed while your form was open. Reopen the journal.")
                    return
                # Avoid silently overwriting another form's newer edit.
                if current.get("name") != self.entry.get("name") or current.get("content") != self.entry.get("content"):
                    await private(interaction, "This entry changed while your form was open. Reopen it before editing.")
                    return
                self.cog.store.update_entry(self.session_id, self.owner_id, self.entry["id"], name, content)
            else:
                self.cog.store.add_entry(self.session_id, self.owner_id, self.kind, name, content)
        await self.cog.show_journal(interaction, self.session_id, self.kind)


class DeleteEntryView(OwnerView):
    def __init__(self, cog: ChronicleCog, owner_id: int, session_id: str, entry: dict[str, Any]):
        super().__init__(owner_id)
        self.cog, self.session_id, self.entry, self.used = cog, int(session_id), entry, False

    @discord.ui.button(label="Delete entry", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorize(interaction):
            return
        if self.used:
            await private(interaction, "This deletion has already been handled.")
            return
        self.used = True
        if not await self.finish(interaction):
            return
        async with self.cog.session_lock(self.session_id):
            session = self.cog.owned_session(self.session_id, self.owner_id)
            if not session or session.get("archived"):
                await private(interaction, "Resume this adventure before changing its journal.")
                return
            current = next((e for e in self.cog.store.list_entries(self.session_id, self.owner_id) if str(e["id"]) == str(self.entry["id"])), None)
            if current and (current.get("name") != self.entry.get("name") or current.get("content") != self.entry.get("content")):
                await private(interaction, "This entry changed. Review the latest version before deleting it.")
                return
            if current:
                self.cog.store.delete_entry(self.session_id, self.owner_id, self.entry["id"])
        self.stop()
        await self.cog.show_journal(interaction, self.session_id, self.entry["kind"])

    @discord.ui.button(label="Keep entry", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            self.used = True
            if not await self.finish(interaction):
                return
            await private(interaction, "Kept. Nothing changed.")


class JournalView(OwnerView):
    def __init__(self, cog: ChronicleCog, owner_id: int, session_id: str, kind: str):
        super().__init__(owner_id)
        self.cog, self.session_id, self.kind = cog, int(session_id), kind
        self.page, self.selected_id = 0, None
        self.selector = discord.ui.Select(placeholder="Select an entry to edit or remove", row=0)
        self.selector.callback = self.select_entry
        self.add_item(self.selector)
        self.rebuild()

    def rebuild(self) -> None:
        self.entries = self.cog.store.list_entries(self.session_id, self.owner_id, self.kind)
        self.page = min(self.page, max(0, (len(self.entries) - 1) // PAGE_SIZE))
        self.batch = self.entries[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        if self.selected_id not in {str(item["id"]) for item in self.batch}:
            self.selected_id = None
        self.selector.options = [discord.SelectOption(label=clipped(item["name"], 100), value=str(item["id"]), description=clipped(item["content"], 100), default=str(item["id"]) == self.selected_id) for item in self.batch] or [discord.SelectOption(label="Your journal is waiting", value="empty")]
        self.selector.disabled = not bool(self.batch)
        self.previous.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * PAGE_SIZE >= len(self.entries)
        self.detail.disabled = self.selected_id is None
        self.edit.disabled = self.selected_id is None
        self.delete.disabled = self.selected_id is None

    def embed(self) -> discord.Embed:
        emoji, label = KINDS[self.kind]
        embed = card(f"{emoji} {label}", f"Chronicle #{self.session_id} · {len(self.entries)} entries · Page {self.page + 1}/{max(1, (len(self.entries) + PAGE_SIZE - 1) // PAGE_SIZE)}\n\n" + ("Saved for recall; relevant and recent entries are included within the model context limit." if self.entries else "Add the items, truths, or objectives you want your world to remember."))
        for entry in self.batch:
            prefix = "▸ " if str(entry["id"]) == self.selected_id else ""
            embed.add_field(name=clipped(f"{prefix}{entry['name']} · #{entry['id']}", 256), value=clipped(entry["content"], 950) or "—", inline=False)
        return embed

    def selected(self) -> dict[str, Any] | None:
        return next((entry for entry in self.entries if str(entry["id"]) == self.selected_id), None)

    async def select_entry(self, interaction: discord.Interaction) -> None:
        if await self.authorize(interaction):
            self.selected_id = self.selector.values[0]
            self.rebuild()
            await self.refresh(interaction, self.embed())

    @discord.ui.button(label="Previous", row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            self.page = max(0, self.page - 1)
            self.rebuild()
            await self.refresh(interaction, self.embed())

    @discord.ui.button(label="Next", row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            self.page += 1
            self.rebuild()
            await self.refresh(interaction, self.embed())

    @discord.ui.button(label="Read entry", emoji="🔎", row=1)
    async def detail(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorize(interaction):
            return
        self.rebuild()
        entry = self.selected()
        if not entry:
            await private(interaction, "Select an entry on this page first.")
            return
        for index, text in enumerate(split_story(entry["content"], 3500), start=1):
            await private(interaction, embed=card(f"{entry['name']} · #{entry['id']} · {index}", text))

    @discord.ui.button(label="Add", emoji="➕", style=discord.ButtonStyle.success, row=2)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            await interaction.response.send_modal(EntryModal(self.cog, self.owner_id, self.session_id, self.kind))

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary, row=2)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorize(interaction):
            return
        self.rebuild()
        entry = self.selected()
        if not entry:
            await private(interaction, "Select an entry on this page first.")
            return
        await interaction.response.send_modal(EntryModal(self.cog, self.owner_id, self.session_id, self.kind, dict(entry)))

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorize(interaction):
            return
        self.rebuild()
        entry = self.selected()
        if not entry:
            await private(interaction, "Select an entry on this page first.")
            return
        view = DeleteEntryView(self.cog, self.owner_id, self.session_id, dict(entry))
        await view.present(interaction, card("Remove this journal entry?", f"**{clipped(entry['name'], 100)}**\n{clipped(entry['content'], 1600)}\n\nThis removes it from the storyteller's journal."))

    @discord.ui.button(label="Dashboard", emoji="📖", row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            await self.cog.show_dashboard(interaction, self.session_id)


class ImageApproval(OwnerView):
    def __init__(self, cog: ChronicleCog, owner_id: int, prompt: str, model: str):
        super().__init__(owner_id)
        self.cog, self.prompt, self.model, self.used = cog, prompt, model, False

    @discord.ui.button(label="Approve paid generation", emoji="🎨", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.authorize(interaction):
            return
        if self.used:
            await private(interaction, "This request was already handled. Use /imagine for another image.")
            return
        # Set before the first await: rapid double clicks never issue duplicate calls.
        self.used = True
        if not await self.finish(interaction):
            return
        settings = self.cog.store.get_settings(self.owner_id) or {}
        if not settings.get("api_key"):
            await private(interaction, "Add your OpenRouter key with /setup first.")
            return
        if settings.get("image_model") != self.model:
            await private(interaction, "Your image model changed. Run /imagine again to review the new model before spending.")
            return
        try:
            image = await self.cog.engine.image(self.owner_id, self.prompt, model=self.model)
            if not isinstance(image, bytes) or not image:
                raise ValueError("invalid image result")
            # A PNG or JPEG is determined by bytes; no model-controlled filenames.
            if image.startswith(b"\xff\xd8\xff"):
                extension = "jpg"
            elif image.startswith((b"GIF87a", b"GIF89a")):
                extension = "gif"
            elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
                extension = "webp"
            elif image.startswith(b"\x89PNG\r\n\x1a\n"):
                extension = "png"
            else:
                raise ValueError("Unsupported image signature")
            file_limit = getattr(interaction, "filesize_limit", 10_000_000) or 10_000_000
            if len(image) > file_limit:
                await private(interaction, "The image was generated but exceeds Discord's upload limit. It cannot be attached here; don't retry unless you want another paid generation.")
                return
            await private(interaction, "🎨 Your scene", file=discord.File(io.BytesIO(image), filename=f"chronicle-scene.{extension}"))
        except EngineError as error:
            await private(interaction, str(error))
        except Exception:
            await private(interaction, "Image generation or delivery failed. Check your selected model and OpenRouter account. No automatic retry was made; the provider may have charged for a completed request.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.authorize(interaction):
            self.used = True
            if not await self.finish(interaction):
                return
            await private(interaction, "Cancelled. No image request was sent.")


class ReplyModal(OwnerModal):
    def __init__(self, cog: ChronicleCog, owner_id: int, session_id: str):
        super().__init__(owner_id, title="Correct the last story reply")
        self.cog, self.session_id = cog, int(session_id)
        self.snapshot = cog.store.history(self.session_id, owner_id, limit=1)
        self.snapshot_revision = cog.store.get_session(self.session_id, owner_id)["updated_at"]
        self.content = discord.ui.TextInput(label="Replacement · updates saved story memory", style=discord.TextStyle.paragraph, max_length=4000)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.claim(interaction):
            return
        try:
            text = required_text(str(self.content), "Reply", 4000)
        except ValueError as error:
            await private(interaction, str(error))
            return
        await deferred(interaction)
        async with self.cog.session_lock(self.session_id):
            session = self.cog.owned_session(self.session_id, self.owner_id)
            if not session or session.get("archived"):
                await private(interaction, "Resume this adventure before correcting its story.")
                return
            if session["updated_at"] != self.snapshot_revision or self.cog.store.history(self.session_id, self.owner_id, limit=1) != self.snapshot:
                await private(interaction, "This adventure changed while your form was open. Open a new correction form for the latest reply.")
                return
            if not self.snapshot:
                await private(interaction, "There isn’t a storyteller reply to correct yet.")
                return
            result = self.cog.store.edit_last_reply(self.session_id, self.owner_id, text)
            if result is False:
                await private(interaction, "There isn't a storyteller reply to correct yet.")
                return
        await private(interaction, "Updated the last reply in stored story memory. Previous Discord messages are unchanged; the next turn uses your correction.")


class ChronicleCog(commands.Cog):
    session = app_commands.Group(name="session", description="Create, resume, and manage your chronicles", guild_only=True)
    journal = app_commands.Group(name="journal", description="Manage inventory, world facts, and quests", guild_only=True)

    def __init__(self, bot: commands.Bot, store: Any, engine: Any, client: Any):
        self.bot, self.store, self.engine, self.client = bot, store, engine, client
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._create_locks: dict[int, asyncio.Lock] = {}
        self._seen_messages: OrderedDict[int, None] = OrderedDict()

    def form_settings(self, owner_id: int) -> dict[str, Any]:
        try:
            return self.store.get_settings(owner_id) or {}
        except ValueError:
            # A lost encryption key must not prevent replacing a stored API key.
            return {}

    def session_lock(self, session_id: Any) -> asyncio.Lock:
        return self._session_locks.setdefault(str(session_id), asyncio.Lock())

    def owned_session(self, session_id: Any, owner_id: int) -> dict[str, Any] | None:
        try:
            return self.store.get_session(int(session_id), owner_id)
        except (LookupError, PermissionError, ValueError, TypeError):
            return None

    async def resolve_session(self, interaction: discord.Interaction, session_id: str | None) -> dict[str, Any] | None:
        if not interaction.guild_id:
            await private(interaction, "Use this command in the server where you play.")
            return None
        session = self.owned_session(session_id, interaction.user.id) if session_id else self.store.get_channel_session(interaction.channel_id, interaction.user.id)
        if not session and not session_id:
            session = next((item for item in self.store.list_sessions(interaction.user.id, interaction.guild_id) if item["channel_id"] == interaction.channel_id), None)
        if not session or session.get("guild_id") != interaction.guild_id:
            await private(interaction, "Choose one of your adventures with /session library, or use this command in its channel.")
            return None
        return session

    async def show_dashboard(self, interaction: discord.Interaction, session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if session:
            view = Dashboard(self, interaction.user.id, str(session["id"]))
            await view.present(interaction, view.embed(session))

    async def show_journal(self, interaction: discord.Interaction, session_id: str | None, kind: str) -> None:
        session = await self.resolve_session(interaction, session_id)
        if session:
            view = JournalView(self, interaction.user.id, str(session["id"]), kind)
            await view.present(interaction, view.embed())

    async def create_adventure(self, interaction: discord.Interaction, title: str, persona: str, scenario: str) -> None:
        try:
            title = required_text(title, "Title", 80)
            persona = required_text(persona, "Persona", 1000)
            scenario = required_text(scenario, "Scenario", 3000)
        except ValueError as error:
            await private(interaction, str(error))
            return
        guild = interaction.guild
        if guild is None or guild.me is None:
            await private(interaction, "Create adventures from a server where Chronicle is installed.")
            return
        required_permissions = ("manage_channels", "view_channel", "send_messages", "read_message_history", "embed_links", "attach_files")
        if not all(getattr(guild.me.guild_permissions, name, False) for name in required_permissions):
            await private(interaction, "I need Manage Channels, View Channels, Send Messages, Read Message History, Embed Links, and Attach Files to create your adventure. Ask a server administrator to check my permissions.")
            return
        settings = self.store.get_settings(interaction.user.id) or {}
        if not settings.get("api_key"):
            await private(interaction, "Run /setup to add your personal OpenRouter key before creating an adventure.")
            return
        if not await deferred(interaction):
            return
        lock = self._create_locks.setdefault(interaction.user.id, asyncio.Lock())
        if lock.locked():
            await private(interaction, "Your previous adventure is still being created. Give it a moment.")
            return
        async with lock:
            if sum(not item["archived"] for item in self.store.list_sessions(interaction.user.id, guild.id)) >= 20:
                await private(interaction, "You have reached the limit of 20 active adventures in this server. Archive an older one with /session library, then start a new world.")
                return
            channel = None
            # Explicit overwrites; no category, no copied story-controlled permissions.
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True, attach_files=True),
            }
            slug = re.sub(r"[^a-z0-9-]", "-", title.lower()).strip("-")[:65] or "adventure"
            try:
                channel = await guild.create_text_channel(name=f"chronicle-{slug}", overwrites=overwrites, topic="Private Chronicle adventure. Server administrators retain access.", reason="Owner-requested Chronicle adventure")
                try:
                    session = self.store.create_session(interaction.user.id, guild.id, channel.id, title, persona, scenario)
                except Exception:
                    try:
                        await channel.delete(reason="Chronicle could not save this new adventure")
                    except discord.HTTPException:
                        await private(interaction, "The adventure couldn't be saved, and its empty private channel couldn't be removed. Ask a server administrator to remove it.")
                        return
                    await private(interaction, "The adventure couldn't be saved. Its empty channel was removed. Please try again.")
                    return
                welcome = card(f"✦ {title}", clipped(scenario, 3000), gold=True)
                welcome.add_field(name="Your adventure begins here", value="Write your next move to continue. /session dashboard opens your inventory, facts, and quests.\n\nStory turns use your personal OpenRouter balance. Server administrators can see this channel.", inline=False)
                try:
                    await channel.send(embed=welcome, allowed_mentions=NO_MENTIONS)
                except discord.HTTPException:
                    await private(interaction, f"Your adventure is saved in <#{channel.id}>, but I couldn't post the welcome. Check my Send Messages and Embed Links permissions.")
                    return
                await self.show_dashboard(interaction, str(session["id"]))
            except discord.Forbidden:
                await private(interaction, "Discord refused channel creation. Ask an administrator to check my Manage Channels permission.")
            except discord.HTTPException:
                await private(interaction, "Discord couldn't create the channel. Check the server's channel limit and try again.")

    def channel_is_private(self, channel: Any, owner_id: int) -> bool:
        """Fail closed after administrator changes; never trust fictional state."""
        guild = getattr(channel, "guild", None)
        if guild is None or guild.me is None:
            return False
        overwrites = getattr(channel, "overwrites", {})
        everyone = overwrites.get(guild.default_role)
        if everyone is None or everyone.view_channel is not False:
            return False
        permitted = {owner_id, guild.me.id}
        for target, permissions in overwrites.items():
            if permissions.view_channel is True and target.id not in permitted:
                return False
        return True

    async def change_archive(self, interaction: discord.Interaction, session_id: str, archived: bool) -> None:
        if not await deferred(interaction):
            return
        async with self._create_locks.setdefault(interaction.user.id, asyncio.Lock()):
            async with self.session_lock(session_id):
                session = await self.resolve_session(interaction, session_id)
                if not session:
                    return
                if not archived and (interaction.guild is None or interaction.guild.get_channel(session["channel_id"]) is None):
                    await private(interaction, "This adventure's channel is missing. Its story is still saved and can be exported with /export. Create a new adventure to keep playing.")
                    return
                if not archived and not self.channel_is_private(interaction.guild.get_channel(session["channel_id"]), interaction.user.id):
                    await private(interaction, "This channel is no longer private. Ask a server administrator to restore access to only you and the bot before resuming.")
                    return
                if not archived and session["archived"] and sum(not item["archived"] for item in self.store.list_sessions(interaction.user.id, interaction.guild_id)) >= 20:
                    await private(interaction, "Archive one of your 20 active adventures before resuming this one.")
                    return
                self.store.set_archived(session["id"], interaction.user.id, archived)
        await self.show_dashboard(interaction, session_id)

    async def export_adventure(self, interaction: discord.Interaction, session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if not session:
            return
        if not await deferred(interaction):
            return
        payload = self.store.export_session(session["id"], interaction.user.id)
        # Defence in depth: exclude credential-shaped metadata even if storage grows.
        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items() if str(key).lower() not in {"api_key", "key", "token", "authorization", "settings", "encrypted_key", "key_ciphertext", "api_key_encrypted"}}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value
        data = json.dumps(redact(payload), ensure_ascii=False, indent=2).encode("utf-8")
        if len(data) > (getattr(interaction, "filesize_limit", 10_000_000) or 10_000_000):
            await private(interaction, "This adventure is larger than Discord's attachment limit. Ask the bot operator for a private database export.")
            return
        await private(interaction, "Your chronicle, journal, and saved story memory. Keep this file private.", file=discord.File(io.BytesIO(data), filename=f"chronicle-{session['id']}.json"))

    @app_commands.command(name="help", description="Your guide to Chronicle")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = card("✦ CHRONICLE", "**A private stage for worlds worth remembering.**\nCreate a story, shape your character, and keep the important details close.", gold=True)
        embed.add_field(name="01 · Choose your storyteller", value="/setup adds your personal OpenRouter key. Discord transports this modal submission; it is never echoed in chat. /model selects story and optional image models. /forget-key removes your stored key.", inline=False)
        embed.add_field(name="02 · Begin or return", value="/session create opens a new adventure. /session library returns to any saved world. Write in its private channel to play; your selected model uses your OpenRouter balance.", inline=False)
        embed.add_field(name="03 · Make it yours", value="/session dashboard opens your inventory, facts, and quests. /journal provides direct editing. /roll handles dice. /imagine asks for explicit approval before any paid image request.", inline=False)
        embed.add_field(name="04 · Keep your story", value="/session archive pauses narration without deleting the channel. /session resume continues it. /export downloads your story without credentials. /session edit-reply corrects stored continuity.", inline=False)
        embed.add_field(name="Privacy & limits", value="Only you and the bot are granted channel access; server administrators retain access. Discord and the bot operator process your data; OpenRouter and the selected model provider receive story context. Management panels are private and expire after 5 minutes. Open a new panel to continue.", inline=False)
        await private(interaction, embed=embed)

    @app_commands.command(name="setup", description="Set your own OpenRouter key privately; Discord transports the submission")
    async def setup_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SetupModal(self, interaction.user.id))

    @app_commands.command(name="forget-key", description="Remove your stored OpenRouter API key")
    async def forget_key(self, interaction: discord.Interaction) -> None:
        self.store.forget_key(interaction.user.id)
        await private(interaction, "Your stored key was removed. Your adventures and model choices are kept. Already-running requests may finish; deleting a stored key does not revoke it at OpenRouter.")

    @app_commands.command(name="model", description="Choose your story model and optional image model")
    async def model_command(self, interaction: discord.Interaction) -> None:
        view = ModelView(self, interaction.user.id)
        await view.present(interaction, view.embed())

    @session.command(name="create", description="Create a private adventure with its own character and scenario")
    async def session_create(self, interaction: discord.Interaction, preset: Literal["custom", "cozy-fantasy", "neon-noir", "space-opera"] = "custom") -> None:
        await interaction.response.send_modal(SessionModal(self, interaction.user.id, preset))

    @session.command(name="library", description="Browse all of your saved adventures in this server")
    async def session_library(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await private(interaction, "Use the library in a server.")
            return
        view = SessionLibrary(self, interaction.user.id, interaction.guild_id)
        await view.present(interaction, view.embed())

    @session.command(name="dashboard", description="Open your adventure's journal and controls")
    async def session_dashboard(self, interaction: discord.Interaction, session_id: str | None = None) -> None:
        await self.show_dashboard(interaction, session_id)

    @session.command(name="archive", description="Pause an adventure while keeping its channel and memories")
    async def session_archive(self, interaction: discord.Interaction, session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if session:
            await self.change_archive(interaction, str(session["id"]), True)

    @session.command(name="resume", description="Resume an archived adventure")
    async def session_resume(self, interaction: discord.Interaction, session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if session:
            await self.change_archive(interaction, str(session["id"]), False)

    @session.command(name="edit-reply", description="Correct the last storyteller reply in saved story memory")
    async def session_edit(self, interaction: discord.Interaction, session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if session:
            await interaction.response.send_modal(ReplyModal(self, interaction.user.id, str(session["id"])))

    @journal.command(name="show", description="Browse a section of your adventure journal")
    async def journal_show(self, interaction: discord.Interaction, kind: Literal["item", "fact", "quest", "note"] = "item", session_id: str | None = None) -> None:
        await self.show_journal(interaction, session_id, kind)

    @journal.command(name="add", description="Add an item, world fact, or quest")
    async def journal_add(self, interaction: discord.Interaction, kind: Literal["item", "fact", "quest", "note"], session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if session:
            await interaction.response.send_modal(EntryModal(self, interaction.user.id, str(session["id"]), kind))

    @journal.command(name="edit", description="Edit a journal entry by its displayed ID")
    async def journal_edit(self, interaction: discord.Interaction, entry_id: str, session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if not session:
            return
        entry = next((e for e in self.store.list_entries(session["id"], interaction.user.id) if str(e["id"]) == entry_id), None)
        if entry:
            await interaction.response.send_modal(EntryModal(self, interaction.user.id, str(session["id"]), entry["kind"], entry))
        else:
            await private(interaction, "That entry isn't in this adventure. Find its ID with /journal show.")

    @journal.command(name="delete", description="Review and remove a journal entry")
    async def journal_delete(self, interaction: discord.Interaction, entry_id: str, session_id: str | None = None) -> None:
        session = await self.resolve_session(interaction, session_id)
        if not session:
            return
        entry = next((e for e in self.store.list_entries(session["id"], interaction.user.id) if str(e["id"]) == entry_id), None)
        if not entry:
            await private(interaction, "That entry isn't in this adventure.")
            return
        view = DeleteEntryView(self, interaction.user.id, str(session["id"]), entry)
        await view.present(interaction, card("Remove this journal entry?", f"**{clipped(entry['name'], 100)}**\n{clipped(entry['content'], 1600)}"))

    @app_commands.command(name="imagine", description="Preview a paid image request and approve it explicitly")
    async def imagine(self, interaction: discord.Interaction, prompt: app_commands.Range[str, 1, 2000]) -> None:
        settings = self.store.get_settings(interaction.user.id) or {}
        if not settings.get("api_key"):
            await private(interaction, "Set up your personal OpenRouter key with /setup first.")
            return
        model = settings.get("image_model")
        if not model:
            await private(interaction, "Images are disabled. Use /model → Custom model / images to choose an image-capable OpenRouter model.")
            return
        prompt = prompt.strip()
        if not prompt:
            await private(interaction, "Describe the scene you want to create.")
            return
        view = ImageApproval(self, interaction.user.id, prompt, model)
        await view.present(interaction, card("🎨 Bring a scene to life", f"**Model:** `{clipped(model, 200)}`\n\n{clipped(prompt, 2000)}\n\n**This is a paid request using your OpenRouter balance.** The amount depends on your provider and model; Chronicle cannot quote it here. Only this prompt is sent. Approve once to generate, or cancel. No image API call has been made."))

    @app_commands.command(name="roll", description="Roll bounded dice, for example 2d6+3")
    async def roll(self, interaction: discord.Interaction, dice: str = "1d20") -> None:
        try:
            values, total, normalized = roll_dice(dice)
        except ValueError as error:
            await private(interaction, str(error))
            return
        await private(interaction, embed=card(f"🎲 {normalized} → {total}", "Rolls: " + " · ".join(str(value) for value in values), gold=True))

    @app_commands.command(name="export", description="Download one of your adventures privately, without credentials")
    async def export(self, interaction: discord.Interaction, session_id: str | None = None) -> None:
        await self.export_adventure(interaction, session_id)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await private(interaction, "That command couldn't finish. Check your setup and permissions, then try again. No secrets are included in this error.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or message.guild is None or not message.content.strip():
            return
        session = self.store.get_channel_session(message.channel.id, message.author.id)
        if not session:
            return
        session_id = session["id"]
        async with self.session_lock(session_id):
            if message.id in self._seen_messages:
                return
            self._seen_messages[message.id] = None
            while len(self._seen_messages) > 2000:
                self._seen_messages.popitem(last=False)
            session = self.owned_session(session_id, message.author.id)
            if not session or session.get("archived"):
                return
            if not self.channel_is_private(message.channel, message.author.id):
                await message.channel.send("This adventure’s channel permissions changed. Narration is paused until an administrator restores private access.", allowed_mentions=NO_MENTIONS)
                return
            if len(message.content) > MAX_INPUT_CHARS:
                await message.channel.send(f"That move is too long. Keep it to {MAX_INPUT_CHARS:,} characters or fewer.", allowed_mentions=NO_MENTIONS)
                return
            try:
                async with message.channel.typing():
                    result = await self.engine.turn(session_id, message.author.id, message.content, request_id=str(message.id))
                latest = self.owned_session(session_id, message.author.id)
                if not latest or latest.get("archived"):
                    return
                for chunk in split_story(result):
                    if not self.channel_is_private(message.channel, message.author.id):
                        return
                    await message.channel.send(chunk, allowed_mentions=NO_MENTIONS)
            except EngineError as error:
                if not self.channel_is_private(message.channel, message.author.id):
                    return
                try:
                    await message.channel.send(str(error), allowed_mentions=NO_MENTIONS)
                except discord.HTTPException:
                    pass
            except Exception:
                # Deliberately avoid exception objects in logs, including chained HTTP errors.
                try:
                    await message.channel.send("The storyteller couldn't finish this turn. Check /setup, your model, and your OpenRouter balance. No automatic retry was made.", allowed_mentions=NO_MENTIONS)
                except discord.HTTPException:
                    pass


async def setup(bot: commands.Bot) -> None:
    """Extension hook; the launcher can also construct ChronicleCog directly."""
    await bot.add_cog(ChronicleCog(bot, bot.store, bot.engine, bot.router))
