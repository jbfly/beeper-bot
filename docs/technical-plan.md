# Technical plan

## 1. Goal

Build a local-first Beeper bot that can answer questions over selected personal chat history.

Primary use cases:
- find an address someone sent
- remember a date or appointment detail
- recall who said something and when
- summarize what a chat or thread said about a topic

The system must keep private data local by default.

## 2. Product shape

The bot will live behind one private Beeper control chat.

Input path:
- user sends a question in the control chat
- bridge syncs or consults the local archive
- retrieval selects relevant message snippets
- local model answers from those snippets
- bot replies with citations

MVP command surface:
- plain text: ask a question
- `/find <query>`: lexical search only
- `/ask <question>`: retrieve and answer
- `/status`: report sync, model, and index state
- `/reindex`: force a sync pass
- `/help`: print command summary

Later commands:
- `/ask-web <question>`: retrieval plus opt-in web search
- `/chats`: list indexed chats
- `/sources on|off`: toggle evidence-heavy replies

## 3. Non-goals for MVP

- replying in group chats
- autonomous multi-step agent behavior
- attachment OCR and image understanding
- direct reads from Beeper's private app database
- cloud models or cloud embeddings

## 4. Architecture

### 4.1 Components

1. `beeper_sync`
   - reads selected chats from the Beeper Desktop local API
   - performs incremental sync into local storage

2. `archive_db`
   - SQLite database for chats, messages, sync cursors, retrieval metadata
   - FTS5 full-text index

3. `retriever`
   - exact search for names, addresses, dates, phones, and emails
   - FTS query expansion and ranking
   - optional semantic retrieval later

4. `llm_client`
   - talks to local `llama.cpp` server over localhost
   - runs answer synthesis only on retrieved snippets

5. `bridge`
   - polls Beeper control chat
   - parses commands
   - sends replies back to Beeper

6. `policy`
   - enforces private control chat only
   - blocks unsafe output patterns
   - controls web search opt-in

### 4.2 Data flow

1. sync messages from Beeper local API
2. normalize and store them in SQLite
3. build or update FTS rows
4. receive a user question in the control chat
5. run retrieval over local archive
6. build a bounded evidence packet
7. ask the local model to answer from evidence only
8. return answer with source citations

## 5. Why our own SQLite database

We will not query Beeper's internal app database directly.

Reasons:
- schema and storage format may change without notice
- direct app DB access is fragile and hard to support
- our use case needs custom FTS, ranking, and sync cursors
- our own DB gives stable backups, migration control, and strict permissions

## 6. Beeper interface choice

Use the Beeper Desktop local HTTP API as the primary source.

Reasons:
- already proven in the existing bridge
- local to the machine
- independent of any one agent runtime
- fits a long-running sync daemon

The MCP server may be useful later as a tool adapter, but not as the base transport.

## 6.1 Deployment note

The MVP should run on one desktop machine.

Reasons:
- the Beeper Desktop local API lives there
- local model inference lives there
- a single-machine proof of concept is easier to debug

Later, the archive or retrieval service may move to an always-on homelab server.
That server could hold a replicated database and send Wake-on-LAN to the desktop when fresh sync or local AI work is needed.
As long as the Beeper Desktop local API is the source, the desktop remains the ingest authority.

## 7. Storage design

Database file:
- `state/beeper-bot.sqlite3`

Tables:

### `chats`
- `chat_id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `is_allowed INTEGER NOT NULL DEFAULT 1`
- `last_synced_at TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

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
- `FOREIGN KEY(chat_id) REFERENCES chats(chat_id)`

Indexes:
- unique index on `(chat_id, sort_key)`
- index on `(chat_id, timestamp)`
- index on `sender_name`

### `sync_state`
- `chat_id TEXT PRIMARY KEY`
- `last_seen_sort_key INTEGER`
- `last_full_sync_at TEXT`
- `updated_at TEXT NOT NULL`

### `message_fts`
FTS5 virtual table over:
- `text`
- `sender_name`
- `chat_name`

Content source can be external-content or mirrored. External-content is preferred if maintenance stays simple.

### optional later tables
- `message_entities`
- `message_embeddings`
- `web_cache`
- `audit_log`

## 8. Retrieval strategy

### 8.1 MVP retrieval

Use hybrid lexical retrieval without embeddings first.

Stage A:
- exact regex and token extraction for:
  - addresses
  - phone numbers
  - emails
  - dates
  - URLs

Stage B:
- SQLite FTS5 search over message text and sender name

Stage C:
- simple reranking with heuristics:
  - recent messages get a small boost
  - exact string match gets a strong boost
  - same sender or same chat clusters get grouped
  - address and date shaped messages get a boost for matching intents

### 8.2 Later retrieval

Add embeddings only after the lexical path is solid.

Candidates later:
- local embedding model via `llama.cpp` or separate runtime
- small reranker if needed

Rationale:
- addresses and dates usually want exact match behavior
- semantic retrieval is most useful for summaries and vague recall

## 9. Answer synthesis

The model will never see full chat history by default. It will only see retrieved evidence.

Prompt contract:
- answer only from the evidence packet
- if evidence is insufficient, say so
- cite chat name, sender, and timestamp when making a factual claim
- do not invent missing addresses, dates, or names
- do not reveal data from non-retrieved messages

Reply shape for MVP:
- short direct answer
- bullet list of sources

Example:
- "The address looks like 123 Sample St, Portland."
- `Sources: Seth, 2026-05-11, chat 'Family logistics'`

## 10. Model plan

Primary runtime:
- existing local `llama.cpp` server in `~/git/ai-ops`

Initial model candidates to benchmark:
1. current Gemma 4 26B-class quant
2. Qwen2.5 14B Instruct quant
3. one larger candidate only if latency stays acceptable

Current assumption:
- the full 16 GB NVIDIA card is free for inference
- this may allow higher GPU layer counts or a larger quant than the old desktop-sharing setup

Tuning plan:
- increase GPU offload and compare tokens/sec
- measure latency for answer synthesis on evidence packets of fixed size
- tune `ctx-size`, `gpu-layers`, `parallel`, and cache reuse
- prefer faster first-token latency over maximum context size

Important note:
- retrieval reduces the need for very large context windows
- model quality matters, but retrieval quality matters more for factual recall

## 11. `llama.cpp` integration

Use OpenAI-compatible local HTTP calls if exposed by the server, or direct server endpoints if not.

Requirements:
- bind model server to `127.0.0.1`, not `0.0.0.0`
- disable any remote exposure by default
- store no prompt logs outside the local machine

Initial generation settings for QA tasks:
- low temperature
- modest max tokens
- deterministic style
- no reasoning trace output

## 12. Security requirements

### 12.1 Defaults
- local-only model inference
- local-only archive DB
- private control chat only
- allowlist of indexed chats
- attachments disabled for MVP
- no cloud embeddings
- no web search unless explicitly requested

### 12.2 Host hardening
- bind inference service to localhost
- file permissions `0600` for config and DB
- minimal logs, no full message dumps
- separate config and state directories

### 12.3 Application policy
- do not answer in non-control chats
- do not dump whole transcripts unless explicitly allowed later
- cap reply size
- return evidence-backed answers only
- explicit banner in docs that the system handles sensitive personal data

## 13. Web search plan

Web search is a later feature and must be opt-in per request.

Reason:
- web search leaks the query to a third party

Planned interface:
- `/ask-web <question>`

Flow:
- run local archive retrieval first
- run web search only if requested
- keep local sources and web sources distinct in the reply

## 14. Proposed repo layout

- `README.md`
- `docs/technical-plan.md`
- `docs/security.md`
- `src/beeper_bot/config.py`
- `src/beeper_bot/db.py`
- `src/beeper_bot/beeper_api.py`
- `src/beeper_bot/sync.py`
- `src/beeper_bot/retrieval.py`
- `src/beeper_bot/llm.py`
- `src/beeper_bot/bridge.py`
- `src/beeper_bot/policy.py`
- `tests/`

## 15. Milestones

### Milestone 1: foundation
- create repo and docs
- define config file format
- define SQLite schema and migrations
- add CLI skeleton

### Milestone 2: sync and archive
- connect to Beeper local API
- sync allowlisted chats into SQLite
- implement status and reindex commands

### Milestone 3: retrieval
- add FTS5 indexing
- add `/find`
- add exact-match helpers for addresses, dates, phones, and emails

### Milestone 4: answer synthesis
- connect to local `llama.cpp`
- add `/ask`
- add source-cited answers

### Milestone 5: hardening
- restrict control chat
- harden localhost model serving
- reduce logging and set file permissions

### Milestone 6: optional search extensions
- add opt-in web search
- evaluate embeddings and reranking

## 16. Immediate next steps

1. scaffold Python package and config model
2. write DB schema and bootstrap migration
3. add Beeper local API client module
4. write sync command for one chat
5. inspect and tune the existing `llama.cpp` server for localhost-only use
6. benchmark current Gemma model against one Qwen candidate

See `docs/implementation-plan.md` for the concrete module, schema, command, and milestone breakdown.

## 17. Open questions

- how far back the Beeper local API can backfill reliably
- whether message IDs are stable enough across all chat sources
- whether edits and deletions need first-class support in MVP
- whether we need per-chat retention controls
- whether a lightweight reranker is needed before embeddings
