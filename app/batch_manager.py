import re
import time
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

BATCH_EXTRA_COLUMNS = {
    'telegram_chat_id': 'TEXT',
    'telegram_message_id': 'INTEGER',
    'telegram_verified': 'INTEGER NOT NULL DEFAULT 0',
    'telegram_can_invite': 'INTEGER NOT NULL DEFAULT 0',
    'access_link_enabled': 'INTEGER NOT NULL DEFAULT 0',
    'slash_trigger': 'TEXT',
}

def ensure_batch_columns(path):
    con=sqlite3.connect(path)
    try:
        cols={r[1] for r in con.execute('PRAGMA table_info(batches)').fetchall()}
        for name, typ in BATCH_EXTRA_COLUMNS.items():
            if name not in cols:
                con.execute(f'ALTER TABLE batches ADD COLUMN {name} {typ}')
        con.commit()
    finally: con.close()

def slug_trigger(name):
    s=re.sub(r'[^a-z0-9_]+','_',name.lower()).strip('_')
    s=s[:30] or 'batch'
    return s

def parse_message_link(link):
    link=link.strip()
    p=urlparse(link)
    if p.scheme not in ('http','https') or p.netloc not in ('t.me','telegram.me','www.t.me'):
        raise ValueError('Please send a valid Telegram message link.')
    parts=[x for x in p.path.split('/') if x]
    if len(parts)<2: raise ValueError('This must be a message link from the batch channel/group.')
    if parts[0]=='c':
        if len(parts)<3 or not parts[1].isdigit() or not parts[2].isdigit(): raise ValueError('Invalid private Telegram message link.')
        return int('-100'+parts[1]), int(parts[2])
    username=parts[0]
    if username.isdigit(): raise ValueError('Invalid Telegram channel link.')
    if not parts[1].isdigit(): raise ValueError('Invalid Telegram message link.')
    return '@'+username, int(parts[1])

def get_batch(path,batch_id):
    con=sqlite3.connect(path); con.row_factory=sqlite3.Row
    try:
        r=con.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
        return dict(r) if r else None
    finally: con.close()

def search_batches(path,query,limit=10):
    con=sqlite3.connect(path); con.row_factory=sqlite3.Row
    try:
        q=f'%{query.strip()}%'
        rows=con.execute('SELECT id,name,fee,duration,enabled,telegram_verified,access_link_enabled FROM batches WHERE name LIKE ? OR description LIKE ? OR search_keywords LIKE ? ORDER BY name LIMIT ?', (q,q,q,limit)).fetchall()
        return [dict(r) for r in rows]
    finally: con.close()

def update_batch_telegram(path,batch_id,chat_id,message_id,verified,can_invite,trigger=None):
    con=sqlite3.connect(path)
    try:
        if trigger is None:
            con.execute('UPDATE batches SET telegram_chat_id=?,telegram_message_id=?,telegram_verified=?,telegram_can_invite=? WHERE id=?',(str(chat_id),message_id,int(verified),int(can_invite),batch_id))
        else:
            con.execute('UPDATE batches SET telegram_chat_id=?,telegram_message_id=?,telegram_verified=?,telegram_can_invite=?,slash_trigger=? WHERE id=?',(str(chat_id),message_id,int(verified),int(can_invite),trigger,batch_id))
        con.commit()
    finally: con.close()

def set_access_enabled(path,batch_id,enabled):
    con=sqlite3.connect(path)
    try: con.execute('UPDATE batches SET access_link_enabled=? WHERE id=?',(int(enabled),batch_id)); con.commit()
    finally: con.close()

def insert_batch(path,data):
    con=sqlite3.connect(path)
    try:
        sql = """INSERT INTO batches (name,icon,chat,enabled,created_at,updated_at,auto_link,description,fee,duration,timing,image_path,demo_link,pay_link,faq,search_keywords,faq_image_path,link_generator,bot_chat_id,request_link,telegram_chat_id,telegram_message_id,telegram_verified,telegram_can_invite,access_link_enabled,slash_trigger) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        vals=(data.get('name'),data.get('icon',''),data.get('chat',''),1,int(time.time()),int(time.time()),0,data.get('description',''),data.get('fee',''),data.get('duration',''),data.get('timing',''),data.get('image_path',''),data.get('demo_link',''),data.get('pay_link',''),data.get('faq',''),data.get('search_keywords',''),data.get('faq_image_path',''),data.get('link_generator',''),data.get('bot_chat_id',''),data.get('request_link',''),data.get('telegram_chat_id'),data.get('telegram_message_id'),int(bool(data.get('telegram_verified'))),int(bool(data.get('telegram_can_invite'))),int(bool(data.get('access_link_enabled'))),data.get('slash_trigger') or slug_trigger(data.get('name','')))
        cur=con.execute(sql,vals)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()

def update_batch_field(path,batch_id,field,value):
    allowed={'name','description','fee','duration','timing','image_path','demo_link','pay_link','request_link','search_keywords','access_link_enabled','slash_trigger','enabled'}
    if field not in allowed: raise ValueError('Field not editable')
    con=sqlite3.connect(path)
    try: con.execute(f'UPDATE batches SET {field}=?,updated_at=? WHERE id=?',(value,int(time.time()),batch_id)); con.commit()
    finally: con.close()

def create_invite(bot,chat_id):
    # Kept as a small helper for the bot layer; Telegram API call is async there.
    return chat_id


def delete_batch(path,batch_id):
    con=sqlite3.connect(path)
    try:
        con.execute('DELETE FROM batches WHERE id=?',(batch_id,))
        con.commit()
    finally:
        con.close()
