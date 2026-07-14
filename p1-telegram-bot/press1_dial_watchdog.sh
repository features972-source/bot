#!/bin/bash
# Restart campaign dialer if it was killed mid-run (STOP not set, numbers remain).
# Intentional /stop touches STOP and clears ACTIVE — we will not restart those.
set +e
LOG=/tmp/press1_dial.log
ACTIVE=/tmp/press1_active_run_id
rid=$(tr -d ' \r\n' < "$ACTIVE" 2>/dev/null)
[ -z "$rid" ] && exit 0
script=/tmp/press1_dial_${rid}.sh
nums=/tmp/press1_numbers_${rid}.txt
donef=/tmp/press1_dial_done_${rid}.txt
stop=/tmp/press1_stop_${rid}
lock=/tmp/press1_lock_${rid}
[ -f "$script" ] || exit 0
[ -f "$nums" ] || exit 0
[ -f "$stop" ] && exit 0
if pgrep -f "bash ${script}" >/dev/null 2>&1; then
  exit 0
fi
total=$(wc -l < "$nums" 2>/dev/null || echo 0)
did=$(wc -l < "$donef" 2>/dev/null || echo 0)
total=${total:-0}
did=${did:-0}
left=$((total - did))
if [ "$left" -le 0 ]; then
  rm -f "$ACTIVE"
  exit 0
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG restart dialer run=$rid done=$did/$total left=$left" >>"$LOG"
rm -f "$lock" /tmp/press1_global_dial.lock
# Skip numbers already in DONE (script handles that); do not wipe counters.
nohup setsid bash "$script" >>"$LOG" 2>&1 </dev/null &
sleep 2
if pgrep -f "bash ${script}" >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG dialer alive run=$rid" >>"$LOG"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG start FAILED run=$rid" >>"$LOG"
fi
