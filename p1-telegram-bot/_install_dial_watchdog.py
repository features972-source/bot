#!/usr/bin/env python3
"""Install dialer watchdog + mark current stalled run finished (no resume)."""
from pathlib import Path

from repair_server import _load_ssh_key

_load_ssh_key()
import vicidial_client as vd

local = Path(__file__).with_name("press1_dial_watchdog.sh")
remote = "/usr/local/bin/press1_dial_watchdog.sh"
unit = """[Unit]
Description=P1 press-1 dialer stall watchdog
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/press1_dial_watchdog.sh
"""
timer = """[Unit]
Description=Run P1 dialer watchdog every 30s

[Timer]
OnBootSec=30
OnUnitActiveSec=30
AccuracySec=5
Unit=press1-dial-watchdog.service

[Install]
WantedBy=timers.target
"""

print("=== install watchdog ===")
with vd.ssh_connect() as client:
    sftp = client.open_sftp()
    sftp.put(str(local), remote)
    with sftp.file("/etc/systemd/system/press1-dial-watchdog.service", "w") as fh:
        fh.write(unit)
    with sftp.file("/etc/systemd/system/press1-dial-watchdog.timer", "w") as fh:
        fh.write(timer)
    sftp.close()

# Mark current stalled run as intentionally stopped (user asked: don't resume)
print(
    vd.run_remote(
        r"""
chmod 755 /usr/local/bin/press1_dial_watchdog.sh
# Do NOT resume the stalled run — mark it stopped and clear ACTIVE
rid=$(tr -d ' \r\n' < /tmp/press1_active_run_id 2>/dev/null)
if [ -n "$rid" ]; then
  touch "/tmp/press1_stop_$rid"
  echo "$(date '+%Y-%m-%d %H:%M:%S') mark stalled run stopped (no auto-resume) rid=$rid" >> /tmp/press1_dial.log
fi
rm -f /tmp/press1_active_run_id
systemctl daemon-reload
systemctl enable --now press1-dial-watchdog.timer
systemctl restart press1-dial-watchdog.timer
systemctl is-active press1-dial-watchdog.timer
systemctl list-timers press1-dial-watchdog.timer --no-pager
echo '---'
# Confirm no dialer running for old rid
pgrep -af 'bash /tmp/press1_dial_' || echo 'no dialers (expected)'
""",
        40,
    )
)
print("unstick no longer kills dialers:", vd.unstick_dial_server())
