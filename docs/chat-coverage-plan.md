# Chat coverage plan

## 1. Goal

Answer questions about any of the user's chats — eventually all ~675 —
without overwhelming model context or requiring config edits per chat.

## 2. Key insight

Indexing more chats does not grow prompt context. Retrieval bounds what the
model sees (evidence packets, bounded slice windows); a bigger archive only
grows the FTS index, which SQLite handles easily at this scale. The real
costs of wide coverage are:

- sync time and API load per pass
- noisier retrieval (more near-miss candidates ranking above the right one)
- a larger sender/chat catalog in the planner prompt (capped at 60 entries
  today; needs relevance-based selection beyond that)

So the design is tiered indexing plus on-demand backfill, not a bigger
prompt.

## 3. Tiers

1. explicit allowlist (`indexed_chat_ids`): always synced; 43 chats as of
   2026-06-12
2. auto-index recent (`auto_index_recent_days`, default 30): every
   non-noise chat with recent activity joins the sync set automatically.
   Noise = bare phone numbers, SMS short codes, archived chats
   (`discovery.is_noise_chat`)
3. dynamic additions: chats added at runtime via `/index <name>` or
   on-the-fly matching are persisted in `runtime_state`
   (`dynamic_indexed_chat_ids`) and synced from then on
4. everything else: not synced until referenced

## 4. On-demand backfill

When a question names a chat that is not in the archive (generic title
matching against the live Beeper chat listing — full-title or token
overlap, never per-question rules), the bridge:

1. syncs that chat immediately (capped at 2 chats per question)
2. adds it to the dynamic tier
3. answers with a note that the chat was synced on the fly

Limitation: this only triggers when the question names the chat. A question
about an old topic without naming the chat ("what was that hostel in Porto
called?") still depends on the chat already being indexed. The long-term
answer is widening tier 2 (e.g. `auto_index_recent_days = 365` once sync
cost is measured) rather than smarter guessing.

## 5. Catch-up summaries

`/catchup <chat>` produces a digest of a chat since the last catch-up:

- per-chat cursor in `runtime_state` (`catchup_cursor:<chat_id>`)
- first run summarizes the most recent window (cap 300 messages); later
  runs summarize only messages past the cursor
- digest format: topics as bullets, concrete details kept, items addressed
  to the user called out
- eval support: `mode: "catchup"` cases with a `catchup_since_sort_key`
  fixture cursor, scored with the usual answer checks and never advancing
  real state

## 6. Risks to watch

- retrieval precision as the archive grows: the borderline starter/core
  failures are ranking-sensitive already; adding 25+ chats may shift them.
  Re-run the full matrix after the first wide sync and compare.
- planner catalog growth past the 60-name cap: switch to picking the most
  recently active and question-relevant names rather than the first 60.
- sync pass duration with 43+ chats: measure; if a full pass exceeds the
  poll budget, sync the allowlist in rotating batches.
