"""SSH control of VICIdial press-1 server (BitCall + 3CX xfer)."""

from __future__ import annotations

import base64
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
CPS = int(os.getenv("VICIDIAL_CPS", "5"))
MIN_PHONE_DIGITS = 9


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
    with ssh_connect() as client:
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if code != 0:
            raise RuntimeError((err or out or f"remote exit {code}").strip())
        return out


def mysql(script: str) -> str:
    b64 = base64.b64encode(script.encode()).decode()
    return run_remote(f"echo {b64} | base64 -d | mysql asterisk")


def ping() -> str:
    return run_remote("echo ok && asterisk -rx 'pjsip show registrations' | grep -i bitcall | head -1")


def add_leads(phones: list[str]) -> int:
    added = 0
    chunks: list[str] = []
    for phone in phones:
        code, num = normalize_uk(phone)
        if len(num) < MIN_PHONE_DIGITS:
            continue
        chunks.append(
            f"""
INSERT INTO vicidial_list (entry_date,status,list_id,phone_code,phone_number,first_name,last_name)
SELECT NOW(),'NEW',{LIST_ID},'{code}','{num}','Lead',''
FROM DUAL WHERE NOT EXISTS (
  SELECT 1 FROM vicidial_list WHERE list_id={LIST_ID} AND phone_number='{num}'
);
UPDATE vicidial_list SET status='NEW', called_count=0
WHERE list_id={LIST_ID} AND phone_number='{num}';
"""
        )
        added += 1
    if not chunks:
        return 0
    mysql("\n".join(chunks))
    return added


def refill_hopper() -> None:
    mysql(
        f"""
DELETE FROM vicidial_hopper WHERE campaign_id='{CAMPAIGN}';
INSERT INTO vicidial_hopper (lead_id, campaign_id, status, list_id, gmt_offset_now, state, alt_dial, priority, source, vendor_lead_code)
SELECT lead_id, '{CAMPAIGN}', 'READY', list_id, gmt_offset_now, state, 'NONE', 0, 'M', vendor_lead_code
FROM vicidial_list WHERE list_id={LIST_ID} AND status='NEW';
"""
    )


def start_campaign() -> None:
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
  hopper_level='50',
  campaign_cid='443300592867',
  survey_first_audio_file='{SOUND_NAME}',
  survey_xfer_exten='8000',
  survey_dtmf_digits='1',
  survey_method='EXTENSION',
  survey_wait_sec='15'
WHERE campaign_id='{CAMPAIGN}';
UPDATE vicidial_live_agents SET status='READY', outbound_autodial='Y', on_hook_agent='Y'
WHERE campaign_id='{CAMPAIGN}' AND user='admin';
"""
    )
    refill_hopper()


def stop_campaign() -> None:
    mysql(
        f"""
UPDATE vicidial_live_agents SET status='PAUSED', outbound_autodial='N'
WHERE campaign_id='{CAMPAIGN}';
UPDATE vicidial_campaigns SET active='N' WHERE campaign_id='{CAMPAIGN}';
"""
    )


def get_status() -> dict[str, str]:
    return get_live_stats()


def get_live_stats() -> dict[str, str]:
    raw = mysql(
        f"""
SELECT 'hopper' AS k, COUNT(*) AS v FROM vicidial_hopper WHERE campaign_id='{CAMPAIGN}'
UNION ALL SELECT 'live', COUNT(*) FROM vicidial_auto_calls WHERE campaign_id='{CAMPAIGN}'
UNION ALL SELECT 'new_leads', COUNT(*) FROM vicidial_list WHERE list_id={LIST_ID} AND status='NEW'
UNION ALL SELECT 'dialed_today', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND call_date >= CURDATE()
UNION ALL SELECT 'press1_today', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND call_date >= CURDATE() AND status IN ('SVYEXT','XFER','SVYCLM')
UNION ALL SELECT 'answered_today', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND call_date >= CURDATE() AND length_in_sec >= 5;
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


def test_calls(numbers: list[str] | None = None) -> list[str]:
    nums = numbers or test_numbers()
    placed: list[str] = []
    for num in nums:
        digits = re.sub(r"\D", "", num)
        if not digits:
            continue
        run_remote(
            f"asterisk -rx 'channel originate PJSIP/{digits}@bitcall extension s@press1-ivr'"
        )
        placed.append(digits)
    return placed
