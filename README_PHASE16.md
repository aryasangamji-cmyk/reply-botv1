# Phase 16 — Train AI Main-Account Reader

This patch makes Read All Chat / Read In Range actually process Telegram history through a Telethon user-account session and stores up to 10 extracted learnings. It also monitors outgoing human replies when Auto Read is ON.

## Required .env additions
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_PHONE=+91...
TRAIN_READER_SESSION=data/train_reader.session

Existing TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, OPENAI_MODEL, DATABASE_PATH and ADMIN_USER_IDS are reused.

Start with: .venv/bin/python run_train_reader.py
The first run may ask for the Telegram login code and 2FA password interactively. Never put the login code/password in .env.

Read All Chat resolves the supplied message link and reads the full available message history. Read In Range requires FIRST and LAST links from the same chat and reads inclusive FIRST..LAST.

Auto Read monitors outgoing messages from the connected personal account when enabled and excludes message IDs registered in ai_generated_message_ids. The existing Bot API AI does not yet register such IDs; future main-account AI integration should use that table when sending AI replies.
