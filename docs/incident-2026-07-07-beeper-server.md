# Incident 2026-07-07: Beeper Server nightly deleted legacy cloud bridges

Status: venus Beeper Server stopped + disabled. Evidence preserved. Drafts below
ready to send. **Nothing has been sent yet.**

## Summary

Running the official headless **Beeper Server 4.2.964** (a nightly-flagged build,
delivered by `beeper-cli 0.6.2` on its default *stable* channel) against the
account `@jbfly:beeper.com` executed one-time migration jobs that deleted the
account's **legacy cloud bridge connections (WhatsApp, Telegram, Google
Messages) and their rooms account-wide**. Chats disappeared from all devices
(Android phone, iPad, alpha desktop). Source-network history (WhatsApp on
phone, SMS in Google Messages, Telegram cloud) unaffected. Beeper-side mirrors
lost pending support restore (long shot) or bridge reconnection.

## Timeline (UTC, 2026-07-07)

- ~01:08 — Round-1 login of Beeper Server on venus (device `OYDOOIPBTE`)
- 01:14:37 — Device fully verified (SAS vs alpha desktop), secrets imported, `setup` → Ready
- **01:15:07 — Deletion storm** in the server's own log (see evidence)
- ~01:46 / ~02:27 — Two more clean-slate login attempts (devices `AZHKMJMCJM`,
  `TIACQZUUWO`) chasing an "empty chats API" symptom, not yet realizing rooms
  were deleted account-side
- ~02:15 — Alpha's Desktop API shows only 7 Matrix-native chats, `hasMore:false`;
  every bridge reports `activeAccountCount: 0`
- ~03:30 — Owner notices all chats gone on phone; server stopped and disabled

## Evidence

Server log excerpts (from the round-1 profile, captured in session transcript;
the profile itself was later wiped during retries):

```
[log]  [2026-07-07T01:15:07.878Z] [BeeperClient] room delete: !ENzcbDTG7HZR8S16pf6h:beeper.local (account whatsapp)
[warn] [2026-07-07T01:15:07.878Z] [syncEventsToAccount] queued 1 events for unloaded account whatsapp; queue size now 397
[log]  [2026-07-07T01:15:07.879Z] [IndexDatabase][deleteRoom] !NZn5s1AGveXzB8b9X6d9:beeper.local rows deleted: 0,1,0,0,0,0,1,0,0,0,0,0
       (dozens more deleteRoom lines for !*:beeper.local rooms)
```

Migration markers found in the server profile's `account.db` `store` table:

```
upgrade00FixMissingEncryptionEvents   upgrade01PopulateLocalBridgeStates
upgrade02DeleteLegacyMegabridges      upgrade03DeleteLegacyMegabridgesAgain
upgrade04DeleteAllLocalBridges        upgrade05DeleteAllLocalBridgesAgain
upgrade06ResyncLocalBridgeMembers     upgrade07SendLocalRoomBackfillStateEvents
upgrade08LeaveLocalRoomsNoBackfillState  upgrade11SetBridgeBotPowerLevelsAgain
```

Post-incident account state: `com.beeper.bridge_state` account data =
`{"bridges":{}, "hungryserv":{...}}`; `/v1/bridges` shows every bridge with
`activeAccountCount: 0, accounts: []`; `/v1/chats` returns exactly 7
Matrix-native rooms.

Install provenance: `beeper install server -y` (channel: stable, default)
downloaded `beeper-server-nightly-4.2.964-linux-arm64.tar.gz`; bundle ID
`com.automattic.beeper.desktop.nightly`; app dir
`~/.cache/BeeperServer/4.2.964-Nightly-linux-arm64-2c20dd220d8a`.

Causality note: inferred from migration names + exact timing correlation; we do
not have Beeper's server-side logs. Stated as such in the drafts.

## Preserved artifacts

- `alpha:~/beeper-incident-20260707/` — beeper-bot archive (95 chats / 18,628
  messages, decrypted+searchable) + alpha's Beeper cache DBs
- Alpha hourly restic to NAS (`restic-alpha-home-nas-backup.timer`) — the ~01:00Z
  snapshot predates deletion; contains pre-incident room inventory (1,532 rooms)
- Round-3 server profile + logs intact on venus (`~/.beeper/profiles/server`)

## Cleanup still to do

- Remove stale devices in Beeper app (Settings → Devices): `AZHKMJMCJM`,
  `TIACQZUUWO`, and `OYDOOIPBTE` if still listed ("Beeper Server (Linux)")
- Reconnect WhatsApp + Google Messages (+ Telegram) from the phone app — owner
  action, after support ticket is filed

---

## Draft 1 — Beeper support ticket

> **Subject: Headless Beeper Server (nightly 4.2.964) deleted my legacy cloud
> bridges and all their chats — restore possible?**
>
> Account: @jbfly:beeper.com (john.bonewitz@gmail.com)
>
> On 2026-07-07 I set up the self-hosted headless Beeper Server on a Linux
> arm64 machine using the official CLI (`beeper-cli 0.6.2`, `beeper setup
> --server`, default stable channel — which installed
> `beeper-server-nightly-4.2.964-linux-arm64`). My account dates from 2023 and
> still used legacy **cloud** bridges for WhatsApp, Telegram, and Google
> Messages.
>
> At 01:15 UTC, immediately after the new device finished verification, the
> server ran what appear to be one-time migrations (markers in its local DB:
> `upgrade02DeleteLegacyMegabridges`, `upgrade04DeleteAllLocalBridges`,
> `upgrade08LeaveLocalRoomsNoBackfillState`, …) and its log shows a storm of
> `room delete: !…:beeper.local (account whatsapp)` lines. Result: my cloud
> bridge accounts were deleted and their rooms disappeared from **all** my
> devices (phone, iPad, desktop). `/v1/bridges` now shows zero connected
> accounts on every bridge and only my 7 Matrix-native chats remain.
>
> Questions:
> 1. Can you restore the deleted cloud bridge accounts and/or their rooms
>    server-side? (I have NOT reconnected the bridges yet, in case
>    reconnecting interferes with a restore. Please tell me if it's safe to
>    reconnect.)
> 2. If restore isn't possible, is any server-side archive of those rooms
>    retrievable?
> 3. Please escalate this as a bug: a headless nightly build running
>    destructive account migrations unattended, on the CLI's default install
>    channel. I'm filing a public issue on the CLI repo with technical
>    details (no account specifics) — happy to share logs privately.
>
> Timeline (UTC 2026-07-07): device verified ~01:14:37; deletions logged
> 01:15:07; new server device IDs involved: OYDOOIPBTE (since removed),
> AZHKMJMCJM, TIACQZUUWO. Log excerpts available on request.

## Draft 2 — GitHub issue (beeper/desktop-api-cli)

> **Title: Beeper Server nightly 4.2.964 (installed via `beeper setup --server`,
> stable channel) ran destructive migrations that deleted legacy cloud bridges
> account-wide**
>
> **Environment**
> - beeper-cli 0.6.2, Linux arm64 (Fedora Asahi), headless box
> - `beeper install server -y` on the **default stable channel** downloaded
>   `beeper-server-nightly-4.2.964-linux-arm64.tar.gz`
>   (bundle `com.automattic.beeper.desktop.nightly`)
> - Account created 2023, still on legacy **cloud** bridges (WhatsApp,
>   Telegram, Google Messages)
>
> **What happened**
> 1. `beeper setup --server` → email login → SAS-verified the new device
>    against an existing desktop → state `ready` (~5 min total).
> 2. ~30 seconds after reaching `ready`, the server logged dozens of
>    `[BeeperClient] room delete: !…:beeper.local (account whatsapp)` and
>    `[IndexDatabase][deleteRoom]` lines, plus
>    `[syncEventsToAccount] queued N events for unloaded account whatsapp`.
> 3. The account's cloud bridge connections were deleted **account-wide**:
>    every bridge now reports `activeAccountCount: 0`,
>    `com.beeper.bridge_state` account data is `{"bridges":{}}`, and all
>    bridged rooms vanished from every logged-in device (phone included).
>    Only Matrix-native rooms survive.
>
> The server profile's `account.db` `store` table shows completed one-time
> migrations including `upgrade02DeleteLegacyMegabridges`,
> `upgrade03DeleteLegacyMegabridgesAgain`, `upgrade04DeleteAllLocalBridges`,
> `upgrade05DeleteAllLocalBridgesAgain`,
> `upgrade08LeaveLocalRoomsNoBackfillState`. Timing strongly suggests one of
> these treated legacy cloud ("megabridge") state as cleanup material. I don't
> have server-side logs, so causality is inferred — happy to provide full
> client logs and timestamps privately (support ticket filed in parallel).
>
> **Expected**
> A headless server login should never run destructive, account-wide
> migrations unattended — especially not from a nightly build delivered on the
> CLI's default "stable" channel.
>
> **Also observed** (before realizing data was being deleted): the
> empty-`/v1/chats`-despite-data symptom from #14, plus rooms/backlog arriving
> only once and being dropped if the app restarts mid-bootstrap.
>
> **Clean-room reproduction of the #14 symptom (same night):** created a
> brand-new account (`@jbflytest:beeper.com`) directly on Beeper Server
> 4.2.964 via `beeper auth email start/response --username` — no bridges, no
> history. Sent it one DM from another account. Result: sync works (the room
> appears in the profile's `mx_room_state`; the note-to-self room even
> materializes in the `threads` table), but `/v1/chats`, `/v1/chats/search`,
> `/v1/messages/search`, and `/v1/chats?accountIDs=<any>` **all return empty**.
> So the chats API on Beeper Server is broken even for a pristine
> single-device account — #14 is not account-shape-specific. `/v1/accounts`
> returns `matrix / connected`, while `threads.accountID` says `hungryserv` —
> possibly an accountID-mapping layer that never initializes on the server
> build.
>
> **Asks**
> 1. Guard destructive migrations behind explicit confirmation (or exclude
>    them from headless/server builds entirely).
> 2. Don't ship nightly-flagged server builds on the stable channel.
> 3. Document the risk for accounts still on legacy cloud bridges.
