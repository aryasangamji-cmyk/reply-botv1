import aiosqlite
from pathlib import Path

SCHEMA = '''
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT, legacy_reference TEXT, name TEXT NOT NULL, icon TEXT,
 telegram_chat_id TEXT, telegram_chat_link TEXT, enabled INTEGER NOT NULL DEFAULT 1, auto_link INTEGER NOT NULL DEFAULT 0,
 description TEXT, fee TEXT, duration TEXT, timing TEXT, image_path TEXT, demo_text TEXT, demo_link TEXT,
 payment_text TEXT, payment_link TEXT, faq_text TEXT, faq_image_path TEXT, link_generation_config TEXT,
 request_link TEXT, aliases_json TEXT, extra_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS courses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, legacy_reference TEXT, name TEXT NOT NULL, description TEXT, fee TEXT, duration TEXT,
 timing TEXT, image_path TEXT, enabled INTEGER NOT NULL DEFAULT 1, aliases_json TEXT, source_link TEXT,
 telegram_chat_id TEXT, private_access_link TEXT, extra_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS shortcuts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, extra_json TEXT);
CREATE TABLE IF NOT EXISTS shortcut_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT, shortcut_id INTEGER NOT NULL REFERENCES shortcuts(id) ON DELETE CASCADE,
 item_order INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, text_content TEXT, media_path TEXT, caption TEXT,
 button_data_json TEXT, extra_json TEXT);
CREATE TABLE IF NOT EXISTS ai_knowledge (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, extra_json TEXT);
CREATE TABLE IF NOT EXISTS ai_rules (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, rule_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS ai_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_user_id TEXT, customer_message TEXT, language TEXT, intent_json TEXT,
 entity_json TEXT, configured_match TEXT, action TEXT, business_data_used TEXT, response_text TEXT, fallback TEXT,
 negotiation_status TEXT, confidence_status TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
'''

async def init_db(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        await db.commit()

async def counts(path: str):
    async with aiosqlite.connect(path) as db:
        out = {}
        for table in ('batches','courses','shortcuts','shortcut_items','ai_knowledge','ai_rules','ai_logs'):
            cur = await db.execute(f'SELECT COUNT(*) FROM {table}')
            out[table] = (await cur.fetchone())[0]
        return out

async def recent_logs(path: str, limit: int = 8):
    async with aiosqlite.connect(path) as db:
        cur = await db.execute('''SELECT customer_message, intent_json, entity_json, action, response_text, created_at
                                  FROM ai_logs ORDER BY id DESC LIMIT ?''', (limit,))
        return await cur.fetchall()

async def add_log(path: str, *, user_id, message, language, intents, entity, configured_match,
                  action, business_data_used, response, fallback, negotiation_status, confidence_status):
    import json
    async with aiosqlite.connect(path) as db:
        await db.execute('''INSERT INTO ai_logs
          (telegram_user_id, customer_message, language, intent_json, entity_json, configured_match, action,
           business_data_used, response_text, fallback, negotiation_status, confidence_status)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
          (str(user_id) if user_id is not None else None, message, language, json.dumps(intents, ensure_ascii=False),
           json.dumps(entity, ensure_ascii=False) if entity is not None else None, configured_match, action,
           business_data_used, response, fallback, negotiation_status, confidence_status))
        await db.commit()


async def business_context(path: str) -> str:
    lines = []
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("""SELECT * FROM batches WHERE enabled=1 ORDER BY id""") as cur:
            for r in await cur.fetchall():
                lines.append("BATCH | " + " | ".join(
                    f"{k}={r[k]}" for k in r.keys()
                    if r[k] is not None and k not in ("created_at","updated_at")
                ))

        async with db.execute("""SELECT * FROM courses WHERE enabled=1 ORDER BY id""") as cur:
            for r in await cur.fetchall():
                lines.append("COURSE | " + " | ".join(
                    f"{k}={r[k]}" for k in r.keys()
                    if r[k] is not None and k not in ("created_at","updated_at")
                ))

        async with db.execute("""SELECT * FROM shortcuts WHERE enabled=1 ORDER BY id""") as cur:
            shortcuts = await cur.fetchall()
            for r in shortcuts:
                lines.append("SHORTCUT | " + " | ".join(
                    f"{k}={r[k]}" for k in r.keys() if r[k] is not None
                ))

        async with db.execute("""SELECT * FROM shortcut_items WHERE enabled=1 ORDER BY shortcut_id,item_order""") as cur:
            items = await cur.fetchall()
            for r in items:
                lines.append("SHORTCUT_ITEM | " + " | ".join(
                    f"{k}={r[k]}" for k in r.keys() if r[k] is not None
                ))

    return "\n".join(lines) if lines else "NO AUTHORITATIVE BUSINESS DATA AVAILABLE"
