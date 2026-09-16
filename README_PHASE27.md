# Phase 27 — Combo Photo Registration Workflow

This patch corrects the Combo photo-message builder so every uploaded photo is explicitly registered and the admin has a clear button-driven flow:

1. Add Message → Photo Message
2. Send Photo 1 → photo is downloaded and registered immediately
3. Choose **Add Another Photo** or **Done — Photos Finished**
4. Each additional photo is registered immediately
5. Done → Caption
6. Caption preserves Telegram-native formatting/entities
7. Embedded Link asks for exact word/line, then URL immediately
8. Save Message
9. Add another message or open the Combo

The patch does not delete or replace the database/media directory.
