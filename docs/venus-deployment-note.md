# Venus deployment: use Beeper Server, not a virtual display

> **↪ SUPERSEDED (2026-07-07).** The Beeper-Server approach below is on hold; the
> live plan is **self-hosted mautrix bridges on venus + a matrix-nio transport**.
> See `self-hosted-bridges-and-matrix-migration.md`. This note is kept for history.

> **⛔ INCIDENT 2026-07-07: DO NOT RUN BEEPER SERVER AGAINST THE REAL ACCOUNT.**
> Beeper Server 4.2.964 (nightly bundle, delivered even on the "stable" channel),
> set up headlessly on this account, ran destructive migrations
> (`DeleteLegacyMegabridges` / `DeleteAllLocalBridges` /
> `LeaveLocalRoomsNoBackfillState`) that **deleted the account's legacy cloud
> bridge connections (WhatsApp, Telegram, Google Messages) and their rooms
> account-wide** — chats vanished from every device including the phone.
> Source-network history (WhatsApp on phone, SMS in Google Messages, Telegram
> cloud) was unaffected; Beeper-side mirrors were lost. Bot archive
> (18.6k messages / 95 chats) preserved at `alpha:~/beeper-incident-20260707/`.
> Rule going forward: any Beeper Server experiment happens on a **throwaway
> Beeper account first**, and only after this bug is confirmed fixed upstream.
> The rest of this note is kept for reference but is ON HOLD.

*2026-07-06. Supersedes the "headless Wayland compositor + WayVNC" idea.*

Beeper now ships an official **headless Beeper Server**, managed by the `beeper` CLI
(https://github.com/beeper/cli — MIT, v0.6.2 as of May 2026):

```
beeper setup --server --install    # installs + starts headless server in one step
beeper start|stop|restart|logs|enable
beeper accounts add                # attach chat networks
```

It serves **the same Desktop API on http://127.0.0.1:23373** that the bot already
talks to, so `beeper_api.py` and the config should work unchanged — point
`token_file` at a token minted for the venus server (auth is browser OAuth/PKCE or
bearer token). Docs: https://developers.beeper.com/desktop-api-reference/cli

Migration notes:

- The venus server logs in as a **new Matrix device** on the same account —
  coexists fine with Beeper Desktop on alpha and the phone. E2EE key backup will be
  involved when the new device first decrypts history.
- **Cloud-bridged networks appear on every device automatically. On-device
  connections do not** — any network connected "on-device" from alpha/phone would
  need attention on the server. Check which connections are on-device before cutover.
- The bot's archive sqlite + cursors (`~/.local/state/beeper-bot/`) can be copied
  from alpha, or just re-backfilled.
- Desktop API also grew an official "Remote Access" mode (2025-09) — an alternative
  is keeping Beeper Desktop on alpha and letting the venus bot hit it remotely, but
  that reintroduces alpha-must-be-awake; local Beeper Server on venus is the better
  end state.

Also researched and rejected/parked:

- **Direct Matrix client access** (matrix-nio against Beeper's homeserver): works in
  principle, but unofficial, the homeserver isn't fully spec-compliant, you'd own
  E2EE/key backup, and on-device-bridged networks never touch the homeserver in
  readable form. Not worth it while Beeper Server exists.
- **bbctl / bridge-manager** (self-hosted bridges attached to the Beeper account):
  healthy and actively maintained, but solves "where bridges run," not "headless
  API." Relevant later only if we want a bridge Beeper lacks, or want bridge
  plaintext to never leave our machines.
