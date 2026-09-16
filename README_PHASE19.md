# Phase 19 — Train AI Reader Peer Resolution + DB Initialization Hotfix

This patch replaces only `run_train_reader.py` from the Phase 16 reader.

Fixes:
- Creates `ai_training_read_requests` and other required Train AI tables at reader startup.
- Resolves `tg://openmessage?user_id=...&message_id=...` private chats through the connected account's dialogs so Telethon has the required access hash.
- Keeps exact range processing and all existing learning logic.
- Does not modify batches, courses, shortcuts, BM3, or business data.

For a private customer chat, the connected main account must have the customer conversation in its Telegram dialogs. If it is not present, open the chat once in Telegram and retry.
