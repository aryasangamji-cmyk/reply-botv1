# Phase 24 — Combo Rich Caption + Media Fix

This patch fixes the Combo system without replacing the business database.

## Fixed
- Combo captions are now stored as native Telegram text + MessageEntity data.
- Bold, italic, underline, strikethrough, inline code, pre/code blocks and blockquotes are preserved.
- Embedded text links (`text_link`) are preserved with the URL hidden.
- Existing Phase 23 HTML captions are sent as HTML instead of being escaped by the generic URL renderer.
- Multiple saved images remain an ordered Telegram media album with one common caption on the first image.
- Existing combo records automatically receive `caption_entities_json` on first access; no data reset is performed.
- Combo edit-caption uses the same native entity storage.

## Important
Do NOT delete the database or media directory. The patch only replaces `app/bot.py`.
