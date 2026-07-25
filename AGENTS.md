# beeper-bot — agent & contributor orientation

Single entry point for working on this repo. Read this first; the `docs/`
files are deeper design notes and history.

`beeper-bot` is a **local-first** assistant behind a private Beeper control
chat. It syncs selected Beeper chats into a local SQLite archive, makes them
searchable (including voice-memo transcripts and image OCR), and answers
questions with cited evidence. All message data and model inference stay
local. Pure Python standard library at runtime (`dependencies = []`).

---

## 1. Quick start

```bash
# from repo root
python -m venv .venv
.venv/bin/pip install -e .            # installs the `beeper-bot` console script
.venv/bin/pip install pytest          # tests only; not a runtime dep
.venv/bin/python -m pytest -q         # 88 tests, ~5s, no network/model needed

# run against the live config
beeper-bot --config ~/.config/beeper-bot/config.toml status
```

Tests use fakes for the Beeper API and the LLM, so they run offline. The
**eval suites** (`eval/*.json`) and any `beeper-bot ask|serve|catchup|
index-media` command need the two live services below.

---

## 2. Runtime dependencies (external to this repo)

1. **Beeper Desktop** with its local HTTP API enabled
   (Settings → Developers → Beeper Desktop API). Base `http://127.0.0.1:23373/v1`,
   bearer token in `~/.config/beeper-bot/token`. Provides chats, messages,
   and `GET /v1/assets/serve?url=<mxc>` which downloads **and decrypts**
   attachments. **This ties the bot to alpha (needs the Desktop GUI).** The
   networks themselves are now bridged by **self-hosted mautrix bridges on
   venus** (via `bbctl`), and a **matrix-nio transport to replace this Desktop
   API leg** is spiked but not yet wired in — see
   `docs/self-hosted-bridges-and-matrix-migration.md` for the current
   self-hosting status, the bridge-login recipe, and the remaining
   `MatrixTransport` work.
2. **ai-ops `llama-serve`** (`~/git/ai-ops/llama-serve`) — a `llama.cpp`
   proxy on `http://127.0.0.1:8090/v1` that starts the model on demand and
   unloads when idle. The bot only ever talks to the proxy, never the raw
   backend.

### The local model

Active model: **Gemma 4 12B Q6_K** (encoderless omni: text + image + audio),
alias `gemma4-google-12b-q6_k-local`. Switch the backend with
`ai-model gemma4_12b_q6k` (fish function); the older 26B MoE is
`ai-model gemma4` (≈2× faster per case, but **no audio** path). The bot's
own model is set in `[llm] model` in the config — keep the two in sync.

**Gotcha:** the encoderless vision path uses non-causal attention, so
`UBATCH_SIZE` in the model env must be **≥ `IMAGE_MAX_TOKENS`** or
`llama-server` core-dumps on any non-tiny image (`GGML_ASSERT n_ubatch >=
n_tokens`). The 12B profile uses `UBATCH_SIZE=512`, `IMAGE_MAX_TOKENS=280`.

---

## 3. Architecture (module map)

`src/beeper_bot/`, ingest → storage → retrieval → synthesis → bridge:

| module | responsibility |
|---|---|
| `config.py` | load TOML config into dataclasses (§6) |
| `db.py` | SQLite schema + migrations; **schema v8** |
| `beeper_api.py` | Beeper Desktop HTTP client: chats, messages, `send_message`, asset download |
| `offline_archive.py` | stable chat approvals, bounded WhatsApp ZIP/TXT import, scoped cited reads |
| `sync.py` | map API payloads → DB rows; mirrored FTS5; deep backfill; re-applies derived media text after upserts |
| `discovery.py` | chat tiering: recent auto-index, dynamic allowlist, on-demand question→chat matching |
| `retrieval.py` | FTS5 (porter) search, additive scoring, span/context expansion, slice windows |
| `planning.py` | `QueryPlan` dataclass (search queries, people, answer_kind, `resolved_question`) |
| `people.py` | people graph: canonical names, aliases, associated chats |
| `llm.py` | the planner, evidence QA + slice reasoning, all prompts, and **`ask_archive` routing** (§5) |
| `memory.py` | control turns, user facts, pending memory writes + confirmation, LLM rolling summary |
| `catchup.py` | per-chat & multi-chat digests, fuzzy chat resolution, natural-language digest parsing |
| `media.py` | attachment fetch/transcribe/describe, derived-text storage, voice-memo lookup |
| `bridge.py` | control-chat serve loop, command parsing, reply formatting + multi-message splitting |
| `music.py` | the "music" control chat: cloud-LLM (Anthropic, purpose `music`) tool loop over music-library-project scripts — now-playing, fixer queue, spectral diagnosis; files changes into the fixer queue, never touches the library |
| `evals.py` | offline eval harness (§7); answer-path validity |
| `tracing.py` | structured trace events → `traces`/`trace_events` tables |
| `console.py` | local operator web console (telemetry); `beeper-bot console` |
| `cli.py` / `__main__.py` | CLI entry (`python -m beeper_bot` / `beeper-bot`) |

---

## 4. Data model (SQLite, schema v8)

`db.py` owns the schema and forward-only migrations (`PRAGMA user_version`,
`migrate_vN_to_vN+1`, applied in `init_db_path`). Tables:

- `chats`, `messages` — the archive. Chats default denied and carry stable-ID approval/revocation metadata; messages carry source kind/reference citations. `messages.text` holds the searchable
  text; for media messages it is replaced by the derived transcript/description.
- `message_fts` — FTS5 mirror, **`tokenize = 'porter unicode61'`** (v5; stems
  owe/owes/owed). Rebuilt from `messages` on migration.
- `sync_state`, `runtime_state` — cursors and key/value state (control-chat
  cursor, catch-up cursors `catchup_cursor:<chat_id>`, rolling-summary state,
  dynamic indexed chat ids).
- `people`, `person_aliases`, `person_chats` — the people graph; **the
  canonical store for aliases** (memory facts reference it, never duplicate).
- `control_turns`, `memory_facts`, `memory_updates` — control-chat memory:
  turn log, user-approved facts, pending writes awaiting confirmation.
- `attachment_derived_text` (v6) — transcripts/descriptions with provenance
  (attachment id, model alias, chunk count, duration, status, error).
- `outbound_queue` (v7) — the `beeper-bot notify` spool: `(target, text,
  created_at, sent_at)`. Enqueued by the CLI, drained by the serve loop.
- `traces`, `trace_events`, `telemetry_samples` — observability.

Per-control-chat cursors live in `runtime_state` under
`control_chat_last_seen_sort_key:<chat_id>` (one row per purpose-scoped chat).

History: v2 people graph · v3 control memory · v4 tracing/telemetry ·
v5 porter FTS · v6 attachment derived text · v7 outbound notify queue ·
v8 default-deny chat approvals and message source citations.

---

## 5. The ask pipeline (`llm.ask_archive`)

A control-chat message (or `beeper-bot ask`) is routed in this order — the
first match wins, and the **direct paths exist precisely because evidence-QA
excerpt caps (~700 chars) would shred long stored text**:

1. `_direct_memory_write_answer` — "remember that X is Y" → propose a
   structured alias/relationship write, ask for confirmation (path `direct`).
2. `_direct_memory_answer` — canonical "who is X again?" → answer from
   stored facts (path `direct`).
3. `_direct_memo_answer` — "transcript/summary of [my|N-min|from-X] voice
   memo" → return the stored transcript verbatim, or summarize the full
   transcript (`media.parse_memo_request`).
4. `_direct_chat_digest_answer` — "summarize the X chat(s)" → route to the
   catch-up machinery (`catchup.parse_chat_digest_request`); fuzzy-resolves
   the chat name and digests **all** matches. Falls through if no chat matches.
5. **Planner** (`plan_archive_query`) — LLM returns a `QueryPlan` with search
   queries, people, `answer_kind`, and a `resolved_question` (pronoun/
   follow-up rewrite, honored only when control context exists).
6. **Retrieval** (`search_archive_multi`) → evidence packet or bounded slice
   windows.
7. **Synthesis** — `answer_from_evidence` / slice reasoning, then a
   verification pass; the app appends a canonical `Sources:` block.

Follow-ups resolve via the planner's `resolved_question`, **not**
question-literal rewrites (those were removed; see plan §4.4). Sender
restriction keys off the resolved question.

---

## 6. Config reference (`~/.config/beeper-bot/config.toml`, TOML)

```toml
[beeper]
control_chat_id = "<required: the private control chat>"
indexed_chat_ids = ["<chat-a>", "..."]   # explicit allowlist
auto_index_recent_days = 30   # >0: also index chats active in last N days
auto_index_max_chats = 100
poll_seconds = 5              # control-chat poll cadence
sync_interval_seconds = 300   # full background sync cadence (serve loop)
history_fetch_limit = 500
history_backfill_pages = 50
http_timeout_seconds = 30

[archive]
path = "~/.local/state/beeper-bot/archive.sqlite3"

[llm]   # the LOCAL/private tier — must be loopback or a private LAN address
base_url = "http://127.0.0.1:8090/v1"   # e.g. 192.168.x when the model is on another box (alpha)
model = "gemma4-google-12b-q6_k-local"
# optional separate planner endpoint/model; temperatures; token caps
planner_temperature = 0.0
max_input_snippets = 5
max_output_tokens = 250
temperature = 0.1

# Optional off-network tier. Only purposes listed here leave the LAN; everything
# that ingests chat content (answer/digest/media/memo) stays local regardless.
# The API key is read from the named env var, never stored in config.
[cloud_llm]
base_url = "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
model = "gpt-5"
api_key_env = "OPENAI_API_KEY"
purposes = ["planner"]                    # empty/omitted = everything stays local

[bridge]
reply_prefix = "[BEEPER-BOT] "
max_reply_chars = 3500   # per-message cap; longer replies are split
max_reply_parts = 6      # max messages per reply before truncation

[media]
exclude_chat_ids = ["<chat-id>"]   # never transcribe/describe these
auto_derive = true                  # derive a few attachments per sync cycle
auto_derive_per_cycle = 3

# Named sets for multi-chat digests. Values in chats may be chat ids or
# title fragments. Exact title/id matches are preferred.
[chat_sets.neighborhood]
display_name = "Neighborhood"
aliases = ["neighborhood", "neighbors"]
chats = ["Neighborhood Community", "Building Updates"]

# Purpose-scoped control chats (keystone for translation / business / cams
# lanes). The serve loop polls every one, each with its own cursor +
# conversational memory. `beeper.control_chat_id` is an implicit `main`.
# persona = literal system directive prepended to free-text answers.
# allowed_commands = restrict command modes (empty = all; help/status always ok).
[control_chats.translate]
chat_id = "!room:beeper.local"
persona = "You are a PT-PT ↔ EN translation assistant."
allowed_commands = ["ask", "help"]

[security]
allow_web_search = false
log_raw_messages = false
```

Loopback-only LLM/Beeper URLs are enforced for MVP. Config and DB are
created `0600` where practical.

---

## 7. Eval harness (`beeper-bot eval --suite eval/<name>.json`)

Add `--deterministic` (pins temps to 0.0) for comparisons, `--output PATH`
to save, `--case ID` / `--tag T` to filter. Suites:

| suite | what it checks |
|---|---|
| `starter`, `core`, `slice` | archive QA, planning, evidence-grounded + slice answers |
| `control-routing` | deterministic product routing (memory writes, canonical lookups) — `direct` path |
| `control-memory` | model-scored continuity & structured-memory use, with held-out paraphrases |
| `context-ladder` | quality under prompt pressure across short/medium/long/stress rungs |
| `catchup` | group-chat digests incl. fuzzy multi-chat, over real fixtures |
| `media` | voice-memo transcript/summary lookup |
| `*-diagnostic` | non-gating tracked cases |

**The core convention — answer-path validity (plan §4.4):** every case
declares `expected_path` (`direct` = deterministic code, `model` = must reach
the LLM). Model-scored suites **fail** a case resolved by a deterministic
shortcut, even if the text matches. No question-literal strings in `src/`.
Each model behavior needs paraphrase variants. Source-class expectations
must be verifiable from the answer (archive = a valid citation). Determinism
caveat: llama-server KV-cache reuse can flip a borderline case run-to-run
(§4.3.1) — run twice for high-stakes comparisons.

The committed `eval/` suites are synthetic public-safe examples. Keep real local benchmark outputs under ignored `state/` paths or another private location.

---

## 8. CLI & control-chat commands

**CLI** (`beeper-bot --config <path> <cmd>`): `init-db`, `status`, `sync`,
`find <q>`, `ask <q>`, `serve [--once]`, `notify <text> [--chat <name>]`,
`catchup <chat> [--since-sort-key] [--no-cursor-update]`,
`index-media --kind voice|image [--limit] [--chat]`, `chat-access {list,approve,revoke}`,
`import-whatsapp`, `archive-search`, `archive-thread`, `chats [--query]`, `eval`,
`console`, `people {list,seed,alias,link,delete}`, `matrix-restore-keys`,
`matrix-create-chat <name> [--topic] [--no-encrypted]`.

`matrix-create-chat` mints a new Matrix room (headless, no Beeper GUI) to use as
a purpose-scoped control chat — see `docs/purpose-scoped-chats.md`, the
integration guide other repos read to plug into the bot.

`notify` queues a fire-and-forget message that the running serve loop delivers
to a control chat (default `main`) — the outbound half of the event bus. Any
homelab box: `ssh venus beeper-bot notify "backup failed" --chat main`.

**Control chat:** plain text = `/ask`; also `/find`, `/catchup <chat>`,
`/index <chat>`, `/music <issue>`, `/music-status`, `/status`, `/reindex`, `/help`. Natural
language also routes: "summarize the X chat(s)", "what is happening in X",
configured chat-set names such as "Neighborhood" or "Sample Festival",
"transcript/summary of my last voice memo", "remember that X is Y"
(confirmation-gated). Replies are reformatted for plain-text Matrix (markdown →
🔹 headers, `•` bullets, blank lines) and split into multiple `(i/n)` messages
when over `max_reply_chars`.

**Purpose-scoped control chats** (`[control_chats.*]`, keystone shipped
2026-07-07): the serve loop polls every configured control chat, each with its
own cursor, per-chat conversational memory (`recent_control_turns(chat_id=…)`),
a `persona` (literal system directive threaded into `ask_archive(persona=…)`),
and an `allowed_commands` filter. `beeper.control_chat_id` remains an implicit
`main` chat, so single-chat configs are unchanged. This is the on-ramp the
translation and Odoo plans depend on (see their docs).

**The music chat** (control chat named `music`) is the first purpose chat with
its own brain: free text there runs a **cloud-LLM tool loop** (`music.py`,
Anthropic Messages API via `llm.anthropic_messages`, purpose `"music"` in
`[cloud_llm].purposes`) over the music-library-project scripts — now-playing,
fixer-queue list/file/resolve, spectral track diagnosis (docker exec into
`music-beets`), Navidrome + beets search. It never modifies the library: write
intents become fixer-queue entries drained by that repo's gated agent, and
`fixer_capture.py` pings the chat back (via `beeper-bot notify`) when issues
resolve or the drain needs an answer. `/music <issue>` (direct capture) and
`/music-status` (queue summary) work without the cloud tier.

---

## 9. Operational state & services

- **systemd user units:** `beeper-bot.service` (the `serve` loop),
  `beeper-bot-console.service` (operator console). `systemctl --user
  restart beeper-bot.service` after code changes that affect the live bot.
- **Where the live bot runs (2026-07-07):** on **venus** via the matrix-nio
  transport (`transport = "matrix"`), from the `~/git/beeper-bot/.venv312`
  Python-3.12 venv, alongside the self-hosted bridges. It uses **alpha's GPU
  model over the LAN** (`[llm] base_url = http://192.168.1.11:8090/v1`). Beeper
  Desktop on alpha is no longer required. Full story + deploy/runbook:
  `docs/self-hosted-bridges-and-matrix-migration.md`.
- The `serve` loop polls the control chat every `poll_seconds`, runs a full
  background sync every `sync_interval_seconds` (with media auto-derivation),
  and uses a fast "quick sync" before answering so questions don't block on
  first-time backfills.
- As of the public repo pass: runtime data, local eval outputs, and private media-derived text must stay outside git. Keep live operational notes in private local docs.

---

## 10. Conventions & gotchas

- **Local-only:** never add cloud calls; LLM/Beeper URLs must be loopback.
- **Derived media text is just text:** transcripts/descriptions are written
  into `messages.text` + FTS, so retrieval/slice/catchup/evals see them with
  no special-casing. Sync re-applies them after upserts rewrite media rows.
- **Chat sets live in config:** use `[chat_sets.<name>]` for curated groups
  such as Neighborhood or Sample Festival. Do not bake private community names
  into source code.
- **No question-literal matching in `src/`** — it's an eval-overfitting smell
  the harness is designed to catch. Route by generic command/intent shapes.
- **Aliases live only in the people graph.** Memory facts may reference a
  person but must not duplicate the alias store.
- Tests are fully offline; keep them that way (fakes for API + LLM).
- Design history & rationale live in `docs/` — `control-chat-memory-and-eval-plan.md`
  (harness rules, §4.4 answer-path validity), `chat-coverage-plan.md`,
  `multimodal-memos-and-images-plan.md`, `implementation-plan.md`,
  `technical-plan.md`, `security.md`.
- **Hosting / transport migration:** `self-hosted-bridges-and-matrix-migration.md`
  is the live story (self-hosted venus bridges ✅, matrix-nio transport spiked,
  bot still on alpha). `incident-2026-07-07-beeper-server.md` and
  `venus-deployment-note.md` are the (on-hold) Beeper-Server history it supersedes.
