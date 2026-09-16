import json
import re
import time
from pathlib import Path
import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

DEFAULT_SETTINGS = {
    "master_enabled": False,
    "behavior_enabled": True,
    "keywords_enabled": True,
    "exact_enabled": True,
}

HINGLISH = re.compile(r"\b(bhai|bhaiya|bro|haan|ha|nahi|nhi|kya|kaise|kitna|kitne|kitni|kab|tak|milega|milta|chahiye|chaiye|kar|kr|hai|h|mein|me|iska|iski|isme|ye|wala)\b", re.I)
SOCIAL_PREFIX = re.compile(r"^(?:(?:ok+|okay|acha+|achha+|hello|hi+|hey|namaste|thanks|thank\s*you|thankyou|thx|please|plz|bro|brother|bhai|bhaiya|sir)[\s,!.?-]*)+", re.I)


def _norm(text):
    text = str(text or "").casefold().replace("₹", " rs ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _behavior_clean(text):
    raw = str(text or "").strip()
    cleaned = SOCIAL_PREFIX.sub("", raw).strip()
    return cleaned or raw


async def ensure(path, seed_path=None):
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS chat_history_settings(
            id INTEGER PRIMARY KEY CHECK(id=1),
            master_enabled INTEGER NOT NULL DEFAULT 0,
            behavior_enabled INTEGER NOT NULL DEFAULT 1,
            keywords_enabled INTEGER NOT NULL DEFAULT 1,
            exact_enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("INSERT OR IGNORE INTO chat_history_settings(id,master_enabled,behavior_enabled,keywords_enabled,exact_enabled) VALUES(1,0,1,1,1)")
        await db.execute("""CREATE TABLE IF NOT EXISTS chat_history_exact(
            message_norm TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'ANY',
            intent TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(message_norm,state,intent)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS chat_history_keywords(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50,
            phrases_json TEXT NOT NULL DEFAULT '[]',
            exclude_json TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'ANY',
            enabled INTEGER NOT NULL DEFAULT 1
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS chat_history_hits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            chat_id TEXT,
            mode TEXT NOT NULL,
            intent TEXT NOT NULL,
            message_norm TEXT,
            estimated_ai_calls_saved INTEGER NOT NULL DEFAULT 1
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS chat_history_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        await db.commit()

    if seed_path is None:
        seed_path = Path(__file__).with_name("chat_history_seed.json")
    else:
        seed_path = Path(seed_path)
    if not seed_path.exists():
        return
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    version = str(seed.get("version", 1))
    async with aiosqlite.connect(path) as db:
        row = await (await db.execute("SELECT value FROM chat_history_meta WHERE key='seed_version'" )).fetchone()
        if row and str(row[0]) == version:
            return
        for x in seed.get("exact", []):
            await db.execute("""INSERT INTO chat_history_exact(message_norm,state,intent,source_count,enabled)
                VALUES(?,?,?,?,1)
                ON CONFLICT(message_norm,state,intent) DO UPDATE SET source_count=excluded.source_count""",
                (x["message_norm"], x.get("state","ANY"), x["intent"], int(x.get("source_count",1))))
        await db.execute("DELETE FROM chat_history_keywords")
        for x in seed.get("keywords", []):
            await db.execute("INSERT INTO chat_history_keywords(intent,priority,phrases_json,exclude_json,state,enabled) VALUES(?,?,?,?,?,1)",
                (x["intent"], int(x.get("priority",50)), json.dumps(x.get("phrases",[]),ensure_ascii=False), json.dumps(x.get("exclude",[]),ensure_ascii=False), x.get("state","ANY")))
        await db.execute("INSERT INTO chat_history_meta(key,value) VALUES('seed_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
        await db.execute("INSERT INTO chat_history_meta(key,value) VALUES('source_summary',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(seed.get('source_summary',{})),))
        await db.commit()


async def settings(path):
    await ensure(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM chat_history_settings WHERE id=1")).fetchone()
    if not row:
        return dict(DEFAULT_SETTINGS)
    return {k: bool(row[k]) for k in ('master_enabled','behavior_enabled','keywords_enabled','exact_enabled')}


async def toggle(path, key):
    if key not in ('master_enabled','behavior_enabled','keywords_enabled','exact_enabled'):
        return
    await ensure(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(f"UPDATE chat_history_settings SET {key}=CASE {key} WHEN 1 THEN 0 ELSE 1 END, updated_at=CURRENT_TIMESTAMP WHERE id=1")
        await db.commit()


async def stats(path):
    await ensure(path)
    async with aiosqlite.connect(path) as db:
        exact = (await (await db.execute("SELECT COUNT(*) FROM chat_history_exact WHERE enabled=1")).fetchone())[0]
        kw = (await (await db.execute("SELECT COUNT(*) FROM chat_history_keywords WHERE enabled=1")).fetchone())[0]
        hits = (await (await db.execute("SELECT COUNT(*) FROM chat_history_hits")).fetchone())[0]
        saved = (await (await db.execute("SELECT COALESCE(SUM(estimated_ai_calls_saved),0) FROM chat_history_hits")).fetchone())[0]
    return {'exact':exact,'keywords':kw,'hits':hits,'saved':saved}


async def show_menu(q, path):
    s = await settings(path); st = await stats(path)
    on = lambda v: '🟢 ON' if v else '🔴 OFF'
    text = (
        "📚 <b>Chat History</b>\n\n"
        "Local history intelligence used before AI to reduce token usage.\n"
        "Historical replies never override current Combo Rules/business data.\n\n"
        f"Master: <b>{on(s['master_enabled'])}</b>\n"
        f"🧠 Behavior: <b>{on(s['behavior_enabled'])}</b>\n"
        f"🔑 Keywords: <b>{on(s['keywords_enabled'])}</b>\n"
        f"🎯 Exact Matching: <b>{on(s['exact_enabled'])}</b>\n\n"
        f"Exact examples: <b>{st['exact']}</b>\n"
        f"Keyword rules: <b>{st['keywords']}</b>\n"
        f"Local matches: <b>{st['hits']}</b>\n"
        f"Estimated AI calls saved: <b>{st['saved']}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(('🔴 Turn Chat History OFF' if s['master_enabled'] else '🟢 Turn Chat History ON'), callback_data='admin:chist:master')],
        [InlineKeyboardButton(('🧠 Behavior: ON' if s['behavior_enabled'] else '🧠 Behavior: OFF'), callback_data='admin:chist:behavior')],
        [InlineKeyboardButton(('🔑 Keywords: ON' if s['keywords_enabled'] else '🔑 Keywords: OFF'), callback_data='admin:chist:keywords')],
        [InlineKeyboardButton(('🎯 Exact Matching: ON' if s['exact_enabled'] else '🎯 Exact Matching: OFF'), callback_data='admin:chist:exact')],
        [InlineKeyboardButton('⬅️ Back', callback_data='admin:home')],
    ])
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)


async def _state(path, chat_id):
    try:
        async with aiosqlite.connect(path) as db:
            row = await (await db.execute("SELECT state,started_at,message_count FROM combo_sticky_state WHERE chat_id=?", (str(chat_id),))).fetchone()
            if row and str(row[0]) in ('PRO_PACK','TOP_FACULTY') and time.time()-float(row[1] or 0) < 1800 and int(row[2] or 0) < 25:
                return str(row[0])
            row = await (await db.execute("SELECT current_state FROM combo_conversation_context WHERE chat_id=?", (str(chat_id),))).fetchone()
            if row and str(row[0]) in ('COMBO','PRO_PACK','TOP_FACULTY'):
                return str(row[0])
    except Exception:
        pass
    return ''


async def _exact_intent(path, message_norm, state):
    # State-aware terse messages learned from customer history.
    if message_norm in ('price','how much','kitne ka','kitne me','cost'):
        if state == 'PRO_PACK': return 'Pro Pack price'
        if state == 'TOP_FACULTY': return 'Top Faculty price'
    if message_norm in ('optional','with optional','optional hai','optional included') and state == 'PRO_PACK':
        return 'One Optional included'
    if message_norm in ('how many groups','total groups','kitne groups') and state == 'PRO_PACK':
        return 'How many groups are in Pro Pack?'
    async with aiosqlite.connect(path) as db:
        row = await (await db.execute("""SELECT intent FROM chat_history_exact
            WHERE enabled=1 AND message_norm=? AND state IN (?, 'ANY')
            ORDER BY CASE WHEN state=? THEN 0 ELSE 1 END, source_count DESC LIMIT 1""",
            (message_norm, state or 'NONE', state or 'NONE'))).fetchone()
    return str(row[0]) if row else None


async def _keyword_intent(path, text_norm, state):
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = [dict(r) for r in await (await db.execute("SELECT * FROM chat_history_keywords WHERE enabled=1 ORDER BY priority DESC,id")).fetchall()]
    best = None
    for r in rows:
        rs = str(r.get('state') or 'ANY')
        if rs != 'ANY' and rs != state:
            continue
        phrases = json.loads(r.get('phrases_json') or '[]')
        excludes = json.loads(r.get('exclude_json') or '[]')
        if any(_norm(x) and _norm(x) in text_norm for x in excludes):
            continue
        hits = sum(1 for x in phrases if _norm(x) and _norm(x) in text_norm)
        if hits <= 0:
            continue
        score = int(r.get('priority') or 0) * 100 + hits
        if best is None or score > best[0]:
            best = (score, str(r['intent']))
    return best[1] if best else None


async def _rule(path, intent):
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT intent,reply_en,reply_hi,trigger FROM combo_rules WHERE enabled=1 AND lower(intent)=lower(?) ORDER BY id DESC LIMIT 1", (intent,))).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _hinglish(text):
    return bool(HINGLISH.search(str(text or '').casefold())) or bool(re.search(r'[\u0900-\u097F]', str(text or '')))


async def _log_hit(path, chat_id, mode, intent, message_norm):
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute("INSERT INTO chat_history_hits(chat_id,mode,intent,message_norm,estimated_ai_calls_saved) VALUES(?,?,?,?,1)", (str(chat_id),mode,intent,message_norm))
            await db.commit()
    except Exception:
        pass


async def process_customer(update, context, path, execute_direct_command):
    if not getattr(update,'message',None) or not getattr(update.message,'text',None):
        return False
    s = await settings(path)
    if not s.get('master_enabled'):
        return False
    message = str(update.message.text or '').strip()
    if not message:
        return False
    chat_id = getattr(getattr(update,'effective_chat',None),'id',None)
    state = await _state(path, chat_id)
    message_norm = _norm(message)
    intent = None; mode = None
    if s.get('exact_enabled'):
        intent = await _exact_intent(path, message_norm, state)
        if intent: mode='exact'
    if not intent and s.get('keywords_enabled'):
        candidate = _behavior_clean(message) if s.get('behavior_enabled') else message
        candidate_norm = _norm(candidate)
        # Never let pure social filler become a history hit.
        if candidate_norm and candidate_norm not in {'hi','hello','hey','hii','hiii','thanks','thank you','thankyou','ok','okay','okk','please','bro','bhai','bhaiya','sir'}:
            intent = await _keyword_intent(path, candidate_norm, state)
            if intent: mode='keywords'
    if not intent:
        return False
    rule = await _rule(path, intent)
    if not rule:
        return False
    reply = (rule.get('reply_hi') if _hinglish(message) and rule.get('reply_hi') else rule.get('reply_en') or rule.get('reply_hi') or '').strip()
    trigger = str(rule.get('trigger') or '').strip()
    handled = False
    # Historical text never supplies the answer. Use only the CURRENT live rule.
    # Non-catalogue triggers (proof/demo/payment/premium/etc.) are safe to run
    # alongside a current reply. Catalogue triggers are executed only when the
    # intent itself has no reply, so a question such as group-count never
    # re-sends the whole Pro Pack catalogue.
    _non_catalogue = {'/proof','/demoall','/pay','/phonepe','/premium'}
    if reply:
        await update.message.reply_text(reply)
        handled = True
        if trigger in _non_catalogue:
            try:
                await execute_direct_command(update, context, trigger, internal=True)
            except TypeError:
                await execute_direct_command(update, context, trigger)
    elif trigger:
        try:
            handled = bool(await execute_direct_command(update, context, trigger, internal=True))
        except TypeError:
            handled = bool(await execute_direct_command(update, context, trigger))
    if handled:
        await _log_hit(path, chat_id, mode or 'local', intent, message_norm)
        return True
    return False
