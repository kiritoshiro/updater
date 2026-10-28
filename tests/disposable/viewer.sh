#!/bin/bash
# Dashboard end to end: user "pc" runs the viewer, which reaches darius over SSH and runs
# site-update through sudo, as from the user's PC. socat exposes the loopback-only viewer on the
# container's published port 8766. Run with PORT=8775 (and publish 127.0.0.1:8775:8766) when a real
# dashboard already uses 8765 on the host.
set -euo pipefail
PORT=${PORT:-8765}  # host and container port of the viewer (published as 127.0.0.1:$PORT:8766)
mkdir -p /run/sshd
[ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -A >/dev/null
pgrep -x sshd >/dev/null || /usr/sbin/sshd
id pc >/dev/null 2>&1 || useradd -m -s /bin/bash pc
if [ ! -f /home/pc/.ssh/id_ed25519 ]; then
  runuser -u pc -- bash -c 'mkdir -p ~/.ssh && ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519'
  install -d -o darius -g darius -m 0700 /home/darius/.ssh
  install -o darius -g darius -m 0600 /home/pc/.ssh/id_ed25519.pub /home/darius/.ssh/authorized_keys
  runuser -u pc -- bash -c 'ssh-keyscan -t ed25519 127.0.0.1 2>/dev/null > ~/.ssh/known_hosts'
fi
echo '{"host": "127.0.0.1", "user": "darius", "port": 22, "updates": true}' > /home/pc/viewer.json
pkill -f "sitewatch --config" || true
pkill -x socat || true
runuser -u pc -- bash -c 'cd /src && nohup python3 -m sitewatch --config ~/viewer.json --no-browser --port '"$PORT"' > ~/viewer.log 2>&1 &'
nohup socat TCP-LISTEN:8766,fork,reuseaddr TCP:127.0.0.1:$PORT >/dev/null 2>&1 &
sleep 2
cat /home/pc/viewer.log
