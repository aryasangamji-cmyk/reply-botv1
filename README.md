# reply-botv1

Telegram AI sales bot source imported from the current production server.

## Current State

- Current production baseline: V34
- V35 was tested in isolation and did not install
- Production server path: `/opt/new-ai-bot`
- Main service: `new-ai-bot.service`

Read `CODEX_HANDOFF.md` completely before changing behavior. It contains the
architecture notes, protected subsystem hashes, known regressions, failed V35
history, and the next engineering task.

## Repository Rules

- Do not commit `.env`, databases, Telegram session files, logs, media, or
  `app/chat_history_seed.json`.
- Keep runtime customer data on the server only.
- Preserve the protected negotiation, cart, payment, Combo, reply-context, and
  main-account bridge modules unless the handoff task explicitly requires a
  reviewed change.

## Local Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements_main_account.txt
cp .env.example .env
```

Fill `.env` locally only. Never commit real credentials.
