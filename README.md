# Chronicle · Roleplay Nanny

**A character-driven RPG companion for Discord.** Create a private world, choose your narrator,
collect items, pin canon, track quests, and come back to the same story tomorrow.

Chronicle is a ground-up redesign of the original Roleplay Nanny prototype. It uses
**your own OpenRouter key**, durable SQLite storage, and a modern slash-command interface.
No shared API key, webhook impersonation, or external dashboard is required.

## Your next chapter

1. Run `/setup` and enter your OpenRouter key in the modal. Only you receive the response.
2. Choose a text model with `/model`.
3. Use `/session create` to create a world with a title, narrator/persona, and opening scenario.
4. Write naturally in your dedicated session channel. The narrator responds in character.
5. Open your session dashboard to browse **Inventory**, **Facts**, **Quests**, and **Notes**.
   Journal entries persist across restarts and become part of the narrator's context.
6. Archive a finished story, resume it later, or export it as JSON.

### Features

- Multiple persistent adventures per player, isolated by owner and server (up to 20 active)
- Private-by-default session channels, owner-only controls, and ephemeral settings
- Character/persona creation, editable cozy fantasy / neon noir / space opera starters, configurable models
- Filter-by-kind, paginated world journal with add/edit/delete controls
- Narration grounded in saved journal entries and recent conversation
- Explicitly approved optional image generation using a separate image-capable model
- Dice rolls, transcript export, archival, and return-to-story navigation
- Encrypted-at-rest API keys, bounded generation, safe errors, duplicate-request protection

“Memory” means stored canon plus a bounded recent context window, not infinite recall.
Save important developments as journal facts. Recall prioritizes entries matching your current
prompt, then recent entries, within a bounded context budget; very large journals are not
sent in full. The latest 24 stored messages are eligible for recent context (also capped
by character budget). The AI cannot silently edit your inventory or
canon, execute tools, change Discord permissions, or read another player's story.

## Command guide

| Command | What it does |
| --- | --- |
| `/setup` | Private key, story model, optional image model settings |
| `/forget-key` | Remove the saved OpenRouter key |
| `/model` | Curated model picker and custom model ID entry |
| `/session create` | Create a narrator and private channel; optional editable genre preset |
| `/session library` | Browse your active and archived adventures |
| `/session dashboard` | Open the current adventure’s controls |
| `/session archive` · `/session resume` | Pause or continue a saved adventure |
| `/session edit-reply` | Correct the latest reply in stored AI continuity |
| `/journal show` · `add` · `edit` · `delete` | Browse and manage persistent world entries |
| `/imagine` | Preview a prompt and explicitly approve one image request |
| `/roll` | Roll bounded dice such as `2d6+3` |
| `/export` | Download the saved session, journal, and transcript privately |
| `/help` | In-app orientation |

Management commands default to the current session channel. Use the library or an optional
`session_id` to manage another saved adventure in the same server. Editing a story reply
changes stored continuity, not historical Discord messages. Expired panels are reopened by
running the command again; cancelling an unsubmitted modal changes nothing.

## Install

Requires Python **3.11+** and a Discord application with a bot.

```sh
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the generated value in `NANNY_ENCRYPTION_KEY` and your bot token in `DISCORD_TOKEN`.
Keep the encryption key stable. Losing it makes saved OpenRouter keys unreadable; players
must then configure replacement keys. Back it up **separately** from the database.
Never send bot tokens or the server encryption key through chat.

In the [Discord Developer Portal](https://discord.com/developers/applications):

- Enable **Message Content Intent** on your bot. Chronicle reads messages only in its saved
  owner session channels. It does not need presence or server-members privileged intents.
- Install using the `bot` and `applications.commands` scopes.
- Grant **View Channels, Send Messages, Read Message History, Embed Links, Attach Files,
  and Manage Channels**. Do not grant Administrator. Channel creation fails safely if the
  required permissions are unavailable.
- Optionally set `DISCORD_GUILD_ID` for immediate slash-command registration in a test server.
  Leave it blank for global commands. Discord may take time to show new global commands.

```sh
python main.py
```

Use a fresh virtual environment when upgrading: the old `py-cord` package conflicts with
`discord.py` because both provide the `discord` module. `requirements-lock.txt` records the
exact development/test environment; normal installs use compatible dependency ranges.

## Hosting and data

Run **one bot process** per database. SQLite is a deliberately small, self-contained choice,
not a distributed queue. Use persistent local storage for `NANNY_DATABASE` (default
`data/chronicle.db`) and restrict the host account/directory to the bot operator. Do not run
multiple replicas against the same database or an unreliable network filesystem.

The database contains story transcripts and journal text in plaintext. Only API keys are
Fernet-encrypted; disk encryption and host access controls are still your responsibility.
Discord also retains messages according to its policies. Private channels remain visible to
server administrators and authorized Discord staff. If ordinary channel permissions are
changed to allow additional people, narration pauses until private access is restored. The bot operator can decrypt stored
keys while holding the encryption key. A modal is **not** a password manager or end-to-end
encryption: Discord transports the entered key. Use a dedicated, spending-limited OpenRouter
key and revoke it in OpenRouter if you suspect exposure.

For backups, stop the process before copying the database (or use SQLite's backup API).
Keep the encryption key separately, secure both backups, and test restoration. The app
creates/updates its versioned schema automatically; unsupported newer schemas are refused.

### Upgrade from the original bot

1. Stop the old bot and back up `database.db` and any desired Discord histories.
2. Create a new virtual environment and new `.env` from this release's `env.example`.
3. Use the default new database path. **Do not point Chronicle at the legacy `database.db`.**
4. Start Chronicle, run `/setup`, and recreate adventures with `/session create`.
5. Copy essential old canon into your new journal. Keep old Discord channels as an archive
   or manage them manually after checking visibility and retention needs.

The original schema saved a shared API key in its `guilds` table. Chronicle deliberately
**does not import legacy credentials or auto-adopt old public channels**. The old files and
channels are left untouched. Rotate/revoke any old shared credential and remove it from old
backups when appropriate. There is no silent import of old Discord history.

## Costs and privacy

Every normal story message you send in an active session can cause a billed OpenRouter
request. Model pricing and availability are controlled by OpenRouter/providers; a model
name or `:free` suffix is not a guarantee of future cost. Set provider-side credit limits.
Chronicle does not retry paid requests automatically after ambiguous network failures.

The selected model provider receives the scenario/persona, selected stored journal context,
recent story messages, and your current prompt. Image generation separately sends the
approved prompt to OpenRouter's image endpoint. No image generation happens automatically.
Do not put real secrets or sensitive personal information into story prompts.

`/forget-key` removes the saved API key, not your OpenRouter account, Discord messages, or
story history. Export only to locations you trust. The bot suppresses mentions in generated
messages. Story text is untrusted context; language-model prompt obedience is not a security
boundary. Isolation and mutation permissions are enforced in application code.

## Development and verification

```sh
python -m pip install -r requirements-dev.txt
ruff check .
python -m compileall -q main.py roleplay.py nanny tests
pytest -q
```

GitHub Actions runs the same checks on Python 3.11, 3.12, and 3.13. Tests use fake credentials
and mocked network responses. They exercise persistence, access control, malformed provider
responses, duplicate/concurrent turns, UI interruption/repetition, and safe failure paths.
They do **not** prove live Discord permissions, command propagation, provider billing, model
quality, or live image compatibility. See [the smoke-test checklist](docs/SMOKE_TEST.md)
before inviting players. No deployment or paid API calls are part of CI.

## Project layout

- `main.py`: validated configuration, minimal intents, lifecycle, command registration
- `nanny/discord_app.py`: slash commands, modals, views, private channel handling
- `nanny/store.py`: versioned SQLite storage, ownership checks, encrypted keys
- `nanny/engine.py`: bounded OpenRouter client and persistent story orchestration
- `tests/`: offline regression tests

The project intentionally avoids arbitrary AI tools, plugin execution, automatic item edits,
public roleplay sharing, and multiplayer billing delegation. Those require separate product
and permission designs rather than giving a model unrestricted authority.
