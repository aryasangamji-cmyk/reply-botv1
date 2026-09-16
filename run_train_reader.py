import asyncio
import html
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiosqlite
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telegram import Bot

load_dotenv()
logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("train_reader")

DB = os.getenv("DATABASE_PATH", "data/bot.sqlite3")
SESSION = os.getenv("TRAIN_READER_SESSION", "data/train_reader.session")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
PHONE = os.getenv("TELEGRAM_PHONE", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = os.getenv("OPENAI_MODEL", "gpt-5").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]

if not API_ID or not API_HASH:
    raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required in .env")
if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY is required in .env")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required in .env")

client = TelegramClient(SESSION, API_ID, API_HASH)
ai = AsyncOpenAI(api_key=OPENAI_KEY)
bot = Bot(BOT_TOKEN)
ME_ID = None
# Outgoing IDs recorded here can be excluded if a future main-account AI process shares the DB.

async def ensure_tables():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_generated_message_ids (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id,message_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_training_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_text TEXT NOT NULL DEFAULT '', human_reply TEXT NOT NULL DEFAULT '',
            intent TEXT NOT NULL DEFAULT 'unknown', entity TEXT NOT NULL DEFAULT '',
            learned_logic TEXT NOT NULL DEFAULT '', learning_summary TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ai_training_read_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL, chat_link TEXT, first_link TEXT, last_link TEXT,
            status TEXT NOT NULL DEFAULT 'queued', created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("INSERT OR IGNORE INTO ai_training_settings(key,value) VALUES('auto_read','0')")
        await db.commit()

async def auto_read_enabled():
    async with aiosqlite.connect(DB) as db:
        row=await (await db.execute("SELECT value FROM ai_training_settings WHERE key='auto_read'" )).fetchone()
    return bool(row and str(row[0])=='1')

async def mark_processed(rid, status='processed'):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE ai_training_read_requests SET status=? WHERE id=?", (status,rid))
        await db.commit()

async def parse_link(link):
    link=(link or '').strip()
    if link.startswith('tg://openmessage?'):
        q=parse_qs(urlparse(link).query)
        uid=q.get('user_id',[''])[0]; mid=q.get('message_id',[''])[0]
        if not uid or not mid: raise ValueError('Invalid tg://openmessage link')
        return ('user', int(uid), int(mid))
    m=re.search(r'https?://t\.me/c/(\d+)/(\d+)',link)
    if m: return ('channel', int('-100'+m.group(1)), int(m.group(2)))
    m=re.search(r'https?://t\.me/([A-Za-z0-9_]+)/?(\d+)?',link)
    if m and m.group(2): return ('username', m.group(1), int(m.group(2)))
    raise ValueError('Unsupported Telegram message link. Use tg://openmessage or https://t.me/.../<message_id>.')

async def resolve_peer(kind, value):
    if kind != 'user':
        return await client.get_entity(value)

    # tg://openmessage links contain only a user_id, while Telethon also
    # needs the peer access_hash. Refresh the account dialogs and resolve
    # the matching user entity from there instead of passing a bare ID.
    target_id = int(value)
    try:
        entity = await client.get_entity(target_id)
        if getattr(entity, 'id', None) == target_id:
            return entity
    except Exception:
        pass

    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, 'entity', None)
        if entity is not None and getattr(entity, 'id', None) == target_id:
            return entity

    raise ValueError(
        f'Customer user_id {target_id} was not found in the main account dialogs. '
        'Open the customer chat in Telegram with the connected account first, then retry.'
    )

async def fetch_range(peer, first_id, last_id):
    lo,hi=sorted((int(first_id),int(last_id)))
    msgs=[]
    async for m in client.iter_messages(peer, min_id=lo-1, max_id=hi+1, reverse=True):
        if lo <= m.id <= hi: msgs.append(m)
    return msgs

async def iter_all_chunks(peer, chunk_size=100):
    """Stream the full chat in small chronological chunks instead of loading it all into RAM."""
    chunk=[]
    async for m in client.iter_messages(peer, reverse=True):
        chunk.append(m)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk=[]
    if chunk:
        yield chunk

async def process_all_chat(peer):
    # Keep only a four-message overlap so each human reply can see up to
    # four messages before and after it without retaining the whole history.
    carry=[]
    saved=[]
    total=0
    async for chunk in iter_all_chunks(peer, chunk_size=100):
        messages=carry + chunk
        total += len(chunk)
        if len(messages) <= 4:
            carry=messages
            continue
        stable=messages[:-4]
        carry=messages[-4:]
        windows=build_learning_windows(stable)
        if windows:
            items=await extract_learnings(windows)
            new_saved=await save_learnings(items,'read_all')
            saved.extend(new_saved)
            saved=saved[-10:]
    if carry:
        windows=build_learning_windows(carry)
        if windows:
            items=await extract_learnings(windows)
            saved.extend(await save_learnings(items,'read_all'))
            saved=saved[-10:]
    return total, saved

def speaker(m):
    return 'ME' if getattr(m,'out',False) or (ME_ID and getattr(m,'sender_id',None)==ME_ID) else 'CUSTOMER'

def message_text(m):
    t=(m.raw_text or '').strip()
    if t: return t
    if m.media: return '[media message]'
    return ''

def build_learning_windows(messages):
    # Every human/outgoing message is a candidate. Include up to 4 messages before and after.
    result=[]
    for i,m in enumerate(messages):
        if speaker(m)!='ME': continue
        reply=message_text(m)
        if not reply: continue
        before=messages[max(0,i-4):i]
        after=messages[i+1:min(len(messages),i+5)]
        result.append({
            'message_id':m.id,
            'before':[(speaker(x),message_text(x)) for x in before if message_text(x)],
            'reply':reply,
            'after':[(speaker(x),message_text(x)) for x in after if message_text(x)],
        })
    return result

async def extract_learnings(windows):
    if not windows: return []
    # Keep the full collected windows, but chunk very large histories to avoid oversized requests.
    chunks=[]; cur=[]; size=0
    for w in windows:
        s=json.dumps(w,ensure_ascii=False)
        if cur and size+len(s)>30000:
            chunks.append(cur); cur=[]; size=0
        cur.append(w); size+=len(s)
    if cur: chunks.append(cur)
    all_items=[]
    for chunk in chunks:
        prompt=("You are extracting training examples from a Telegram customer conversation. "
                "ONLY learn from messages labeled ME. CUSTOMER messages are context only. "
                "Return JSON array, maximum 10 items. For each item provide intent, entity, learned_logic, "
                "learning_summary, human_reply, and context_text. The summary must be concise and explain what the human learned/did. "
                "Do not invent facts. Preserve the human reply exactly.\n\n"+json.dumps(chunk,ensure_ascii=False))
        r=await ai.responses.create(model=MODEL,input=prompt)
        txt=getattr(r,'output_text','') or ''
        try:
            data=json.loads(txt)
        except Exception:
            m=re.search(r'\[.*\]',txt,re.S)
            data=json.loads(m.group(0)) if m else []
        if isinstance(data,list): all_items.extend(data)
    return all_items[-10:]

async def save_learnings(items, source):
    if not items: return []
    saved=[]
    async with aiosqlite.connect(DB) as db:
        for x in items:
            intent=str(x.get('intent') or 'unknown').strip()
            entity=str(x.get('entity') or '').strip()
            logic=str(x.get('learned_logic') or '').strip()
            summary=str(x.get('learning_summary') or logic).strip()
            reply=str(x.get('human_reply') or '').strip()
            ctx=str(x.get('context_text') or '').strip()
            if not reply: continue
            cur=await db.execute("INSERT INTO ai_learnings(context_text,human_reply,intent,entity,learned_logic,learning_summary,source) VALUES(?,?,?,?,?,?,?)",
                                 (ctx,reply,intent,entity,logic,summary,source))
            saved.append((cur.lastrowid,intent,summary,reply))
        await db.execute("DELETE FROM ai_learnings WHERE id NOT IN (SELECT id FROM ai_learnings ORDER BY id DESC LIMIT 10)")
        await db.commit()
    return saved

async def notify_learning(saved, title):
    if not saved: return
    lines=[f'🧠 <b>{html.escape(title)}</b>', '', f'New learnings saved: <b>{len(saved)}</b>', '']
    for lid,intent,summary,reply in saved[-10:]:
        lines.append(f'<b>#{lid}</b> 🎯 <code>{html.escape(intent)}</code>\n🧠 {html.escape(summary[:700])}\n💬 {html.escape(reply[:500])}\n')
    lines.append('📚 Open Train AI → Recent Learnings to edit or delete them.')
    text='\n'.join(lines)
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid,text,parse_mode='HTML')
        except Exception: logger.exception('Failed to notify admin %s',aid)

async def process_request(row):
    rid,mode,chat_link,first_link,last_link=row
    try:
        if mode=='all':
            kind,val,mid=await parse_link(chat_link)
            peer=await resolve_peer(kind,val)
            total,saved=await process_all_chat(peer)
            if not total: raise ValueError('No messages found in the requested chat.')
            await mark_processed(rid,'processed')
            await notify_learning(saved, 'Read All Chat completed')
            logger.info('Processed request #%s: %s messages, %s learning(s)',rid,total,len(saved))
            return
        else:
            k1,v1,fid=await parse_link(first_link); k2,v2,lid=await parse_link(last_link)
            if (k1,v1)!=(k2,v2): raise ValueError('FIRST and LAST links must belong to the same chat.')
            peer=await resolve_peer(k1,v1)
            messages=await fetch_range(peer,fid,lid)
        if not messages: raise ValueError('No messages found in the requested range.')
        windows=build_learning_windows(messages)
        items=await extract_learnings(windows)
        saved=await save_learnings(items, 'read_range')
        await mark_processed(rid,'processed')
        await notify_learning(saved, 'Read In Range completed')
        logger.info('Processed request #%s: %s messages, %s learning(s)',rid,len(messages),len(saved))
    except Exception as e:
        logger.exception('Read request #%s failed',rid)
        await mark_processed(rid,'error:'+str(e)[:500])
        for aid in ADMIN_IDS:
            try: await bot.send_message(aid,f'❌ <b>Read Chat request #{rid} failed</b>\n\n{html.escape(str(e))}',parse_mode='HTML')
            except Exception: pass

async def request_worker():
    while True:
        try:
            async with aiosqlite.connect(DB) as db:
                row=await (await db.execute("SELECT id,mode,chat_link,first_link,last_link FROM ai_training_read_requests WHERE status='queued' ORDER BY id LIMIT 1")).fetchone()
            if row:
                await mark_processed(row[0],'processing')
                await process_request(row)
            else:
                await asyncio.sleep(3)
        except Exception:
            logger.exception('request worker error'); await asyncio.sleep(5)

@client.on(events.NewMessage(incoming=True))
async def incoming_handler(event):
    # Kept for future context buffering; Auto Read is finalized on outgoing human replies.
    return

@client.on(events.NewMessage(outgoing=True))
async def outgoing_handler(event):
    try:
        if not await auto_read_enabled(): return
        if not event.is_private: return
        mid=event.message.id; chat_id=event.chat_id
        async with aiosqlite.connect(DB) as db:
            airow=await (await db.execute('SELECT 1 FROM ai_generated_message_ids WHERE chat_id=? AND message_id=?',(chat_id,mid))).fetchone()
        if airow: return
        # Wait briefly so any immediately following context messages can appear; then use current ±4 window.
        await asyncio.sleep(2)
        peer=await event.get_chat()
        messages=await client.get_messages(peer, limit=9, offset_id=mid+4)
        # Telethon's offset behavior differs across versions; use explicit surrounding IDs for reliability.
        messages=await fetch_range(peer, max(1,mid-4), mid+4)
        windows=[w for w in build_learning_windows(messages) if w['message_id']==mid]
        items=await extract_learnings(windows)
        saved=await save_learnings(items,'auto_read')
        await notify_learning(saved,'Auto Read learning captured')
    except Exception:
        logger.exception('Auto Read failed')

async def main():
    global ME_ID
    await ensure_tables()
    await client.start(phone=PHONE or None)
    me=await client.get_me(); ME_ID=me.id if me else None
    logger.info('Main-account training reader connected as user_id=%s',ME_ID)
    worker=asyncio.create_task(request_worker())
    try: await client.run_until_disconnected()
    finally: worker.cancel(); await bot.shutdown()

if __name__=='__main__': asyncio.run(main())
