# Translation assistant plan

*Drafted 2026-07-06 (venus clone). Status: planned, not started.*

## The itch

When a Portuguese message arrives (Bom Sucesso chats, client WhatsApp), the current
workflow is copy-paste into an AI chat to get a contextual translation, then compose a
reply by hand. The bot already has everything needed to do this in place: the archive
has the chat history (context), the local LLM does the inference (privacy — neighbors'
messages never leave the GPU), and the control chat is already on the phone.

No bot joins the Portuguese chats. The bot reads them through the archive and talks to
the owner in a control chat. It **drafts** replies; the owner copy-pastes. v1 never
sends into the target chat.

## Interaction sketch

```
/translate bom sucesso            # point at a chat (reuses /catchup fuzzy title match)
[BEEPER-BOT] Targeting "Bom Sucesso Moradores" (last message 14:32).
              Last 6 messages, translated: ...
              (Maria's "fico à espera" here reads as mild impatience — she asked once before)

what should I say? I want to say the plumber comes thursday, politely
[BEEPER-BOT] Draft (European PT, informal-polite):
              "Boa tarde Maria! O canalizador vem quinta de manhã. Peço desculpa pela demora."
              Note: "canalizador" not "encanador" (that's Brazilian).
```

Key mechanic: a **sticky target** — after `/translate <chat>`, subsequent plain messages
in that control chat are interpreted against the target (translate this / draft that /
what did she mean). Stored alongside the pending-confirmation state that
`bridge.py` already keeps.

## Stages

- **T1 — inline command.** `/translate <chat>` in the existing control chat: fuzzy-match
  the chat (same path as `/catchup`), pull the last N messages from the archive (force a
  quick sync of that chat first), prompt the LLM for translation + contextual notes
  (idiom, tone, formality, who's annoyed). Ship this first; it's mostly plumbing that
  exists.
- **T2 — sticky target + drafting.** Remember the active target per control chat.
  Free-text follow-ups get a system prompt: "you are a PT-PT ↔ EN translation and
  drafting assistant; here are the last 20 messages of the target chat; the owner will
  ask for meaning or drafts." Draft replies with register control (tu/você, formal).
- **T3 — dedicated purpose chat.** A "Tradução" note-to-self-style chat whose whole
  persona is T2 (no `/translate` prefix needed — every message is translation work).
  Depends on the purpose-chats generalization below.
- **T4 (later, optional) — send with confirmation.** "send it" → bot posts the approved
  draft into the target chat via `send_message`. Gate behind the existing
  confirm/reject flow, per-chat allowlist in config. Not before T1–T3 have earned trust.

## Purpose-chats prerequisite (shared with the Odoo plan)

Config today has a single `control_chat_id`. Generalize:

```toml
[control_chats.main]
chat_id = "..."
# default: current behavior

[control_chats.translate]
chat_id = "..."
persona = "translation"        # selects system prompt + command set
allowed_commands = ["translate", "help", "status"]
```

The serve loop polls all control chats; each carries its own cursor, persona, and
sticky state. `control_chat_id` stays as an alias for `control_chats.main` so nothing
breaks.

## Quality gate

Gemma 12B is decent at PT↔EN but the *contextual* judgments (tone, PT-PT vs PT-BR
idiom) are where it can be mediocre. Before trusting T2 drafts: run a small eval — take
5 real threads, compare bot drafts against what the owner actually sent (the archive
has both sides). If local quality disappoints, translation is a candidate for a
larger local model slot rather than a cloud fallback — local-only inference is a hard
constraint here, these are neighbors' and clients' private messages.

## Non-goals (v1)

- Bot as a member of the Portuguese chats (community isn't ready; also unnecessary).
- Auto-send without confirmation, ever.
- Live/streaming translation — this is pull-based, on demand.
