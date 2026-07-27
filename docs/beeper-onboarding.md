# Private WhatsApp archive onboarding

## What this is / what it is not

- Your approved WhatsApp history becomes searchable on **your Mac**.
- The archive is not uploaded; it stays on your Mac.
- This setup cannot send WhatsApp messages or reply to anyone.

## Current state

As of 2026-07-26, the archive has **25 approved chats**: 24 live Beeper chats plus the offline `wa-duarte-mendes` import. It contains **57,318 messages**.

## Anna's steps

1. Open **Beeper** from `/Applications` and log in.
2. In Beeper, add WhatsApp. When the QR code appears, open WhatsApp on your phone, go to **Settings → Linked Devices → Link a Device**, and scan the code.
3. Leave Beeper open while the first archive is prepared.
4. To search later, open Terminal and type:

   ```sh
   ~/search-whatsapp.sh "invoice"
   ```

   Replace `invoice` with the words you want to find.
5. You do not need to manage technical IDs or approval commands. Ask an agent **“what chats are you archiving?”** or **“also archive the thread with X.”**

## Operator (John) steps

Connect over the LAN as `annawright` at `Annas-MacBook-Pro.local` or `192.168.1.36`. Do not rely on WireGuard for this work: its tunnel dropped repeatedly under Beeper's CPU load during the first sync.

Run these on Anna's Mac after Beeper is linked:

1. Find the intended chat and copy its ID:

   ```sh
   beeper-bot chats --query "person or group name"
   ```

2. Approve only that chat, then run its first sync:

   ```sh
   beeper-bot approve <chat_id>
   beeper-bot sync --chat-id <chat_id>
   ```

   Unapproved chats are denied by default and are not archived or returned by search.
3. Verify the local approval state, archive counts, and a harmless test search:

   ```sh
   beeper-bot chats --local
   beeper-bot status
   beeper-bot find "test keyword"
   ```

4. Confirm sending is not enabled:

   ```sh
   grep -n '^[[:space:]]*allow_send' ~/.config/beeper-bot/config.toml
   ```

   `[security] allow_send` defaults to `false`; sending raises `PermissionError` while it is false. Keep the setting absent or explicitly `false`. **Nothing in this setup should ever set `allow_send = true`.**
5. Do not add `api_base` to the config. The built-in default resolves correctly, and `beeper-bot status` works without an explicit value.
6. Confirm `~/search-whatsapp.sh` exists and works before handing the Mac back to Anna.

## Adding, revoking, or deleting a chat

These commands operate on the local archive database:

```sh
beeper-bot approve <chat_id>
# Now archiving: <chat name>

beeper-bot revoke <chat_id>
# Stops archiving and searching it, but keeps its stored data.

beeper-bot forget <chat_id> --yes
# Permanently deletes that chat's stored archive data.

beeper-bot chats --local
# Shows the local approval state without contacting Beeper.
```

`forget` refuses deletion unless `--yes` is supplied. After deletion it reports what it deleted: the chat's messages, search index entries, and attachment text; it also reports the archive-wide operator/bot history, summaries, diagnostics, and memory proposals that it cleared. It explicitly says what it retained, including saved facts, people's names and nicknames, and queued operator notifications. Read that output before treating the deletion as complete.

## Automatic sync

A launchd job named `com.exceptionalspirits.beeper-sync` runs every 15 minutes on Anna's Mac. It calls `~/bb-sync.sh` and appends its log to `~/bb-sync.log`.

The wrapper reads the approved chat list from the database and passes those IDs to sync. This is required because bare `beeper-bot sync` does **not** read database approvals: it reads `indexed_chat_ids` from config, which is empty here, and exits with an error. Approving a new chat therefore needs no config change and no launchd job edit; the wrapper picks it up from the database on its next run.

## Offline export drop folder (operator)

The stdlib-only `scripts/whatsapp_export_dropbox.py` scanner wraps the existing
`import-whatsapp` implementation; it does not parse exports itself, run `serve`,
send messages, or contact the Beeper API. Its default private root is
`~/WhatsApp Exports`. Each immediate chat folder contains an owner-only
`chat.json` with exactly `chat_id` and `name`; the folder, never the export
filename, selects the approved offline chat identity.

Create a private manifest outside git from the synthetic template, set up the
folders, and explicitly approve the same stable IDs:

```sh
cd ~/git/beeper-bot
cp scripts/whatsapp-export-chats.example.tsv ~/.config/beeper-bot/whatsapp-export-chats.tsv
chmod 600 ~/.config/beeper-bot/whatsapp-export-chats.tsv
# Edit the private copy: folder<TAB>chat_id<TAB>name, one chat per line.
PYTHONPATH="$PWD/src" .venv/bin/python scripts/whatsapp_export_dropbox.py setup \
  ~/.config/beeper-bot/whatsapp-export-chats.tsv
while IFS=$'\t' read -r folder chat_id name; do
  beeper-bot --config ~/.config/beeper-bot/config.toml chat-access approve "$chat_id" --name "$name"
done < <(tail -n +2 ~/.config/beeper-bot/whatsapp-export-chats.tsv)
```

Install and load the user launchd job. The generated plist runs the scanner
every 60 seconds and writes only metadata-only summaries or sanitized errors
to an owner-only log. An `fcntl` lock permits only one scanner process; the
kernel releases it after crashes, so no stale lock cleanup is needed.

```sh
PYTHONPATH="$PWD/src" .venv/bin/python scripts/whatsapp_export_dropbox.py install-launchd \
  --manifest ~/.config/beeper-bot/whatsapp-export-chats.tsv
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.exceptionalspirits.whatsapp-export-dropbox.plist 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.exceptionalspirits.whatsapp-export-dropbox.plist
launchctl kickstart -k "gui/$(id -u)/com.exceptionalspirits.whatsapp-export-dropbox"
```

Place an owner-only `.zip` or `.txt` in the matching chat folder. Files less
than 30 seconds old are left for the next scan so Finder can finish writing
them. Successful
files move within the root to `Processed/<folder>/<sha256>/`; failed
files move to `Failed/<folder>/<sha256>/`. Each destination contains a `0600`
JSON receipt with filename, SHA-256, chat ID/name, timestamp, and imported and
duplicate counts (or only a sanitized exception class on failure), never
message text. Every scan rechecks each folder against the private manifest and
reports sanitized per-folder outcomes; one refused folder does not block later
folders, and unrelated files such as `.DS_Store` are ignored. The scanner
refuses
symlinks, duplicate IDs, extra metadata keys, paths escaping the root, and any
root/chat/source/metadata item
with group or other permissions. Re-copying an already imported export is
safe: the reviewed importer's message fingerprints keep the archive
idempotent, and the receipt reports the rows as duplicates.

Manual check, with no Beeper API contact:

```sh
PYTHONPATH="$PWD/src" .venv/bin/python scripts/whatsapp_export_dropbox.py scan \
  --manifest ~/.config/beeper-bot/whatsapp-export-chats.tsv
find ~/WhatsApp\ Exports/Processed ~/WhatsApp\ Exports/Failed -name receipt.json -type f -print
```

Do not put the private manifest, exports, receipts, logs, database, chat names,
or IDs in git or support messages.

## Known limit: history backfill

WhatsApp gives a newly linked device only a small, unpredictable fragment of history. This was verified empirically on 2026-07-26 by manually paging Beeper's API beyond the bot's sync: `hasMore` kept returning true, but message timestamps never went earlier. Raising `history_backfill_pages` does not recover older history.

For real historic conversations, a WhatsApp offline export is the only source. The Duarte export contains **57,083 messages** dating back to August 2023.

Daily counts are snapshotted to `~/bb-history.csv` so archive growth can be compared over time. Its columns are `date, chat_id, count, oldest, newest`; it contains IDs and counts/timestamps only, never chat names or message text.

## Other known limits

- **Revoking is not deletion.** `revoke` stops future archiving and removes the chat from search, but already-stored messages remain until `beeper-bot forget <chat_id> --yes` is run.
- **Attachment contents are not searchable at launch.** Photos and voice memos become content-searchable only after `beeper-bot index-media`, which needs a multimodal model at `127.0.0.1:8090`; that model is not present on Anna's Mac.
- A voice memo leaves no searchable trace until indexed. An image leaves its caption or a generated placeholder. PDF and other document contents are never indexed.
- If the model is missing, `index-media` marks the item failed but still exits with status 0. Automation that checks only the exit status can miss the failure.

## Privacy/security invariants

- The archive database is created with `0600` permissions: only Anna's Mac account can read or write it.
- Database files are gitignored. Never commit, copy into a repository, or paste chat data into issues or logs.
- Beeper access is read-only in this setup. Message sending raises `PermissionError` unless explicitly enabled; never enable `[security] allow_send` for this use case.
- The E2EE concern is **resolved**: suppressing `keys_upload` did not break decryption during the first live sync, which produced zero unreadable messages.

## Development trap: worktree tests

The shared `~/git/beeper-bot/.venv` is an editable install whose `.pth` file points to `~/git/beeper-bot/src`. Running tests from another worktree without setting `PYTHONPATH` therefore silently tests the main checkout instead of the worktree.

Always run worktree tests with the worktree source first, for example:

```sh
PYTHONPATH="$PWD/src" ~/git/beeper-bot/.venv/bin/pytest
```
