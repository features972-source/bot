import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from config import load_settings
from threex_token import fetch_token
import httpx


async def main() -> None:
    settings = load_settings()
    token = await fetch_token(settings)
    if not token:
        print("no token")
        return
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://{settings.threex_fqdn}/xapi/v1"
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    urls = [
        (f"{base}/CallHistoryView", {"$top": "3", "$orderby": "SegmentStartTime desc"}),
        (f"{base}/CallHistoryView", {"$top": "3"}),
        (
            f"{base}/ReportCallLogData/Pbx.GetCallLogData(periodFrom={start},periodTo={end})",
            {"$top": "3"},
        ),
        (f"{base}/ActiveCalls", None),
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        for url, params in urls:
            r = await client.get(url, params=params, headers=headers)
            print(url.split("/xapi/v1/")[-1], r.status_code)
            if r.status_code < 400:
                data = r.json()
                items = data.get("value", data) if isinstance(data, dict) else data
                if isinstance(items, list) and items:
                    print("  keys:", list(items[0].keys())[:15])
            else:
                print(" ", r.text[:120])


asyncio.run(main())
