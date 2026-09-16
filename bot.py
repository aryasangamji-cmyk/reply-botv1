from app.context_cache import BusinessCache
import json
import logging
import html
import time
import re
import difflib
import asyncio
from pathlib import Path
import aiosqlite
from openai import AsyncOpenAI
from .batch_manager import ensure_batch_columns, parse_message_link, search_batches, get_batch, insert_batch, update_batch_telegram, set_access_enabled, update_batch_field, slug_trigger, delete_batch
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatMemberHandler, ContextTypes, filters
from .ai import AIService
from .config import Settings
from .db import init_db, counts, recent_logs, add_log, business_context
from .actions import execute

logger = logging.getLogger(__name__)

DEFAULT_BEHAVIOUR = {
    'match_customer_language': True,
    'concise_replies': True,
    'friendly_tone': True,
    'ask_relevant_next_step': True,
    'avoid_repetition': True,
    'handle_multiple_intents': True,
    'never_expose_internal_info': True,
    'never_invent_business_info': True,
}

DEFAULT_SAVED_ACTIONS = {
    'show_batch': True,
    'show_course': True,
    'send_demo': True,
    'send_payment': True,
    'send_purchase_link': True,
    'show_faq': True,
    'talk_to_admin': True,
    'negotiate': True,
    'generic_reply': True,
}

DEFAULT_AI_SETTINGS = {
    'knowledge_enabled': True,
    'behaviour_enabled': True,
    'logging_enabled': True,
}


def is_admin(settings: Settings, user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_user_ids


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📚 Batches', callback_data='admin:batches'), InlineKeyboardButton('🎓 Courses', callback_data='admin:courses')],
        [InlineKeyboardButton('⚡ Shortcuts', callback_data='admin:shortcuts'), InlineKeyboardButton('🎁 Combos', callback_data='admin:combos')],
        [InlineKeyboardButton('⚙️ Rules', callback_data='admin:rules')],
        [InlineKeyboardButton('🧠 AI', callback_data='admin:ai'), InlineKeyboardButton('ℹ️ Information', callback_data='admin:information')],
        [InlineKeyboardButton('🔓 Pre Access', callback_data='admin:preaccess')],
        [InlineKeyboardButton('💰 Negotiation', callback_data='admin:negotiation')],
        [InlineKeyboardButton('📊 AI Logs', callback_data='admin:logs'), InlineKeyboardButton('🧾 AI Calls', callback_data='admin:ai_calls')],
        [InlineKeyboardButton('🧪 Test AI', callback_data='admin:test')],
        [InlineKeyboardButton('⚙️ AI Settings', callback_data='admin:settings')],
    ])


def main_text(c):
    return (f'🤖 <b>New AI Bot — Admin Panel</b> <code>PHASE25</code>\n\n'
            f'📚 Batches: <b>{c["batches"]}</b>\n'
            f'🎓 Courses: <b>{c["courses"]}</b>\n'
            f'⚡ Shortcuts: <b>{c["shortcuts"]}</b> ({c["shortcut_items"]} items)\n'
            f'🧠 AI Knowledge: <b>{c["ai_knowledge"]}</b>\n'
            f'📊 AI Logs: <b>{c["ai_logs"]}</b>')


async def get_rule(path, name, defaults):
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, rule_json, enabled FROM ai_rules WHERE lower(name)=lower(?) ORDER BY id DESC LIMIT 1", (name,)) as cur:
            row = await cur.fetchone()
    if not row:
        return dict(defaults)
    try:
        data = json.loads(row['rule_json'])
    except Exception:
        data = {}
    result = {**defaults, **data}
    result['enabled'] = bool(row['enabled']) and bool(result.get('enabled', False)) if name == 'negotiation' else bool(row['enabled'])
    return result


async def save_rule(path, name, data, enabled=None):
    if enabled is None:
        enabled = bool(data.get('enabled', True))
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT id FROM ai_rules WHERE lower(name)=lower(?) ORDER BY id DESC LIMIT 1", (name,)) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute('UPDATE ai_rules SET rule_json=?, enabled=? WHERE id=?', (json.dumps(data, ensure_ascii=False), int(enabled), row[0]))
        else:
            await db.execute('INSERT INTO ai_rules(name, rule_json, enabled) VALUES(?,?,?)', (name, json.dumps(data, ensure_ascii=False), int(enabled)))
        await db.commit()


async def ai_settings_rule(path):
    return await get_rule(path, 'ai_settings', DEFAULT_AI_SETTINGS)


async def update_ai_settings(path, **changes):
    rule = await ai_settings_rule(path)
    rule.update(changes)
    await save_rule(path, 'ai_settings', rule, enabled=True)


def ai_settings_keyboard(rule):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(('🧠 Knowledge: ON' if rule.get('knowledge_enabled', True) else '🧠 Knowledge: OFF'), callback_data='admin:set:knowledge')],
        [InlineKeyboardButton(('🤖 Behaviour: ON' if rule.get('behaviour_enabled', True) else '🤖 Behaviour: OFF'), callback_data='admin:set:behaviour')],
        [InlineKeyboardButton(('📊 Logging: ON' if rule.get('logging_enabled', True) else '📊 Logging: OFF'), callback_data='admin:set:logging')],
        [InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')]
    ])



async def show_ai_menu(q, path):
    """Main AI administration menu."""
    text = (
        '🧠 <b>AI Control Panel</b>\n\n'
        'Configure how the AI understands customers and uses your saved business data.'
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📚 AI Knowledge', callback_data='admin:knowledge'),
            InlineKeyboardButton('🧠 Train AI', callback_data='admin:train')
        ],
        [
            InlineKeyboardButton('🤖 AI Behaviour', callback_data='admin:behaviour'),
            InlineKeyboardButton('⚡ Saved Actions', callback_data='admin:saved_actions')
        ],
        [
            InlineKeyboardButton('💬 Generic Replies', callback_data='admin:generic_replies'),
            InlineKeyboardButton('💰 Negotiation', callback_data='admin:negotiation')
        ],
        [
            InlineKeyboardButton('⚙️ AI Settings', callback_data='admin:settings'),
            InlineKeyboardButton('🧪 Test AI', callback_data='admin:test')
        ],
        [
            InlineKeyboardButton('📊 AI Logs', callback_data='admin:logs')
        ],
        [
            InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')
        ]
    ])
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=keyboard)


async def show_ai_settings(q, path, model):
    rule = await ai_settings_rule(path)
    text = (f'⚙️ <b>AI Settings</b>\n\n'
            f'Model: <code>{html.escape(model)}</code>\n'
            f'AI runtime: <b>ACTIVE</b>\n'
            f'Business-data source: <b>SQLite</b>\n\n'
            f'🧠 AI Knowledge: <b>{"ON" if rule.get("knowledge_enabled", True) else "OFF"}</b>\n'
            f'🤖 AI Behaviour: <b>{"ON" if rule.get("behaviour_enabled", True) else "OFF"}</b>\n'
            f'📊 AI Logging: <b>{"ON" if rule.get("logging_enabled", True) else "OFF"}</b>')
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=ai_settings_keyboard(rule))


async def negotiation_rule(path):
    defaults = {'enabled': False, 'max_discount_amount': 0, 'minimum_price': None, 'accept_customer_offer': True, 'counter_offer': True}
    return await get_rule(path, 'negotiation', defaults)


def negotiation_keyboard(rule):
    enabled = bool(rule.get('enabled', False))
    toggle = '⛔ Disable' if enabled else '✅ Enable'
    callback = 'admin:neg:disable' if enabled else 'admin:neg:enable'
    accept = bool(rule.get('accept_customer_offer', True))
    counter = bool(rule.get('counter_offer', True))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=callback)],
        [InlineKeyboardButton('💸 Set Max Discount', callback_data='admin:neg:max')],
        [InlineKeyboardButton('🔒 Set Minimum Price', callback_data='admin:neg:min')],
        [InlineKeyboardButton(('✅ Accept Offer: ON' if accept else '❌ Accept Offer: OFF'), callback_data='admin:neg:accept')],
        [InlineKeyboardButton(('🔄 Counter Offer: ON' if counter else '❌ Counter Offer: OFF'), callback_data='admin:neg:counter')],
        [InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')]
    ])


async def show_negotiation(q, path):
    rule = await negotiation_rule(path)
    status = 'ENABLED ✅' if rule.get('enabled') else 'DISABLED ⛔'
    minimum = rule.get('minimum_price')
    minimum_text = f'₹{minimum}' if minimum is not None else 'Not configured'
    text = (f'💰 <b>Negotiation Settings</b>\n\n'
            f'Status: <b>{status}</b>\n'
            f'Maximum discount: <b>₹{rule.get("max_discount_amount", 0)}</b>\n'
            f'Minimum price: <b>{minimum_text}</b>\n'
            f'Accept customer offer: <b>{"YES" if rule.get("accept_customer_offer", True) else "NO"}</b>\n'
            f'Counter offer: <b>{"YES" if rule.get("counter_offer", True) else "NO"}</b>')
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=negotiation_keyboard(rule))


async def set_negotiation_enabled(path, enabled):
    rule = await negotiation_rule(path)
    rule['enabled'] = enabled
    await save_rule(path, 'negotiation', rule, enabled=enabled)


async def update_negotiation_rule(path, **changes):
    rule = await negotiation_rule(path)
    rule.update(changes)
    await save_rule(path, 'negotiation', rule, enabled=bool(rule.get('enabled', False)))


async def saved_actions_rule(path):
    return await get_rule(path, 'saved_actions', {**DEFAULT_SAVED_ACTIONS, 'enabled': True})


def saved_actions_keyboard(rule):
    labels = {
        'show_batch': '📚 Show Batch',
        'show_course': '🎓 Show Course',
        'send_demo': '🎬 Send Demo',
        'send_payment': '💳 Send Payment',
        'send_purchase_link': '🔗 Send Purchase Link',
        'show_faq': '❓ Show FAQ',
        'talk_to_admin': '👤 Talk to Admin',
        'negotiate': '💰 Negotiate',
        'generic_reply': '💬 Generic Reply',
    }
    rows = []
    keys = list(DEFAULT_SAVED_ACTIONS.keys())
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i:i+2]:
            on = bool(rule.get(key, True))
            row.append(InlineKeyboardButton(
                f'{"✅" if on else "❌"} {labels[key]}',
                callback_data=f'admin:sa:{key}'
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton('⬅️ AI', callback_data='admin:ai')])
    return InlineKeyboardMarkup(rows)


async def show_saved_actions(q, path):
    rule = await saved_actions_rule(path)
    enabled = sum(1 for k in DEFAULT_SAVED_ACTIONS if bool(rule.get(k, True)))
    text = (
        '⚡ <b>Saved Actions</b>\n\n'
        f'Enabled actions: <b>{enabled}/{len(DEFAULT_SAVED_ACTIONS)}</b>\n\n'
        'These are the only approved actions the AI action engine may use. '
        'Disabling an action makes the engine fall back safely instead of executing it.'
    )
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=saved_actions_keyboard(rule))


async def toggle_saved_action(path, key):
    rule = await saved_actions_rule(path)
    if key not in DEFAULT_SAVED_ACTIONS:
        return
    # Keep generic_reply available as the final safety fallback.
    if key == 'generic_reply' and bool(rule.get(key, True)):
        return
    rule[key] = not bool(rule.get(key, True))
    await save_rule(path, 'saved_actions', rule, enabled=True)

DEFAULT_GENERIC_REPLIES = {
    "greeting": "Hi! 👋 How can I help you today?",
    "unknown": "Sorry, I couldn't understand that clearly. Please tell me which batch or course you're looking for.",
    "talk_to_admin": "Sure. 👤 You can talk to the admin for further help.",
    "informational": "Sure! Please tell me what information you need, and I'll help you with it.",
    "thanks": "You're welcome! 😊 Let me know if you need anything else.",
    "unavailable": "Sorry, I don't have verified information about that yet.",
}


# ================================================================
# FINAL GENERIC INTENT SYSTEM
# ================================================================

async def ensure_final_generic_tables(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS final_generic_intents (id INTEGER PRIMARY KEY AUTOINCREMENT,intent TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 1,sort_order INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS final_generic_actions (id INTEGER PRIMARY KEY AUTOINCREMENT,intent_id INTEGER NOT NULL,action_type TEXT NOT NULL,value TEXT NOT NULL DEFAULT '',enabled INTEGER NOT NULL DEFAULT 1,sort_order INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        await db.commit()



async def migrate_legacy_generic_to_final(path):
    """Migrate old generic_replies data into the unified final Generic Intent system."""
    await ensure_final_generic_tables(path)

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        try:
            async with db.execute(
                "SELECT id,name,intent,reply_text,enabled,response_type,trigger_command "
                "FROM generic_replies ORDER BY sort_order ASC,id ASC"
            ) as cur:
                rows = await cur.fetchall()
        except Exception:
            return

    for row in rows:
        if not int(row["enabled"] or 0):
            continue

        intent = str(row["intent"] or "").strip()
        if not intent:
            continue

        iid, _ = await final_generic_add_intent(path, intent)

        response_type = str(row["response_type"] or "reply").strip().lower()
        if response_type == "trigger":
            value = str(row["trigger_command"] or "").strip()
            if value:
                if not value.startswith("/"):
                    value = "/" + value
                existing = await final_generic_actions(path, iid)
                if not any(
                    str(x.get("action_type")) == "command"
                    and str(x.get("action_value")) == value
                    for x in existing
                ):
                    await final_generic_add_action(path, iid, "command", value)
        else:
            value = str(row["reply_text"] or "")
            if value:
                existing = await final_generic_actions(path, iid)
                if not any(
                    str(x.get("action_type")) == "reply"
                    and str(x.get("action_value")) == value
                    for x in existing
                ):
                    await final_generic_add_action(path, iid, "reply", value)


async def ensure_generic_replies_table(path):
    """Backward-compatible startup wrapper for the legacy initializer."""
    await ensure_final_generic_tables(path)

async def final_generic_intents(path):
    await ensure_final_generic_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT id,intent,enabled,sort_order FROM final_generic_intents ORDER BY sort_order,id") as cur:
            return [dict(x) for x in await cur.fetchall()]

async def final_generic_intent(path,iid):
    rows=await final_generic_intents(path)
    return next((x for x in rows if int(x["id"])==int(iid)),None)

async def final_generic_actions(path,iid):
    await ensure_final_generic_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT id,intent_id,action_type,value,enabled,sort_order FROM final_generic_actions WHERE intent_id=? ORDER BY sort_order,id",(iid,)) as cur:
            return [dict(x) for x in await cur.fetchall()]

async def final_generic_add_intent(path,intent):
    await ensure_final_generic_tables(path)
    intent=str(intent or "").strip()
    if not intent: raise ValueError("empty intent")
    async with aiosqlite.connect(path) as db:
        cur=await db.execute("SELECT id FROM final_generic_intents WHERE lower(intent)=lower(?)",(intent,))
        old=await cur.fetchone()
        if old: return old[0],False
        cur=await db.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM final_generic_intents")
        order=(await cur.fetchone())[0]
        cur=await db.execute("INSERT INTO final_generic_intents(intent,enabled,sort_order) VALUES(?,?,?)",(intent,1,order))
        await db.commit()
        return cur.lastrowid,True

async def final_generic_add_action(path,iid,kind,value):
    await ensure_final_generic_tables(path)
    kind="command" if str(kind).lower()=="command" else "reply"
    value=str(value or "").strip()
    if kind=="command": value="/"+value.lstrip("/")
    if not value: raise ValueError("empty value")
    async with aiosqlite.connect(path) as db:
        cur=await db.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM final_generic_actions WHERE intent_id=?",(iid,))
        order=(await cur.fetchone())[0]
        await db.execute("INSERT INTO final_generic_actions(intent_id,action_type,value,enabled,sort_order) VALUES(?,?,?,?,?)",(iid,kind,value,1,order))
        await db.commit()

async def final_generic_delete_action(path,aid):
    await ensure_final_generic_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM final_generic_actions WHERE id=?",(aid,))
        await db.commit()

async def final_generic_delete_intent(path,iid):
    await ensure_final_generic_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM final_generic_actions WHERE intent_id=?",(iid,))
        await db.execute("DELETE FROM final_generic_intents WHERE id=?",(iid,))
        await db.commit()

# ===================== INTENT DATABASE V1 =====================
async def ensure_intent_database_tables(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS generic_intent_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                example TEXT NOT NULL,
                normalized TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(intent_id, normalized)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_generic_intent_examples_normalized ON generic_intent_examples(normalized)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_generic_intent_examples_intent ON generic_intent_examples(intent_id)")
        await db.commit()

def intent_db_normalize(value):
    value = str(value or "")
    value = value.replace("\u200b", " ").replace("\u200c", " ").replace("\u200d", " ")
    value = value.casefold()
    value = re.sub(r"\s+", " ", value, flags=re.UNICODE).strip()
    return value

def intent_db_parse_bullets(raw):
    """One example per • delimiter; visual Telegram wrapping is ignored."""
    raw = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if "•" not in raw:
        return []
    return [re.sub(r"\s+", " ", p, flags=re.UNICODE).strip()
            for p in raw.split("•")[1:] if re.sub(r"\s+", " ", p, flags=re.UNICODE).strip()]

async def intent_db_count(path, iid):
    await ensure_intent_database_tables(path)
    async with aiosqlite.connect(path) as db:
        row = await (await db.execute("SELECT COUNT(*) FROM generic_intent_examples WHERE intent_id=?", (iid,))).fetchone()
    return int(row[0] or 0)

async def intent_db_add_examples(path, iid, examples):
    await ensure_intent_database_tables(path)
    added = 0
    async with aiosqlite.connect(path) as db:
        for example in examples:
            normalized = intent_db_normalize(example)
            if not normalized:
                continue
            cur = await db.execute(
                "INSERT OR IGNORE INTO generic_intent_examples(intent_id,example,normalized) VALUES(?,?,?)",
                (iid, example, normalized)
            )
            if cur.rowcount:
                added += 1
        await db.commit()
    return added

async def intent_db_delete(path, iid):
    await ensure_intent_database_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM generic_intent_examples WHERE intent_id=?", (iid,))
        await db.commit()

async def intent_db_rows(path, iid, limit=50):
    await ensure_intent_database_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id,example,created_at FROM generic_intent_examples WHERE intent_id=? ORDER BY id LIMIT ?",
            (iid, limit)
        )).fetchall()
    return [dict(r) for r in rows]

async def intent_db_match(path, message):
    """Exact normalized local lookup. This path makes no AI call."""
    normalized = intent_db_normalize(message)
    if not normalized:
        return None
    await ensure_intent_database_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("""
            SELECT e.intent_id, e.id AS example_id, e.example, i.intent, i.enabled
            FROM generic_intent_examples e
            JOIN final_generic_intents i ON i.id=e.intent_id
            WHERE e.normalized=? AND i.enabled=1
            ORDER BY e.id LIMIT 1
        """, (normalized,))).fetchone()
    return dict(row) if row else None

async def intent_db_execute_match(path, match, update, context):
    if not match:
        return False
    actions = await final_generic_actions(path, int(match["intent_id"]))
    for action in actions:
        if not action.get("enabled"):
            continue
        if action.get("action_type") == "reply":
            value = str(action.get("value") or "")
            if value:
                await update.message.reply_text(customer_embedded_links(value), parse_mode="HTML", disable_web_page_preview=True)
                return True
        elif action.get("action_type") == "command":
            command = str(action.get("value") or "").strip()
            if command and not command.startswith("/"):
                command = "/" + command.lstrip("/")
            if command and await execute_direct_command(update, context, command, internal=True):
                return True
    return True

def intent_db_keyboard(iid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Import Examples", callback_data=f"fgi:dbimport:{iid}")],
        [InlineKeyboardButton("📋 View Examples", callback_data=f"fgi:dbview:{iid}")],
        [InlineKeyboardButton("🗑️ Delete Database", callback_data=f"fgi:dbdelete:{iid}")],
        [InlineKeyboardButton("🔙 Back to Intent", callback_data=f"fgi:view:{iid}")],
    ])

async def show_intent_database(q, path, iid):
    row = await final_generic_intent(path, iid)
    if not row:
        await final_generic_show(q, path)
        return
    count = await intent_db_count(path, iid)
    await q.edit_message_text(
        f"🗄️ <b>Intent Database</b>\n\n"
        f"Intent: <b>{html.escape(str(row['intent']))}</b>\n"
        f"Saved examples: <b>{count}</b>\n\n"
        "Use <code>•</code> before every example. One <code>•</code> = one example. "
        "Telegram visual line wrapping does not create extra examples.\n\n"
        "Example:\n<code>• payment kaise karna hai\n• bhaiya payment kese kru\n• long example that may wrap visually</code>",
        parse_mode="HTML", reply_markup=intent_db_keyboard(iid)
    )

async def show_intent_database_examples(q, path, iid):
    row = await final_generic_intent(path, iid)
    if not row:
        await final_generic_show(q, path)
        return
    count = await intent_db_count(path, iid)
    rows = await intent_db_rows(path, iid, 50)
    lines = ["📋 <b>Intent Database — Saved Examples</b>", "",
             f"Intent: <b>{html.escape(str(row['intent']))}</b>", f"Total: <b>{count}</b>", ""]
    for idx, r in enumerate(rows, 1):
        example = str(r["example"]).replace("\n", " ")
        if len(example) > 180:
            example = example[:177] + "..."
        lines.append(f"{idx}. {html.escape(example)}")
    if count > len(rows):
        lines.append(f"\n…and {count-len(rows)} more.")
    await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=intent_db_keyboard(iid))
# =================== END INTENT DATABASE V1 ===================

async def final_generic_toggle(path,iid):
    await ensure_final_generic_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE final_generic_intents SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END,updated_at=CURRENT_TIMESTAMP WHERE id=?",(iid,))
        await db.commit()

async def final_generic_show(q,path):
    rows=await final_generic_intents(path)
    kb=[[InlineKeyboardButton(("🟢 " if r["enabled"] else "🔴 ")+str(r["intent"]),callback_data=f"fgi:view:{r['id']}")] for r in rows[:30]]
    kb += [[InlineKeyboardButton("➕ Add Intent",callback_data="fgi:add")],[InlineKeyboardButton("🔙 Back",callback_data="admin:ai")],[InlineKeyboardButton("🏠 Admin",callback_data="admin:home")]]
    await q.edit_message_text("💬 <b>Generic</b>\n\nAdd an Intent first, then attach an exact Command and/or Reply.\nAI only understands the intent; the server executes the saved action.",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))

async def final_generic_editor(q,path,iid):
    row=await final_generic_intent(path,iid)
    if not row:
        await final_generic_show(q,path); return
    acts=await final_generic_actions(path,iid)
    lines=[f"💬 <b>{html.escape(str(row['intent']))}</b>","","Configured actions:"]
    if not acts: lines.append("— none —")
    for x in acts:
        lines.append(("⚡ " if x["action_type"]=="command" else "💬 ")+html.escape(str(x["value"])))
    kb=[]
    for x in acts:
        kb.append([InlineKeyboardButton("🗑️ Remove "+str(x["action_type"]).title(),callback_data=f"fgi:delact:{x['id']}:{iid}")])
    kb += [[InlineKeyboardButton("⚡ Add Command",callback_data=f"fgi:addcmd:{iid}")],[InlineKeyboardButton("💬 Add Reply",callback_data=f"fgi:addreply:{iid}")],[InlineKeyboardButton("🗄️ Intent Database",callback_data=f"fgi:db:{iid}")],[InlineKeyboardButton("💾 Save",callback_data=f"fgi:save:{iid}")],[InlineKeyboardButton("🔴 Disable" if row["enabled"] else "🟢 Enable",callback_data=f"fgi:toggle:{iid}")],[InlineKeyboardButton("🗑️ Delete Intent",callback_data=f"fgi:del:{iid}")],[InlineKeyboardButton("🔙 Back",callback_data="admin:generic_replies")],[InlineKeyboardButton("🏠 Admin",callback_data="admin:home")]]
    await q.edit_message_text("\n".join(lines),parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))

async def final_generic_action_for(path, intents):
    """
    Deterministic Final Generic Intent resolver.

    AI must return one of the admin-configured intent names.
    The server then finds that exact intent and executes its first
    enabled saved action. No old generic reply is consulted here.
    """
    names = []

    for item in (intents or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name.casefold())

    if not names:
        return None

    rows = await final_generic_intents(path)

    # Exact configured-intent match only.
    for row in rows:
        if not row.get("enabled"):
            continue

        configured = str(row.get("intent") or "").strip()

        if configured.casefold() not in names:
            continue

        actions = await final_generic_actions(path, int(row["id"]))

        # Preserve administrator ordering.
        for action in actions:
            if action.get("enabled"):
                return action

    return None

async def final_generic_intent_context(path):
    rows=[x for x in await final_generic_intents(path) if x["enabled"]]
    if not rows: return ""
    return "ADMIN-DEFINED GENERIC INTENTS:\n"+"\n".join("- "+str(x["intent"]) for x in rows)+"\nIf the customer clearly matches one, return that exact intent name. Do not rename it."
async def ensure_intent_triggers_table(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS intent_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent TEXT NOT NULL,
                trigger TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(intent, trigger)
            )
        """)
        await db.commit()


def _norm_command(value):
    value = str(value or '').strip().lower()
    value = value.replace('_', ' ')
    value = re.sub(r'[^a-z0-9\u0900-\u097f]+', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def _command_key(value):
    return _norm_command(value).replace(' ', '')


async def intent_trigger_rows(path):
    await ensure_intent_triggers_table(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT id,intent,trigger,enabled FROM intent_triggers ORDER BY id')
        return [dict(r) for r in await cur.fetchall()]


async def add_intent_trigger(path, intent, trigger):
    await ensure_intent_triggers_table(path)
    intent = str(intent or '').strip()
    trigger = str(trigger or '').strip()
    if not intent or not trigger.startswith('/'):
        raise ValueError('Intent and slash trigger are required.')
    if len(trigger) > 100 or '\n' in trigger or '\r' in trigger:
        raise ValueError('Trigger is invalid.')
    async with aiosqlite.connect(path) as db:
        await db.execute(
            'INSERT INTO intent_triggers(intent,trigger,enabled) VALUES(?,?,1) '
            'ON CONFLICT(intent,trigger) DO UPDATE SET enabled=1,updated_at=CURRENT_TIMESTAMP',
            (intent, trigger)
        )
        await db.commit()


async def delete_intent_trigger(path, row_id):
    await ensure_intent_triggers_table(path)
    async with aiosqlite.connect(path) as db:
        await db.execute('DELETE FROM intent_triggers WHERE id=?', (row_id,))
        await db.commit()


async def toggle_intent_trigger(path, row_id):
    await ensure_intent_triggers_table(path)
    async with aiosqlite.connect(path) as db:
        await db.execute('UPDATE intent_triggers SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END,updated_at=CURRENT_TIMESTAMP WHERE id=?', (row_id,))
        await db.commit()


async def intent_trigger_for(path, intents):
    await ensure_intent_triggers_table(path)
    names=[]
    for item in intents or []:
        if isinstance(item, dict):
            name=str(item.get('name') or '').strip()
        else:
            name=str(item or '').strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return None
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        placeholders=','.join('?' for _ in names)
        cur=await db.execute(
            f'SELECT id,intent,trigger,enabled FROM intent_triggers '
            f'WHERE enabled=1 AND intent IN ({placeholders}) ORDER BY id LIMIT 1',
            tuple(names)
        )
        row=await cur.fetchone()
    return dict(row) if row else None


def intent_trigger_context(rows):
    rows=[r for r in rows if r.get('enabled')]
    if not rows:
        return ''
    lines=['CONFIGURED AI INTENT -> COMMAND MAPS:']
    for r in rows:
        lines.append(f'- Intent: {r["intent"]} -> Trigger: {r["trigger"]}')
    lines.append('When a customer clearly matches one of these configured intents, return that exact intent name. The application will execute the mapped trigger; do not invent or alter the intent name.')
    return '\n'.join(lines)


def _match_name(query, rows, name_key='name'):
    """Small deterministic matcher for direct commands only (not customer AI search)."""
    q=_command_key(query)
    if not q:
        return None
    exact=[]
    scored=[]
    for row in rows:
        name=str(row.get(name_key) or '')
        key=_command_key(name)
        if not key:
            continue
        if q == key:
            exact.append(row); continue
        ratio=difflib.SequenceMatcher(None,q,key).ratio()
        if q in key or key in q:
            ratio=max(ratio,0.90)
        if ratio >= 0.84:
            scored.append((ratio,row))
    if exact:
        return exact[0]
    scored.sort(key=lambda x:x[0], reverse=True)
    return scored[0][1] if scored else None


async def _shortcut_by_trigger(path, raw_trigger):
    await ensure_intent_triggers_table(path)
    key=_command_key(str(raw_trigger).lstrip('/'))
    if not key:
        return None
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        # trigger is added lazily so migrated shortcut data remains untouched.
        cols=[r[1] for r in await (await db.execute('PRAGMA table_info(shortcuts)')).fetchall()]
        if 'trigger' not in cols:
            await db.execute('ALTER TABLE shortcuts ADD COLUMN trigger TEXT')
            await db.execute('UPDATE shortcuts SET trigger=name WHERE trigger IS NULL OR trim(trigger)=\'\'')
            await db.commit()
        cur=await db.execute('SELECT * FROM shortcuts WHERE enabled=1')
        rows=[dict(r) for r in await cur.fetchall()]
    for row in rows:
        if _command_key(row.get('trigger') or row.get('name')) == key:
            return row
    return None


async def _send_shortcut(path, bot, chat_id, shortcut):
    sid=int(shortcut['id'])
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        cols=[r[1] for r in await (await db.execute('PRAGMA table_info(shortcut_items)')).fetchall()]
        order_col='item_order' if 'item_order' in cols else 'sort_order'
        rows=await (await db.execute(f'SELECT * FROM shortcut_items WHERE shortcut_id=? AND enabled=1 ORDER BY {order_col},id',(sid,))).fetchall()
        items=[dict(r) for r in rows]
    for item in items:
        text=str(item.get('text_content') or item.get('content') or '')
        caption=str(item.get('caption') or '')
        media=str(item.get('media_path') or item.get('file_path') or '')
        url=''; visible='Click Here'
        raw=item.get('button_data_json') or ''
        if raw:
            try:
                d=json.loads(raw); url=str(d.get('url') or '').strip(); visible=str(d.get('text') or 'Click Here')
            except Exception: pass
        content=caption or text
        if url and visible and visible in content:
            content=content.replace(visible,f'<a href="{html.escape(url,quote=True)}">{html.escape(visible)}</a>',1)
        elif url:
            content=(content+'\n' if content else '')+f'<a href="{html.escape(url,quote=True)}">Click Here</a>'
        if media:
            try:
                if media.lower().endswith(('.jpg','.jpeg','.png','.webp')):
                    await bot.send_photo(chat_id,photo=media,caption=content or None,parse_mode='HTML',disable_notification=True)
                elif media.lower().endswith(('.mp4','.mov','.mkv')):
                    await bot.send_video(chat_id,video=media,caption=content or None,parse_mode='HTML',disable_notification=True)
                else:
                    await bot.send_document(chat_id,document=media,caption=content or None,parse_mode='HTML',disable_notification=True)
            except Exception:
                if content: await bot.send_message(chat_id,content,parse_mode='HTML',disable_web_page_preview=True)
        elif content:
            await bot.send_message(chat_id,content,parse_mode='HTML',disable_web_page_preview=True)
    return True


async def _course_direct(path, query):
    key=_command_key(query)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute('SELECT * FROM courses WHERE enabled=1')
        rows=[dict(r) for r in await cur.fetchall()]
    for row in rows:
        if _command_key(row.get('name')) == key:
            return row
    return _match_name(query, rows)


async def _batch_direct(path, query):
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute('SELECT * FROM batches WHERE enabled=1')
        rows=[dict(r) for r in await cur.fetchall()]
    for row in rows:
        trigger=str(row.get('slash_trigger') or '').strip()
        if trigger and _command_key(trigger.lstrip('/')) == _command_key(query):
            return row
    return _match_name(query, rows)


async def execute_direct_command(update, context, raw_command, internal=False):
    """Deterministic command executor used by admin commands and AI-generated triggers."""
    settings=context.application.bot_data['settings']
    text=str(raw_command or '').strip()
    if text.startswith('/'):
        text=text[1:].strip()
    text=re.sub(r'^\S+@[^\s]+', lambda m:m.group(0).split('@',1)[0], text, count=1)
    if not text:
        return False
    # Exact shortcut trigger first. This is intentionally not AI.
    # Human-triggered shortcut execution is admin-only; AI-generated triggers
    # use internal=True and are allowed to execute for the customer.
    shortcut=await _shortcut_by_trigger(settings.database_path, text) if (internal or is_admin(settings, update.effective_user.id)) else None
    if shortcut:
        await _send_shortcut(settings.database_path, context.bot, update.effective_chat.id, shortcut)
        return True

    norm_text=_norm_command(text)
    # Pre Access is admin/main-console/AI only and uses the exact saved Batch/Course record.
    if norm_text.startswith('pre '):
        if internal or is_admin(settings, update.effective_user.id):
            handled=await execute_pre_command(update,context,norm_text[4:].strip(),internal=True)
            if handled:return True

    # Optional deterministic batch-demo command. Examples: /demo_batch_name
    # or /demo Batch Name. This remains entirely outside the AI layer.
    demo_query=None
    if norm_text.startswith('demo '):
        demo_query=norm_text[5:]
    elif text.lower().startswith('demo_'):
        demo_query=text[5:]
    if demo_query and (internal or is_admin(settings, update.effective_user.id)):
        batch=await _exact_saved_batch(settings.database_path, demo_query)
        target=batch
        if not target:
            target=await _exact_saved_course(settings.database_path, demo_query)
        if target:
            demo=str(target.get('demo_link') or '').strip()
            purchase=str(target.get('pay_link') or '').strip()
            details=str(target.get('description') or '').strip()
            lines=[]
            if demo: lines.append('Demo: '+demo)
            if purchase: lines.append('Purchase Link: '+purchase)
            if not lines and details: lines.append(details)
            if lines:
                await context.bot.send_message(update.effective_chat.id, customer_embedded_links('\n'.join(lines)), parse_mode='HTML', disable_web_page_preview=True)
            else:
                await context.bot.send_message(update.effective_chat.id, 'Demo is not available for this saved item.')
            return True

    # Course names accept spaces and underscores, case-insensitively.
    # Course direct commands are admin-only, except for an AI-generated
    # internal trigger.
    course=await _exact_saved_course(settings.database_path, text) if (internal or is_admin(settings, update.effective_user.id)) else None
    if course:
        chat_id=str(course.get('bot_chat_id') or '').strip()
        if not chat_id:
            await context.bot.send_message(update.effective_chat.id, '❌ This course is not configured with a Telegram access chat yet.')
            return True
        try:
            invite=await context.bot.create_chat_invite_link(chat_id=int(chat_id), expire_date=int(time.time())+86400, member_limit=1)
            await context.bot.send_message(update.effective_chat.id, f'🔐 <b>{html.escape(str(course["name"]))}</b>\n\nYour 24-hour access link ~ <a href="{html.escape(invite.invite_link, quote=True)}">Click Here</a>', parse_mode='HTML', disable_web_page_preview=True)
        except Exception as e:
            await context.bot.send_message(update.effective_chat.id, f'❌ Could not create the course access link.\n\n{html.escape(str(e))}')
        return True

    # Batch direct trigger/name is admin-only when typed by a human; internally
    # generated triggers are allowed because they originate from the AI map.
    if internal or is_admin(settings, update.effective_user.id):
        batch=await _exact_saved_batch(settings.database_path, text)
        if batch:
            if not batch.get('access_link_enabled') or not batch.get('telegram_verified') or not batch.get('telegram_can_invite'):
                await context.bot.send_message(update.effective_chat.id, '❌ This batch is not configured for 24-hour + 1-person access yet.')
                return True
            chat_id=str(batch.get('telegram_chat_id') or batch.get('bot_chat_id') or '').strip()
            if not chat_id:
                await context.bot.send_message(update.effective_chat.id, '❌ This batch has no verified Telegram chat configured.')
                return True
            try:
                invite=await context.bot.create_chat_invite_link(chat_id=int(chat_id), expire_date=int(time.time())+86400, member_limit=1)
                await context.bot.send_message(update.effective_chat.id, f'🔐 <b>{html.escape(str(batch["name"]))}</b>\n\nYour 24-hour access link ~ <a href="{html.escape(invite.invite_link, quote=True)}">Click Here</a>', parse_mode='HTML', disable_web_page_preview=True)
            except Exception as e:
                await context.bot.send_message(update.effective_chat.id, f'❌ Could not create the batch access link.\n\n{html.escape(str(e))}')
            return True
    return False

async def ai_knowledge_rows(path, enabled_only=False):
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        sql = 'SELECT id, name, content, enabled FROM ai_knowledge'
        if enabled_only:
            sql += ' WHERE enabled=1'
        sql += ' ORDER BY id DESC'
        async with db.execute(sql) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def save_ai_knowledge(path, name, content):
    async with aiosqlite.connect(path) as db:
        await db.execute('INSERT INTO ai_knowledge(name, content, enabled) VALUES(?,?,1)', (name, content))
        await db.commit()


async def ai_knowledge_context(path):
    rows = await ai_knowledge_rows(path, enabled_only=True)
    if not rows:
        return ''
    parts = ['APPROVED AI KNOWLEDGE (guidance only; authoritative batch/course data overrides it):']
    for row in rows:
        parts.append(f'- {row["name"]}: {row["content"]}')
    return '\n'.join(parts)


def ai_knowledge_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Teach AI', callback_data='admin:knowledge:add')],
        [InlineKeyboardButton('📚 View Knowledge', callback_data='admin:knowledge:list')],
        [InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')]
    ])


async def show_ai_knowledge(q, path):
    rows = await ai_knowledge_rows(path)
    if not rows:
        text = '🧠 <b>AI Knowledge</b>\n\nNo knowledge entries have been added yet.'
    else:
        parts = [f'🧠 <b>AI Knowledge</b> — {len(rows)} entries']
        for row in rows[:12]:
            status = 'ON' if row['enabled'] else 'OFF'
            content = row['content'].replace('\n', ' ')
            if len(content) > 180:
                content = content[:177] + '...'
            parts.append(f'\n<b>#{row["id"]} {html.escape(row["name"])}</b> [{status}]\n{html.escape(content)}')
        if len(rows) > 12:
            parts.append(f'\n…and {len(rows)-12} more.')
        text = '\n'.join(parts)
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=ai_knowledge_keyboard())


# ---- Information system ----
async def ensure_information_table(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_information (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()

async def information_rows(path, limit=50):
    await ensure_information_table(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM ai_information ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

async def information_one(path, iid):
    await ensure_information_table(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM ai_information WHERE id=?", (iid,))
        r=await cur.fetchone()
    return dict(r) if r else None

async def information_add(path, title, aliases, details):
    await ensure_information_table(path)
    async with aiosqlite.connect(path) as db:
        cur=await db.execute("INSERT INTO ai_information(title,aliases,details) VALUES(?,?,?)", (title.strip(), aliases.strip(), details.strip()))
        await db.commit()
        return cur.lastrowid

async def information_update(path, iid, title=None, aliases=None, details=None, enabled=None):
    row=await information_one(path,iid)
    if not row: return False
    vals={'title': row['title'], 'aliases': row['aliases'], 'details': row['details'], 'enabled': row['enabled']}
    if title is not None: vals['title']=title
    if aliases is not None: vals['aliases']=aliases
    if details is not None: vals['details']=details
    if enabled is not None: vals['enabled']=enabled
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE ai_information SET title=?,aliases=?,details=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (vals['title'],vals['aliases'],vals['details'],vals['enabled'],iid))
        await db.commit()
    return True

async def information_delete(path, iid):
    await ensure_information_table(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM ai_information WHERE id=?", (iid,))
        await db.commit()

def information_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Add Information', callback_data='admin:info:add')],
        [InlineKeyboardButton('📋 Saved Information', callback_data='admin:info:list')],
        [InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')]
    ])

async def information_context(path):
    rows=await information_rows(path,100)
    rows=[r for r in rows if r.get('enabled')]
    if not rows: return ''
    parts=['ADMIN INFORMATION PAGES (authoritative saved information):']
    for r in rows:
        parts.append(f"ID={r['id']} | TITLE={r['title']} | ALIASES={r.get('aliases','')}\nDETAILS:\n{r.get('details','')}")
    parts.append('Use this information when the customer asks about the corresponding page/topic. Do not invent or alter saved details.')
    return '\n\n'.join(parts)

async def show_information(q,path):
    rows=await information_rows(path,100)
    text=f'ℹ️ <b>Information System</b>\n\nSaved information pages: <b>{len(rows)}</b>\n\nAdd any page/topic and save its complete details (price, year, duration, rules, links, etc.) as provided by the admin.'
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=information_keyboard())

async def show_information_list(q,path):
    rows=await information_rows(path,100)
    if not rows:
        await q.edit_message_text('ℹ️ <b>Saved Information</b>\n\nNo information pages saved yet.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('➕ Add Information',callback_data='admin:info:add')],[InlineKeyboardButton('⬅️ Information',callback_data='admin:information')]])); return
    lines=['ℹ️ <b>Saved Information</b>','']
    kb=[]
    for r in rows:
        lines.append(f"{'🟢' if r['enabled'] else '🔴'} <b>#{r['id']}</b> {html.escape(r['title'])}")
        kb.append([InlineKeyboardButton(f"#{r['id']} · {r['title'][:32]}",callback_data=f"admin:info:view:{r['id']}")])
    kb += [[InlineKeyboardButton('➕ Add Information',callback_data='admin:info:add')],[InlineKeyboardButton('⬅️ Information',callback_data='admin:information')]]
    await q.edit_message_text('\n'.join(lines),parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def show_information_editor(q,path,iid):
    r=await information_one(path,iid)
    if not r: return await show_information_list(q,path)
    text=(f"ℹ️ <b>Information #{iid}</b>\n\n<b>Title:</b> {html.escape(r['title'])}\n<b>Aliases:</b> {html.escape(r.get('aliases','') or 'None')}\n<b>Status:</b> {'🟢 ON' if r['enabled'] else '🔴 OFF'}\n\n<b>Details:</b>\n{html.escape(r['details'][:3500])}")
    kb=[[InlineKeyboardButton('✏️ Edit',callback_data=f'admin:info:edit:{iid}')],
        [InlineKeyboardButton(('🔴 Disable' if r['enabled'] else '🟢 Enable'),callback_data=f'admin:info:toggle:{iid}')],
        [InlineKeyboardButton('🗑️ Delete',callback_data=f'admin:info:delete:{iid}')],
        [InlineKeyboardButton('⬅️ Saved Information',callback_data='admin:info:list')]]
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

# ---- Train AI / human-learning vault ----
async def ensure_training_tables(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_training_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_text TEXT NOT NULL DEFAULT '',
            human_reply TEXT NOT NULL DEFAULT '',
            intent TEXT NOT NULL DEFAULT 'unknown',
            entity TEXT NOT NULL DEFAULT '',
            learned_logic TEXT NOT NULL DEFAULT '',
            learning_summary TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        cols=[r[1] for r in await (await db.execute('PRAGMA table_info(ai_learnings)')).fetchall()]
        if 'learning_summary' not in cols:
            await db.execute("ALTER TABLE ai_learnings ADD COLUMN learning_summary TEXT NOT NULL DEFAULT ''")
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_training_read_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            chat_link TEXT NOT NULL DEFAULT '',
            first_link TEXT NOT NULL DEFAULT '',
            last_link TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("INSERT OR IGNORE INTO ai_training_settings(key,value) VALUES('auto_read','0')")
        await db.commit()

async def training_auto_read(path):
    await ensure_training_tables(path)
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT value FROM ai_training_settings WHERE key='auto_read'") as cur:
            row=await cur.fetchone()
    return bool(row and str(row[0]) == '1')

async def set_training_auto_read(path, enabled):
    await ensure_training_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT OR REPLACE INTO ai_training_settings(key,value) VALUES('auto_read',?)", ('1' if enabled else '0',))
        await db.commit()

async def training_rows(path, limit=10):
    await ensure_training_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM ai_learnings ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def training_one(path, learning_id):
    await ensure_training_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM ai_learnings WHERE id=?", (learning_id,)) as cur:
            r=await cur.fetchone()
    return dict(r) if r else None

async def training_add(path, context_text, human_reply, intent='unknown', entity='', learned_logic='', source='manual'):
    await ensure_training_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO ai_learnings(context_text,human_reply,intent,entity,learned_logic,source) VALUES(?,?,?,?,?,?)",
                         (context_text, human_reply, intent, entity, learned_logic, source))
        await db.execute("DELETE FROM ai_learnings WHERE id NOT IN (SELECT id FROM ai_learnings ORDER BY id DESC LIMIT 10)")
        await db.commit()

async def training_update(path, learning_id, intent, human_reply, learned_logic, context_text=None, learning_summary=None):
    row=await training_one(path, learning_id)
    if not row: return False
    if context_text is None: context_text=row['context_text']
    if learning_summary is None:
        learning_summary=row.get('learning_summary') or (learned_logic or human_reply or '').strip()[:1200]
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE ai_learnings SET context_text=?,human_reply=?,intent=?,learned_logic=?,learning_summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (context_text,human_reply,intent,learned_logic,learning_summary,learning_id))
        await db.commit()
    return True

async def training_delete(path, learning_id):
    await ensure_training_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM ai_learnings WHERE id=?", (learning_id,))
        await db.commit()

def train_ai_keyboard(auto_on=False):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f'🔄 Auto Read: {"🟢 ON" if auto_on else "🔴 OFF"}', callback_data='admin:train:toggle')],
        [InlineKeyboardButton('📖 Read Chat', callback_data='admin:train:read')],
        [InlineKeyboardButton('🟢 Reader Status', callback_data='admin:train:reader_status')],
        [InlineKeyboardButton('📚 Recent Learnings (10)', callback_data='admin:train:list')],
        [InlineKeyboardButton('⬅️ AI', callback_data='admin:ai')]
    ])

def training_read_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📖 Read All Chat', callback_data='admin:train:read:all')],
        [InlineKeyboardButton('📑 Read In Range', callback_data='admin:train:read:range')],
        [InlineKeyboardButton('❌ Cancel', callback_data='admin:train')]
    ])

def training_link_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='admin:train')]])

async def show_train_ai(q, path):
    on=await training_auto_read(path)
    rows=await training_rows(path,10)
    text=('🧠 <b>Train AI</b>\n\n'
          f'🔄 Auto Read: <b>{"ON 🟢" if on else "OFF 🔴"}</b>\n\n'
          'When Auto Read is ON, only genuine human/admin replies are eligible for learning; AI-generated replies are excluded.\n\n'
          f'📚 Saved learnings: <b>{len(rows)}/10</b>\n'
          'Only the latest 10 learning records are retained.\n\n'
          '📖 Read Chat lets you choose the complete chat or an exact first-to-last message range.')
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=train_ai_keyboard(on))

async def show_training_list(q,path):
    rows=await training_rows(path,10)
    if not rows:
        text='📚 <b>Recent Learnings</b>\n\nNo learnings saved yet.'
        kb=[[InlineKeyboardButton('⬅️ Train AI',callback_data='admin:train')]]
    else:
        lines=['📚 <b>Recent Learnings</b>','',f'Latest <b>{len(rows)}</b> of maximum 10:']
        kb=[]
        for r in rows:
            intent=str(r['intent'] or 'unknown')
            summary=str(r.get('learning_summary') or r.get('learned_logic') or r.get('human_reply') or '').replace('\n',' ')
            if len(summary)>120: summary=summary[:117]+'...'
            lines.append(f'\n<b>#{r["id"]}</b> 🎯 <code>{html.escape(intent)}</code>\n🧠 {html.escape(summary)}')
            kb.append([InlineKeyboardButton(f'#{r["id"]} · {intent[:28]}',callback_data=f'admin:train:view:{r["id"]}')])
        kb.append([InlineKeyboardButton('⬅️ Train AI',callback_data='admin:train')])
        text='\n'.join(lines)
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def show_training_editor(q,path,learning_id):
    r=await training_one(path,learning_id)
    if not r:
        await show_training_list(q,path); return
    ctx=str(r['context_text'] or '').replace('\n',' ')
    if len(ctx)>700: ctx=ctx[:697]+'...'
    reply=str(r['human_reply'] or '')
    logic=str(r['learned_logic'] or '')
    summary=str(r.get('learning_summary') or logic or reply or '')
    text=(f'🧠 <b>Learning #{r["id"]}</b>\n\n'
          f'🎯 Intent: <code>{html.escape(str(r["intent"]))}</code>\n'
          f'🔎 Entity: {html.escape(str(r["entity"] or "None"))}\n'
          f'📌 Source: <b>{html.escape(str(r["source"]))}</b>\n\n'
          f'<b>📝 Learning Summary:</b>\n{html.escape(summary[:1500] or "None")}\n\n'
          f'<b>Context:</b>\n{html.escape(ctx or "None")}\n\n'
          f'<b>Human Reply:</b>\n{html.escape(reply[:1500])}\n\n'
          f'<b>Learned Logic:</b>\n{html.escape(logic[:1200] or "None")}')
    kb=[[InlineKeyboardButton('✏️ Edit Learning',callback_data=f'admin:train:edit:{learning_id}')],
        [InlineKeyboardButton('🗑 Delete Learning',callback_data=f'admin:train:delete:{learning_id}')],
        [InlineKeyboardButton('⬅️ Recent Learnings',callback_data='admin:train:list')]]
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def queue_training_read_request(path, mode, chat_link='', first_link='', last_link=''):
    await ensure_training_tables(path)
    async with aiosqlite.connect(path) as db:
        cur=await db.execute('INSERT INTO ai_training_read_requests(mode,chat_link,first_link,last_link,status) VALUES(?,?,?,?,?)',(mode,chat_link,first_link,last_link,'queued'))
        rid=cur.lastrowid
        await db.commit()
    return rid

async def behaviour_rule(path):
    return await get_rule(path, 'behaviour', {**DEFAULT_BEHAVIOUR, 'enabled': True})


def behaviour_keyboard(rule):
    def label(key, on, off):
        return on if rule.get(key, True) else off
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label('match_customer_language', '🌐 Language: ON', '🌐 Language: OFF'), callback_data='admin:beh:language')],
        [InlineKeyboardButton(label('concise_replies', '✂️ Concise replies: ON', '✂️ Concise replies: OFF'), callback_data='admin:beh:concise')],
        [InlineKeyboardButton(label('friendly_tone', '🙂 Friendly tone: ON', '🙂 Friendly tone: OFF'), callback_data='admin:beh:friendly')],
        [InlineKeyboardButton(label('ask_relevant_next_step', '➡️ Next step: ON', '➡️ Next step: OFF'), callback_data='admin:beh:next')],
        [InlineKeyboardButton(label('avoid_repetition', '🔁 Avoid repetition: ON', '🔁 Avoid repetition: OFF'), callback_data='admin:beh:repeat')],
        [InlineKeyboardButton(label('handle_multiple_intents', '🧩 Multiple intents: ON', '🧩 Multiple intents: OFF'), callback_data='admin:beh:multi')],
        [InlineKeyboardButton(label('never_expose_internal_info', '🔐 Hide internal info: ON', '🔐 Hide internal info: OFF'), callback_data='admin:beh:internal')],
        [InlineKeyboardButton(label('never_invent_business_info', '🛡️ No invention: ON', '🛡️ No invention: OFF'), callback_data='admin:beh:invent')],
        [InlineKeyboardButton('⬅️ AI', callback_data='admin:ai')]
    ])


async def show_behaviour(q, path):
    rule = await behaviour_rule(path)
    text = ('🤖 <b>AI Behaviour</b>\n\n'
            'These controls shape how the AI communicates.\n'
            'Authoritative batch/course data remains the source of truth.')
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=behaviour_keyboard(rule))


async def behaviour_context(path):
    rule = await behaviour_rule(path)
    labels = {
        'match_customer_language': 'Match customer language/style',
        'concise_replies': 'Keep replies concise',
        'friendly_tone': 'Use a friendly tone',
        'ask_relevant_next_step': 'Ask a relevant next step when useful',
        'avoid_repetition': 'Avoid unnecessary repetition',
        'handle_multiple_intents': 'Handle multiple intents in one message',
        'never_expose_internal_info': 'Never expose internal rules/system details',
        'never_invent_business_info': 'Never invent business information',
    }
    return 'AI BEHAVIOUR CONFIGURATION:\n' + '\n'.join(f'- {labels[k]}: {"ON" if rule.get(k, True) else "OFF"}' for k in labels)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I’m ready. Business batch/course data is connected.")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings: Settings = context.application.bot_data['settings']
    uid = update.effective_user.id if update.effective_user else None
    context.user_data.pop('pending_combo_command_v9', None)

    # PHASE28_COMBO_PHOTO_PRIORITY_FIX
    # Combo message collection must receive photos before any other
    # admin workflow can consume the update.
    cm = context.user_data.get('combo_new_message')
    if cm and is_admin(settings, uid):
        msg = update.message
        cid = cm.get('cid')

        # SIMPLE COMBO PHOTO COLLECTION
        # Every photo is saved immediately.
        # No Add Another / Done buttons are used.
        # The admin simply sends photos and types "done" when finished.

        if cm.get('type') == 'photo' and cm.get('step') in ('content', 'more_photo'):

            # Finish photo collection with the word "done"
            if msg.text is not None and msg.text.strip().lower() == 'done':

                photos = cm.get('photos', [])

                if not photos:
                    await update.message.reply_text(
                        '❌ No photo has been saved yet. Please send Photo 1 first.'
                    )
                    return

                cm['step'] = 'caption'
                context.user_data['combo_new_message'] = cm

                await update.message.reply_text(
                    f'✅ <b>Photo upload completed.</b>\n\n'
                    f'<b>{len(photos)}</b> photo(s) saved successfully.\n\n'
                    '✍️ Now send the caption.\n\n'
                    'The caption will be saved exactly as sent, including Telegram formatting.',
                    parse_mode='HTML'
                )
                return

            # Any incoming Telegram photo is immediately saved
            if msg.photo:

                d = Path(settings.media_root) / 'combos'
                d.mkdir(parents=True, exist_ok=True)

                ph = msg.photo[-1]
                tgfile = await context.bot.get_file(ph.file_id)

                photo_number = len(cm.get('photos', [])) + 1

                dest = d / (
                    f'combo_{cid}_msg_{int(time.time())}_'
                    f'{photo_number}_{ph.file_unique_id}.jpg'
                )

                await tgfile.download_to_drive(custom_path=str(dest))

                cm.setdefault('photos', []).append(str(dest))
                cm['step'] = 'more_photo'
                context.user_data['combo_new_message'] = cm

                await update.message.reply_text(
                    f'✅ <b>Photo {photo_number} saved.</b>\n\n'
                    'Send another photo if you want.\n'
                    'When you have finished adding photos, type <code>done</code>.',
                    parse_mode='HTML'
                )
                return

            # Anything other than a photo/done while collecting photos
            await update.message.reply_text(
                '🖼️ Please send a photo.\n\n'
                'When you have finished adding photos, type <code>done</code>.',
                parse_mode='HTML'
            )
            return

        if cm.get('type') == 'photo' and cm.get('step') == 'caption' and (
            msg.text is not None or msg.caption is not None
        ):
            cap, ents = telegram_entities_payload(msg)
            cm['caption'] = cap
            cm['caption_entities_json'] = json.dumps(ents, ensure_ascii=False)
            cm['step'] = 'link_text'
            context.user_data['combo_new_message'] = cm

            await update.message.reply_text(
                '🔗 <b>Embedded Link</b>\n\n'
                'Send the exact word or line from the caption that should become clickable, '
                'or tap Skip.',
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        '⏭️ Skip',
                        callback_data=f'admin:combo:link_skip:{cid}'
                    )]
                ])
            )
            return

        if cm.get('step') == 'link_text' and msg.text is not None:
            visible = msg.text.strip()
            cap = cm.get('caption', '')

            if visible and visible in cap:
                cm['link_visible'] = visible
                cm['step'] = 'link_url'
                context.user_data['combo_new_message'] = cm

                await update.message.reply_text(
                    '🔗 <b>Now send the URL</b>\n\n'
                    'The URL will stay hidden; only the selected word/line will be clickable.',
                    parse_mode='HTML'
                )
                return

            await update.message.reply_text(
                '❌ That exact word/line is not present in the caption. '
                'Send it exactly as it appears.'
            )
            return

        if cm.get('step') == 'link_url' and msg.text is not None:
            url = msg.text.strip()

            if not re.match(r'^(?:https?://|tg://)', url, re.I):
                await update.message.reply_text(
                    'Please send a valid http:// or https:// URL.'
                )
                return

            cap = cm.get('caption', '')
            visible = cm.get('link_visible', '')
            ents = json.loads(cm.get('caption_entities_json') or '[]')

            idx = cap.find(visible)
            if idx < 0:
                await update.message.reply_text(
                    '❌ Selected text is no longer present in the caption.'
                )
                return

            ents.append({
                'type': 'text_link',
                'offset': _utf16_len(cap[:idx]),
                'length': _utf16_len(visible),
                'url': url
            })

            cm['caption_entities_json'] = json.dumps(ents, ensure_ascii=False)
            cm['step'] = 'save_photo'
            context.user_data['combo_new_message'] = cm

            await update.message.reply_text(
                '🔗 <b>Embedded link set successfully.</b>\n\n'
                'Now save this message.',
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        '💾 Save Message',
                        callback_data=f'admin:combo:linksave:{cid}'
                    )],
                    [InlineKeyboardButton(
                        '❌ Cancel',
                        callback_data=f'admin:combo:view:{cid}'
                    )]
                ])
            )
            return
    if not is_admin(settings, uid):
        await update.message.reply_text(f'Not authorized. Your Telegram ID is {uid}.')
        return
    c = await counts(settings.database_path)
    
    try:
        async with aiosqlite.connect(settings.database_path) as db:
            combo_count=int((await (await db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='combos'")).fetchone())[0])
            if combo_count:
                combo_count=int((await (await db.execute('SELECT COUNT(*) FROM combos')).fetchone())[0])
    except Exception:
        combo_count=0
    await update.message.reply_text(main_text(c)+f'\n🎁 Combos: <b>{combo_count}</b>', parse_mode='HTML', reply_markup=admin_keyboard())



def batches_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔍 Search Batches', callback_data='admin:batch:search')],
        [InlineKeyboardButton('➕ Add Batch', callback_data='admin:batch:add')],
        [InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')],
    ])

def batch_skip_keyboard(next_action):
    return InlineKeyboardMarkup([[InlineKeyboardButton('⏭️ Skip', callback_data=f'admin:batch:skip:{next_action}')],
                                 [InlineKeyboardButton('❌ Cancel', callback_data='admin:batches')]])

def batch_detail_keyboard(batch):
    bid=int(batch['id'])
    enabled='🟢' if batch.get('enabled') else '🔴'
    access='ON' if batch.get('access_link_enabled') else 'OFF'
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📡 Register / Verify Channel', callback_data=f'admin:batch:verify:{bid}')],
        [InlineKeyboardButton('🔗 PRE ACCESS Private Link', callback_data=f'admin:prelink:view:batch:{bid}')],
        [InlineKeyboardButton(f'🔐 24h + 1-person: {access}', callback_data=f'admin:batch:access:{bid}')],
        [InlineKeyboardButton('✏️ Edit Batch', callback_data=f'admin:batch:edit:{bid}')],
        [InlineKeyboardButton('⬅️ Batches', callback_data='admin:batches')],
    ])

async def show_batches(update_or_query, path):
    c=await counts(path)
    text=f'📚 <b>Batches</b>\n\nSaved batches: <b>{c["batches"]}</b>\n\nSearch an existing batch or add a new one.'
    kb=batches_menu()
    if hasattr(update_or_query,'edit_message_text'):
        await update_or_query.edit_message_text(text,parse_mode='HTML',reply_markup=kb)
    else:
        await update_or_query.message.reply_text(text,parse_mode='HTML',reply_markup=kb)

async def show_batch_search_results(q, path, query):
    rows=search_batches(path,query,15)
    if not rows:
        await q.edit_message_text(f'🔍 <b>Batch Search</b>\n\nNo batch found for <code>{html.escape(query)}</code>.',parse_mode='HTML',reply_markup=batches_menu()); return
    buttons=[]
    for r in rows:
        status='✅' if r.get('enabled') else '⛔'
        buttons.append([InlineKeyboardButton(f'{status} {r["name"]}',callback_data=f'admin:batch:view:{r["id"]}')])
    buttons.append([InlineKeyboardButton('🔍 Search Again',callback_data='admin:batch:search')])
    buttons.append([InlineKeyboardButton('⬅️ Batches',callback_data='admin:batches')])
    await q.edit_message_text(f'🔍 <b>Batch Search</b>\n\nResults for <code>{html.escape(query)}</code>:',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(buttons))


def batch_edit_keyboard(batch):
    bid=int(batch['id'])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✏️ Name', callback_data=f'admin:batch:field:{bid}:name'), InlineKeyboardButton('📌 Chat/Group', callback_data=f'admin:batch:verify:{bid}')],
        [InlineKeyboardButton('📝 Description', callback_data=f'admin:batch:field:{bid}:description'), InlineKeyboardButton('💰 Fee', callback_data=f'admin:batch:field:{bid}:fee')],
        [InlineKeyboardButton('🎬 Demo Link', callback_data=f'admin:batch:field:{bid}:demo_link'), InlineKeyboardButton('💳 Purchase Link', callback_data=f'admin:batch:field:{bid}:pay_link')],
        [InlineKeyboardButton('🔎 Keywords', callback_data=f'admin:batch:field:{bid}:search_keywords'), InlineKeyboardButton('🖼️ Batch Image', callback_data=f'admin:batch:image:{bid}')],
        [InlineKeyboardButton('🔗 Request Link', callback_data=f'admin:batch:request:{bid}'), InlineKeyboardButton('✏️ Change Request Link', callback_data=f'admin:batch:field:{bid}:request_link')],
        [InlineKeyboardButton('🗑️ Delete', callback_data=f'admin:batch:delete:{bid}')],
        [InlineKeyboardButton('⬅️ Batches', callback_data='admin:batches')],
    ])

async def show_batch_editor(q,path,bid):
    row=get_batch(path,bid)
    if not row:
        await q.edit_message_text('Batch not found.', reply_markup=batches_menu()); return
    def v(x): return html.escape(str(x)) if x not in (None,'') else '—'
    text=(f'✏️ <b>Edit Batch</b>\n\n<b>{v(row.get("name"))}</b>\n\n'
          f'💰 Fee: {v(row.get("fee"))}\n'
          f'⏳ Duration: {v(row.get("duration"))}\n'
          f'🕐 Timing: {v(row.get("timing"))}\n'
          f'🎬 Demo: {"YES" if row.get("demo_link") else "NO"}\n'
          f'💳 Purchase: {"YES" if row.get("pay_link") else "NO"}\n'
          f'🔗 Request Link: {"YES" if row.get("request_link") else "NO"}\n'
          f'📡 Channel verified: {"YES ✅" if row.get("telegram_verified") else "NO ❌"}')
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=batch_edit_keyboard(row))

async def prompt_batch_field(q, context, bid, field):
    labels={'name':'batch name','description':'description','fee':'fee','demo_link':'demo URL','pay_link':'purchase/payment URL','search_keywords':'keywords/aliases','request_link':'request link'}
    label=labels.get(field,field)
    context.user_data['pending_batch_edit']={'id':bid,'field':field}
    await q.edit_message_text(f'✏️ <b>Edit {html.escape(label.title())}</b>\n\nSend the new value.\n\nSend <code>skip</code> to clear this field.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:batch:edit:{bid}')]]))

async def show_batch_request_link(q,path,bid):
    row=get_batch(path,bid)
    if not row:
        await q.edit_message_text('Batch not found.',reply_markup=batches_menu()); return
    link=row.get('request_link')
    text='🔗 <b>Request Link</b>\n\n'+(html.escape(str(link)) if link else 'Not configured.')
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton('✏️ Change Request Link',callback_data=f'admin:batch:field:{bid}:request_link')],
        [InlineKeyboardButton('⬅️ Edit Batch',callback_data=f'admin:batch:edit:{bid}')]
    ]))

async def show_batch_image_editor(q,path,bid):
    row=get_batch(path,bid)
    if not row:
        await q.edit_message_text('Batch not found.',reply_markup=batches_menu()); return
    context=q
    await q.edit_message_text('🖼️ <b>Batch Image</b>\n\nSend a new image in this chat, or remove the current image.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton('🗑️ Remove Image',callback_data=f'admin:batch:image_remove:{bid}')],
        [InlineKeyboardButton('⬅️ Edit Batch',callback_data=f'admin:batch:edit:{bid}')]
    ]))

async def show_batch(q,path,bid):
    row=get_batch(path,bid)
    if not row:
        await q.edit_message_text('Batch not found.',reply_markup=batches_menu()); return
    def v(x): return html.escape(str(x)) if x not in (None,'') else '—'
    verified='YES ✅' if row.get('telegram_verified') else 'NO ❌'
    access='ON ✅' if row.get('access_link_enabled') else 'OFF ⛔'
    text=(f'📚 <b>{v(row["name"])}</b>\n\n'
          f'💰 Fee: {v(row.get("fee"))}\n⏳ Duration: {v(row.get("duration"))}\n🕐 Timing: {v(row.get("timing"))}\n'
          f'📡 Channel verified: <b>{verified}</b>\n🔐 24h + 1-person: <b>{access}</b>\n'
          f'⌨️ Slash trigger: <code>/{v(row.get("slash_trigger") or slug_trigger(row["name"]))}</code>')
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=batch_detail_keyboard(row))

async def start_batch_add(q,context):
    context.user_data['batch_add']={'step':'name','data':{}}
    await q.edit_message_text('➕ <b>Add Batch</b>\n\n<b>Step 1/9 — Batch name</b>\n\nSend the exact batch name.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data='admin:batches')]]))

async def batch_prompt(update, text, keyboard=None):
    await update.message.reply_text(text,parse_mode='HTML',reply_markup=keyboard)

async def verify_batch_channel(context, link):
    chat_id,msg_id=parse_message_link(link)
    me=await context.bot.get_me()
    member=await context.bot.get_chat_member(chat_id,me.id)
    if member.status not in ('administrator','creator'):
        raise ValueError('The bot is not an admin in this channel/group.')
    can_invite=bool(getattr(member,'can_invite_users',False))
    if not can_invite:
        raise ValueError('The bot is admin, but it does not have permission to invite users.')
    chat=await context.bot.get_chat(chat_id)
    return str(chat.id),msg_id,chat.title or str(chat.id),can_invite

async def finish_batch_add(update,context):
    settings=context.application.bot_data['settings']
    d=context.user_data['batch_add']['data']
    bid=insert_batch(settings.database_path,d)
    context.user_data.pop('batch_add',None)
    row=get_batch(settings.database_path,bid)
    await update.message.reply_text(
        f'✅ <b>Batch Added Successfully</b>\n\n<b>{html.escape(row["name"])}</b>\n'
        f'Fee: {html.escape(str(row.get("fee") or "—"))}\n'
        f'24h + 1-person generation: {"ON ✅" if row.get("access_link_enabled") else "OFF ⛔"}\n'
        f'Slash trigger: <code>/{html.escape(str(row.get("slash_trigger") or ""))}</code>',
        parse_mode='HTML',reply_markup=batch_detail_keyboard(row))

async def process_batch_add_message(update,context):
    settings=context.application.bot_data['settings']
    state=context.user_data.get('batch_add')
    if not state or not is_admin(settings,update.effective_user.id): return False
    d=state['data']; step=state['step']
    msg=update.message
    if step=='image' and msg.photo:
        media_dir=Path(settings.media_root)/'batches'
        media_dir.mkdir(parents=True,exist_ok=True)
        photo=msg.photo[-1]
        tgfile=await context.bot.get_file(photo.file_id)
        dest=media_dir/f'batch_{int(time.time())}_{photo.file_unique_id}.jpg'
        await tgfile.download_to_drive(custom_path=str(dest))
        d['image_path']=str(dest)
        state['step']='description'
        await batch_prompt(update,'📝 <b>Step 4/9 — Description</b>\n\nSend the description, or type <code>skip</code>.',batch_skip_keyboard('description'))
        return True
    if not msg.text: return True
    raw=msg.text.strip()
    if step=='name':
        if not raw: await batch_prompt(update,'Batch name cannot be empty.'); return True
        d['name']=raw; state['step']='channel'
        await batch_prompt(update,'📡 <b>Step 2/9 — Private channel/group message link</b>\n\nPaste one Telegram message link from the batch channel/group. I will verify the bot is admin and can create invite links.'); return True
    if step=='channel':
        try:
            chat_id,msg_id,title,can_invite=await verify_batch_channel(context,raw)
        except Exception as e:
            await batch_prompt(update,f'❌ <b>Channel verification failed</b>\n\n{html.escape(str(e))}\n\nPlease send another message link.')
            return True
        d.update({'telegram_chat_id':chat_id,'telegram_message_id':msg_id,'telegram_verified':1,'telegram_can_invite':1,'bot_chat_id':chat_id,'chat':title,'slash_trigger':slug_trigger(d['name'])})
        state['step']='image'
        await batch_prompt(update,f'✅ <b>Channel verified successfully</b>\n\nBot admin: YES ✅\nInvite-link permission: YES ✅\n\n🖼️ <b>Step 3/9 — Batch image</b>\n\nSend an image or press Skip.',InlineKeyboardMarkup([[InlineKeyboardButton('⏭️ Skip',callback_data='admin:batch:skip:image')],[InlineKeyboardButton('❌ Cancel',callback_data='admin:batches')]])); return True
    if raw.lower()=='skip':
        if step in ('image','description','timing','demo','payment'):
            nxt={'image':'description','description':'fee','timing':'demo','demo':'payment','payment':'access'}[step]
            state['step']=nxt
            prompts={'description':'📝 <b>Step 4/9 — Description</b>\n\nSend description or skip.',
                     'fee':'💰 <b>Step 5/9 — Fee</b>\n\nSend the fee, or type skip.',
                     'duration':'⏳ <b>Step 6/9 — Duration</b>\n\nSend duration, or type skip.',
                     'timing':'🕐 <b>Step 7/9 — Timing</b>\n\nSend timing, or type skip.',
                     'demo':'🎬 <b>Step 8/9 — Demo link</b>\n\nSend the demo URL, or type skip.',
                     'payment':'💳 <b>Step 9/9 — Payment link</b>\n\nSend the payment URL, or type skip.',
                     'access':'🔐 <b>24-hour + 1-person generation</b>\n\nShould this batch support your admin-only access-link generation?'} 
            if nxt=='access':
                await batch_prompt(update,prompts[nxt],InlineKeyboardMarkup([[InlineKeyboardButton('🟢 ON',callback_data='admin:batch:access_add:1')],[InlineKeyboardButton('🔴 OFF',callback_data='admin:batch:access_add:0')]]))
            else: await batch_prompt(update,prompts[nxt],batch_skip_keyboard(nxt))
            return True
    if step=='description': d['description']=raw; state['step']='fee'; await batch_prompt(update,'💰 <b>Step 5/9 — Fee</b>\n\nSend the fee, or type <code>skip</code>.',batch_skip_keyboard('fee')); return True
    if step=='fee': d['fee']=raw; state['step']='duration'; await batch_prompt(update,'⏳ <b>Step 6/9 — Duration</b>\n\nSend duration, or type <code>skip</code>.',batch_skip_keyboard('duration')); return True
    if step=='duration': d['duration']=raw; state['step']='timing'; await batch_prompt(update,'🕐 <b>Step 7/9 — Timing</b>\n\nSend timing, or type <code>skip</code>.',batch_skip_keyboard('timing')); return True
    if step=='timing': d['timing']=raw; state['step']='demo'; await batch_prompt(update,'🎬 <b>Step 8/9 — Demo link</b>\n\nSend the demo URL, or type <code>skip</code>.',batch_skip_keyboard('demo')); return True
    if step=='demo': d['demo_link']=raw; state['step']='payment'; await batch_prompt(update,'💳 <b>Step 9/9 — Payment link</b>\n\nSend the payment URL, or type <code>skip</code>.',batch_skip_keyboard('payment')); return True
    if step=='payment': d['pay_link']=raw; state['step']='access'; await batch_prompt(update,'🔐 <b>24-hour + 1-person generation</b>\n\nChoose ON or OFF.',InlineKeyboardMarkup([[InlineKeyboardButton('🟢 ON',callback_data='admin:batch:access_add:1')],[InlineKeyboardButton('🔴 OFF',callback_data='admin:batch:access_add:0')]])); return True
    return True

async def handle_batch_slash(update,context):
    settings=context.application.bot_data['settings']
    if not update.message or not update.message.text or not update.message.text.startswith('/'): return False
    if not is_admin(settings,update.effective_user.id): return False
    cmd=update.message.text.split()[0][1:].split('@')[0].lower()
    if not cmd: return False
    rows=search_batches(settings.database_path,cmd,10)
    exact=[r for r in rows if slug_trigger(r['name'])==cmd or str(r['name']).lower()==cmd]
    if not exact: return False
    row=get_batch(settings.database_path,exact[0]['id'])
    if not row.get('access_link_enabled') or not row.get('telegram_verified') or not row.get('telegram_can_invite'):
        await update.message.reply_text('❌ This batch is not configured for 24-hour + 1-person access yet.'); return True
    await update.message.reply_text('ℹ️ The admin-only batch trigger is registered. Main-account delivery will be activated when the main-account interface is connected.')
    return True


def intent_triggers_keyboard(rows):
    buttons=[]
    for row in rows[:30]:
        status='✅' if row['enabled'] else '❌'
        label=f'{status} {row["intent"]} → {row["trigger"]}'
        if len(label)>55: label=label[:52]+'...'
        buttons.append([InlineKeyboardButton(label, callback_data=f'admin:it:view:{row["id"]}')])
    buttons.append([InlineKeyboardButton('➕ Add Intent → Trigger', callback_data='admin:it:add')])
    buttons.append([InlineKeyboardButton('⬅️ AI', callback_data='admin:ai')])
    return InlineKeyboardMarkup(buttons)


async def show_intent_triggers(q, path):
    rows=await intent_trigger_rows(path)
    text=('🎯 <b>Intent → Trigger</b>\n\n'
          'AI identifies the configured customer intent. The mapped slash trigger is then executed by the normal deterministic command system.\n\n'
          + ('No mappings saved yet.' if not rows else f'Saved mappings: <b>{len(rows)}</b>'))
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=intent_triggers_keyboard(rows))


async def show_intent_trigger_editor(q,path,row_id):
    rows=await intent_trigger_rows(path)
    row=next((r for r in rows if int(r['id'])==int(row_id)),None)
    if not row:
        await show_intent_triggers(q,path); return
    status='ON' if row['enabled'] else 'OFF'
    await q.edit_message_text(
        f'🎯 <b>Intent → Trigger</b>\n\n'
        f'Intent: <code>{html.escape(str(row["intent"]))}</code>\n'
        f'Trigger: <code>{html.escape(str(row["trigger"]))}</code>\n'
        f'Status: <b>{status}</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f'🔄 Toggle {status}',callback_data=f'admin:it:toggle:{row_id}')],
            [InlineKeyboardButton('🗑 Delete',callback_data=f'admin:it:delete:{row_id}')],
            [InlineKeyboardButton('⬅️ Intent → Trigger',callback_data='admin:intent_triggers')]
        ]))


# ---- Shortcut admin UI ----
async def sc_cols(path):
    async with aiosqlite.connect(path) as d:
        c=[r[1] for r in await (await d.execute('PRAGMA table_info(shortcuts)')).fetchall()]
        if 'trigger' not in c:
            await d.execute('ALTER TABLE shortcuts ADD COLUMN trigger TEXT'); await d.execute("UPDATE shortcuts SET trigger=name WHERE trigger IS NULL OR trim(trigger)=''"); await d.commit()
async def sc_rows(path):
    await sc_cols(path)
    async with aiosqlite.connect(path) as d:
        d.row_factory=aiosqlite.Row; return [dict(r) for r in await (await d.execute('SELECT * FROM shortcuts ORDER BY id')).fetchall()]
async def sc_one(path,sid):
    await sc_cols(path)
    async with aiosqlite.connect(path) as d:
        d.row_factory=aiosqlite.Row; r=await (await d.execute('SELECT * FROM shortcuts WHERE id=?',(sid,))).fetchone(); return dict(r) if r else None
async def sc_items(path,sid):
    async with aiosqlite.connect(path) as d:
        d.row_factory=aiosqlite.Row; c=[r[1] for r in await (await d.execute('PRAGMA table_info(shortcut_items)')).fetchall()]; o='item_order' if 'item_order' in c else 'sort_order'; return [dict(r) for r in await (await d.execute(f'SELECT * FROM shortcut_items WHERE shortcut_id=? ORDER BY {o},id',(sid,))).fetchall()]
async def sc_add(path,name,trigger):
    await sc_cols(path)
    async with aiosqlite.connect(path) as d:
        r=await d.execute('INSERT INTO shortcuts(name,enabled,trigger) VALUES(?,?,?)',(name,1,trigger)); sid=r.lastrowid; await d.commit(); return sid
async def sc_update(path,sid,**kw):
    await sc_cols(path); kw={k:v for k,v in kw.items() if k in {'name','trigger','enabled'}}
    if not kw:return
    async with aiosqlite.connect(path) as d: await d.execute('UPDATE shortcuts SET '+','.join(k+'=?' for k in kw)+' WHERE id=?',list(kw.values())+[sid]); await d.commit()
async def sc_additem(path,sid,text='',media='',caption=''):
    async with aiosqlite.connect(path) as d:
        c=[r[1] for r in await (await d.execute('PRAGMA table_info(shortcut_items)')).fetchall()]; o='item_order' if 'item_order' in c else 'sort_order'; n=(await (await d.execute(f'SELECT COALESCE(MAX({o}),0)+1 FROM shortcut_items WHERE shortcut_id=?',(sid,))).fetchone())[0]
        await d.execute(f'INSERT INTO shortcut_items(shortcut_id,{o},enabled,text_content,media_path,caption,button_data_json,extra_json) VALUES(?,?,?,?,?,?,?,?)',(sid,n,1,text,media,caption,'','')); await d.commit()
async def sc_item(path,iid):
    async with aiosqlite.connect(path) as d:
        d.row_factory=aiosqlite.Row; r=await (await d.execute('SELECT * FROM shortcut_items WHERE id=?',(iid,))).fetchone(); return dict(r) if r else None
async def sc_item_update(path,iid,**kw):
    kw={k:v for k,v in kw.items() if k in {'text_content','media_path','caption','button_data_json','enabled'}}
    if not kw:return
    async with aiosqlite.connect(path) as d: await d.execute('UPDATE shortcut_items SET '+','.join(k+'=?' for k in kw)+' WHERE id=?',list(kw.values())+[iid]); await d.commit()
async def sc_delete(path,sid):
    async with aiosqlite.connect(path) as d: await d.execute('DELETE FROM shortcut_items WHERE shortcut_id=?',(sid,)); await d.execute('DELETE FROM shortcuts WHERE id=?',(sid,)); await d.commit()
async def sc_delitem(path,iid):
    async with aiosqlite.connect(path) as d: await d.execute('DELETE FROM shortcut_items WHERE id=?',(iid,)); await d.commit()
async def sc_move(path,sid,iid,direction):
    rows=await sc_items(path,sid); i=next((x for x,r in enumerate(rows) if r['id']==iid),None); j=i-1 if direction=='up' else i+1
    if i is None or j<0 or j>=len(rows): return
    a,b=rows[i],rows[j]; o='item_order' if 'item_order' in a else 'sort_order'
    async with aiosqlite.connect(path) as d: await d.execute(f'UPDATE shortcut_items SET {o}=? WHERE id=?',(b[o],a['id'])); await d.execute(f'UPDATE shortcut_items SET {o}=? WHERE id=?',(a[o],b['id'])); await d.commit()
async def show_shortcuts(q,path):
    rows=await sc_rows(path); lines=['⚡ <b>Shortcuts</b>','',f'Saved: <b>{len(rows)}</b>','']+[f'{"🟢" if r["enabled"] else "🔴"} {html.escape(r["name"])} — <code>/{html.escape(r.get("trigger") or r["name"])}</code>' for r in rows]
    kb=[[InlineKeyboardButton('➕ Add Shortcut',callback_data='admin:sc:add')],[InlineKeyboardButton('📋 Saved Shortcuts',callback_data='admin:sc:list')],[InlineKeyboardButton('⬅️ Admin Home',callback_data='admin:home')]]; await q.edit_message_text('\n'.join(lines),parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))
async def show_sc_list(q,path):
    rows=await sc_rows(path); kb=[[InlineKeyboardButton(('🟢 ' if r['enabled'] else '🔴 ')+r['name'],callback_data=f'admin:sc:view:{r["id"]}')] for r in rows]; kb += [[InlineKeyboardButton('➕ Add Shortcut',callback_data='admin:sc:add')],[InlineKeyboardButton('⬅️ Shortcuts',callback_data='admin:shortcuts')]]; await q.edit_message_text('📋 <b>Saved Shortcuts</b>\n\nTap a shortcut:',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))
async def show_sc_editor(q,path,sid):
    r=await sc_one(path,sid)
    if not r:return await show_shortcuts(q,path)
    its=await sc_items(path,sid); t=f'⚡ <b>{html.escape(r["name"])}</b>\n\nStatus: <b>{"ON" if r["enabled"] else "OFF"}</b>\nTrigger: <code>/{html.escape(r.get("trigger") or r["name"])}</code>\nItems: <b>{len(its)}</b>'
    kb=[[InlineKeyboardButton('➕ Add Item',callback_data=f'admin:sc:additem:{sid}')],[InlineKeyboardButton('✏️ Rename',callback_data=f'admin:sc:rename:{sid}'),InlineKeyboardButton('⌨️ Trigger',callback_data=f'admin:sc:trigger:{sid}')],[InlineKeyboardButton('🔴 Disable' if r['enabled'] else '🟢 Enable',callback_data=f'admin:sc:toggle:{sid}')]]
    for n,it in enumerate(its,1): kb.append([InlineKeyboardButton(f'{n}. {"📝 Text" if it.get("text_content") else "🖼️ Image"}',callback_data=f'admin:sc:item:{it["id"]}')])
    kb += [[InlineKeyboardButton('🗑️ Delete Shortcut',callback_data=f'admin:sc:delete:{sid}')],[InlineKeyboardButton('⬅️ Saved Shortcuts',callback_data='admin:sc:list')]]; await q.edit_message_text(t,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))
async def show_sc_item(q,path,iid):
    it=await sc_item(path,iid); typ='Text' if it and it.get('text_content') else 'Image'; t=f'🧩 <b>Item #{iid}</b>\n\nType: {typ}\nEnabled: {"ON" if it.get("enabled",1) else "OFF"}\n\n{html.escape(str(it.get("caption") or it.get("text_content") or "")[:700])}' if it else 'Item not found.'
    if not it:return await q.edit_message_text(t)
    kb=[[InlineKeyboardButton('✏️ Edit Content',callback_data=f'admin:sc:edititem:{iid}')],[InlineKeyboardButton('🔗 Embedded Link',callback_data=f'admin:sc:link:{iid}')],[InlineKeyboardButton('🔴 Disable' if it['enabled'] else '🟢 Enable',callback_data=f'admin:sc:itemtoggle:{iid}')],[InlineKeyboardButton('⬆️ Up',callback_data=f'admin:sc:up:{iid}'),InlineKeyboardButton('⬇️ Down',callback_data=f'admin:sc:down:{iid}')],[InlineKeyboardButton('🗑️ Delete Item',callback_data=f'admin:sc:itemdelete:{iid}')],[InlineKeyboardButton('⬅️ Shortcut',callback_data=f'admin:sc:parent:{it["shortcut_id"]}')]]; await q.edit_message_text(t,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))


# ---- Combo management / Pre Access ----
def _entity_to_dict(entity):
    d = entity.to_dict()
    return {k: v for k, v in d.items() if v is not None}


def telegram_entities_payload(message):
    """Store original text/caption and Telegram-native entities."""
    text = message.text or message.caption or ''
    entities = message.entities or message.caption_entities or []
    return text, [_entity_to_dict(e) for e in entities]


def _message_entities_from_json(raw):
    result=[]
    try:
        for item in json.loads(raw or '[]'):
            if isinstance(item, dict) and item.get('type'):
                result.append(MessageEntity(**item))
    except Exception as exc:
        logger.warning('Could not restore combo entities: %s', exc)
    return result


def _utf16_len(text):
    return len(text.encode('utf-16-le')) // 2


def add_text_link_entity(text, entities, visible):
    """Add one exact text_link entity without destroying existing formatting."""
    visible = visible.strip()
    if not visible or visible not in text:
        raise ValueError('That exact word/line was not found in the caption.')
    idx = text.find(visible)
    offset = _utf16_len(text[:idx])
    length = _utf16_len(visible)
    out = [dict(x) for x in entities]
    out.append({'type':'text_link','offset':offset,'length':length,'url':''})
    return out


async def ensure_combo_pre_tables(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS combos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '', caption TEXT NOT NULL DEFAULT '', caption_entities_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        try:
            await db.execute("ALTER TABLE combos ADD COLUMN caption_entities_json TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass
        await db.execute("""CREATE TABLE IF NOT EXISTS combo_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, combo_id INTEGER NOT NULL, item_order INTEGER NOT NULL DEFAULT 0,
            media_path TEXT NOT NULL DEFAULT '', FOREIGN KEY(combo_id) REFERENCES combos(id) ON DELETE CASCADE
        )""")
        # New architecture: a Combo is an ordered list of independent messages.
        # Each message can be text OR one/more photos with one common caption.
        await db.execute("""CREATE TABLE IF NOT EXISTS combo_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            combo_id INTEGER NOT NULL,
            message_order INTEGER NOT NULL DEFAULT 0,
            message_type TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            media_paths_json TEXT NOT NULL DEFAULT '[]',
            caption TEXT NOT NULL DEFAULT '',
            caption_entities_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(combo_id) REFERENCES combos(id) ON DELETE CASCADE
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS pre_access_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        await db.execute("INSERT OR IGNORE INTO pre_access_settings(key,value) VALUES('duration_minutes','5')")
        await db.execute("""CREATE TABLE IF NOT EXISTS pre_access_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer_chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            target_chat_id INTEGER NOT NULL, target_type TEXT NOT NULL, target_name TEXT NOT NULL,
            invite_link TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'issued',
            issued_at INTEGER NOT NULL, joined_at INTEGER, expires_at INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()


async def combo_rows(path):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        return [dict(r) for r in await (await db.execute('SELECT * FROM combos ORDER BY id DESC')).fetchall()]


async def combo_one(path,cid):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        r=await (await db.execute('SELECT * FROM combos WHERE id=?',(cid,))).fetchone()
        return dict(r) if r else None


async def combo_items(path,cid):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        return [dict(r) for r in await (await db.execute('SELECT * FROM combo_items WHERE combo_id=? ORDER BY item_order,id',(cid,))).fetchall()]


async def combo_messages(path,cid):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        return [dict(r) for r in await (await db.execute('SELECT * FROM combo_messages WHERE combo_id=? ORDER BY message_order,id',(cid,))).fetchall()]


async def combo_create(path,name,price,details):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        r=await db.execute('INSERT INTO combos(name,price,details,caption,caption_entities_json,enabled) VALUES(?,?,?,?,?,1)',(name,price,details,'','[]'))
        cid=r.lastrowid; await db.commit(); return cid


async def combo_update(path,cid,**kw):
    await ensure_combo_pre_tables(path)
    if not kw:return
    async with aiosqlite.connect(path) as db:
        await db.execute('UPDATE combos SET '+','.join(k+'=?' for k in kw)+' ,updated_at=CURRENT_TIMESTAMP WHERE id=?',list(kw.values())+[cid]); await db.commit()


async def combo_delete(path,cid):
    await ensure_combo_pre_tables(path)
    await delete_combo_pre_message(path,cid)
    async with aiosqlite.connect(path) as db:
        await db.execute('DELETE FROM combo_messages WHERE combo_id=?',(cid,))
        await db.execute('DELETE FROM combo_items WHERE combo_id=?',(cid,))
        await db.execute('DELETE FROM combos WHERE id=?',(cid,)); await db.commit()


async def combo_add_message(path,cid,message_type,text='',media_paths=None,caption='',caption_entities_json='[]'):
    await ensure_combo_pre_tables(path)
    media_paths=media_paths or []
    async with aiosqlite.connect(path) as db:
        n=(await (await db.execute('SELECT COALESCE(MAX(message_order),0)+1 FROM combo_messages WHERE combo_id=?',(cid,))).fetchone())[0]
        r=await db.execute('INSERT INTO combo_messages(combo_id,message_order,message_type,text,media_paths_json,caption,caption_entities_json) VALUES(?,?,?,?,?,?,?)',(cid,n,message_type,text,json.dumps(media_paths,ensure_ascii=False),caption,caption_entities_json))
        mid=r.lastrowid; await db.commit(); return mid


async def combo_delete_message(path,mid):
    async with aiosqlite.connect(path) as db:
        await db.execute('DELETE FROM combo_messages WHERE id=?',(mid,)); await db.commit()


# COMBO_COMMAND_SYSTEM_V9
async def ensure_combo_command_system_v9(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS combo_commands_v9 (combo_id INTEGER PRIMARY KEY, command TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS all_combo_command_v9 (id INTEGER PRIMARY KEY CHECK(id=1), command TEXT NOT NULL DEFAULT 'allcombo', enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS all_combo_children_v9 (id INTEGER PRIMARY KEY AUTOINCREMENT, combo_id INTEGER NOT NULL UNIQUE, command TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0)")
        await db.execute("INSERT OR IGNORE INTO all_combo_command_v9(id,command,enabled) VALUES(1,'allcombo',1)")
        await db.commit()

async def combo_command_v9(path,cid):
    await ensure_combo_command_system_v9(path)
    async with aiosqlite.connect(path) as db:
        r=await (await db.execute("SELECT command FROM combo_commands_v9 WHERE combo_id=? AND enabled=1",(cid,))).fetchone()
    return str(r[0] or "").strip() if r else ""

async def save_combo_command_v9(path,cid,command):
    await ensure_combo_command_system_v9(path)
    command=str(command or "").strip().lstrip("/")
    if not command: raise ValueError("empty command")
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO combo_commands_v9(combo_id,command,enabled) VALUES(?,?,1) ON CONFLICT(combo_id) DO UPDATE SET command=excluded.command,enabled=1,updated_at=CURRENT_TIMESTAMP",(cid,command))
        await db.commit()

async def all_combo_command_v9(path):
    await ensure_combo_command_system_v9(path)
    async with aiosqlite.connect(path) as db:
        r=await (await db.execute("SELECT command FROM all_combo_command_v9 WHERE id=1 AND enabled=1")).fetchone()
    return str(r[0] or "").strip() if r else ""

async def save_all_combo_command_v9(path,command):
    await ensure_combo_command_system_v9(path)
    command=str(command or "").strip().lstrip("/")
    if not command: raise ValueError("empty command")
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE all_combo_command_v9 SET command=?,enabled=1,updated_at=CURRENT_TIMESTAMP WHERE id=1",(command,))
        await db.commit()

async def all_combo_children_v9(path):
    await ensure_combo_command_system_v9(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=await (await db.execute("SELECT a.id,a.combo_id,a.command,a.enabled,a.sort_order,c.name FROM all_combo_children_v9 a LEFT JOIN combos c ON c.id=a.combo_id WHERE a.enabled=1 ORDER BY a.sort_order,a.id")).fetchall()
    return [dict(x) for x in rows]

async def save_all_combo_child_v9(path,cid,command):
    await ensure_combo_command_system_v9(path)
    command=str(command or "").strip().lstrip("/")
    if not command: raise ValueError("empty command")
    async with aiosqlite.connect(path) as db:
        n=(await (await db.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM all_combo_children_v9")).fetchone())[0]
        await db.execute("INSERT INTO all_combo_children_v9(combo_id,command,enabled,sort_order) VALUES(?,?,1,?) ON CONFLICT(combo_id) DO UPDATE SET command=excluded.command,enabled=1,sort_order=excluded.sort_order",(cid,command,n))
        await db.commit()

async def delete_all_combo_child_v9(path,child_id):
    await ensure_combo_command_system_v9(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM all_combo_children_v9 WHERE id=?",(child_id,))
        await db.commit()

async def execute_combo_command_v9(update,context,command,seen=None):
    command=str(command or "").strip().lstrip("/")
    if not command:return False
    key=command.casefold(); seen=seen or set()
    if key in seen:return False
    seen.add(key)
    path=context.application.bot_data["settings"].database_path
    parent=await all_combo_command_v9(path)
    if parent and key==parent.casefold():
        sent=False
        for row in await all_combo_children_v9(path):
            if await execute_combo_command_v9(update,context,row["command"],seen): sent=True
        return sent
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        row=await (await db.execute("SELECT c.id FROM combo_commands_v9 cc JOIN combos c ON c.id=cc.combo_id WHERE cc.enabled=1 AND c.enabled=1 AND lower(cc.command)=lower(?) LIMIT 1",(command,))).fetchone()
    if not row:return False
    await send_combo(path,context.bot,update.effective_chat.id,int(row["id"]))
    return True

async def show_combo_command_v9(q,path,cid):
    r=await combo_one(path,cid)
    if not r:return await show_combo_list(q,path)
    cmd=await combo_command_v9(path,cid)
    await q.edit_message_text(f"🔗 <b>Combo Command</b>\n\nCombo: <b>{html.escape(str(r['name']))}</b>\nCurrent: <code>{html.escape(cmd or 'Not set')}</code>\n\nType command name only, without /.",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Combo",callback_data=f"admin:combo:view:{cid}")]]))

async def show_all_combo_command_v9(q,path):
    cmd=await all_combo_command_v9(path); children=await all_combo_children_v9(path)
    lines=["🎁 <b>All Combos Command</b>","",f"Main command: <code>{html.escape(cmd or 'Not set')}</code>","", "<b>Child commands:</b>"]
    lines += [f"• {html.escape(str(x.get('name') or 'Combo'))} → <code>{html.escape(str(x.get('command') or ''))}</code>" for x in children] or ["— None configured —"]
    kb=[[InlineKeyboardButton("🔗 Set All Combos Command",callback_data="admin:allcombo:v9:set")],[InlineKeyboardButton("➕ Add / Set Child Command",callback_data="admin:allcombo:v9:add")]]
    kb += [[InlineKeyboardButton(f"🗑️ Remove {str(x.get('name') or 'Combo')[:28]}",callback_data=f"admin:allcombo:v9:del:{x['id']}")] for x in children]
    kb.append([InlineKeyboardButton("⬅️ Combos",callback_data="admin:combos")])
    await q.edit_message_text("\n".join(lines),parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))

def combo_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Add Combo',callback_data='admin:combo:add')],
        [InlineKeyboardButton('📋 Saved Combos',callback_data='admin:combo:list')],
        [InlineKeyboardButton('⬅️ Admin Home',callback_data='admin:home')]
    ])


def combo_message_count_legacy(items):
    return 1 if items else 0


async def show_combos(q,path):
    rows=await combo_rows(path)
    text='🎁 <b>Combos</b>\n\n'+f'Saved Combos: <b>{len(rows)}</b>\n\n'
    text += '\n'.join(f'{"🟢" if r["enabled"] else "🔴"} <b>{html.escape(r["name"])}</b> — ₹{html.escape(str(r["price"]))}' for r in rows) if rows else 'No combos saved yet.'
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🎁 All Combos Command',callback_data='admin:allcombo:v9')],[InlineKeyboardButton('➕ Add Combo',callback_data='admin:combo:add')],[InlineKeyboardButton('📋 Saved Combos',callback_data='admin:combo:list')],[InlineKeyboardButton('⬅️ Admin Home',callback_data='admin:home')]]))


async def show_combo_list(q,path):
    rows=await combo_rows(path)
    kb=[[InlineKeyboardButton(f'{"🟢" if r["enabled"] else "🔴"} {r["name"][:40]} — ₹{r["price"]}',callback_data=f'admin:combo:view:{r["id"]}')] for r in rows]
    kb += [[InlineKeyboardButton('➕ Add Combo',callback_data='admin:combo:add')],[InlineKeyboardButton('⬅️ Combos',callback_data='admin:combos')]]
    await q.edit_message_text('📋 <b>Saved Combos</b>\n\nTap a combo:',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))


async def show_combo_editor(q,path,cid):
    r=await combo_one(path,cid)
    if not r:return await show_combo_list(q,path)
    msgs=await combo_messages(path,cid)
    legacy=await combo_items(path,cid)
    count=len(msgs) if msgs else combo_message_count_legacy(legacy)
    text=(f'🎁 <b>{html.escape(str(r["name"]))}</b>\n\n💰 Price: <b>₹{html.escape(str(r["price"]))}</b>\n'
          f'Status: <b>{"ON 🟢" if r["enabled"] else "OFF 🔴"}</b>\nMessages: <b>{count}</b>\n\n'
          f'<b>Details:</b>\n{html.escape(str(r.get("details") or "")[:1200])}')
    _pre_msg=await combo_pre_message(path,cid)
    text += f'\n\n<b>Pre Message:</b> {"✅ Set" if _pre_msg.strip() else "❌ Not set"}'
    if msgs:
        for i,m in enumerate(msgs,1):
            if m['message_type']=='text':
                preview=m.get('text','')[:250]
                text += f'\n\n<b>Message {i} — Text</b>\n{html.escape(preview)}'
            else:
                paths=json.loads(m.get('media_paths_json') or '[]')
                cap=m.get('caption') or ''
                text += f'\n\n<b>Message {i} — Photo</b> ({len(paths)} photo{"s" if len(paths)!=1 else ""})\n{html.escape(cap[:250])}'
    elif legacy:
        text += '\n\n<b>Legacy media message</b> — this existing combo remains sendable.'
    kb=[]
    for i,m in enumerate(msgs,1):
        kb.append([InlineKeyboardButton(f'✏️ Message {i}',callback_data=f'admin:combo:msgview:{m["id"]}')])
    kb += [[InlineKeyboardButton('➕ Add Message',callback_data=f'admin:combo:addmsg:{cid}')],
           [InlineKeyboardButton('✏️ Name',callback_data=f'admin:combo:edit:name:{cid}'),InlineKeyboardButton('💰 Price',callback_data=f'admin:combo:edit:price:{cid}')],
           [InlineKeyboardButton('📝 Details',callback_data=f'admin:combo:edit:details:{cid}')],
           [InlineKeyboardButton('🔗 PRE ACCESS Private Link',callback_data=f'admin:prelink:view:combo:{cid}')],
           [InlineKeyboardButton('➕ Add Pre Message',callback_data=f'admin:combo:premsg:{cid}')],
           [InlineKeyboardButton('🔗 Set Command',callback_data=f'admin:combo:command:v9:{cid}')],
           [InlineKeyboardButton('👁️ Preview / Send',callback_data=f'admin:combo:send:{cid}')],
           [InlineKeyboardButton('🔴 Disable' if r['enabled'] else '🟢 Enable',callback_data=f'admin:combo:toggle:{cid}')],
           [InlineKeyboardButton('🗑️ Delete',callback_data=f'admin:combo:delete:{cid}')],
           [InlineKeyboardButton('⬅️ Saved Combos',callback_data='admin:combo:list')]]
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))


async def show_combo_message_editor(q,path,mid):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        m=await (await db.execute('SELECT * FROM combo_messages WHERE id=?',(mid,))).fetchone()
    if not m:return await show_combo_list(q,path)
    m=dict(m); cid=m['combo_id']; order=m['message_order']
    if m['message_type']=='text':
        body=f'📝 <b>Message {order} — Text</b>\n\n{html.escape(m.get("text") or "")[:2500]}'
    else:
        paths=json.loads(m.get('media_paths_json') or '[]')
        body=f'🖼️ <b>Message {order} — Photo</b>\n\nPhotos: <b>{len(paths)}</b>\nCaption:\n{html.escape(m.get("caption") or "")[:1800]}'
    kb=[[InlineKeyboardButton('🗑️ Delete Message',callback_data=f'admin:combo:msgdelete:{mid}')],
        [InlineKeyboardButton('⬅️ Combo',callback_data=f'admin:combo:view:{cid}')]]
    await q.edit_message_text(body,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))


async def combo_move_image(path, iid, direction):
    # Legacy helper retained for old combo records.
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        r=await (await db.execute('SELECT * FROM combo_items WHERE id=?',(iid,))).fetchone()
        if not r:return
        if direction=='up':
            other=await (await db.execute('SELECT * FROM combo_items WHERE combo_id=? AND item_order < ? ORDER BY item_order DESC,id DESC LIMIT 1',(r['combo_id'],r['item_order']))).fetchone()
        else:
            other=await (await db.execute('SELECT * FROM combo_items WHERE combo_id=? AND item_order > ? ORDER BY item_order,id LIMIT 1',(r['combo_id'],r['item_order']))).fetchone()
        if not other:return
        await db.execute('UPDATE combo_items SET item_order=? WHERE id=?',(other['item_order'],iid)); await db.execute('UPDATE combo_items SET item_order=? WHERE id=?',(r['item_order'],other['id'])); await db.commit()


async def send_combo(path, bot, chat_id, combo_id):
    """
    Send a saved Combo exactly in its saved message order.

    Photo messages are sent as Telegram media groups when they contain
    multiple photos. File objects are passed directly to PTB so the
    multipart attachments are generated correctly by python-telegram-bot.
    """
    import json
    from pathlib import Path
    from telegram import InputMediaPhoto

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            "SELECT * FROM combo_messages WHERE combo_id=? ORDER BY message_order ASC, id ASC",
            (combo_id,)
        )
        msgs = await cur.fetchall()

    if not msgs:
        return False

    for m in msgs:
        m = dict(m)
        message_type = str(m.get("message_type") or "").lower()

        # TEXT MESSAGE
        if message_type == "text":
            text = str(m.get("text") or "")
            if text:
                entities = _message_entities_from_json(
                    str(m.get("text_entities_json") or "[]")
                )

                kwargs = {}
                if entities:
                    kwargs["entities"] = entities

                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    disable_web_page_preview=True,
                    **kwargs
                )
            continue

        # PHOTO MESSAGE
        raw_paths = m.get("media_paths_json") or "[]"

        try:
            paths = json.loads(raw_paths)
        except Exception:
            paths = []

        valid = [
            str(x) for x in paths
            if x and Path(str(x)).is_file()
        ]

        caption = str(m.get("caption") or "")

        try:
            entities = _message_entities_from_json(
                str(m.get("caption_entities_json") or "[]")
            )
        except Exception:
            entities = []

        if not valid:
            if caption:
                kwargs = {}
                if entities:
                    kwargs["entities"] = entities

                await bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    disable_web_page_preview=True,
                    **kwargs
                )
            continue

        # ONE PHOTO
        if len(valid) == 1:
            with open(valid[0], "rb") as fh:
                kwargs = {
                    "chat_id": chat_id,
                    "photo": fh,
                    "disable_notification": False
                }

                if caption:
                    kwargs["caption"] = caption

                if entities:
                    kwargs["caption_entities"] = entities

                await bot.send_photo(**kwargs)

            continue

        # MULTIPLE PHOTOS = ONE TELEGRAM ALBUM
        handles = []
        media = []

        try:
            for index, photo_path in enumerate(valid):
                fh = open(photo_path, "rb")
                handles.append(fh)

                kwargs = {}

                # Telegram albums support caption on the first item.
                if index == 0 and caption:
                    kwargs["caption"] = caption

                    if entities:
                        kwargs["caption_entities"] = entities

                # IMPORTANT:
                # Pass the actual file object directly.
                # PTB will create the multipart attachment correctly.
                media.append(
                    InputMediaPhoto(
                        media=fh,
                        **kwargs
                    )
                )

            await bot.send_media_group(
                chat_id=chat_id,
                media=media
            )

        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass

    return True


# PRE ACCESS V6 — shared destination registration + design + Combo pre-message
async def ensure_pre_access_v6_tables(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS pre_access_targets (
            target_type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            title TEXT NOT NULL DEFAULT '',
            verified INTEGER NOT NULL DEFAULT 0,
            can_invite INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(target_type,target_id)
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pre_access_targets_chat ON pre_access_targets(chat_id)")
        await db.execute("INSERT OR IGNORE INTO pre_access_settings(key,value) VALUES('design_text','You are getting pre access for the configured time. Kindly check all the content. After the access time you will be removed.\\n\\nTo join the group, CLICK HERE.')")
        await db.execute("INSERT OR IGNORE INTO pre_access_settings(key,value) VALUES('design_embed_text','CLICK HERE')")
        await db.execute("""CREATE TABLE IF NOT EXISTS combo_pre_messages (
            combo_id INTEGER PRIMARY KEY,
            message_text TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()

async def pre_design(path):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        rows=await (await db.execute("SELECT key,value FROM pre_access_settings WHERE key IN ('design_text','design_embed_text')")).fetchall()
    d={str(k):str(v) for k,v in rows}
    return d.get('design_text',''), d.get('design_embed_text','')

async def set_pre_design(path,text,embed_text=''):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT OR REPLACE INTO pre_access_settings(key,value) VALUES('design_text',?)",(str(text),))
        await db.execute("INSERT OR REPLACE INTO pre_access_settings(key,value) VALUES('design_embed_text',?)",(str(embed_text),))
        await db.commit()

async def combo_pre_message(path,cid):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        r=await (await db.execute("SELECT message_text FROM combo_pre_messages WHERE combo_id=?",(int(cid),))).fetchone()
    return str(r[0]) if r else ''

async def set_combo_pre_message(path,cid,text):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT OR REPLACE INTO combo_pre_messages(combo_id,message_text,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)",(int(cid),str(text)))
        await db.commit()

async def delete_combo_pre_message(path,cid):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM combo_pre_messages WHERE combo_id=?",(int(cid),)); await db.commit()

async def pre_target_one(path,target_type,target_id):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        r=await (await db.execute("SELECT * FROM pre_access_targets WHERE target_type=? AND target_id=?",(target_type,int(target_id)))).fetchone()
    return dict(r) if r else None

async def pre_target_register(path,target_type,target_id,chat_id,message_id,title,can_invite=True):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("""INSERT OR REPLACE INTO pre_access_targets
            (target_type,target_id,chat_id,message_id,title,verified,can_invite,updated_at)
            VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (target_type,int(target_id),int(chat_id),int(message_id) if message_id is not None else None,str(title or chat_id),1,int(bool(can_invite))))
        await db.commit()

async def pre_target_delete(path,target_type,target_id):
    await ensure_pre_access_v6_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM pre_access_targets WHERE target_type=? AND target_id=?",(target_type,int(target_id))); await db.commit()

async def verify_pre_target(context,link):
    chat_id,msg_id=parse_message_link(link)
    me=await context.bot.get_me()
    member=await context.bot.get_chat_member(chat_id,me.id)
    if member.status not in ('administrator','creator'):
        raise ValueError('The bot is not an admin in this group/channel.')
    can_invite=bool(getattr(member,'can_invite_users',False))
    if not can_invite:
        raise ValueError('The bot is admin, but it does not have permission to invite users.')
    chat=await context.bot.get_chat(chat_id)
    return int(chat.id),int(msg_id) if msg_id is not None else None,str(chat.title or chat.username or chat.id),can_invite

async def pre_target_name(path,target_type,target_id):
    if target_type=='combo':
        r=await combo_one(path,int(target_id)); return str(r.get('name') if r else target_id)
    if target_type=='batch':
        r=get_batch(path,int(target_id)); return str(r.get('name') if r else target_id)
    if target_type=='course':
        async with aiosqlite.connect(path) as db:
            db.row_factory=aiosqlite.Row
            r=await (await db.execute('SELECT name FROM courses WHERE id=? LIMIT 1',(int(target_id),))).fetchone()
        return str(r['name'] if r else target_id)
    return str(target_id)

async def show_pre_design(q,path):
    text,embed=await pre_design(path)
    preview=html.escape(text[:2500]) if text else 'Not configured.'
    await q.edit_message_text(
        '🎨 <b>PRE ACCESS Design</b>\n\n'
        f'<b>Message:</b>\n{preview}\n\n'
        f'<b>Embedded text:</b> <code>{html.escape(embed or "Not set")}</code>\n\n'
        'The embedded text will receive the newly generated one-person PRE ACCESS invite.',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('✏️ Set Design',callback_data='admin:pre:design:set')],
            [InlineKeyboardButton('🔗 Set Embedded Text',callback_data='admin:pre:design:embed')],
            [InlineKeyboardButton('⬅️ PRE ACCESS',callback_data='admin:preaccess')]
        ])
    )

def pre_design_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ PRE ACCESS',callback_data='admin:preaccess')]])

async def show_pre_target(q,path,target_type,target_id):
    r=await pre_target_one(path,target_type,target_id)
    name=await pre_target_name(path,target_type,target_id)
    status='✅ Registered' if r and r.get('verified') and r.get('can_invite') else '❌ Not Registered'
    title=str(r.get('title') or '') if r else ''
    txt=f'🔗 <b>PRE ACCESS Private Link</b>\n\n<b>{html.escape(name)}</b>\n\nStatus: <b>{status}</b>'
    if title: txt += f'\nDestination: <b>{html.escape(title)}</b>'
    kb=[
        [InlineKeyboardButton('➕ Register / Replace',callback_data=f'admin:prelink:register:{target_type}:{target_id}')],
    ]
    if r:
        kb.append([InlineKeyboardButton('🗑 Remove Registration',callback_data=f'admin:prelink:delete:{target_type}:{target_id}')])
    back={'batch':f'admin:batch:view:{target_id}','course':'admin:courses','combo':f'admin:combo:view:{target_id}'}[target_type]
    kb.append([InlineKeyboardButton('⬅️ Back',callback_data=back)])
    await q.edit_message_text(txt,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def show_courses(q,path):
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await db.execute('SELECT id,name,enabled FROM courses ORDER BY id')).fetchall()]
    kb=[[InlineKeyboardButton(f'{"🟢" if r.get("enabled") else "🔴"} {str(r.get("name") or "Course")[:42]}',callback_data=f'admin:course:view:{r["id"]}')] for r in rows]
    kb.append([InlineKeyboardButton('⬅️ Admin Home',callback_data='admin:home')])
    await q.edit_message_text('🎓 <b>Courses</b>\n\nSelect a saved course to configure its PRE ACCESS private link.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def show_course_detail(q,path,cid):
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        r=await (await db.execute('SELECT * FROM courses WHERE id=? LIMIT 1',(int(cid),))).fetchone()
    if not r:return await show_courses(q,path)
    pr=await pre_target_one(path,'course',cid)
    status='✅ Registered' if pr and pr.get('verified') and pr.get('can_invite') else '❌ Not Registered'
    await q.edit_message_text(f'🎓 <b>{html.escape(str(r["name"]))}</b>\n\n🔗 PRE ACCESS Private Link: <b>{status}</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton('🔗 PRE ACCESS Private Link',callback_data=f'admin:prelink:view:course:{cid}')],
        [InlineKeyboardButton('⬅️ Courses',callback_data='admin:courses')]
    ]))

async def send_pre_access_messages(settings,bot,customer_chat_id,target_type,target_id,target):
    name=str(target.get('name') or await pre_target_name(settings.database_path,target_type,target_id))
    if target_type=='combo':
        pre_msg=await combo_pre_message(settings.database_path,target_id)
        if pre_msg.strip():
            await bot.send_message(customer_chat_id,pre_msg,disable_web_page_preview=True)
    design,embed=await pre_design(settings.database_path)
    link=await create_pre_link(settings,bot,customer_chat_id,target_type,target)
    if not design.strip():
        design=f'Your PRE ACCESS link for {name}.\n\nClick Here'
        embed='Click Here'
    visible=embed.strip()
    if visible and visible in design:
        safe=html.escape(design)
        visible_safe=html.escape(visible)
        safe=safe.replace(visible_safe,f'<a href="{html.escape(link,quote=True)}">{visible_safe}</a>',1)
        await bot.send_message(customer_chat_id,safe,parse_mode='HTML',disable_web_page_preview=True)
    else:
        await bot.send_message(customer_chat_id,html.escape(design)+f'\n\n<a href="{html.escape(link,quote=True)}">Click Here</a>',parse_mode='HTML',disable_web_page_preview=True)
    return link

async def pre_duration(path):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        r=await (await db.execute("SELECT value FROM pre_access_settings WHERE key='duration_minutes'")).fetchone()
        return int(r[0]) if r and str(r[0]).isdigit() else 5

async def set_pre_duration(path,minutes):
    await ensure_combo_pre_tables(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT OR REPLACE INTO pre_access_settings(key,value) VALUES('duration_minutes',?)",(str(minutes),)); await db.commit()

def pre_keyboard(minutes):
    return InlineKeyboardMarkup([[InlineKeyboardButton(f'⏱️ Current: {minutes} min',callback_data='admin:pre:set')],[InlineKeyboardButton('1 min',callback_data='admin:pre:preset:1'),InlineKeyboardButton('2 min',callback_data='admin:pre:preset:2'),InlineKeyboardButton('3 min',callback_data='admin:pre:preset:3')],[InlineKeyboardButton('4 min',callback_data='admin:pre:preset:4'),InlineKeyboardButton('5 min',callback_data='admin:pre:preset:5'),InlineKeyboardButton('10 min',callback_data='admin:pre:preset:10')],[InlineKeyboardButton('✏️ Custom Minutes',callback_data='admin:pre:set')],[InlineKeyboardButton('🎨 Set Design',callback_data='admin:pre:design')],[InlineKeyboardButton('📋 Active Pre Access',callback_data='admin:pre:list')],[InlineKeyboardButton('⬅️ Admin Home',callback_data='admin:home')]])

async def show_preaccess(q,path):
    m=await pre_duration(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await db.execute("SELECT * FROM pre_access_sessions WHERE status IN ('issued','joined') ORDER BY id DESC LIMIT 20")).fetchall()]
    lines=[f'🔓 <b>Pre Access</b>\n\n⏱️ Duration: <b>{m} minutes</b>','']
    lines += [f'#{r["id"]} — {html.escape(r["target_name"])} — {r["status"]} — user {r["user_id"]}' for r in rows] or ['No active Pre Access sessions.']
    await q.edit_message_text('\n'.join(lines),parse_mode='HTML',reply_markup=pre_keyboard(m))

async def create_pre_link(settings,bot,customer_chat_id,target_type,target):
    chat_id=int(str(target.get('telegram_chat_id') or target.get('chat_id') or '').strip())
    if not chat_id: raise RuntimeError('Target has no registered PRE ACCESS destination.')
    # Invite validity is always 24 hours. Customer membership duration is controlled separately by PRE ACCESS Set Time.
    invite=await bot.create_chat_invite_link(chat_id=chat_id,member_limit=1,expire_date=int(time.time())+86400)
    await ensure_combo_pre_tables(settings.database_path)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute('INSERT INTO pre_access_sessions(customer_chat_id,user_id,target_chat_id,target_type,target_name,invite_link,status,issued_at) VALUES(?,?,?,?,?,?,?,?)',(customer_chat_id,0,chat_id,target_type,str(target.get('name') or ''),invite.invite_link,'issued',int(time.time())))
        await db.commit()
    return invite.invite_link

async def _exact_saved_batch(path, query):
    key=_command_key(query)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await db.execute('SELECT * FROM batches WHERE enabled=1')).fetchall()]
    for row in rows:
        if _command_key(row.get('name')) == key or _command_key(str(row.get('slash_trigger') or '').lstrip('/')) == key:
            return row
    return None

async def _exact_saved_course(path, query):
    key=_command_key(query)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await db.execute('SELECT * FROM courses WHERE enabled=1')).fetchall()]
    for row in rows:
        if _command_key(row.get('name')) == key:
            return row
    return None

async def _pre_direct(path,query):
    b=await _exact_saved_batch(path,query)
    if b:return ('batch',b)
    c=await _exact_saved_course(path,query)
    if c:return ('course',c)
    return (None,None)

async def _pre_target_record_for_execution(path,target_type,target_id):
    r=await pre_target_one(path,target_type,target_id)
    if not r or not r.get('verified') or not r.get('can_invite'):
        raise RuntimeError('PRE ACCESS private link is not registered/verified for this item.')
    return {'telegram_chat_id':str(r['chat_id']),'name':await pre_target_name(path,target_type,target_id)}

async def _pre_direct(path,query):
    b=await _exact_saved_batch(path,query)
    if b:return ('batch',b)
    c=await _exact_saved_course(path,query)
    if c:return ('course',c)
    rows=await combo_rows(path)
    cr=_match_name(query,rows)
    if cr:return ('combo',cr)
    return (None,None)

async def execute_pre_command(update,context,query,internal=False):
    settings=context.application.bot_data['settings']
    if not (internal or is_admin(settings, update.effective_user.id)): return False
    typ,target=await _pre_direct(settings.database_path,query)
    if not target:return False
    try:
        target_id=int(target['id'])
        reg=await _pre_target_record_for_execution(settings.database_path,typ,target_id)
        target=dict(target); target.update(reg)
        await send_pre_access_messages(settings,context.bot,update.effective_chat.id,typ,target_id,target)
        return True
    except Exception as e:
        await context.bot.send_message(update.effective_chat.id,f'❌ Could not create PRE ACCESS link.\n\n{html.escape(str(e))}')
        return True

def clear_admin_workflows(context, keep=None):
    """Clear pending admin input states when switching between admin pages.

    This prevents an unfinished Information/Shortcut/Batch workflow from
    accidentally consuming the next message intended for Train AI (or vice versa).
    """
    keys = [
        'pending_info_add','pending_info_edit',
        'pending_training_read_chat','pending_training_read_chat_link',
        'pending_training_range_first','pending_training_range_last','pending_training_edit',
        'sc_add','sc_edit','sc_item','sc_item_edit','sc_link',
        'pending_batch_add','batch_add','pending_batch_edit','pending_batch_search',
        'pending_batch_image_edit','pending_batch_reverify',
        'pending_intent_trigger','pending_generic_intent','pending_intent_database_import','combo_add','combo_edit','combo_images','pending_pre_duration','pending_pre_design','pending_prelink_register','pending_combo_pre_message','pending_generic_reply','pending_generic_edit',
    ]
    for key in keys:
        if key != keep:
            context.user_data.pop(key, None)


# ========================= PHASE30_COMBO_SALES_RULES =========================
async def phase30_ensure_combo_sales_rule(path):
    defaults = {
        "generic_upsc": "show_both_combos",
        "specific_combo": "show_requested_combo",
        "generic_followup": "ask_which_combo",
        "top_faculty_price": 999,
        "pro_pack_price": 1499,
        "negotiation": "no_discount_persuade",
        "updates_until": "UPSC Mains 2027",
        "demo_command": "/pre Top Faculty",
        "payment_question": "Do you use PhonePe?",
        "phonepe_command": "/phonepe",
        "other_payment_command": "/pay"
    }
    sql = (
        "CREATE TABLE IF NOT EXISTS combo_sales_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "name TEXT NOT NULL UNIQUE,"
        "enabled INTEGER NOT NULL DEFAULT 1,"
        "rule_json TEXT NOT NULL DEFAULT '{}',"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    async with aiosqlite.connect(path) as db:
        await db.execute(sql)
        await db.execute(
            "INSERT OR IGNORE INTO combo_sales_rules(name,enabled,rule_json) VALUES(?,?,?)",
            ("Combo Sales Rule", 1, json.dumps(defaults, ensure_ascii=False))
        )
        await db.commit()

async def phase30_get_combo_sales_rule(path):
    await phase30_ensure_combo_sales_rule(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id,name,enabled,rule_json FROM combo_sales_rules WHERE name=? LIMIT 1",
            ("Combo Sales Rule",)
        ) as cur:
            return await cur.fetchone()

def phase30_rules_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Combo Sales Rule", callback_data="admin:rules:combo")],
        [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin:home")]
    ])

def phase30_combo_rule_editor(enabled):
    toggle = "🔴 Disable Rule" if enabled else "🟢 Enable Rule"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data="admin:rules:combo:toggle")],
        [InlineKeyboardButton("🗑️ Delete Rule", callback_data="admin:rules:combo:delete")],
        [InlineKeyboardButton("⬅️ Rules", callback_data="admin:rules")]
    ])

async def phase30_show_rules(q, path):
    await phase30_ensure_combo_sales_rule(path)
    await q.edit_message_text(
        "⚙️ <b>Rules</b>\n\nSelect a rule:",
        parse_mode="HTML",
        reply_markup=phase30_rules_menu()
    )

async def phase30_show_combo_rule(q, path):
    row = await phase30_get_combo_sales_rule(path)
    if not row:
        await q.edit_message_text(
            "🎁 <b>Combo Sales Rule</b>\n\nRule is currently deleted.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Create Rule Again", callback_data="admin:rules:combo:create")],
                [InlineKeyboardButton("⬅️ Rules", callback_data="admin:rules")]
            ])
        )
        return
    enabled = bool(row["enabled"])
    await q.edit_message_text(
        "🎁 <b>Combo Sales Rule</b>\n\n"
        f"Status: <b>{'🟢 ENABLED' if enabled else '🔴 DISABLED'}</b>\n\n"
        "This rule controls the separate Top Faculty / Pro Pack sales behaviour.\n"
        "Disabling or deleting the rule does not delete Combo content.",
        parse_mode="HTML",
        reply_markup=phase30_combo_rule_editor(enabled)
    )
# ======================= END PHASE30_COMBO_SALES_RULES =======================
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    settings: Settings = context.application.bot_data['settings']
    if not is_admin(settings, q.from_user.id):
        await q.edit_message_text(f'Not authorized. Your Telegram ID is {q.from_user.id}.')
        return
    if q.data == 'admin:home':
        c = await counts(settings.database_path)
        await q.edit_message_text(main_text(c), parse_mode='HTML', reply_markup=admin_keyboard())
        return
    action = q.data.split(':', 1)[1]

    if action == 'ai_calls':
        await show_ai_calls(q, settings.database_path, 0)
        return
    if action.startswith('ai_calls:'):
        try: page=int(action.split(':',1)[1])
        except Exception: page=0
        await show_ai_calls(q, settings.database_path, page)
        return
    if action.startswith('ai_call:'):
        try: call_id=int(action.split(':',1)[1])
        except Exception: call_id=0
        await show_ai_call_detail(q, settings.database_path, call_id)
        return


    # PRE ACCESS V6 ADMIN UI
    if action == 'courses':
        clear_admin_workflows(context); await show_courses(q,settings.database_path); return
    if action.startswith('course:view:'):
        await show_course_detail(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action == 'pre:design':
        await show_pre_design(q,settings.database_path); return
    if action == 'pre:design:set':
        context.user_data['pending_pre_design']={'step':'text'}
        await q.edit_message_text('🎨 <b>Set PRE ACCESS Design</b>\n\nSend the exact message customers should receive.\n\nUse the exact visible text you want to turn into the PRE ACCESS link, for example: <code>CLICK HERE</code>.',parse_mode='HTML',reply_markup=pre_design_keyboard()); return
    if action == 'pre:design:embed':
        context.user_data['pending_pre_design']={'step':'embed'}
        await q.edit_message_text('🔗 <b>Set Embedded Link Text</b>\n\nSend the exact word/line from your saved PRE ACCESS design that should contain the generated PRE ACCESS invite.\n\nExample: <code>CLICK HERE</code>\nSend <code>none</code> to remove embedding.',parse_mode='HTML',reply_markup=pre_design_keyboard()); return
    if action.startswith('prelink:view:'):
        _,_,typ,tid=action.split(':',3); await show_pre_target(q,settings.database_path,typ,int(tid)); return
    if action.startswith('prelink:register:'):
        _,_,_,typ,tid=action.split(':',4)
        context.user_data['pending_prelink_register']={'type':typ,'id':int(tid)}
        await q.edit_message_text('🔗 <b>Register PRE ACCESS Private Link</b>\n\nMake the bot an Administrator in the target group/channel, then send <b>any message link</b> from that group/channel.\n\nThe bot will verify admin + invite permission and save the destination.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:prelink:view:{typ}:{tid}')]])); return
    if action.startswith('prelink:delete:'):
        _,_,_,typ,tid=action.split(':',4); await pre_target_delete(settings.database_path,typ,int(tid)); await show_pre_target(q,settings.database_path,typ,int(tid)); return
    if action.startswith('combo:premsg:'):
        cid=int(action.rsplit(':',1)[1]); current=await combo_pre_message(settings.database_path,cid)
        context.user_data['pending_combo_pre_message']=cid
        await q.edit_message_text('➕ <b>Add Pre Message</b>\n\nSend the message that must be sent immediately before the PRE ACCESS design/link message.\n\nSend <code>clear</code> to remove it.\n\nCurrent:\n'+html.escape(current[:2000] if current else 'Not set'),parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])); return

    # FINAL GENERIC INTENT CALLBACKS — handle before all legacy admin routing
    if q.data.startswith("fgi:"):
        logger.warning("FGI_CALLBACK_RECEIVED: %s", q.data)

        if q.data == "fgi:add":
            context.user_data["pending_final_generic_intent"] = True
            await q.edit_message_text(
                "➕ <b>Add Intent</b>\n\nSend the intent you want the AI to understand.\n\nExample: <code>customer asks how to make payment</code>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="admin:generic_replies")],
                    [InlineKeyboardButton("🏠 Admin", callback_data="admin:home")]
                ])
            )
            return

        if q.data.startswith("fgi:view:"):
            await final_generic_editor(
                q, settings.database_path, int(action.rsplit(":", 1)[1])
            )
            return

        if q.data.startswith("fgi:addcmd:"):
            iid = int(action.rsplit(":", 1)[1])
            context.user_data["pending_final_generic_command"] = iid
            await q.edit_message_text(
                "⚡ <b>Add Command</b>\n\n"
                "Type only the command word, without /.\n"
                "Example: <code>P</code> → saved as <code>/P</code>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data=f"fgi:view:{iid}")],
                    [InlineKeyboardButton("🏠 Admin", callback_data="admin:home")]
                ])
            )
            return

        if q.data.startswith("fgi:addreply:"):
            iid = int(action.rsplit(":", 1)[1])
            context.user_data["pending_final_generic_reply"] = iid
            await q.edit_message_text(
                "💬 <b>Add Reply</b>\n\n"
                "Send the exact line/phrase/reply to save.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data=f"fgi:view:{iid}")],
                    [InlineKeyboardButton("🏠 Admin", callback_data="admin:home")]
                ])
            )
            return


        if q.data.startswith("fgi:db:"):
            iid = int(action.rsplit(":", 1)[1])
            clear_admin_workflows(context)
            await show_intent_database(q, settings.database_path, iid)
            return

        if q.data.startswith("fgi:dbimport:"):
            iid = int(action.rsplit(":", 1)[1])
            clear_admin_workflows(context)
            context.user_data["pending_intent_database_import"] = {"iid": iid, "messages": 0, "examples": 0}
            await q.edit_message_text(
                "➕ <b>Import Intent Examples</b>\n\n"
                "Send one or more messages containing examples.\n\n"
                "Put <code>•</code> before every example. One <code>•</code> = one example. "
                "A long example can wrap over multiple Telegram lines and remains ONE example.\n\n"
                "Type <code>done</code> when finished.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"fgi:db:{iid}")]])
            )
            return

        if q.data.startswith("fgi:dbview:"):
            iid = int(action.rsplit(":", 1)[1])
            await show_intent_database_examples(q, settings.database_path, iid)
            return

        if q.data.startswith("fgi:dbdelete:"):
            iid = int(action.rsplit(":", 1)[1])
            count = await intent_db_count(settings.database_path, iid)
            await q.edit_message_text(
                f"⚠️ <b>Delete Intent Database?</b>\n\n"
                f"This will permanently delete <b>{count}</b> saved examples.\n\n"
                "The Generic Intent, its Reply and Command will NOT be deleted.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ Yes, Delete Database", callback_data=f"fgi:dbdeleteconfirm:{iid}")],
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"fgi:db:{iid}")]
                ])
            )
            return

        if q.data.startswith("fgi:dbdeleteconfirm:"):
            iid = int(action.rsplit(":", 1)[1])
            await intent_db_delete(settings.database_path, iid)
            await show_intent_database(q, settings.database_path, iid)
            return

        if q.data.startswith("fgi:delact:"):
            _, _, aid, iid = action.split(":")
            await final_generic_delete_action(
                settings.database_path, int(aid)
            )
            await final_generic_editor(
                q, settings.database_path, int(iid)
            )
            return

        if q.data.startswith("fgi:toggle:"):
            iid = int(action.rsplit(":", 1)[1])
            await final_generic_toggle(settings.database_path, iid)
            await final_generic_editor(q, settings.database_path, iid)
            return

        if q.data.startswith("fgi:del:"):
            iid = int(action.rsplit(":", 1)[1])
            await final_generic_delete_intent(settings.database_path, iid)
            await final_generic_show(q, settings.database_path)
            return

        if q.data.startswith("fgi:save:"):
            iid = int(action.rsplit(":", 1)[1])
            await final_generic_editor(q, settings.database_path, iid)
            return

        return


    # ---------------------- Phase 30 Rules routing ----------------------
    if action == 'rules':
        clear_admin_workflows(context)
        await phase30_show_rules(q, settings.database_path)
        return

    if action == 'rules:combo':
        await phase30_show_combo_rule(q, settings.database_path)
        return

    if action == 'rules:combo:toggle':
        row = await phase30_get_combo_sales_rule(settings.database_path)
        if row:
            new_enabled = 0 if int(row["enabled"]) else 1
            async with aiosqlite.connect(settings.database_path) as db:
                await db.execute(
                    "UPDATE combo_sales_rules SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (new_enabled, row["id"])
                )
                await db.commit()
        await phase30_show_combo_rule(q, settings.database_path)
        return

    if action == 'rules:combo:delete':
        await q.edit_message_text(
            "⚠️ <b>Delete Combo Sales Rule?</b>\n\n"
            "Only the rule configuration will be deleted.\n"
            "Top Faculty and Pro Pack Combo data will NOT be deleted.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ Yes, Delete Rule", callback_data="admin:rules:combo:deleteconfirm")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin:rules:combo")]
            ])
        )
        return

    if action == 'rules:combo:deleteconfirm':
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute("DELETE FROM combo_sales_rules WHERE name=?", ("Combo Sales Rule",))
            await db.commit()
        await q.edit_message_text(
            "🗑️ <b>Combo Sales Rule deleted.</b>\n\n"
            "Top Faculty and Pro Pack Combo data remains untouched.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Create Rule Again", callback_data="admin:rules:combo:create")],
                [InlineKeyboardButton("⬅️ Rules", callback_data="admin:rules")]
            ])
        )
        return

    if action == 'rules:combo:create':
        await phase30_ensure_combo_sales_rule(settings.database_path)
        await phase30_show_combo_rule(q, settings.database_path)
        return
    # -------------------- End Phase 30 Rules routing --------------------

    # Top-level admin navigation must cancel unfinished input workflows.
    # Otherwise a message intended for Read Chat can be consumed by an older
    # Information/Shortcut/Batch state, exactly like the reported Step 2/3 issue.
    if action in ('home','information','shortcuts','batches','train','ai','courses','combos','preaccess','rules'):
        clear_admin_workflows(context)

    if action=='shortcuts': await show_shortcuts(q,settings.database_path); return
    if action=='sc:list': await show_sc_list(q,settings.database_path); return
    if action=='sc:add': context.user_data['sc_add']={'step':'name'}; await q.edit_message_text('➕ <b>Add Shortcut</b>\n\nSend shortcut name.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data='admin:shortcuts')]])); return
    if action.startswith('sc:view:'): await show_sc_editor(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action.startswith('sc:rename:'): sid=int(action.rsplit(':',1)[1]); context.user_data['sc_edit']={'sid':sid,'field':'name'}; await q.edit_message_text('Send new shortcut name.'); return
    if action.startswith('sc:trigger:'): sid=int(action.rsplit(':',1)[1]); context.user_data['sc_edit']={'sid':sid,'field':'trigger'}; await q.edit_message_text('Send custom trigger, e.g. /phonepe.'); return
    if action.startswith('sc:toggle:'): sid=int(action.rsplit(':',1)[1]); r=await sc_one(settings.database_path,sid); await sc_update(settings.database_path,sid,enabled=0 if r['enabled'] else 1); await show_sc_editor(q,settings.database_path,sid); return
    if action.startswith('sc:delete:'): sid=int(action.rsplit(':',1)[1]); await q.edit_message_text('🗑️ <b>Delete Shortcut?</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ Yes, Delete',callback_data=f'admin:sc:deleteconfirm:{sid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:sc:view:{sid}')]])); return
    if action.startswith('sc:deleteconfirm:'): sid=int(action.rsplit(':',1)[1]); await sc_delete(settings.database_path,sid); await show_shortcuts(q,settings.database_path); return
    if action.startswith('sc:additem:'): sid=int(action.rsplit(':',1)[1]); context.user_data['sc_item']={'sid':sid,'step':'type'}; await q.edit_message_text('➕ <b>Add Item</b>\n\nChoose type.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 Text',callback_data=f'admin:sc:itemtype:text:{sid}')],[InlineKeyboardButton('🖼️ Image',callback_data=f'admin:sc:itemtype:image:{sid}')],[InlineKeyboardButton('✅ Done',callback_data=f'admin:sc:view:{sid}')]])); return
    if action.startswith('sc:itemtype:'): parts=action.split(':'); typ=parts[2]; sid=int(parts[3]); context.user_data['sc_item']={'sid':sid,'step':'content','type':typ}; await q.edit_message_text('📝 Send exact text.' if typ=='text' else '🖼️ Send image.'); return
    if action.startswith('sc:item:'): await show_sc_item(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action.startswith('sc:parent:'): await show_sc_editor(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action.startswith('sc:itemtoggle:'): iid=int(action.rsplit(':',1)[1]); it=await sc_item(settings.database_path,iid); await sc_item_update(settings.database_path,iid,enabled=0 if it['enabled'] else 1); await show_sc_item(q,settings.database_path,iid); return
    if action.startswith('sc:itemdelete:'): iid=int(action.rsplit(':',1)[1]); it=await sc_item(settings.database_path,iid); await sc_delitem(settings.database_path,iid); await show_sc_editor(q,settings.database_path,it['shortcut_id']); return
    if action.startswith('sc:up:') or action.startswith('sc:down:'): iid=int(action.rsplit(':',1)[1]); it=await sc_item(settings.database_path,iid); await sc_move(settings.database_path,it['shortcut_id'],iid,'up' if action.startswith('sc:up:') else 'down'); await show_sc_editor(q,settings.database_path,it['shortcut_id']); return
    if action.startswith('sc:edititem:'): iid=int(action.rsplit(':',1)[1]); it=await sc_item(settings.database_path,iid); context.user_data['sc_item_edit']={'iid':iid}; await q.edit_message_text('🖼️ Send replacement image.' if it.get('media_path') else '📝 Send replacement text.'); return
    if action.startswith('sc:link:'): iid=int(action.rsplit(':',1)[1]); context.user_data['sc_link']=iid; await q.edit_message_text('🔗 Send: Visible text | https://example.com\n\nVisible text must already exist in the saved caption/text.'); return
    if action == 'combos':
        clear_admin_workflows(context); await show_combos(q, settings.database_path); return
    # COMBO_COMMAND_CALLBACKS_V9
    if action == 'allcombo:v9':
        await show_all_combo_command_v9(q,settings.database_path); return
    if action == 'allcombo:v9:set':
        context.user_data['pending_combo_command_v9']={'type':'all'}
        await q.edit_message_text("🔗 <b>Set All Combos Command</b>\n\nType command name only, without <code>/</code>.",parse_mode='HTML'); return
    if action == 'allcombo:v9:add':
        async with aiosqlite.connect(settings.database_path) as db:
            db.row_factory=aiosqlite.Row
            rows=await (await db.execute("SELECT id,name FROM combos WHERE enabled=1 ORDER BY id")).fetchall()
        kb=[[InlineKeyboardButton(f"➕ {str(r['name'])[:38]}",callback_data=f"admin:allcombo:v9:choose:{r['id']}")] for r in rows]
        kb.append([InlineKeyboardButton("⬅️ Back",callback_data="admin:allcombo:v9")])
        await q.edit_message_text("➕ <b>Select Combo</b>\n\nChoose the saved Combo whose command All Combos should trigger.",parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb)); return
    if action.startswith('allcombo:v9:choose:'):
        cid=int(action.rsplit(':',1)[1]); context.user_data['pending_combo_command_v9']={'type':'child','combo_id':cid}
        r=await combo_one(settings.database_path,cid)
        await q.edit_message_text(f"🔗 <b>Set Child Command</b>\n\nCombo: <b>{html.escape(str(r['name'] if r else 'Combo'))}</b>\n\nType command name only, without <code>/</code>.",parse_mode='HTML'); return
    if action.startswith('allcombo:v9:del:'):
        await delete_all_combo_child_v9(settings.database_path,int(action.rsplit(':',1)[1])); await show_all_combo_command_v9(q,settings.database_path); return
    if action.startswith('combo:command:v9:'):
        cid=int(action.rsplit(':',1)[1]); context.user_data['pending_combo_command_v9']={'type':'combo','combo_id':cid}; await show_combo_command_v9(q,settings.database_path,cid); return

    if action == 'combo:list':
        await show_combo_list(q, settings.database_path); return
    if action == 'combo:add':
        clear_admin_workflows(context); context.user_data['combo_add']={'step':'name'}
        await q.edit_message_text('➕ <b>Add Combo</b>\n\nStep 1/3 — Send combo name.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data='admin:combos')]])); return
    if action.startswith('combo:view:'):
        await show_combo_editor(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action.startswith('combo:toggle:'):
        cid=int(action.rsplit(':',1)[1]); r=await combo_one(settings.database_path,cid); await combo_update(settings.database_path,cid,enabled=0 if r['enabled'] else 1); await show_combo_editor(q,settings.database_path,cid); return
    if action.startswith('combo:delete:'):
        cid=int(action.rsplit(':',1)[1]); await q.edit_message_text('🗑️ <b>Delete Combo?</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ Yes, Delete',callback_data=f'admin:combo:deleteconfirm:{cid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])); return
    if action.startswith('combo:deleteconfirm:'):
        await combo_delete(settings.database_path,int(action.rsplit(':',1)[1])); await show_combo_list(q,settings.database_path); return
    if action.startswith('combo:edit:'):
        _,_,field,cid=action.split(':'); context.user_data['combo_edit']={'cid':int(cid),'field':field}; await q.edit_message_text(f'✏️ Send new <b>{field}</b>.',parse_mode='HTML'); return
    if action.startswith('combo:addmsg:'):
        cid=int(action.rsplit(':',1)[1]); clear_admin_workflows(context); context.user_data['combo_new_message']={'cid':cid,'step':'type'}
        await q.edit_message_text('➕ <b>Add Message</b>\n\nChoose the message type. You can add 3–4 or more messages, in exact order.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 Text Message',callback_data=f'admin:combo:msgtype:text:{cid}')],[InlineKeyboardButton('🖼️ Photo Message',callback_data=f'admin:combo:msgtype:photo:{cid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])); return
    if action.startswith('combo:msgtype:'):
        parts=action.split(':'); typ=parts[2]; cid=int(parts[3]); st={'cid':cid,'step':'content','type':typ,'photos':[]}
        context.user_data['combo_new_message']=st
        if typ=='text':
            await q.edit_message_text('📝 <b>Message</b>\n\nSend the exact text for this message.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]]))
        else:
            await q.edit_message_text(
                '🖼️ <b>Photo Message</b>\n\n'
                'Send Photo 1.\n\n'
                'After each photo is saved, simply send the next photo. '
                'When you are finished, type <code>done</code>.',
                parse_mode='HTML'
            )
        return
    if action.startswith('combo:msgsave:'):
        mid=int(action.rsplit(':',1)[1]); await show_combo_message_editor(q,settings.database_path,mid); return
    if action.startswith('combo:msgview:'):
        await show_combo_message_editor(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action.startswith('combo:msgdelete:'):
        mid=int(action.rsplit(':',1)[1]);
        async with aiosqlite.connect(settings.database_path) as db:
            rr=await (await db.execute('SELECT combo_id FROM combo_messages WHERE id=?',(mid,))).fetchone()
        if rr:
            await combo_delete_message(settings.database_path,mid); await q.edit_message_text('🗑️ Message deleted.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Combo',callback_data=f'admin:combo:view:{rr[0]}')]]))
        return
    if action.startswith('combo:photo_more:'):
        cid=int(action.rsplit(':',1)[1]); st=context.user_data.get('combo_new_message')
        if not st or st.get('cid')!=cid or st.get('type')!='photo': return
        st['step']='more_photo'; context.user_data['combo_new_message']=st
        await q.edit_message_text(
            f'🖼️ <b>Photo {len(st.get("photos",[]))+1}</b>\n\nSend the next photo. It will be registered immediately.',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Done — Photos Finished',callback_data=f'admin:combo:photo_done:{cid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])
        ); return
    if action.startswith('combo:photo_done:'):
        cid=int(action.rsplit(':',1)[1]); st=context.user_data.get('combo_new_message')
        if not st or st.get('cid')!=cid:return
        if not st.get('photos'):
            await q.edit_message_text('Please send at least one photo first.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])); return
        st['step']='caption'; context.user_data['combo_new_message']=st
        await q.edit_message_text('✍️ <b>Caption</b>\n\nSend the caption now. You can use bold, italic, underline, code, quote/highlight, line breaks, emojis and Telegram formatting.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏭️ Skip Caption',callback_data=f'admin:combo:caption_skip:{cid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])); return
    if action.startswith('combo:caption_skip:'):
        cid=int(action.rsplit(':',1)[1]); st=context.user_data.get('combo_new_message')
        if not st or st.get('cid')!=cid:return
        st['caption']=''; st['caption_entities_json']='[]'; st['step']='link_text'; context.user_data['combo_new_message']=st
        await q.edit_message_text('🔗 <b>Embedded Link</b>\n\nIf you want a hyperlink, send the exact word or line from the caption. Otherwise tap Skip.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏭️ Skip',callback_data=f'admin:combo:link_skip:{cid}')]])); return
    if action.startswith('combo:link_skip:'):
        cid=int(action.rsplit(':',1)[1]); st=context.user_data.get('combo_new_message')
        if not st or st.get('cid')!=cid:return
        mid=await combo_add_message(settings.database_path,cid,'photo',media_paths=st['photos'],caption=st.get('caption',''),caption_entities_json=st.get('caption_entities_json','[]'))
        context.user_data.pop('combo_new_message',None)
        await q.edit_message_text(f'✅ <b>Message Saved</b>\n\nMessage #{mid} saved in this Combo. Add another message whenever you want.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('➕ Add Message',callback_data=f'admin:combo:addmsg:{cid}')],[InlineKeyboardButton('🎁 Open Combo',callback_data=f'admin:combo:view:{cid}')]])); return
    if action.startswith('combo:message_save_text:'):
        cid=int(action.rsplit(':',1)[1]); st=context.user_data.get('combo_new_message')
        if not st or st.get('cid')!=cid:return
        mid=await combo_add_message(settings.database_path,cid,'text',text=st.get('text',''))
        context.user_data.pop('combo_new_message',None)
        await q.edit_message_text(f'✅ <b>Message Saved</b>\n\nMessage saved in order. Add another message whenever you want.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('➕ Add Message',callback_data=f'admin:combo:addmsg:{cid}')],[InlineKeyboardButton('🎁 Open Combo',callback_data=f'admin:combo:view:{cid}')]])); return
    if action.startswith('combo:linksave:'):
        cid=int(action.rsplit(':',1)[1]); st=context.user_data.get('combo_new_message')
        if not st or st.get('cid')!=cid:return
        mid=await combo_add_message(settings.database_path,cid,'photo',media_paths=st['photos'],caption=st.get('caption',''),caption_entities_json=st.get('caption_entities_json','[]'))
        context.user_data.pop('combo_new_message',None)
        await q.edit_message_text('✅ <b>Message Saved</b>\n\nThe photo message, caption and embedded link were saved exactly. Add another message or open the Combo.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('➕ Add Message',callback_data=f'admin:combo:addmsg:{cid}')],[InlineKeyboardButton('🎁 Open Combo',callback_data=f'admin:combo:view:{cid}')]])); return
    if action.startswith('combo:images:'):
        cid=int(action.rsplit(':',1)[1]); await show_combo_editor(q,settings.database_path,cid); return
    if action.startswith('combo:send:'):
        cid=int(action.rsplit(':',1)[1]); await send_combo(settings.database_path,context.bot,q.message.chat_id,cid); return

    if action == 'preaccess':
        clear_admin_workflows(context); await show_preaccess(q,settings.database_path); return
    if action.startswith('pre:preset:'):
        m=int(action.rsplit(':',1)[1]); await set_pre_duration(settings.database_path,m); await show_preaccess(q,settings.database_path); return
    if action == 'pre:set':
        context.user_data['pending_pre_duration']=True; await q.edit_message_text('⏱️ <b>Set Pre Access Duration</b>\n\nSend minutes, e.g. <code>1</code>, <code>5</code>, or <code>10</code>.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data='admin:preaccess')]])); return
    if action == 'pre:list':
        await show_preaccess(q,settings.database_path); return

    if action == 'information':
        clear_admin_workflows(context)
        await show_information(q, settings.database_path); return
    if action == 'info:list':
        clear_admin_workflows(context)
        await show_information_list(q, settings.database_path); return
    if action == 'info:add':
        clear_admin_workflows(context)
        context.user_data['pending_info_add']={'step':'title'}
        await q.edit_message_text('➕ <b>Add Information</b>\n\nStep 1/3 — Send the page/topic title.\nExample: <code>UPSC 2027 Batch Details</code>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data='admin:information')]])); return
    if action.startswith('info:view:'):
        await show_information_editor(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action.startswith('info:toggle:'):
        iid=int(action.rsplit(':',1)[1]); r=await information_one(settings.database_path,iid)
        if r: await information_update(settings.database_path,iid,enabled=0 if r['enabled'] else 1)
        await show_information_editor(q,settings.database_path,iid); return
    if action.startswith('info:delete:'):
        iid=int(action.rsplit(':',1)[1])
        await q.edit_message_text('🗑️ <b>Delete Information?</b>\n\nThis saved information page will be removed.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ Yes, Delete',callback_data=f'admin:info:deleteconfirm:{iid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:info:view:{iid}')]])); return
    if action.startswith('info:deleteconfirm:'):
        await information_delete(settings.database_path,int(action.rsplit(':',1)[1])); await show_information_list(q,settings.database_path); return
    if action.startswith('info:edit:'):
        iid=int(action.rsplit(':',1)[1]); context.user_data['pending_info_edit']={'id':iid,'step':'title'}
        r=await information_one(settings.database_path,iid)
        await q.edit_message_text(f'✏️ <b>Edit Information #{iid}</b>\n\nSend: <code>Title | Aliases | Complete details</code>\n\nCurrent title: {html.escape(r["title"] if r else "")}',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:info:view:{iid}')]])); return
    if action == 'train':
        clear_admin_workflows(context)
        context.user_data.pop('pending_training_read_chat', None)
        context.user_data.pop('pending_training_edit', None)
        await show_train_ai(q, settings.database_path); return
    if action == 'train:toggle':
        current=await training_auto_read(settings.database_path)
        await set_training_auto_read(settings.database_path, not current)
        await show_train_ai(q, settings.database_path); return
    if action == 'train:read':
        clear_admin_workflows(context)
        context.user_data['pending_training_read_chat']=True
        context.user_data.pop('pending_training_range_first', None)
        await q.edit_message_text(
            '📖 <b>Read Chat</b>\n\nSend one Telegram message link from the customer conversation.\n\nAfter you send it, choose whether to read the complete chat or an exact message range.',
            parse_mode='HTML', reply_markup=training_link_cancel_keyboard()
        ); return
    if action == 'train:read:all':
        link=context.user_data.pop('pending_training_read_chat_link', '')
        if not link:
            await q.edit_message_text('❌ No chat link is waiting. Open 📖 Read Chat and send a message link first.', parse_mode='HTML', reply_markup=training_link_cancel_keyboard()); return
        rid=await queue_training_read_request(settings.database_path,'all',chat_link=link)
        await q.edit_message_text(
            f'📖 <b>Read All Chat</b>\n\nRequest <b>#{rid}</b> saved.\n\nThe main-account Telegram reader will process this request automatically when <code>run_train_reader.py</code> is running.',
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🧠 Train AI',callback_data='admin:train')]])
        ); return
    if action == 'train:read:range':
        context.user_data.pop('pending_training_read_chat_link', None)
        context.user_data['pending_training_range_first']=True
        await q.edit_message_text(
            '📑 <b>Read In Range</b>\n\nSend the <b>FIRST message link</b>.\n\nThis will become the exact start boundary.',
            parse_mode='HTML', reply_markup=training_link_cancel_keyboard()
        ); return
    if action == 'train:reader_status':
        await q.edit_message_text(
            '🟢 <b>Main-Account Reader</b>\n\n'
            'The reader is a separate Telethon process. Start <code>run_train_reader.py</code> on the server after configuring TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE in <code>.env</code>.\n\n'
            'It processes queued Read Chat requests and Auto Read learning events.',
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Train AI', callback_data='admin:train')]])
        ); return
    if action == 'train:list':
        await show_training_list(q, settings.database_path); return
    if action.startswith('train:view:'):
        await show_training_editor(q, settings.database_path, int(action.rsplit(':',1)[1])); return
    if action.startswith('train:delete:'):
        await training_delete(settings.database_path, int(action.rsplit(':',1)[1]))
        await show_training_list(q, settings.database_path); return
    if action.startswith('train:edit:'):
        lid=int(action.rsplit(':',1)[1])
        context.user_data['pending_training_edit']=lid
        r=await training_one(settings.database_path,lid)
        if r:
            await q.edit_message_text(
                '✏️ <b>Edit Learning</b>\n\n'
                'Send in this format:\n<code>Intent | Human reply | Learned logic | Summary</code>\n\nSummary is what you want to see as the concise explanation of this learning. You may omit it; the logic/reply will be used as fallback.\n\n'
                f'Current intent: <code>{html.escape(str(r["intent"]))}</code>',
                parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:train:view:{lid}')]])
            )
        return

    if action == 'intent_triggers':
        await show_intent_triggers(q, settings.database_path); return
    if action == 'it:add':
        context.user_data['pending_intent_trigger'] = 'intent'
        await q.edit_message_text('🎯 <b>Add Intent → Trigger</b>\n\nStep 1/2 — Send the exact configured intent name.\nExample: <code>link_expired</code>', parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='admin:intent_triggers')]])); return
    if action.startswith('it:view:'):
        await show_intent_trigger_editor(q, settings.database_path, int(action.rsplit(':',1)[1])); return
    if action.startswith('it:toggle:'):
        await toggle_intent_trigger(settings.database_path, int(action.rsplit(':',1)[1])); await show_intent_triggers(q, settings.database_path); return
    if action.startswith('it:delete:'):
        await delete_intent_trigger(settings.database_path, int(action.rsplit(':',1)[1])); await show_intent_triggers(q, settings.database_path); return

    if action == 'batches':
        await show_batches(q, settings.database_path); return
    if action == 'batch:search':
        context.user_data['pending_batch_search']=True
        await q.edit_message_text('🔍 <b>Search Batches</b>\n\nEnter batch name, keyword, or alias:',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Batches',callback_data='admin:batches')]])); return
    if action == 'batch:add':
        await start_batch_add(q,context); return
    if action.startswith('batch:view:'):
        await show_batch(q,settings.database_path,int(action.rsplit(':',1)[1])); return
    if action.startswith('batch:verify:'):
        bid=int(action.rsplit(':',1)[1])
        context.user_data['pending_batch_reverify']=bid
        await q.edit_message_text('📡 <b>Register / Verify Channel</b>\n\nPaste one message link from this batch channel/group.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:batch:view:{bid}')]])); return
    if action.startswith('batch:access:'):
        bid=int(action.rsplit(':',1)[1]); row=get_batch(settings.database_path,bid)
        if row: set_access_enabled(settings.database_path,bid,not bool(row.get('access_link_enabled')))
        await show_batch(q,settings.database_path,bid); return
    if action.startswith('batch:skip:'):
        step=action.split(':')[-1]
        state=context.user_data.get('batch_add')
        if state:
            d=state['data']
            if step in ('image','description','timing','demo','payment','fee','duration'):
                d.setdefault({'image':'image_path','description':'description','timing':'timing','demo':'demo_link','payment':'pay_link','fee':'fee','duration':'duration'}[step], '')
            nxt={'image':'description','description':'fee','fee':'duration','duration':'timing','timing':'demo','demo':'payment','payment':'access'}.get(step)
            if nxt=='access':
                state['step']='access'
                await q.edit_message_text('🔐 <b>24-hour + 1-person generation</b>\n\nChoose ON or OFF.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🟢 ON',callback_data='admin:batch:access_add:1')],[InlineKeyboardButton('🔴 OFF',callback_data='admin:batch:access_add:0')]]))
            elif nxt:
                state['step']=nxt
                prompts={'description':'📝 <b>Description</b>\n\nSend description or skip.','fee':'💰 <b>Fee</b>\n\nSend fee or skip.','duration':'⏳ <b>Duration</b>\n\nSend duration or skip.','timing':'🕐 <b>Timing</b>\n\nSend timing or skip.','demo':'🎬 <b>Demo link</b>\n\nSend demo URL or skip.','payment':'💳 <b>Payment link</b>\n\nSend payment URL or skip.'}
                await q.edit_message_text(prompts[nxt],parse_mode='HTML',reply_markup=batch_skip_keyboard(nxt))
        return
    if action.startswith('batch:access_add:'):
        state=context.user_data.get('batch_add')
        if state:
            state['data']['access_link_enabled']=1 if action.endswith(':1') else 0
            d=state['data']
            bid=insert_batch(settings.database_path,d)
            context.user_data.pop('batch_add',None)
            row=get_batch(settings.database_path,bid)
            await q.edit_message_text(f'✅ <b>Batch Added Successfully</b>\n\n{html.escape(str(row["name"]))}\n24h + 1-person generation: {"ON ✅" if row.get("access_link_enabled") else "OFF ⛔"}\nSlash trigger: <code>/{html.escape(str(row.get("slash_trigger") or ""))}</code>',parse_mode='HTML',reply_markup=batch_detail_keyboard(row))
        return
    if action.startswith('batch:edit:'):
        bid=int(action.rsplit(':',1)[1])
        await show_batch_editor(q, settings.database_path, bid); return
    if action.startswith('batch:field:'):
        parts=action.split(':')
        bid=int(parts[2]); field=parts[3]
        await prompt_batch_field(q, context, bid, field); return
    if action.startswith('batch:request:'):
        bid=int(action.rsplit(':',1)[1])
        await show_batch_request_link(q, settings.database_path, bid); return
    if action.startswith('batch:image:') and not action.startswith('batch:image_remove:'):
        bid=int(action.rsplit(':',1)[1])
        context.user_data['pending_batch_image_edit']=bid
        await show_batch_image_editor(q, settings.database_path, bid); return
    if action.startswith('batch:image_remove:'):
        bid=int(action.rsplit(':',1)[1])
        update_batch_field(settings.database_path,bid,'image_path','')
        await show_batch_editor(q, settings.database_path, bid); return
    if action.startswith('batch:delete:'):
        bid=int(action.rsplit(':',1)[1])
        row=get_batch(settings.database_path,bid)
        await q.edit_message_text(f'🗑️ <b>Delete Batch?</b>\n\n{html.escape(str(row["name"])) if row else "Batch not found"}\n\nThis cannot be undone.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚠️ Yes, Delete',callback_data=f'admin:batch:delete_confirm:{bid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:batch:edit:{bid}')]])); return
    if action.startswith('batch:delete_confirm:'):
        bid=int(action.rsplit(':',1)[1])
        delete_batch(settings.database_path,bid)
        await show_batches(q, settings.database_path); return



    if action == 'ai':
        await show_ai_menu(q, settings.database_path)
        return


    if action == 'negotiation':
        await show_negotiation(q, settings.database_path); return
    if action == 'neg:enable':
        await set_negotiation_enabled(settings.database_path, True); await show_negotiation(q, settings.database_path); return
    if action == 'neg:disable':
        await set_negotiation_enabled(settings.database_path, False); await show_negotiation(q, settings.database_path); return
    if action in ('neg:max', 'neg:min'):
        context.user_data['pending_negotiation_setting'] = 'max_discount_amount' if action == 'neg:max' else 'minimum_price'
        label = 'maximum discount amount' if action == 'neg:max' else 'minimum allowed price'
        await q.edit_message_text(f'💰 Send the {label} in rupees.\n\nExample: <code>100</code>', parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='admin:negotiation')]])); return
    if action == 'neg:accept':
        rule = await negotiation_rule(settings.database_path); await update_negotiation_rule(settings.database_path, accept_customer_offer=not bool(rule.get('accept_customer_offer', True))); await show_negotiation(q, settings.database_path); return
    if action == 'neg:counter':
        rule = await negotiation_rule(settings.database_path); await update_negotiation_rule(settings.database_path, counter_offer=not bool(rule.get('counter_offer', True))); await show_negotiation(q, settings.database_path); return

    if action in ('behaviour', 'beh'):
        await show_behaviour(q, settings.database_path); return
    if action.startswith('beh:'):
        keymap = {'language':'match_customer_language','concise':'concise_replies','friendly':'friendly_tone','next':'ask_relevant_next_step','repeat':'avoid_repetition','multi':'handle_multiple_intents','internal':'never_expose_internal_info','invent':'never_invent_business_info'}
        key = keymap.get(action.split(':',1)[1])
        if key:
            rule = await behaviour_rule(settings.database_path)
            rule[key] = not bool(rule.get(key, True))
            await save_rule(settings.database_path, 'behaviour', rule, enabled=True)
            await show_behaviour(q, settings.database_path)
            return

    if action == 'saved_actions':
        await show_saved_actions(q, settings.database_path); return
    if action.startswith('sa:'):
        key = action.split(':', 1)[1]
        await toggle_saved_action(settings.database_path, key)
        await show_saved_actions(q, settings.database_path)
        return


    if action == 'generic_replies':
        for k in ('pending_final_generic_intent','pending_final_generic_command','pending_final_generic_reply'):
            context.user_data.pop(k,None)
        await final_generic_show(q,settings.database_path); return

    if action == 'fgi:add':
        logger.warning("FGI_ADD_BRANCH_START")
        try:
            context.user_data['pending_final_generic_intent'] = True
            logger.warning("FGI_ADD_STATE_SET")
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin:generic_replies")],
                [InlineKeyboardButton("🏠 Admin", callback_data="admin:home")]
            ])
            await q.edit_message_text(
                "➕ <b>Add Intent</b>\\n\\nSend the exact intent name.",
                parse_mode="HTML",
                reply_markup=markup
            )
            logger.warning("FGI_ADD_MESSAGE_EDITED")
        except Exception:
            logger.exception("FGI_ADD_BRANCH_FAILED")
        return

    if action.startswith('fgi:view:'):
        await final_generic_editor(q,settings.database_path,int(action.rsplit(':',1)[1])); return

    if action.startswith('fgi:addcmd:'):
        iid=int(action.rsplit(':',1)[1]); context.user_data['pending_final_generic_command']=iid
        await q.edit_message_text("⚡ <b>Add Command</b>\n\nType only the command word, without /.\nExample: <code>P</code> → saved as <code>/P</code>.",parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data=f"fgi:view:{iid}")],[InlineKeyboardButton("🏠 Admin",callback_data="admin:home")]])); return

    if action.startswith('fgi:addreply:'):
        iid=int(action.rsplit(':',1)[1]); context.user_data['pending_final_generic_reply']=iid
        await q.edit_message_text("💬 <b>Add Reply</b>\n\nSend the exact line/phrase/reply to save.",parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data=f"fgi:view:{iid}")],[InlineKeyboardButton("🏠 Admin",callback_data="admin:home")]])); return

    if action.startswith('fgi:delact:'):
        _,_,aid,iid=action.split(':'); await final_generic_delete_action(settings.database_path,int(aid)); await final_generic_editor(q,settings.database_path,int(iid)); return

    if action.startswith('fgi:toggle:'):
        iid=int(action.rsplit(':',1)[1]); await final_generic_toggle(settings.database_path,iid); await final_generic_editor(q,settings.database_path,iid); return

    if action.startswith('fgi:del:'):
        iid=int(action.rsplit(':',1)[1]); await final_generic_delete_intent(settings.database_path,iid); await final_generic_show(q,settings.database_path); return

    if action.startswith('fgi:save:'):
        await final_generic_editor(q,settings.database_path,int(action.rsplit(':',1)[1])); return

    if action == 'logs':
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton('🔄 Refresh Logs', callback_data='admin:logs')],
            [InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')]
        ])
        # Telegram limits a message to 4096 characters. Split logs safely.
        chunks = []
        remaining = text
        while len(remaining) > 3500:
            cut = remaining.rfind('\n<b>#', 0, 3500)
            if cut <= 0:
                cut = 3500
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        await q.edit_message_text(chunks[0], parse_mode='HTML', reply_markup=markup)
        for chunk in chunks[1:]:
            await q.message.reply_text(chunk, parse_mode='HTML')
        return
    else:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')]])
        return




async def generic_reply_rows(path):
    """Compatibility reader for legacy generic_replies consumers.
    The new Final Generic Intent system uses final_generic_* tables.
    """
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,name,intent,reply_text,enabled,sort_order,"
                "response_type,trigger_command "
                "FROM generic_replies ORDER BY sort_order ASC,id ASC"
            ) as cur:
                return [dict(x) for x in await cur.fetchall()]
    except Exception:
        return []



async def generic_reply_for(path, intents):
    """Compatibility helper for legacy Generic Reply routing."""
    rows = await generic_reply_rows(path)
    names = {
        str(i.get("name") if isinstance(i, dict) else i).strip().casefold()
        for i in (intents or [])
    }

    for row in rows:
        if not row.get("enabled"):
            continue
        intent = str(row.get("intent") or "").strip().casefold()
        if intent in names and str(row.get("response_type") or "reply").casefold() == "reply":
            return str(row.get("reply_text") or "")

    for row in rows:
        if row.get("enabled") and str(row.get("intent") or "").strip().casefold() == "unknown":
            if str(row.get("response_type") or "reply").casefold() == "reply":
                return str(row.get("reply_text") or "")

    return DEFAULT_GENERIC_REPLIES["unknown"]


async def generic_intent_context(path):
    """Unified AI context for legacy + final Generic Intents."""
    names = []
    seen = set()

    try:
        final_rows = await final_generic_intents(path)
        for row in final_rows:
            if not row.get("enabled"):
                continue
            intent = str(row.get("intent") or "").strip()
            if intent and intent not in seen:
                seen.add(intent)
                names.append(intent)
    except Exception:
        pass

    try:
        legacy_rows = await generic_reply_rows(path)
        for row in legacy_rows:
            if not row.get("enabled"):
                continue
            intent = str(row.get("intent") or "").strip()
            if intent and intent not in seen:
                seen.add(intent)
                names.append(intent)
    except Exception:
        pass

    if not names:
        return ""

    return (
        "CUSTOM GENERIC REPLY INTENTS (admin-defined):\n"
        + "\n".join(f"- {x}" for x in names)
        + "\nIf the customer message clearly matches one of these intents, "
          "return that exact configured intent. "
          "AI only identifies the intent; the server executes the saved action."
    )

async def combo_ai_context(path):
    await ensure_combo_pre_tables(path)
    rows=await combo_rows(path)
    if not rows:return ''
    lines=['AUTHORITATIVE SAVED COMBOS (use exact saved values; do not invent):']
    for r in rows:
        if not r.get('enabled'): continue
        lines.append(f'- ID={r["id"]}; NAME={r["name"]}; PRICE={r["price"]}; DETAILS={r["details"]}; CAPTION={r["caption"]}')
    try:
        await ensure_combo_command_system_v9(path)
        allcmd=await all_combo_command_v9(path)
        lines.append("COMBO SEMANTIC INTENTS (AI identifies intent; server executes saved command):")
        lines.append("- generic_combo = generic Combo information/price/details or generic UPSC Combo/package request")
        lines.append("- top_faculty_combo = specifically Top Faculty")
        lines.append("- pro_pack_combo = specifically Pro Pack")
        if allcmd: lines.append(f"- generic_combo command = /{allcmd}")
        for rr in await all_combo_children_v9(path):
            lines.append(f"- All Combos child: {rr.get('name')} -> /{rr.get('command')}")
        async with aiosqlite.connect(path) as db:
            rr=await (await db.execute("SELECT c.name,cc.command FROM combo_commands_v9 cc JOIN combos c ON c.id=cc.combo_id WHERE cc.enabled=1 AND c.enabled=1")).fetchall()
        for row in rr:
            lines.append(f"- {str(row[0]).strip().casefold().replace(' ','_')}_combo command = /{row[1]}")
    except Exception:
        logger.exception("Combo command context unavailable")
    return '\n'.join(lines) if len(lines)>1 else ''


# ======================= AI CALL TOKEN AUDIT =======================
AI_CALL_PAGE_SIZE = 5
AI_PRICING_USD_PER_1M = {
    # model prefix: (input, cached_input, output)
    'gpt-6-astra': (10.00, 1.00, 50.00),
    'gpt-5.6-sol': (4.00, 0.40, 20.00),
    'gpt-5.6-terra': (2.00, 0.20, 12.00),
    'gpt-5.6-luna': (0.20, 0.02, 1.20),
    'gpt-5.5': (5.00, 0.50, 30.00),
    'gpt-5': (1.25, 0.125, 10.00),
}


def _ai_price_for_model(model: str):
    name = str(model or '').strip().lower()
    if name in AI_PRICING_USD_PER_1M:
        return AI_PRICING_USD_PER_1M[name]
    for key, rates in AI_PRICING_USD_PER_1M.items():
        if name.startswith(key):
            return rates
    return (None, None, None)


def _usage_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _extract_openai_usage(resp):
    usage = getattr(resp, 'usage', None)
    prompt_tokens = _usage_int(getattr(usage, 'prompt_tokens', 0) if usage else 0)
    completion_tokens = _usage_int(getattr(usage, 'completion_tokens', 0) if usage else 0)
    total_tokens = _usage_int(getattr(usage, 'total_tokens', 0) if usage else 0)
    prompt_details = getattr(usage, 'prompt_tokens_details', None) if usage else None
    completion_details = getattr(usage, 'completion_tokens_details', None) if usage else None
    cached_tokens = _usage_int(getattr(prompt_details, 'cached_tokens', 0) if prompt_details else 0)
    reasoning_tokens = _usage_int(getattr(completion_details, 'reasoning_tokens', 0) if completion_details else 0)
    return prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens, total_tokens


def _estimate_ai_cost_usd(model, input_tokens, cached_tokens, output_tokens):
    input_rate, cached_rate, output_rate = _ai_price_for_model(model)
    if input_rate is None:
        return None
    # OpenAI's prompt_tokens includes cached input. Charge the uncached remainder
    # at the normal input rate, cached_tokens at the cached-input rate, and output
    # tokens at the output rate. Reasoning tokens are already included in output.
    cached_tokens = min(max(int(cached_tokens or 0), 0), max(int(input_tokens or 0), 0))
    uncached_tokens = max(int(input_tokens or 0) - cached_tokens, 0)
    return (
        uncached_tokens / 1_000_000.0 * input_rate
        + cached_tokens / 1_000_000.0 * cached_rate
        + int(output_tokens or 0) / 1_000_000.0 * output_rate
    )


async def ensure_ai_call_audit_table(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_call_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL DEFAULT 'customer',
                telegram_user_id TEXT,
                chat_id TEXT,
                customer_message TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'success',
                error_text TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL,
                latency_ms INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_call_audit_created_at ON ai_call_audit(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_call_audit_user ON ai_call_audit(telegram_user_id)")
        for _col, _typ in [("route","TEXT"),("selected_entity_id","TEXT"),("selected_entity_name","TEXT"),("selected_intent","TEXT"),("ai_confidence","TEXT"),("ai_raw_response","TEXT"),("ai_input_context","TEXT"),("server_action","TEXT"),("server_status","TEXT"),("server_error","TEXT")]:
            try:
                await db.execute(f"ALTER TABLE ai_call_audit ADD COLUMN {_col} {_typ} NOT NULL DEFAULT ''")
            except Exception:
                pass
        await db.commit()


async def record_ai_call_audit(path, *, source, telegram_user_id, chat_id, customer_message,
                               model, status, error_text='', input_tokens=0,
                               cached_input_tokens=0, output_tokens=0, reasoning_tokens=0,
                               total_tokens=0, estimated_cost_usd=None, latency_ms=0):
    await ensure_ai_call_audit_table(path)
    uncached = max(int(input_tokens or 0) - int(cached_input_tokens or 0), 0)
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            INSERT INTO ai_call_audit
            (source, telegram_user_id, chat_id, customer_message, model, status, error_text,
             input_tokens, cached_input_tokens, uncached_input_tokens, output_tokens,
             reasoning_tokens, total_tokens, estimated_cost_usd, latency_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (str(source or 'customer'), str(telegram_user_id) if telegram_user_id is not None else None,
              str(chat_id) if chat_id is not None else None, str(customer_message or ''),
              str(model or ''), str(status or 'success'), str(error_text or ''),
              int(input_tokens or 0), int(cached_input_tokens or 0), uncached,
              int(output_tokens or 0), int(reasoning_tokens or 0), int(total_tokens or 0),
              estimated_cost_usd, int(latency_ms or 0)))
        await db.commit()
        cur=await db.execute("SELECT last_insert_rowid()")
        row=await cur.fetchone()
        return int(row[0])


async def update_ai_call_audit_decision(path, call_id, *, route='', selected_entity_id='', selected_entity_name='', selected_intent='', confidence='', raw_response='', input_context='', server_action='', server_status='', server_error=''):
    if not call_id:
        return
    await ensure_ai_call_audit_table(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE ai_call_audit SET route=?,selected_entity_id=?,selected_entity_name=?,selected_intent=?,ai_confidence=?,ai_raw_response=?,ai_input_context=?,server_action=?,server_status=?,server_error=? WHERE id=?", (str(route or ''),str(selected_entity_id or ''),str(selected_entity_name or ''),str(selected_intent or ''),str(confidence or ''),str(raw_response or '')[:8000],str(input_context or '')[:12000],str(server_action or ''),str(server_status or ''),str(server_error or '')[:2000],int(call_id)))
        await db.commit()


def ai_calls_panel_keyboard(page=0, has_next=False):
    rows = []
    if page > 0:
        rows.append([InlineKeyboardButton('⬅️ Previous', callback_data=f'admin:ai_calls:{page-1}')])
    if has_next:
        rows.append([InlineKeyboardButton('Next ➡️', callback_data=f'admin:ai_calls:{page+1}')])
    rows.append([
        InlineKeyboardButton('🔄 Refresh', callback_data=f'admin:ai_calls:{page}'),
        InlineKeyboardButton('⬅️ AI', callback_data='admin:ai')
    ])
    return InlineKeyboardMarkup(rows)


async def show_ai_calls(q, path, page=0):
    await ensure_ai_call_audit_table(path)
    page = max(int(page or 0), 0)
    offset = page * AI_CALL_PAGE_SIZE
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id,created_at,source,telegram_user_id,customer_message,model,status,input_tokens,cached_input_tokens,uncached_input_tokens,output_tokens,reasoning_tokens,total_tokens,estimated_cost_usd,latency_ms,route,selected_entity_id,selected_entity_name,selected_intent,ai_confidence,server_action,server_status "
            "FROM ai_call_audit ORDER BY id DESC LIMIT ? OFFSET ?", (AI_CALL_PAGE_SIZE + 1, offset)
        )).fetchall()
        today = await (await db.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS inp, COALESCE(SUM(cached_input_tokens),0) AS cached, "
            "COALESCE(SUM(output_tokens),0) AS outp, COALESCE(SUM(reasoning_tokens),0) AS reason, COALESCE(SUM(total_tokens),0) AS total, "
            "COALESCE(SUM(estimated_cost_usd),0) AS cost FROM ai_call_audit WHERE date(created_at)=date('now','localtime')"
        )).fetchone()
        alltime = await (await db.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS inp, COALESCE(SUM(cached_input_tokens),0) AS cached, "
            "COALESCE(SUM(output_tokens),0) AS outp, COALESCE(SUM(reasoning_tokens),0) AS reason, COALESCE(SUM(total_tokens),0) AS total, "
            "COALESCE(SUM(estimated_cost_usd),0) AS cost FROM ai_call_audit"
        )).fetchone()
    has_next = len(rows) > AI_CALL_PAGE_SIZE
    rows = rows[:AI_CALL_PAGE_SIZE]
    def summary(r):
        cost = r['cost']
        return (f"Calls: {int(r['calls'] or 0)}\n"
                f"Input: {int(r['inp'] or 0):,}  | Cached: {int(r['cached'] or 0):,}\n"
                f"Output: {int(r['outp'] or 0):,} | Reasoning: {int(r['reason'] or 0):,}\n"
                f"Total: {int(r['total'] or 0):,} | Est. cost: ${float(cost or 0):.6f}")
    parts = [
        '🧾 <b>AI CALL AUDIT</b>',
        '',
        '<b>Today</b>', summary(today),
        '',
        '<b>All time</b>', summary(alltime),
        '',
        f'<b>Calls {offset+1}–{offset+len(rows)}' + (' · page '+str(page+1) if rows else '') + '</b>'
    ]
    if not rows:
        parts.append('\nNo AI calls recorded yet.')
    else:
        for r in rows:
            msg = str(r['customer_message'] or '')
            preview = msg if len(msg) <= 180 else msg[:180] + '…'
            cost = r['estimated_cost_usd']
            cost_text = f"${float(cost):.6f}" if cost is not None else 'unknown'
            parts.append(
                f"\n<b>#{r['id']} · {html.escape(str(r['created_at'] or ''))}</b>\n"
                f"👤 <b>Message:</b> {html.escape(preview)}\n"
                f"🔹 Source: <code>{html.escape(str(r['source'] or ''))}</code> | Model: <code>{html.escape(str(r['model'] or ''))}</code>\n"
                f"📥 Input: <b>{int(r['input_tokens'] or 0):,}</b> (uncached {int(r['uncached_input_tokens'] or 0):,})\n"
                f"🗄️ Cache: <b>{int(r['cached_input_tokens'] or 0):,}</b>\n"
                f"📤 Output: <b>{int(r['output_tokens'] or 0):,}</b> | 🧠 Reasoning: <b>{int(r['reasoning_tokens'] or 0):,}</b>\n"
                f"🔢 Total: <b>{int(r['total_tokens'] or 0):,}</b> | 💵 Est.: <b>{cost_text}</b>\n"
                f"⏱️ {int(r['latency_ms'] or 0):,} ms | Status: <b>{html.escape(str(r['status'] or ''))}</b>"
                + (f"\n🧠 Route: <b>{html.escape(str(r['route'] or ''))}</b> | Intent: <b>{html.escape(str(r['selected_intent'] or ''))}</b>" if r['route'] else '')
                + (f"\n🎯 Selected: <b>{html.escape(str(r['selected_entity_name'] or ''))}</b>" if r['selected_entity_name'] else '')
                + (f"\n⚙️ Server: <b>{html.escape(str(r['server_status'] or ''))}</b>" if r['server_status'] else '')
            )
    parts.append('\n💡 Tap a call below for the <b>full exact customer message</b> and complete token/cost breakdown.')
    keyboard_rows=[]
    for r in rows:
        msg=str(r['customer_message'] or '').replace('\n',' ')
        label=f"#{r['id']} · {msg[:42]}" if msg else f"#{r['id']} · (empty)"
        keyboard_rows.append([InlineKeyboardButton(label[:60], callback_data=f"admin:ai_call:{r['id']}")])
    base = ai_calls_panel_keyboard(page, has_next)
    # merge call buttons above navigation
    keyboard = keyboard_rows + list(base.inline_keyboard)
    await q.edit_message_text('\n'.join(parts), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def show_ai_call_detail(q, path, call_id):
    await ensure_ai_call_audit_table(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM ai_call_audit WHERE id=?", (int(call_id),))).fetchone()
    if not row:
        await q.edit_message_text('🧾 AI call not found.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ AI Calls', callback_data='admin:ai_calls:0')]]))
        return
    cost = row['estimated_cost_usd']
    cost_text = f"${float(cost):.8f}" if cost is not None else 'Unknown model pricing'
    message = str(row['customer_message'] or '')
    # Telegram supports 4096 chars; keep the full message whenever possible. If it is
    # unusually long, split it into separate messages after the detail header.
    header = (
        f"🧾 <b>AI CALL #{row['id']}</b>\n\n"
        f"🕒 <b>Time:</b> {html.escape(str(row['created_at'] or ''))}\n"
        f"👤 <b>User ID:</b> <code>{html.escape(str(row['telegram_user_id'] or ''))}</code>\n"
        f"💬 <b>Source:</b> <code>{html.escape(str(row['source'] or ''))}</code>\n"
        f"🤖 <b>Model:</b> <code>{html.escape(str(row['model'] or ''))}</code>\n"
        f"📌 <b>Status:</b> <code>{html.escape(str(row['status'] or ''))}</code>\n"
        f"⏱️ <b>Latency:</b> {int(row['latency_ms'] or 0):,} ms\n\n"
        f"📥 <b>Input tokens:</b> {int(row['input_tokens'] or 0):,}\n"
        f"🗄️ <b>Cached input:</b> {int(row['cached_input_tokens'] or 0):,}\n"
        f"📥 <b>Uncached input:</b> {int(row['uncached_input_tokens'] or 0):,}\n"
        f"📤 <b>Output tokens:</b> {int(row['output_tokens'] or 0):,}\n"
        f"🧠 <b>Reasoning tokens:</b> {int(row['reasoning_tokens'] or 0):,}\n"
        f"🔢 <b>Total tokens:</b> {int(row['total_tokens'] or 0):,}\n"
        f"💵 <b>Estimated cost:</b> {cost_text}\n"
    )
    if row['error_text']:
        header += f"\n❌ <b>Error:</b> {html.escape(str(row['error_text']))}\n"
    if row['route'] or row['selected_entity_id'] or row['selected_intent'] or row['server_status']:
        header += ('\n🧠 <b>AI DECISION</b>\n'
            f"Route: <b>{html.escape(str(row['route'] or ''))}</b>\n"
            f"Selected ID: <code>{html.escape(str(row['selected_entity_id'] or ''))}</code>\n"
            f"Selected: <b>{html.escape(str(row['selected_entity_name'] or ''))}</b>\n"
            f"Intent: <b>{html.escape(str(row['selected_intent'] or ''))}</b>\n"
            f"Confidence: <b>{html.escape(str(row['ai_confidence'] or ''))}</b>\n"
            f"\n⚙️ <b>SERVER EXECUTION</b>\n"
            f"Action: <b>{html.escape(str(row['server_action'] or ''))}</b>\n"
            f"Status: <b>{html.escape(str(row['server_status'] or ''))}</b>\n")
        if row['server_error']:
            header += f"Error: {html.escape(str(row['server_error']))}\n"
        if row['ai_input_context']:
            header += f"\n📦 <b>ROUTING INPUT SENT</b>\n<code>{html.escape(str(row['ai_input_context']))}</code>\n"
        if row['ai_raw_response']:
            header += f"\n🤖 <b>RAW AI JSON</b>\n<code>{html.escape(str(row['ai_raw_response']))}</code>\n"
    header += '\n💬 <b>EXACT CUSTOMER MESSAGE</b>\n'
    full = header + html.escape(message)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ AI Calls', callback_data='admin:ai_calls:0')]])
    if len(full) <= 3900:
        await q.edit_message_text(full, parse_mode='HTML', reply_markup=markup)
        return
    # Header remains in the editable message; full message is sent as follow-up chunks.
    await q.edit_message_text(header + html.escape(message[:3600-len(header)]), parse_mode='HTML', reply_markup=markup)
    rest = message[max(0, 3600-len(header)):]
    while rest:
        chunk = rest[:3900]
        rest = rest[3900:]
        await q.message.reply_text(html.escape(chunk), parse_mode='HTML')

# ===================== END AI CALL TOKEN AUDIT =====================


async def token_saver_ai_classify(context, message, compact_context, source='customer', telegram_user_id=None, chat_id=None):
    """Ultra-minimal routing classifier. AI sees only the exact message and route-scoped candidates."""
    settings=context.application.bot_data['settings']
    client=context.application.bot_data.get('token_saver_openai')
    if client is None:
        client=AsyncOpenAI(api_key=settings.openai_api_key)
        context.application.bot_data['token_saver_openai']=client
    prompt=(
        'Route only. Do not answer. Do not invent. Choose only a supplied candidate. '
        'Return JSON only: {"intents":[{"name":"..."}],"entity":null|{"type":"batch|combo|information","id":"..."},"confidence":"high|medium|low"}.\n'
        f'{compact_context}\nCUSTOMER: {message}'
    )
    started=time.perf_counter()
    input_tokens=cached_tokens=output_tokens=reasoning_tokens=total_tokens=0
    try:
        kwargs={
            'model':settings.openai_model,
            'messages':[{'role':'system','content':prompt}],
            'max_completion_tokens':64,
            'response_format':{'type':'json_object'},
        }
        if str(settings.openai_model).strip().lower() == 'gpt-5':
            kwargs['reasoning_effort']='minimal'
        resp=await client.chat.completions.create(**kwargs)
        latency_ms=int((time.perf_counter()-started)*1000)
        input_tokens,cached_tokens,output_tokens,reasoning_tokens,total_tokens=_extract_openai_usage(resp)
        cost=_estimate_ai_cost_usd(settings.openai_model,input_tokens,cached_tokens,output_tokens)
        raw=(resp.choices[0].message.content or '').strip()
        try:
            data=json.loads(raw); parse_status='success'; parse_error=''
        except Exception as exc:
            data=None; parse_status='parse_error'; parse_error=str(exc)
        audit_id=await record_ai_call_audit(settings.database_path, source=source, telegram_user_id=telegram_user_id, chat_id=chat_id, customer_message=message, model=settings.openai_model, status=parse_status, error_text=parse_error[:2000], input_tokens=input_tokens, cached_input_tokens=cached_tokens, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens, total_tokens=total_tokens, estimated_cost_usd=cost, latency_ms=latency_ms)
        if data is None:
            await update_ai_call_audit_decision(settings.database_path,audit_id,route='unknown',confidence='low',raw_response=raw,input_context=compact_context,server_action='none',server_status='silence')
            return {'intents':[],'entity':None,'confidence':'low','language':'auto','style':'classifier','_audit_id':audit_id,'_raw_response':raw}
        intents=data.get('intents') if isinstance(data.get('intents'),list) else []
        clean=[]
        for item in intents[:2]:
            if isinstance(item,dict) and str(item.get('name') or '').strip():
                clean.append({'name':str(item['name']).strip()[:80],'details':''})
        ent=data.get('entity')
        if not isinstance(ent,dict) or not str(ent.get('type') or '').strip() or not str(ent.get('id') or '').strip():
            ent=None
        elif str(ent.get('type')).strip() not in ('batch','combo','information'):
            ent=None
        else:
            ent={'type':str(ent.get('type')).strip(),'id':str(ent.get('id')).strip()}
        conf=str(data.get('confidence') or 'low').lower()
        if conf not in ('high','medium','low'): conf='low'
        return {'intents':clean,'entity':ent,'confidence':conf,'language':'auto','style':'classifier','_audit_id':audit_id,'_raw_response':raw,'_input_context':compact_context}
    except Exception as exc:
        latency_ms=int((time.perf_counter()-started)*1000)
        audit_id=None
        try:
            audit_id=await record_ai_call_audit(settings.database_path, source=source, telegram_user_id=telegram_user_id, chat_id=chat_id, customer_message=message, model=settings.openai_model, status='api_error', error_text=str(exc)[:2000], input_tokens=input_tokens, cached_input_tokens=cached_tokens, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens, total_tokens=total_tokens, estimated_cost_usd=_estimate_ai_cost_usd(settings.openai_model,input_tokens,cached_tokens,output_tokens), latency_ms=latency_ms)
            await update_ai_call_audit_decision(settings.database_path,audit_id,route='unknown',confidence='low',input_context=compact_context,server_action='none',server_status='api_error',server_error=str(exc)[:2000])
        except Exception:
            logger.exception('Failed to record AI call audit error')
        logger.exception('Token-saver AI classifier failed')
        return {'intents':[],'entity':None,'confidence':'low','language':'auto','style':'classifier','_audit_id':audit_id,'_raw_response':'','_input_context':compact_context}


async def build_ai_inputs(settings, context):
    cfg=await ai_settings_rule(settings.database_path)
    pieces=[await business_context(settings.database_path)]
    combo_ctx=await combo_ai_context(settings.database_path)
    info_ctx=await information_context(settings.database_path)
    generic_ctx=await final_generic_intent_context(settings.database_path)
    if combo_ctx: pieces.append(combo_ctx)
    if info_ctx: pieces.append(info_ctx)
    if generic_ctx: pieces.append(generic_ctx)
    if cfg.get('knowledge_enabled',True):
        k=await ai_knowledge_context(settings.database_path)
        if k: pieces.append(k)
    if cfg.get('behaviour_enabled',True):
        pieces.append(await behaviour_context(settings.database_path))
    return "\n\n".join(x for x in pieces if x)


def customer_embedded_links(response: str):
    """Render customer-facing URLs as embedded HTML links.

    The URL itself is never shown as text.  Telegram receives an HTML
    hyperlink such as <a href="...">Click Here</a>, with web previews
    disabled by the caller.
    """
    response = response or ''
    url_re = re.compile(r'(?:https?://|tg://)[^\s<>\]\)]+')
    parts = []
    pos = 0
    found = False
    for m in url_re.finditer(response):
        found = True
        before = response[pos:m.start()]
        url = m.group(0).rstrip('.,!?;:')
        trailing = m.group(0)[len(url):]
        parts.append(html.escape(before))

        context = before.lower()[-120:]
        if any(x in context for x in ('demo', 'sample', 'preview')):
            label = '🎁 Click Here'
        elif any(x in context for x in ('payment', 'pay', 'purchase', 'join', 'enroll')):
            label = '💳 Click Here'
        elif any(x in context for x in ('access', 'course link', 'class link')):
            label = '📚 Click Here'
        elif any(x in context for x in ('faq', 'question')):
            label = '❓ Click Here'
        else:
            label = '👉 Click Here'

        # Escape the URL for safe HTML attribute embedding.
        parts.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
        if trailing:
            parts.append(html.escape(trailing))
        pos = m.end()

    if not found:
        return html.escape(response)
    parts.append(html.escape(response[pos:]))
    return ''.join(parts)



# FAST_UNIQUE_BATCH_MATCH_V1
async def fast_unique_batch_match(database_path, customer_message):
    """
    Fast path for an unambiguous batch name/alias.
    This intentionally bypasses OpenAI when one batch is clearly identified.
    """
    import json as _json
    import re as _re
    import aiosqlite as _aiosqlite

    text = (customer_message or "").lower()
    text = _re.sub(r"[_\-]+", " ", text)
    text = _re.sub(r"[^\w\s]", " ", text, flags=_re.UNICODE)

    stopwords = {
        "ka", "ke", "ki", "ko", "hai", "hain", "he", "ho",
        "me", "mein", "m", "mai", "for", "the", "a", "an",
        "is", "are", "this", "that", "sir", "mam", "maam",
        "batch", "course", "chahiye", "chahta", "chahti",
        "please", "plz", "bhi", "kya", "koi", "do", "you",
        "available", "hai", "wala", "wali", "waala", "waali"
    }

    query_tokens = {
        x for x in text.split()
        if len(x) >= 4 and x not in stopwords
    }

    if not query_tokens:
        return None

    async with _aiosqlite.connect(database_path) as db:
        db.row_factory = _aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM batches WHERE enabled=1 ORDER BY id"
        )
        rows = await cur.fetchall()

    candidates = []

    for row in rows:
        names = [str(row["name"] or "")]

        aliases_raw = row["aliases_json"] if "aliases_json" in row.keys() else None
        if aliases_raw:
            try:
                aliases = _json.loads(aliases_raw)
                if isinstance(aliases, list):
                    names.extend(str(x) for x in aliases)
                elif isinstance(aliases, dict):
                    names.extend(str(x) for x in aliases.values())
            except Exception:
                pass

        searchable = " ".join(names).lower()
        searchable = _re.sub(r"[_\-]+", " ", searchable)
        searchable = _re.sub(r"[^\w\s]", " ", searchable)

        name_tokens = set(searchable.split())
        matched = query_tokens & name_tokens

        if not matched:
            continue

        # Strong match: at least one meaningful customer token is present.
        # Only accept it if exactly one enabled batch has that token.
        candidates.append((row, matched))

    if not candidates:
        return None

    # Determine whether the matched informative token identifies exactly one batch.
    token_hits = {}
    for row, matched in candidates:
        for token in matched:
            token_hits.setdefault(token, []).append(row)

    unique_rows = {}

    for row, matched in candidates:
        unique_tokens = [
            token for token in matched
            if len(token_hits.get(token, [])) == 1
        ]
        if unique_tokens:
            unique_rows[row["id"]] = (row, unique_tokens)

    if len(unique_rows) != 1:
        return None

    row, unique_tokens = next(iter(unique_rows.values()))

    return {
        "type": "batch",
        "id": str(row["id"]),
        "name": str(row["name"]),
        "matched_tokens": unique_tokens,
    }



# LOCAL_BATCH_FOLLOWUP_V1

# GENERIC_PAYMENT_PRIORITY_V1
async def generic_payment_reply(path, message):
    """
    Payment questions must use the admin-configured Generic Reply.
    Supports common payment-related custom intent names without touching
    batch payment fields.
    """
    import re

    text = (message or "").lower().strip()
    compact = re.sub(r"[\s_\-]+", " ", text)

    payment_words = (
        "payment", "pay", "phonepe", "paytm", "upi",
        "payment kaise", "pay kaise", "kaise payment",
        "kaise pay", "payment method", "payment process",
        "paisa kaise", "paise kaise", "payment kar",
        "pay kar", "payment kaise karun", "payment kaise karu",
        "how to pay", "how can i pay", "how do i pay"
    )

    if not any(x in compact for x in payment_words):
        return None

    rows = await generic_reply_rows(path)

    # First prefer the canonical payment intent if the admin created it.
    preferred = (
        "payment_inquiry",
        "customer_payment",
        "payment",
        "payment_query",
        "how_to_pay",
        "payment_information",
        "payment_info",
    )

    for wanted in preferred:
        for row in rows:
            if row["enabled"] and str(row["intent"]).strip().lower() == wanted:
                return row["reply_text"]

    # Then support custom admin intent names containing payment-related words.
    for row in rows:
        if not row["enabled"]:
            continue
        intent_name = str(row["intent"]).strip().lower()
        if any(x in intent_name for x in (
            "payment", "pay", "phonepe", "paytm", "upi"
        )):
            return row["reply_text"]

    return None


async def local_batch_followup(database_path, entity, message):
    """Handle obvious follow-ups against the already-selected batch locally."""
    import aiosqlite
    import re
    import html as _html

    if not entity or entity.get("type") != "batch" or not entity.get("id"):
        return None

    text = (message or "").strip().lower()
    text = re.sub(r"[^\w\s₹]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)

    # Keep this deliberately narrow: these are deterministic field requests,
    # not a replacement for AI understanding.
    intent = None

    if any(x in text for x in (
        "price", "price kya", "kitna", "kitne", "fee", "fees",
        "paisa", "paise", "rate", "cost", "how much", "₹"
    )):
        intent = "price"
    elif any(x in text for x in (
        "demo", "sample", "preview", "sample lecture"
    )):
        intent = "demo"
    elif any(x in text for x in (
        "duration", "validity", "kitne din", "how long",
        "kab tak", "valid", "validity kitni"
    )):
        intent = "duration"
    elif any(x in text for x in (
        "timing", "time", "timings", "class time", "classes kab"
    )):
        intent = "timing"
    elif any(x in text for x in (
        "payment", "pay kaise", "payment kaise", "kaise pay",
        "pay kaise kar", "payment method", "phonepe", "paytm"
    )):
        intent = "payment"
    elif any(x in text for x in (
        "link", "purchase", "buy", "join", "access"
    )):
        intent = "link"

    if not intent:
        return None

    async with aiosqlite.connect(database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM batches WHERE enabled=1 AND id=? LIMIT 1",
            (entity["id"],)
        )
        row = await cur.fetchone()

    if not row:
        return None

    def val(*names):
        for name in names:
            try:
                value = row[name]
            except Exception:
                continue
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    name = val("name") or entity.get("name", "this batch")
    fee = val("fee")
    duration = val("duration")
    timing = val("timing")
    demo = val("demo_link", "demo_text")
    payment = val("payment_link", "payment_text")
    purchase = val("pay_link", "payment_link", "private_access_link")

    if intent == "price":
        if not fee:
            return {
                "intent": "price_inquiry",
                "response": f'<b>{_html.escape(name)}</b>\nPrice information is not available.'
            }
        return {
            "intent": "price_inquiry",
            "response": f'₹{_html.escape(fee)}'
        }

    if intent == "demo":
        if not demo:
            return {
                "intent": "demo_request",
                "response": f'<b>{_html.escape(name)}</b>\nDemo is not available.'
            }
        return {
            "intent": "demo_request",
            "response": demo
        }

    if intent == "duration":
        return {
            "intent": "duration_inquiry",
            "response": f'<b>{_html.escape(name)}</b>\nDuration: {_html.escape(duration or "Not available")}'
        }

    if intent == "timing":
        return {
            "intent": "timing_inquiry",
            "response": f'<b>{_html.escape(name)}</b>\nTiming: {_html.escape(timing or "Not available")}'
        }

    if intent == "payment":
        return {
            "intent": "payment_inquiry",
            "response": payment or "Payment information is not available."
        }

    if intent == "link":
        return {
            "intent": "link_request",
            "response": purchase or payment or "Purchase link is not available."
        }

    return None




# TOKEN_SMART_ROUTER_V1
# Customer-facing router: local matching first, tiny AI only for unresolved
# intent among already-filtered candidates. No large business context is sent.
OPTIONAL_ONLY_SUBJECTS = {
    'sociology','agriculture','maths','mathematics','psir','anthropology',
    'philosophy','psychology','law','chemistry','public administration',
    'commerce','geology','mechanical engineering','physics','forestry','ifos'
}
OPTIONAL_OVERLAP_SUBJECTS = {'geography','economy','history'}


def _ts_norm(s):
    s=str(s or '').casefold().replace('_',' ')
    s=re.sub(r'[^\w\s]+',' ',s,flags=re.UNICODE)
    return re.sub(r'\s+',' ',s).strip()


def _ts_tokens(s):
    return {x for x in _ts_norm(s).split() if len(x)>=2}


def _ts_is_optional(text):
    n=_ts_norm(text)
    explicit=bool(re.search(r'\boptional\b|\boptionals\b|\bopt\b|\bवैकल्पिक\b',n,re.UNICODE))
    subjects=[]
    for s in sorted(OPTIONAL_OVERLAP_SUBJECTS|OPTIONAL_ONLY_SUBJECTS,key=len,reverse=True):
        if re.search(r'(?<!\w)'+re.escape(s)+r'(?!\w)',n,re.UNICODE):
            subjects.append(s)
    if explicit:
        return True, subjects[0] if subjects else None
    for s in OPTIONAL_ONLY_SUBJECTS:
        if re.search(r'(?<!\w)'+re.escape(s)+r'(?!\w)',n,re.UNICODE):
            return True, s
    return False, (subjects[0] if subjects else None)


async def token_smart_batch_candidates(path, message, limit=8):
    import json as _json
    n=_ts_norm(message); q=_ts_tokens(message)
    if not q:return []
    is_opt, opt_subject=_ts_is_optional(message)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await db.execute('SELECT * FROM batches WHERE enabled=1 ORDER BY id')).fetchall()]
    out=[]
    for r in rows:
        name=str(r.get('name') or '')
        aliases=[]
        raw=r.get('aliases_json')
        if raw:
            try:
                v=_json.loads(raw)
                aliases += [str(x) for x in v] if isinstance(v,list) else [str(x) for x in v.values()] if isinstance(v,dict) else []
            except Exception: pass
        kw=str(r.get('search_keywords') or '')
        hay=' '.join([name,*aliases,kw])
        hn=_ts_norm(hay)
        # Optional/GS guard.
        if opt_subject:
            has_subject=bool(re.search(r'(?<!\w)'+re.escape(opt_subject)+r'(?!\w)',hn,re.UNICODE))
            if not has_subject: continue
            if opt_subject in OPTIONAL_OVERLAP_SUBJECTS:
                row_opt=bool(re.search(r'\boptional\b|\boptionals\b|\bopt\b|\bवैकल्पिक\b',hn,re.UNICODE))
                if is_opt and not row_opt: continue
                if not is_opt and row_opt: continue
            elif not is_opt:
                continue
        ht=_ts_tokens(hay); overlap=q & ht
        phrase=1 if _ts_norm(name) and _ts_norm(name) in n else 0
        if not overlap and not phrase: continue
        score=len(overlap)*2+phrase*8
        # Prefer exact name and longer meaningful overlap.
        if opt_subject: score += 2
        out.append((score,r))
    out.sort(key=lambda x:(x[0],len(str(x[1].get('name') or ''))),reverse=True)
    return [r for _,r in out[:limit]]




def token_smart_rank_batch_candidates(message, candidates, limit=2):
    """Rank already-discovered batch candidates without reading/sending business payload."""
    q=_ts_tokens(message)
    scored=[]
    for r in candidates or []:
        name=str(r.get('name') or '')
        kw=str(r.get('search_keywords') or '')
        hay=_ts_tokens(name+' '+kw)
        overlap=q & hay
        phrase=1 if _ts_norm(name) and _ts_norm(name) in _ts_norm(message) else 0
        score=len(overlap)*2 + phrase*8
        scored.append((score, len(overlap), r))
    scored.sort(key=lambda x:(x[0],x[1],len(str(x[2].get('name') or ''))), reverse=True)
    return scored[:max(int(limit),1)]


def token_smart_min_batch_candidates(scored):
    """Return only the routing metadata AI needs: ID, name, saved keywords."""
    out=[]
    for score, overlap, r in scored:
        out.append({
            'id':str(r.get('id') or ''),
            'name':str(r.get('name') or ''),
            'search_keywords':str(r.get('search_keywords') or '')[:300],
        })
    return out


def token_smart_batch_context(message, scored):
    candidates=token_smart_min_batch_candidates(scored)
    lines=['BATCH CANDIDATES:']
    for c in candidates:
        lines.append(f"ID={c['id']} | NAME={c['name']} | KEYWORDS={c['search_keywords']}")
    lines.append('INTENT: batch_search for show/send; otherwise identify the short request intent.')
    return '\n'.join(lines)


async def token_smart_generic_candidates(path, message, limit=2):
    """Find only the most relevant configured Generic Intents for an AI fallback."""
    n=_ts_norm(message); q=_ts_tokens(n)
    if not q:return []
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory=aiosqlite.Row
            rows=[dict(r) for r in await (await db.execute(
                'SELECT e.intent_id,e.example,e.normalized,i.intent,i.enabled '
                'FROM generic_intent_examples e JOIN final_generic_intents i '
                'ON i.id=e.intent_id WHERE i.enabled=1 ORDER BY e.id DESC'
            )).fetchall()]
    except Exception:
        return []
    best={}
    for ex in rows:
        en=str(ex.get('normalized') or '')
        et=_ts_tokens(en)
        overlap=len(q & et)
        ratio=difflib.SequenceMatcher(None,n,en).ratio() if en else 0
        if overlap==0: continue
        score=overlap*2.5+ratio*4
        iid=int(ex['intent_id'])
        if iid not in best or score>best[iid][0]:
            best[iid]=(score,ex)
    ranked=sorted(best.values(), key=lambda x:x[0], reverse=True)[:max(int(limit),1)]
    return [{'id':str(ex['intent_id']), 'intent':str(ex['intent']), 'example':str(ex['example'])[:180]} for score,ex in ranked]


async def token_smart_combo_candidates(path, message, limit=2):
    q=_ts_tokens(message)
    if not q:return []
    rows=[r for r in await combo_rows(path) if r.get('enabled')]
    ranked=[]
    for r in rows:
        name=str(r.get('name') or '')
        kw=_ts_tokens(name)
        overlap=len(q & kw)
        if overlap:
            ranked.append((overlap,r))
    ranked.sort(key=lambda x:x[0], reverse=True)
    out=[]
    for score,r in ranked[:max(int(limit),1)]:
        cmd=await combo_command_v9(path,int(r['id']))
        out.append({'id':str(r['id']), 'name':str(r.get('name') or ''), 'command':str(cmd or '')})
    return out


def token_smart_combo_context(candidates):
    lines=['ROUTE: COMBO_DISAMBIGUATION','TASK: choose the correct combo and identify the customer intent.']
    for c in candidates:
        lines.append(f"- ID={c['id']}; NAME={c['name']}; COMMAND={c['command']}")
    lines.append('INTENT OPTIONS: generic_combo, top_faculty_combo, pro_pack_combo, unknown')
    return '\n'.join(lines)


def token_smart_info_context(candidates):
    lines=['ROUTE: INFORMATION_DISAMBIGUATION','TASK: choose the correct saved information item.']
    for c in candidates:
        lines.append(f"- ID={c.get('id')}; TITLE={c.get('title')}; ALIASES={c.get('aliases','')}")
    lines.append('INTENT OPTIONS: informational, unknown')
    return '\n'.join(lines)

async def token_smart_combo_for_subject(path, subject):
    if not subject:return None
    s=_ts_norm(subject)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await db.execute('SELECT * FROM combos WHERE enabled=1 ORDER BY id DESC')).fetchall()]
    candidates=[]
    for r in rows:
        hay=_ts_norm(str(r.get('name') or '')+' '+str(r.get('details') or '')+' '+str(r.get('caption') or ''))
        if s in hay and ('combo' in hay or 'pack' in hay or 'optional' in hay):
            candidates.append(r)
    return candidates[0] if candidates else None


async def token_smart_generic_local(path,message):
    """Return a saved generic action only when local evidence is strong."""
    n=_ts_norm(message)
    if not n:return None
    rows=await final_generic_intents(path)
    # Intent Database V2: local example matching is the primary generic router.
    # One exact normalized example = zero AI tokens. Then use conservative local
    # fuzzy/token overlap against stored examples before allowing an LLM fallback.
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory=aiosqlite.Row
            exrows=[dict(r) for r in await (await db.execute(
                'SELECT e.intent_id,e.example,e.normalized,i.intent,i.enabled FROM generic_intent_examples e '
                'JOIN final_generic_intents i ON i.id=e.intent_id WHERE i.enabled=1 ORDER BY e.id DESC'
            )).fetchall()]
        for ex in exrows:
            if str(ex.get('normalized') or '')==n:
                acts=await final_generic_actions(path,int(ex['intent_id']))
                for a in acts:
                    if a.get('enabled'): return a
        qtok=_ts_tokens(n)
        best=None
        for ex in exrows:
            en=str(ex.get('normalized') or '')
            etok=_ts_tokens(en)
            if not etok or not qtok: continue
            overlap=len(qtok & etok)
            ratio=difflib.SequenceMatcher(None,n,en).ratio()
            score=(overlap*2.5)+ratio*4
            if overlap and (ratio>=0.78 or overlap>=2):
                if best is None or score>best[0]: best=(score,ex)
        if best and best[0]>=4.0:
            acts=await final_generic_actions(path,int(best[1]['intent_id']))
            for a in acts:
                if a.get('enabled'): return a
    except Exception:
        logger.exception('Intent Database local matching failed')
    # Very common deterministic intents: never spend an LLM call.
    families={
        'greeting': ('hi','hello','hii','hey','namaste','good morning','good evening'),
        'thanks': ('thanks','thank you','thx','dhanyawad','shukriya','ty'),
    }
    for intent,words in families.items():
        if any(re.search(r'(?<!\w)'+re.escape(w)+r'(?!\w)',n) for w in words):
            for r in rows:
                if r.get('enabled') and str(r.get('intent') or '').casefold()==intent:
                    acts=await final_generic_actions(path,int(r['id']))
                    for a in acts:
                        if a.get('enabled'): return a
    # Match configured intent text itself. This is intentionally conservative.
    q=_ts_tokens(n)
    best=None
    for r in rows:
        if not r.get('enabled'): continue
        it=str(r.get('intent') or '')
        toks=_ts_tokens(it)
        if not toks: continue
        hit=len(q&toks)
        if hit>=2 or (_ts_norm(it) and _ts_norm(it) in n):
            score=hit*3+(5 if _ts_norm(it) in n else 0)
            if best is None or score>best[0]: best=(score,r)
    if best:
        acts=await final_generic_actions(path,int(best[1]['id']))
        for a in acts:
            if a.get('enabled'): return a
    return None


async def token_smart_info_candidates(path,message,limit=5):
    n=_ts_norm(message); q=_ts_tokens(message)
    if not q:return []
    rows=[r for r in await information_rows(path,100) if r.get('enabled')]
    scored=[]
    for r in rows:
        hay=_ts_norm(str(r.get('title') or '')+' '+str(r.get('aliases') or ''))
        hit=q&_ts_tokens(hay)
        if hit:
            scored.append((len(hit),r))
    scored.sort(key=lambda x:x[0],reverse=True)
    return [r for _,r in scored[:limit]]


async def token_smart_precheck(path,message,batch_candidates=None,info_candidates=None,combo_rows=None,generic_names=None):
    """Cheap gate: return True only when there is evidence for one of the five paths."""
    n=_ts_norm(message); q=_ts_tokens(message)
    if not q:return False
    if batch_candidates or info_candidates:return True
    for r in (combo_rows or []):
        if _ts_tokens(str(r.get('name') or '')) & q:return True
    # Generic/trigger names are small server-side metadata; no AI tokens here.
    for name in (generic_names or []):
        t=_ts_tokens(name)
        if t and (q&t):return True
    # Explicitly cheap-recognize the common generic UPSC Combo request family.
    if 'upsc' in q and any(x in q for x in ('course','courses','batch','batches','combo','combos','package','packages')):
        return True
    if any(x in q for x in ('combo','combos','package','packages','कॉम्बो')):
        return True
    return False


def token_smart_shortcut_hint(message):
    n=_ts_norm(message)
    return n if n else ''


def token_smart_known_intent_context(kind, candidates=None, generic=None, combos=None, infos=None):
    lines=[f'ROUTER PATH: {kind}']
    if candidates:
        lines.append('BATCH CANDIDATES (authoritative; choose only among these):')
        for r in candidates:
            lines.append(f"- ID={r.get('id')}; NAME={r.get('name')}; KEYWORDS={r.get('search_keywords','')}")
    if combos:
        lines.append('COMBO CANDIDATES (authoritative):')
        for r in combos:
            lines.append(f"- ID={r.get('id')}; NAME={r.get('name')}; PRICE={r.get('price')}")
    if infos:
        lines.append('INFORMATION CANDIDATES (authoritative):')
        for r in infos:
            lines.append(f"- ID={r.get('id')}; TITLE={r.get('title')}; ALIASES={r.get('aliases','')}")
    if generic:
        lines.append('GENERIC INTENTS (choose exact configured intent only):')
        lines.extend(f"- {x}" for x in generic)
    lines.append('Return a clear intent/entity only when supported by these candidates. Do not invent entities.')
    return '\n'.join(lines)


# TOKEN_SMART_ROUTER_V2 helpers
_BATCH_INTENT_WORDS = {
    'batch','batches','batchh','batchwala','batchwali','batchka','batchki','batchke',
    'course','courses','chahiye','needed','need','want','available','hai','hoga','hogi',
    'milega','milega','mil','do','de','teacher','sir','mam','maam'
}
_GENERIC_STOP = _BATCH_INTENT_WORDS | {
    'the','a','an','is','are','of','for','to','in','on','me','my','mujhe','mujhko',
    'ka','ki','ke','ko','se','par','ye','yeh','wo','woh','hai','hain','please','bhaiya','bhai'
}


def _ts_subject_in_name(subject, row):
    if not subject: return False
    hay=_ts_norm(str(row.get('name') or '')+' '+str(row.get('search_keywords') or '')+' '+str(row.get('aliases_json') or ''))
    return bool(re.search(r'(?<!\\w)'+re.escape(_ts_norm(subject))+r'(?!\\w)',hay,re.UNICODE))


def _ts_specific_terms(text, subject=None):
    toks=_ts_tokens(text)
    if subject:
        toks -= _ts_tokens(subject)
    toks -= {'optional','optionals','opt','वैकल्पिक','batch','batches','course','courses','chahiye','needed','need','want','please','sir','sirs','mam','maam','ka','ki','ke','ko','hai','hain','hoga','hogi','kya','do','de','for','me','mujhe','the'}
    return toks


def _ts_is_batch_like(text):
    n=_ts_norm(text); q=_ts_tokens(text)
    if any(x in q for x in ('batch','batches','course','courses')): return True
    return any(p in n for p in ('batch hoga','batch hoga kya','batch chahiye','batch milega','batch hai','course chahiye','course hai'))


async def _ts_choice_rows(path, ids):
    if not ids: return []
    marks=','.join('?'*len(ids))
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        return [dict(r) for r in await (await db.execute(f'SELECT * FROM batches WHERE enabled=1 AND id IN ({marks}) ORDER BY id',tuple(ids))).fetchall()]


async def _ts_handle_pending_choice(update, context, path, text):
    state=context.user_data.get('pending_batch_choices')
    if not state: return False
    ids=state.get('ids',[]) if isinstance(state,dict) else state
    rows=await _ts_choice_rows(path,ids)
    if not rows:
        context.user_data.pop('pending_batch_choices',None); return False
    n=_ts_norm(text)
    selected=[]
    if n in ('all','all batches','sab','sabhi','sabhi batches','all batch'):
        selected=rows
    elif n.isdigit() and 1 <= int(n) <= len(rows):
        selected=[rows[int(n)-1]]
    else:
        # Name/teacher/subject selection is resolved ONLY inside the displayed list.
        q=_ts_tokens(n)
        scored=[]
        for r in rows:
            hay=_ts_norm(str(r.get('name') or '')+' '+str(r.get('search_keywords') or ''))
            hit=len(q & _ts_tokens(hay))
            phrase=1 if _ts_norm(str(r.get('name') or '')) in n else 0
            if hit or phrase: scored.append((hit*2+phrase*5,r))
        scored.sort(key=lambda x:x[0],reverse=True)
        if scored and (len(scored)==1 or scored[0][0]>scored[1][0]): selected=[scored[0][1]]
    if not selected:
        return False
    context.user_data.pop('pending_batch_choices',None)
    for r in selected:
        ar={'language':'auto','style':'concise','intents':[{'name':'batch_search','details':'server-side selection'}],
            'entity':{'type':'batch','id':str(r['id']),'name':str(r['name'])},'confidence':'high','response':''}
        result=await execute(path,ar,{'entity':ar['entity'],'customer_message':text})
        resp=result.get('response')
        if resp: await update.message.reply_text(customer_embedded_links(resp),parse_mode='HTML',disable_web_page_preview=True)
    return True


def _ts_local_generic_family(text):
    n=_ts_norm(text)
    if re.fullmatch(r'(hi|hii|hello|hey|namaste|good morning|good evening|good afternoon)',n): return 'greeting'
    if re.fullmatch(r'(thanks|thank you|thx|ty|shukriya|dhanyawad)',n): return 'thanks'
    if any(x in n for x in ('payment','pay kaise','payment kaise','kaise pay','phonepe','paytm','upi')): return 'payment'
    return None


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    settings = context.application.bot_data['settings']
    uid = update.effective_user.id if update.effective_user else None

    # PRE ACCESS V6 admin text workflows
    if is_admin(settings, uid) and context.user_data.get('pending_prelink_register') and update.message.text:
        st=context.user_data.pop('pending_prelink_register')
        try:
            chat_id,msg_id,title,can_invite=await verify_pre_target(context,update.message.text.strip())
            await pre_target_register(settings.database_path,st['type'],st['id'],chat_id,msg_id,title,can_invite)
            await update.message.reply_text(f'✅ <b>PRE ACCESS Private Link Registered</b>\n\nTarget: <b>{html.escape(await pre_target_name(settings.database_path,st["type"],st["id"]))}</b>\nDestination: <b>{html.escape(title)}</b>\nBot admin: YES ✅\nInvite permission: YES ✅',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔗 Open Private Link Settings',callback_data=f'admin:prelink:view:{st["type"]}:{st["id"]}')]]))
        except Exception as e:
            await update.message.reply_text(f'❌ Could not register PRE ACCESS destination.\n\n{html.escape(str(e))}',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ Try Again',callback_data=f'admin:prelink:view:{st["type"]}:{st["id"]}')]]))
        return

    pd=context.user_data.get('pending_pre_design')
    if is_admin(settings,uid) and pd and update.message.text is not None:
        raw=update.message.text
        if pd.get('step')=='text':
            context.user_data['pending_pre_design']={'step':'embed','text':raw}
            await update.message.reply_text('🔗 <b>Embedded Link Text</b>\n\nSend the exact word/line from the design that should contain the generated PRE ACCESS invite.\n\nExample: <code>CLICK HERE</code>\nSend <code>none</code> if you do not want an embedded link.',parse_mode='HTML',reply_markup=pre_design_keyboard()); return
        embed='' if raw.strip().casefold()=='none' else raw.strip()
        await set_pre_design(settings.database_path,pd.get('text',''),embed)
        context.user_data.pop('pending_pre_design',None)
        await update.message.reply_text('✅ <b>PRE ACCESS Design Saved</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🎨 Open Design',callback_data='admin:pre:design')],[InlineKeyboardButton('⬅️ PRE ACCESS',callback_data='admin:preaccess')]])); return

    if is_admin(settings,uid) and context.user_data.get('pending_combo_pre_message') and update.message.text is not None:
        cid=int(context.user_data.pop('pending_combo_pre_message'))
        raw=update.message.text
        if raw.strip().casefold()=='clear':
            await delete_combo_pre_message(settings.database_path,cid)
            msg='🗑️ Combo Pre Message removed.'
        else:
            await set_combo_pre_message(settings.database_path,cid,raw)
            msg='✅ Combo Pre Message saved.'
        await update.message.reply_text(msg,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🎁 Open Combo',callback_data=f'admin:combo:view:{cid}')]])); return

    # ADMIN_COMBO_COMMAND_INPUT_V10
    _pcv10 = context.user_data.get('pending_combo_command_v9')
    if _pcv10 and is_admin(settings, uid):
        _raw_v10 = update.message.text
        if _raw_v10 is not None:
            # A slash command is never a Combo command name.
            if _raw_v10.strip().startswith('/'):
                context.user_data.pop('pending_combo_command_v9', None)
                return
            _cmd_v10 = _raw_v10.strip().lstrip('/').strip()
            if not _cmd_v10:
                await update.message.reply_text('Please send a command name.')
                return
            try:
                _target_v10 = _pcv10.get('type')
                if _target_v10 == 'combo':
                    await save_combo_command_v9(settings.database_path, int(_pcv10['combo_id']), _cmd_v10)
                elif _target_v10 == 'all':
                    await save_all_combo_command_v9(settings.database_path, _cmd_v10)
                elif _target_v10 == 'child':
                    await save_all_combo_child_v9(settings.database_path, int(_pcv10['combo_id']), _cmd_v10)
                else:
                    raise ValueError('Invalid Combo command target')
            except Exception as _e_v10:
                context.user_data.pop('pending_combo_command_v9', None)
                await update.message.reply_text(f'❌ Could not save command: {html.escape(str(_e_v10))}', parse_mode='HTML')
                return
            context.user_data.pop('pending_combo_command_v9', None)
            await update.message.reply_text(f'✅ Command saved as <code>/{html.escape(_cmd_v10)}</code>.', parse_mode='HTML')
            return


    # REAL_COMBO_PHOTO_HANDLER_V1
    # IMPORTANT: This is inside the REAL message_handler.
    # Every incoming Combo photo is saved immediately.
    combo_state = context.user_data.get('combo_new_message')

    if combo_state and is_admin(settings, uid):
        incoming = update.message
        combo_cid = combo_state.get('cid')
        combo_type = combo_state.get('type')
        combo_step = combo_state.get('step')

        if combo_type == 'photo' and combo_step in ('content', 'more_photo'):

            # "done" finishes photo collection
            if incoming.text and incoming.text.strip().lower() == 'done':
                saved_photos = combo_state.get('photos', [])

                if not saved_photos:
                    await incoming.reply_text(
                        '❌ No photo has been saved yet. Send Photo 1 first.'
                    )
                    return

                combo_state['step'] = 'caption'
                context.user_data['combo_new_message'] = combo_state

                await incoming.reply_text(
                    f'✅ <b>Photo upload completed.</b>\n\n'
                    f'<b>{len(saved_photos)}</b> photo(s) saved successfully.\n\n'
                    '✍️ Now send the caption.',
                    parse_mode='HTML'
                )
                return

            # PHOTO RECEIVED -> SAVE IT IMMEDIATELY
            if incoming.photo:
                media_dir = Path(settings.media_root) / 'combos'
                media_dir.mkdir(parents=True, exist_ok=True)

                photo = incoming.photo[-1]
                telegram_file = await context.bot.get_file(photo.file_id)

                photo_number = len(combo_state.get('photos', [])) + 1

                filename = (
                    f'combo_{combo_cid}_'
                    f'{int(time.time()*1000)}_'
                    f'{photo_number}_'
                    f'{photo.file_unique_id}.jpg'
                )

                destination = media_dir / filename

                await telegram_file.download_to_drive(
                    custom_path=str(destination)
                )

                combo_state.setdefault('photos', []).append(str(destination))
                combo_state['step'] = 'more_photo'
                context.user_data['combo_new_message'] = combo_state

                await incoming.reply_text(
                    f'✅ <b>Photo {photo_number} saved.</b>\n\n'
                    'Send another photo if you want.\n'
                    'When finished, type <code>done</code>.',
                    parse_mode='HTML'
                )
                return

            # Still waiting for a photo
            await incoming.reply_text(
                '🖼️ Please send a photo.\n\n'
                'When finished adding photos, type <code>done</code>.',
                parse_mode='HTML'
            )
            return

    if context.user_data.get('batch_add') and is_admin(settings, uid):
        handled = await process_batch_add_message(update, context)
        if handled:
            return
    if context.user_data.get('pending_batch_reverify') and is_admin(settings, uid) and update.message.text:
        bid=context.user_data.pop('pending_batch_reverify')
        try:
            chat_id,msg_id,title,can_invite=await verify_batch_channel(context,update.message.text.strip())
            update_batch_telegram(settings.database_path,bid,chat_id,msg_id,1,1)
            await update.message.reply_text('✅ <b>Channel verified successfully.</b>\nBot admin: YES ✅\nInvite-link permission: YES ✅',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📚 Batches',callback_data='admin:batches')]]))
        except Exception as e:
            await update.message.reply_text('❌ Verification failed: '+html.escape(str(e)),parse_mode='HTML')
        return
    if context.user_data.get('pending_batch_search') and is_admin(settings, uid) and update.message.text:
        context.user_data.pop('pending_batch_search',None)
        # show results as a fresh message, because there is no callback query here
        rows=search_batches(settings.database_path,update.message.text.strip(),15)
        buttons=[[InlineKeyboardButton(f'{"✅" if r.get("enabled") else "⛔"} {r["name"]}',callback_data=f'admin:batch:view:{r["id"]}')] for r in rows]
        buttons += [[InlineKeyboardButton('🔍 Search Again',callback_data='admin:batch:search')],[InlineKeyboardButton('⬅️ Batches',callback_data='admin:batches')]]
        await update.message.reply_text(('No batch found.' if not rows else '🔍 <b>Batch Search Results</b>\n\n'+ '\n'.join(f'• {html.escape(r["name"])}' for r in rows)),parse_mode='HTML',reply_markup=InlineKeyboardMarkup(buttons)); return
    if context.user_data.get('pending_batch_image_edit') and is_admin(settings, uid) and update.message.photo:
        bid=context.user_data.pop('pending_batch_image_edit')
        media_dir=Path(settings.media_root)/'batches'
        media_dir.mkdir(parents=True,exist_ok=True)
        photo=update.message.photo[-1]
        tgfile=await context.bot.get_file(photo.file_id)
        dest=media_dir/f'batch_edit_{int(time.time())}_{photo.file_unique_id}.jpg'
        await tgfile.download_to_drive(custom_path=str(dest))
        update_batch_field(settings.database_path,bid,'image_path',str(dest))
        await update.message.reply_text('✅ Batch image updated.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✏️ Edit Batch',callback_data=f'admin:batch:edit:{bid}')]]))
        return

    if is_admin(settings, uid) and context.user_data.get('pending_info_add') and update.message.text:
        state=context.user_data['pending_info_add']; raw=update.message.text.strip()
        if state['step']=='title':
            state.update(step='aliases',title=raw)
            await update.message.reply_text('Step 2/3 — Send aliases separated by commas, or <code>skip</code>.',parse_mode='HTML'); return
        if state['step']=='aliases':
            state.update(step='details',aliases='' if raw.lower()=='skip' else raw)
            await update.message.reply_text('Step 3/3 — Send the COMPLETE saved details. You can include price, year, duration, rules, links, notes, etc. Do not omit anything you want the AI to know.'); return
        iid=await information_add(settings.database_path,state['title'],state['aliases'],raw)
        context.user_data.pop('pending_info_add',None)
        await update.message.reply_text(f'✅ <b>Information saved.</b>\n\nID: <b>#{iid}</b>\nTitle: <b>{html.escape(state["title"])}</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('ℹ️ View Information',callback_data=f'admin:info:view:{iid}')],[InlineKeyboardButton('ℹ️ Information System',callback_data='admin:information')]])); return

    if is_admin(settings, uid) and context.user_data.get('pending_info_edit') and update.message.text:
        state=context.user_data.pop('pending_info_edit'); raw=update.message.text.strip(); parts=[x.strip() for x in raw.split('|',2)]
        if len(parts)!=3 or not parts[0] or not parts[2]:
            context.user_data['pending_info_edit']=state
            await update.message.reply_text('Use: <code>Title | Aliases | Complete details</code>',parse_mode='HTML'); return
        await information_update(settings.database_path,state['id'],title=parts[0],aliases=parts[1],details=parts[2])
        await update.message.reply_text('✅ <b>Information updated.</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('ℹ️ View Information',callback_data=f'admin:info:view:{state["id"]}')]])); return

    if is_admin(settings, uid) and context.user_data.get('pending_training_read_chat') and update.message.text:
        link=update.message.text.strip()
        context.user_data.pop('pending_training_read_chat',None)
        context.user_data['pending_training_read_chat_link']=link
        await update.message.reply_text(
            '📖 <b>Chat link received</b>\n\n'
            f'<code>{html.escape(link)}</code>\n\n'
            'Choose exactly what to read:',
            parse_mode='HTML', reply_markup=training_read_choice_keyboard()
        )
        return

    if is_admin(settings, uid) and context.user_data.get('pending_training_range_first') and update.message.text:
        first=update.message.text.strip()
        context.user_data.pop('pending_training_range_first',None)
        context.user_data['pending_training_range_last']=first
        await update.message.reply_text(
            '📑 <b>Read In Range</b>\n\n'
            f'FIRST message: <code>{html.escape(first)}</code>\n\n'
            'Now send the <b>LAST message link</b>.\n\nThe reader will process the FIRST message, every message between it, and the LAST message.',
            parse_mode='HTML', reply_markup=training_link_cancel_keyboard()
        )
        return

    if is_admin(settings, uid) and context.user_data.get('pending_training_range_last') and update.message.text:
        first=context.user_data.pop('pending_training_range_last')
        last=update.message.text.strip()
        rid=await queue_training_read_request(settings.database_path,'range',first_link=first,last_link=last)
        await update.message.reply_text(
            f'📑 <b>Range received</b>\n\nRequest <b>#{rid}</b> saved.\n\n'
            f'<b>FIRST:</b> <code>{html.escape(first)}</code>\n'
            f'<b>LAST:</b> <code>{html.escape(last)}</code>\n\n'
            'The main-account Telegram reader will process the exact range: FIRST → all messages in between → LAST when <code>run_train_reader.py</code> is running.',
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🧠 Train AI',callback_data='admin:train')]])
        )
        return

    if is_admin(settings, uid) and context.user_data.get('pending_training_edit') and update.message.text:
        lid=context.user_data.pop('pending_training_edit')
        raw=update.message.text.strip()
        parts=[x.strip() for x in raw.split('|',3)]
        if len(parts) not in (3,4) or not all(parts[:3]):
            context.user_data['pending_training_edit']=lid
            await update.message.reply_text('Use: <code>Intent | Human reply | Learned logic | Summary</code>',parse_mode='HTML'); return
        summary=parts[3] if len(parts)==4 and parts[3] else None
        await training_update(settings.database_path,lid,parts[0],parts[1],parts[2],learning_summary=summary)
        await update.message.reply_text('✅ <b>Learning updated.</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📚 Recent Learnings',callback_data='admin:train:list')]]))
        return

    if is_admin(settings, uid):
        a=context.user_data.get('sc_add')
        if a and update.message.text:
            raw=update.message.text.strip()
            if a['step']=='name': context.user_data['sc_add']={'step':'trigger','name':raw}; await update.message.reply_text('⌨️ Send custom slash trigger, e.g. /phonepe (or <code>skip</code>).',parse_mode='HTML'); return
            trigger=('/'+re.sub(r'[^a-zA-Z0-9_]+','_',a['name']).strip('_').lower()) if raw.lower()=='skip' else raw
            if not trigger.startswith('/') or ' ' in trigger: await update.message.reply_text('Invalid trigger.'); return
            try: sid=await sc_add(settings.database_path,a['name'],trigger)
            except Exception as e: await update.message.reply_text('❌ '+html.escape(str(e))); return
            context.user_data.pop('sc_add',None); context.user_data['sc_item']={'sid':sid,'step':'type'}; await update.message.reply_text('✅ Shortcut created. Add items:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 Text',callback_data=f'admin:sc:itemtype:text:{sid}')],[InlineKeyboardButton('🖼️ Image',callback_data=f'admin:sc:itemtype:image:{sid}')],[InlineKeyboardButton('✅ Done',callback_data=f'admin:sc:view:{sid}')]])); return
        e=context.user_data.get('sc_edit')
        if e and update.message.text:
            raw=update.message.text.strip(); sid=e['sid']
            if e['field']=='trigger' and (not raw.startswith('/') or ' ' in raw): await update.message.reply_text('Invalid slash trigger.'); return
            await sc_update(settings.database_path,sid,**{e['field']:raw}); context.user_data.pop('sc_edit',None); await update.message.reply_text('✅ Shortcut updated.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚡ Shortcut',callback_data=f'admin:sc:view:{sid}')]])); return
        x=context.user_data.get('sc_item')
        if x and x.get('step')=='content':
            if x['type']=='text' and update.message.text: await sc_additem(settings.database_path,x['sid'],text=update.message.text); context.user_data['sc_item']={'sid':x['sid'],'step':'type'}; await update.message.reply_text('✅ Text added. Add another item:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 Text',callback_data=f'admin:sc:itemtype:text:{x["sid"]}')],[InlineKeyboardButton('🖼️ Image',callback_data=f'admin:sc:itemtype:image:{x["sid"]}')],[InlineKeyboardButton('✅ Done',callback_data=f'admin:sc:view:{x["sid"]}')]])); return
            if x['type']=='image' and update.message.photo:
                d=Path(settings.media_root)/'shortcuts'; d.mkdir(parents=True,exist_ok=True); ph=update.message.photo[-1]; f=await context.bot.get_file(ph.file_id); dest=d/f'sc_{x["sid"]}_{int(time.time())}_{ph.file_unique_id}.jpg'; await f.download_to_drive(custom_path=str(dest)); context.user_data['sc_item']={'sid':x['sid'],'step':'caption','media':str(dest)}; await update.message.reply_text('📝 Send caption or <code>skip</code>.',parse_mode='HTML'); return
        if x and x.get('step')=='caption' and update.message.text:
            cap='' if update.message.text.strip().lower()=='skip' else update.message.text; await sc_additem(settings.database_path,x['sid'],media=x['media'],caption=cap); context.user_data['sc_item']={'sid':x['sid'],'step':'type'}; await update.message.reply_text('✅ Image added. Add another item:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 Text',callback_data=f'admin:sc:itemtype:text:{x["sid"]}')],[InlineKeyboardButton('🖼️ Image',callback_data=f'admin:sc:itemtype:image:{x["sid"]}')],[InlineKeyboardButton('✅ Done',callback_data=f'admin:sc:view:{x["sid"]}')]])); return
        e=context.user_data.get('sc_item_edit')
        if e:
            it=await sc_item(settings.database_path,e['iid'])
            if it.get('media_path') and update.message.photo:
                d=Path(settings.media_root)/'shortcuts'; d.mkdir(parents=True,exist_ok=True); ph=update.message.photo[-1]; f=await context.bot.get_file(ph.file_id); dest=d/f'sc_edit_{e["iid"]}_{int(time.time())}.jpg'; await f.download_to_drive(custom_path=str(dest)); await sc_item_update(settings.database_path,e['iid'],media_path=str(dest)); context.user_data.pop('sc_item_edit',None); await update.message.reply_text('✅ Image updated.'); return
            if update.message.text: await sc_item_update(settings.database_path,e['iid'],text_content=update.message.text if it.get('text_content') else '',caption=update.message.text if it.get('media_path') else ''); context.user_data.pop('sc_item_edit',None); await update.message.reply_text('✅ Item updated.'); return
        l=context.user_data.get('sc_link')
        if l and update.message.text:
            parts=[z.strip() for z in update.message.text.split('|',1)]; it=await sc_item(settings.database_path,int(l)); content=it.get('caption') or it.get('text_content') or ''
            if len(parts)!=2 or parts[0] not in content: await update.message.reply_text('Use: Visible text | URL, and visible text must exist.'); return
            await sc_item_update(settings.database_path,int(l),button_data_json=json.dumps({'text':parts[0],'url':parts[1]},ensure_ascii=False)); context.user_data.pop('sc_link',None); await update.message.reply_text('✅ Embedded link saved.'); return

    pending_edit=context.user_data.get('pending_batch_edit')
    if pending_edit and is_admin(settings, uid) and update.message.text:
        bid=int(pending_edit['id']); field=str(pending_edit['field']); raw=update.message.text.strip()
        value='' if raw.lower()=='skip' else raw
        try:
            update_batch_field(settings.database_path,bid,field,value)
            if field=='name' and value:
                update_batch_field(settings.database_path,bid,'slash_trigger',slug_trigger(value))
            context.user_data.pop('pending_batch_edit',None)
            await update.message.reply_text('✅ Batch field updated.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✏️ Edit Batch',callback_data=f'admin:batch:edit:{bid}')]]))
        except Exception as e:
            await update.message.reply_text('❌ Could not update field: '+html.escape(str(e)),parse_mode='HTML')
        return
    if not update.message.text:
        return
    if update.message.text.startswith('/') and is_admin(settings, uid):
        if await execute_direct_command(update, context, update.message.text, internal=False):
            return

    pending_it = context.user_data.get('pending_intent_trigger')
    if pending_it and is_admin(settings, uid):
        raw = update.message.text.strip()
        if pending_it == 'intent':
            if not raw or len(raw) > 100 or '\n' in raw or '|' in raw:
                await update.message.reply_text('Please send a valid intent name only.'); return
            context.user_data['pending_intent_trigger'] = {'intent': raw}
            await update.message.reply_text('✅ Intent saved: <code>'+html.escape(raw)+'</code>\n\nStep 2/2 — Send the slash trigger, e.g. <code>/expire</code>', parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='admin:intent_triggers')]])); return
        data=pending_it
        trigger=raw if raw.startswith('/') else '/'+raw
        if ' ' in trigger or len(trigger)>100:
            await update.message.reply_text('Trigger must be one slash command without spaces, e.g. <code>/expire</code>.', parse_mode='HTML'); return
        try:
            await add_intent_trigger(settings.database_path, data['intent'], trigger)
        except Exception as e:
            await update.message.reply_text('❌ Could not save mapping: '+html.escape(str(e)), parse_mode='HTML'); return
        context.user_data.pop('pending_intent_trigger', None)
        await update.message.reply_text(f'✅ <b>Intent → Trigger saved</b>\n\n<code>{html.escape(data["intent"])}</code> → <code>{html.escape(trigger)}</code>', parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🎯 Intent → Trigger', callback_data='admin:intent_triggers')]])); return

    pending_generic_action_edit = context.user_data.get('pending_generic_action_edit')
    if pending_generic_action_edit and is_admin(settings, uid):
        value=(update.message.text or '').strip()
        row_id=int(pending_generic_action_edit['id'])
        if pending_generic_action_edit['type']=='trigger':
            if not await generic_trigger_allowed_final2(settings.database_path,value):
                await update.message.reply_text('❌ That command is not an allowed configured bot command.',parse_mode='HTML'); return
            await update_generic_reply(settings.database_path,row_id,response_type='trigger',trigger_command=value,reply_text='')
            msg='⚡ <b>Trigger Command Updated</b>'
        else:
            await update_generic_reply(settings.database_path,row_id,response_type='reply',reply_text=value,trigger_command='')
            msg='💬 <b>Saved Reply Updated</b>'
        context.user_data.pop('pending_generic_action_edit',None)
        await update.message.reply_text(msg,parse_mode='HTML')
        return


    pending_final_intent=context.user_data.get('pending_final_generic_intent')
    if pending_final_intent and is_admin(settings,uid):
        value=(update.message.text or '').strip()
        if not value: await update.message.reply_text("Please send a valid intent name."); return
        iid,created=await final_generic_add_intent(settings.database_path,value)
        context.user_data.pop('pending_final_generic_intent',None)
        if created:
            msg = "✅ <b>Intent saved.</b>"
        else:
            msg = "ℹ️ <b>This intent already exists.</b>\n\nOpening the existing intent so you can add or edit its actions."
        await update.message.reply_text(
            msg,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Add Command",callback_data=f"fgi:addcmd:{iid}")],
                [InlineKeyboardButton("💬 Add Reply",callback_data=f"fgi:addreply:{iid}")],
                [InlineKeyboardButton("💾 Save",callback_data=f"fgi:save:{iid}")],
                [InlineKeyboardButton("🔙 Back",callback_data="admin:generic_replies")],
                [InlineKeyboardButton("🏠 Admin",callback_data="admin:home")]
            ])
        )
        return

    pending_final_cmd=context.user_data.get('pending_final_generic_command')
    if pending_final_cmd and is_admin(settings,uid):
        value=(update.message.text or '').strip()
        if not value: await update.message.reply_text("Type only the command word, e.g. P."); return
        iid=int(pending_final_cmd); command="/"+value.lstrip("/")
        await final_generic_add_action(settings.database_path,iid,"command",command)
        context.user_data.pop('pending_final_generic_command',None)
        await update.message.reply_text(f"✅ Command saved as <code>{html.escape(command)}</code>.",parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Add Command",callback_data=f"fgi:addcmd:{iid}")],[InlineKeyboardButton("💬 Add Reply",callback_data=f"fgi:addreply:{iid}")],[InlineKeyboardButton("💾 Save",callback_data=f"fgi:save:{iid}")],[InlineKeyboardButton("🔙 Back",callback_data="admin:generic_replies")],[InlineKeyboardButton("🏠 Admin",callback_data="admin:home")]])); return

    pending_final_reply=context.user_data.get('pending_final_generic_reply')
    if pending_final_reply and is_admin(settings,uid):
        value=(update.message.text or '').strip()
        if not value: await update.message.reply_text("Send a non-empty reply."); return
        iid=int(pending_final_reply); await final_generic_add_action(settings.database_path,iid,"reply",value)
        context.user_data.pop('pending_final_generic_reply',None)
        await update.message.reply_text("✅ Reply saved.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Add Command",callback_data=f"fgi:addcmd:{iid}")],[InlineKeyboardButton("💬 Add Reply",callback_data=f"fgi:addreply:{iid}")],[InlineKeyboardButton("💾 Save",callback_data=f"fgi:save:{iid}")],[InlineKeyboardButton("🔙 Back",callback_data="admin:generic_replies")],[InlineKeyboardButton("🏠 Admin",callback_data="admin:home")]])); return

    pending_generic_intent = context.user_data.get('pending_generic_intent')
    if pending_generic_intent and is_admin(settings, uid):
        intent = update.message.text.strip()
        if not intent or len(intent) > 100 or '|' in intent or '\n' in intent:
            await update.message.reply_text(
                'Please send only the intent name, e.g. <code>customer_thanks</code>.',
                parse_mode='HTML'
            ); return
        context.user_data.pop('pending_generic_intent', None)
        if context.user_data.get('pending_generic_add_type') == 'trigger':
            context.user_data['pending_generic_trigger_intent'] = intent
            await update.message.reply_text('⚡ Now send the existing trigger command, e.g. <code>/phonepe</code>.',parse_mode='HTML')
        else:
            context.user_data['pending_generic_reply'] = intent
        await update.message.reply_text(
            f'✅ Intent saved: <code>{html.escape(intent)}</code>\n\n'
            '<b>Step 2/2 — Enter the approved reply</b>\n\n'
            'Send exactly what the bot should reply when this intent is recognized.',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='admin:generic_replies')]])
        ); return

    pending_generic_trigger_intent = context.user_data.get('pending_generic_trigger_intent')
    if pending_generic_trigger_intent and is_admin(settings, uid):
        command=(update.message.text or '').strip()
        if not await generic_trigger_allowed_final2(settings.database_path,command):
            await update.message.reply_text('❌ That command is not an allowed configured bot command.',parse_mode='HTML'); return
        intent=str(pending_generic_trigger_intent)
        await add_generic_reply(settings.database_path,intent,intent,'',response_type='trigger',trigger_command=command)
        context.user_data.pop('pending_generic_trigger_intent',None)
        context.user_data.pop('pending_generic_add_type',None)
        await update.message.reply_text(f'✅ <b>Generic Trigger Saved</b>\n\nIntent: <code>{html.escape(intent)}</code>\nTrigger: <code>{html.escape(command)}</code>',parse_mode='HTML')
        return

    pending_generic_reply = context.user_data.get('pending_generic_reply')
    if pending_generic_reply and is_admin(settings, uid):
        reply_text = update.message.text.strip()
        if not reply_text:
            await update.message.reply_text('The approved reply cannot be empty.'); return
        intent = str(pending_generic_reply)
        rows = await generic_reply_rows(settings.database_path)
        same = [r for r in rows if r['intent'] == intent]
        name = intent if not same else f'{intent} #{len(same)+1}'
        await add_generic_reply(settings.database_path, name, intent, reply_text)
        context.user_data.pop('pending_generic_reply', None)
        await update.message.reply_text(
            f'💬 <b>Generic Reply Added</b>\n\n'
            f'Intent: <code>{html.escape(intent)}</code>\n'
            f'Reply: {html.escape(reply_text)}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💬 Generic Replies', callback_data='admin:generic_replies')]])
        ); return

    pending_generic_edit = context.user_data.get('pending_generic_edit')
    if pending_generic_edit and is_admin(settings, uid):
        raw = update.message.text.strip()
        parts = [x.strip() for x in raw.split('|', 2)]
        if len(parts) != 3 or not all(parts):
            await update.message.reply_text(
                'Use: <code>Name | Intent | Approved reply text</code>',
                parse_mode='HTML'
            ); return
        name, intent, reply_text = parts
        if not intent or len(intent) > 100:
            await update.message.reply_text('Intent must be a non-empty name up to 100 characters.'); return
        await update_generic_reply(settings.database_path, pending_generic_edit, name, intent, reply_text)
        context.user_data.pop('pending_generic_edit', None)
        await update.message.reply_text('💬 <b>Generic Reply Updated</b>', parse_mode='HTML')
        return

    if context.user_data.get('pending_pre_duration') and is_admin(settings, uid):
        raw=update.message.text.strip()
        try: m=int(raw); assert 1<=m<=1440
        except Exception:
            await update.message.reply_text('Send a whole number of minutes between 1 and 1440.'); return
        await set_pre_duration(settings.database_path,m); context.user_data.pop('pending_pre_duration',None)
        await update.message.reply_text(f'✅ Pre Access duration set to <b>{m} minutes</b>.',parse_mode='HTML',reply_markup=pre_keyboard(m)); return

    ce=context.user_data.get('combo_edit')
    if ce and is_admin(settings,uid):
        raw=update.message.text or ''
        cid=ce['cid']
        if ce['field'] in ('name','price','details'):
            await combo_update(settings.database_path,cid,**{ce['field']:raw.strip()})
            context.user_data.pop('combo_edit',None)
            await update.message.reply_text('✅ Combo updated.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🎁 Open Combo',callback_data=f'admin:combo:view:{cid}')]])); return

    ca=context.user_data.get('combo_add')
    if ca and is_admin(settings,uid) and update.message.text is not None:
        raw=update.message.text.strip()
        if ca['step']=='name': ca.update(step='price',name=raw); await update.message.reply_text('Step 2/3 — Send price.'); return
        if ca['step']=='price': ca.update(step='details',price=raw); await update.message.reply_text('Step 3/3 — Send complete details.'); return
        if ca['step']=='details':
            cid=await combo_create(settings.database_path,ca['name'],ca['price'],raw)
            context.user_data.pop('combo_add',None)
            await update.message.reply_text('✅ <b>Combo created.</b>\n\nNow build it message-by-message. Each saved message keeps its own content, photo(s), caption and embedded link.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('➕ Add Message',callback_data=f'admin:combo:addmsg:{cid}')],[InlineKeyboardButton('🎁 Open Combo',callback_data=f'admin:combo:view:{cid}')]])); return

    cm=context.user_data.get('combo_new_message')
    if cm and is_admin(settings,uid):
        msg=update.message; cid=cm.get('cid')
        if cm.get('type')=='text' and cm.get('step')=='content' and msg.text is not None:
            cm['text']=msg.text; context.user_data['combo_new_message']=cm
            await update.message.reply_text('💾 <b>Save Message?</b>\n\nThis exact text will be saved as this Combo message.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💾 Save Message',callback_data=f'admin:combo:message_save_text:{cid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])); return
        if cm.get('type')=='photo' and cm.get('step') in ('content','more_photo'):
            if msg.text is not None and msg.text.strip().lower() == 'done':
                if not cm.get('photos'):
                    await update.message.reply_text(
                        '❌ No photo has been saved yet. Send Photo 1 first.'
                    )
                    return

                cm['step']='caption'
                context.user_data['combo_new_message']=cm

                await update.message.reply_text(
                    f'✅ <b>Photo upload finished.</b>\n\n'
                    f'{len(cm["photos"])} photo(s) saved successfully.\n\n'
                    '✍️ Now send the caption. '
                    'It will be registered with its Telegram formatting exactly as sent.',
                    parse_mode='HTML'
                )
                return

            if msg.photo:
                d=Path(settings.media_root)/'combos'
                d.mkdir(parents=True,exist_ok=True)

                ph=msg.photo[-1]
                f=await context.bot.get_file(ph.file_id)
                dest=d/f'combo_{cid}_msg_{int(time.time())}_{ph.file_unique_id}.jpg'
                await f.download_to_drive(custom_path=str(dest))

                cm.setdefault('photos',[]).append(str(dest))
                cm['step']='more_photo'
                context.user_data['combo_new_message']=cm

                n=len(cm['photos'])

                await update.message.reply_text(
                    f'✅ <b>Photo {n} saved.</b>\n\n'
                    'If you want another photo, just send it.\n'
                    'When you are finished, type <code>done</code>.',
                    parse_mode='HTML'
                )
                return

            await update.message.reply_text(
                '🖼️ Please send a photo.\n\n'
                'If you have finished adding photos, type <code>done</code>.',
                parse_mode='HTML'
            )
            return
        if cm.get('type')=='photo' and cm.get('step')=='caption' and (msg.text is not None or msg.caption is not None):
            cap,ents=telegram_entities_payload(msg)
            cm['caption']=cap; cm['caption_entities_json']=json.dumps(ents,ensure_ascii=False); cm['step']='link_text'; context.user_data['combo_new_message']=cm
            await update.message.reply_text('🔗 <b>Embedded Link</b>\n\nSend the exact word or line from the caption that should become clickable, or tap Skip.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⏭️ Skip',callback_data=f'admin:combo:link_skip:{cid}')]])); return
        if cm.get('step')=='link_text' and msg.text is not None:
            visible=msg.text.strip(); cap=cm.get('caption','')
            if visible and visible in cap:
                cm['link_visible']=visible; cm['step']='link_url'; context.user_data['combo_new_message']=cm
                await update.message.reply_text('🔗 <b>Now send the URL</b>\n\nThe URL will stay hidden; only your selected word/line will be clickable.',parse_mode='HTML'); return
            await update.message.reply_text('❌ That exact word/line is not present in the caption. Send it exactly as it appears.'); return
        if cm.get('step')=='link_url' and msg.text is not None:
            url=msg.text.strip()
            if not re.match(r'^https?://',url,re.I): await update.message.reply_text('Please send a valid http:// or https:// URL.'); return
            cap=cm.get('caption',''); visible=cm.get('link_visible',''); ents=json.loads(cm.get('caption_entities_json') or '[]')
            idx=cap.find(visible); ents.append({'type':'text_link','offset':_utf16_len(cap[:idx]),'length':_utf16_len(visible),'url':url})
            cm['caption_entities_json']=json.dumps(ents,ensure_ascii=False); cm['step']='save_photo'; context.user_data['combo_new_message']=cm
            await update.message.reply_text('🔗 <b>Embedded link set.</b>\n\nNow save this message.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💾 Save Message',callback_data=f'admin:combo:linksave:{cid}')],[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:combo:view:{cid}')]])); return


    # INTENT_DATABASE_V1_IMPORT
    _idb = context.user_data.get("pending_intent_database_import")
    if _idb and is_admin(settings, uid):
        _msg = update.message
        _raw = _msg.text if _msg and _msg.text is not None else ""
        if _raw.strip().casefold() == "done":
            iid = int(_idb["iid"])
            total = await intent_db_count(settings.database_path, iid)
            context.user_data.pop("pending_intent_database_import", None)
            await update.message.reply_text(
                f"✅ <b>Intent Database import completed.</b>\n\n"
                f"Messages received: <b>{_idb['messages']}</b>\n"
                f"New examples saved: <b>{_idb['examples']}</b>\n"
                f"Total examples in database: <b>{total}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗄️ Intent Database", callback_data=f"fgi:db:{iid}")],
                    [InlineKeyboardButton("💬 Open Intent", callback_data=f"fgi:view:{iid}")]
                ])
            )
            return

        if _msg and _msg.text is not None:
            parsed = intent_db_parse_bullets(_raw)
            if "•" not in _raw:
                await update.message.reply_text(
                    "❌ No <code>•</code> delimiter found.\n\n"
                    "Each example must start with <code>•</code>.",
                    parse_mode="HTML"
                )
                return
            added = await intent_db_add_examples(settings.database_path, int(_idb["iid"]), parsed)
            _idb["messages"] = int(_idb.get("messages", 0)) + 1
            _idb["examples"] = int(_idb.get("examples", 0)) + added
            context.user_data["pending_intent_database_import"] = _idb
            total = await intent_db_count(settings.database_path, int(_idb["iid"]))
            await update.message.reply_text(
                f"📥 <b>Message received.</b>\n\n"
                f"Examples in this message: <b>{len(parsed)}</b>\n"
                f"New examples saved: <b>{added}</b>\n\n"
                f"Messages received: <b>{_idb['messages']}</b>\n"
                f"New examples received: <b>{_idb['examples']}</b>\n"
                f"Total database examples: <b>{total}</b>\n\n"
                "Send another message, or type <code>done</code>.",
                parse_mode="HTML"
            )
            return
        return
    if context.user_data.get('pending_ai_knowledge') and is_admin(settings, uid):
        raw = update.message.text.strip()
        if '|' not in raw:
            await update.message.reply_text('Use: <code>Title | Approved guidance</code>', parse_mode='HTML'); return
        name, content = [x.strip() for x in raw.split('|', 1)]
        if not name or not content:
            await update.message.reply_text('Both title and guidance are required.', parse_mode='HTML'); return
        await save_ai_knowledge(settings.database_path, name, content)
        context.user_data.pop('pending_ai_knowledge', None)
        await update.message.reply_text('🧠 <b>AI Knowledge Saved</b>\n\n' + html.escape(name), parse_mode='HTML', reply_markup=ai_knowledge_keyboard()); return

    pending_test = context.user_data.get('pending_test_ai')
    if pending_test and is_admin(settings, uid):
        context.user_data.pop('pending_test_ai', None)
        # TOKEN_SAVER_V4: Test AI uses exactly the same route-scoped minimal context as live traffic.
        _test_text=str(update.message.text or '')[:150]
        _test_batches=await token_smart_batch_candidates(settings.database_path,_test_text,12)
        _test_scored=token_smart_rank_batch_candidates(_test_text,_test_batches,2)
        if len(_test_scored)==1 or (len(_test_scored)>1 and _test_scored[0][0]>_test_scored[1][0]+2):
            _test_context=token_smart_batch_context(_test_text,_test_scored[:1])
        elif len(_test_scored)>1:
            _test_context=token_smart_batch_context(_test_text,_test_scored[:2])
        else:
            _test_combos=await token_smart_combo_candidates(settings.database_path,_test_text,2)
            if len(_test_combos)>0:
                _test_context=token_smart_combo_context(_test_combos)
            else:
                _test_infos=await token_smart_info_candidates(settings.database_path,_test_text,2)
                if len(_test_infos)>0:
                    _test_info_min=[{'id':str(r.get('id') or ''),'title':str(r.get('title') or ''),'aliases':str(r.get('aliases') or '')[:200]} for r in _test_infos[:2]]
                    _test_context=token_smart_info_context(_test_info_min)
                else:
                    _test_generics=await token_smart_generic_candidates(settings.database_path,_test_text,2)
                    _test_lines=['ROUTE: GENERIC_INTENT_DISAMBIGUATION','TASK: choose the exact configured intent.']
                    for _gc in _test_generics:
                        _test_lines.append(f"- ID={_gc['id']}; INTENT={_gc['intent']}; EXAMPLE={_gc['example']}")
                    _test_lines.append('Return the exact configured intent name.')
                    _test_context='\n'.join(_test_lines)
        ai_result = await token_saver_ai_classify(context, _test_text, _test_context, source='test_ai', telegram_user_id=uid, chat_id=update.effective_chat.id if update.effective_chat else None)

        current_entity = ai_result.get('entity')
        action_result = await execute(
            settings.database_path,
            ai_result,
            {'entity': current_entity, 'customer_message': update.message.text}
        )

        entity = action_result.get('entity')
        intents = ai_result.get('intents') or []
        intent_text = ', '.join(
            i.get('name', 'unknown') if isinstance(i, dict) else str(i)
            for i in intents
        ) or 'unknown'

        entity_text = (
            'None'
            if not entity
            else f"{entity.get('type', '?')} — {entity.get('name', '?')} "
                 f"(ID: {entity.get('id', '?')})"
        )

        test_text = (
            '🧪 <b>AI Test Result</b>\n\n'
            f'<b>Customer message:</b> {html.escape(update.message.text)}\n'
            f'<b>Language:</b> {html.escape(str(ai_result.get("language", "unknown")))}\n'
            f'<b>Intent:</b> {html.escape(intent_text)}\n'
            f'<b>Entity:</b> {html.escape(entity_text)}\n'
            f'<b>Business data:</b> authoritative SQLite\n'
            f'<b>Configured match:</b> ai_entity\n'
            f'<b>Action:</b> {html.escape(str(action_result.get("action", "generic_reply")))}\n'
            f'<b>Negotiation:</b> '
            f'{"requested" if action_result.get("action") == "negotiate" else "not applicable"}\n'
            f'<b>Confidence:</b> '
            f'{html.escape(str(ai_result.get("confidence", "low")))}\n'
            f'<b>Final response:</b> '
            f'{html.escape(str(action_result.get("response") or "No response"))}'
        )

        await update.message.reply_text(
            test_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🧪 Test Another', callback_data='admin:test')],
                [InlineKeyboardButton('⬅️ Admin Home', callback_data='admin:home')]
            ])
        )
        return

    pending = context.user_data.get('pending_negotiation_setting')
    if pending and is_admin(settings, uid):
        raw = update.message.text.strip().replace('₹', '').replace(',', '')
        try:
            amount = int(raw)
            if amount < 0: raise ValueError
        except ValueError:
            await update.message.reply_text('Please send a valid non-negative whole number, e.g. <code>100</code>.', parse_mode='HTML'); return
        changes = {pending: amount}
        await update_negotiation_rule(settings.database_path, **changes)
        context.user_data.pop('pending_negotiation_setting', None)
        await show_negotiation_message(update, settings.database_path)
        return

    # NEGOTIATION_FIRST_ROUTER_V35
    # Resolve contextual/cart turns before numbered-choice state can consume them.
    _customer_text=str(update.message.text or '')
    if not is_admin(settings, uid) and _customer_text and len(_customer_text) <= 150:
        try:
            if await process_negotiation_layer(update, context, settings.database_path, _customer_text):
                return
        except Exception:
            logger.exception('NEGOTIATION_FIRST_ROUTER_V35 early gate failed chat_id=%s', getattr(getattr(update, 'effective_chat', None), 'id', None))

    # TOKEN_SMART_BATCH_SELECTION_V1
    if not is_admin(settings, uid) and update.message.text and context.user_data.get('pending_batch_choices'):
        _sel=_ts_norm(update.message.text)
        _ids=context.user_data.pop('pending_batch_choices',[])
        async with aiosqlite.connect(settings.database_path) as _db:
            _db.row_factory=aiosqlite.Row
            if _sel in ('all','all batches','sab','sabhi'):
                _rows=[dict(r) for r in await (await _db.execute('SELECT * FROM batches WHERE enabled=1 AND id IN ('+','.join('?'*len(_ids))+') ORDER BY id',_ids)).fetchall()] if _ids else []
            elif _sel.isdigit() and 1 <= int(_sel) <= len(_ids):
                _rows=[dict(await (await _db.execute('SELECT * FROM batches WHERE id=? AND enabled=1',(_ids[int(_sel)-1],))).fetchone())]
            else:
                _rows=[]
        if _rows:
            for _r in _rows:
                _ar={'language':'auto','style':'concise','intents':[{'name':'batch_search','details':'saved Optional selection'}],'entity':{'type':'batch','id':str(_r['id']),'name':str(_r['name'])},'confidence':'high','response':''}
                _res=await execute(settings.database_path,_ar,{'entity':_ar['entity'],'customer_message':update.message.text})
                _txt=_res.get('response')
                if _txt: await update.message.reply_text(customer_embedded_links(_txt),parse_mode='HTML',disable_web_page_preview=True)
            return

    # ADMIN_MAIN_ACCOUNT_ISOLATION_V10
    # The configured admin/main account is never processed as a customer.
    if is_admin(settings, uid):
        return

    # TOKEN_SMART_ROUTER_V2
    # Hard customer-message limit: never call AI for messages over 150 chars.
    _customer_text=str(update.message.text or '')
    if len(_customer_text)>150:
        return

    # Admin/main account is never a customer.
    if is_admin(settings, uid):
        return

    # Existing numbered/name list context is handled entirely server-side.
    if update.message.text and await _ts_handle_pending_choice(update,context,settings.database_path,_customer_text):
        return

    # If the customer previously asked for an Optional without naming a subject,
    # remember that context and resolve the next subject locally.
    _pending_opt=context.user_data.get('pending_optional_request')
    if _pending_opt:
        _nopt=_ts_norm(_customer_text)
        _opt_found=None
        for _sub in sorted(OPTIONAL_OVERLAP_SUBJECTS|OPTIONAL_ONLY_SUBJECTS,key=len,reverse=True):
            if re.search(r'(?<!\w)'+re.escape(_sub)+r'(?!\w)',_nopt,re.UNICODE):
                _opt_found=_sub; break
        if _opt_found:
            context.user_data.pop('pending_optional_request',None)
            # Continue as an Optional request using the resolved subject.
            _customer_text=f'{_opt_found} optional'
        else:
            return

    _is_opt,_opt_subject=_ts_is_optional(_customer_text)
    _batch_candidates=await token_smart_batch_candidates(settings.database_path,_customer_text,12)

    # Optional path: specific saved batch wins. Generic subject request prefers
    # the subject Combo; only if no Combo exists do we offer individual batches.
    if _is_opt:
        if not _opt_subject:
            context.user_data['pending_optional_request']=True
            await update.message.reply_text("What's your optional subject?")
            return
        _subject_batches=[r for r in _batch_candidates if _ts_subject_in_name(_opt_subject,r)]
        _specific=_ts_specific_terms(_customer_text,_opt_subject)
        _specific_hits=[]
        for r in _subject_batches:
            rh=_ts_tokens(str(r.get('name') or '')+' '+str(r.get('search_keywords') or ''))
            if _specific & rh: _specific_hits.append(r)
        # Exact/strong specific coaching or teacher request.
        if _specific_hits:
            _specific_hits.sort(key=lambda r: len(_specific & _ts_tokens(str(r.get('name') or '')+' '+str(r.get('search_keywords') or ''))), reverse=True)
            top=_specific_hits[0]
            top_score=len(_specific & _ts_tokens(str(top.get('name') or '')+' '+str(top.get('search_keywords') or '')))
            if len(_specific_hits)==1 or top_score>len(_specific & _ts_tokens(str(_specific_hits[1].get('name') or '')+' '+str(_specific_hits[1].get('search_keywords') or ''))):
                ar={'language':'auto','style':'concise','intents':[{'name':'batch_search','details':'specific optional batch'}], 'entity':{'type':'batch','id':str(top['id']),'name':str(top['name'])},'confidence':'high','response':''}
                result=await execute(settings.database_path,ar,{'entity':ar['entity'],'customer_message':_customer_text})
                if result.get('response'):
                    context.user_data['entity']=result.get('entity') or ar['entity']
                    await update.message.reply_text(customer_embedded_links(result['response']),parse_mode='HTML',disable_web_page_preview=True)
                return
        _combo=await token_smart_combo_for_subject(settings.database_path,_opt_subject)
        if _combo:
            _cmd=await combo_command_v9(settings.database_path,int(_combo['id']))
            if _cmd and await execute_combo_command_v9(update,context,_cmd): return
        if len(_subject_batches)==1:
            r=_subject_batches[0]
            ar={'language':'auto','style':'concise','intents':[{'name':'batch_search','details':'single optional batch'}], 'entity':{'type':'batch','id':str(r['id']),'name':str(r['name'])},'confidence':'high','response':''}
            result=await execute(settings.database_path,ar,{'entity':ar['entity'],'customer_message':_customer_text})
            if result.get('response'):
                context.user_data['entity']=result.get('entity') or ar['entity']; await update.message.reply_text(customer_embedded_links(result['response']),parse_mode='HTML',disable_web_page_preview=True)
            return
        if len(_subject_batches)>1:
            context.user_data['pending_batch_choices']={'ids':[int(r['id']) for r in _subject_batches], 'subject':_opt_subject, 'optional':True}
            await update.message.reply_text('\n'.join(f'{i}. {r["name"]}' for i,r in enumerate(_subject_batches,1))+'\n\nTell me which coaching you want. Send the number, the coaching name, or <b>all</b> for all.',parse_mode='HTML')
            return
        # No Optional batch/Combo here: check Information below.

    # Strong local Batch matching. A unique/decisive match is executed with ZERO AI.
    # If there is a genuine conflict, AI sees ONLY the top two batch names/keywords
    # plus the customer's exact message. Nothing else from the business database is sent.
    _batch_ambiguous=False
    if _batch_candidates:
        if len(_batch_candidates)==1:
            r=_batch_candidates[0]
            ar={'language':'auto','style':'concise','intents':[{'name':'batch_search','details':'unique local batch match'}], 'entity':{'type':'batch','id':str(r['id']),'name':str(r['name'])},'confidence':'high','response':''}
            result=await execute(settings.database_path,ar,{'entity':ar['entity'],'customer_message':_customer_text})
            if result.get('response'):
                context.user_data['entity']=result.get('entity') or ar['entity']; await update.message.reply_text(customer_embedded_links(result['response']),parse_mode='HTML',disable_web_page_preview=True)
            return
        scored=_token_batch_scored=token_smart_rank_batch_candidates(_customer_text,_batch_candidates,2)
        if scored:
            top_score=scored[0][0]
            second_score=scored[1][0] if len(scored)>1 else -1
            if top_score>=4 and top_score>=second_score+2:
                r=scored[0][2]
                ar={'language':'auto','style':'concise','intents':[{'name':'batch_search','details':'strong local batch match'}], 'entity':{'type':'batch','id':str(r['id']),'name':str(r['name'])},'confidence':'high','response':''}
                result=await execute(settings.database_path,ar,{'entity':ar['entity'],'customer_message':_customer_text})
                if result.get('response'):
                    context.user_data['entity']=result.get('entity') or ar['entity']; await update.message.reply_text(customer_embedded_links(result['response']),parse_mode='HTML',disable_web_page_preview=True)
                return
            # Real conflict: resolve it with ONLY the top two batch candidates.
            _batch_router_context=token_smart_batch_context(_customer_text,scored[:2])
            try:
                _batch_ai=await token_saver_ai_classify(context,_customer_text,_batch_router_context,source='customer',telegram_user_id=uid,chat_id=update.effective_chat.id if update.effective_chat else None)
            except Exception:
                _batch_ai={'intents':[],'entity':None,'confidence':'low','_audit_id':None,'_raw_response':'','_input_context':_batch_router_context}
            _audit_id=_batch_ai.get('_audit_id')
            _ai_conf=str(_batch_ai.get('confidence') or 'low').casefold()
            _ai_intent=str((_batch_ai.get('intents') or [{'name':'batch_search'}])[0].get('name') or 'batch_search')
            _ent=_batch_ai.get('entity') or {}
            allowed={str(c['id']):c for c in token_smart_min_batch_candidates(scored[:2])}
            if _ai_conf in ('high','medium') and isinstance(_ent,dict) and str(_ent.get('id') or '') in allowed:
                _ent={'type':'batch','id':str(_ent['id']),'name':allowed[str(_ent['id'])]['name']}
                _follow=await local_batch_followup(settings.database_path,_ent,_customer_text)
                if _follow and _follow.get('response'):
                    context.user_data['entity']=_ent
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='batch',selected_entity_id=_ent['id'],selected_entity_name=_ent['name'],selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_batch_ai.get('_raw_response',''),input_context=_batch_ai.get('_input_context',_batch_router_context),server_action='local_batch_followup',server_status='sent')
                    await update.message.reply_text(customer_embedded_links(_follow['response']),parse_mode='HTML',disable_web_page_preview=True)
                    return
                _ar={'language':'auto','style':'concise','intents':[{'name':_ai_intent,'details':''}],'entity':_ent,'confidence':_ai_conf,'response':''}
                _res=await execute(settings.database_path,_ar,{'entity':_ent,'customer_message':_customer_text})
                if _res.get('entity'): context.user_data['entity']=_res['entity']
                if _res.get('response'):
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='batch',selected_entity_id=_ent['id'],selected_entity_name=_ent['name'],selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_batch_ai.get('_raw_response',''),input_context=_batch_ai.get('_input_context',_batch_router_context),server_action='execute_saved_batch',server_status='sent')
                    await update.message.reply_text(customer_embedded_links(_res['response']),parse_mode='HTML',disable_web_page_preview=True)
                else:
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='batch',selected_entity_id=_ent['id'],selected_entity_name=_ent['name'],selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_batch_ai.get('_raw_response',''),input_context=_batch_ai.get('_input_context',_batch_router_context),server_action='execute_saved_batch',server_status='no_response')
                return
            await update_ai_call_audit_decision(settings.database_path,_audit_id,route='batch',selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_batch_ai.get('_raw_response',''),input_context=_batch_ai.get('_input_context',_batch_router_context),server_action='none',server_status='silence')
            return


    # INTENT_DATABASE_V1_LOCAL_MATCH
    # Exact normalized lookup is server-side and consumes zero AI tokens.
    _idb_match = await intent_db_match(settings.database_path, _customer_text)
    if _idb_match:
        if await intent_db_execute_match(settings.database_path, _idb_match, update, context):
            return
    # Generic saved replies are checked locally before AI.
    _gfam=_ts_local_generic_family(_customer_text)
    _local_generic=None
    if _gfam:
        _local_generic=await token_smart_generic_local(settings.database_path,_gfam)
        if not _local_generic:
            # Try the actual customer text for custom configured intent names.
            _local_generic=await token_smart_generic_local(settings.database_path,_customer_text)
    else:
        _local_generic=await token_smart_generic_local(settings.database_path,_customer_text)
    if _local_generic:
        if _local_generic.get('action_type')=='reply':
            await update.message.reply_text(customer_embedded_links(str(_local_generic.get('value') or '')),parse_mode='HTML',disable_web_page_preview=True); return
        _gcmd=str(_local_generic.get('value') or '').strip()
        if _gcmd:
            if not _gcmd.startswith('/'): _gcmd='/'+_gcmd.lstrip('/')
            if await execute_direct_command(update,context,_gcmd,internal=True): return

    # Combo path. Generic UPSC request routes directly to configured All Combos command.
    _ctxt=_ts_tokens(_customer_text)
    if 'upsc' in _ctxt and any(x in _ctxt for x in ('course','courses','batch','batches','combo','combos','package','packages')):
        _allcmd=await all_combo_command_v9(settings.database_path)
        if _allcmd and await execute_combo_command_v9(update,context,_allcmd):
            await update.message.reply_text('Which combo would you like?'); return
    _combo_rows=await combo_rows(settings.database_path)
    _combo_candidates=[]
    for r in _combo_rows:
        if not r.get('enabled'): continue
        hit=_ctxt&_ts_tokens(str(r.get('name') or ''))
        if hit: _combo_candidates.append((len(hit),r))
    if _combo_candidates:
        _combo_candidates.sort(key=lambda x:x[0],reverse=True)
        _cr=_combo_candidates[0][1]
        _cmd=await combo_command_v9(settings.database_path,int(_cr['id']))
        if _cmd and await execute_combo_command_v9(update,context,_cmd): return

    # Information is a deterministic saved-data lookup. One clear match is sent.
    _infos=await token_smart_info_candidates(settings.database_path,_customer_text,5)
    if len(_infos)==1 and str(_infos[0].get('details') or '').strip():
        await update.message.reply_text(customer_embedded_links(str(_infos[0]['details'])),parse_mode='HTML',disable_web_page_preview=True); return

    # Only unresolved-but-evidenced cases reach AI. Each route gets ONLY its
    # own minimal candidate set. Never send the full generic/batch/combo/info catalog.
    _generic_candidates=await token_smart_generic_candidates(settings.database_path,_customer_text,2)
    _combo_candidates_min=await token_smart_combo_candidates(settings.database_path,_customer_text,2)

    # Information ambiguity: only the top two titles/aliases are sent.
    if _infos and len(_infos)>1:
        _info_min=[{'id':str(r.get('id') or ''),'title':str(r.get('title') or ''),'aliases':str(r.get('aliases') or '')[:200]} for r in _infos[:2]]
        _info_ctx=token_smart_info_context(_info_min)
        try:
            _info_ai=await token_saver_ai_classify(context,_customer_text,_info_ctx,source='customer',telegram_user_id=uid,chat_id=update.effective_chat.id if update.effective_chat else None)
        except Exception:
            _info_ai={'intents':[],'entity':None,'confidence':'low'}
        if str(_info_ai.get('confidence') or '').casefold() in ('high','medium'):
            _ie=_info_ai.get('entity') or {}
            allowed={str(x['id']):x for x in _info_min}
            _audit_id=_info_ai.get('_audit_id'); _ai_conf=str(_info_ai.get('confidence') or 'low').casefold(); _ai_intent=str((_info_ai.get('intents') or [{'name':'information'}])[0].get('name') or 'information')
            if isinstance(_ie,dict) and str(_ie.get('id') or '') in allowed:
                rr=allowed[str(_ie['id'])]
                async with aiosqlite.connect(settings.database_path) as db:
                    row=await (await db.execute('SELECT details FROM information WHERE enabled=1 AND id=? LIMIT 1',(int(rr['id']),))).fetchone()
                if row and str(row[0] or '').strip():
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='information',selected_entity_id=str(rr['id']),selected_entity_name=str(rr['title']),selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_info_ai.get('_raw_response',''),input_context=_info_ai.get('_input_context',''),server_action='saved_information',server_status='sent')
                    await update.message.reply_text(customer_embedded_links(str(row[0])),parse_mode='HTML',disable_web_page_preview=True)
                else:
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='information',selected_entity_id=str(rr['id']),selected_entity_name=str(rr['title']),selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_info_ai.get('_raw_response',''),input_context=_info_ai.get('_input_context',''),server_action='saved_information',server_status='no_response')
                return
            await update_ai_call_audit_decision(settings.database_path,_audit_id,route='information',selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_info_ai.get('_raw_response',''),input_context=_info_ai.get('_input_context',''),server_action='none',server_status='silence')

    # Combo ambiguity: only name + configured command are sent.
    if _combo_candidates_min:
        # If exactly one combo candidate exists, the deterministic command path wins.
        if len(_combo_candidates_min)==1:
            _cmd=str(_combo_candidates_min[0].get('command') or '').strip()
            if _cmd and await execute_combo_command_v9(update,context,_cmd): return
        else:
            _combo_ctx=token_smart_combo_context(_combo_candidates_min)
            try:
                _combo_ai=await token_saver_ai_classify(context,_customer_text,_combo_ctx,source='customer',telegram_user_id=uid,chat_id=update.effective_chat.id if update.effective_chat else None)
            except Exception:
                _combo_ai={'intents':[],'entity':None,'confidence':'low'}
            if str(_combo_ai.get('confidence') or '').casefold() in ('high','medium'):
                _ce=_combo_ai.get('entity') or {}
                allowed={str(x['id']):x for x in _combo_candidates_min}
                _audit_id=_combo_ai.get('_audit_id'); _ai_conf=str(_combo_ai.get('confidence') or 'low').casefold(); _ai_intent=str((_combo_ai.get('intents') or [{'name':'combo'}])[0].get('name') or 'combo')
                if isinstance(_ce,dict) and str(_ce.get('id') or '') in allowed:
                    _cmd=str(allowed[str(_ce['id'])].get('command') or '').strip()
                    if _cmd and await execute_combo_command_v9(update,context,_cmd):
                        await update_ai_call_audit_decision(settings.database_path,_audit_id,route='combo',selected_entity_id=str(_ce['id']),selected_entity_name=str(allowed[str(_ce['id'])].get('name') or ''),selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_combo_ai.get('_raw_response',''),input_context=_combo_ai.get('_input_context',''),server_action='execute_combo_command',server_status='sent')
                        return
                await update_ai_call_audit_decision(settings.database_path,_audit_id,route='combo',selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_combo_ai.get('_raw_response',''),input_context=_combo_ai.get('_input_context',''),server_action='none',server_status='silence')
                return

    # Generic ambiguity: only the two most relevant configured intents/examples are sent.
    if _generic_candidates:
        lines=['ROUTE: GENERIC_INTENT_DISAMBIGUATION','TASK: choose the exact configured intent for the customer message.']
        for c in _generic_candidates:
            lines.append(f"- ID={c['id']}; INTENT={c['intent']}; EXAMPLE={c['example']}")
        lines.append('Return the exact configured INTENT name; do not invent one.')
        try:
            _gen_ai=await token_saver_ai_classify(context,_customer_text,'\n'.join(lines),source='customer',telegram_user_id=uid,chat_id=update.effective_chat.id if update.effective_chat else None)
        except Exception:
            _gen_ai={'intents':[],'entity':None,'confidence':'low'}
        if str(_gen_ai.get('confidence') or '').casefold() in ('high','medium'):
            _audit_id=_gen_ai.get('_audit_id'); _ai_conf=str(_gen_ai.get('confidence') or 'low').casefold(); _ai_intent=str((_gen_ai.get('intents') or [{'name':'generic'}])[0].get('name') or 'generic')
            _mapped=None
            try: _mapped=await intent_trigger_for(settings.database_path,_gen_ai.get('intents',[]))
            except Exception: pass
            if _mapped:
                _cmd=str(_mapped.get('trigger') or '').strip()
                if _cmd and await execute_direct_command(update,context,_cmd,internal=True):
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='generic',selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_gen_ai.get('_raw_response',''),input_context=_gen_ai.get('_input_context',''),server_action='intent_trigger',server_status='sent')
                    return
            _final_action=await final_generic_action_for(settings.database_path,_gen_ai.get('intents',[]))
            if _final_action:
                if _final_action.get('action_type')=='reply':
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='generic',selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_gen_ai.get('_raw_response',''),input_context=_gen_ai.get('_input_context',''),server_action='saved_generic_reply',server_status='sent')
                    await update.message.reply_text(customer_embedded_links(str(_final_action.get('value') or '')),parse_mode='HTML',disable_web_page_preview=True); return
                _cmd=str(_final_action.get('value') or '').strip()
                if _cmd and await execute_direct_command(update,context,_cmd,internal=True):
                    await update_ai_call_audit_decision(settings.database_path,_audit_id,route='generic',selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_gen_ai.get('_raw_response',''),input_context=_gen_ai.get('_input_context',''),server_action='generic_command',server_status='sent')
                    return
            await update_ai_call_audit_decision(settings.database_path,_audit_id,route='generic',selected_intent=_ai_intent,confidence=_ai_conf,raw_response=_gen_ai.get('_raw_response',''),input_context=_gen_ai.get('_input_context',''),server_action='none',server_status='silence')

    # No confident configured match: SILENCE.
    return


async def show_negotiation_message(update, path):
    rule = await negotiation_rule(path)
    minimum = rule.get('minimum_price')
    minimum_text = f'₹{minimum}' if minimum is not None else 'Not configured'
    text = (f'💰 <b>Negotiation Settings Updated</b>\n\n'
            f'Maximum discount: <b>₹{rule.get("max_discount_amount",0)}</b>\n'
            f'Minimum price: <b>{minimum_text}</b>\n'
            f'Status: <b>{"ENABLED ✅" if rule.get("enabled") else "DISABLED ⛔"}</b>')
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=negotiation_keyboard(rule))


async def pre_access_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member: return
    cm=update.chat_member; old=str(cm.old_chat_member.status); new=str(cm.new_chat_member.status)
    if new not in ('member','administrator','creator') or old in ('member','administrator','creator'): return
    uid=cm.new_chat_member.user.id
    chat_id=cm.chat.id
    settings=context.application.bot_data['settings']
    await ensure_combo_pre_tables(settings.database_path)
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory=aiosqlite.Row
        invite_obj=getattr(cm,'invite_link',None)
        invite_url=getattr(invite_obj,'invite_link',None) if invite_obj else None
        if invite_url:
            r=await (await db.execute("SELECT * FROM pre_access_sessions WHERE target_chat_id=? AND status='issued' AND invite_link=? ORDER BY id DESC LIMIT 1",(chat_id,invite_url))).fetchone()
        else:
            r=await (await db.execute("SELECT * FROM pre_access_sessions WHERE target_chat_id=? AND status='issued' ORDER BY id DESC LIMIT 1",(chat_id,))).fetchone()
        if not r:return
        rid=int(r['id']); dur=await pre_duration(settings.database_path); now=int(time.time()); exp=now+dur*60
        await db.execute("UPDATE pre_access_sessions SET user_id=?,status='joined',joined_at=?,expires_at=? WHERE id=?",(uid,now,exp,rid))
        await db.commit()
    try:
        # Revoke immediately after the first join.
        await context.bot.revoke_chat_invite_link(chat_id=chat_id,invite_link=r['invite_link'])
    except Exception: logger.exception('Failed to revoke Pre Access invite')
    try:
        await context.bot.send_message(r['customer_chat_id'], f'🔗 <b>Pre Access Link Revoked</b>\n\n{html.escape(r["target_name"])}\nThe one-person invite is no longer usable.', parse_mode='HTML')
    except Exception: pass
    try:
        await context.bot.send_message(r['customer_chat_id'],f'👤 <b>Pre Access Joined</b>\n\n{html.escape(r["target_name"])}\n⏱️ Access expires in <b>{dur} minutes</b>.',parse_mode='HTML')
    except Exception: pass

async def pre_access_expiry_worker(application):
    settings=application.bot_data['settings']; await ensure_combo_pre_tables(settings.database_path)
    while True:
        try:
            now=int(time.time())
            async with aiosqlite.connect(settings.database_path) as db:
                db.row_factory=aiosqlite.Row
                rows=[dict(r) for r in await (await db.execute("SELECT * FROM pre_access_sessions WHERE status='joined' AND expires_at IS NOT NULL AND expires_at<=? LIMIT 50",(now,))).fetchall()]
            for r in rows:
                try:
                    # Ban then immediately unban: removes the member while clearing the banned-list state.
                    await application.bot.ban_chat_member(chat_id=r['target_chat_id'],user_id=r['user_id'])
                    await application.bot.unban_chat_member(chat_id=r['target_chat_id'],user_id=r['user_id'],only_if_banned=True)
                    status='removed_unbanned'
                    try: await application.bot.send_message(r['customer_chat_id'],f'🚫 <b>Pre Access Removed</b>\n\n{html.escape(r["target_name"])}\n♻️ Customer unbanned and cleared from the removed/banned list.',parse_mode='HTML')
                    except Exception: pass
                except Exception as e:
                    status='remove_failed'
                    logger.exception('Pre Access removal failed: %s',e)
                async with aiosqlite.connect(settings.database_path) as db:
                    await db.execute('UPDATE pre_access_sessions SET status=? WHERE id=?',(status,r['id'])); await db.commit()
        except Exception: logger.exception('Pre Access worker error')
        await asyncio.sleep(10)

async def run(settings: Settings):
    await init_db(settings.database_path)
    await ensure_ai_call_audit_table(settings.database_path)
    ensure_batch_columns(settings.database_path)
    await ensure_generic_replies_table(settings.database_path)
    await migrate_legacy_generic_to_final(settings.database_path)
    await ensure_intent_database_tables(settings.database_path)
    await ensure_intent_triggers_table(settings.database_path)
    await ensure_combo_command_system_v9(settings.database_path)
    await ensure_training_tables(settings.database_path)
    await ensure_information_table(settings.database_path)
    await ensure_combo_pre_tables(settings.database_path)
    await ensure_pre_access_v6_tables(settings.database_path)
    ai = AIService(settings.openai_api_key, settings.openai_model)
    application = Application.builder().token(settings.telegram_token).build()
    application.bot_data['settings'] = settings
    application.bot_data['ai'] = ai
    application.bot_data['business_cache'] = BusinessCache(settings.database_path, refresh_seconds=60)
    await application.bot_data['business_cache'].refresh(force=True)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('admin', admin))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^admin:'))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^fgi:'))
    # Slash commands are routed by the deterministic command layer.
    application.add_handler(MessageHandler(filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))
    logger.info('New AI Telegram Bot starting')
    application.add_handler(ChatMemberHandler(pre_access_member_update, ChatMemberHandler.CHAT_MEMBER))
    await application.initialize(); await application.start(); await application.updater.start_polling()
    asyncio.create_task(pre_access_expiry_worker(application))
    async def _phase34_cache_refresh():
        while True:
            try:
                await application.bot_data["business_cache"].refresh(force=True)
            except Exception:
                logger.exception("Phase34 cache refresh failed")
            await asyncio.sleep(60)

    asyncio.create_task(_phase34_cache_refresh())

    await asyncio.Event().wait()
