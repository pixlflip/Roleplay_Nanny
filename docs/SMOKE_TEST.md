# Live smoke-test checklist

Run in a test Discord server after the automated suite passes. This checklist requires your
own Discord bot and OpenRouter credentials. Text/image steps may incur provider charges.

## Setup and registration

- Fresh virtual environment, new database, stable encryption key; launch without errors.
- `/help` and `/session` appear; no obsolete duplicate commands after global propagation.
- Missing token/encryption key fails with a safe configuration message.
- `/setup` opens a modal, cancel leaves settings unchanged, submit responds privately.
- Run setup twice; replacing a key/model persists after restart without echoing the key.
- `/forget-key` removes access and subsequent narration asks for setup.

## Story and access controls

- Create two sessions; each opens a dedicated channel hidden from a second ordinary member.
- Owner and bot can see/send/read, server admin visibility is expected and documented.
- A second member cannot use someone else's dashboard, export, journal, or billing settings.
- Persona and scenario appear in the story; no mass mentions fire even if generated.
- Send a normal turn, another immediately, and a long turn; see useful busy/length handling.
- Archive during an active turn; verify final state and no new turns while archived.
- Resume with dashboard, restart the process, then reopen `/session`; saved state survives.
- Delete a channel manually; library navigation reports it missing rather than losing data.

## Journal and UI

- Add, edit, delete a fact, item, quest, and note; duplicates and invalid lengths are handled.
- Add enough entries for multiple pages; Next/Back stays in bounds after deletion.
- Cancel modals, repeatedly click actions, open a newer panel, let an old panel expire.
- Ask the narrator about a saved fact; verify included canon and owner isolation.
- Export includes expected session/journal/transcript, never settings/API credentials.

## Provider behavior and images

- Invalid key, unavailable model, no credits: useful safe error, no raw upstream body.
- Configure a real image-capable model, preview approval, cancel: no image request.
- Approve once, click again: a single generation; output uploads as a Discord attachment.
- Check actual provider billing, image format support, and spending limits in OpenRouter.
- Simulate temporary network failure; no invisible paid retry or duplicate journal write.

## Operational recovery

- Stop, back up database and encryption key separately, restore to a disposable directory.
- Restore has all sessions and journal entries; original key still decrypts settings.
- Interrupted in-flight requests do not automatically issue a second paid request on restart.
- Confirm data-directory permissions and log output do not expose credentials or prompts.
