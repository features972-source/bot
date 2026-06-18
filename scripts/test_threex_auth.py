"""Test 3CX OAuth for bot 1 or 2. Usage: python scripts/test_threex_auth.py [.env.bot2]"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

env_file = sys.argv[1] if len(sys.argv) > 1 else ".env"
os.environ["BOT_ENV_FILE"] = env_file

from config import load_settings  # noqa: E402
from threex_token import fetch_token  # noqa: E402


async def main() -> None:
    settings = load_settings()
    token = await fetch_token(settings)
    print(f"Env: {env_file}")
    print(f"FQDN: {settings.threex_fqdn}")
    print(f"Client ID: {settings.threex_client_id}")
    if token:
        print("Token: OK")
    else:
        print("Token: FAILED - check API key + Client ID in 3CX Admin -> Integrations -> API")


if __name__ == "__main__":
    asyncio.run(main())
