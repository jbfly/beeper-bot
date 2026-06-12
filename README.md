# beeper-bot

Local-first Beeper memory and assistant bot.

## Purpose

Run a private agent behind a Beeper control chat. The agent indexes selected Beeper chats into a local SQLite database, searches them, and answers questions with cited evidence. The default design keeps message data and model inference local.

## Initial scope

- sync selected chats from the Beeper Desktop local API
- store messages in a local SQLite archive
- support exact and fuzzy retrieval for dates, addresses, names, and discussion summaries
- answer through a private Beeper control chat
- use the local `ai-ops` `llama.cpp` proxy on `127.0.0.1:8090` for synthesis

See:
- `docs/technical-plan.md`
- `docs/implementation-plan.md`
- `docs/control-chat-memory-and-eval-plan.md`
- `docs/remote-alpha-workflow.md`
- `docs/alpha-matrix-wake-listener.md`

## Local model endpoint

Point the bot at the local `ai-ops` proxy, not the raw backend port:

```toml
[llm]
base_url = "http://127.0.0.1:8090/v1"
model = "gemma4-google-26b-a4b-q4_0-local"
```

That proxy starts `llama-server` on demand and unloads it when idle. See `~/git/ai-ops/docs/llama-arbiter.md`.

## Benchmarking

Run the starter benchmark suite:

- `beeper-bot --config ~/.config/beeper-bot/config.toml eval --suite eval/starter.json`
- add `--json` for machine-readable output
- add `--output state/eval-latest.json` to save a run

Current deterministic baseline on the live local Gemma stack:

- `starter`: `8/8` scored passed, `2` diagnostic/info cases
- `core`: `20/20` scored passed
- `slice`: `14/14` scored passed
- `control-memory`: `3/3` scored passed (first-pass plumbing check; scored via deterministic paths, not yet a model baseline — see plan §4.4)
- `context-ladder`: `9/9` scored passed, `3` stress cases left metrics-only (same caveat)

Use `--deterministic` for comparison runs. That pins answer and planner temperatures to `0.0` unless explicitly overridden.
