# Security notes

This project is meant to handle sensitive personal chat data.

Default rules:
- keep model inference local
- keep the archive database local
- use a private Beeper control chat
- index only allowlisted chats
- do not enable web search by default
- do not expose the local model server on the LAN

Operational notes:
- prefer `127.0.0.1` binds for local services
- keep database, config, and logs readable only by the local user
- avoid logging raw message content unless debugging demands it
- treat exported backups as sensitive material
