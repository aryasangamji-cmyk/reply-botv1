# Intent -> Trigger + Deterministic Command Router Patch

This patch is additive to the current New AI Telegram Bot. It does NOT import, copy, or recreate BM3.

## Routing rules

1. Customer batch search/questions remain AI-driven.
2. Human/admin slash commands are handled by the deterministic command router and do not call OpenAI.
3. Course direct commands are deterministic, case-insensitive, and treat spaces and underscores as equivalent.
4. Shortcut triggers are deterministic and do not call OpenAI.
5. A configured AI intent can map to a slash trigger (example: `link_expired` -> `/expire`).
6. When AI recognizes that configured intent, the bot creates the configured slash command in the customer chat, routes it through the same deterministic command executor, and deletes the internal command message after successful routing.
7. The mapped trigger is executed only once; there is no second AI call and the AI-generated free-form response is not sent.
8. Human/customer access to direct Course/Batch/Shortcut commands is restricted to the admin; AI-generated internal commands are explicitly allowed.
9. Batch direct access commands use the saved 24-hour + 1-person configuration.
10. Optional deterministic batch demo commands are supported as `/demo_<batch>` or `/demo Batch Name` for admin/internal routing.

## Admin UI

AI -> `🎯 Intent → Trigger`

Add mapping:
- exact intent name
- slash trigger

Mappings can be viewed, toggled, and deleted.

The AI context receives only the configured intent/trigger mappings. It is instructed to return the exact configured intent name; it does not receive permission to invent commands.

## Validation performed

- Python syntax compilation completed successfully for the patched bot and supporting files.
- Static checks performed for deterministic command routing, intent mapping, command handler registration, and migration-safe shortcut schema handling.
- No BM3 tables, algorithms, or BM3 dependencies were added.
