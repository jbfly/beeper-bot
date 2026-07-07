# es-odoo × beeper-bot: WhatsApp business signals plan

*Drafted 2026-07-06 (venus clone). Status: planned, not started. Companion repo:
`~/git/es-odoo` (exists on both alpha and venus).*

## The thesis

A major source of business signal is WhatsApp client chats — payment promises, order
requests, complaints, delivery confirmations. Today that signal lives only in the
owner's head. Beeper already bridges those WhatsApp chats; beeper-bot already archives
and summarizes chats on the local GPU. The missing piece is a structured lane between
the archive and Odoo, in both directions:

- **Inbound:** WhatsApp chatter → structured signals → Odoo (as log notes / activities
  on the right partner, behind review).
- **Outbound:** Odoo state (overdue invoices, stale leads) → drafted WhatsApp messages
  the owner approves and sends.

Everything drafts-first. The bot never writes to Odoo or messages a client without an
explicit per-item confirmation — and `bridge.py` already has the confirm/reject
pending-action machinery (`looks_like_confirmation` / `looks_like_rejection`) that the
`ask` mode uses, so the approval loop is an extension, not new invention.

## Identity mapping — the real work

Everything hinges on mapping Beeper chats/participants to Odoo `res.partner` records.
WhatsApp participants come with phone numbers via the bridge; partners in Odoo have
phones too, but expect mess (missing country codes, personal vs. company numbers,
group chats mixing several clients). Plan:

- A mapping table in the bot's sqlite: `(beeper_chat_id, participant_id) → partner_id`,
  each row confirmed by the owner once ("this chat is Café Central — confirm?").
- Auto-propose matches by normalized phone number; never auto-confirm.
- Group chats map to a company partner, individual senders optionally to contacts.

## Stages

- **O1 — read-only signal digest.** Index the client WhatsApp chats (config:
  `[chat_sets.clients]`). A `/business` command (or weekly cron message) that runs
  signal extraction over the last week: payments promised, orders mentioned, unanswered
  client questions, tone flags. Pure archive + local LLM; touches nothing. This alone
  is valuable and proves extraction quality before any Odoo writes.
- **O2 — partner mapping + Odoo log notes.** Build the mapping table with the
  confirm flow. Then per confirmed signal, write a log note (`mail.message`) or
  activity on the partner via Odoo's JSON-RPC API. Log notes are low-risk (no
  accounting effect) and make the signal visible to anyone in Odoo, not just the owner.
- **O3 — Odoo-driven outbound drafts.** Pull from Odoo: overdue invoices, quotes
  awaiting response, leads gone quiet. Bot posts a chase list into a "Business" control
  chat with a drafted PT message per item (shares the drafting/register machinery with
  the translation plan). Owner approves item by item; approved drafts are copy-paste
  at first.
- **O4 — assisted send.** After O3 has earned trust: "send 2 and 3" → bot sends via
  Beeper `send_message` to the mapped chat, then writes the sent message back to Odoo
  as a log note. Per-chat allowlist, confirmation always, rate-limited.

## Odoo access

- es-odoo repo has live-instance access patterns already (`odoo-live-backups`,
  reconciliation scripts) — reuse whatever auth/endpoint convention those scripts use
  rather than inventing a new one. Signals plan should read `es-odoo/docs/OPERATING-RULES.md`
  before any live write: that repo has strong evidence-first / dry-run norms, and O2+
  must follow them.
- Odoo writes go through a thin module in beeper-bot (`odoo.py`) or a small CLI in
  es-odoo that beeper-bot shells out to — same pattern as `/music` shelling to
  `fixer_capture.py`. **Prefer the CLI-in-es-odoo pattern:** keeps Odoo credentials and
  business rules in the business repo, and beeper-bot stays a router.

## Privacy & risk notes

- Client messages are business-sensitive: extraction stays on the local GPU like
  everything else.
- Odoo writes: log notes only until much later; nothing that touches accounting.
- Outbound messages to clients are the highest-stakes action this bot would ever take —
  hence draft-only through O3 and per-item confirmation forever.

## Open questions for the owner

- Which WhatsApp chats are in scope for O1? (A handful of key clients first.)
- Is the live Odoo reachable from venus/alpha directly, or only via backups?
- PT-PT drafting register per client (tu vs. você) — probably a per-partner field.
