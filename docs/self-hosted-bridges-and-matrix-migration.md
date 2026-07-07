# Self-hosted bridges + the matrix-nio transport migration

*Status as of 2026-07-07. This is the live migration story. It supersedes the
"Beeper Server" plan in `venus-deployment-note.md` (that approach is on hold
after the 2026-07-07 incident — see `incident-2026-07-07-beeper-server.md`).*

## TL;DR — what is and isn't self-hosted

Two separate milestones; don't conflate them:

1. **Message bridges: self-hosted on venus. ✅ Done.** Every network
   (WhatsApp, Telegram, Signal, Google Messages, Discord, Instagram, Facebook)
   now bridges through **mautrix bridges we run on venus** via `bbctl`
   (bridge-manager), not Beeper's cloud bridges. Network plaintext terminates
   on our hardware.
2. **The bot application: still on alpha. ⛔ Not migrated.** `beeper-bot`
   still reads/sends through **Beeper Desktop's local API**
   (`http://127.0.0.1:23373/v1`) on alpha, which needs the Desktop GUI running.
   The bot does **not** yet use a Matrix transport.

Also note: the **homeserver is still Beeper's** (`hungryserv`). Self-hosting
the *bridges* attaches them to the Beeper account; it does not replace Beeper's
Matrix server, and replacing it is **not** a goal. "Self-hosted on Beeper" here
means self-hosted bridges + (eventually) a direct Matrix client, still on the
Beeper account.

What changed on 2026-07-07: we stood up and fully de-risked a **matrix-nio
client device on venus** — cross-signed, decrypting, sending, and able to drive
bridge logins. That is the transport the bot will move onto. It is a proven
spike, **not yet wired into the bot**.

## Architecture now

```
 phone / network apps
        │  (network plaintext)
        ▼
 mautrix bridges on VENUS  ── bbctl run sh-<net> ──┐   systemd user units
   sh-whatsapp/telegram/signal/gmessages/          │   sh-<net>-bridge.service
   discord/instagram/facebook                      │
        │ (appservice-over-websocket, E2E-bridged) │
        ▼                                           │
 Beeper hungryserv (matrix.beeper.com)  ◄───────────┘
        ▲
        │ Beeper Desktop API (localhost:23373)     ← bot talks to THIS today
        │
 beeper-bot on ALPHA (needs Desktop GUI)
```

Target end state: replace the alpha/Desktop-API leg with a matrix-nio client on
venus talking straight to hungryserv, so the bot runs headless on venus next to
the bridges and alpha's GUI is no longer required.

## The matrix-nio transport spike (what works today)

- **Device:** a dedicated Matrix device for the bot, credentials at
  `~/.config/beeper-bot/matrix-credentials.json` on venus (homeserver =
  `…/_hungryserv/<user>`, plus user_id / device_id / access_token). E2EE store
  at `~/.local/state/beeper-bot/matrix-store`.
- **Library:** `matrix-nio[e2e]`. **Requires Python 3.10** — `python-olm` does
  not build against the system 3.14. Run via
  `uv run --python 3.10 --with "matrix-nio[e2e],…"`.
- **Proven:** initial sync, list rooms, send **encrypted** messages, receive &
  decrypt live messages, and drive each bridge's login over its management room.
- **Known gap:** the device cannot decrypt **pre-existing history** (rooms it
  wasn't in when the megolm sessions were created) — every backfilled encrypted
  event logs `no session found`. Fixing this = the **key-backup restore** step
  below. It is the crux of making the bot useful on this transport.

## How the bridges were logged in (reusable recipe)

Driven entirely from venus by chatting with each bridge bot
(`@sh-<net>bot:beeper.local`) over Matrix from the bot's nio device. Helper
scripts lived in a scratchpad (not committed; they handle live secrets). The
method, in order:

1. **Cross-sign the bot's device.** Bridges require `cross-signed-tofu`
   (`verification_levels` in each bridge config), so a self-signed-only device
   is rejected ("device is not trusted"). We cross-signed it using the account
   **recovery key**: decode the base58 key → SSSS key, decrypt
   `m.cross_signing.self_signing` account data → self-signing seed, sign the
   device key object, `POST /keys/signatures/upload`. (The same recovery key
   also unlocks `m.megolm_backup.v1` — see next section.)
2. **Publish one-time keys.** nio uploads OTKs on first `keys_upload()`; without
   them the bridge can't establish an Olm session to share room keys
   ("Failed to re-share … No session found").
3. **Clear a stale bridge megolm session (only if the bridge cached the device
   as untrusted before step 1).** Symptom: bridge replies stay undecryptable and
   `crypto_megolm_outbound_session_shared` in the bridge DB is empty for that
   room. Fix: stop the bridge, in
   `~/.local/share/bbctl/prod/sh-<net>/mautrix-<net>.db` delete the room's rows
   from `crypto_megolm_outbound_session` (+ matching `_shared` rows) and the
   user's row from `crypto_tracked_user` (forces a fresh key+cross-sign fetch),
   restart. Next reply is a fresh session shared with the now-trusted device.
   **Back up the DB first**; these are ephemeral crypto tables. Only the first
   bridge we did needed this; the rest trusted the device immediately.
4. **Run each bridge's own login flow** in its management room. Command prefixes:
   `!tg !signal !gm !discord !ig !fb`. Flows differ:
   - **Telegram:** `!tg login phone` → number → code (Telegram in-app).
   - **Signal / Discord:** QR flow; render the QR as ANSI in the terminal to
     scan. QRs expire in ~1–2 min; regenerate if needed.
   - **Google Messages:** **no QR** in this build — only `!gm login google`:
     supply Google-account cookies (devtools cURL / JSON), then **tap the shown
     emoji** in the phone Messages app. Requires an **OSID** cookie that only
     exists after you've actually signed into `messages.google.com/web` in a
     browser.
   - **Facebook:** `!fb login facebook` (facebook.com cookies) — *not*
     `messenger` unless you have messenger.com cookies.
   - **Instagram:** `!ig login instagram` (instagram.com cookies incl. a real
     `sessionid`).

### Cookie extraction for the cookie-based bridges

`gmessages`, `facebook`, `instagram` authenticate with browser session cookies.
We pulled them from **alpha's Chrome** over SSH (alpha holds the logged-in
sessions). Notes for whoever repeats this:

- Chrome cookies: `~/.config/google-chrome/Default/Cookies` (sqlite),
  `encrypted_value` is `v10` → AES-128-CBC, IV = 16 spaces, key =
  `PBKDF2-HMAC-SHA1(pw, "saltysalt", 1, 16)`. On this box **pw = `b"peanuts"`**
  works (the keyring value from `secret-tool lookup application chrome` is the
  fallback). Newer Chrome prepends a **32-byte domain hash** to the plaintext —
  strip it (`value[32:]`, fall back to `value`).
- Cookies are **bound to alpha's browser/IP**: replaying them to Google from
  venus returns `CookieMismatch`. So extract on alpha and hand the cookies to
  the bridge, which contacts the network itself.
- Alpha's login shell is **fish** — pipe scripts in
  (`ssh alpha 'python3 -' < script.py`), heredocs won't parse.
- These payloads are **live credentials**: keep them out of git and shred the
  temp files afterward.

## What remains — the transport migration

The bridges are done. The remaining work is moving the **bot** onto the nio
transport (the original handoff plan). Progress so far:

### ✅ Done — `MatrixTransport` + config toggle (`src/beeper_bot/matrix_transport.py`)

- **`MatrixTransport`** implements the `beeper_api.py` surface
  (`fetch_all_chats`, `fetch_chat`, `fetch_messages` / `fetch_messages_page`,
  `send_message`) and returns the same dict shapes `sync.py` / `bridge.py` /
  `discovery.py` already consume (id/title/lastActivity; sortKey/type/text/
  senderID/senderName/isSender/attachments). `sortKey` = event
  `origin_server_ts`. It runs a persistent nio client on its own event loop in a
  daemon thread and marshals sync calls across with `run_coroutine_threadsafe`.
- **Config toggle** `[beeper] transport = "desktop-api" | "matrix"` (default
  desktop-api), plus `matrix_credentials_file` / `matrix_store_path`. A factory
  `beeper_api.make_message_client(config.beeper)` returns the right client and is
  wired into `cli.py` (sync/chats) and `bridge.py`. nio is imported lazily so the
  default runtime stays dependency-free and the offline tests never touch it.
- **Two hungryserv quirks handled** (both verified live):
  - Incremental sync omits quiet rooms → force a **full initial sync**
    (`loaded_sync_token=""`, `next_batch=None`) so every room (incl. the control
    chat) is known.
  - `/messages` rejects the **global** sync token as `from` but accepts each
    room's **`prev_batch`**. So the first page is served from a live per-room
    buffer (kept current by `sync_forever` response callbacks — note those
    callbacks do **not** fire on the manual initial `sync()`, so the initial
    response is ingested by hand), and history pages paginate from the stored
    per-room `prev_batch`. Beeper's sync dedups by message id, so buffer/history
    overlap is harmless.
- **Verified live** on venus: 251 rooms enumerated, control chat read,
  first-page + history pagination, text and image message mapping. Offline unit
  tests in `tests/test_transport.py` cover the config toggle + factory.
- **Python version:** nio's `python-olm` builds on **3.11 / 3.12** but **not
  3.13 / 3.14**. The bot uses `tomllib` (3.11+). So the venus deployment must run
  on **Python 3.12** (3.11 also works). alpha's current 3.14 env cannot host the
  matrix transport.

### ✅ Done — megolm key-backup restore (`matrix-transport.py`, `beeper-bot matrix-restore-keys`)

The device couldn't decrypt history from before it joined a room. Restoring the
server-side megolm backup fixes that. Implemented and verified live (La Familia
went from 0 readable messages to full history — 342 sessions recovered):

- `restore_key_backup(config, recovery_key)` decrypts the SSSS secret
  `m.megolm_backup.v1` with the recovery key, fetches
  `/room_keys/keys?version=N`, decrypts each session with **libolm's
  `PkDecryption`** (bind our backup private key via `olm_pk_key_from_private` —
  hand-rolled curve25519-aes-sha2 got the SSSS-vs-backup HKDF info wrong; use
  libolm), and imports each via `InboundGroupSession.import_session` +
  `save_inbound_group_session`.
- CLI: `beeper-bot matrix-restore-keys` (reads the recovery key from
  `$BEEPER_RECOVERY_KEY` or `--recovery-key-file`). One-shot; the store persists.
  Idempotent (DB upsert), so re-running is safe.
- Gotcha: the SSSS layer's HKDF `info` is the secret **name**
  (`m.megolm_backup.v1`); the backup PK cipher's HKDF `info` is **empty** — don't
  reuse one for the other. All base64 in backups is **unpadded**.

### ✅ Done — attachment fetch/decrypt (`matrix_transport.download_attachment`, wired into `media.py`)

- `download_attachment(config, attachment)` fetches an mxc from the homeserver
  via a short-lived nio client (a plain GET on the media path returns HTTP 400 —
  Beeper's media routing needs nio's `client.download`) and decrypts encrypted
  attachments with nio's `decrypt_attachment` using the `encFile` block the
  message dict already carries.
- `media.fetch_attachment` now takes the attachment dict and branches on
  transport: Desktop `/assets/serve` for desktop-api, `download_attachment` for
  matrix. Verified live end-to-end (image downloaded, cached, valid JPEG).
- Note: in practice Beeper serves media as **plaintext mxc** even from bridges
  (a sweep of all rooms found 0 encrypted / several plaintext), so the decrypt
  branch is a correct standards fallback that rarely fires here.

**The transport is now feature-complete.** What's left is operational cutover.

### Cutover status (2026-07-07) — transport side done on venus

Done and verified on venus:
- **Python 3.12 venv** at `~/git/beeper-bot/.venv312` with `matrix-nio[e2e]` +
  `pycryptodome` (3.13/3.14 can't build python-olm), bot installed editable.
- **`~/.config/beeper-bot/config.toml`** on venus = alpha's config + `transport =
  "matrix"`.
- **Key backup restored** into the venus store (`beeper-bot matrix-restore-keys`,
  342 sessions).
- **Archive builds from Matrix**: `beeper-bot sync --chat-id <current-room>`
  stored 48 real decrypted messages (La Familia) with FTS populated.
- **systemd unit** `~/.config/systemd/user/beeper-bot.service` created pointing at
  `.venv312` — **left disabled/stopped** (see gating below).

### Two blockers before the serve loop can move to venus

1. **`indexed_chat_ids` are 100% stale.** The 2026-07-07 incident deleted the old
   cloud-bridge rooms, so *every* room ID in alpha's config is dead on the
   self-hosted-bridge account (a full `sync` stored 0). The same conversations
   exist under **new** IDs via the sh-* bridges. `sync_chats` was made resilient
   (skips unknown rooms), but the list must be **re-derived** — match the old
   config's name comments against current room titles (`fetch_all_chats`), or lean
   on `auto_index_recent_days` to auto-index active chats. This is an owner
   curation decision (which chats to archive), so propose a title→new-id mapping
   for approval rather than guessing.
2. **LLM stays on alpha (owner decision).** The local GPU model lives on **alpha**
   and is *not* being moved to venus. Plan: venus's beeper-bot points its
   **chat-history** inference (ask/planner/catchup/summaries + voice/image
   derivation — all private chat content) at **alpha's `:8090`** over the LAN
   (SSH tunnel keeps the config's `127.0.0.1:8090`, or bind alpha's llama-serve to
   the LAN). Control/actuator features (/music, catcam, etc.) are intended to use
   **cloud** models — that per-purpose LLM routing is a future change, not built
   yet. So moving the bridges off Beeper's cloud removed the *Beeper-Desktop*
   dependency on alpha; the *model* dependency on alpha is intentional and stays.

### Final cutover steps (do WITH the owner)

1. Re-derive `indexed_chat_ids` for the venus config (approved mapping) and run a
   full `sync` to build the archive.
2. Make alpha's `:8090` reachable from venus (tunnel or LAN bind).
3. **Stop alpha's `beeper-bot.service`** before starting venus's — both poll the
   same control chat, so running both = double answers + cursor fights.
4. `systemctl --user enable --now beeper-bot.service` on venus; retire the
   Beeper-Desktop-on-alpha dependency (AGENTS.md §2 runtime dep #1).

Until these land, the bot stays on alpha via the Desktop API (default toggle).
Nothing about the self-hosted bridges forces a bot change — the Desktop API keeps
working because hungryserv now routes through our bridges transparently.
