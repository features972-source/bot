"""SSH control of VICIdial press-1 server (BitCall + 3CX xfer)."""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import paramiko

from press1_settings import DEFAULT_THREECX, THREECX_PROFILES, profile
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
BATCH_PAUSE_SEC = int(os.getenv("VICIDIAL_BATCH_PAUSE_SEC", "0"))
CALL_GAP_SEC = float(os.getenv("VICIDIAL_CALL_GAP_SEC", "0.2"))
MAX_LEADS = int(os.getenv("VICIDIAL_MAX_LEADS", "5000"))
CPS = int(os.getenv("VICIDIAL_CPS", "20"))
# Stable outbound caller ID for BitCall (required — empty CLI causes instant hangup).
DEFAULT_CALLER_ID = re.sub(r"\D", "", os.getenv("VICIDIAL_CALLER_ID", "442038969244")) or "442038969244"
AU_CALLER_ID = re.sub(r"\D", "", os.getenv("VICIDIAL_AU_CALLER_ID", DEFAULT_CALLER_ID)) or DEFAULT_CALLER_ID
MIN_PHONE_DIGITS = 9


def outbound_caller_id(number: str) -> str:
    """Return CLI for call files."""
    return AU_CALLER_ID

DIAL_SCRIPT = "/tmp/press1_dial_run.sh"
DIAL_NUMBERS = "/tmp/press1_dial_numbers.txt"
DIAL_TOTAL = "/tmp/press1_dial_total"
DIAL_STARTED = "/tmp/press1_dial_started"
DIAL_FAILED = "/tmp/press1_dial_failed"
DIAL_STOP = "/tmp/press1_dial_stop"
DIAL_PAUSE = "/tmp/press1_dial_pause"
DIAL_LOG = "/tmp/press1_dial.log"
DIAL_RUN_MARK = "/tmp/press1_dial_run_mark"
DIAL_RUN_ID = "/tmp/press1_dial_run_id"
# Written by the dialplan (System app) — must live under /var/lib/asterisk so the
# SELinux-confined asterisk_t domain is allowed to append to them (not /tmp).
DIAL_RUN_PRESS1 = "/var/lib/asterisk/press1_run_press1"
DIAL_RUN_ANSWERED = "/var/lib/asterisk/press1_run_answered"
DIAL_STATS_DIR = "/var/lib/asterisk/stats"
DIAL_LOCK = "/tmp/press1_dial.lock"
SETTINGS_PATH = "/var/lib/asterisk/press1_bot_settings.json"
PJSIP_CONF = "/etc/asterisk/pjsip.conf"


def _dial_done_path(run_id: str) -> str:
    return f"/tmp/press1_dial_done_{run_id}.txt"


def _stats_answered_path(run_id: str) -> str:
    return f"{DIAL_STATS_DIR}/{run_id}/answered"


def _stats_press1_path(run_id: str) -> str:
    return f"{DIAL_STATS_DIR}/{run_id}/press1"


def _default_settings() -> dict[str, str]:
    return {"threex_target": DEFAULT_THREECX}


def load_bot_settings() -> dict[str, str]:
    """Read persisted bot settings from the dial server."""
    try:
        raw = run_remote(f"cat {SETTINGS_PATH} 2>/dev/null", timeout=15).strip()
        if not raw:
            return _default_settings()
        data = json.loads(raw)
        target = str(data.get("threex_target", DEFAULT_THREECX)).strip().lower()
        if target not in THREECX_PROFILES:
            target = DEFAULT_THREECX
        return {"threex_target": target}
    except Exception:
        return _default_settings()


def save_bot_settings(data: dict[str, str]) -> None:
    target = str(data.get("threex_target", DEFAULT_THREECX)).strip().lower()
    profile(target)  # validate
    payload = json.dumps({"threex_target": target})
    run_remote(
        f"mkdir -p $(dirname {SETTINGS_PATH}); "
        f"cat > {SETTINGS_PATH} <<'EOF'\n{payload}\nEOF\n"
        f"chmod 644 {SETTINGS_PATH}",
        timeout=20,
    )


def get_threex_target() -> str:
    return load_bot_settings().get("threex_target", DEFAULT_THREECX)


def apply_threex_target(profile_id: str) -> dict[str, str]:
    """Point the Asterisk 3cx trunk at the selected 3CX and persist the choice."""
    p = profile(profile_id)
    contact = p["sip_contact"]
    host = p["host"]
    ext = p["ext"]
    run_remote(
        f"cp -a {PJSIP_CONF} {PJSIP_CONF}.bak.settings-$(date +%s); "
        f"sed -i '/^\\[3cx-aor\\]/,/^\\[/ s|^contact=.*|contact=sip:{contact}:5060|' {PJSIP_CONF}; "
        f"sed -i '/^\\[3cx\\]/,/^\\[/ s|^from_domain=.*|from_domain={contact}|' {PJSIP_CONF}; "
        f"sed -i '/^\\[3cx-identify\\]/,/^\\[/ s|^match=.*|match={host}|' {PJSIP_CONF}; "
        f"asterisk -rx 'pjsip reload' >/dev/null; "
        f"mysql asterisk -e \"UPDATE vicidial_campaigns SET survey_xfer_exten='{ext}' WHERE campaign_id='{CAMPAIGN}';\"",
        timeout=30,
    )
    save_bot_settings({"threex_target": profile_id})
    return p


def ensure_threex_target() -> dict[str, str]:
    """Apply saved 3CX target on startup (idempotent)."""
    target = get_threex_target()
    return apply_threex_target(target)


def _press1_ivr_dialplan(*, server_ip: str, xfer_ext: str, sound: str) -> str:
    """Canonical press1-ivr: Background+WaitExten, per-run stats, 3CX xfer."""
    return f"""[press1-ivr]
exten => _X.,1,Set(LEADNUM=${{FILTER(0-9,${{EXTEN}})}})
 same => n,Goto(ivr,1)

exten => s,1,Set(LEADNUM=${{FILTER(0-9,${{LEADNUM}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{DB(press1/lead)}})}})}})
 same => n,Goto(ivr,1)

exten => ivr,1,Answer()
 same => n,Wait(2)
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{DB(press1/lead)}})}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{CALLERID(dnid)}})}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{PJSIP_HEADER(read,To)}})}})}})
 same => n,Set(CHANNEL(language)=en)
 same => n,NoOp(IVR lead=${{LEADNUM}})
 same => n,Set(P1RUN=${{DB(press1/runs/${{FILTER(0-9,${{LEADNUM}})}})}})
 same => n,ExecIf($["${{LEN(${{P1RUN}})}}" = "0"]?Set(P1RUN=0))
 same => n,System(mkdir -p {DIAL_STATS_DIR}/${{P1RUN}})
 same => n,System(echo 1 >> {DIAL_STATS_DIR}/${{P1RUN}}/answered)
 same => n,Background({sound})
 same => n,WaitExten(25)
 same => n,Hangup()

exten => 1,1,StopPlaytones()
 same => n,Goto(xfer,1)

exten => xfer,1,NoOp(Press1 xfer lead ${{LEADNUM}} to 3CX {xfer_ext})
 same => n,StopPlaytones()
 same => n,GotoIf($[${{LEN(${{LEADNUM}})}}<10]?xferdial,1)
 same => n,Set(CIDNUM=+${{LEADNUM}})
 same => n,Set(CALLERID(num)=${{CIDNUM}})
 same => n,Set(CALLERID(name)=${{CIDNUM}})
 same => n,Set(CONNECTEDLINE(num)=${{CIDNUM}})
 same => n,Set(CONNECTEDLINE(name)=${{CIDNUM}})
 same => n,Set(PJSIP_HEADER(add,P-Asserted-Identity)=<sip:+${{LEADNUM}}@{server_ip}>)
 same => n,Goto(xferdial,1)

exten => xferdial,1,StopPlaytones()
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CALLERID(num)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CALLERID(name)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CONNECTEDLINE(num)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CONNECTEDLINE(name)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(PJSIP_HEADER(add,P-Asserted-Identity)=<sip:+${{LEADNUM}}@{server_ip}>))
 same => n,ExecIf($["${{LEN(${{P1RUN}})}}" = "0"]?Set(P1RUN=${{DB(press1/runs/${{FILTER(0-9,${{LEADNUM}})}})}}))
 same => n,ExecIf($["${{LEN(${{P1RUN}})}}" = "0"]?Set(P1RUN=0))
 same => n,System(/bin/sh -c 'mkdir -p {DIAL_STATS_DIR}/${{P1RUN}} && echo 1 >> {DIAL_STATS_DIR}/${{P1RUN}}/press1 &' )
 same => n,Dial(PJSIP/{xfer_ext}@3cx,120,tT)
 same => n,Hangup()

exten => t,1,Hangup()
exten => i,1,Hangup()
"""


def ensure_press1_dialplan(xfer_ext: str | None = None) -> str:
    """Idempotently apply press-1 IVR dialplan + BitCall DTMF on the dial server."""
    import base64

    if xfer_ext is None:
        xfer_ext = profile(get_threex_target())["ext"]
    block = _press1_ivr_dialplan(server_ip=SERVER_IP, xfer_ext=xfer_ext, sound=SOUND_NAME)
    b64 = base64.b64encode(block.encode()).decode()
    ext_conf = "/etc/asterisk/extensions.conf"
    ast_conf = "/etc/asterisk/asterisk.conf"
    out = run_remote(
        f"python3 <<'PY'\n"
        f"import base64, re\n"
        f"from pathlib import Path\n"
        f"block = base64.b64decode('{b64}').decode()\n"
        f"ext = Path('{ext_conf}')\n"
        f"text = ext.read_text()\n"
        f"text = re.sub(r'\\[press1-ivr\\][\\s\\S]*?(?=\\n\\[default\\]|\\Z)', block + '\\n', text, count=1)\n"
        f"if '[press1-ivr]' not in text:\n"
        f"    text = text.rstrip() + '\\n\\n' + block + '\\n'\n"
        f"ext.write_text(text)\n"
        f"p = Path('{PJSIP_CONF}')\n"
        f"t = p.read_text()\n"
        f"m = re.search(r'(\\[bitcall\\][\\s\\S]*?)(?=\\n\\[|\\Z)', t)\n"
        f"if m:\n"
        f"    b = m.group(1)\n"
        f"    b = re.sub(r'dtmf_mode=\\w+', 'dtmf_mode=rfc4733', b, count=1) if 'dtmf_mode=' in b else b.rstrip() + '\\ndtmf_mode=rfc4733\\n'\n"
        f"    if 'telephone-event' not in b:\n"
        f"        b = re.sub(r'allow=alaw', 'allow=alaw\\nallow=telephone-event', b, count=1)\n"
        f"    t = t[:m.start(1)] + b + t[m.end(1):]\n"
        f"    p.write_text(t)\n"
        f"ac = Path('{ast_conf}')\n"
        f"at = ac.read_text()\n"
        f"if 'live_dangerously' not in at:\n"
        f"    at = re.sub(r'(\\[options\\]\\s*\\n)', r'\\1live_dangerously = yes\\n', at, count=1)\n"
        f"else:\n"
        f"    at = re.sub(r';?live_dangerously\\s*=.*', 'live_dangerously = yes', at)\n"
        f"ac.write_text(at)\n"
        f"Path('{DIAL_STATS_DIR}').mkdir(parents=True, exist_ok=True)\n"
        f"print('OK: press1-ivr dialplan + bitcall rfc4733')\n"
        f"PY\n"
        f"asterisk -rx 'module reload res_pjsip.so' >/dev/null 2>&1; "
        f"asterisk -rx 'dialplan reload' >/dev/null; "
        f"asterisk -rx 'dialplan show ivr@press1-ivr' | grep -E 'Background|WaitExten' | head -2",
        timeout=60,
    )
    return out.strip()


def settings_summary() -> dict[str, str]:
    target = get_threex_target()
    p = profile(target)
    return {
        "threex_target": target,
        "threex_label": p["label"],
        "threex_fqdn": p["fqdn"],
        "threex_host": p["host"],
        "threex_ext": p["ext"],
        "sound_name": SOUND_NAME,
        "call_gap": str(CALL_GAP_SEC),
        "batch_size": str(BATCH_SIZE),
        "batch_pause": str(BATCH_PAUSE_SEC),
        "max_concurrent": str(MAX_CONCURRENT),
        "dialer_cap": str(DIALER_CONCURRENT_CAP),
    }


def test_numbers() -> list[str]:
    raw = os.getenv("VICIDIAL_TEST_NUMBERS", "447769799593")
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


def _server_dial_script(run_id: str) -> str:
    batch, pause, gap = BATCH_SIZE, BATCH_PAUSE_SEC, CALL_GAP_SEC
    return f"""#!/bin/bash
set +e
STOP={DIAL_STOP}
PAUSEFILE={DIAL_PAUSE}
STARTED={DIAL_STARTED}
FAILED={DIAL_FAILED}
NUMFILE={DIAL_NUMBERS}
LOG={DIAL_LOG}
LOCK={DIAL_LOCK}
# run_id is baked in at generation time — never read from a file that a
# load-stressed launch command might have failed to write. Fall back to the
# file only if somehow blank.
RUNID={run_id}
[ -z "$RUNID" ] && RUNID=$(cat {DIAL_RUN_ID} 2>/dev/null)
[ -z "$RUNID" ] && RUNID=0
DONE=/tmp/press1_dial_done_${{RUNID}}.txt
BATCH={batch}
PAUSE={pause}
GAP={gap}
CAP={DIALER_CONCURRENT_CAP}
AU_CALLER_ID={AU_CALLER_ID}
SPOOLDIR=/var/spool/asterisk/outgoing
TMPDIR=/var/spool/asterisk/tmp
wait_if_paused() {{
  while [ -f "$PAUSEFILE" ]; do
    [ -f "$STOP" ] && exit 0
    sleep 1
  done
}}
exec 9>"$LOCK"
flock -n 9 || {{ echo "$(date '+%Y-%m-%d %H:%M:%S') skip duplicate dialer (locked)" >>"$LOG"; exit 0; }}
# This dialer owns the lock, so it is the single source of truth for THIS run.
# Publish our run_id so the bot's monitoring always matches, even if the
# launch-time init command was cut short by the SSH channel closing under load.
echo "$RUNID" > {DIAL_RUN_ID}
mkdir -p {DIAL_STATS_DIR}/"$RUNID"
if [ ! -s "$DONE" ]; then
  : > "$DONE"
  echo 0 > "$STARTED"
  echo 0 > "$FAILED"
  : > {DIAL_STATS_DIR}/"$RUNID"/answered
  : > {DIAL_STATS_DIR}/"$RUNID"/press1
else
  s=$(wc -l < "$DONE" 2>/dev/null || echo 0); echo "$s" > "$STARTED"
  touch {DIAL_STATS_DIR}/"$RUNID"/answered {DIAL_STATS_DIR}/"$RUNID"/press1 2>/dev/null
fi
chown -R asterisk:asterisk {DIAL_STATS_DIR}/"$RUNID" 2>/dev/null
chmod 664 {DIAL_STATS_DIR}/"$RUNID"/answered {DIAL_STATS_DIR}/"$RUNID"/press1 2>/dev/null
batch_n=0
while IFS= read -r num || [ -n "$num" ]; do
  wait_if_paused
  [ -f "$STOP" ] && exit 0
  num=$(echo "$num" | tr -d '\\r' | tr -d ' ')
  [ -z "$num" ] && continue
  grep -qxF "$num" "$DONE" 2>/dev/null && continue
  # Optional concurrency gate. CAP=0 means unlimited provider capacity, so only
  # the placement rate (GAP/BATCH/PAUSE) controls dialing speed.
  while [ "$CAP" -gt 0 ]; do
    wait_if_paused
    [ -f "$STOP" ] && exit 0
    live=$(asterisk -rx "core show channels concise" 2>/dev/null | grep -ci 'bitcall')
    [ "$live" -lt "$CAP" ] && break
    sleep 1
  done
  digits=$(echo "$num" | tr -cd '0-9')
  asterisk -rx "database put press1 runs/${{digits}} ${{RUNID}}" >>"$LOG" 2>&1
  asterisk -rx "database put press1 lead ${{num}}" >>"$LOG" 2>&1
  callfile="$TMPDIR/press1_${{RUNID}}_${{digits}}_$$.call"
  mkdir -p "$TMPDIR" "$SPOOLDIR" 2>/dev/null
  {{
    echo "Channel: PJSIP/${{num}}@bitcall"
    if [ -n "$AU_CALLER_ID" ]; then
      printf 'CallerID: "%s" <%s>\\n' "$AU_CALLER_ID" "$AU_CALLER_ID"
    fi
    cat <<'CALLBODY'
MaxRetries: 0
RetryTime: 60
WaitTime: 30
Context: press1-ivr
Extension: NUMPLACEHOLDER
Priority: 1
Setvar: LEADNUM=NUMPLACEHOLDER
CALLBODY
  }} | sed "s/NUMPLACEHOLDER/${{num}}/g" > "$callfile"
  if chown asterisk:asterisk "$callfile" 2>/dev/null && chmod 0640 "$callfile" 2>/dev/null && mv "$callfile" "$SPOOLDIR/" 2>>"$LOG"
  then
    echo "$num" >>"$DONE"
    s=$(wc -l < "$DONE" 2>/dev/null || echo 0); echo "$s" > "$STARTED"
    echo "$(date '+%Y-%m-%d %H:%M:%S') ok $num" >>"$LOG"
  else
    f=$(cat "$FAILED" 2>/dev/null || echo 0); echo $((f+1)) > "$FAILED"
    echo "$(date '+%Y-%m-%d %H:%M:%S') fail $num" >>"$LOG"
  fi
  batch_n=$((batch_n+1))
  wait_if_paused
  sleep "$GAP"
  if [ "$batch_n" -ge "$BATCH" ]; then
    batch_n=0
    wait_if_paused
    sleep "$PAUSE"
  fi
done < "$NUMFILE"
touch "$STOP"
echo "$(date '+%Y-%m-%d %H:%M:%S') finished run $RUNID" >>"$LOG"
exit 0
"""


def _fetch_server_dial_state(expected_run_id: str | None = None) -> dict[str, int | bool | str]:
    """Counter files + pgrep + live channels in one SSH round-trip."""
    if expected_run_id:
        press1_path = _stats_press1_path(expected_run_id)
        answered_path = _stats_answered_path(expected_run_id)
    else:
        press1_path = DIAL_RUN_PRESS1
        answered_path = DIAL_RUN_ANSWERED
    raw = run_remote(
        f"cat {DIAL_TOTAL} 2>/dev/null || echo 0; "
        f"cat {DIAL_STARTED} 2>/dev/null || echo 0; "
        f"cat {DIAL_FAILED} 2>/dev/null || echo 0; "
        f"cat {DIAL_RUN_ID} 2>/dev/null || echo; "
        f"wc -l < {press1_path} 2>/dev/null || echo 0; "
        f"wc -l < {answered_path} 2>/dev/null || echo 0; "
        f"echo 0; "
        f"echo 0; "
        f"ps aux 2>/dev/null | grep -c '[b]ash {DIAL_SCRIPT}' || echo 0; "
        f"wc -l < {DIAL_NUMBERS} 2>/dev/null || echo 0; "
        f"rid=$(cat {DIAL_RUN_ID} 2>/dev/null); "
        f"if [ -n \"$rid\" ] && [ -f /tmp/press1_dial_done_${{rid}}.txt ]; then wc -l < /tmp/press1_dial_done_${{rid}}.txt; else echo 0; fi; "
        f"test -f {DIAL_PAUSE} && echo 1 || echo 0",
        timeout=25,
    ).strip().splitlines()
    vals: list[str] = []
    for ln in raw[:12]:
        vals.append((ln.strip().split() or ["0"])[-1])
    while len(vals) < 12:
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
    paused = int(vals[11] or 0) > 0

  # Empty server run_id must not match — otherwise stale global counters bleed in.
    run_match = (
        not expected_run_id
        or (bool(server_run_id) and server_run_id == expected_run_id)
    )
    if not run_match:
        started_raw = 0
        failed = 0
        press1 = 0
        answered = 0
        done_count = 0

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
        "paused": paused,
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
  campaign_cid='{AU_CALLER_ID or ""}',
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
    """Full international digits for BitCall originate (same format as /testcall)."""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if digits.startswith("44") or digits.startswith("61"):
        return digits
    if digits.startswith("0"):
        # AU mobile 04xxxxxxxx -> 614xxxxxxxx
        if digits.startswith("04") and len(digits) == 10:
            return "61" + digits[1:]
        return "44" + digits[1:]
    return "44" + digits


def originate_press1(phone: str) -> str:
    """Place one outbound call — identical path to /testcall."""
    ensure_press1_dialplan()
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
            f"wc -l < {_stats_press1_path(run_id)} 2>/dev/null || echo 0; "
            f"wc -l < {_stats_answered_path(run_id)} 2>/dev/null || echo 0",
            timeout=20,
        ).strip().splitlines()
        press1 = int((raw[0] if raw else "0").strip().split()[-1])
        answered = int((raw[1] if len(raw) > 1 else "0").strip().split()[-1])
        return press1, answered
    except Exception:
        return 0, 0


def _dial_state_label(running: bool, total: int, left: int, failed: int, *, paused: bool = False) -> str:
    if paused and total > 0:
        return "paused"
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
        paused = bool(state.get("paused"))
        left = max(0, total - started - failed)
        dial_state = _dial_state_label(running, total, left, failed, paused=paused)

        press1 = int(state["press1"])
        answered = int(state["answered"])
        if run_id and bool(state.get("run_match", True)):
            if press1 == 0 and answered == 0 and (running or started > 0):
                press1, answered = _fetch_outcome_stats(run_id)
        elif not run_id:
            press1, answered = 0, 0

        # Answered/press-1 can never exceed dialed for the current run.
        if started > 0:
            answered = min(answered, started)
        if answered > 0:
            press1 = min(press1, answered)

        if dial_state == "running":
            prog["running"] = True
            prog.pop("stalled", None)
            prog.pop("paused", None)
        elif dial_state == "paused":
            prog["running"] = True
            prog["paused"] = True
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
        paused = bool(prog.get("paused"))
        left = max(0, total - started - failed)
        dial_state = _dial_state_label(running, total, left, failed, paused=paused)
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
        "paused": "Y" if dial_state == "paused" else "N",
    }


def _stop_remote_dialer() -> None:
    try:
        run_remote(
            f"touch {DIAL_STOP} 2>/dev/null; rm -f {DIAL_PAUSE} 2>/dev/null; "
            f"pkill -9 -f press1_dial_run.sh 2>/dev/null; true",
            timeout=20,
        )
    except Exception:
        pass


def _dialer_process_count() -> int:
    try:
        raw = run_remote(
            f"ps aux 2>/dev/null | grep -c '[b]ash {DIAL_SCRIPT}' || echo 0",
            timeout=15,
        ).strip().split()[-1]
        return int(raw or 0)
    except Exception:
        return 0


def _campaign_counters() -> tuple[int, int, int, int]:
    """total, started, failed, left"""
    try:
        raw = run_remote(
            f"cat {DIAL_TOTAL} 2>/dev/null || echo 0; "
            f"cat {DIAL_STARTED} 2>/dev/null || echo 0; "
            f"cat {DIAL_FAILED} 2>/dev/null || echo 0",
            timeout=15,
        ).strip().splitlines()
        total = int((raw[0] if raw else "0").strip().split()[-1])
        started = int((raw[1] if len(raw) > 1 else "0").strip().split()[-1])
        failed = int((raw[2] if len(raw) > 2 else "0").strip().split()[-1])
        left = max(0, total - started - failed)
        return total, started, failed, left
    except Exception:
        return 0, 0, 0, 0


def _dial_script_supports_pause() -> bool:
    try:
        raw = run_remote(
            f"grep -c wait_if_paused {DIAL_SCRIPT} 2>/dev/null || echo 0",
            timeout=15,
        ).strip().split()[-1]
        return int(raw or 0) > 0
    except Exception:
        return False


def pause_dial_campaign() -> dict[str, str]:
    """Pause placing new calls; live calls continue. Does not kill the dialer."""
    running = _dialer_process_count()
    total, started, failed, left = _campaign_counters()
    if running < 1 and left <= 0:
        raise RuntimeError("No active campaign to pause")
    if running < 1 and left > 0:
        raise RuntimeError("Campaign stalled — use /unpause to resume dialing")
    if _dial_script_supports_pause():
        run_remote(f"touch {DIAL_PAUSE}", timeout=15)
    else:
        # Older dialer scripts ignore PAUSEFILE — stop gracefully without pkill.
        run_remote(f"touch {DIAL_STOP}", timeout=15)
    return {
        "paused": "Y",
        "dialed": str(started),
        "left": str(left),
        "total": str(total),
        "failed": str(failed),
    }


def unpause_dial_campaign() -> dict[str, str]:
    """Resume a paused campaign, or restart the dialer if it exited with leads left."""
    run_remote(f"rm -f {DIAL_PAUSE}", timeout=15)
    total, started, failed, left = _campaign_counters()
    if total <= 0:
        raise RuntimeError("No campaign loaded on server")
    if left <= 0:
        raise RuntimeError("Nothing left to dial")
    if _dialer_process_count() < 1:
        if not run_remote(f"test -f {DIAL_NUMBERS} && echo yes || echo no", timeout=15).strip().endswith("yes"):
            raise RuntimeError("Cannot resume — numbers file missing. Upload a list and /run again.")
        run_remote(f"rm -f {DIAL_STOP}", timeout=15)
        _start_dial_script()
    elif _dial_script_supports_pause():
        pass  # pause file already removed; running dialer continues on its own
    else:
        raise RuntimeError("Dialer still running — wait a few seconds and try /unpause again")
    return {
        "paused": "N",
        "dialed": str(started),
        "left": str(left),
        "total": str(total),
        "failed": str(failed),
    }


def _start_dial_script() -> None:
    """Start dial script detached; verify with pgrep in a separate SSH call."""
    import time

    run_remote(f"chmod +x {DIAL_SCRIPT}", timeout=15)
    run_remote(
        f"rm -f {DIAL_STOP}; rm -f {DIAL_PAUSE}; nohup setsid bash {DIAL_SCRIPT} >>{DIAL_LOG} 2>&1 </dev/null &",
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
    ensure_press1_dialplan()
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

    # Write the run_id in its own tiny, reliable call FIRST. The big init block
    # below can be cut short when the SSH channel closes under heavy call load
    # (paramiko reports exit -1, which we treat as success), so we must never
    # depend on it to persist the run_id.
    run_remote(f"echo {run_id} > {DIAL_RUN_ID}", timeout=15)

    run_remote(
        f"touch {DIAL_STOP}; "
        f"pkill -9 -f press1_dial_run.sh 2>/dev/null; true; "
        f"sleep 1; "
        f"pkill -9 -f AST_VDauto_dial 2>/dev/null; true; "
        f"mysql asterisk -e \"UPDATE vicidial_campaigns SET active='N' WHERE campaign_id='{CAMPAIGN}'\" 2>/dev/null; "
        f"rm -f {DIAL_STOP}; rm -f {DIAL_PAUSE}; "
        f"echo 0 > {DIAL_STARTED}; echo 0 > {DIAL_FAILED}; "
        # Self-clean: drop stale per-run dirs, any bare root counters, and the
        # accumulated per-number run tags so stats always start from a clean slate.
        f"mkdir -p {DIAL_STATS_DIR}; "
        f"rm -f {DIAL_STATS_DIR}/answered {DIAL_STATS_DIR}/press1 2>/dev/null; "
        f"find {DIAL_STATS_DIR} -mindepth 1 -maxdepth 1 -type d ! -name {run_id} -exec rm -rf {{}} + 2>/dev/null; "
        f"asterisk -rx 'database deltree press1/runs' >/dev/null 2>&1; "
        f"mkdir -p {DIAL_STATS_DIR}/{run_id}; "
        f": > {_stats_answered_path(run_id)}; : > {_stats_press1_path(run_id)}; "
        f"chown -R asterisk:asterisk {DIAL_STATS_DIR} 2>/dev/null; "
        f"chmod 664 {_stats_answered_path(run_id)} {_stats_press1_path(run_id)} 2>/dev/null; "
        # Legacy global counters (kept empty for backwards compatibility).
        f": > {DIAL_RUN_PRESS1}; : > {DIAL_RUN_ANSWERED}; "
        f"chown asterisk:asterisk {DIAL_RUN_PRESS1} {DIAL_RUN_ANSWERED} 2>/dev/null; "
        f"chmod 664 {DIAL_RUN_PRESS1} {DIAL_RUN_ANSWERED} 2>/dev/null; "
        f"echo {run_id} > {DIAL_RUN_ID}; "
        f"rm -f /tmp/press1_dial_done_*.txt; "
        f"rm -f /tmp/press1_dial_done_{run_id}.txt; "
        f"echo {len(numbers)} > {DIAL_TOTAL}; "
        f"date '+%Y-%m-%d %H:%M:%S' > {DIAL_RUN_MARK}; "
        f"asterisk -rx 'logger notice PRESS1_RUN_START {run_id}' 2>/dev/null; "
        f"echo \"=== RUN {run_id} {len(numbers)} leads $(date -Iseconds) ===\" >> {DIAL_LOG}",
        timeout=30,
    )

    script_body = _server_dial_script(run_id)
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

    # Trust the server's run_id as the source of truth for monitoring. The dialer
    # reads the run_id from the file at startup; reading it back here guarantees the
    # bot's expected run_id matches what the dialer actually used, so a successful
    # run can never be zeroed by a run_id mismatch.
    try:
        actual = run_remote(f"cat {DIAL_RUN_ID} 2>/dev/null", timeout=15).strip().split()
        if actual and actual[-1]:
            progress["run_id"] = actual[-1]
    except Exception:
        pass


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
