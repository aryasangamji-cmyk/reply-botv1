# Phase 17 — Train AI DB Hotfix + Information System

- Fixes the missing `ai_training_read_requests` table in the main-account reader by initializing all training tables in `run_train_reader.py`.
- Adds a dedicated ℹ️ Information System in the admin panel.
- Admin can add information pages with Title, Aliases and complete free-form Details (price, year, duration, rules, links, etc.).
- Saved information is injected into the existing AI context as authoritative admin information; no second AI call is introduced.
- Supports list/view/edit/enable-disable/delete.
- Existing business data, shortcuts, batches, courses and AI systems are preserved.
- Does not use or recreate BM3.

Install both `app/bot.py` and `run_train_reader.py` from this patch. Restart both processes after installation.
