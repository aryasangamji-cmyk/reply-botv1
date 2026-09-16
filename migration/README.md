# Business Data Migration

This directory is intentionally empty of old data.

When the old database/project is supplied:
1. Inspect it read-only.
2. Identify the 70 Batch records, 15 Course records, 5 Shortcuts and 21 Shortcut Items.
3. Explicitly exclude all BM3 tables/state/logic.
4. Locate referenced media files.
5. Copy only required business media into `data/media/`.
6. Transform records into the new schema.
7. Validate counts, fields, ordering and media references.
8. Import into `data/bot.sqlite3`.
9. Produce a migration report.

Never overwrite or modify the original old database.
Never copy the old database file into this project.
Never migrate BM3.
