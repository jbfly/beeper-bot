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

Beyond archive QA, the control chat supports:
- `/catchup <chat>`: digest of a group chat since the last catch-up
- `/index <chat>`: add any Beeper chat to the archive at runtime
- on-the-fly indexing: questions that name an unindexed chat trigger a sync
- `auto_index_recent_days` config: automatically index recently active chats

See:
- `docs/technical-plan.md`
- `docs/implementation-plan.md`
- `docs/control-chat-memory-and-eval-plan.md`
- `docs/chat-coverage-plan.md`
- `docs/multimodal-memos-and-images-plan.md`
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

- `catchup`: `5/5` scored passed (real Bom Sucesso group-chat digests)

After the 43-chat archive expansion (12k messages) the 26B baseline held:
core improved to `19/20`, slice `13/14` (one borderline flake), starter
unchanged, control-memory `7/8` — the pronoun follow-up now answers
correctly from the control turn but without re-grounding in an archive
citation. Single-case run-to-run flips are expected; see the determinism
caveat in `docs/control-chat-memory-and-eval-plan.md` §4.3.1.

`anna_owed_john` fails because FTS has no stemming (`owe` does not match
`owes` in a third-party message); switching `message_fts` to a porter
tokenizer is the identified fix and needs a schema migration.

First shootout entry — Gemma 4 12B Q6_K (`state/eval-12b/`, same
deterministic settings): starter 6/8, core 18/20, slice 14/14,
control-routing 3/3, control-memory 8/8, catchup 5/5, context-ladder 3/9.
Within noise of the 26B baseline on low-pressure suites (it passes
`anna_owed_john`, fails `adriana_address`/`tom_automation_link` instead),
one rung worse under context pressure, roughly 2x slower per case (dense
12B vs the 26B's 4B-active MoE), but it adds native audio and ~5 GB more
VRAM headroom.

Use `--deterministic` for comparison runs. That pins answer and planner temperatures to `0.0` unless explicitly overridden.
