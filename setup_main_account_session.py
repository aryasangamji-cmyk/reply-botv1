import os
from pathlib import Path
from telethon import TelegramClient

api_id = os.getenv("MAIN_ACCOUNT_API_ID","").strip()
api_hash = os.getenv("MAIN_ACCOUNT_API_HASH","").strip()
phone = os.getenv("MAIN_ACCOUNT_PHONE","").strip()
session = os.getenv("MAIN_ACCOUNT_SESSION","data/main_account.session").strip() or "data/main_account.session"

if not api_id or not api_hash:
    raise SystemExit("Set MAIN_ACCOUNT_API_ID and MAIN_ACCOUNT_API_HASH in .env first.")
if not phone:
    raise SystemExit("Set MAIN_ACCOUNT_PHONE in .env first.")

Path(session).parent.mkdir(parents=True, exist_ok=True)
client = TelegramClient(session, int(api_id), api_hash)

async def main():
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"LOGIN_OK user_id={me.id} name={me.first_name or ''} {me.last_name or ''}".strip())
    await client.disconnect()

with client:
    client.loop.run_until_complete(main())
