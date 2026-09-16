import html
import json
import time
import logging
from pathlib import Path
import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

DEFAULT_ENABLED = False

# USD / 1M tokens. Keep aligned with the bot's existing audit estimates.
MODEL_RATES = {
    'gpt-6-astra': (10.0, 1.0, 50.0),
    'gpt-5.6-sol': (4.0, 0.4, 20.0),
    'gpt-5.6-terra': (2.0, 0.2, 12.0),
    'gpt-5.6-luna': (0.2, 0.02, 1.2),
    'gpt-5.5': (5.0, 0.5, 30.0),
    'gpt-5': (1.25, 0.125, 10.0),
}

BEHAVIOR_PROFILE = """Style: brief, natural Indian English/Hinglish matching the customer. Ignore greetings/thanks/filler when a real request exists. Use current conversation state for short follow-ups. Maximum 1-2 short lines."""

BASE_INSTRUCTIONS = """AI_REPLY_LANGUAGE_GUARD_V1
Write the final Telegram customer reply.
Be concise and natural, normally one short line.
Reply only in simple English or natural Hinglish written in the Latin alphabet.
Never reply in Gujarati, Bengali, Tamil, Telugu, Marathi, Punjabi, or another language/script merely because the customer's language is unclear.
For short or language-neutral messages, default to simple English/Hinglish.
Never invent price, validity, availability, faculty, links, payment details, discounts, group counts, course years, or other business facts.
If a required fact is missing from current context, ask one short clarification.
Output only the customer-facing reply."""


def _usage(response):
    u = getattr(response, 'usage', None)
    if u is None:
        return dict(input=0, cached=0, output=0, reasoning=0, total=0)
    inp = int(getattr(u, 'input_tokens', 0) or 0)
    out = int(getattr(u, 'output_tokens', 0) or 0)
    total = int(getattr(u, 'total_tokens', 0) or 0)
    cached = 0
    reasoning = 0
    try:
        cached = int(getattr(getattr(u, 'input_tokens_details', None), 'cached_tokens', 0) or 0)
    except Exception:
        pass
    try:
        reasoning = int(getattr(getattr(u, 'output_tokens_details', None), 'reasoning_tokens', 0) or 0)
    except Exception:
        pass
    return dict(input=inp, cached=cached, output=out, reasoning=reasoning, total=total)


def _cost(model, usage):
    m = str(model or '').casefold()
    rates = None
    for key, value in MODEL_RATES.items():
        if key in m:
            rates = value
            break
    if rates is None:
        # Safe estimate fallback matching current gpt-5 pricing used by this bot.
        rates = MODEL_RATES['gpt-5']
    input_rate, cached_rate, output_rate = rates
    cached = int(usage.get('cached', 0) or 0)
    total_input = int(usage.get('input', 0) or 0)
    uncached = max(0, total_input - cached)
    output = int(usage.get('output', 0) or 0)
    return ((uncached * input_rate) + (cached * cached_rate) + (output * output_rate)) / 1_000_000.0


async def ensure(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_reply_settings(
            id INTEGER PRIMARY KEY CHECK(id=1),
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("INSERT OR IGNORE INTO ai_reply_settings(id,enabled) VALUES(1,0)")
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_reply_usage(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            telegram_user_id TEXT,
            chat_id TEXT,
            customer_name TEXT,
            customer_username TEXT,
            customer_message TEXT,
            current_state TEXT,
            model TEXT,
            status TEXT,
            error_text TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            generated_reply TEXT
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_reply_usage_created ON ai_reply_usage(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_reply_usage_chat ON ai_reply_usage(chat_id,created_at DESC)")
        await db.commit()


async def enabled(path):
    await ensure(path)
    async with aiosqlite.connect(path) as db:
        row = await (await db.execute("SELECT enabled FROM ai_reply_settings WHERE id=1")).fetchone()
    return bool(row and row[0])


async def toggle(path):
    await ensure(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE ai_reply_settings SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END, updated_at=CURRENT_TIMESTAMP WHERE id=1")
        await db.commit()


async def _history_behavior_enabled(path):
    try:
        async with aiosqlite.connect(path) as db:
            row = await (await db.execute("SELECT master_enabled,behavior_enabled FROM chat_history_settings WHERE id=1")).fetchone()
        return bool(row and row[0] and row[1])
    except Exception:
        return False


async def _current_context(path, chat_id):
    context = {'state': '', 'combo': '', 'intent': '', 'stage': ''}
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("""SELECT current_state,active_combo,current_intent,combo_stage
                FROM combo_conversation_context WHERE chat_id=?""", (str(chat_id),))).fetchone()
            if row:
                context = {
                    'state': str(row['current_state'] or ''),
                    'combo': str(row['active_combo'] or ''),
                    'intent': str(row['current_intent'] or ''),
                    'stage': str(row['combo_stage'] or ''),
                }
    except Exception:
        pass
    return context


async def _insert_usage(path, **x):
    async with aiosqlite.connect(path) as db:
        await db.execute("""INSERT INTO ai_reply_usage(
            telegram_user_id,chat_id,customer_name,customer_username,customer_message,current_state,
            model,status,error_text,input_tokens,cached_input_tokens,uncached_input_tokens,
            output_tokens,reasoning_tokens,total_tokens,estimated_cost_usd,latency_ms,generated_reply
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            x.get('telegram_user_id'), x.get('chat_id'), x.get('customer_name'), x.get('customer_username'),
            x.get('customer_message'), x.get('current_state'), x.get('model'), x.get('status'), x.get('error_text'),
            int(x.get('input_tokens') or 0), int(x.get('cached_input_tokens') or 0), int(x.get('uncached_input_tokens') or 0),
            int(x.get('output_tokens') or 0), int(x.get('reasoning_tokens') or 0), int(x.get('total_tokens') or 0),
            float(x.get('estimated_cost_usd') or 0), int(x.get('latency_ms') or 0), x.get('generated_reply')
        ))
        await db.commit()


async def generate_and_send(update, context, path, ai, customer_message):
    """Generate one fallback customer reply. Returns True iff a reply was sent."""
    if not await enabled(path):
        return False
    if not getattr(update, 'message', None) or not str(customer_message or '').strip():
        return False

    chat_id = getattr(getattr(update, 'effective_chat', None), 'id', None)
    user = getattr(update, 'effective_user', None)
    uid = getattr(user, 'id', None)
    name = ' '.join(x for x in [getattr(user, 'first_name', '') or '', getattr(user, 'last_name', '') or ''] if x).strip()
    username = str(getattr(user, 'username', '') or '')
    current = await _current_context(path, chat_id)
    behavior = await _history_behavior_enabled(path)

    current_text = (
        f"Current state: {current.get('state') or 'NONE'}\n"
        f"Current combo: {current.get('combo') or 'NONE'}\n"
        f"Current intent: {current.get('intent') or 'NONE'}\n"
        f"Current stage: {current.get('stage') or 'NONE'}"
    )
    instructions = BASE_INSTRUCTIONS + ('\n' + BEHAVIOR_PROFILE if behavior else '')
    input_text = f"{current_text}\n\nCUSTOMER MESSAGE:\n{str(customer_message).strip()}"
    model = "gpt-5.6-luna" 
    start = time.perf_counter()
    try:
        response = await ai.client.responses.create(
            model=model,
            instructions=instructions,
            input=input_text,
            reasoning={"effort": "none"},
            max_output_tokens=80,
        )
        latency = int((time.perf_counter() - start) * 1000)
        reply = str(getattr(response, 'output_text', '') or '').strip()
        u = _usage(response)
        cost = _cost(model, u)
        await _insert_usage(
            path,
            telegram_user_id=str(uid or ''), chat_id=str(chat_id or ''), customer_name=name,
            customer_username=username, customer_message=str(customer_message), current_state=current.get('state',''),
            model=model, status='success', error_text='', input_tokens=u['input'], cached_input_tokens=u['cached'],
            uncached_input_tokens=max(0, u['input']-u['cached']), output_tokens=u['output'], reasoning_tokens=u['reasoning'],
            total_tokens=u['total'], estimated_cost_usd=cost, latency_ms=latency, generated_reply=reply,
        )
        if not reply:
            return False
        await update.message.reply_text(reply)
        return True
    except Exception as e:
        latency = int((time.perf_counter() - start) * 1000)
        await _insert_usage(
            path,
            telegram_user_id=str(uid or ''), chat_id=str(chat_id or ''), customer_name=name,
            customer_username=username, customer_message=str(customer_message), current_state=current.get('state',''),
            model=model, status='error', error_text=f'{type(e).__name__}: {e}', latency_ms=latency,
        )
        return False


async def _summary(path, modifier):
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("""SELECT
            COUNT(*) calls,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) success,
            SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors,
            COALESCE(SUM(input_tokens),0) input_tokens,
            COALESCE(SUM(cached_input_tokens),0) cached_tokens,
            COALESCE(SUM(output_tokens),0) output_tokens,
            COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,
            COALESCE(SUM(total_tokens),0) total_tokens,
            COALESCE(SUM(estimated_cost_usd),0) cost,
            COALESCE(AVG(CASE WHEN status='success' THEN total_tokens END),0) avg_total,
            COALESCE(AVG(CASE WHEN status='success' THEN estimated_cost_usd END),0) avg_cost
            FROM ai_reply_usage WHERE created_at >= datetime('now', ?)""", (modifier,))).fetchone()
    return dict(row)


async def show_menu(q, path):
    e = await enabled(path)
    today = await _summary(path, 'start of day')
    text = (
        "🤖 <b>AI Reply</b>\n\n"
        "Final fallback response generator.\n"
        "Saved replies/actions and local Chat History matching always have priority.\n"
        "Historical chats influence reply behavior only; old business facts are never copied.\n\n"
        f"Status: <b>{'🟢 ON' if e else '🔴 OFF'}</b>\n\n"
        f"Today generated successfully: <b>{int(today.get('success') or 0)}</b>\n"
        f"Today tokens: <b>{int(today.get('total_tokens') or 0)}</b>\n"
        f"Today cost: <b>${float(today.get('cost') or 0):.6f}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('🔴 Turn AI Reply OFF' if e else '🟢 Turn AI Reply ON', callback_data='admin:aireply:toggle')],
        [InlineKeyboardButton('📊 AI Reply Usage', callback_data='admin:aireply:usage')],
        [InlineKeyboardButton('⬅️ Back', callback_data='admin:home')],
    ])
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)


async def show_usage(q, path):
    await ensure(path)
    today = await _summary(path, 'start of day')
    week = await _summary(path, '-7 days')
    month = await _summary(path, '-30 days')
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = [dict(r) for r in await (await db.execute("""SELECT id,created_at,customer_name,customer_username,
            customer_message,model,status,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,total_tokens,
            estimated_cost_usd FROM ai_reply_usage ORDER BY id DESC LIMIT 10""")).fetchall()]
    def block(label, s):
        return (
            f"<b>{label}</b>\n"
            f"Replies: {int(s.get('success') or 0)} | Errors: {int(s.get('errors') or 0)}\n"
            f"Input: {int(s.get('input_tokens') or 0)} | Cached: {int(s.get('cached_tokens') or 0)}\n"
            f"Output: {int(s.get('output_tokens') or 0)} | Reasoning: {int(s.get('reasoning_tokens') or 0)}\n"
            f"Total: {int(s.get('total_tokens') or 0)} | Cost: ${float(s.get('cost') or 0):.6f}\n"
            f"Avg/reply: {float(s.get('avg_total') or 0):.1f} tokens | ${float(s.get('avg_cost') or 0):.6f}"
        )
    text = "📊 <b>AI Reply Usage</b>\n\n" + block('Today', today) + "\n\n" + block('Last 7 Days', week) + "\n\n" + block('Last 30 Days', month)
    kb = []
    for r in rows:
        msg = str(r.get('customer_message') or '').replace('\n',' ').strip()
        if len(msg) > 28: msg = msg[:28] + '…'
        icon = '✅' if r.get('status') == 'success' else '❌'
        kb.append([InlineKeyboardButton(f"{icon} #{r['id']} · {msg or '(empty)'}", callback_data=f"admin:aireply:view:{r['id']}")])
    kb += [[InlineKeyboardButton('🔄 Refresh', callback_data='admin:aireply:usage')], [InlineKeyboardButton('⬅️ AI Reply', callback_data='admin:ai_reply')]]
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))


async def show_usage_detail(q, path, usage_id):
    await ensure(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        r = await (await db.execute("SELECT * FROM ai_reply_usage WHERE id=?", (int(usage_id),))).fetchone()
    if not r:
        await show_usage(q, path); return
    r = dict(r)
    cust = str(r.get('customer_name') or '').strip() or 'Unknown'
    if r.get('customer_username'):
        cust += ' @' + str(r['customer_username'])
    text = (
        f"📊 <b>AI Reply #{int(r['id'])}</b>\n\n"
        f"<b>Customer:</b> {html.escape(cust)}\n"
        f"<b>Time:</b> {html.escape(str(r.get('created_at') or ''))}\n"
        f"<b>Model:</b> <code>{html.escape(str(r.get('model') or ''))}</code>\n"
        f"<b>Status:</b> {html.escape(str(r.get('status') or ''))}\n"
        f"<b>State:</b> {html.escape(str(r.get('current_state') or 'NONE'))}\n\n"
        f"<b>Input tokens:</b> {int(r.get('input_tokens') or 0)}\n"
        f"<b>Cached input:</b> {int(r.get('cached_input_tokens') or 0)}\n"
        f"<b>Uncached input:</b> {int(r.get('uncached_input_tokens') or 0)}\n"
        f"<b>Output tokens:</b> {int(r.get('output_tokens') or 0)}\n"
        f"<b>Reasoning tokens:</b> {int(r.get('reasoning_tokens') or 0)}\n"
        f"<b>Total tokens:</b> {int(r.get('total_tokens') or 0)}\n"
        f"<b>Estimated cost:</b> ${float(r.get('estimated_cost_usd') or 0):.8f}\n"
        f"<b>Latency:</b> {int(r.get('latency_ms') or 0)} ms\n\n"
        f"<b>Customer message:</b>\n{html.escape(str(r.get('customer_message') or ''))}\n\n"
        f"<b>AI-generated reply:</b>\n{html.escape(str(r.get('generated_reply') or ''))}"
    )
    if r.get('error_text'):
        text += "\n\n<b>Error:</b>\n" + html.escape(str(r['error_text']))
    if len(text) > 3900:
        text = text[:3900] + '…'
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Usage', callback_data='admin:aireply:usage')],[InlineKeyboardButton('🏠 Admin', callback_data='admin:home')]])
    await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)

# ============================================================
# AI_REPLY_FALLBACK_USAGE_V1_COMPAT
# Compatibility API used by the live bot.py integration.
# ============================================================

async def ai_reply_ensure(path):
    await ensure(path)


async def ai_reply_get_settings(path):
    await ensure(path)
    async with aiosqlite.connect(path) as db:
        row = await (
            await db.execute(
                "SELECT enabled FROM ai_reply_settings WHERE id=1"
            )
        ).fetchone()
    return {
        "enabled": bool(row and row[0])
    }


async def ai_reply_toggle(path, new_enabled=None):
    await ensure(path)

    async with aiosqlite.connect(path) as db:
        if new_enabled is None:
            await db.execute(
                """
                UPDATE ai_reply_settings
                SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1
                """
            )
        else:
            await db.execute(
                """
                UPDATE ai_reply_settings
                SET enabled=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=1
                """,
                (1 if new_enabled else 0,)
            )

        await db.commit()

    return await ai_reply_get_settings(path)


async def ai_reply_generate(
    path,
    ai,
    customer_message,
    telegram_user_id=None,
    chat_id=None,
    customer_name="",
    customer_username=""
):
    """
    Generate but DO NOT send the reply.

    bot.py remains responsible for sending the final response.
    This prevents duplicate replies and lets this module record
    exact token/cost usage for AI-generated fallback responses.
    """

    await ensure(path)

    message = str(customer_message or "").strip()

    if not message:
        return {
            "response": "",
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

    current = await _current_context(path, chat_id)
    behavior_enabled = await _history_behavior_enabled(path)

    current_text = (
        f"Current state: {current.get('state') or 'NONE'}\n"
        f"Current combo: {current.get('combo') or 'NONE'}\n"
        f"Current intent: {current.get('intent') or 'NONE'}\n"
        f"Current stage: {current.get('stage') or 'NONE'}"
    )

    # HISTORICAL_BEHAVIOR_PACK_V1
    # Historical chats teach communication STYLE only.
    # Dynamic business facts must continue to come from live/current systems.
    instructions = BASE_INSTRUCTIONS

    try:
        _behavior_card_path = Path(
            "data/ai_behavior_training/behavior_card.txt"
        )

        if _behavior_card_path.exists():
            _historical_behavior = (
                _behavior_card_path.read_text(
                    encoding="utf-8"
                ).strip()
            )

            if _historical_behavior:
                instructions += (
                    "\n\nHISTORICAL COMMUNICATION BEHAVIOR:\n"
                    + _historical_behavior
                )
    except Exception:
        logger.exception(
            "Historical behavior card loading failed"
        )

    if behavior_enabled:
        instructions += "\n" + BEHAVIOR_PROFILE

    input_text = (
        f"{current_text}\n\n"
        f"CUSTOMER MESSAGE:\n{message}"
    )

    model = "gpt-5.6-luna" 

    start = time.perf_counter()

    try:
        response = await ai.client.responses.create(
            model=model,
            instructions=instructions,
            input=input_text,
            reasoning={"effort": "none"},
            max_output_tokens=80,
        )

        latency_ms = int(
            (time.perf_counter() - start) * 1000
        )

        reply = str(
            getattr(response, "output_text", "") or ""
        ).strip()

        usage = _usage(response)
        cost = _cost(model, usage)

        await _insert_usage(
            path,
            telegram_user_id=str(telegram_user_id or ""),
            chat_id=str(chat_id or ""),
            customer_name=str(customer_name or ""),
            customer_username=str(customer_username or ""),
            customer_message=message,
            current_state=current.get("state", ""),
            model=model,
            status="success",
            error_text="",
            input_tokens=usage["input"],
            cached_input_tokens=usage["cached"],
            uncached_input_tokens=max(
                0,
                usage["input"] - usage["cached"]
            ),
            output_tokens=usage["output"],
            reasoning_tokens=usage["reasoning"],
            total_tokens=usage["total"],
            estimated_cost_usd=cost,
            latency_ms=latency_ms,
            generated_reply=reply,
        )

        return {
            "response": reply,
            "model": model,
            "input_tokens": usage["input"],
            "cached_tokens": usage["cached"],
            "uncached_tokens": max(
                0,
                usage["input"] - usage["cached"]
            ),
            "output_tokens": usage["output"],
            "reasoning_tokens": usage["reasoning"],
            "total_tokens": usage["total"],
            "estimated_cost_usd": cost,
            "latency_ms": latency_ms,
        }

    except Exception as e:
        latency_ms = int(
            (time.perf_counter() - start) * 1000
        )

        await _insert_usage(
            path,
            telegram_user_id=str(telegram_user_id or ""),
            chat_id=str(chat_id or ""),
            customer_name=str(customer_name or ""),
            customer_username=str(customer_username or ""),
            customer_message=message,
            current_state=current.get("state", ""),
            model=model,
            status="error",
            error_text=f"{type(e).__name__}: {e}",
            latency_ms=latency_ms,
            generated_reply="",
        )

        raise


async def ai_reply_usage_summary(path, days):
    await ensure(path)

    if int(days) <= 1:
        modifier = "start of day"
    else:
        modifier = f"-{int(days)} days"

    result = await _summary(path, modifier)

    return {
        "calls": int(result.get("success") or 0),
        "errors": int(result.get("errors") or 0),
        "input_tokens": int(
            result.get("input_tokens") or 0
        ),
        "cached_tokens": int(
            result.get("cached_tokens") or 0
        ),
        "output_tokens": int(
            result.get("output_tokens") or 0
        ),
        "reasoning_tokens": int(
            result.get("reasoning_tokens") or 0
        ),
        "total_tokens": int(
            result.get("total_tokens") or 0
        ),
        "cost": float(
            result.get("cost") or 0
        ),
        "avg_total": float(
            result.get("avg_total") or 0
        ),
        "avg_cost": float(
            result.get("avg_cost") or 0
        ),
    }


async def ai_reply_recent(path, limit=8):
    await ensure(path)

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        rows = await (
            await db.execute(
                """
                SELECT *
                FROM ai_reply_usage
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),)
            )
        ).fetchall()

    return [dict(r) for r in rows]


async def ai_reply_get_record(path, record_id):
    await ensure(path)

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row

        row = await (
            await db.execute(
                """
                SELECT *
                FROM ai_reply_usage
                WHERE id=?
                LIMIT 1
                """,
                (int(record_id),)
            )
        ).fetchone()

    if not row:
        return None

    r = dict(row)

    # Names expected by the live admin UI.
    r["cached_tokens"] = int(
        r.get("cached_input_tokens") or 0
    )
    r["response"] = str(
        r.get("generated_reply") or ""
    )
    r["latency_seconds"] = (
        float(r.get("latency_ms") or 0) / 1000.0
    )

    return r

