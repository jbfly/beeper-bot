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
- configured `[chat_sets.<name>]`: curated multi-chat digests for places or events, e.g. Neighborhood or Sample Festival
- natural-language digests: "what is happening in Neighborhood?" routes to the same catch-up machinery
- `/index <chat>`: add any Beeper chat to the archive at runtime
- on-the-fly indexing: questions that name an unindexed chat trigger a sync
- `auto_index_recent_days` config: automatically index recently active chats

Voice memos and images become searchable text via
`beeper-bot index-media --kind voice|image` — attachments are fetched
through the Beeper Desktop API (`/v1/assets/serve`, which also decrypts),
transcribed/described by the local model in bounded chunks, and indexed
into FTS so `/ask`, `/find`, and `/catchup` see them like any other
message.

**New here? Read [`AGENTS.md`](AGENTS.md) first** — it is the authoritative
current-state map (architecture, data model, ask pipeline, config & command
reference, how to run tests, operational state). The `docs/` files below are
deeper design notes and history:

- `docs/technical-plan.md`
- `docs/implementation-plan.md`
- `docs/control-chat-memory-and-eval-plan.md`
- `docs/chat-coverage-plan.md`
- `docs/multimodal-memos-and-images-plan.md`

## Local model endpoint

Point the bot at the local `ai-ops` proxy, not the raw backend port:

```toml
[llm]
base_url = "http://127.0.0.1:8090/v1"
model = "gemma4-google-12b-q6_k-local"
```

That proxy starts `llama-server` on demand and unloads it when idle. See `~/git/ai-ops/docs/llama-arbiter.md`.

## Benchmarking

Run the starter benchmark suite:

- `beeper-bot --config ~/.config/beeper-bot/config.toml eval --suite eval/starter.json`
- add `--json` for machine-readable output
- add `--output state/eval-latest.json` to save a run

Active model since 2026-06-12: **Gemma 4 12B Q6_K**
(`gemma4-google-12b-q6_k-local`), chosen for text parity with the 26B plus
native audio and image input for the multimodal roadmap. Current
deterministic baseline from the private development archive is not published. The committed suites are synthetic public-safe examples:

- `starter`: `8/8` scored passed
- `core`: `20/20` scored passed
- `slice`: `14/14` scored passed
- `control-routing`: `3/3` passed (deterministic product routing, no LLM)
- `control-memory`: `9/9` scored passed through the model path (includes
  an ambiguous-pronoun case that rewards surfacing both candidates)
- `catchup`: `5/5` scored passed (synthetic public-safe group-chat digests)
- `media`: `3/3` scored passed (voice-memo transcript/summary lookup)
- `context-ladder`: `9/9` scored passed across all rungs, after fixing two
  suite-validity defects (an unverifiable "summary" source expectation,
  and pronoun rungs whose distractors introduced a competing referent) and
  one retrieval fix (sender restriction now keys off the planner-resolved
  question)

The porter-stemming migration (schema v5) closed the `owe`/`owes`
morphology gap that capped starter and core for weeks. The control chat
also maintains a real LLM-refreshed rolling summary of older turns
(`maybe_refresh_control_summary`). The 26B (`ai-model gemma4`) remains
available; it is ~2x faster per case (4B-active MoE) but has no audio
path. Single-case run-to-run flips can still occur from llama-server
KV-cache reuse; see `docs/control-chat-memory-and-eval-plan.md` §4.3.1.

Use `--deterministic` for comparison runs. That pins answer and planner temperatures to `0.0` unless explicitly overridden.

## Tests

From a git worktree, run `PYTHONPATH=$PWD/src ~/git/beeper-bot/.venv/bin/python -m unittest discover -s tests` so the editable shared environment imports that worktree's source.
