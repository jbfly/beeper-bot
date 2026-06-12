# Alpha Matrix wake listener

This service runs on `sputnik`. It watches the Beeper control room through the Matrix client API. The control room is end-to-end encrypted, so the listener cannot read message bodies. Instead it wakes `alpha` on any new encrypted timeline event in that room, waits for SSH, renews a short awake lease, and makes sure `beeper-bot.service` is started.

It does not answer the message itself. It only wakes the real worker on `alpha`.

## Why this shape

- `sputnik` stays on all the time
- `alpha` can sleep when idle
- `beeper-bot` still uses the Beeper Desktop local API on `alpha`
- Matrix traffic is enough to decide when `alpha` should wake

## Files

- `scripts/alpha_matrix_wake.py`
- `scripts/install_alpha_matrix_wake_on_sputnik.sh`
- `systemd/alpha-matrix-wake.service`

## Runtime config on sputnik

Install a config file at:

```sh
~/.config/alpha-matrix-wake/config.toml
```

Shape:

```toml
[matrix]
homeserver = "https://matrix.beeper.com"
access_token_file = "/home/jbfly/.config/alpha-matrix-wake/access-token"
user_id = "@jbfly:beeper.com"
room_id = "!control-room-id:beeper.com"
reply_prefix = "[BEEPER-BOT] "

[alpha]
host = "192.168.1.11"
mac = "2c:f0:5d:57:7f:f6"
ssh_user = "jbfly"
ssh_key = "/home/jbfly/.ssh/id_ed25519_alpha_remote"
awake_lease = "2h"

[runtime]
sync_timeout_ms = 30000
wake_timeout_seconds = 180
trigger_cooldown_seconds = 60

[state]
path = "/home/jbfly/.local/state/alpha-matrix-wake/state.json"
```

The access token should be the Matrix access token from the Beeper Desktop account database on `alpha`, not the local desktop HTTP token.

## Install on sputnik

### Prerequisite (one-time)

```sh
sudo loginctl enable-linger jbfly
```

This keeps your user systemd instance alive after SSH disconnects.

### Install
Use the installer from `alpha`:

```sh
./scripts/install_alpha_matrix_wake_on_sputnik.sh
```

That refreshes the Matrix token and room config on `sputnik`, installs the script and unit, and enables the user service.

## Checks

Status:

```sh
~/.local/bin/alpha-matrix-wake status
systemctl --user status alpha-matrix-wake.service
```

Prime one sync cycle without entering the long-running loop:

```sh
~/.local/bin/alpha-matrix-wake once
```

The first sync pass records `next_batch` and does not act on old history. Later syncs react only to new events.

If Beeper is reinstalled or the Matrix token changes, rerun:

```sh
./scripts/install_alpha_matrix_wake_on_sputnik.sh
```
