#!/bin/bash
# Stop the OLD p1-dialer bot on the VPS so it does not compete with Render.
# Two bots polling the same Telegram token = every reply sent twice.
set -euo pipefail
ssh -i /root/.ssh/do_id -o StrictHostKeyChecking=no root@167.99.193.119 <<'EOF'
  systemctl stop p1-dialer.service || true
  systemctl disable p1-dialer.service || true
  systemctl is-active p1-dialer.service || echo "p1-dialer stopped"
EOF
echo "Done — only Render p1-bot should answer Telegram now."
