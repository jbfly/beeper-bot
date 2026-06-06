# beeper-bot

Local-first Beeper memory and assistant bot.

## Purpose

Run a private agent behind a Beeper control chat. The agent indexes selected Beeper chats into a local SQLite database, searches them, and answers questions with cited evidence. The default design keeps message data and model inference local.

## Initial scope

- sync selected chats from the Beeper Desktop local API
- store messages in a local SQLite archive
- support exact and fuzzy retrieval for dates, addresses, names, and discussion summaries
- answer through a private Beeper control chat
- use a local `llama.cpp` server for synthesis

See:
- `docs/technical-plan.md`
- `docs/implementation-plan.md`

## Benchmarking

Run the starter benchmark suite:

- `beeper-bot --config ~/.config/beeper-bot/config.toml eval --suite eval/starter.json`
- add `--json` for machine-readable output
- add `--output state/eval-latest.json` to save a run
