#!/bin/bash
# Dashboard end to end: user "pc" runs the viewer, which reaches darius over SSH and runs
# site-update through sudo, as from the user's PC. socat exposes the loopback-only viewer on the
# container's published port, so the host browser still sees http://127.0.0.1:8765.
set -euo pipefail
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
runuser -u pc -- bash -c 'cd /src && nohup python3 -m sitewatch --config ~/viewer.json --no-browser --port 8765 > ~/viewer.log 2>&1 &'
nohup socat TCP-LISTEN:8766,fork,reuseaddr TCP:127.0.0.1:8765 >/dev/null 2>&1 &
sleep 2
cat /home/pc/viewer.log
