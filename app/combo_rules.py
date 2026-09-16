import html, json, re, time
from pathlib import Path
import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import logging

logger = logging.getLogger(__name__)
GROUP_NAME = "Combo Rules"
GROUP_SLUG = "combo"

SEED_RULES = [{'intent': 'What is Top Faculty?', 'reply_en': 'Whatever is mentioned in the Top Faculty list is part of Top Faculty.', 'reply_hi': 'Top Faculty ki list mein jo bhi mentioned hai, woh Top Faculty ka part hai.', 'trigger': '/topfaculty', 'keywords': 'top faculty list mentioned part'}, {'intent': 'What does Top Faculty include?', 'reply_en': 'Top Faculty includes only the Top Faculty lectures mentioned in the Top Faculty list.', 'reply_hi': 'Top Faculty mein sirf Top Faculty list mein mentioned lectures included hain.', 'trigger': '', 'keywords': 'top faculty includes lectures list'}, {'intent': 'What does Pro Pack include?', 'reply_en': 'Pro Pack includes Top Faculty lectures, All Coaching Combo, and one Optional.', 'reply_hi': 'Pro Pack mein Top Faculty lectures, All Coaching Combo aur ek Optional included hai.', 'trigger': '', 'keywords': 'pro pack include'}, {'intent': 'Difference between Top Faculty and Pro Pack', 'reply_en': 'Top Faculty includes only the Top Faculty lectures mentioned in the Top Faculty list. Pro Pack includes Top Faculty + All Coaching GS + one Optional Combo (one subject).', 'reply_hi': 'Top Faculty mein sirf Top Faculty list mein mentioned lectures included hain. Pro Pack mein Top Faculty + All Coaching GS + ek Optional Combo (ek subject) included hai.', 'trigger': '', 'keywords': 'difference top faculty pro pack'}, {'intent': 'Top Faculty price', 'reply_en': '', 'reply_hi': '', 'trigger': '/topfaculty', 'keywords': 'top faculty price 999'}, {'intent': 'Pro Pack price', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'pro pack price 1499'}, {'intent': '₹999 course or batch request', 'reply_en': '', 'reply_hi': '', 'trigger': '/topfaculty', 'keywords': '999 top faculty course batch'}, {'intent': '₹1499 course or batch request', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': '1499 pro pack course batch'}, {'intent': 'Generic GS lecture request', 'reply_en': '', 'reply_hi': '', 'trigger': '/allcombo', 'keywords': 'gs lecture generic'}, {'intent': 'What things do you provide', 'reply_en': '', 'reply_hi': '', 'trigger': '/allcombo', 'keywords': 'what things provide'}, {'intent': 'Generic UPSC course request', 'reply_en': '', 'reply_hi': '', 'trigger': '/allcombo', 'keywords': 'upsc course classes lectures batch'}, {'intent': 'Generic UPSC package request', 'reply_en': '', 'reply_hi': '', 'trigger': '/allcombo', 'keywords': 'upsc package coaching'}, {'intent': 'Generic combo interest', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'interested combo offer price details'}, {'intent': 'GS plus Optional request', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'gs optional together'}, {'intent': 'How many groups are in Pro Pack?', 'reply_en': 'Pro Pack has 18 groups in total, including the Optional.', 'reply_hi': 'Pro Pack mein total 18 groups hain, Optional ko include karke.', 'trigger': '/propack', 'keywords': 'how many groups pro pack 18'}, {'intent': 'Is the course genuine?', 'reply_en': 'Yes, it is genuine.', 'reply_hi': 'Haan bro, genuine hai.', 'trigger': '/proof', 'keywords': 'genuine course'}, {'intent': 'Is the course authentic?', 'reply_en': 'Yes, it is authentic.', 'reply_hi': 'Haan bro, authentic hai.', 'trigger': '/proof', 'keywords': 'authentic'}, {'intent': 'Show proof', 'reply_en': '', 'reply_hi': '', 'trigger': '/proof', 'keywords': 'show proof'}, {'intent': 'What proof do you have?', 'reply_en': '', 'reply_hi': '', 'trigger': '/proof', 'keywords': 'what proof'}, {'intent': 'Show proof that you have lectures', 'reply_en': '', 'reply_hi': '', 'trigger': '/proof', 'keywords': 'proof lectures'}, {'intent': 'Can I download the lectures?', 'reply_en': 'Yes, you can download the lectures on an Android phone.', 'reply_hi': 'Haan bro, Android phone par lectures download kar sakte ho.', 'trigger': '', 'keywords': 'download lectures android'}, {'intent': 'Where are the lectures?', 'reply_en': 'Lectures are provided through private premium group access.', 'reply_hi': 'Lectures private premium group access ke through milte hain.', 'trigger': '', 'keywords': 'where lectures private group'}, {'intent': 'Can I get PRE-access before payment?', 'reply_en': 'If you want, we can provide PRE-access for five minutes to Top Faculty.', 'reply_hi': 'Agar aap chaho toh Top Faculty ka five-minute PRE-access de sakte hain.', 'trigger': '', 'keywords': 'pre access before payment'}, {'intent': 'Customer confirms PRE-access', 'reply_en': '', 'reply_hi': '', 'trigger': '/pre topfaculty', 'keywords': 'yes pre access want pre access confirm'}, {'intent': 'Add me first then I will pay', 'reply_en': 'If you want, we can provide PRE-access for five minutes to Top Faculty.', 'reply_hi': 'Agar aap chaho toh Top Faculty ka five-minute PRE-access de sakte hain.', 'trigger': '', 'keywords': 'add first pay pre access'}, {'intent': 'Add me first in Pro Pack', 'reply_en': 'If you want, we can provide PRE-access for five minutes to Top Faculty.', 'reply_hi': 'Agar aap chaho toh Top Faculty ka five-minute PRE-access de sakte hain.', 'trigger': '', 'keywords': 'add first pro pack'}, {'intent': 'Why only Top Faculty for PRE-access?', 'reply_en': 'Pro Pack has 18 groups, so we can provide PRE-access only to Top Faculty.', 'reply_hi': 'Pro Pack mein 18 groups hain, isliye sirf Top Faculty ka PRE-access de sakte hain.', 'trigger': '', 'keywords': 'why only top faculty pre access'}, {'intent': 'Optional included in Pro Pack', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'optional included pro pack'}, {'intent': 'Optional not included in Top Faculty', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'optional not top faculty'}, {'intent': 'One Optional included', 'reply_en': 'One Optional subject is included in Pro Pack.', 'reply_hi': 'Pro Pack mein ek Optional subject included hai.', 'trigger': '', 'keywords': 'one optional included'}, {'intent': 'Additional Optional price', 'reply_en': 'Additional Optional is ₹500.', 'reply_hi': 'Additional Optional ₹500 ka hai.', 'trigger': '', 'keywords': 'additional optional 500'}, {'intent': 'Can I choose Optional later?', 'reply_en': 'Yes, you can choose the included Optional later anytime without an extra fee.', 'reply_hi': 'Haan bro, included Optional baad mein kabhi bhi choose kar sakte ho, extra fee nahi lagegi.', 'trigger': '', 'keywords': 'optional later choose'}, {'intent': 'Customer selects an Optional', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'select optional subject'}, {'intent': 'Selected Optional is unavailable', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'optional unavailable relevant list'}, {'intent': 'QR payment request', 'reply_en': 'We cannot accept UPI QR/scanner payments; payment is only through gift cards.', 'reply_hi': 'Hum UPI QR/scanner payment accept nahi karte; payment sirf gift card se karna hai.', 'trigger': '', 'keywords': 'qr scanner upi payment'}, {'intent': 'UPI payment request', 'reply_en': 'We cannot accept UPI QR/scanner payments; payment is only through gift cards.', 'reply_hi': 'Hum UPI QR/scanner payment accept nahi karte; payment sirf gift card se karna hai.', 'trigger': '', 'keywords': 'upi payment'}, {'intent': 'Scanner payment request', 'reply_en': 'We cannot accept UPI QR/scanner payments; payment is only through gift cards.', 'reply_hi': 'Hum UPI QR/scanner payment accept nahi karte; payment sirf gift card se karna hai.', 'trigger': '', 'keywords': 'scanner payment'}, {'intent': 'Purchase or payment intent', 'reply_en': 'Do you use PhonePe?', 'reply_hi': 'Aap PhonePe use karte ho?', 'trigger': '', 'keywords': 'purchase payment phonepe'}, {'intent': 'Customer says yes to PhonePe', 'reply_en': '', 'reply_hi': '', 'trigger': '/phonepe', 'keywords': 'yes phonepe'}, {'intent': 'Customer cannot use PhonePe', 'reply_en': 'Can you download PhonePe? It will be easier for the payment.', 'reply_hi': 'Kya aap PhonePe download kar sakte ho? Payment ke liye easier rahega.', 'trigger': '', 'keywords': 'cannot phonepe download'}, {'intent': 'PhonePe not possible', 'reply_en': '', 'reply_hi': '', 'trigger': '/pay', 'keywords': 'phonepe not possible no phonepe'}, {'intent': 'Gift card payment', 'reply_en': 'Yes, gift card se hi payment karna hai.', 'reply_hi': 'Haan bro, gift card se hi payment karna hai.', 'trigger': '', 'keywords': 'gift card payment'}, {'intent': 'After payment', 'reply_en': 'Please send the payment screenshot.', 'reply_hi': 'Payment ke baad screenshot send kar dena.', 'trigger': '', 'keywords': 'after payment screenshot'}, {'intent': 'Send group link after payment', 'reply_en': '', 'reply_hi': '', 'trigger': '/pay', 'keywords': 'group link after payment'}, {'intent': 'Telegram Premium or folder limit', 'reply_en': '', 'reply_hi': '', 'trigger': '/premium', 'keywords': 'telegram premium folder limit'}, {'intent': 'Final price or how much', 'reply_en': 'The price is fixed.', 'reply_hi': 'Price fixed hai.', 'trigger': '', 'keywords': 'final price how much'}, {'intent': 'Discount request', 'reply_en': "Bro, this is a premium course. We can't discount it.", 'reply_hi': 'Bro, ye premium course hai. Isme discount nahi de sakte.', 'trigger': '', 'keywords': 'discount lower price price kam'}, {'intent': 'Validity', 'reply_en': 'Lifetime — there is no time limit.', 'reply_hi': 'Lifetime hai — koi time limit nahi hai.', 'trigger': '', 'keywords': 'validity lifetime time limit'}, {'intent': 'Updates until Mains 2027', 'reply_en': 'Top Faculty, Pro Pack, and the included Optional are updated till Mains 2027.', 'reply_hi': 'Top Faculty, Pro Pack aur included Optional Mains 2027 tak update hote rahenge.', 'trigger': '', 'keywords': 'updates mains 2027'}, {'intent': 'Everything mentioned is included', 'reply_en': 'Yes, absolutely. Everything mentioned in the list is included.', 'reply_hi': 'Haan bro, bilkul. List mein jo bhi mentioned hai, sab included hai.', 'trigger': '', 'keywords': 'everything mentioned included list'}, {'intent': 'Copyright concern', 'reply_en': 'There are no copyright issues; everything is safe from copyright.', 'reply_hi': 'Copyright ka koi issue nahi hai; sab copyright se safe hai.', 'trigger': '', 'keywords': 'copyright safe issue'}, {'intent': 'Will lectures disappear or be deleted?', 'reply_en': 'No, they are safe for lifetime use with no time limit.', 'reply_hi': 'Nahi bro, safe hain; lifetime use hai, koi time limit nahi hai.', 'trigger': '', 'keywords': 'deleted disappear removed lectures'}, {'intent': 'Trust or scam concern', 'reply_en': "Yes bro, don't worry. You will get access. We are genuine.", 'reply_hi': 'Haan bro, tension mat lo. Aapko access mil jayega. Hum genuine hain.', 'trigger': '', 'keywords': 'trust scam access'}, {'intent': 'Are the lectures recorded?', 'reply_en': 'Mostly recorded, with a few running/live lectures.', 'reply_hi': 'Mostly recorded hain, aur kuch running/live lectures bhi hain.', 'trigger': '', 'keywords': 'recorded lecture live running'}, {'intent': 'Latest or old batch', 'reply_en': 'Please refer to the mentioned list/image and the demo for the year.', 'reply_hi': '', 'trigger': '/demoall', 'keywords': 'latest old batch year'}, {'intent': 'Which year is the course?', 'reply_en': 'Please refer to the mentioned list/image and the demo for the year.', 'reply_hi': '', 'trigger': '/demoall', 'keywords': 'which year course'}, {'intent': 'Show demo', 'reply_en': '', 'reply_hi': '', 'trigger': '/demoall', 'keywords': 'demo sample preview'}, {'intent': 'Does it include GS?', 'reply_en': 'Yes, GS is included in both Top Faculty and Pro Pack.', 'reply_hi': 'Haan bro, GS Top Faculty aur Pro Pack dono mein included hai.', 'trigger': '', 'keywords': 'gs included'}, {'intent': 'One Telegram ID access', 'reply_en': 'Access is provided on one Telegram ID only.', 'reply_hi': 'Access sirf ek Telegram ID par diya jata hai.', 'trigger': '', 'keywords': 'one telegram id'}, {'intent': 'Can I use it on laptop?', 'reply_en': 'Yes, you can use the same Telegram ID on a laptop.', 'reply_hi': 'Haan bro, same Telegram ID laptop par bhi use kar sakte ho.', 'trigger': '', 'keywords': 'laptop telegram id'}, {'intent': 'Use multiple Telegram IDs?', 'reply_en': 'No, access is provided on one Telegram ID only.', 'reply_hi': 'Nahi bro, access sirf ek Telegram ID par diya jata hai.', 'trigger': '', 'keywords': 'multiple telegram ids'}, {'intent': 'Do I need Telegram Premium?', 'reply_en': 'Telegram Premium may be needed if Telegram folder/access limits apply.', 'reply_hi': '', 'trigger': '/premium', 'keywords': 'telegram premium needed'}, {'intent': 'Do I get private group access?', 'reply_en': 'Yes, access is provided through the private premium groups.', 'reply_hi': 'Haan bro, private premium groups ke through access milta hai.', 'trigger': '', 'keywords': 'private group access'}, {'intent': 'Can I get access after payment?', 'reply_en': 'Yes, you will get access after payment.', 'reply_hi': 'Haan bro, payment ke baad access mil jayega.', 'trigger': '', 'keywords': 'access after payment'}, {'intent': 'Is there a time limit?', 'reply_en': 'No, it is lifetime access with no time limit.', 'reply_hi': 'Nahi bro, lifetime access hai, koi time limit nahi hai.', 'trigger': '', 'keywords': 'time limit lifetime'}, {'intent': 'Is Top Faculty a single group?', 'reply_en': 'Top Faculty is one group with multiple topics, with lectures arranged topic-wise.', 'reply_hi': 'Top Faculty ek group hai jisme multiple topics hain aur lectures topic-wise arranged hain.', 'trigger': '', 'keywords': 'top faculty one group topics'}, {'intent': 'How are Top Faculty lectures arranged?', 'reply_en': 'Top Faculty lectures are arranged by topics within the Top Faculty group.', 'reply_hi': 'Top Faculty group ke andar lectures topics ke according arranged hain.', 'trigger': '', 'keywords': 'top faculty arranged topics'}, {'intent': 'What is included besides Top Faculty in Pro Pack?', 'reply_en': 'Pro Pack also includes All Coaching GS and one Optional Combo.', 'reply_hi': 'Pro Pack mein Top Faculty ke saath All Coaching GS aur ek Optional Combo bhi included hai.', 'trigger': '', 'keywords': 'besides top faculty pro pack all coaching optional'}, {'intent': 'Is All Coaching GS included?', 'reply_en': 'Yes, All Coaching GS is included in Pro Pack.', 'reply_hi': 'Haan bro, All Coaching GS Pro Pack mein included hai.', 'trigger': '', 'keywords': 'all coaching gs included'}, {'intent': 'Can I select any one Optional?', 'reply_en': 'Yes, one Optional subject is included in Pro Pack.', 'reply_hi': 'Haan bro, Pro Pack mein ek Optional subject included hai.', 'trigger': '', 'keywords': 'select one optional'}, {'intent': 'Can I buy another Optional?', 'reply_en': 'Yes, an additional Optional is available for ₹500.', 'reply_hi': 'Haan bro, additional Optional ₹500 ka available hai.', 'trigger': '', 'keywords': 'another optional'}, {'intent': 'Do both combos have GS?', 'reply_en': 'Yes, GS is included in both Top Faculty and Pro Pack.', 'reply_hi': 'Haan bro, GS dono Top Faculty aur Pro Pack mein included hai.', 'trigger': '', 'keywords': 'both combos gs'}, {'intent': 'Can I see the list of included content?', 'reply_en': 'Yes, the included content is shown in the list and demo.', 'reply_hi': 'Haan bro, included content list aur demo mein shown hai.', 'trigger': '/demoall', 'keywords': 'list included content'}, {'intent': 'Can I get a sample lecture?', 'reply_en': '', 'reply_hi': '', 'trigger': '/demoall', 'keywords': 'sample lecture'}, {'intent': 'I want Top Faculty', 'reply_en': '', 'reply_hi': '', 'trigger': '/topfaculty', 'keywords': 'want top faculty faculty only'}, {'intent': 'I want Pro Pack', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'want pro pack pro combo'}, {'intent': 'I want All Combo', 'reply_en': '', 'reply_hi': '', 'trigger': '/allcombo', 'keywords': 'want all combo all combos'}, {'intent': 'Top Faculty batch request', 'reply_en': '', 'reply_hi': '', 'trigger': '/topfaculty', 'keywords': 'top faculty batch course chahiye'}, {'intent': 'Pro Pack batch request', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'pro pack batch course chahiye'}, {'intent': 'All Combo request', 'reply_en': '', 'reply_hi': '', 'trigger': '/allcombo', 'keywords': 'all combo request'}, {'intent': 'Generic combo price question', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'combo price generic'}, {'intent': 'Generic combo details question', 'reply_en': '', 'reply_hi': '', 'trigger': '/propack', 'keywords': 'combo details generic'}, {'intent': 'Can I get Top Faculty PRE-access?', 'reply_en': 'If you want, we can provide PRE-access for five minutes to Top Faculty.', 'reply_hi': 'Agar aap chaho toh Top Faculty ka five-minute PRE-access de sakte hain.', 'trigger': '', 'keywords': 'top faculty pre access'}, {'intent': 'PRE-access duration', 'reply_en': 'PRE-access is available for five minutes to Top Faculty.', 'reply_hi': 'Top Faculty ka PRE-access five minutes ka available hai.', 'trigger': '', 'keywords': 'pre access five minutes duration'}, {'intent': 'Why should I choose Pro Pack?', 'reply_en': 'Pro Pack includes Top Faculty + All Coaching GS + one Optional Combo.', 'reply_hi': 'Pro Pack mein Top Faculty + All Coaching GS + ek Optional Combo included hai.', 'trigger': '', 'keywords': 'why choose pro pack'}, {'intent': 'Why should I choose Top Faculty?', 'reply_en': 'Top Faculty gives the lectures listed in the Top Faculty list.', 'reply_hi': 'Top Faculty mein Top Faculty list mein mentioned lectures milte hain.', 'trigger': '', 'keywords': 'why choose top faculty'}, {'intent': 'Is the price negotiable?', 'reply_en': 'The price is fixed.', 'reply_hi': 'Price fixed hai.', 'trigger': '', 'keywords': 'negotiable price'}]


def _norm(s):
    s = str(s or "").casefold().replace("_", " ")
    s = re.sub(r"[^\w\s₹]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()

def _tokens(s):
    return {x for x in _norm(s).split() if len(x) >= 2}

async def ensure_combo_rules(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS rule_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS combo_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL,
            intent TEXT NOT NULL,
            reply_en TEXT NOT NULL DEFAULT '',
            reply_hi TEXT NOT NULL DEFAULT '',
            trigger TEXT NOT NULL DEFAULT '',
            keywords TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(group_id, sort_order)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS combo_conversation_context (
            chat_id TEXT PRIMARY KEY,
            active_group TEXT,
            active_combo TEXT,
            last_rule_id INTEGER,
            last_customer_message TEXT,
            last_bot_question TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        # Remove the old single JSON rule so it cannot be used as a second rules source.
        await db.execute("DELETE FROM combo_sales_rules WHERE name='Combo Sales Rule'")
        await db.execute("INSERT OR IGNORE INTO rule_groups(name,slug,enabled) VALUES(?,?,1)", (GROUP_NAME,GROUP_SLUG))
        row = await (await db.execute("SELECT id FROM rule_groups WHERE slug=?", (GROUP_SLUG,))).fetchone()
        gid = int(row[0])
        count = await (await db.execute("SELECT COUNT(*) FROM combo_rules WHERE group_id=?", (gid,))).fetchone()
        if int(count[0]) == 0:
            for i, r in enumerate(SEED_RULES, 1):
                await db.execute("INSERT INTO combo_rules(group_id,sort_order,intent,reply_en,reply_hi,trigger,keywords,enabled) VALUES(?,?,?,?,?,?,?,1)",
                                 (gid,i,r['intent'],r['reply_en'],r['reply_hi'],r['trigger'],r['keywords']))
        await db.commit()

async def group_row(path, slug=GROUP_SLUG):
    await ensure_combo_rules(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        return await (await db.execute("SELECT * FROM rule_groups WHERE slug=?", (slug,))).fetchone()

async def rules_rows(path, slug=GROUP_SLUG, enabled_only=False):
    g = await group_row(path, slug)
    if not g: return []
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        sql="SELECT * FROM combo_rules WHERE group_id=?"
        if enabled_only: sql += " AND enabled=1"
        sql += " ORDER BY sort_order,id"
        return [dict(r) for r in await (await db.execute(sql,(g['id'],))).fetchall()]

async def _renumber(path, gid):
    async with aiosqlite.connect(path) as db:
        rows=await (await db.execute("SELECT id FROM combo_rules WHERE group_id=? ORDER BY sort_order,id",(gid,))).fetchall()
        for i,(rid,) in enumerate(rows,1):
            await db.execute("UPDATE combo_rules SET sort_order=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(i,rid))
        await db.commit()

async def add_rule(path, intent, reply_en='', reply_hi='', trigger='', keywords=''):
    g=await group_row(path); gid=int(g['id'])
    async with aiosqlite.connect(path) as db:
        n=await (await db.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM combo_rules WHERE group_id=?",(gid,))).fetchone()
        cur=await db.execute("INSERT INTO combo_rules(group_id,sort_order,intent,reply_en,reply_hi,trigger,keywords,enabled) VALUES(?,?,?,?,?,?,?,1)",(gid,int(n[0]),intent,reply_en,reply_hi,trigger,keywords))
        rid=cur.lastrowid; await db.commit()
    return int(rid)

async def update_rule(path,rid,**changes):
    allowed={'intent','reply_en','reply_hi','trigger','keywords','enabled'}
    changes={k:v for k,v in changes.items() if k in allowed}
    if not changes:return
    sets=','.join(f'{k}=?' for k in changes)
    vals=list(changes.values())+[rid]
    async with aiosqlite.connect(path) as db:
        await db.execute(f"UPDATE combo_rules SET {sets},updated_at=CURRENT_TIMESTAMP WHERE id=?",vals); await db.commit()

def _esc(x): return html.escape(str(x or ''))

def _page_buttons(page, pages):
    b=[]
    if page>1:b.append(InlineKeyboardButton('◀️ Prev',callback_data=f'admin:rules:combo:list:{page-1}'))
    if page<pages:b.append(InlineKeyboardButton('Next ▶️',callback_data=f'admin:rules:combo:list:{page+1}'))
    return [b] if b else []

async def show_groups(q,path):
    await ensure_combo_rules(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        groups=[dict(r) for r in await (await db.execute("SELECT * FROM rule_groups ORDER BY id")).fetchall()]
    rows=[]
    for g in groups:
        n=await (await aiosqlite.connect(path)).execute("SELECT 1") if False else None
        async with aiosqlite.connect(path) as db:
            cnt=await (await db.execute("SELECT COUNT(*) FROM combo_rules WHERE group_id=?",(g['id'],))).fetchone()
        rows.append(InlineKeyboardButton(f"{'🟢' if g['enabled'] else '🔴'} {g['name']} ({cnt[0]})",callback_data=f"admin:rules:group:{g['slug']}"))
    kb=[[x] for x in rows]
    kb.append([InlineKeyboardButton('➕ Add Rule Group',callback_data='admin:rules:groupadd')])
    kb.append([InlineKeyboardButton('⬅️ Admin Home',callback_data='admin:home')])
    await q.edit_message_text('⚙️ <b>Rules</b>\n\nSelect a rule group:',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def show_rule_list(q,path,slug=GROUP_SLUG,page=1):
    g=await group_row(path,slug); rows=await rules_rows(path,slug)
    if not g:return await show_groups(q,path)
    per=5; pages=max(1,(len(rows)+per-1)//per); page=max(1,min(page,pages)); chunk=rows[(page-1)*per:page*per]
    lines=[f"⚙️ <b>{_esc(g['name'])}</b>  {'🟢 ON' if g['enabled'] else '🔴 OFF'}",f"Page <b>{page}/{pages}</b> • {len(rows)} rules","",'<b>No. | Intent | English | Hinglish | Trigger | Status</b>']
    for r in chunk:
        lines.append(f"<b>{r['sort_order']}.</b> <b>{_esc(r['intent'])}</b>\nEN: {_esc(r['reply_en']) or '—'}\nHI: {_esc(r['reply_hi']) or '—'}\nTrigger: <code>{_esc(r['trigger']) or '—'}</code> • {'🟢 ON' if r['enabled'] else '🔴 OFF'}")
    kb=[]
    for r in chunk: kb.append([InlineKeyboardButton(f"{'🟢' if r['enabled'] else '🔴'} Edit #{r['sort_order']}",callback_data=f"admin:rules:edit:{r['id']}"),InlineKeyboardButton('Toggle',callback_data=f"admin:rules:toggle:{r['id']}"),InlineKeyboardButton('🗑️',callback_data=f"admin:rules:delete:{r['id']}")])
    kb += _page_buttons(page,pages)
    kb.append([InlineKeyboardButton('➕ Add Rule',callback_data=f'admin:rules:add:{slug}'),InlineKeyboardButton(('🔴 Turn Group OFF' if g['enabled'] else '🟢 Turn Group ON'),callback_data=f'admin:rules:grouptoggle:{slug}')])
    kb.append([InlineKeyboardButton('⬅️ Rule Groups',callback_data='admin:rules')])
    await q.edit_message_text('\n'.join(lines),parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def show_edit(q,path,rid):
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row; r=await (await db.execute("SELECT r.*,g.slug,g.name FROM combo_rules r JOIN rule_groups g ON g.id=r.group_id WHERE r.id=?",(rid,))).fetchone()
    if not r:return await show_rule_list(q,path)
    text=(f"✏️ <b>Rule #{r['sort_order']}</b>\n\n<b>Intent:</b> {_esc(r['intent'])}\n<b>English:</b> {_esc(r['reply_en']) or '—'}\n<b>Hinglish:</b> {_esc(r['reply_hi']) or '—'}\n<b>Trigger:</b> <code>{_esc(r['trigger']) or '—'}</code>\n<b>Keywords:</b> {_esc(r['keywords']) or '—'}\n<b>Status:</b> {'🟢 ON' if r['enabled'] else '🔴 OFF'}")
    kb=[[InlineKeyboardButton('✏️ Edit Rule',callback_data=f'admin:rules:editstart:{rid}')],[InlineKeyboardButton('Toggle',callback_data=f'admin:rules:toggle:{rid}'),InlineKeyboardButton('🗑️ Delete',callback_data=f'admin:rules:delete:{rid}')],[InlineKeyboardButton('⬅️ Rules',callback_data=f"admin:rules:group:{r['slug']}")]]
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def begin_add(update_or_q,context,path,slug=GROUP_SLUG):
    context.user_data['combo_rule_add']={'slug':slug,'step':'all'}
    q=getattr(update_or_q,'callback_query',None)
    text=('➕ <b>Add Rule</b>\n\nSend one line in this format:\n\n<code>Intent | English reply | Hinglish reply | Trigger | keywords</code>\n\nFor no reply use empty field. For no trigger use empty field.\nExample:\n<code>Customer asks price |  |  | /propack | price kitna</code>')
    if q: await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:rules:group:{slug}')]]))
    else: await update_or_q.message.reply_text(text,parse_mode='HTML')

async def begin_edit(q,context,rid):
    context.user_data['combo_rule_edit']=rid
    await q.edit_message_text('✏️ <b>Edit Rule</b>\n\nSend:\n<code>Intent | English reply | Hinglish reply | Trigger | keywords</code>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel',callback_data=f'admin:rules:edit:{rid}')]]))

async def handle_admin_message(update,context,path):
    state=context.user_data.get('combo_rule_add')
    if state and update.message and update.message.text:
        raw=update.message.text.strip(); parts=[x.strip() for x in raw.split('|')]
        if len(parts)!=5:
            await update.message.reply_text('Use exactly 5 fields separated by |.'); return True
        await add_rule(path,*parts); context.user_data.pop('combo_rule_add',None)
        await update.message.reply_text('✅ Rule added successfully.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Combo Rules',callback_data='admin:rules:group:combo')]])); return True
    rid=context.user_data.get('combo_rule_edit')
    if rid and update.message and update.message.text:
        parts=[x.strip() for x in update.message.text.strip().split('|')]
        if len(parts)!=5: await update.message.reply_text('Use exactly 5 fields separated by |.'); return True
        await update_rule(path,int(rid),intent=parts[0],reply_en=parts[1],reply_hi=parts[2],trigger=parts[3],keywords=parts[4]); context.user_data.pop('combo_rule_edit',None)
        await update.message.reply_text('✅ Rule updated.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Combo Rules',callback_data='admin:rules:group:combo')]])); return True
    return False

async def toggle_rule(path,rid):
    async with aiosqlite.connect(path) as db:
        row=await (await db.execute('SELECT enabled FROM combo_rules WHERE id=?',(rid,))).fetchone()
        if row: await db.execute('UPDATE combo_rules SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(0 if row[0] else 1,rid)); await db.commit()

async def delete_rule(path,rid):
    async with aiosqlite.connect(path) as db:
        row=await (await db.execute('SELECT group_id FROM combo_rules WHERE id=?',(rid,))).fetchone()
        if not row:return
        await db.execute('DELETE FROM combo_rules WHERE id=?',(rid,)); await db.commit()
        gid=row[0]
    await _renumber(path,gid)

async def toggle_group(path,slug):
    g=await group_row(path,slug)
    if not g:return
    async with aiosqlite.connect(path) as db:
        await db.execute('UPDATE rule_groups SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(0 if g['enabled'] else 1,g['id'])); await db.commit()

async def context_get(path,chat_id):
    await ensure_combo_rules(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        return await (await db.execute('SELECT * FROM combo_conversation_context WHERE chat_id=?',(str(chat_id),))).fetchone()

async def context_set(path,chat_id,**changes):
    old=await context_get(path,chat_id); data=dict(old) if old else {'chat_id':str(chat_id)}; data.update(changes)
    cols=['chat_id','active_group','active_combo','last_rule_id','last_customer_message','last_bot_question']
    vals=[data.get(c) for c in cols]
    async with aiosqlite.connect(path) as db:
        await db.execute('INSERT INTO combo_conversation_context(chat_id,active_group,active_combo,last_rule_id,last_customer_message,last_bot_question,updated_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(chat_id) DO UPDATE SET active_group=excluded.active_group,active_combo=excluded.active_combo,last_rule_id=excluded.last_rule_id,last_customer_message=excluded.last_customer_message,last_bot_question=excluded.last_bot_question,updated_at=CURRENT_TIMESTAMP',vals); await db.commit()

async def context_clear(path,chat_id):
    async with aiosqlite.connect(path) as db:
        await db.execute('DELETE FROM combo_conversation_context WHERE chat_id=?',(str(chat_id),)); await db.commit()

def detect_combo(message):
    n=_norm(message)
    # Explicit non-combo product/batch signals invalidate Combo Rules.
    other_subjects=('anthropology','psir','sociology','geography optional','philosophy','psychology','history optional','public administration','agriculture optional')
    if any(x in n for x in other_subjects): return None
    if 'pro pack' in n or 'propack' in n or 'pro combo' in n: return 'Pro Pack'
    if 'top faculty' in n or 'topfaculty' in n or 'faculty combo' in n: return 'Top Faculty'
    if 'all combo' in n or 'allcombo' in n or 'all combos' in n: return 'All Combo'
    # Approved generic "combo" sales wording maps to Pro Pack.
    if any(x in n for x in ('interested in your combo','combo price','combo details','combo offer')): return 'Pro Pack'
    return None

def looks_like_other_batch(message):
    n=_norm(message)
    if any(x in n for x in ('anthropology','psir','sociology','philosophy','psychology','public administration','agriculture')): return True
    return bool(re.search(r'\b(batch|course|classes|lecture|lectures)\b',n) and not any(x in n for x in ('top faculty','topfaculty','pro pack','propack','all combo','allcombo','combo')))

async def eligible_context(path,chat_id,message):
    ctx=await context_get(path,chat_id); explicit=detect_combo(message)
    if explicit:
        await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo=explicit,last_customer_message=message)
        return explicit,True
    if looks_like_other_batch(message):
        await context_clear(path,chat_id); return None,False
    if ctx and ctx['active_group']==GROUP_SLUG and ctx['active_combo']:
        return str(ctx['active_combo']),True
    return None,False

async def relevant_rules(path,chat_id,message,limit=8):
    combo,eligible=await eligible_context(path,chat_id,message)
    if not eligible:return None,[]
    g=await group_row(path); 
    if not g or not g['enabled']:return combo,[]
    rows=await rules_rows(path,enabled_only=True); ctx=await context_get(path,chat_id)
    q=_tokens(message); scored=[]
    for r in rows:
        hay=_tokens((r['intent']+' '+r['keywords']))
        hit=len(q&hay)
        # Keep short confirmations/questions tied to the previous rule.
        bonus=10 if ctx and ctx['last_rule_id']==r['id'] and len(q)<=2 else 0
        combo_tokens=_tokens(combo)
        combo_hit=len(q&combo_tokens)
        score=hit*4+bonus+combo_hit
        if score>0: scored.append((score,r))
    if not scored:
        # For an explicit combo selection, use only a tiny product-relevant slice.
        for r in rows:
            if _tokens(combo)&_tokens(r['intent']+' '+r['keywords']): scored.append((1,r))
    scored.sort(key=lambda x:(-x[0],x[1]['sort_order']))
    return combo,[r for _,r in scored[:limit]]

async def process_customer(update,context,path,ai):
    if not update.message or not update.message.text or not update.effective_chat:return False
    message=update.message.text.strip(); combo,rs=await relevant_rules(path,update.effective_chat.id,message)
    if not combo or not rs:return False
    # This is the strict token gate: only filtered rules enter the AI prompt.
    ctx=await context_get(path,update.effective_chat.id)
    hist=[]
    if ctx:
        if ctx['last_customer_message']: hist.append('Previous customer message: '+str(ctx['last_customer_message']))
        if ctx['last_bot_question']: hist.append('Previous bot question: '+str(ctx['last_bot_question']))
    lines=[f'COMBO RULE GATE: ELIGIBLE',f'ACTIVE COMBO: {combo}', 'ONLY USE THE FOLLOWING ACTIVE COMBO RULES:',]
    for r in rs:
        lines.append(f"RULE_ID={r['id']} | INTENT={r['intent']} | ENGLISH={r['reply_en']} | HINGLISH={r['reply_hi']} | TRIGGER={r['trigger']}")
    if hist: lines.append('\nCONVERSATION CONTEXT:\n'+'\n'.join(hist))
    lines.append('\nDo not use any other Combo Rule. Do not invent facts. Return the exact configured intent when a listed rule matches.')
    try: result=await ai.understand_and_reply(message,'\n'.join(lines))
    except Exception:return False
    intents=result.get('intents') or []; names={str(i.get('name')).casefold() for i in intents if isinstance(i,dict)}
    chosen=None
    for r in rs:
        if r['intent'].casefold() in names: chosen=r; break
    if not chosen:
        # Conservative fallback only when one filtered rule is clearly strongest.
        if len(rs)==1: chosen=rs[0]
        else:
            q=_tokens(message); ranked=sorted(rs,key=lambda r:len(q&_tokens(r['intent']+' '+r['keywords'])),reverse=True)
            if ranked and len(q&_tokens(ranked[0]['intent']+' '+ranked[0]['keywords']))>=2: chosen=ranked[0]
    if not chosen:return False
    await context_set(path,update.effective_chat.id,active_group=GROUP_SLUG,active_combo=combo,last_rule_id=int(chosen['id']),last_customer_message=message,last_bot_question='')
    trigger=str(chosen['trigger'] or '').strip()
    if trigger:
        if not trigger.startswith('/'):trigger='/'+trigger.lstrip('/')
        ok=await getattr(getattr(context, 'application', None), 'bot_data', {}).get('combo_rules_execute_command')(update,context,trigger)
        if ok:return True
    lang=str(result.get('language') or '').casefold(); reply=str(chosen['reply_hi'] if ('hindi' in lang or 'hinglish' in lang or 'hi'==lang) and chosen['reply_hi'] else chosen['reply_en'] or chosen['reply_hi'] or '')
    if reply:
        await update.message.reply_text(reply)
        await context_set(path,update.effective_chat.id,last_bot_question=reply if '?' in reply else '')
        return True
    return False

# Backward-compatible adapter for old phase30 code.
async def legacy_rule_row(path):
    g=await group_row(path); return {'id':g['id'],'name':GROUP_NAME,'enabled':g['enabled'],'rule_json':json.dumps({'generic_combo_command':'/allcombo','top_faculty_command':'/topfaculty','pro_pack_command':'/propack'},ensure_ascii=False)} if g else None


# COMBO_RULES_ADDON_V3
# Conversation-aware add-on. This layer is intentionally isolated to Combo Rules.
# It does not alter Main Account sessions or non-Combo routing tables.

V3_RULES = [
    {'intent':'Generic combo interest','reply_en':'','reply_hi':'','trigger':'/allcombo','keywords':'i want your combo want combo interested in your combo combo offer combo price combo details generic combo'},
    {'intent':'Generic combo price question','reply_en':'','reply_hi':'','trigger':'/allcombo','keywords':'combo price generic price combo kitna price batao'},
    {'intent':'Generic combo details question','reply_en':'','reply_hi':'','trigger':'/allcombo','keywords':'combo details generic details combo kya provide'},
    {'intent':'Multiple optional subjects mentioned before combo selection','reply_en':'Which combo would you like — Pro Pack or Top Faculty?','reply_hi':'Kaunsa combo chahiye — Pro Pack ya Top Faculty?','trigger':'','keywords':'psir sociology both included mil jayega isme dono optional combo'},
    {'intent':'Optional inclusion without combo selection','reply_en':'Which combo are you taking — Pro Pack or Top Faculty?','reply_hi':'Aap kaunsa combo le rahe ho — Pro Pack ya Top Faculty?','trigger':'','keywords':'optional included is optional included in it which combo pack'},
    {'intent':'Optional included in Pro Pack','reply_en':'','reply_hi':'','trigger':'','keywords':'optional included pro pack'},
    {'intent':'Optional not included in Top Faculty','reply_en':'Optional is not included in Top Faculty.','reply_hi':'Top Faculty mein Optional included nahi hai.','trigger':'','keywords':'top faculty optional included hai optional nahi hai'},
    {'intent':'One Optional included','reply_en':'One optional subject is included in Pro Pack.','reply_hi':'Pro Pack mein ek Optional subject included hai.','trigger':'','keywords':'one optional included pro pack'},
    {'intent':'Which Optional subject','reply_en':"What's your optional subject?",'reply_hi':'Aapka optional subject kya hai?','trigger':'','keywords':'which optional what optional subject'},
    {'intent':'Optional not selected yet','reply_en':'Okay bro, you can choose your optional anytime. Whenever you choose, we will provide the link.','reply_hi':'Okay bro, aap optional kabhi bhi choose kar sakte ho. Jab choose karoge, hum link provide kar denge.','trigger':'','keywords':'optional not selected nahi select kiya baad mein kar sakta later choose later select'},
    {'intent':'Combo purchase confirmation','reply_en':'Are you paying now?','reply_hi':'Aap abhi payment kar rahe ho?','trigger':'','keywords':'paying now purchase selected combo buy now payment now'},
    {'intent':'First negotiation','reply_en':"Bro, this is a premium course. We can't discount it.",'reply_hi':'Bro, ye premium course hai. Isme discount nahi de sakte.','trigger':'','keywords':'discount lower price price kam discount please first discount'},
    {'intent':'Second negotiation','reply_en':'No bro, our price is already lowest. Price is half now, so no more discount possible.','reply_hi':'Nahi bro, hamara price already lowest hai. Ab price half hai, isliye aur discount possible nahi hai.','trigger':'','keywords':'discount again little discount more discount please again lower price kam karo second discount'},
    {'intent':'Third negotiation Pro Pack or All Combo','reply_en':'Bro, my profit is just ₹500.','reply_hi':'Bro, mera profit sirf ₹500 hai.','trigger':'','keywords':'discount again third time final discount profit 500 pro pack all combo'},
    {'intent':'Third negotiation Top Faculty','reply_en':'Bro, my profit is just ₹250.','reply_hi':'Bro, mera profit sirf ₹250 hai.','trigger':'','keywords':'discount again third time final discount profit 250 top faculty'},
    {'intent':'Updates until Mains 2027','reply_en':'Top Faculty, Pro Pack, and the included Optional are updated till Mains 2027.','reply_hi':'Top Faculty, Pro Pack aur included Optional Mains 2027 tak update hote rahenge.','trigger':'','keywords':'updates till mains 2027 update kab tak update duration updates one year'},
]

V3_REPLACEMENT_ALIASES = {
    'Generic combo interest': ['Generic combo interest'],
    'Generic combo price question': ['Generic combo price question'],
    'Generic combo details question': ['Generic combo details question'],
    'Optional not included in Top Faculty': ['Optional not included in Top Faculty'],
    'One Optional included': ['One Optional included','Can I select any one Optional?'],
    'Optional included in Pro Pack': ['Optional included in Pro Pack'],
    'Customer selects an Optional': ['Customer selects an Optional'],
    'Optional not selected yet': ['Can I choose Optional later?'],
    'Combo purchase confirmation': ['Purchase or payment intent'],
    'First negotiation': ['Discount request'],
    'Updates until Mains 2027': ['Updates until Mains 2027'],
}

async def _v3_delete_conflicts_and_upsert(path, gid):
    # Remove older rows when this add-on defines the same intent with a changed
    # reply/trigger. This is deterministic; AI never decides which DB rows to delete.
    async with aiosqlite.connect(path) as db:
        for nr in V3_RULES:
            aliases=V3_REPLACEMENT_ALIASES.get(nr['intent'],[nr['intent']])
            for old in aliases:
                await db.execute('DELETE FROM combo_rules WHERE group_id=? AND lower(intent)=lower(?)',(gid,old))
        # Reinsert canonical new rules at the end. Avoid duplicates on restart.
        for nr in V3_RULES:
            row=await (await db.execute('SELECT id FROM combo_rules WHERE group_id=? AND lower(intent)=lower(?)',(gid,nr['intent']))).fetchone()
            if not row:
                mx=await (await db.execute('SELECT COALESCE(MAX(sort_order),0) FROM combo_rules WHERE group_id=?',(gid,))).fetchone()
                await db.execute('INSERT INTO combo_rules(group_id,sort_order,intent,reply_en,reply_hi,trigger,keywords,enabled) VALUES(?,?,?,?,?,?,?,1)',(gid,int(mx[0])+1,nr['intent'],nr['reply_en'],nr['reply_hi'],nr['trigger'],nr['keywords']))
        await db.commit()
    await _renumber(path,gid)

_old_ensure_combo_rules = ensure_combo_rules

async def ensure_combo_rules(path):
    await _old_ensure_combo_rules(path)
    async with aiosqlite.connect(path) as db:
        # Add-on state is kept entirely inside Combo Rules context.
        cols=[r[1] for r in await (await db.execute('PRAGMA table_info(combo_conversation_context)')).fetchall()]
        if 'combo_stage' not in cols:
            await db.execute('ALTER TABLE combo_conversation_context ADD COLUMN combo_stage TEXT DEFAULT \'\'')
        if 'negotiation_count' not in cols:
            await db.execute('ALTER TABLE combo_conversation_context ADD COLUMN negotiation_count INTEGER NOT NULL DEFAULT 0')
        await db.commit()
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        g=await (await db.execute('SELECT * FROM rule_groups WHERE slug=?',(GROUP_SLUG,))).fetchone()
    if g:
        await _v3_delete_conflicts_and_upsert(path,int(g['id']))



async def context_set(path,chat_id,**changes):
    old=await context_get(path,chat_id) if False else None
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        old=await (await db.execute('SELECT * FROM combo_conversation_context WHERE chat_id=?',(str(chat_id),))).fetchone()
        data=dict(old) if old else {'chat_id':str(chat_id)}
        data.update(changes)
        cols=['chat_id','active_group','active_combo','last_rule_id','last_customer_message','last_bot_question','combo_stage','negotiation_count']
        vals=[data.get(c) for c in cols]
        await db.execute('INSERT INTO combo_conversation_context(chat_id,active_group,active_combo,last_rule_id,last_customer_message,last_bot_question,combo_stage,negotiation_count,updated_at) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(chat_id) DO UPDATE SET active_group=excluded.active_group,active_combo=excluded.active_combo,last_rule_id=excluded.last_rule_id,last_customer_message=excluded.last_customer_message,last_bot_question=excluded.last_bot_question,combo_stage=excluded.combo_stage,negotiation_count=excluded.negotiation_count,updated_at=CURRENT_TIMESTAMP',vals)
        await db.commit()

def _v3_hinglish(text):
    n=_norm(text)
    return bool(re.search(r'\b(haan|han|bhai|bhaiya|aap|apka|mujhe|chahiye|isme|kya|hai|hain|kar|do|de|mil|nahi|nahin|baad|abhi|mera|meri|kaunsa|konsa|kitna|kab|tak)\b',n))

def _v3_yes(text):
    n=_norm(text)
    return bool(re.search(r'\b(yes|yeah|yep|haan|han|ha|ji|bilkul|sure|please|do it|de do|kar do|bhej do|send it|send kar do)\b',n))

def _v3_no(text):
    n=_norm(text)
    return bool(re.search(r'\b(no|nah|nahi|nahin|not now|later|baad me|check|checking|soch|think)\b',n))

def _v3_combo_explicit(text):
    n=_norm(text)
    if re.search(r'\b(pro pack|propack|pro combo)\b',n): return 'Pro Pack'
    if re.search(r'\b(top faculty|topfaculty|faculty combo)\b',n): return 'Top Faculty'
    if re.search(r'\b(all combo|allcombo|all combos)\b',n): return 'All Combo'
    return None

def _v3_generic_combo(text):
    n=_norm(text)
    return bool(re.search(r'\b(i want|want|need|interested|chahiye|chahta|chahie|combo|package|packages)\b',n) and re.search(r'\b(combo|package|offer|details|price)\b',n))

def _v3_subjects(text):
    n=_norm(text)
    subjects=['sociology','psir','anthropology','geography','philosophy','psychology','public administration','agriculture','history','economy']
    return [s for s in subjects if re.search(r'(?<!\w)'+re.escape(s)+r'(?!\w)',n)]

def _v3_inclusion_question(text):
    n=_norm(text)
    return bool(re.search(r'\b(mil jayega|milega|included|include|isme|is me|dono|both)\b',n) and _v3_subjects(text))

def _v3_optional_selected(text):
    n=_norm(text)
    subs=_v3_subjects(text)
    return bool(re.search(r'\b(my optional|mera optional|meri optional|optional is|optional hai|optional sociology|optional psir|optional hai)\b',n)) and bool(subs)

def _v3_optional_unselected(text):
    n=_norm(text)
    return bool(re.search(r'(optional.*(nahi|nahin|not).*(select|choose)|abhi.*(nahi|nahin).*optional|optional.*baad mein|optional.*later|not selected.*optional)',n))

def _v3_optional_question(text):
    n=_norm(text)
    return bool(re.search(r'\b(which optional|what optional|kaunsa optional|konsa optional|which opt|kaunsa opt)\b',n))

def _v3_optional_included_question(text):
    n=_norm(text)
    return bool(re.search(r'\b(is optional included|optional included|optional.*included|optional.*isme|optional.*in it)\b',n))

def _v3_payment_now_question(text):
    n=_norm(text)
    return bool(re.search(r'\b(paying now|pay now|payment now|abhi payment|abhi pay|pay karu|payment karu|payment kare|pay kare|buy now)\b',n))

def _v3_negotiation(text):
    n=_norm(text)
    return bool(re.search(r'\b(discount|discount please|little discount|price kam|kam kar|lower price|negotiate|negotiation|thoda kam|aur kam|reduce price|less price|cheap)\b',n))

def _v3_update_question(text):
    n=_norm(text)
    return bool(re.search(r'\b(update|updates|updated)\b',n) and re.search(r'\b(how long|kitne time|kab tak|for how much time|duration|till|until)\b',n))

def _v3_rule_by_intent(rows,intent):
    return next((r for r in rows if str(r['intent']).casefold()==intent.casefold()),None)

async def _v3_rows(path):
    return await rules_rows(path,GROUP_SLUG,enabled_only=True)

async def _v3_execute_trigger(update,context,trigger):
    trigger=str(trigger or '').strip()
    if not trigger:return False
    if not trigger.startswith('/'): trigger='/'+trigger.lstrip('/')

    # Normal PTB updates expose application.bot_data. Main Account bridge
    # intentionally supplies a lightweight SimpleNamespace instead, so the
    # Combo Rules layer must resolve the same command executor without
    # requiring any change to the bridge or session.
    executor=None
    app=getattr(context,'application',None)
    data=getattr(app,'bot_data',None)
    if isinstance(data,dict):
        executor=data.get('combo_rules_execute_command')
    if executor is None:
        data=getattr(context,'bot_data',None)
        if isinstance(data,dict):
            executor=data.get('combo_rules_execute_command')
    if executor is None:
        try:
            from app import bot as bot_module
            executor=getattr(bot_module,'execute_direct_command',None)
        except Exception:
            executor=None
    if executor is None:
        return False

    # Main Account bridge uses a lightweight context without
    # context.application. Reuse the real PTB application context for
    # deterministic command execution while preserving the original
    # customer update/chat.
    if executor.__name__ == 'execute_direct_command':
        real_app = getattr(context, '_real_application', None)
        if real_app is None:
            try:
                from app import bot as bot_module
                real_app = getattr(bot_module, 'application', None)
            except Exception:
                real_app = None

        if real_app is not None:
            proxy_context = SimpleNamespace(
                application=real_app,
                bot=getattr(real_app, 'bot', None),
                bot_data=getattr(real_app, 'bot_data', {}),
                user_data=getattr(context, 'user_data', {}),
                chat_data=getattr(context, 'chat_data', {}),
            )
            return bool(await executor(update, proxy_context, trigger, internal=True))

        # Fallback: expose the existing bot_data through the lightweight
        # context when no application object is available.
        if not hasattr(context, 'application'):
            context.application = SimpleNamespace(
                bot_data=getattr(context, 'bot_data', {})
            )

        return bool(await executor(update, context, trigger, internal=True))

    return bool(await executor(update,context,trigger))

async def _v3_subject_combo(path,subject):
    s=_norm(subject)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        rows=[dict(r) for r in await (await db.execute('SELECT * FROM combos WHERE enabled=1 ORDER BY id DESC')).fetchall()]
        for r in rows:
            hay=_norm(str(r.get('name') or '')+' '+str(r.get('details') or '')+' '+str(r.get('caption') or ''))
            if s in hay and ('combo' in hay or 'pack' in hay or 'optional' in hay):
                cmd=await (await db.execute('SELECT command FROM combo_commands_v9 WHERE combo_id=? AND enabled=1',(int(r['id']),))).fetchone()
                if cmd and str(cmd[0] or '').strip(): return str(cmd[0]).strip()
    return ''

async def process_customer(update,context,path,ai):
    if not update.message or not update.message.text or not update.effective_chat:return False
    message=update.message.text.strip(); chat_id=update.effective_chat.id
    await ensure_combo_rules(path)
    ctx=await context_get(path,chat_id)
    rows=await _v3_rows(path)
    if not rows:return False
    stage=str((ctx['combo_stage'] if ctx and 'combo_stage' in ctx.keys() else '') or '')
    active=str((ctx['active_combo'] if ctx else '') or '')
    explicit=_v3_combo_explicit(message)

    # A generic catalogue request is deterministic: show All Combo only.
    if _v3_generic_combo(message) and not explicit and not _v3_subjects(message):
        await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo='ASKING',last_customer_message=message,last_bot_question='Which combo would you like?',combo_stage='AWAITING_COMBO',negotiation_count=0)
        ok=await _v3_execute_trigger(update,context,'/allcombo')
        if ok:
            await update.message.reply_text('Which combo would you like?')
            return True

    # Explicit selection always wins over subject/batch semantics.
    if explicit:
        await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo=explicit,last_customer_message=message,combo_stage='SELECTED',negotiation_count=(ctx['negotiation_count'] if ctx and 'negotiation_count' in ctx.keys() else 0))
        if explicit=='All Combo':
            ok=await _v3_execute_trigger(update,context,'/allcombo')
            if ok:
                await update.message.reply_text('Which combo would you like?')
            return ok
        cmd='/propack' if explicit=='Pro Pack' else '/topfaculty'
        ok=await _v3_execute_trigger(update,context,cmd)
        if ok:
            await update.message.reply_text('Are you paying now?' if str((ctx['active_combo'] if ctx else '') or '')!=explicit or stage!='SELECTED' else 'Are you paying now?')
            await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo=explicit,last_customer_message=message,last_bot_question='Are you paying now?',combo_stage='AWAITING_PAYMENT',negotiation_count=0)
        return ok

    # Multi-subject inclusion questions are NOT optional selection.
    if _v3_inclusion_question(message) and not active:
        r=_v3_rule_by_intent(rows,'Multiple optional subjects mentioned before combo selection')
        if r:
            await update.message.reply_text(r['reply_hi'] if _v3_hinglish(message) else r['reply_en'])
            await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo='ASKING',last_customer_message=message,last_bot_question=(r['reply_hi'] if _v3_hinglish(message) else r['reply_en']),combo_stage='AWAITING_COMBO',negotiation_count=0)
            return True

    # PRE-access confirmation must consume the previous offer and never repeat it.
    if ctx and stage=='PRE_OFFERED' and _v3_yes(message):
        await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage='PRE_CONFIRMED')
        return await _v3_execute_trigger(update,context,'/pre topfaculty')

    # Optional-not-selected is an explicit statement, not a request for a subject.
    if active=='Pro Pack' and _v3_optional_unselected(message):
        r=_v3_rule_by_intent(rows,'Optional not selected yet')
        if r:
            reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']
            await update.message.reply_text(reply)
            await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage='SELECTED')
            return True

    # Optional selected inside Pro Pack: send only the relevant Optional combo.
    if active=='Pro Pack' and _v3_optional_selected(message):
        subjects=_v3_subjects(message); subject=subjects[0] if subjects else ''
        cmd=await _v3_subject_combo(path,subject)
        if cmd:
            ok=await _v3_execute_trigger(update,context,cmd)
            if ok:
                await update.message.reply_text('Everything mentioned in the list will be included in your Pro Pack.' if not _v3_hinglish(message) else 'List mein jo bhi mentioned hai, sab aapke Pro Pack mein included hoga.')
                await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage='SELECTED')
                return True

    # If customer asks which optional after Pro Pack is known, ask subject.
    if active=='Pro Pack' and _v3_optional_question(message):
        r=_v3_rule_by_intent(rows,'Which Optional subject')
        if r:
            reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']
            await update.message.reply_text(reply)
            await context_set(path,chat_id,last_customer_message=message,last_bot_question=reply,combo_stage='AWAITING_OPTIONAL')
            return True

    # Optional included question: only answer directly when combo is known.
    if _v3_optional_included_question(message):
        if active=='Top Faculty':
            r=_v3_rule_by_intent(rows,'Optional not included in Top Faculty')
            if r:
                reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']; await update.message.reply_text(reply); await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage='SELECTED'); return True
        if active=='Pro Pack':
            r=_v3_rule_by_intent(rows,'One Optional included')
            if r:
                reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']; await update.message.reply_text(reply); await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage='SELECTED'); return True
        r=_v3_rule_by_intent(rows,'Optional inclusion without combo selection')
        if r:
            reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']; await update.message.reply_text(reply); await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo='ASKING',last_customer_message=message,last_bot_question=reply,combo_stage='AWAITING_COMBO'); return True

    # Payment confirmation is stateful: yes -> PhonePe question, not /phonepe yet.
    if ctx and stage=='AWAITING_PAYMENT':
        if _v3_yes(message):
            reply='Do you use PhonePe?' if not _v3_hinglish(message) else 'Aap PhonePe use karte ho?'
            await update.message.reply_text(reply)
            await context_set(path,chat_id,last_customer_message=message,last_bot_question=reply,combo_stage='AWAITING_PHONEPE')
            return True
        # A different query should fall through to normal Combo Rules.

    if ctx and stage=='AWAITING_PHONEPE':
        if _v3_yes(message):
            await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage='PAYMENT_STARTED')
            return await _v3_execute_trigger(update,context,'/phonepe')
        if _v3_no(message):
            r=_v3_rule_by_intent(rows,'Customer cannot use PhonePe')
            if r:
                reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']; await update.message.reply_text(reply); await context_set(path,chat_id,last_customer_message=message,last_bot_question=reply,combo_stage='PHONEPE_UNAVAILABLE'); return True

    # Negotiation stages are stored only in Combo Rules context.
    if active in ('Pro Pack','All Combo','Top Faculty') and _v3_negotiation(message):
        count=int((ctx['negotiation_count'] if ctx and 'negotiation_count' in ctx.keys() and ctx['negotiation_count'] is not None else 0)) + 1
        if count==1: intent='First negotiation'
        elif count==2: intent='Second negotiation'
        else: intent='Third negotiation Top Faculty' if active=='Top Faculty' else 'Third negotiation Pro Pack or All Combo'
        r=_v3_rule_by_intent(rows,intent)
        if r:
            reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']; await update.message.reply_text(reply); await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage='NEGOTIATING',negotiation_count=count); return True

    # Update duration must not be confused with access validity.
    if _v3_update_question(message):
        r=_v3_rule_by_intent(rows,'Updates until Mains 2027')
        if r:
            reply=r['reply_hi'] if _v3_hinglish(message) else r['reply_en']; await update.message.reply_text(reply); await context_set(path,chat_id,last_customer_message=message,last_bot_question='',combo_stage=stage or 'SELECTED'); return True

    # Detect a PRE offer from existing Combo Rule replies and mark it so the next
    # affirmative response triggers /pre topfaculty rather than repeating the offer.
    # This executes after the deterministic branches above.
    combo,rs=await relevant_rules(path,chat_id,message)
    if not combo or not rs:return False
    # Exclude generic command rules from AI ambiguity: generic requests are handled above.
    ctx=await context_get(path,chat_id)
    hist=[]
    if ctx:
        if ctx['last_customer_message']: hist.append('Previous customer message: '+str(ctx['last_customer_message']))
        if ctx['last_bot_question']: hist.append('Previous bot question: '+str(ctx['last_bot_question']))
    lines=[f'COMBO RULE GATE: ELIGIBLE',f'ACTIVE COMBO: {combo}','ONLY USE THE FOLLOWING ACTIVE COMBO RULES:']
    for r in rs: lines.append(f"RULE_ID={r['id']} | INTENT={r['intent']} | ENGLISH={r['reply_en']} | HINGLISH={r['reply_hi']} | TRIGGER={r['trigger']}")
    if hist: lines.append('\nCONVERSATION CONTEXT:\n'+'\n'.join(hist))
    lines.append('\nDo not use any other Combo Rule. Do not invent facts. Return the exact configured intent when a listed rule matches.')
    try: result=await ai.understand_and_reply(message,'\n'.join(lines))
    except Exception:return False
    intents=result.get('intents') or []; names={str(i.get('name')).casefold() for i in intents if isinstance(i,dict)}
    chosen=None
    for r in rs:
        if r['intent'].casefold() in names: chosen=r; break
    if not chosen and len(rs)==1: chosen=rs[0]
    if not chosen:return False
    trigger=str(chosen['trigger'] or '').strip()
    if trigger:
        # PRE offer confirmations are handled deterministically on the next turn.
        if chosen['intent'] in ('Can I get PRE-access before payment?','Add me first then I will pay','Add me first in Pro Pack','Can I get Top Faculty PRE-access?'):
            reply=chosen['reply_hi'] if _v3_hinglish(message) and chosen['reply_hi'] else chosen['reply_en'] or chosen['reply_hi']
            if reply:
                await update.message.reply_text(reply)
                await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo='Top Faculty',last_rule_id=int(chosen['id']),last_customer_message=message,last_bot_question=reply,combo_stage='PRE_OFFERED')
                return True
        ok=await _v3_execute_trigger(update,context,trigger)
        if ok:return True
    lang=str(result.get('language') or '').casefold(); reply=str(chosen['reply_hi'] if ('hindi' in lang or 'hinglish' in lang or lang=='hi') and chosen['reply_hi'] else chosen['reply_en'] or chosen['reply_hi'] or '')
    if reply:
        await update.message.reply_text(reply)
        await context_set(path,chat_id,active_group=GROUP_SLUG,active_combo=combo,last_rule_id=int(chosen['id']),last_customer_message=message,last_bot_question=reply if '?' in reply else '',combo_stage=stage or 'SELECTED')
        return True
    return False


# ==================== V6 STATE + FILTER + TOKEN AUDIT ====================
# Canonical rules are loaded from combo_rules_data.json. The previous Combo Rules
# rows are replaced as a dataset on migration; no duplicate legacy intents remain.
_V6_DATA_PATH = Path(__file__).with_name("combo_rules_data.json")
try:
    with open(_V6_DATA_PATH, "r", encoding="utf-8") as _f:
        V6_RULES = json.load(_f)
except Exception:
    V6_RULES = []

_V6_STATE_ALIASES = {
    'COMBO': ('combo', 'all combo', 'all combos', 'combo'),
    'PRO_PACK': ('pro pack', 'propack', 'pro combo'),
    'TOP_FACULTY': ('top faculty', 'topfaculty', 'faculty combo'),
}

def _v6_state_norm(v):
    return str(v or '').strip().upper()

def _v6_state_from_text(text, current=''):
    n=_norm(text)
    if re.search(r'\b(pro\s*pack|propack|pro\s*combo)\b', n): return 'PRO_PACK'
    if re.search(r'\b(top\s*faculty|topfaculty|faculty\s*combo)\b', n): return 'TOP_FACULTY'
    if re.search(r'\b(all\s*combo|all\s*combos)\b', n): return 'COMBO'
    return _v6_state_norm(current)

def _v6_rule_state(r):
    # COMBO_TRAINED100_STATE_PREFIX_V2
    # For human-trained rules, the stored scope prefix is authoritative.
    # This prevents cross-combo questions such as
    # "Top Faculty ... Pro Pack upgrade?" being assigned to the wrong state.
    _trained_intent = str(r.get('intent') or '')

    if _trained_intent.startswith('TRAINED100|Pro Pack|'):
        return 'PRO_PACK'

    if _trained_intent.startswith('TRAINED100|Top Faculty|'):
        return 'TOP_FACULTY'

    if _trained_intent.startswith('TRAINED100|All Combo|'):
        return 'COMBO'

    s=str(r.get('intent','')).casefold()+' '+str(r.get('keywords','')).casefold()
    if 'pro pack' in s or 'propack' in s or 'pro combo' in s: return 'PRO_PACK'
    if 'top faculty' in s or 'topfaculty' in s or 'faculty combo' in s: return 'TOP_FACULTY'
    return 'COMBO'

def _v6_phrases(text):
    return [p.strip() for p in str(text or '').split('|') if p.strip()]

def _v6_match_score(message, ai_keywords, r):
    n=_norm(message); toks=_tokens(message); score=0.0
    for p in _v6_phrases(r.get('keywords')):
        pn=_norm(p)
        if pn and pn in n: score=max(score, 100.0 + min(len(pn.split()),6)*2)
        pt=_tokens(p)
        if pt:
            overlap=len(pt & toks)/max(len(pt),1)
            score=max(score, overlap*70.0)
    for k in (ai_keywords or []):
        kn=_norm(k)
        if not kn: continue
        if kn in n: score += 18.0
        kt=_tokens(kn); score += 10.0*len(kt & toks)/max(len(kt),1)
        for p in _v6_phrases(r.get('keywords')):
            if kn==_norm(p): score += 35.0
    return score

async def _v6_context_get(path, chat_id):
    await ensure_combo_rules(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        return await (await db.execute('SELECT * FROM combo_conversation_context WHERE chat_id=?',(str(chat_id),))).fetchone()

async def _v6_context_set(path, chat_id, **changes):
    await ensure_combo_rules(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory=aiosqlite.Row
        old=await (await db.execute('SELECT * FROM combo_conversation_context WHERE chat_id=?',(str(chat_id),))).fetchone()
        data=dict(old) if old else {'chat_id':str(chat_id)}
        data.update(changes)
        cols=['chat_id','active_group','active_combo','last_rule_id','last_customer_message','last_bot_question','combo_stage','negotiation_count','current_state','current_intent','last_ai_keywords']
        vals=[data.get(c,'') for c in cols]
        await db.execute('INSERT INTO combo_conversation_context(chat_id,active_group,active_combo,last_rule_id,last_customer_message,last_bot_question,combo_stage,negotiation_count,current_state,current_intent,last_ai_keywords,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(chat_id) DO UPDATE SET active_group=excluded.active_group,active_combo=excluded.active_combo,last_rule_id=excluded.last_rule_id,last_customer_message=excluded.last_customer_message,last_bot_question=excluded.last_bot_question,combo_stage=excluded.combo_stage,negotiation_count=excluded.negotiation_count,current_state=excluded.current_state,current_intent=excluded.current_intent,last_ai_keywords=excluded.last_ai_keywords,updated_at=CURRENT_TIMESTAMP',vals)
        await db.commit()

async def _v6_ai_audit(path, ai, update, message, response, *, route, selected_intent='', keywords=None, state='', status='success', error_text=''):
    try:
        from app import bot as _bot_module
        rec=getattr(_bot_module,'record_ai_call_audit',None); upd=getattr(_bot_module,'update_ai_call_audit_decision',None)
        if not rec:return
        usage=getattr(response,'usage',None)
        def iv(v):
            try:return int(v or 0)
            except:return 0
        inp=iv(getattr(usage,'input_tokens',0) if usage else 0)
        out=iv(getattr(usage,'output_tokens',0) if usage else 0)
        total=iv(getattr(usage,'total_tokens',0) if usage else 0)
        if not total: total=inp+out
        pd=getattr(usage,'input_tokens_details',None) if usage else None; cd=getattr(usage,'output_tokens_details',None) if usage else None
        cached=iv(getattr(pd,'cached_tokens',0) if pd else 0); reasoning=iv(getattr(cd,'reasoning_tokens',0) if cd else 0)
        model=str(getattr(ai,'model','') or '')
        price=getattr(_bot_module,'_estimate_ai_cost_usd',None)
        cost=price(model,inp,cached,out) if price else None
        user_obj=getattr(update,'effective_user',None)
        uid=getattr(user_obj,'id',None); cid=getattr(getattr(update,'effective_chat',None),'id',None)
        cname=' '.join(x for x in [getattr(user_obj,'first_name',''),getattr(user_obj,'last_name','')] if x).strip()
        cusername=str(getattr(user_obj,'username','') or '')
        call_id=await rec(path,source='combo_rules',telegram_user_id=uid,chat_id=cid,customer_message=message,model=model,status=status,error_text=error_text,input_tokens=inp,cached_input_tokens=cached,output_tokens=out,reasoning_tokens=reasoning,total_tokens=total,estimated_cost_usd=cost,latency_ms=0,customer_name=cname,customer_username=cusername)
        if upd:
            raw=getattr(response,'output_text','') if response else ''
            await upd(path,call_id,route=route,selected_intent=selected_intent,ai_confidence=state,ai_raw_response=str(raw)[:8000],ai_input_context=f'STATE={state}; AI_KEYWORDS={json.dumps(keywords or [],ensure_ascii=False)}',server_action='combo_rules_state_filter',server_status=status,server_error=error_text)
    except Exception:
        pass


# AI_REPLY_ON_COMBO_AI_BYPASS_V1
async def _v6_ai_reply_enabled(path):
    """
    True only when the new AI Reply fallback is enabled.

    When True:
      - deterministic Combo Rules continue working
      - Combo Rules OpenAI probe/disambiguation are bypassed
      - unresolved messages fall through to the one-call Luna reply path

    When False:
      - legacy Combo Rules AI behaves exactly as before
    """
    try:
        async with aiosqlite.connect(path) as db:
            row = await (
                await db.execute(
                    "SELECT enabled FROM ai_reply_settings WHERE id=1"
                )
            ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


async def _v6_ai_intent_probe(path, ai, update, message, state):
    if await _v6_ai_reply_enabled(path):
        logger.info(
            "AI REPLY ON: Combo Rules intent probe bypassed message=%r ai_calls=0",
            message
        )
        return {
            'keywords': [],
            'state': state,
            'language': 'english'
        }, None

    client=getattr(ai,'client',None); model=getattr(ai,'model',None)
    if client is None or not model:return {'keywords':[],'state':state,'language':'english'},None
    prompt=("Return JSON only: {\"keywords\":[up to 5 short normalized intent terms],\"state\":\"COMBO|PRO_PACK|TOP_FACULTY|KEEP\",\"language\":\"english|hinglish|hindi\"}. "
            "Do not invent business facts. Determine the customer's current subject from the message. "
            f"CURRENT STATE: {state or 'NONE'}\nCUSTOMER: {message}")
    t=time.perf_counter(); resp=None
    try:
        resp=await client.responses.create(model=model,instructions='You are a compact intent extractor. Output JSON only.',input=prompt,max_output_tokens=80)
        raw=getattr(resp,'output_text','') or ''
        try:data=json.loads(raw)
        except Exception:
            m=re.search(r'\{.*\}',raw,re.S); data=json.loads(m.group(0)) if m else {}
        kws=data.get('keywords') if isinstance(data.get('keywords'),list) else []
        st=str(data.get('state') or '').upper(); st=state if st=='KEEP' else (st if st in ('COMBO','PRO_PACK','TOP_FACULTY') else state)
        result={'keywords':[str(x)[:60] for x in kws[:5]],'state':st,'language':str(data.get('language') or 'english')}
        await _v6_ai_audit(path,ai,update,message,resp,route='combo_rules_probe',keywords=result['keywords'],state=st)
        return result,resp
    except Exception as e:
        if resp is not None: await _v6_ai_audit(path,ai,update,message,resp,route='combo_rules_probe',status='error',error_text=str(e)[:1000])
        return {'keywords':[],'state':state,'language':'english'},resp

async def _v6_ai_choose(path, ai, update, message, state, candidates):
    if await _v6_ai_reply_enabled(path):
        logger.info(
            "AI REPLY ON: Combo Rules disambiguation bypassed message=%r ai_calls=0",
            message
        )
        return None, None

    client=getattr(ai,'client',None); model=getattr(ai,'model',None)
    if client is None or not model:return None,None
    compact=[{'id':int(r['id']),'intent':r['intent'],'keywords':_v6_phrases(r['keywords'])[:6]} for r in candidates[:3]]
    prompt=("Return JSON only: {\"id\": integer}. Choose exactly one candidate that best matches the customer. "
            "Use only the candidates. No explanation.\n"
            f"CURRENT STATE: {state}\nCUSTOMER: {message}\nCANDIDATES: {json.dumps(compact,ensure_ascii=False)}")
    resp=None
    try:
        resp=await client.responses.create(model=model,instructions='You are a compact intent selector. Output JSON only.',input=prompt,max_output_tokens=40)
        raw=getattr(resp,'output_text','') or ''
        try:data=json.loads(raw)
        except Exception:
            m=re.search(r'\{.*?\}',raw,re.S); data=json.loads(m.group(0)) if m else {}
        rid=int(data.get('id')) if data.get('id') is not None else None
        chosen=next((r for r in candidates if int(r['id'])==rid),None)
        await _v6_ai_audit(path,ai,update,message,resp,route='combo_rules_disambiguation',selected_intent=chosen['intent'] if chosen else '',keywords=[],state=state)
        return chosen,resp
    except Exception as e:
        if resp is not None: await _v6_ai_audit(path,ai,update,message,resp,route='combo_rules_disambiguation',status='error',error_text=str(e)[:1000])
        return None,resp

async def ensure_combo_rules(path):
    # Migrate schema first using the prior implementation, then make V6 canonical data authoritative.
    try: await _old_ensure_combo_rules(path)
    except Exception: pass
    async with aiosqlite.connect(path) as db:
        cols=[r[1] for r in await (await db.execute('PRAGMA table_info(combo_conversation_context)')).fetchall()]
        for col,typ in [('combo_stage','TEXT DEFAULT \'\''),('negotiation_count','INTEGER NOT NULL DEFAULT 0'),('current_state','TEXT DEFAULT \'\''),('current_intent','TEXT DEFAULT \'\''),('last_ai_keywords','TEXT DEFAULT \'\'')]:
            if col not in cols: await db.execute(f'ALTER TABLE combo_conversation_context ADD COLUMN {col} {typ}')
        await db.commit()
        db.row_factory=aiosqlite.Row
        g=await (await db.execute('SELECT id FROM rule_groups WHERE slug=?',(GROUP_SLUG,))).fetchone()
        if not g:return
        gid=int(g['id'])
        import hashlib
        payload=json.dumps(V6_RULES,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(payload.encode('utf-8')).hexdigest()
        await db.execute('CREATE TABLE IF NOT EXISTS combo_rules_dataset_meta (group_id INTEGER PRIMARY KEY, dataset_hash TEXT NOT NULL)')
        meta=await (await db.execute('SELECT dataset_hash FROM combo_rules_dataset_meta WHERE group_id=?',(gid,))).fetchone()
        # Only replace rows when the canonical dataset itself changed. This prevents
        # deleting/reinserting the 73 rules on every customer message and preserves
        # deliberate admin edits until the next dataset update.
        if not meta or str(meta[0] or '')!=digest:
            await db.execute('DELETE FROM combo_rules WHERE group_id=?',(gid,))
            for i,r in enumerate(V6_RULES,1):
                await db.execute('INSERT INTO combo_rules(group_id,sort_order,intent,reply_en,reply_hi,trigger,keywords,enabled) VALUES(?,?,?,?,?,?,?,1)',(gid,i,r['intent'],r.get('reply_en',''),r.get('reply_hi',''),r.get('trigger',''),r.get('keywords','')))
            await db.execute('INSERT INTO combo_rules_dataset_meta(group_id,dataset_hash) VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET dataset_hash=excluded.dataset_hash',(gid,digest))
            await db.commit()


async def _v7_claim_message(path, chat_id, message, window_seconds=45):
    # COMBO_STATEFUL_DEDUPE_FIX_V8_1
    key = _norm(message)
    if not key:
        return True

    now = time.time()
    cutoff = now - float(window_seconds)

    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS combo_ai_message_dedupe (
                chat_id TEXT NOT NULL,
                message_key TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_key)
            )
        """)

        short_reply = bool(re.fullmatch(
            r'(?:yes|yeah|yep|yup|haan|han|ha|haa|hmm|ok|okay|no|nope|nah|nahi|nhi|na)',
            key
        ))

        if short_reply:
            try:
                row_stage = await (
                    await db.execute(
                        "SELECT combo_stage FROM combo_conversation_context "
                        "WHERE chat_id=? LIMIT 1",
                        (str(chat_id),)
                    )
                ).fetchone()

                stage = str(
                    (row_stage[0] if row_stage else '') or ''
                ).strip().upper()

                if stage in (
                    'AWAITING_PAYMENT',
                    'AWAITING_PHONEPE',
                    'PRE_OFFERED',
                    'AWAITING_OPTIONAL',
                ):
                    key = f"{stage}::{key}"
            except Exception:
                pass

        await db.execute("BEGIN IMMEDIATE")

        row = await (await db.execute(
            "SELECT claimed_at FROM combo_ai_message_dedupe "
            "WHERE chat_id=? AND message_key=?",
            (str(chat_id), key)
        )).fetchone()

        if row and float(row[0] or 0) >= cutoff:
            await db.rollback()
            return False

        await db.execute(
            "INSERT INTO combo_ai_message_dedupe "
            "(chat_id,message_key,claimed_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,message_key) "
            "DO UPDATE SET claimed_at=excluded.claimed_at",
            (str(chat_id), key, now)
        )

        await db.execute(
            "DELETE FROM combo_ai_message_dedupe WHERE claimed_at < ?",
            (cutoff,)
        )

        await db.commit()
        return True


async def _v8_sticky_state_get(path, chat_id):
    """Return sticky combo state metadata for the customer."""
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS combo_sticky_state (
                chat_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                started_at REAL NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                last_message_at REAL NOT NULL
            )
        """)
        row = await (await db.execute("""
            SELECT state, started_at, message_count, last_message_at
            FROM combo_sticky_state
            WHERE chat_id=?
        """, (str(chat_id),))).fetchone()
        await db.commit()

    if not row:
        return None

    state, started_at, message_count, last_message_at = row
    now = time.time()

    # Sticky state expires after 30 minutes OR 25 customer messages.
    if (
        str(state) not in ("PRO_PACK", "TOP_FACULTY")
        or now - float(started_at or 0) >= 1800
        or int(message_count or 0) >= 25
    ):
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "DELETE FROM combo_sticky_state WHERE chat_id=?",
                (str(chat_id),)
            )
            await db.commit()
        return None

    return {
        "state": str(state),
        "started_at": float(started_at or now),
        "message_count": int(message_count or 0),
        "last_message_at": float(last_message_at or now),
    }


async def _v8_sticky_state_set(path, chat_id, state):
    """Start/reset a sticky combo state."""
    if state not in ("PRO_PACK", "TOP_FACULTY"):
        return

    now = time.time()

    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS combo_sticky_state (
                chat_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                started_at REAL NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                last_message_at REAL NOT NULL
            )
        """)
        await db.execute("""
            INSERT INTO combo_sticky_state
                (chat_id,state,started_at,message_count,last_message_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                state=excluded.state,
                started_at=excluded.started_at,
                message_count=excluded.message_count,
                last_message_at=excluded.last_message_at
        """, (str(chat_id), state, now, 0, now))
        await db.commit()


async def _v8_sticky_state_touch(path, chat_id):
    """Count a customer message without changing the selected combo."""
    now = time.time()

    async with aiosqlite.connect(path) as db:
        row = await (await db.execute("""
            SELECT state, started_at, message_count
            FROM combo_sticky_state
            WHERE chat_id=?
        """, (str(chat_id),))).fetchone()

        if not row:
            return None

        state, started_at, count = row
        count = int(count or 0) + 1

        if (
            str(state) not in ("PRO_PACK", "TOP_FACULTY")
            or now - float(started_at or 0) >= 1800
            or count > 25
        ):
            await db.execute(
                "DELETE FROM combo_sticky_state WHERE chat_id=?",
                (str(chat_id),)
            )
            await db.commit()
            return None

        await db.execute("""
            UPDATE combo_sticky_state
            SET message_count=?, last_message_at=?
            WHERE chat_id=?
        """, (count, now, str(chat_id)))
        await db.commit()

        return {
            "state": str(state),
            "started_at": float(started_at or now),
            "message_count": count,
            "last_message_at": now,
        }


async def process_customer(update,context,path,ai):
    if not getattr(update,'message',None) or not getattr(update.message,'text',None): return False
    message=update.message.text.strip(); chat_id=update.effective_chat.id

    # V12: exact combo selections are deterministic.
    # Use normalized exact forms; no regex and no AI.
    _direct_combo_norm = _norm(message)
    _direct_combo_state = None

    if _direct_combo_norm in (
        'pro pack',
        'propack',
        'pro combo',
        'i want pro pack',
        'i wants pro pack',
        'want pro pack',
        'wants pro pack',
        'need pro pack',
        'choose pro pack',
        'select pro pack',
        'give pro pack',
        'send pro pack',
        'show pro pack',
    ):
        _direct_combo_state = 'PRO_PACK'

    elif _direct_combo_norm in (
        'top faculty',
        'topfaculty',
        'top faculty combo',
        'i want top faculty',
        'i wants top faculty',
        'want top faculty',
        'wants top faculty',
        'need top faculty',
        'choose top faculty',
        'select top faculty',
        'give top faculty',
        'send top faculty',
        'show top faculty',
    ):
        _direct_combo_state = 'TOP_FACULTY'

    if _direct_combo_state:
        _direct_trigger = (
            '/propack'
            if _direct_combo_state == 'PRO_PACK'
            else '/topfaculty'
        )
        _direct_intent = (
            'I want Pro Pack'
            if _direct_combo_state == 'PRO_PACK'
            else 'I want Top Faculty'
        )

        _direct_rows = await rules_rows(path, enabled_only=True)
        _direct_rule = next(
            (
                r for r in _direct_rows
                if str(r.get('intent') or '').strip().casefold()
                == _direct_intent.casefold()
            ),
            None
        )

        if _direct_rule:
            try:
                if await _v3_execute_trigger(
                    update, context, _direct_trigger
                ):
                    await _v8_sticky_state_set(
                        path, chat_id, _direct_combo_state
                    )
                    await _v6_context_set(
                        path, chat_id,
                        active_group=GROUP_SLUG,
                        active_combo=(
                            'Pro Pack'
                            if _direct_combo_state == 'PRO_PACK'
                            else 'Top Faculty'
                        ),
                        current_state=_direct_combo_state,
                        current_intent=_direct_intent,
                        last_customer_message=message,
                        combo_stage='SELECTED',
                        negotiation_count=0,
                        last_ai_keywords='[]'
                    )
                    logger.info(
                        "V12 DETERMINISTIC COMBO: state=%s message=%r ai_calls=0",
                        _direct_combo_state,
                        message
                    )
                    return True
            except Exception:
                logger.exception(
                    "V12 deterministic combo execution failed"
                )

    # COMBO_PAYMENT_STATE_LATEST_WRAPPER_FIX_V8_3
    # Handle stateful payment replies in the CURRENT/latest process_customer
    # before dedupe, AI probing, or generic candidate routing.
    _payment_ctx_v83 = await _v6_context_get(path, chat_id)
    _payment_stage_v83 = str(
        (
            _payment_ctx_v83['combo_stage']
            if _payment_ctx_v83
            and 'combo_stage' in _payment_ctx_v83.keys()
            else ''
        ) or ''
    ).strip().upper()

    if _payment_stage_v83 == 'AWAITING_PAYMENT':
        if _v3_yes(message):
            _reply_v83 = (
                'Aap PhonePe use karte ho?'
                if _v3_hinglish(message)
                else 'Do you use PhonePe?'
            )
            await update.message.reply_text(_reply_v83)

            await _v6_context_set(
                path,
                chat_id,
                last_customer_message=message,
                last_bot_question=_reply_v83,
                combo_stage='AWAITING_PHONEPE'
            )

            logger.info(
                "V8_3 PAYMENT STATE: AWAITING_PAYMENT -> "
                "AWAITING_PHONEPE chat_id=%s",
                chat_id
            )
            return True

    if _payment_stage_v83 == 'AWAITING_PHONEPE':
        if _v3_yes(message):
            await _v6_context_set(
                path,
                chat_id,
                last_customer_message=message,
                last_bot_question='',
                combo_stage='PAYMENT_STARTED'
            )

            logger.info(
                "V8_3 PAYMENT STATE: AWAITING_PHONEPE -> "
                "PAYMENT_STARTED chat_id=%s",
                chat_id
            )

            return await _v3_execute_trigger(
                update,
                context,
                '/phonepe'
            )

        if _v3_no(message):
            _rows_v83 = await rules_rows(path, enabled_only=True)
            _rule_v83 = next(
                (
                    r for r in _rows_v83
                    if str(r.get('intent') or '').strip().casefold()
                    == 'customer cannot use phonepe'
                ),
                None
            )

            if _rule_v83:
                _reply_v83 = (
                    _rule_v83.get('reply_hi')
                    if _v3_hinglish(message)
                    and _rule_v83.get('reply_hi')
                    else
                    _rule_v83.get('reply_en')
                    or _rule_v83.get('reply_hi')
                    or ''
                )

                if _reply_v83:
                    await update.message.reply_text(_reply_v83)

                    await _v6_context_set(
                        path,
                        chat_id,
                        last_customer_message=message,
                        last_bot_question=_reply_v83,
                        combo_stage='PHONEPE_UNAVAILABLE'
                    )
                    return True

    if not await _v7_claim_message(path,chat_id,message): return True

    # V8: preserve selected combo context for up to 30 minutes / 25 messages.
    _sticky = await _v8_sticky_state_get(path, chat_id)
    if _sticky:
        _sticky = await _v8_sticky_state_touch(path, chat_id)
    ctx=await _v6_context_get(path,chat_id)
    old_state=str(ctx['current_state'] if ctx and 'current_state' in ctx.keys() else (ctx['active_combo'] if ctx else '') or '')
    if _sticky and _sticky.get('state') in ('PRO_PACK','TOP_FACULTY'):
        old_state = _sticky['state']
    explicit=_v6_state_from_text(message,old_state)
    signal=bool(re.search(r'\b(combo|all\s*combo|all\s*combos|pro\s*pack|propack|pro\s*combo|top\s*faculty|topfaculty|faculty\s*combo)\b',_norm(message)))

    # V8: Specific follow-up questions outrank combo-name selection.
    # The presence of "Pro Pack"/"Top Faculty" inside a question
    # must not re-run the combo catalogue.
    _m8 = _norm(message)
    _state8 = old_state

    _groups8 = bool(re.search(
        r'\b(how\s+many|kitne|kitna|kitni|number\s+of)\b.*\b(group|groups|grp|groups?)\b'
        r'|\b(group|groups|grp)\b.*\b(how\s+many|kitne|kitna|kitni)\b',
        _m8
    ))

    _validity8 = bool(re.search(
        r'\b(validity|valid|duration|kitne\s+(din|time)|kab\s+tak|kabb\s+tak)\b',
        _m8
    ))

    _updates8 = bool(re.search(
        r'\b(update|updates|updated|milega|milenge|milte|kab\s+tak)\b',
        _m8
    ))

    _optional8 = bool(re.search(
        r'\b(optional|subject|optional\s+subject|which\s+optional|kaunsa\s+optional)\b',
        _m8
    ))

    if _state8 == 'PRO_PACK':
        if _groups8:
            explicit = 'PRO_PACK_GROUPS'
        elif _validity8:
            explicit = 'PRO_PACK_VALIDITY'
        elif _updates8:
            explicit = 'PRO_PACK_UPDATES'
        elif _optional8:
            explicit = 'PRO_PACK_OPTIONAL'
    elif _state8 == 'TOP_FACULTY':
        if _groups8:
            explicit = 'TOP_FACULTY_GROUPS'
        elif _validity8:
            explicit = 'TOP_FACULTY_VALIDITY'
        elif _updates8:
            explicit = 'TOP_FACULTY_UPDATES'
    if not signal and old_state not in ('COMBO','PRO_PACK','TOP_FACULTY'): return False

    # V10: deterministic routing BEFORE AI.
    # Explicit combo selections never need an AI probe.
    _pre_rows = await rules_rows(path,enabled_only=True)

    _explicit_pro = bool(re.fullmatch(
        r'\s*(?:i\s+)?(?:want|wants|need|choose|select|give|send|show)?\s*'
        r'pro\s*(?:pack|combo)\s*\??\s*',
        message,
        re.IGNORECASE
    ))
    _explicit_top = bool(re.fullmatch(
        r'\s*(?:i\s+)?(?:want|wants|need|choose|select|give|send|show)?\s*'
        r'top\s*faculty(?:\s+combo)?\s*\??\s*',
        message,
        re.IGNORECASE
    ))

    if _explicit_pro or _explicit_top:
        _direct_state = 'PRO_PACK' if _explicit_pro else 'TOP_FACULTY'
        _direct_intent = 'I want Pro Pack' if _explicit_pro else 'I want Top Faculty'
        _direct_trigger = '/propack' if _explicit_pro else '/topfaculty'
        _direct_rule = next(
            (r for r in _pre_rows if str(r.get('intent') or '').strip() == _direct_intent),
            None
        )

        if _direct_rule and await _v3_execute_trigger(update,context,_direct_trigger):
            await _v8_sticky_state_set(path, chat_id, _direct_state)
            await _v6_context_set(
                path,chat_id,
                active_group=GROUP_SLUG,
                active_combo='Pro Pack' if _direct_state == 'PRO_PACK' else 'Top Faculty',
                current_state=_direct_state,
                current_intent=_direct_intent,
                last_customer_message=message,
                combo_stage='SELECTED',
                negotiation_count=0,
                last_ai_keywords='[]'
            )
            logger.info(
                "V10 DETERMINISTIC COMBO: state=%s message=%r ai_calls=0",
                _direct_state, message
            )
            return True

    # V10: common greetings do not need the Combo Rules AI.
    # Return False so the normal customer/greeting handler can answer.
    if old_state in ('PRO_PACK','TOP_FACULTY') and re.fullmatch(
        r'\s*(?:hi|hello|hey|hii|hiii|namaste|namaskar|'
        r'hi\s+(?:bro|brother)|hello\s+(?:bro|brother)|'
        r'hey\s+(?:bro|brother))\s*[!.?]*\s*',
        message,
        re.IGNORECASE
    ):
        logger.info(
            "V10 DETERMINISTIC GREETING: state=%s message=%r ai_calls=0",
            old_state, message
        )
        return False

    # One compact AI probe: state + customer message only. Never send the rule catalog.
    probe,_=await _v6_ai_intent_probe(path,ai,update,message,old_state)

    # V9: Sticky combo state is authoritative while active.
    # The AI probe must not move the customer out of the selected
    # combo merely because an unrelated message suggests another state.
    if _sticky and _sticky.get('state') in ('PRO_PACK','TOP_FACULTY'):
        _sticky_state = _sticky['state']

        # Only an explicit selection of another combo may switch state.
        _explicit_combo_switch = explicit if explicit in ('PRO_PACK','TOP_FACULTY') else None

        if _explicit_combo_switch:
            state = _explicit_combo_switch
        else:
            state = _sticky_state
    else:
        state=probe.get('state') or explicit or old_state

    if state not in ('COMBO','PRO_PACK','TOP_FACULTY'):
        state=old_state or explicit
    rows=await rules_rows(path,enabled_only=True)
    if not rows:return False

    # V9: Deterministic state-specific follow-up answers.
    # These intents must be answered directly before trigger selection.
    _direct_followup_intents = {
        'PRO_PACK_GROUPS': 'How many groups are in Pro Pack?',
        'PRO_PACK_VALIDITY': 'Validity',
        'PRO_PACK_UPDATES': 'Updates until Mains 2027',
        'PRO_PACK_OPTIONAL': 'Optional included in Pro Pack',
        'TOP_FACULTY_GROUPS': 'Is Top Faculty a single group?',
        'TOP_FACULTY_VALIDITY': 'Validity',
        'TOP_FACULTY_UPDATES': 'Updates until Mains 2027',
    }

    if explicit in _direct_followup_intents:
        _direct_intent = _direct_followup_intents[explicit]
        _direct_rule = next(
            (r for r in rows if str(r.get('intent') or '').strip() == _direct_intent),
            None
        )
        if _direct_rule:
            _direct_reply = (
                _direct_rule.get('reply_hi')
                if _v3_hinglish(message) and _direct_rule.get('reply_hi')
                else _direct_rule.get('reply_en') or _direct_rule.get('reply_hi') or ''
            )
            if _direct_reply:
                await update.message.reply_text(_direct_reply)
                await _v6_context_set(
                    path,
                    chat_id,
                    active_group=GROUP_SLUG,
                    active_combo='Pro Pack' if state == 'PRO_PACK' else 'Top Faculty',
                    current_state=state,
                    current_intent=_direct_rule['intent'],
                    last_customer_message=message,
                    last_bot_question='',
                    combo_stage='SELECTED',
                    negotiation_count=0,
                    last_ai_keywords=json.dumps(
                        probe.get('keywords', []),
                        ensure_ascii=False
                    )
                )
                return True

    # Server-side candidate retrieval, restricted to current state.
    candidates=[]
    for r in rows:
        rs=_v6_rule_state(r)
        if state=='PRO_PACK' and rs not in ('PRO_PACK','COMBO'): continue
        if state=='TOP_FACULTY' and rs not in ('TOP_FACULTY','COMBO'): continue
        score=_v6_match_score(message,probe.get('keywords',[]),r)
        if score>=45:candidates.append((score,r))
    # COMBO_TRAINED100_PRIORITY_V1
    # Human-trained rules get priority only inside their own Combo state.
    _trained100_prefix={
        'PRO_PACK':'TRAINED100|Pro Pack|',
        'TOP_FACULTY':'TRAINED100|Top Faculty|',
        'COMBO':'TRAINED100|All Combo|',
    }.get(state,'')

    _trained100_candidates=[
        x for x in candidates
        if (
            _trained100_prefix
            and str(x[1].get('intent') or '').startswith(_trained100_prefix)
        )
    ]

    _trained100_candidates.sort(
        key=lambda x:(x[0],-int(x[1]['sort_order'])),
        reverse=True
    )

    _trained100_priority=bool(
        _trained100_candidates
        and _trained100_candidates[0][0] >= 45
    )

    if _trained100_priority:
        candidates=_trained100_candidates
    else:
        candidates.sort(
            key=lambda x:(x[0],-int(x[1]['sort_order'])),
            reverse=True
        )

    top=[r for _,r in candidates[:3]]

    # AI_REPLY_ON_DETERMINISTIC_COMBO_ONLY_V1
    # When AI Reply is ON, Combo Rules may execute only a CLEAR local/saved
    # match. Weak or ambiguous matches fall through to the single Luna reply.
    _ai_reply_combo_mode = await _v6_ai_reply_enabled(path)

    if _ai_reply_combo_mode and candidates:
        _top_score = float(candidates[0][0] or 0)
        _second_score = float(candidates[1][0] or 0) if len(candidates) > 1 else -1

        _clear_local_combo_match = (
            _top_score >= 70
            and (
                len(candidates) == 1
                or (_top_score - _second_score) >= 25
            )
        )

        if not _clear_local_combo_match:
            logger.info(
                "AI REPLY ON: Combo Rules uncertain local match -> fallthrough "
                "message=%r top_score=%.2f second_score=%.2f",
                message,
                _top_score,
                _second_score
            )
            return False

        logger.info(
            "AI REPLY ON: Combo Rules clear deterministic match "
            "message=%r intent=%r score=%.2f ai_calls=0",
            message,
            str(candidates[0][1].get('intent') or ''),
            _top_score
        )

    # Generic catalogue remains deterministic.
    if state=='COMBO' and not _trained100_priority and not re.search(r'\b(pro\s*pack|propack|pro\s*combo|top\s*faculty|topfaculty|faculty\s*combo)\b',_norm(message)):
        generic=[r for r in rows if str(r.get('trigger') or '').strip()=='/allcombo']
        ranked=sorted(((_v6_match_score(message,probe.get('keywords',[]),r),r) for r in generic),key=lambda x:(x[0],-int(x[1]['sort_order'])),reverse=True)
        if ranked and ranked[0][0]>=45:
            chosen=ranked[0][1]
            await _v6_context_set(path,chat_id,active_group=GROUP_SLUG,active_combo='COMBO',current_state='COMBO',current_intent=chosen['intent'],last_customer_message=message,last_ai_keywords=json.dumps(probe.get('keywords',[]),ensure_ascii=False),combo_stage='AWAITING_COMBO',negotiation_count=0)
            if await _v3_execute_trigger(update,context,'/allcombo'):
                await update.message.reply_text('Which combo would you like?' if not _v3_hinglish(message) else 'Aap kaunsa combo chahte ho?'); return True
    # COMBO_PERSISTENT_FOLLOWUP_FIX_V7
    #
    # Once a customer has selected Top Faculty or Pro Pack, keep ordinary
    # questions inside that combo context. Do not resend /topfaculty,
    # /propack or /allcombo for follow-up questions.
    #
    # A deliberate selection of the OTHER combo (or All Combo) is allowed
    # to change the state and show that newly selected catalogue once.
    _selected_state_v7 = (
        old_state
        if old_state in ('PRO_PACK', 'TOP_FACULTY')
        else ''
    )
    _m7 = _norm(message)

    if _selected_state_v7:
        _mentions_pro_v7 = bool(re.search(
            r'\b(?:pro\s*pack|propack|pro\s*combo)\b',
            _m7
        ))
        _mentions_top_v7 = bool(re.search(
            r'\b(?:top\s*faculty|topfaculty|faculty\s*combo)\b',
            _m7
        ))
        _mentions_all_v7 = bool(re.search(
            r'\b(?:all\s*combo|allcombo|all\s*combos)\b',
            _m7
        ))

        _selection_words_v7 = bool(re.search(
            r'\b(?:want|need|choose|select|send|show|give|join|buy|'
            r'chahiye|chahie|lena|leni|do|bhejo)\b',
            _m7
        ))

        _plain_pro_v7 = bool(re.fullmatch(
            r'(?:pro\s*pack|propack|pro\s*combo)[?.!]*',
            _m7
        ))
        _plain_top_v7 = bool(re.fullmatch(
            r'(?:top\s*faculty|topfaculty|faculty\s*combo)[?.!]*',
            _m7
        ))
        _plain_all_v7 = bool(re.fullmatch(
            r'(?:all\s*combo|allcombo|all\s*combos)[?.!]*',
            _m7
        ))

        # ----------------------------------------------------
        # 1. Explicit switch to a DIFFERENT combo.
        # ----------------------------------------------------
        _switch_v7 = ''
        if (
            _selected_state_v7 == 'PRO_PACK'
            and _mentions_top_v7
            and (_selection_words_v7 or _plain_top_v7)
        ):
            _switch_v7 = 'TOP_FACULTY'
        elif (
            _selected_state_v7 == 'TOP_FACULTY'
            and _mentions_pro_v7
            and (_selection_words_v7 or _plain_pro_v7)
        ):
            _switch_v7 = 'PRO_PACK'
        elif (
            _mentions_all_v7
            and (_selection_words_v7 or _plain_all_v7)
        ):
            _switch_v7 = 'COMBO'

        if _switch_v7:
            _switch_trigger_v7 = {
                'TOP_FACULTY': '/topfaculty',
                'PRO_PACK': '/propack',
                'COMBO': '/allcombo',
            }[_switch_v7]

            if await _v3_execute_trigger(
                update,
                context,
                _switch_trigger_v7
            ):
                if _switch_v7 in ('TOP_FACULTY', 'PRO_PACK'):
                    await _v8_sticky_state_set(
                        path,
                        chat_id,
                        _switch_v7
                    )

                await _v6_context_set(
                    path,
                    chat_id,
                    active_group=GROUP_SLUG,
                    active_combo={
                        'TOP_FACULTY': 'Top Faculty',
                        'PRO_PACK': 'Pro Pack',
                        'COMBO': 'COMBO',
                    }[_switch_v7],
                    current_state=_switch_v7,
                    current_intent='Explicit combo switch',
                    last_customer_message=message,
                    combo_stage=(
                        'SELECTED'
                        if _switch_v7 != 'COMBO'
                        else 'AWAITING_COMBO'
                    ),
                    negotiation_count=0,
                )
                return True

        # ----------------------------------------------------
        # 2. State-aware FAQ. Text only; never resend poster.
        # ----------------------------------------------------
        _is_hi_v7 = _v3_hinglish(message)

        _validity_v7 = bool(re.search(
            r'\b(?:validity|valid|duration|kab\s+tak|kabb\s+tak|'
            r'kitne\s+(?:din|time)|access\s+kab\s+tak)\b',
            _m7
        ))

        _download_v7 = bool(re.search(
            r'\b(?:download|offline\s+save|save\s+lecture|'
            r'android|phone\s+me\s+download)\b',
            _m7
        ))

        _price_v7 = bool(re.search(
            r'\b(?:price|cost|fees?|amount|kitne\s+ka|kitna\s+ka|'
            r'rate)\b',
            _m7
        ))

        _groups_v7 = bool(re.search(
            r'\b(?:group|groups|grp)\b',
            _m7
        ) and re.search(
            r'\b(?:how\s+many|kitne|kitna|kitni|total|number)\b',
            _m7
        ))

        _notes_v7 = bool(
            re.search(r'\b(?:notes?|pdf)\b', _m7)
            and re.search(r'\b(?:lecture|lectures|both|dono|mil)\w*\b', _m7)
        )

        _optional_v7 = bool(re.search(
            r'\b(?:optional|optionals|optional\s+subject|'
            r'which\s+subject|kaunsa\s+subject|subject)\b',
            _m7
        ))

        _optional_subjects_v7 = [
            ('Anthropology', r'\banthropology\b|\banthro\b'),
            ('Geography', r'\bgeography\b|\bgeo\b'),
            ('Sociology', r'\bsociology\b|\bsocio\b'),
            ('History', r'\bhistory\b'),
            ('PSIR', r'\bpsir\b|political\s+science'),
            ('Philosophy', r'\bphilosophy\b'),
            ('Psychology', r'\bpsychology\b'),
            ('Public Administration', r'\bpublic\s+administration\b|\bpub\s*ad\b'),
            ('Mathematics', r'\bmathematics\b|\bmaths?\b'),
            ('Agriculture', r'\bagriculture\b|\bagri\b'),
        ]
        _named_optional_v7 = next(
            (
                name
                for name, pat in _optional_subjects_v7
                if re.search(pat, _m7)
            ),
            ''
        )

        _coaching_teacher_v7 = bool(re.search(
            r'\b(?:coaching|coachings|teacher|teachers|faculty|'
            r'sir|mam|maam)\b',
            _m7
        ))

        _content_v7 = bool(re.search(
            r'\b(?:course|courses|class|classes|lecture|lectures|'
            r'content|include|included|includes|kya\s+milega|'
            r'kya\s+milta|kya\s+milenge|kaun\s+se|kon\s+se|'
            r'kya\s+kya)\b',
            _m7
        ))

        _updates_v7 = bool(re.search(
            r'\b(?:update|updates|updated)\b',
            _m7
        ))

        _answer_v7 = ''

        if _validity_v7:
            _answer_v7 = (
                'Lifetime access hai — koi time limit nahi hai.'
                if _is_hi_v7
                else 'It has lifetime access with no time limit.'
            )

        elif _download_v7:
            _answer_v7 = (
                'Haan, Android phone par lectures download kar sakte ho.'
                if _is_hi_v7
                else 'Yes, lectures can be downloaded on an Android phone.'
            )

        elif _price_v7:
            if _selected_state_v7 == 'PRO_PACK':
                _answer_v7 = (
                    'Pro Pack ka price ₹1499 hai.'
                    if _is_hi_v7
                    else 'Pro Pack is ₹1499.'
                )
            else:
                _answer_v7 = (
                    'Top Faculty ka price ₹999 hai.'
                    if _is_hi_v7
                    else 'Top Faculty is ₹999.'
                )

        elif _groups_v7:
            if _selected_state_v7 == 'PRO_PACK':
                _answer_v7 = (
                    'Pro Pack mein total 18 groups hain, Optional ko include karke.'
                    if _is_hi_v7
                    else 'Pro Pack has 18 groups in total, including the Optional.'
                )
            else:
                _answer_v7 = (
                    'Top Faculty ek group hai jisme multiple topics hain aur lectures topic-wise arranged hain.'
                    if _is_hi_v7
                    else 'Top Faculty is one group with multiple topics, with lectures arranged topic-wise.'
                )

        elif _optional_v7 or _named_optional_v7:
            if _selected_state_v7 == 'TOP_FACULTY':
                _answer_v7 = (
                    'Top Faculty mein Optional included nahi hai.'
                    if _is_hi_v7
                    else 'An Optional is not included in Top Faculty.'
                )
            elif _named_optional_v7:
                _answer_v7 = (
                    f'Haan, Pro Pack mein ek Optional included hai. '
                    f'{_named_optional_v7} ko apna one Optional choose kar sakte ho.'
                    if _is_hi_v7
                    else (
                        f'Yes. Pro Pack includes one Optional, and '
                        f'{_named_optional_v7} can be chosen as that Optional.'
                    )
                )
            else:
                _answer_v7 = (
                    'Pro Pack mein ek Optional subject included hai. Kaunsa Optional subject chahiye?'
                    if _is_hi_v7
                    else 'One Optional subject is included in Pro Pack. Which Optional subject do you want?'
                )

        elif _coaching_teacher_v7:
            if _selected_state_v7 == 'PRO_PACK':
                _answer_v7 = (
                    'Pro Pack mein Top Faculty + All Coaching GS + ek Optional subject included hai.'
                    if _is_hi_v7
                    else 'Pro Pack includes Top Faculty, All Coaching GS, and one Optional subject.'
                )
            else:
                _answer_v7 = (
                    'Top Faculty mein sirf Top Faculty list mein mentioned teachers aur lectures milte hain; All Coaching GS aur Optional included nahi hain.'
                    if _is_hi_v7
                    else 'Top Faculty includes only the teachers and lectures listed in Top Faculty; All Coaching GS and an Optional are not included.'
                )

        elif _notes_v7:
            _answer_v7 = (
                'Haan, lectures aur notes dono milte hain.'
                if _is_hi_v7
                else 'Yes, both lectures and notes are included.'
            )

        elif _updates_v7:
            _answer_v7 = (
                'Free updates Mains 2027 tak milenge.'
                if _is_hi_v7
                else 'Free updates are available through Mains 2027.'
            )

        elif _content_v7:
            if _selected_state_v7 == 'PRO_PACK':
                _answer_v7 = (
                    'Pro Pack mein Top Faculty + All Coaching GS + ek Optional subject included hai.'
                    if _is_hi_v7
                    else 'Pro Pack includes Top Faculty, All Coaching GS, and one Optional subject.'
                )
            else:
                _answer_v7 = (
                    'Top Faculty mein Top Faculty list ke teachers aur lectures included hain. Optional included nahi hai.'
                    if _is_hi_v7
                    else 'Top Faculty includes the teachers and lectures listed in Top Faculty. An Optional is not included.'
                )

        if _answer_v7:
            await update.message.reply_text(_answer_v7)
            await _v6_context_set(
                path,
                chat_id,
                active_group=GROUP_SLUG,
                active_combo=(
                    'Pro Pack'
                    if _selected_state_v7 == 'PRO_PACK'
                    else 'Top Faculty'
                ),
                current_state=_selected_state_v7,
                current_intent='Selected combo follow-up',
                last_customer_message=message,
                last_bot_question=(
                    _answer_v7 if '?' in _answer_v7 else ''
                ),
                combo_stage='SELECTED',
                last_ai_keywords=json.dumps(
                    probe.get('keywords', []),
                    ensure_ascii=False
                ),
            )
            return True

        # ----------------------------------------------------
        # 3. Re-selecting the SAME combo must not resend its card.
        # ----------------------------------------------------
        _same_selection_v7 = bool(
            (
                _selected_state_v7 == 'PRO_PACK'
                and _mentions_pro_v7
                and (_selection_words_v7 or _plain_pro_v7)
            )
            or (
                _selected_state_v7 == 'TOP_FACULTY'
                and _mentions_top_v7
                and (_selection_words_v7 or _plain_top_v7)
            )
        )

        if _same_selection_v7:
            _pay_q_v8 = (
                'Aap abhi payment kar rahe ho?'
                if _is_hi_v7
                else 'Are you paying now?'
            )
            await update.message.reply_text(_pay_q_v8)
            await _v6_context_set(
                path,
                chat_id,
                active_group=GROUP_SLUG,
                active_combo=(
                    'Pro Pack'
                    if _selected_state_v7 == 'PRO_PACK'
                    else 'Top Faculty'
                ),
                current_state=_selected_state_v7,
                current_intent='Combo purchase confirmation',
                last_customer_message=message,
                last_bot_question=_pay_q_v8,
                combo_stage='AWAITING_PAYMENT',
                negotiation_count=0,
            )
            return True

    # Explicit selection changes persistent state.
    if state=='PRO_PACK' and not _trained100_priority and re.search(r'\b(pro\s*pack|propack|pro\s*combo)\b',_norm(message)):
        chosen=next((r for r in rows if r['intent']=='I want Pro Pack'),None)
        if chosen:
            await _v6_context_set(
                path,chat_id,
                active_group=GROUP_SLUG,
                active_combo='Pro Pack',
                current_state='PRO_PACK',
                current_intent=chosen['intent'],
                last_customer_message=message,
                last_ai_keywords=json.dumps(probe.get('keywords',[]),ensure_ascii=False),
                combo_stage='SELECTED',
                negotiation_count=0
            )
            await _v8_sticky_state_set(path, chat_id, 'PRO_PACK')
            if await _v3_execute_trigger(update,context,'/propack'): return True
    if state=='TOP_FACULTY' and not _trained100_priority and re.search(r'\b(top\s*faculty|topfaculty|faculty\s*combo)\b',_norm(message)):
        chosen=next((r for r in rows if r['intent']=='I want Top Faculty'),None)
        if chosen:
            await _v6_context_set(
                path,chat_id,
                active_group=GROUP_SLUG,
                active_combo='Top Faculty',
                current_state='TOP_FACULTY',
                current_intent=chosen['intent'],
                last_customer_message=message,
                last_ai_keywords=json.dumps(probe.get('keywords',[]),ensure_ascii=False),
                combo_stage='SELECTED',
                negotiation_count=0
            )
            await _v8_sticky_state_set(path, chat_id, 'TOP_FACULTY')
            if await _v3_execute_trigger(update,context,'/topfaculty'): return True
    if not top:return False
    chosen=top[0]
    # Second AI only when the OLD AI system is active.
    # With AI Reply ON, ambiguity already falls through above.
    if (
        not _ai_reply_combo_mode
        and len(top)>=2
        and (
            candidates[1][0]>=60
            or candidates[0][0]-candidates[1][0]<25
        )
    ):
        chosen,_=await _v6_ai_choose(path,ai,update,message,state,top)
        if not chosen:
            return False
    trigger=str(chosen.get('trigger') or '').strip()
    if chosen['intent'] in ('Can I get PRE-access before payment?','Add me first then I will pay','Add me first in Pro Pack','Can I get Top Faculty PRE-access?'):
        reply=chosen['reply_hi'] if _v3_hinglish(message) and chosen['reply_hi'] else chosen['reply_en'] or chosen['reply_hi']
        if reply:
            await update.message.reply_text(reply)
            await _v6_context_set(path,chat_id,active_group=GROUP_SLUG,active_combo='Top Faculty',current_state='TOP_FACULTY',current_intent=chosen['intent'],last_customer_message=message,last_bot_question=reply,combo_stage='PRE_OFFERED',negotiation_count=0,last_ai_keywords=json.dumps(probe.get('keywords',[]),ensure_ascii=False)); return True
    # COMBO_PERSISTENT_FOLLOWUP_FIX_V7_TRIGGER_GUARD
    # If a combo is already selected, a matched generic/same-combo rule may
    # still carry /propack, /topfaculty or /allcombo. Convert that to the
    # rule's text reply (or a state summary) instead of resending a catalogue.
    if (
        old_state in ('PRO_PACK', 'TOP_FACULTY')
        and trigger in ('/propack', '/topfaculty', '/allcombo')
    ):
        _guard_reply_v7 = (
            chosen['reply_hi']
            if _v3_hinglish(message) and chosen.get('reply_hi')
            else chosen.get('reply_en') or chosen.get('reply_hi') or ''
        )

        if not _guard_reply_v7:
            if old_state == 'PRO_PACK':
                _guard_reply_v7 = (
                    'Pro Pack mein Top Faculty + All Coaching GS + ek Optional subject included hai.'
                    if _v3_hinglish(message)
                    else 'Pro Pack includes Top Faculty, All Coaching GS, and one Optional subject.'
                )
            else:
                _guard_reply_v7 = (
                    'Top Faculty mein Top Faculty list ke teachers aur lectures included hain. Optional included nahi hai.'
                    if _v3_hinglish(message)
                    else 'Top Faculty includes the teachers and lectures listed in Top Faculty. An Optional is not included.'
                )

        await update.message.reply_text(_guard_reply_v7)
        await _v6_context_set(
            path,
            chat_id,
            active_group=GROUP_SLUG,
            active_combo=(
                'Pro Pack'
                if old_state == 'PRO_PACK'
                else 'Top Faculty'
            ),
            current_state=old_state,
            current_intent=chosen['intent'],
            last_customer_message=message,
            last_bot_question=(
                _guard_reply_v7 if '?' in _guard_reply_v7 else ''
            ),
            combo_stage='SELECTED',
            last_ai_keywords=json.dumps(
                probe.get('keywords', []),
                ensure_ascii=False
            ),
        )
        return True

    if trigger:
        if await _v3_execute_trigger(update,context,trigger):
            ns='PRO_PACK' if trigger=='/propack' else 'TOP_FACULTY' if trigger=='/topfaculty' else 'COMBO' if trigger=='/allcombo' else state

            if ns in ('PRO_PACK','TOP_FACULTY'):
                await _v8_sticky_state_set(path, chat_id, ns)

            await _v6_context_set(path,chat_id,active_group=GROUP_SLUG,active_combo={'PRO_PACK':'Pro Pack','TOP_FACULTY':'Top Faculty','COMBO':'COMBO'}.get(ns,''),current_state=ns,current_intent=chosen['intent'],last_customer_message=message,last_ai_keywords=json.dumps(probe.get('keywords',[]),ensure_ascii=False),combo_stage='SELECTED'); return True
    lang=str(probe.get('language') or '').casefold()
    _local_hinglish = _v3_hinglish(message)
    reply=chosen['reply_hi'] if (
        (
            'hindi' in lang
            or 'hinglish' in lang
            or lang=='hi'
            or (_ai_reply_combo_mode and _local_hinglish)
        )
        and chosen['reply_hi']
    ) else chosen['reply_en'] or chosen['reply_hi'] or ''
    if reply:
        await update.message.reply_text(reply)
        await _v6_context_set(path,chat_id,active_group=GROUP_SLUG,active_combo={'PRO_PACK':'Pro Pack','TOP_FACULTY':'Top Faculty','COMBO':'COMBO'}.get(state,''),current_state=state,current_intent=chosen['intent'],last_customer_message=message,last_bot_question=reply if '?' in reply else '',combo_stage='SELECTED',last_ai_keywords=json.dumps(probe.get('keywords',[]),ensure_ascii=False)); return True
    return False
