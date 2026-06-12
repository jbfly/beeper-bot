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

Current deterministic baseline on the live local Gemma stack (post de-shim;
all control-memory and ladder cases now genuinely reach the model, see
`docs/control-chat-memory-and-eval-plan.md` §4.4):

- `starter`: `6/8` scored passed (known borderline: `anna_owed_john`, `addy_and_i_may18`)
- `core`: `18/20` scored passed (known borderline: `anna_owed_john`, `pensao_amor_address`)
- `slice`: `14/14` scored passed
- `control-routing`: `3/3` passed (deterministic product routing, no LLM)
- `control-memory`: `8/8` scored passed through the model path
- `context-ladder`: `4/9` scored passed; families degrade at the medium/long
  rungs, which is the real context-pressure signal the suite exists to
  measure (the earlier `9/9` was scored against deterministic shims)

`anna_owed_john` fails because FTS has no stemming (`owe` does not match
`owes` in a third-party message); switching `message_fts` to a porter
tokenizer is the identified fix and needs a schema migration.

Use `--deterministic` for comparison runs. That pins answer and planner temperatures to `0.0` unless explicitly overridden.
