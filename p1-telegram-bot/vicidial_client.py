"""SSH control of VICIdial press-1 server (BitCall + 3CX xfer)."""

from __future__ import annotations

import os
import re
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
)
SERVER_IP = os.getenv("VICIDIAL_SERVER_IP", "206.189.118.204")
MAX_CONCURRENT = int(os.getenv("VICIDIAL_MAX_CONCURRENT", "25"))
BATCH_SIZE = int(os.getenv("VICIDIAL_BATCH_SIZE", "25"))
BATCH_PAUSE_SEC = int(os.getenv("VICIDIAL_BATCH_PAUSE_SEC", "5"))
MAX_LEADS = int(os.getenv("VICIDIAL_MAX_LEADS", "5000"))
CPS = int(os.getenv("VICIDIAL_CPS", "5"))
MIN_PHONE_DIGITS = 9

DIAL_SCRIPT = "/tmp/press1_dial_run.sh"
DIAL_NUMBERS = "/tmp/press1_dial_numbers.txt"
DIAL_TOTAL = "/tmp/press1_dial_total"
DIAL_STARTED = "/tmp/press1_dial_started"
DIAL_FAILED = "/tmp/press1_dial_failed"
DIAL_STOP = "/tmp/press1_dial_stop"
DIAL_LOG = "/tmp/press1_dial.log"


def test_numbers() -> list[str]:
    raw = os.getenv("VICIDIAL_TEST_NUMBERS", "447934567847,447300899954")
    return [re.sub(r"\D", "", n) for n in raw.split(",") if n.strip()]


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
                # Paramiko often returns -1 after background/detached commands even when OK.
                if code != 0 and not (code == -1 and out.strip()):
                    raise RuntimeError((err or out or f"remote exit {code}").strip())
                return out
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                import time

                time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last_err or "SSH failed"))


def _server_dial_script() -> str:
    return f"""#!/bin/bash
set +e
STOP={DIAL_STOP}
STARTED={DIAL_STARTED}
FAILED={DIAL_FAILED}
NUMFILE={DIAL_NUMBERS}
BATCH={BATCH_SIZE}
PAUSE={BATCH_PAUSE_SEC}
MAX={MAX_CONCURRENT}
mapfile -t NUMS < "$NUMFILE"
total=${{#NUMS[@]}}
idx=0
while [ "$idx" -lt "$total" ]; do
  [ -f "$STOP" ] && exit 0
  batch_n=0
  while [ "$batch_n" -lt "$BATCH" ] && [ "$idx" -lt "$total" ]; do
    [ -f "$STOP" ] && exit 0
    live=$(asterisk -rx "core show channels" 2>/dev/null | grep -c "@bitcall" || echo 0)
    while [ "$live" -ge "$MAX" ]; do
      [ -f "$STOP" ] && exit 0
      sleep 2
      live=$(asterisk -rx "core show channels" 2>/dev/null | grep -c "@bitcall" || echo 0)
    done
    num=$(echo "${{NUMS[$idx]}}" | tr -d '\\r')
    [ -z "$num" ] && {{ idx=$((idx+1)); continue; }}
    if asterisk -rx "channel originate PJSIP/${{num}}@bitcall extension s@press1-ivr" >>{DIAL_LOG} 2>&1; then
      c=$(cat "$STARTED" 2>/dev/null || echo 0); echo $((c+1)) > "$STARTED"
    else
      f=$(cat "$FAILED" 2>/dev/null || echo 0); echo $((f+1)) > "$FAILED"
    fi
    idx=$((idx+1))
    batch_n=$((batch_n+1))
  done
  if [ "$idx" -lt "$total" ]; then
    sleep "$PAUSE"
  fi
done
exit 0
"""


def _fetch_server_dial_state() -> dict[str, int | bool]:
    """Fast poll: counter files + pgrep only (no slow asterisk -rx per tick)."""
    raw = run_remote(
        f"cat {DIAL_TOTAL} 2>/dev/null || echo 0; "
        f"cat {DIAL_STARTED} 2>/dev/null || echo 0; "
        f"cat {DIAL_FAILED} 2>/dev/null || echo 0; "
        f"pgrep -fc '[p]ress1_dial_run.sh' 2>/dev/null || echo 0",
        timeout=20,
    ).strip().splitlines()
    vals = [int((ln.strip().split() or ["0"])[-1]) for ln in raw[:4]] if raw else [0, 0, 0, 0]
    while len(vals) < 4:
        vals.append(0)
    live = 0
    try:
        live = live_bitcall_channels()
    except Exception:
        pass
    return {
        "total": vals[0],
        "started": vals[1],
        "failed": vals[2],
        "script_running": vals[3] > 0,
        "live": live,
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
        f"asterisk -rx 'channel originate PJSIP/{digits}@bitcall extension s@press1-ivr'"
    )
    return digits


def live_bitcall_channels() -> int:
    out = run_remote(
        r"asterisk -rx 'core show channels' 2>/dev/null | grep -c '@bitcall' || echo 0",
        timeout=15,
    ).strip()
    try:
        return int(out.split()[0])
    except ValueError:
        return 0


def get_dial_stats(since: str | None, progress: dict | None) -> dict[str, str]:
    """Read live counters from the server (source of truth for /run)."""
    prog = progress or {}
    try:
        state = _fetch_server_dial_state()
        total = int(state["total"] or prog.get("total", 0) or 0)
        started = int(state["started"])
        failed = int(state["failed"])
        live = int(state["live"])
        running = bool(state["script_running"])
        if running:
            prog["running"] = True
        elif started + failed >= total and total > 0:
            prog["running"] = False
        prog["started"] = started
        prog["failed"] = failed
        if total:
            prog["total"] = total
        if started > 0 or running:
            prog.pop("error", None)
    except Exception:
        total = int(prog.get("total", 0) or 0)
        started = int(prog.get("started", 0) or 0)
        failed = int(prog.get("failed", 0) or 0)
        live = 0
        running = bool(prog.get("running"))
    left = max(0, total - started - failed)
    return {
        "hopper": str(left),
        "live": str(live),
        "new_leads": str(left),
        "dialed": str(started),
        "press1": "0",
        "answered": "0",
        "campaign_active": "Y" if running else "N",
        "agent_status": "—",
    }


def _stop_remote_dialer() -> None:
    run_remote(
        f"touch {DIAL_STOP} 2>/dev/null; pkill -f '[p]ress1_dial_run.sh' 2>/dev/null || true",
        timeout=20,
    )


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
    progress["running"] = True
    progress["stop"] = False
    progress.pop("error", None)

    if not numbers:
        progress["running"] = False
        raise RuntimeError("No valid numbers to dial")

    run_remote(
        f"pkill -f '[p]ress1_dial_run.sh' 2>/dev/null || true; "
        f"pkill -f AST_VDauto_dial 2>/dev/null || true; "
        f"rm -f {DIAL_STOP}; "
        f"echo 0 > {DIAL_STARTED}; echo 0 > {DIAL_FAILED}; "
        f"echo {len(numbers)} > {DIAL_TOTAL}",
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

    out = run_remote(
        f"chmod +x {DIAL_SCRIPT}; "
        f"nohup setsid bash {DIAL_SCRIPT} >>{DIAL_LOG} 2>&1 </dev/null & "
        f"sleep 2; "
        f"if pgrep -f '[p]ress1_dial_run.sh' >/dev/null; then echo STARTED; "
        f"else echo START_FAIL; tail -20 {DIAL_LOG} 2>/dev/null; fi",
        timeout=45,
    )
    if "START_FAIL" in out and "STARTED" not in out:
        progress["running"] = False
        raise RuntimeError(f"Dialer failed to start: {out.strip()[:300]}")


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
