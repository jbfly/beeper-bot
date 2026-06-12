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
deterministic baseline (43-chat archive, porter-stemmed FTS, schema v5;
verified identical across two consecutive runs):

- `starter`: `8/8` scored passed
- `core`: `20/20` scored passed
- `slice`: `14/14` scored passed
- `control-routing`: `3/3` passed (deterministic product routing, no LLM)
- `control-memory`: `8/8` scored passed through the model path
- `catchup`: `5/5` scored passed (real Bom Sucesso group-chat digests)
- `context-ladder`: `3/9` scored passed — families degrade at the medium
  rung; this is the honest context-pressure frontier, not a regression
  (the historical `9/9` was scored against deterministic shims)

The porter-stemming migration (schema v5) closed the `owe`/`owes`
morphology gap that capped starter and core for weeks. Full run outputs:
`state/eval-12b/`. The 26B (`ai-model gemma4`) remains available; it is
~2x faster per case (4B-active MoE) and one ladder rung stronger, but has
no audio path. Single-case run-to-run flips can still occur from
llama-server KV-cache reuse; see
`docs/control-chat-memory-and-eval-plan.md` §4.3.1.

Use `--deterministic` for comparison runs. That pins answer and planner temperatures to `0.0` unless explicitly overridden.
