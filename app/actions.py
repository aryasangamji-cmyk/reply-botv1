import json
import re
import aiosqlite

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

async def _saved_actions(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT rule_json, enabled FROM ai_rules WHERE lower(name)='saved_actions' ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    if not row:
        return dict(DEFAULT_SAVED_ACTIONS)
    try:
        data = json.loads(row['rule_json'])
    except Exception:
        data = {}
    result = dict(DEFAULT_SAVED_ACTIONS)
    result.update({k: bool(v) for k, v in data.items() if k in DEFAULT_SAVED_ACTIONS})
    return result

ALLOWED_ACTIONS = {
    'show_batch', 'show_course', 'send_demo', 'send_payment',
    'send_purchase_link', 'show_faq', 'talk_to_admin',
    'negotiate', 'generic_reply'
}

async def get_entity(db_path, entity):
    if not entity or entity.get('type') not in ('batch', 'course'):
        return None
    table = 'batches' if entity['type'] == 'batch' else 'courses'
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if entity.get('id') is not None:
            async with db.execute(f'SELECT * FROM {table} WHERE id=? AND enabled=1', (entity['id'],)) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        name = (entity.get('name') or '').strip()
        if name:
            async with db.execute(
                f'SELECT * FROM {table} WHERE lower(trim(name))=lower(trim(?)) AND enabled=1',
                (name,)
            ) as cur:
                rows = await cur.fetchall()
                if len(rows) == 1:
                    return dict(rows[0])
        return None

def choose_action(intents, entity):
    names = {x.get('name') for x in intents if isinstance(x, dict)}
    if 'talk_to_admin' in names: return 'talk_to_admin'
    if 'negotiation' in names: return 'negotiate'
    if 'demo_request' in names: return 'send_demo'
    if 'payment_inquiry' in names: return 'send_payment'
    if 'link_request' in names or 'purchase_intent' in names: return 'send_purchase_link'
    if 'faq_inquiry' in names: return 'show_faq'
    if entity and entity.get('type') == 'batch': return 'show_batch'
    if entity and entity.get('type') == 'course': return 'show_course'
    return 'generic_reply'

def _offer(text):
    if not text: return None
    m = re.search(r'(?:₹|rs\.?\s*)?\s*(\d{2,6})\s*(?:/-|rupees|rs)?', text.lower())
    return int(m.group(1)) if m else None

def _price(record):
    if not record: return None
    m = re.search(r'\d[\d,]*', str(record.get('fee') or ''))
    return int(m.group().replace(',', '')) if m else None

async def _rule(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT rule_json FROM ai_rules WHERE lower(name)='negotiation' AND enabled=1 LIMIT 1") as cur:
            row = await cur.fetchone()
    if not row: return None
    try: return json.loads(row['rule_json'])
    except Exception: return None

def negotiation_response(rule, record, customer_message):
    if not record:
        return 'Please tell me the exact batch or course you want.'
    if not rule or not rule.get('enabled', False):
        return 'Discount offers are not configured for this batch/course yet.'
    price = _price(record)
    if price is None:
        return 'The current price could not be verified.'
    minimum = rule.get('minimum_price')
    max_discount = rule.get('max_discount_amount', 0)
    try: max_discount = max(0, int(max_discount))
    except Exception: max_discount = 0
    floor = price if minimum is None else max(0, int(minimum))
    allowed = min(max_discount, max(0, price - floor))
    final_price = price - allowed
    offer = _offer(customer_message)
    if offer is not None and offer >= final_price:
        return f'Yes, I can offer this for ₹{offer}.'
    if offer is not None and offer < final_price:
        return f'The lowest available offer is ₹{final_price}.'
    if final_price < price:
        return f'I can offer it for ₹{final_price}.'
    return f'The current price is ₹{price}.'

async def _generic_reply(db_path, intents):
    """Return an admin-approved generic reply for the first matching custom intent."""
    names = []
    for item in intents or []:
        if isinstance(item, dict):
            name = str(item.get('name') or '').strip()
            if name and name not in names:
                names.append(name)
    if not names:
        return None
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # Preserve admin ordering. Multiple replies for one intent are allowed.
        placeholders = ','.join('?' for _ in names)
        async with db.execute(
            f"SELECT reply_text FROM generic_replies "
            f"WHERE enabled=1 AND intent IN ({placeholders}) "
            f"ORDER BY sort_order ASC, id ASC LIMIT 1",
            tuple(names)
        ) as cur:
            row = await cur.fetchone()
    return str(row['reply_text']) if row else None

async def execute(db_path, ai_result, context=None):
    context = context or {}
    current_entity = ai_result.get('entity')
    previous_entity = context.get('entity')
    intents = ai_result.get('intents', [])
    action = choose_action(intents, current_entity or previous_entity)
    if action not in ALLOWED_ACTIONS:
        action = 'generic_reply'

    customer_message = context.get('customer_message', '')

    # A numeric offer / negotiation follow-up may change the action after the
    # initial intent selection. Resolve that BEFORE checking Saved Actions so
    # the final action is always permission-checked.
    names = {x.get('name') for x in intents if isinstance(x, dict)}
    if previous_entity and (
        'negotiation' in names or
        ('purchase_intent' in names and _offer(customer_message) is not None) or
        ('price_inquiry' in names and _offer(customer_message) is not None)
    ):
        action = 'negotiate'

    # Enforce the admin-approved action list on the FINAL action. The AI
    # response itself is never trusted to bypass this gate.
    saved = await _saved_actions(db_path)
    if not saved.get(action, True):
        return {
            'action': 'generic_reply',
            'entity': current_entity or previous_entity,
            'record': None,
            'response': 'I can help with that, but this action is currently unavailable. Please contact the admin.'
        }

    entity = current_entity or previous_entity
    record = await get_entity(db_path, entity) if entity else None

    # If the AI supplied an invalid entity on a follow-up, fall back to the
    # last verified entity instead of losing the conversation context.
    if action == 'negotiate' and previous_entity and not record:
        fallback_record = await get_entity(db_path, previous_entity)
        if fallback_record:
            entity = previous_entity
            record = fallback_record

    if entity and not record:
        return {'action':'generic_reply','entity':None,'record':None,
                'response':"Sorry, I couldn't find that exact batch/course in our available data."}

    if action == 'negotiate':
        rule = await _rule(db_path)
        return {'action':'negotiate','entity':entity,'record':record,
                'response':negotiation_response(rule, record, customer_message)}

    if action == 'generic_reply':
        approved = await _generic_reply(db_path, intents)
        return {
            'action': 'generic_reply',
            'entity': entity,
            'record': record,
            'response': approved or ai_result.get('response') or 'Sorry, I could not find that information.'
        }

    # Customer-facing product replies are built from authoritative database fields.
    # Do not expose discussion/access-chat links here. Actual course/access links
    # are intentionally handled by a separate future criterion.
    if action in ('show_batch', 'show_course') and record:
        name = str(record.get('name') or '').strip()
        fee = str(record.get('fee') or '').strip()
        duration = str(record.get('duration') or '').strip()
        timing = str(record.get('timing') or '').strip()
        demo = str(record.get('demo_link') or '').strip() if action == 'show_batch' else ''
        purchase = str(record.get('pay_link') or '').strip() if action == 'show_batch' else str(record.get('source_link') or '').strip()
        lines = [f'{name} available hai.']
        if fee: lines.append(f'Fee: ₹{fee}' if not fee.startswith('₹') else f'Fee: {fee}')
        if duration:
            access = duration
            if timing:
                access = f'{duration} ({timing})'
            lines.append(f'Access: {access}')
        if demo: lines.append(f'Demo: {demo}')
        if purchase: lines.append(f'Purchase Link: {purchase}')
        return {'action':action,'entity':entity,'record':record,'response':'\n'.join(lines)}

    if action == 'send_demo' and record:
        demo = str(record.get('demo_link') or '').strip() if record.get('demo_link') is not None else ''
        purchase = str(record.get('pay_link') or '').strip() if record.get('pay_link') is not None else ''
        lines = []
        if demo: lines.append(f'Demo: {demo}')
        if purchase: lines.append(f'Purchase Link: {purchase}')
        return {'action':action,'entity':entity,'record':record,'response':'\n'.join(lines) or 'Demo is not available for this batch.'}

    if action in ('send_payment', 'send_purchase_link') and record:
        purchase = str(record.get('pay_link') or '').strip() if record.get('pay_link') is not None else str(record.get('source_link') or '').strip()
        if purchase:
            return {'action':action,'entity':entity,'record':record,'response':f'Purchase Link: {purchase}'}
        return {'action':action,'entity':entity,'record':record,'response':'Purchase link is not available for this batch/course yet.'}

    return {'action':action,'entity':entity,'record':record,
            'response':ai_result.get('response') or 'Sorry, I could not find that information.'}
