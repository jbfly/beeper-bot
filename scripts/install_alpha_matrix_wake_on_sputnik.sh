#!/usr/bin/env bash
set -euo pipefail

sputnik_host="${1:-sputnik}"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

require() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require python3
require ssh

readarray -t values < <(python3 - <<'PY'
import sqlite3
from pathlib import Path

config_path = Path.home() / '.config' / 'beeper-bot' / 'config.toml'
room_id = ''
reply_prefix = '[BEEPER-BOT] '
for line in config_path.read_text().splitlines():
    s = line.strip()
    if s.startswith('control_chat_id'):
        room_id = s.split('=', 1)[1].strip().strip('"')
    elif s.startswith('reply_prefix'):
        reply_prefix = s.split('=', 1)[1].strip().strip('"')

con = sqlite3.connect(str(Path.home() / '.config' / 'BeeperTexts' / 'account.db'))
user_id, device_id, access_token, homeserver = con.execute(
    'select user_id, device_id, access_token, homeserver from account limit 1'
).fetchone()
con.close()

for item in (homeserver.rstrip('/'), user_id, room_id, reply_prefix, access_token):
    print(item)
PY
)

homeserver="${values[0]:-}"
user_id="${values[1]:-}"
room_id="${values[2]:-}"
reply_prefix="${values[3]:-[BEEPER-BOT] }"
access_token="${values[4]:-}"

[[ -n "$homeserver" && -n "$user_id" && -n "$room_id" && -n "$access_token" ]] || die "missing Matrix wake config values"

ssh "$sputnik_host" 'mkdir -p ~/.config/alpha-matrix-wake ~/.local/state/alpha-matrix-wake ~/.config/systemd/user ~/.local/bin && chmod 700 ~/.config/alpha-matrix-wake'

ssh "$sputnik_host" "cat > ~/.config/alpha-matrix-wake/access-token <<'EOF'
$access_token
EOF
chmod 600 ~/.config/alpha-matrix-wake/access-token
cat > ~/.config/alpha-matrix-wake/config.toml <<'EOF'
[matrix]
homeserver = \"$homeserver\"
access_token_file = \"/home/jbfly/.config/alpha-matrix-wake/access-token\"
user_id = \"$user_id\"
room_id = \"$room_id\"
reply_prefix = \"$reply_prefix\"

[alpha]
host = \"192.168.1.11\"
mac = \"2c:f0:5d:57:7f:f6\"
ssh_user = \"jbfly\"
ssh_key = \"/home/jbfly/.ssh/id_ed25519_alpha_remote\"
awake_lease = \"2h\"

[runtime]
sync_timeout_ms = 30000
wake_timeout_seconds = 180
trigger_cooldown_seconds = 60

[state]
path = \"/home/jbfly/.local/state/alpha-matrix-wake/state.json\"
EOF
chmod 600 ~/.config/alpha-matrix-wake/config.toml"

ssh "$sputnik_host" 'cat > ~/.local/bin/alpha-matrix-wake' < "$repo_root/scripts/alpha_matrix_wake.py"
ssh "$sputnik_host" 'chmod +x ~/.local/bin/alpha-matrix-wake'
ssh "$sputnik_host" 'cat > ~/.config/systemd/user/alpha-matrix-wake.service' < "$repo_root/systemd/alpha-matrix-wake.service"
ssh "$sputnik_host" 'systemctl --user daemon-reload && systemctl --user enable --now alpha-matrix-wake.service'

printf 'installed alpha-matrix-wake on %s\n' "$sputnik_host"
