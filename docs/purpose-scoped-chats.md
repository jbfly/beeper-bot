# Purpose-scoped control chats (how to plug a project into beeper-bot)

*Shipped 2026-07-07. This is the integration guide other projects (e.g.
`~/git/music-library-project`) should read to wire themselves into beeper-bot.*

beeper-bot runs a serve loop on **venus** that polls one or more Beeper "control
chats" and reacts to commands. Historically there was one control chat; now you
can add **purpose-scoped** ones — a dedicated chat per project — each with its
own cursor, conversational memory, persona, and command set.

Two integration directions:

1. **Inbound** — you type a command in the chat (e.g. `/music <issue>`), the bot
   handles it.
2. **Outbound** — your project (any machine) posts a message into the chat with
   `beeper-bot notify` — a download finished, a fixer run completed, an alert.

## The moving parts

- **Config:** `~/.config/beeper-bot/config.toml` on venus. `beeper.control_chat_id`
  is the implicit `main` chat; every `[control_chats.<name>]` is polled alongside
  it.
  ```toml
  [control_chats.music]
  chat_id = "!DMcPBnyaHHDxfBzmiz:beeper.com"
  allowed_commands = ["music", "status", "help"]   # empty = all commands
  # persona = "..."   # a system directive prepended to free-text /ask answers
  ```
- **Minting a chat (no Beeper GUI needed):**
  ```
  beeper-bot matrix-create-chat "beeper-bot · <name>" --topic "..."
  ```
  prints a `room_id`. Put it under a `[control_chats.<name>]` section and restart
  `beeper-bot.service` (user unit on venus). The room shows up as a chat in your
  Beeper apps.
- **Restart to load config:** config is read at startup.
  ```
  systemctl --user restart beeper-bot.service
  ```
  (needs `XDG_RUNTIME_DIR=/run/user/1000` +
  `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus` in a non-login shell.)

## Outbound: `beeper-bot notify`

```
beeper-bot notify "<text>" [--chat <name>]      # default --chat main
```

Queues a fire-and-forget message in the archive's `outbound_queue`; the running
serve loop delivers it to that control chat and marks it sent (survives the bot
being briefly down). From another machine:

```
ssh venus beeper-bot notify "library fix run: 12 covers, 3 needs-review" --chat music
```

## The `/music` chat (live)

- **Chat:** `beeper-bot · music` = `!DMcPBnyaHHDxfBzmiz:beeper.com`
- **allowed_commands:** `music`, `status`, `help` (plain chatter is ignored here,
  so it stays a clean control channel).
- **Inbound `/music <issue>`:** already implemented — `bridge._handle_music`
  shells out to `music-library-project/scripts/fixer_capture.py <issue>`, which
  snapshots what's playing now (Navidrome `getNowPlaying`) and appends to the
  fixer queue. That capture logic lives in **music-library-project**, not here —
  beeper-bot is just the router.
- **Outbound:** have the fixer/pipeline post status back with
  `beeper-bot notify --chat music "..."` (e.g. when a queued issue is resolved,
  or a nightly run finishes). Keep it to summaries; it's a chat, not a log.

### For the music-library-project session

You don't need to touch beeper-bot to integrate — the chat and command already
exist. Your side is:
- Keep `scripts/fixer_capture.py <issue>` as the capture entry point (stdout is
  echoed back into the chat as the reply, so make it a one-line human summary).
- To push results/notifications into the music chat, call
  `ssh venus ~/git/beeper-bot/.venv312/bin/beeper-bot notify "<summary>" --chat music`
  (or run it locally on venus). No new API, no bot code.
- If you later want free-form Q&A in the music chat (e.g. "what did I flag about
  the Dead Can Dance covers?"), add `"ask"` to `allowed_commands` and optionally a
  `persona`.
