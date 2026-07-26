# Implementation plan

## 1. Scope

Build the first working system as a single-machine desktop service.

The desktop hosts:
- Beeper Desktop and its local HTTP API
- the SQLite archive
- the bridge poller
- the retrieval code
- the local `llama.cpp` server

This is the proof-of-concept target. It avoids split deployment work while the product shape is still moving.

The code must still keep clean boundaries between ingest, storage, retrieval, and inference. That makes later migration possible.

The next design step is not more narrow benchmark tuning. It is bounded slice reasoning and control-chat memory under explicit prompt-budget pressure. The evaluation plan in `docs/control-chat-memory-and-eval-plan.md` is therefore part of the implementation contract, not a side note.

## 2. Deployment phases

### 2.1 MVP

Run everything on the desktop.

Reason:
- the Beeper local HTTP API exists on the desktop
- local inference also lives on the desktop
- a single process model is easier to debug

### 2.2 Later split deployment

Later, move archive and retrieval services to an always-on server in the homelab.

Expected shape:
- desktop remains the Beeper sync source
- desktop remains the local AI worker
- server holds a replicated archive or retrieval service
- server can send Wake-on-LAN to the desktop when fresh sync or AI work is needed

Constraint:
- as long as the Beeper Desktop local API is the source, the desktop stays the authority for ingest

## 3. Runtime layout

### 2.3 Later mixed-model runtime

A later runtime may use more than one local model, but only sequentially.
The present GPU budget does not support two resident 26B-class endpoints in a practical way.

If mixed-model routing is added, the likely shape is:
- one smaller long-context model for continuity, summary refresh, and memory-write routing
- one stronger model for archive QA and harder slice reasoning
- a local arbiter layer that can unload and load on demand

Do not make this the first memory implementation.
First build the memory substrate and the context-pressure harness. Then use the ladder results to decide whether the extra orchestration is justified.

## 3. Runtime layout

Use XDG-style paths.

- config: `~/.config/beeper-bot/config.toml`
- state dir: `~/.local/state/beeper-bot/`
- database: `~/.local/state/beeper-bot/archive.sqlite3`
- lock file: `~/.local/state/beeper-bot/serve.lock`

Use the systemd user journal for logs. Do not create a separate raw-message log for MVP.

## 4. Package layout

Planned modules:

- `src/beeper_bot/config.py`
- `src/beeper_bot/db.py`
- `src/beeper_bot/schema.py`
- `src/beeper_bot/beeper_api.py`
- `src/beeper_bot/sync.py`
- `src/beeper_bot/retrieval.py`
- `src/beeper_bot/llm.py`
- `src/beeper_bot/bridge.py`
- `src/beeper_bot/policy.py`
- `src/beeper_bot/cli.py`

Module rules:
- `beeper_api.py` knows the Beeper HTTP interface and nothing about retrieval
- `db.py` owns SQLite connections, migrations, and row-level helpers
- `sync.py` maps API payloads into DB rows
- `retrieval.py` reads from the DB and returns scored evidence
- `llm.py` turns evidence into an answer
- `bridge.py` handles the control chat loop and remote commands
- `policy.py` enforces chat restrictions and output checks
- `cli.py` wires commands together

## 5. CLI shape

Expose one entrypoint:

- `python -m beeper_bot ...`

Add a console script later if wanted.

MVP subcommands:
- `init-db`: create schema if missing
- `sync`: run one sync pass for allowlisted chats
- `serve`: run the control-chat poller and periodic sync loop
- `find <query>`: local debug path for lexical retrieval
- `ask <question>`: local debug path for retrieval plus synthesis
- `status`: print local archive and runtime state

The control chat uses the same logic as `find` and `ask`. The CLI exists for local testing and debugging.

## 6. Config schema v1

Use TOML. Python can read it with `tomllib`.

Example:

```toml
[beeper]
api_base = "http://127.0.0.1:23373/v1"
token_file = "~/.config/beeper-bot/token"
control_chat_id = "<beeper-chat-id>"
indexed_chat_ids = ["<chat-a>", "<chat-b>"]
poll_seconds = 5
sync_interval_seconds = 300
history_fetch_limit = 500
http_timeout_seconds = 30

[archive]
path = "~/.local/state/beeper-bot/archive.sqlite3"

[llm]
base_url = "http://127.0.0.1:8090/v1"
model = "gemma"
timeout_seconds = 120
max_input_snippets = 10
max_output_tokens = 300
temperature = 0.1

[bridge]
reply_prefix = "[BEEPER-BOT] "
max_reply_chars = 3500
send_ack = true

[security]
allow_web_search = false
log_raw_messages = false
```

Config rules:
- `control_chat_id` is required
- `indexed_chat_ids` is an explicit allowlist
- no wildcard indexing in MVP
- all local paths must be owned by the local user and created with restrictive permissions

## 7. Database schema v1

Use SQLite with `PRAGMA user_version` for migrations.

Tables:

### `chats`
- `chat_id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `is_allowed INTEGER NOT NULL DEFAULT 1`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `last_synced_at TEXT`

### `messages`
- `message_id TEXT PRIMARY KEY`
- `chat_id TEXT NOT NULL`
- `sort_key INTEGER NOT NULL`
- `timestamp TEXT NOT NULL`
- `sender_id TEXT`
- `sender_name TEXT`
- `is_sender INTEGER NOT NULL DEFAULT 0`
- `message_type TEXT NOT NULL`
- `text TEXT`
- `normalized_text TEXT`
- `raw_json TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `FOREIGN KEY(chat_id) REFERENCES chats(chat_id)`

Indexes:
- unique index on `(chat_id, sort_key)`
- index on `(chat_id, timestamp)`
- index on `(chat_id, sender_name)`

### `sync_state`
- `chat_id TEXT PRIMARY KEY`
- `last_seen_sort_key INTEGER`
- `last_full_sync_at TEXT`
- `updated_at TEXT NOT NULL`

### `runtime_state`
- `key TEXT PRIMARY KEY`
- `value TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Use this for:
- control chat cursor
- reply preference toggles later
- lightweight serve-loop state

### `message_fts`
Use a mirrored FTS5 table for MVP.

Columns:
- `message_id UNINDEXED`
- `chat_id UNINDEXED`
- `chat_name`
- `sender_name`
- `text`

Reason:
- mirrored FTS is simple to inspect
- sync code can update base rows and FTS rows in one transaction
- external-content FTS can wait until the data model is stable

## 8. Message normalization rules

For each stored message:
- keep original API payload in `raw_json`
- keep original `text` if present
- compute `normalized_text` with:
  - Unicode NFKC normalization
  - CRLF to LF conversion
  - leading and trailing whitespace trim
  - internal whitespace collapse for exact-match helpers only

Do not discard the original text.

For non-text messages in MVP:
- store metadata row
- store `text = NULL` unless the API already exposes useful text
- exclude attachments from retrieval and synthesis

## 9. Sync algorithm v1

### 9.1 Initial behavior

Implement recent-history sync first.

Per allowlisted chat:
1. fetch messages from the Beeper local API
2. sort by `sortKey` ascending
3. upsert the chat row
4. upsert message rows
5. rebuild or replace matching FTS rows for changed text messages
6. update `sync_state`
7. commit once per chat

This is enough for a working archive and avoids guessing about deep pagination before the API behavior is measured.

### 9.2 Identity and updates

Use `message_id` as the primary key.

If the API payload lacks a stable message id for a case, fall back to `(chat_id, sort_key)` for duplicate detection during ingest, but still write a synthetic `message_id` so the rest of the code has one stable key.

If a known `message_id` appears with changed text or payload:
- replace the stored row
- update `updated_at`
- replace the FTS row

### 9.3 Deletions

Ignore deletions in MVP.

Reason:
- message recall is the main goal
- delete semantics can vary by bridge
- removal support can be added later with a tombstone field

### 9.4 Sync schedule

`serve` runs a hybrid schedule:
- poll the control chat every `poll_seconds`
- run a full allowlist sync every `sync_interval_seconds`
- before answering `/find` or `/ask`, run a catch-up sync if the archive is stale

Use a small stale threshold, for example 30 seconds.

## 10. Retrieval plan v1

### 10.1 Intent classes

Handle five query shapes first:
- exact address or location recall
- phone or email recall
- date or schedule recall
- who-said-what lookup
- topic summary

Use lightweight pattern checks. Do not add a separate classifier model.

### 10.2 Candidate generation

Stage A: exact helper search
- regex and token extraction for phone numbers, emails, URLs, and date-like strings
- simple address heuristics for street numbers and street suffixes

Stage B: FTS5 search
- search `message_fts.text`
- search `message_fts.sender_name`
- search `message_fts.chat_name`

### 10.3 Scoring

Start with a small additive score:
- FTS `bm25`
- exact substring match boost
- entity-shape match boost
- recency boost
- sender-name match boost

Group near-duplicate results from the same chat and nearby timestamps.

### 10.4 Output shape for `/find`

Return up to 5 results.

Each result should include:
- result number
- chat name
- sender name
- timestamp
- short excerpt

If nothing matches, say so plainly.

## 11. Evidence packet and answer synthesis

### 11.1 Evidence packet

For `/ask`, retrieval returns up to `max_input_snippets` evidence items.

Each item gets a stable citation id:
- `[1]`, `[2]`, ...

Each item includes:
- chat name
- sender name
- timestamp
- excerpt

### 11.2 Prompt contract

The model prompt must say:
- answer only from the evidence block
- if evidence is insufficient, say that directly
- cite claims with citation ids like `[2]`
- do not invent names, dates, or addresses
- do not output hidden reasoning

### 11.3 Reply assembly

Do not let the model invent the final source list.

Instead:
1. the model returns a short answer that cites evidence ids
2. the application validates those ids
3. the application appends a canonical `Sources:` block from the local evidence packet

This keeps citations stable even if the model is sloppy.

Example reply shape:

- `It looks like the address was 123 Sample St [1].`
- `Sources:`
- `[1] Family logistics — Seth — 2026-05-11 14:22`

## 12. Bridge behavior

### 12.1 Chat policy

The bot answers only in the configured control chat.

Rules:
- ignore all non-control chats
- ignore non-text messages in MVP
- ignore the bot's own prefixed replies
- reject commands from any path that is not the control chat

### 12.2 Command semantics

Supported commands in the control chat:
- plain text: same as `/ask <text>`
- `/find <query>`: lexical retrieval only
- `/ask <question>`: retrieval plus synthesis
- `/status`: archive, sync, and model status
- `/reindex`: force a sync pass now
- `/help`: print command summary

### 12.3 Concurrency

Allow one in-flight control-chat request at a time.

If a second request arrives while one is running, reply with a short busy message.

Reason:
- avoids overlapping sync and model work
- avoids out-of-order replies
- keeps state handling simple

## 13. LLM client plan

Use the OpenAI-compatible HTTP API if the local `llama.cpp` server exposes it.

Initial generation settings:
- low temperature
- modest token cap
- deterministic style
- short timeout

Runtime checks:
- verify the base URL is `127.0.0.1` or another explicit loopback bind for MVP
- fail closed if the model endpoint is unreachable

The bridge must return a clear local error message instead of hanging.

## 14. Security and operations

Defaults:
- local-only inference
- local-only archive
- explicit chat allowlist
- no web search by default
- no raw transcript logging by default

Operational rules:
- create config and DB with `0600` permissions where practical
- keep systemd logs free of message bodies unless a debug flag is set
- store no cloud credentials except the local Beeper token already used for the local API
- keep the `llama.cpp` server bound to localhost

## 15. Testing plan

### 15.1 Unit tests

Add tests for:
- config parsing and defaults
- message normalization
- command parsing
- DB migrations
- sync upsert rules
- retrieval scoring helpers
- citation validation

### 15.2 Integration tests

Use fixtures and fake local servers for:
- Beeper API responses
- local LLM responses

Test cases:
- first sync into empty DB
- repeated sync with no duplicates
- edited message upsert
- `/find` result formatting
- `/ask` with sufficient evidence
- `/ask` with insufficient evidence
- control-chat policy rejection

### 15.3 Manual evaluation set

Create a small local query set with known answers.

Measure:
- retrieval hit rate in top 5
- citation correctness
- time to first reply

## 16. Build order

### Milestone 1: foundation
- add `config.py`
- add `db.py` and schema bootstrap
- add `cli.py`
- implement `init-db` and `status`

### Milestone 2: Beeper ingest
- add `beeper_api.py`
- add `sync.py`
- sync one allowlisted chat into SQLite
- persist `sync_state`

### Milestone 3: lexical retrieval
- add mirrored FTS5 table
- implement `/find`
- add exact helpers for phones, emails, URLs, dates, and simple address patterns

### Milestone 4: synthesis
- add `llm.py`
- add evidence packet builder
- implement `/ask`
- append canonical source list

### Milestone 5: bridge
- add `bridge.py`
- poll the control chat
- implement `/help`, `/status`, `/reindex`, `/find`, and plain-text `/ask`
- add single-request busy handling

### Milestone 6: hardening
- reduce logs
- set file modes
- check localhost-only model bind
- add more sync and retrieval tests

## 17. Deferred work

Do not block MVP on these items:
- deep history backfill beyond the recent message window
- delete tombstones
- attachment OCR
- embeddings
- reranker model
- web search
- split desktop/server deployment
- Wake-on-LAN orchestration

Track these next experiments after the current span-retrieval patch:
- add deterministic eval mode for model comparisons and regression runs
- add date-bounded and chat-bounded slice retrieval for day-specific questions
- add last-meaningful-request logic for shopping and follow-up threads
- benchmark one Qwen candidate that fits the 16 GB GPU well
- refresh the local `llama.cpp` tree, rebuild, and rerun the model matrix

Add a control-chat memory and facts layer after the current retrieval work stabilizes:
- keep a bounded recent control-chat turn window for conversational continuity
- maintain a rolling control-chat summary for older turns
- store user-approved facts with provenance and timestamps
- store people, aliases, and relationship facts as structured records
- support safe memory updates from the control chat, with explicit confirmation where needed
- include memory-aware evals that test long control-chat threads, fact carry-forward, and context-budget limits

See `docs/control-chat-memory-and-eval-plan.md` for the work order, harness design, eval classes, and model-prescreen policy.

## 18. First coding step

Start with:
1. `config.py`
2. `db.py`
3. `cli.py`
4. `init-db`
5. `status`

That yields a small executable slice and fixes the runtime contract before API work starts.
