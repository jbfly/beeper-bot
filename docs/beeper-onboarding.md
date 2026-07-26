# Private WhatsApp archive onboarding

## What this is / what it is not

- Your approved WhatsApp history becomes searchable on **your Mac**.
- The archive is not uploaded; it stays on your Mac.
- This setup cannot send WhatsApp messages or reply to anyone.

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

   No output, or `allow_send = false`, is safe. Stop if it says `true`.
5. Confirm `~/search-whatsapp.sh` exists and works before handing the Mac back to Anna.

## Adding or removing a chat later

```sh
beeper-bot approve <chat_id>
# Now archiving: <chat name>

beeper-bot revoke <chat_id>
# Stopped archiving: <chat name>. Already-stored messages are still on disk; no deletion command exists.
```

Use `beeper-bot chats --local` to check the result. These three commands use only the local archive database.

## KNOWN LIMITS

- **Revoking is not deletion.** `revoke` stops future archiving and removes the chat from search, but already-stored messages remain on disk. There is currently **no deletion command**.
- **Attachment contents are not searchable at launch.** Photos and voice memos become content-searchable only after `beeper-bot index-media`, which needs a multimodal model at `127.0.0.1:8090`; that model is not present on Anna's Mac.
- A voice memo leaves no searchable trace until indexed. An image leaves its caption, or a placeholder such as `[image: filename]`. PDF and other document contents are never indexed.
- If the model is missing, `index-media` marks the item failed but still exits with status 0. Automation that checks only the exit status can miss the failure.

## Privacy/security invariants

- The archive database is created with `0600` permissions: only Anna's Mac account can read or write it.
- Database files are gitignored. Never commit, copy into a repository, or paste chat data into issues or logs.
- Beeper access is read-only in this setup. Message sending raises `PermissionError` unless explicitly enabled.
- `[security] allow_send` defaults to `false`; keep it absent or set to `false`.
