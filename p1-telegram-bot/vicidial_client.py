"""SSH control of VICIdial press-1 server (BitCall + 3CX xfer)."""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import paramiko

from press1_utils import normalize_uk

HOST = os.getenv("VICIDIAL_SSH_HOST", "206.189.118.204")
USER = os.getenv("VICIDIAL_SSH_USER", "root")
CAMPAIGN = os.getenv("VICIDIAL_CAMPAIGN", "press1")
LIST_ID = int(os.getenv("VICIDIAL_LIST_ID", "101"))
SOUND_NAME = os.getenv("VICIDIAL_SOUND_NAME", "press1_alice")
SOUND_DIRS = (
    "/usr/share/asterisk/sounds/en",
    "/usr/share/asterisk/sounds/custom",
    "/var/lib/asterisk/sounds",
    "/var/lib/asterisk/sounds/en",
    "/var/lib/asterisk/sounds/custom",
)
SERVER_IP = os.getenv("VICIDIAL_SERVER_IP", "206.189.118.204")
MAX_CONCURRENT = int(os.getenv("VICIDIAL_MAX_CONCURRENT", "100"))
DIALER_CONCURRENT_CAP = int(os.getenv("VICIDIAL_DIALER_CAP", "0"))
BATCH_SIZE = int(os.getenv("VICIDIAL_BATCH_SIZE", "100"))
BATCH_PAUSE_SEC = int(os.getenv("VICIDIAL_BATCH_PAUSE_SEC", "2"))
CALL_GAP_SEC = float(os.getenv("VICIDIAL_CALL_GAP_SEC", "0.5"))
MAX_LEADS = int(os.getenv("VICIDIAL_MAX_LEADS", "5000"))
CPS = int(os.getenv("VICIDIAL_CPS", "20"))
MIN_PHONE_DIGITS = 9

DIAL_SCRIPT = "/tmp/press1_dial_run.sh"
DIAL_NUMBERS = "/tmp/press1_dial_numbers.txt"
DIAL_TOTAL = "/tmp/press1_dial_total"
DIAL_STARTED = "/tmp/press1_dial_started"
DIAL_FAILED = "/tmp/press1_dial_failed"
DIAL_STOP = "/tmp/press1_dial_stop"
DIAL_LOG = "/tmp/press1_dial.log"
DIAL_RUN_MARK = "/tmp/press1_dial_run_mark"
DIAL_RUN_ID = "/tmp/press1_dial_run_id"
# Written by the dialplan (System app) — must live under /var/lib/asterisk so the
# SELinux-confined asterisk_t domain is allowed to append to them (not /tmp).
DIAL_RUN_PRESS1 = "/var/lib/asterisk/press1_run_press1"
DIAL_RUN_ANSWERED = "/var/lib/asterisk/press1_run_answered"
DIAL_LOCK = "/tmp/press1_dial.lock"


def _dial_done_path(run_id: str) -> str:
    return f"/tmp/press1_dial_done_{run_id}.txt"


def test_numbers() -> list[str]:
    raw = os.getenv("VICIDIAL_TEST_NUMBERS", "447934567847,447300899954")
    nums: list[str] = []
    seen: set[str] = set()
    for n in raw.split(","):
        digits = re.sub(r"\D", "", n)
        if digits and digits not in seen:
            seen.add(digits)
            nums.append(digits)
    return nums


def _load_pkey() -> paramiko.PKey:
    key_data = os.getenv("VICIDIAL_SSH_KEY", "").strip()
    if not key_data:
        raise RuntimeError("VICIDIAL_SSH_KEY is not set on Render")
    if "\\n" in key_data:
        key_data = key_data.replace("\\n", "\n")
    stream = StringIO(key_data)
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            stream.seek(0)
            return key_cls.from_private_key(stream)
        except Exception:
            continue
    raise RuntimeError("VICIDIAL_SSH_KEY is not a valid private key")


@contextmanager
def ssh_connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        pkey=_load_pkey(),
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
    )
    try:
        yield client
    finally:
        client.close()


def run_remote(cmd: str, timeout: int = 120) -> str:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with ssh_connect() as client:
                _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
                code = stdout.channel.recv_exit_status()
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
                # Paramiko returns -1 when the SSH channel closes oddly (common with nohup/&).
                if code not in (0, -1):
                    raise RuntimeError((err or out or f"remote exit {code}").strip())
                return out
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                import time

                time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last_err or "SSH failed"))


def _server_dial_script() -> str:
    batch, pause, gap = BATCH_SIZE, BATCH_PAUSE_SEC, CALL_GAP_SEC
    return f"""#!/bin/bash
set +e
STOP={DIAL_STOP}
STARTED={DIAL_STARTED}
FAILED={DIAL_FAILED}
NUMFILE={DIAL_NUMBERS}
LOG={DIAL_LOG}
LOCK={DIAL_LOCK}
RUNID=$(cat {DIAL_RUN_ID} 2>/dev/null)
[ -z "$RUNID" ] && RUNID=0
DONE=/tmp/press1_dial_done_${{RUNID}}.txt
BATCH={batch}
PAUSE={pause}
GAP={gap}
CAP={DIALER_CONCURRENT_CAP}
exec 9>"$LOCK"
flock -n 9 || {{ echo "$(date '+%Y-%m-%d %H:%M:%S') skip duplicate dialer (locked)" >>"$LOG"; exit 0; }}
touch "$DONE"
batch_n=0
while IFS= read -r num || [ -n "$num" ]; do
  [ -f "$STOP" ] && exit 0
  num=$(echo "$num" | tr -d '\\r' | tr -d ' ')
  [ -z "$num" ] && continue
  grep -qxF "$num" "$DONE" 2>/dev/null && continue
  # Optional concurrency gate. CAP=0 means unlimited provider capacity, so only
  # the placement rate (GAP/BATCH/PAUSE) controls dialing speed.
  while [ "$CAP" -gt 0 ]; do
    [ -f "$STOP" ] && exit 0
    live=$(asterisk -rx "core show channels concise" 2>/dev/null | grep -ci 'bitcall')
    [ "$live" -lt "$CAP" ] && break
    sleep 1
  done
  asterisk -rx "database put press1 lead ${{num}}" >>"$LOG" 2>&1
  if asterisk -rx "channel originate PJSIP/${{num}}@bitcall extension ${{num}}@press1-ivr" >>"$LOG" 2>&1; then
    echo "$num" >>"$DONE"
    s=$(wc -l < "$DONE" 2>/dev/null || echo 0); echo "$s" > "$STARTED"
    echo "$(date '+%Y-%m-%d %H:%M:%S') ok $num" >>"$LOG"
  else
    f=$(cat "$FAILED" 2>/dev/null || echo 0); echo $((f+1)) > "$FAILED"
    echo "$(date '+%Y-%m-%d %H:%M:%S') fail $num" >>"$LOG"
  fi
  batch_n=$((batch_n+1))
  sleep "$GAP"
  if [ "$batch_n" -ge "$BATCH" ]; then
    batch_n=0
    sleep "$PAUSE"
  fi
done < "$NUMFILE"
touch "$STOP"
echo "$(date '+%Y-%m-%d %H:%M:%S') finished run $RUNID" >>"$LOG"
exit 0
"""


def _fetch_server_dial_state(expected_run_id: str | None = None) -> dict[str, int | bool | str]:
    """Counter files + pgrep + live channels in one SSH round-trip."""
    raw = run_remote(
        f"cat {DIAL_TOTAL} 2>/dev/null || echo 0; "
        f"cat {DIAL_STARTED} 2>/dev/null || echo 0; "
        f"cat {DIAL_FAILED} 2>/dev/null || echo 0; "
        f"cat {DIAL_RUN_ID} 2>/dev/null || echo; "
        f"wc -l < {DIAL_RUN_PRESS1} 2>/dev/null || echo 0; "
        f"wc -l < {DIAL_RUN_ANSWERED} 2>/dev/null || echo 0; "
        f"echo 0; "
        f"echo 0; "
        f"ps aux 2>/dev/null | grep -c '[b]ash {DIAL_SCRIPT}' || echo 0; "
        f"wc -l < {DIAL_NUMBERS} 2>/dev/null || echo 0; "
        f"rid=$(cat {DIAL_RUN_ID} 2>/dev/null); "
        f"if [ -n \"$rid\" ] && [ -f /tmp/press1_dial_done_${{rid}}.txt ]; then wc -l < /tmp/press1_dial_done_${{rid}}.txt; else echo 0; fi",
        timeout=25,
    ).strip().splitlines()
    vals: list[str] = []
    for ln in raw[:11]:
        vals.append((ln.strip().split() or ["0"])[-1])
    while len(vals) < 11:
        vals.append("0")
    server_run_id = vals[3].strip()
    file_lines = int(vals[9] or 0)
    file_total = int(vals[0] or 0)
    total = max(file_total, file_lines)
    started_raw = int(vals[1] or 0)
    done_count = int(vals[10] or 0)
    failed = int(vals[2] or 0)
    press1_file = int(vals[4] or 0)
    answered_file = int(vals[5] or 0)
    press1_ast = int(vals[6] or 0)
    answered_ast = int(vals[7] or 0)
    press1 = max(press1_file, press1_ast)
    answered = max(answered_file, answered_ast)
    script_running = int(vals[8] or 0) > 0

    run_match = (
        not expected_run_id
        or not server_run_id
        or server_run_id == expected_run_id
    )
    if not run_match:
        started_raw = 0
        failed = 0
        press1 = 0
        answered = 0

    started = min(max(started_raw, done_count), total) if total > 0 else max(started_raw, done_count)
    live = 0
    try:
        live = live_bitcall_channels()
    except Exception:
        pass
    return {
        "total": total,
        "started": started,
        "started_raw": started_raw,
        "failed": failed,
        "script_running": script_running,
        "live": live,
        "file_lines": file_lines,
        "press1": press1,
        "answered": answered,
        "server_run_id": server_run_id,
        "run_match": run_match,
    }


def mysql(script: str, timeout: int = 120) -> str:
    """Run SQL on the remote server (piped via stdin — avoids ARG_MAX on large lead uploads)."""
    with ssh_connect() as client:
        _stdin, stdout, stderr = client.exec_command("mysql asterisk", timeout=timeout)
        _stdin.write(script)
        _stdin.channel.shutdown_write()
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if code != 0:
            raise RuntimeError((err or out or f"mysql exit {code}").strip())
        return out


def add_leads(phones: list[str]) -> int:
    rows: list[tuple[str, str]] = []
    for phone in phones:
        code, num = normalize_uk(phone)
        if len(num) < MIN_PHONE_DIGITS:
            continue
        rows.append((code, num))
    if not rows:
        return 0
    statements: list[str] = []
    for code, num in rows:
        statements.append(
            f"INSERT INTO vicidial_list (entry_date,status,list_id,phone_code,phone_number,first_name,last_name)"
            f" SELECT NOW(),'NEW',{LIST_ID},'{code}','{num}','Lead',''"
            f" FROM DUAL WHERE NOT EXISTS ("
            f"SELECT 1 FROM vicidial_list WHERE list_id={LIST_ID} AND phone_number='{num}');"
            f"UPDATE vicidial_list SET status='NEW',called_count=0,phone_code='{code}',phone_number='{num}'"
            f" WHERE list_id={LIST_ID} AND phone_number='{num}';"
        )
    sql = "\n".join(statements)
    remote_path = "/tmp/press1_leads_upload.sql"
    with ssh_connect() as client:
        sftp = client.open_sftp()
        with sftp.file(remote_path, "w") as remote_file:
            remote_file.write(sql)
        sftp.close()
        _stdin, stdout, stderr = client.exec_command(
            f"mysql asterisk < {remote_path} && rm -f {remote_path}",
            timeout=300,
        )
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if code != 0:
            raise RuntimeError((err or out or f"lead upload exit {code}").strip())
    return len(rows)


def ping() -> str:
    return run_remote("echo ok && asterisk -rx 'pjsip show registrations' | grep -i bitcall | head -1")


def refill_hopper() -> None:
    mysql(
        f"""
DELETE FROM vicidial_hopper WHERE campaign_id='{CAMPAIGN}';
INSERT INTO vicidial_hopper (lead_id, campaign_id, status, list_id, gmt_offset_now, state, alt_dial, priority, source, vendor_lead_code)
SELECT lead_id, '{CAMPAIGN}', 'READY', list_id, gmt_offset_now, state, 'NONE', 0, 'M', vendor_lead_code
FROM vicidial_list WHERE list_id={LIST_ID} AND status='NEW';
"""
    )


def _ensure_dial_infra() -> None:
    """Phone + remote agent rows required for VICIdial autodial (not just test originate)."""
    mysql(
        f"""
INSERT INTO phones (
  extension,dialplan_number,voicemail_id,phone_ip,computer_ip,server_ip,
  login,pass,status,active,phone_type,fullname,protocol,local_gmt,
  conf_on_extension,ext_context,on_hook_agent,template_id
)
SELECT '8600','8600','8600','0.0.0.0','0.0.0.0','{SERVER_IP}',
  '8600','8600','ACTIVE','Y','VICIdial','Press1 VDAD','SIP','0.00',
  '8600','default','Y','--NONE--'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM phones WHERE extension='8600');
UPDATE vicidial_users SET phone_login='8600', phone_pass='8600' WHERE user='admin';
INSERT INTO vicidial_remote_agents (user_start, number_of_lines, server_ip, status, campaign_id, closer_campaigns, on_hook_agent)
SELECT 'admin', {MAX_CONCURRENT}, '{SERVER_IP}', 'ACTIVE', '{CAMPAIGN}', '', 'Y'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM vicidial_remote_agents WHERE campaign_id='{CAMPAIGN}');
UPDATE vicidial_remote_agents SET number_of_lines={MAX_CONCURRENT}, status='ACTIVE', on_hook_agent='Y'
WHERE campaign_id='{CAMPAIGN}';
"""
    )


def _ensure_daemons() -> None:
    """VDAD/manager processes must run or campaign calls never leave the queue."""
    run_remote(
        r"""
for p in AST_manager_listen.pl AST_manager_send.pl AST_VDhopper.pl AST_VDauto_dial.pl; do
  pgrep -f "$p" >/dev/null || nohup /usr/share/astguiclient/$p >>/var/log/astguiclient/${p%.pl}.log 2>&1 &
done
sleep 3
if ! crontab -l 2>/dev/null | grep -q ADMIN_keepalive_ALL; then
  (crontab -l 2>/dev/null; echo '@reboot /usr/share/astguiclient/ADMIN_keepalive_ALL.pl --cuplogin >> /var/log/astguiclient/keepalive.log 2>&1') | crontab -
fi
"""
    )


def start_campaign() -> None:
    _ensure_dial_infra()
    mysql(
        f"""
UPDATE servers SET max_vicidial_trunks='{MAX_CONCURRENT}', outbound_calls_per_second='{CPS}'
WHERE server_ip='{SERVER_IP}';
UPDATE system_settings SET auto_dial_limit='{MAX_CONCURRENT}', outbound_autodial_active='1', disable_auto_dial='0';
UPDATE vicidial_campaigns SET
  active='Y',
  dial_method='ADAPT_HARD_LIMIT',
  auto_dial_level='1.0',
  adaptive_maximum_level='{MAX_CONCURRENT}',
  adaptive_intensity='2',
  hopper_level='50',
  use_auto_hopper='Y',
  no_hopper_dialing='N',
  dial_prefix='X',
  omit_phone_code='N',
  campaign_cid='443300592867',
  campaign_vdad_exten='8368',
  survey_first_audio_file='{SOUND_NAME}',
  survey_xfer_exten='8000',
  survey_dtmf_digits='1',
  survey_method='EXTENSION',
  survey_wait_sec='15'
WHERE campaign_id='{CAMPAIGN}';
UPDATE vicidial_lists SET dial_prefix='X' WHERE list_id={LIST_ID};
UPDATE vicidial_live_agents SET
  status='READY', outbound_autodial='Y', on_hook_agent='Y', extension='8600',
  conf_exten='8600051', lead_id=0, last_update_time=NOW(), last_call_time=NOW(), last_call_finish=NOW()
WHERE campaign_id='{CAMPAIGN}' AND user='admin';
DELETE FROM vicidial_manager WHERE status='NEW';
"""
    )
    refill_hopper()
    _ensure_daemons()


def stop_campaign() -> None:
    mysql(
        f"""
UPDATE vicidial_live_agents SET status='PAUSED', outbound_autodial='N'
WHERE campaign_id='{CAMPAIGN}';
UPDATE vicidial_campaigns SET active='N' WHERE campaign_id='{CAMPAIGN}';
"""
    )


def server_now() -> str:
    """MySQL datetime on the VICIdial server (for per-run stats)."""
    return mysql("SELECT NOW();").strip()


def get_status() -> dict[str, str]:
    return get_live_stats()


def get_live_stats(since: str | None = None) -> dict[str, str]:
    time_filter = f"call_date >= '{since}'" if since else "call_date >= CURDATE()"
    raw = mysql(
        f"""
SELECT 'hopper' AS k, COUNT(*) AS v FROM vicidial_hopper WHERE campaign_id='{CAMPAIGN}'
UNION ALL SELECT 'live', COUNT(*) FROM vicidial_auto_calls WHERE campaign_id='{CAMPAIGN}'
UNION ALL SELECT 'new_leads', COUNT(*) FROM vicidial_list WHERE list_id={LIST_ID} AND status='NEW'
UNION ALL SELECT 'dialed', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND {time_filter}
UNION ALL SELECT 'press1', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND {time_filter} AND status IN ('SVYEXT','XFER','SVYCLM')
UNION ALL SELECT 'answered', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND {time_filter} AND length_in_sec >= 5;
"""
    )
    out: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    active = mysql(
        f"SELECT active FROM vicidial_campaigns WHERE campaign_id='{CAMPAIGN}' LIMIT 1;"
    ).strip()
    out["campaign_active"] = active or "?"
    agent = mysql(
        f"SELECT status FROM vicidial_live_agents WHERE campaign_id='{CAMPAIGN}' AND user='admin' LIMIT 1;"
    ).strip()
    out["agent_status"] = agent or "?"
    return out


def deploy_audio(files: dict[str, Path]) -> None:
    with ssh_connect() as client:
        sftp = client.open_sftp()
        for directory in SOUND_DIRS:
            for ext, local in files.items():
                remote = f"{directory}/{SOUND_NAME}.{ext}"
                sftp.put(str(local), remote)
        sftp.close()
    globs = " ".join(f"{d}/{SOUND_NAME}.*" for d in SOUND_DIRS)
    run_remote(f"chown asterisk:asterisk {globs} 2>/dev/null; chmod 644 {globs}")
    mysql(
        f"UPDATE vicidial_campaigns SET survey_first_audio_file='{SOUND_NAME}' WHERE campaign_id='{CAMPAIGN}';"
    )
    run_remote("asterisk -rx 'dialplan reload'")


def to_e164(phone: str) -> str:
    """Full UK digits for BitCall originate (same format as /testcall)."""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if digits.startswith("44"):
        return digits
    if digits.startswith("0"):
        return "44" + digits[1:]
    return "44" + digits


def originate_press1(phone: str) -> str:
    """Place one outbound call — identical path to /testcall."""
    digits = to_e164(phone)
    if len(digits) < MIN_PHONE_DIGITS + 2:
        raise ValueError(f"invalid number: {phone!r}")
    run_remote(
        f"asterisk -rx 'database put press1 lead {digits}'; "
        f"asterisk -rx 'channel originate PJSIP/{digits}@bitcall extension {digits}@press1-ivr'"
    )
    return digits


def live_bitcall_channels() -> int:
    out = run_remote(
        r"asterisk -rx 'core show channels concise' 2>/dev/null | grep -ci 'bitcall' || "
        r"asterisk -rx 'core show channels' 2>/dev/null | grep -ci 'PJSIP/bitcall' || echo 0",
        timeout=15,
    ).strip()
    try:
        return int(out.split()[0])
    except ValueError:
        return 0


def _fetch_outcome_stats(run_id: str | None) -> tuple[int, int]:
    """Press-1 xfers and answers for the current run (one line per event, appended by dialplan)."""
    if not run_id:
        return 0, 0
    try:
        raw = run_remote(
            f"wc -l < {DIAL_RUN_PRESS1} 2>/dev/null || echo 0; "
            f"wc -l < {DIAL_RUN_ANSWERED} 2>/dev/null || echo 0",
            timeout=20,
        ).strip().splitlines()
        press1 = int((raw[0] if raw else "0").strip().split()[-1])
        answered = int((raw[1] if len(raw) > 1 else "0").strip().split()[-1])
        return press1, answered
    except Exception:
        return 0, 0


def _dial_state_label(running: bool, total: int, left: int, failed: int) -> str:
    if total <= 0 and not running:
        return "idle"
    if running and left > 0:
        return "running"
    if running and left == 0:
        return "finishing"
    if not running and total > 0 and left == 0:
        return "finished"
    if not running and total > 0 and left > 0:
        return "stalled"
    return "idle"


def get_dial_stats(since: str | None, progress: dict | None) -> dict[str, str]:
    """Read live counters from the server (source of truth for /run)."""
    prog = progress or {}
    expected = int(prog.get("total", 0) or 0)
    run_id = str(prog.get("run_id", "") or "")
    try:
        state = _fetch_server_dial_state(run_id or None)
        file_lines = int(state["file_lines"])
        file_total = int(state["total"])
        total = file_lines or file_total
        if expected > 0:
            total = max(expected, file_lines, file_total)
        started = int(state["started"])
        failed = int(state["failed"])
        live = int(state["live"])
        running = bool(state["script_running"])
        left = max(0, total - started - failed)
        dial_state = _dial_state_label(running, total, left, failed)

        press1 = int(state["press1"])
        answered = int(state["answered"])
        if run_id and bool(state.get("run_match", True)):
            if press1 == 0 and answered == 0 and (running or started > 0):
                press1, answered = _fetch_outcome_stats(run_id)
        elif not run_id:
            press1, answered = 0, 0

        if dial_state == "running":
            prog["running"] = True
            prog.pop("stalled", None)
        else:
            prog["running"] = False
            if dial_state == "stalled":
                prog["stalled"] = True
            else:
                prog.pop("stalled", None)
        prog["started"] = started
        prog["failed"] = failed
        prog["total"] = total
        prog["press1"] = press1
        prog["answered"] = answered
        if started > 0 or running:
            prog.pop("error", None)
    except Exception:
        total = int(prog.get("total", 0) or 0)
        started = int(prog.get("started", 0) or 0)
        failed = int(prog.get("failed", 0) or 0)
        live = 0
        running = bool(prog.get("running"))
        left = max(0, total - started - failed)
        dial_state = _dial_state_label(running, total, left, failed)
        press1 = int(prog.get("press1", 0) or 0)
        answered = int(prog.get("answered", 0) or 0)

    return {
        "hopper": str(left),
        "live": str(live),
        "new_leads": str(left),
        "list_size": str(total),
        "dialed": str(started),
        "press1": str(press1),
        "answered": str(answered),
        "failed": str(failed),
        "campaign_active": "Y" if dial_state == "running" else "N",
        "dial_state": dial_state,
        "agent_status": "—",
        "run_id": run_id,
    }


def _stop_remote_dialer() -> None:
    try:
        run_remote(
            f"touch {DIAL_STOP} 2>/dev/null; pkill -9 -f press1_dial_run.sh 2>/dev/null; true",
            timeout=20,
        )
    except Exception:
        pass


def _start_dial_script() -> None:
    """Start dial script detached; verify with pgrep in a separate SSH call."""
    import time

    run_remote(f"chmod +x {DIAL_SCRIPT}", timeout=15)
    run_remote(
        f"rm -f {DIAL_STOP}; nohup setsid bash {DIAL_SCRIPT} >>{DIAL_LOG} 2>&1 </dev/null &",
        timeout=15,
    )
    time.sleep(2)
    running = run_remote(
        f"ps aux 2>/dev/null | grep -c '[b]ash {DIAL_SCRIPT}' || echo 0",
        timeout=15,
    ).strip().split()[-1]
    if int(running or "0") < 1:
        # Small runs can complete before this health check; treat that as success.
        started = run_remote(f"cat {DIAL_STARTED} 2>/dev/null || echo 0", timeout=15).strip().split()[-1]
        if int(started or "0") > 0:
            return
        log = run_remote(f"tail -25 {DIAL_LOG} 2>/dev/null || echo empty", timeout=15)
        raise RuntimeError(f"Dialer did not start: {log.strip()[:250]}")


def launch_dial_campaign(phones: list[str], progress: dict) -> None:
    """Upload list + start server-side dialer (handles 1k+ leads; bot only monitors)."""
    seen: set[str] = set()
    numbers: list[str] = []
    for phone in phones:
        digits = to_e164(phone)
        if len(digits) >= MIN_PHONE_DIGITS + 2 and digits not in seen:
            seen.add(digits)
            numbers.append(digits)
    if len(numbers) > MAX_LEADS:
        numbers = numbers[:MAX_LEADS]
        progress["truncated"] = MAX_LEADS

    progress["total"] = len(numbers)
    progress["started"] = 0
    progress["failed"] = 0
    progress["press1"] = 0
    progress["answered"] = 0
    progress["running"] = True
    progress["stop"] = False
    progress.pop("error", None)

    if not numbers:
        progress["running"] = False
        raise RuntimeError("No valid numbers to dial")

    run_id = str(int(time.time()))
    progress["run_id"] = run_id

    run_remote(
        f"touch {DIAL_STOP}; "
        f"pkill -9 -f press1_dial_run.sh 2>/dev/null; true; "
        f"sleep 1; "
        f"pkill -9 -f AST_VDauto_dial 2>/dev/null; true; "
        f"mysql asterisk -e \"UPDATE vicidial_campaigns SET active='N' WHERE campaign_id='{CAMPAIGN}'\" 2>/dev/null; "
        f"rm -f {DIAL_STOP}; "
        f"echo 0 > {DIAL_STARTED}; echo 0 > {DIAL_FAILED}; "
        # Answered/press-1 counters are append-logs (one line per event) written by
        # the dialplan; truncate to empty and make world-writable for the asterisk user.
        f": > {DIAL_RUN_PRESS1}; : > {DIAL_RUN_ANSWERED}; "
        f"chown asterisk:asterisk {DIAL_RUN_PRESS1} {DIAL_RUN_ANSWERED} 2>/dev/null; "
        f"chmod 664 {DIAL_RUN_PRESS1} {DIAL_RUN_ANSWERED} 2>/dev/null; "
        f"echo {run_id} > {DIAL_RUN_ID}; "
        f"rm -f /tmp/press1_dial_done_{run_id}.txt; "
        f"echo {len(numbers)} > {DIAL_TOTAL}; "
        f"date '+%Y-%m-%d %H:%M:%S' > {DIAL_RUN_MARK}; "
        f"asterisk -rx 'logger notice PRESS1_RUN_START {run_id}' 2>/dev/null; "
        f"echo \"=== RUN {run_id} {len(numbers)} leads $(date -Iseconds) ===\" >> {DIAL_LOG}",
        timeout=30,
    )

    script_body = _server_dial_script()
    with ssh_connect() as client:
        sftp = client.open_sftp()
        with sftp.file(DIAL_NUMBERS, "w") as remote_file:
            remote_file.write("\n".join(numbers) + "\n")
        with sftp.file(DIAL_SCRIPT, "w") as remote_file:
            remote_file.write(script_body)
        sftp.close()

    verify = run_remote(
        f"wc -l < {DIAL_NUMBERS}; grep -c 'while IFS' {DIAL_SCRIPT}",
        timeout=20,
    ).strip().splitlines()
    line_count = int(verify[0].strip()) if verify else 0
    if line_count < len(numbers):
        raise RuntimeError(f"Upload failed: expected {len(numbers)} lines, got {line_count}")

    run_remote(f"echo {len(numbers)} > {DIAL_TOTAL}", timeout=15)
    run_remote(
        f"sed -i 's/^GAP=.*/GAP={CALL_GAP_SEC}/' {DIAL_SCRIPT}; "
        f"sed -i 's/^BATCH=.*/BATCH={BATCH_SIZE}/' {DIAL_SCRIPT}; "
        f"sed -i 's/^PAUSE=.*/PAUSE={BATCH_PAUSE_SEC}/' {DIAL_SCRIPT}; "
        f"sed -i 's/^CAP=.*/CAP={DIALER_CONCURRENT_CAP}/' {DIAL_SCRIPT}",
        timeout=15,
    )
    _start_dial_script()


def dial_leads(phones: list[str], progress: dict) -> None:
    """Alias: upload + start on server (monitoring is via get_dial_stats)."""
    launch_dial_campaign(phones, progress)

def test_calls(numbers: list[str] | None = None) -> list[str]:
    nums = numbers or test_numbers()
    placed: list[str] = []
    for num in nums:
        try:
            placed.append(originate_press1(num))
        except Exception:
            continue
    return placed
