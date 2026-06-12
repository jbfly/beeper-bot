# Remote alpha workflow

Use `sputnik` as the always-on front door. `alpha` stays asleep unless you wake it for work.

`sputnik` also runs a small Matrix wake listener. If a new message lands in the Beeper control room while `alpha` is asleep, `sputnik` wakes `alpha` and gives it a short awake lease so `beeper-bot` can answer.

## Main entry point

From another machine on WireGuard:

```sh
ssh -t sputnik ~/alpha
```

From the desktop itself, use the same shared tmux workspace with:

```sh
alpha-work
```

That attaches the same `tmux -L alpha` session used by the remote entry point.

On this desktop, the Niri bindings are now:

```text
Mod+Return       shared tmux workspace
Mod+Shift+Return plain fish shell
```

Each new shared-workspace terminal creates a fresh tmux window inside the shared `alpha` session, so multiple Ghostty windows can attach to the same session without all landing on the same tmux window.

This does five things:

1. wake `alpha` with Wake-on-LAN if needed
2. wait for SSH on `alpha`
3. renew an 8 hour awake lease on `alpha`
4. ensure the persistent tmux workspace exists
5. attach to the `alpha` tmux session

If you want a shorter or longer lease, pass it as the first argument:

```sh
ssh -t sputnik '~/alpha shell 4h'
```

## Optional SSH shortcut on the laptop

If you want one short command, add this to the laptop SSH config:

```sshconfig
Host alpha-remote
  HostName 192.168.1.51
  User jbfly
  RequestTTY yes
  RemoteCommand ~/alpha
```

Then the normal entry point becomes:

```sh
ssh alpha-remote
```

## Detached managed jobs

Run long jobs through the same entry point:

```sh
ssh sputnik '~/alpha run --cwd /home/jbfly/git/beeper-bot --name eval-starter -- \
  /usr/bin/env bash -lc "PYTHONPATH=src python3 -m beeper_bot --config ~/.config/beeper-bot/config.toml eval --suite eval/starter.json"'
```

The job runs as a transient user unit on `alpha` and holds a sleep inhibitor only while it is active.

## Status

```sh
ssh sputnik '~/alpha status'
```

This shows:

- the current awake lease
- the tmux workspace service
- active managed jobs

## Direct tmux details

The shared workspace uses:

- socket: `alpha`
- session: `alpha`

`alpha-work` now creates a fresh tmux window on each attach, then attaches the client there.

So the raw tmux form is:

```sh
tmux -L alpha attach -t alpha
```

But `alpha-work` is the preferred entry point because it starts the backing user service if needed.

## Design notes

- raw SSH sessions do not keep `alpha` awake
- the awake lease is time bounded; if the laptop sleeps and renewals stop, `alpha` may sleep again
- long work should run as managed jobs, not as orphaned processes in SSH session scopes
- `beeper-bot` still depends on the Beeper Desktop local API on `alpha`
