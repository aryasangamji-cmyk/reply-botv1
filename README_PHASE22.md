# Phase 22 — Correct Combo + Pre Access Admin Installation

This patch corrects the Phase 21 packaging/path issue: the bot module is placed at `app/bot.py`, which is the path used by the running application.

Includes the Combo and Pre Access UI/workflows from Phase 21, including:
- 🎁 Combos admin page
- Add/list/edit/delete/toggle combos
- price/details/caption
- multiple ordered images
- 🔓 Pre Access admin page
- configurable duration
- deterministic admin/internal /pre routing
- existing batch/course command routing

IMPORTANT: This patch only replaces `app/bot.py`. It does not replace the database, `db.py`, `ai.py`, `actions.py`, or `batch_manager.py`.
