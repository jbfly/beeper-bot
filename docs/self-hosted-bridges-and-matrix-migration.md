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
transport (the original handoff plan, now with concrete detail):

1. **`MatrixTransport` class** implementing the same surface `beeper_api.py`
   exposes and that `bridge.py` / `sync.py` / `media.py` consume:
   `fetch_all_chats`, `fetch_chat`, `fetch_messages` / `fetch_messages_page`
   (with cursor/direction paging → map to Matrix `/messages` pagination),
   `send_message`. Wrap the nio client; keep the return shapes identical so the
   rest of the pipeline is untouched.
2. **Attachment fetch/decrypt.** Today `media.py` relies on Beeper Desktop's
   `/assets/serve?url=<mxc>` which downloads *and decrypts*. On Matrix the
   transport must `download()` the mxc and decrypt the attachment (nio has the
   helpers) itself.
3. **Config toggle** to select the transport (e.g. `[beeper] transport =
   "desktop-api" | "matrix"`), defaulting to desktop-api so alpha keeps working
   until cutover. Point the `serve` loop at the selected transport.
4. **Key-backup / history restore.** Restore the megolm backup
   (`m.megolm_backup.v1`, present on the server) using the recovery key so the
   device can decrypt pre-existing history — without this the archive can't be
   built from Matrix. This is the main remaining unknown; the recovery-key →
   SSSS path used for cross-signing already proves we can decrypt the backup
   secret.
5. **systemd on venus** next to the bridges; retire the Beeper-Desktop-on-alpha
   dependency (AGENTS.md §2 runtime dep #1).

Until 1–5 land, the bot stays on alpha via the Desktop API. Nothing about the
self-hosted bridges forces a bot change — the Desktop API keeps working because
hungryserv now routes through our bridges transparently.
