from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .payment_architecture import (
    _items_from_text,
    _merge_selected,
    _state_get as _payment_state_get,
    begin_payment_for_selected,
)
from .reply_context import (
    get_reference as _reply_reference_get,
    get_target_anchor as _reply_target_anchor,
    save_message_context as _reply_message_context_save,
    is_reference_followup as _reply_is_followup,
)
from .state_reply_anchor import get_anchor as _state_reply_anchor_get

logger = logging.getLogger(__name__)

MARKER = "NEGOTIATION_LIST_AI_V34"
BURST_GAP_SECONDS = 12.0
STATE_TTL_SECONDS = 45 * 60
MAX_BURST_MESSAGES = 8
MAX_BURST_CHARS = 1200

_LOCKS: dict[str, asyncio.Lock] = {}
_TASKS: dict[str, asyncio.Task] = {}
_TASK_GENERATION: dict[str, int] = {}

# Layer-A: fixed-price products.  No discount is ever allowed.
LAYER_A_PRODUCTS = {
    "PRO_PACK": {
        "name": "Pro Pack",
        "price": 1499,
        "profit": 500,
        "key": "neg_combo:pro_pack",
    },
    "TOP_FACULTY": {
        "name": "Top Faculty",
        "price": 999,
        "profit": 250,
        "key": "neg_combo:top_faculty",
    },
}

NEGOTIATION_RE = re.compile(
    r"(?:\bdiscount\b|\bnegotia\w*\b|\b(?:price|rate|amount)\s*(?:kam|less|lower|reduce)\b|"
    r"\b(?:kam|less|lower|reduce)\s*(?:price|rate|amount)?\b|\bthoda\s+(?:aur\s+)?kam\b|"
    r"\baur\s+kam\b|\bbest\s+price\b|\blowest\s*(?:price|rate)?\b|\blast\s+price\b|\bfinal\s+(?:price|rate)\b|"
    r"\bfinancial\b|\bbudget\b|\bafford\w*\b|\bexpensive\b|\bmehenga\b|\bmahinga\b|"
    r"\bpaise?\s+(?:kam|nahi|nhi)\b|\bstudent\s+(?:hu|hun|hoon|h|hai|i\s+am)\b|"
    r"\bi\s+am\s+(?:a\s+)?student\b|\bplease\s+(?:bro\s+)?(?:discount|kam|reduce)\b)",
    re.I,
)

FAQ_RE = re.compile(
    r"\b(?:latest|updated|update|validity|lifetime|notes?|pdf|material|demo|sample|proof|"
    r"genuine|authentic|original|download|phone|mobile|laptop|device|group|groups|optional|"
    r"lecture|lectures|faculty|teacher|content|included|include|year|2026|2027)\b",
    re.I,
)

# NEGOTIATION_GENERIC_FAQ_AI_V33
# These are clearly property/access questions about the already active course.
# Unlike words such as "teacher", "faculty" or "optional", these terms must
# never be allowed to fall through to catalogue clarification during an active
# negotiation merely because the AI classifier is uncertain.
KNOWN_CONTEXT_FAQ_RE = re.compile(
    r"\b(?:latest|updated|update|validity|lifetime|notes?|pdf|material|demo|sample|proof|"
    r"genuine|authentic|original|download|phone|mobile|laptop|device|group|groups|"
    r"lecture|lectures|content|included|include|year|2026|2027)\b",
    re.I,
)

POSTPONE_RE = re.compile(
    r"(?:\bbaad\s+me\b|\bbad\s+me\b|\blater\b|\bnot\s+now\b|\babhi\s+(?:paise|paisa)\s+"
    r"(?:nahi|nhi)\b|\bpaise?\s+(?:nahi|nhi)\s+(?:hai|h)\b|\bparents?\s+se\s+(?:puch|pooch)|"
    r"\bask\s+(?:my\s+)?parents?\b|\bnext\s+month\b|\bsalary\s+(?:aane|ane)\b|"
    r"\bsoch\s+ke\b|\bthink\s+about\s+it\b|\bwill\s+buy\s+later\b|\bpayment\s+later\b)",
    re.I,
)

PAYMENT_RE = re.compile(
    r"\b(?:payment|pay|phone\s*pe|phonepe|gpay|google\s*pay|paytm|amazon\s*pay|"
    r"payment\s+link|pay\s+link|qr|upi|gift\s*card)\b",
    re.I,
)

ACCEPT_RE = re.compile(
    r"(?:\b(?:okay|ok|done|deal|fine|yes|haan|han|haa|thik|theek)\b|"
    r"\b(?:le\s+raha|le\s+rahi|lunga|lungi|buy|purchase|take\s+it)\b)",
    re.I,
)

SELECTION_RE = re.compile(
    r"\b(?:chahiye|chahie|chahiyeh|need|want|wants|lena|leni|lunga|lungi|buy|purchase|"
    r"select|choose|add|include|bhi|also)\b",
    re.I,
)

ADDITIVE_RE = re.compile(
    r"\b(?:bhi|also|aur|and|dono|both|add|include)\b|\+|&",
    re.I,
)

# NEGOTIATION_LIST_AI_V34
REMOVE_RE = re.compile(
    r"(?:\b(?:remove|delete|drop|hata(?:o|do)?|nikal(?:o|do)?|nahi\s+chahiye|nhi\s+chahiye|not\s+needed|dont\s+want|don't\s+want)\b|"
    r"\b(?:nahi|nhi)\b(?=.*\b(?:chahiye|want|need|rakh|keep|add|kar\s*do)\b))",
    re.I,
)
ONLY_RE = re.compile(r"\b(?:sirf|only|bas|just|baki\s+dono|baaki\s+dono|rest\s+two|keep\s+only|(?:hi|hee)\s+rakh)\b", re.I)
REPLACE_RE = re.compile(
    r"(?:\b(?:replace|instead|jagah|ki\s+jagah|ke\s+jagah|hata\s+ke|hata\s+kar|nahi\s*,?|nhi\s*,?)\b.*"
    r"\b(?:chahiye|want|need|kar\s+do|kardo|add)\b)",
    re.I,
)
MULTI_SELECT_RE = re.compile(
    r"(?:\b(?:dono|both|teeno|three|sab|all)\b|"
    r"\b(?:ye|isko|is\s*ko|wala|wali)\s+bhi\b|"
    r"\b(?:bhi|also)\s+(?:chahiye|chahie|lunga|lungi|lena|want|need)\b|"
    r"\b(?:aur|and|&)\b.*\b(?:chahiye|chahie|lunga|lungi|lena|want|need)\b)",
    re.I,
)
RECENT_NEGOTIATION_TTL_SECONDS = 2 * 60 * 60
MAX_RECENT_NEGOTIATED_ITEMS = 12

COMBINED_PRICE_RE = re.compile(
    r"(?:\b(?:dono|both|sab|all|together|combined)\b.*\b(?:price|kitna|kitne|amount|total)\b|"
    r"\b(?:price|kitna|kitne|amount|total)\b.*\b(?:dono|both|sab|all|together|combined)\b|"
    r"\bmila\s*ke\s+(?:kitna|kitne)\b)",
    re.I,
)

PRICE_QUESTION_RE = re.compile(
    r"\b(?:price|cost|fees?|amount|rate|kitne\s+ka|kitna\s+ka|how\s+much)\b",
    re.I,
)

ENGLISH_HINT_RE = re.compile(
    r"\b(?:can|could|would|please|price|discount|student|budget|payment|later|parents?|"
    r"lectures?|course|batch|need|want|give|reduce|lower|final|best|afford|expensive)\b",
    re.I,
)

HINGLISH_HINT_RE = re.compile(
    r"\b(?:bhai|bro|yaar|kam|karo|kar\s+do|de\s+do|dedo|chahiye|hai|hain|hu|hun|hoon|"
    r"paise|paisa|abhi|baad|puch|pooch|milega|milenge|sakta|nahi|nhi|thik|theek)\b",
    re.I,
)


def _runtime_path(production_db_path: str) -> str:
    return str(Path(production_db_path).with_name("negotiation_runtime.sqlite3"))


def _lock(chat_id: Any) -> asyncio.Lock:
    key = str(chat_id)
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


def _con(path: str):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return db


def ensure_schema(production_db_path: str):
    runtime = _runtime_path(production_db_path)
    with _con(runtime) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS negotiation_state(
                chat_id TEXT PRIMARY KEY,
                cart_json TEXT NOT NULL DEFAULT '[]',
                layer TEXT NOT NULL DEFAULT '',
                original_total INTEGER NOT NULL DEFAULT 0,
                current_offer INTEGER NOT NULL DEFAULT 0,
                floor_price INTEGER NOT NULL DEFAULT 0,
                negotiation_round INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                accepted_price INTEGER NOT NULL DEFAULT 0,
                language TEXT NOT NULL DEFAULT '',
                last_seller_reply TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS negotiation_burst(
                chat_id TEXT PRIMARY KEY,
                messages_json TEXT NOT NULL DEFAULT '[]',
                first_at REAL NOT NULL DEFAULT 0,
                last_at REAL NOT NULL DEFAULT 0,
                generation INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS negotiation_ai_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                intent TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                customer_offer INTEGER,
                financial_issue INTEGER NOT NULL DEFAULT 0,
                immediate_payment INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS negotiation_recent_item(
                chat_id TEXT NOT NULL,
                target_key TEXT NOT NULL,
                target_name TEXT NOT NULL DEFAULT '',
                catalog_price INTEGER NOT NULL DEFAULT 0,
                current_price INTEGER NOT NULL DEFAULT 0,
                negotiation_round INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                phrases_json TEXT NOT NULL DEFAULT '[]',
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id,target_key)
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_neg_recent_chat_updated ON negotiation_recent_item(chat_id,updated_at DESC)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS negotiation_cart_ai_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                customer_query TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                final_keys_json TEXT NOT NULL DEFAULT '[]',
                route_to_matcher INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        db.commit()
    return runtime


def _empty_state():
    return {
        "cart": [],
        "layer": "",
        "original_total": 0,
        "current_offer": 0,
        "floor_price": 0,
        "negotiation_round": 0,
        "status": "",
        "accepted_price": 0,
        "language": "",
        "last_seller_reply": "",
        "updated_at": 0.0,
    }


def _load_state(production_db_path: str, chat_id: Any):
    runtime = ensure_schema(production_db_path)
    with _con(runtime) as db:
        row = db.execute(
            "SELECT * FROM negotiation_state WHERE chat_id=? LIMIT 1",
            (str(chat_id),),
        ).fetchone()
    if not row:
        return _empty_state()
    updated = float(row["updated_at"] or 0)
    if not updated or time.time() - updated > STATE_TTL_SECONDS:
        return _empty_state()
    try:
        cart = json.loads(row["cart_json"] or "[]")
        if not isinstance(cart, list):
            cart = []
    except Exception:
        cart = []
    return {
        "cart": cart,
        "layer": str(row["layer"] or ""),
        "original_total": int(row["original_total"] or 0),
        "current_offer": int(row["current_offer"] or 0),
        "floor_price": int(row["floor_price"] or 0),
        "negotiation_round": int(row["negotiation_round"] or 0),
        "status": str(row["status"] or ""),
        "accepted_price": int(row["accepted_price"] or 0),
        "language": str(row["language"] or ""),
        "last_seller_reply": str(row["last_seller_reply"] or ""),
        "updated_at": updated,
    }


def _save_state(production_db_path: str, chat_id: Any, state: dict):
    runtime = ensure_schema(production_db_path)
    state = dict(_empty_state(), **dict(state or {}))
    state["updated_at"] = time.time()
    with _con(runtime) as db:
        db.execute(
            """
            INSERT INTO negotiation_state(
                chat_id,cart_json,layer,original_total,current_offer,floor_price,
                negotiation_round,status,accepted_price,language,last_seller_reply,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                cart_json=excluded.cart_json,
                layer=excluded.layer,
                original_total=excluded.original_total,
                current_offer=excluded.current_offer,
                floor_price=excluded.floor_price,
                negotiation_round=excluded.negotiation_round,
                status=excluded.status,
                accepted_price=excluded.accepted_price,
                language=excluded.language,
                last_seller_reply=excluded.last_seller_reply,
                updated_at=excluded.updated_at
            """,
            (
                str(chat_id),
                json.dumps(state["cart"], ensure_ascii=False, separators=(",", ":")),
                state["layer"],
                int(state["original_total"] or 0),
                int(state["current_offer"] or 0),
                int(state["floor_price"] or 0),
                int(state["negotiation_round"] or 0),
                state["status"],
                int(state["accepted_price"] or 0),
                state["language"],
                state["last_seller_reply"],
                float(state["updated_at"]),
            ),
        )
        db.commit()
    try:
        _remember_recent_state(production_db_path, chat_id, state)
    except Exception:
        logger.exception("%s recent-ledger save failed chat=%s", MARKER, chat_id)
    return state


def _save_burst(production_db_path: str, chat_id: Any, messages: list[str], first_at: float, last_at: float, generation: int):
    runtime = ensure_schema(production_db_path)
    with _con(runtime) as db:
        db.execute(
            """
            INSERT INTO negotiation_burst(chat_id,messages_json,first_at,last_at,generation)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                messages_json=excluded.messages_json,
                first_at=excluded.first_at,
                last_at=excluded.last_at,
                generation=excluded.generation
            """,
            (
                str(chat_id),
                json.dumps(messages, ensure_ascii=False),
                float(first_at),
                float(last_at),
                int(generation),
            ),
        )
        db.commit()


def _load_burst(production_db_path: str, chat_id: Any):
    runtime = ensure_schema(production_db_path)
    with _con(runtime) as db:
        row = db.execute(
            "SELECT * FROM negotiation_burst WHERE chat_id=? LIMIT 1",
            (str(chat_id),),
        ).fetchone()
    if not row:
        return None
    try:
        messages = json.loads(row["messages_json"] or "[]")
        if not isinstance(messages, list):
            messages = []
    except Exception:
        messages = []
    return {
        "messages": [str(x) for x in messages],
        "first_at": float(row["first_at"] or 0),
        "last_at": float(row["last_at"] or 0),
        "generation": int(row["generation"] or 0),
    }


def _clear_burst(production_db_path: str, chat_id: Any):
    runtime = ensure_schema(production_db_path)
    with _con(runtime) as db:
        db.execute("DELETE FROM negotiation_burst WHERE chat_id=?", (str(chat_id),))
        db.commit()


def _total(cart: list[dict]) -> int:
    return sum(max(0, int(x.get("price") or 0)) for x in (cart or []))


def _layer_for_cart(cart: list[dict]) -> str:
    if len(cart or []) == 1:
        key = str(cart[0].get("key") or "")
        if key in {"neg_combo:pro_pack", "neg_combo:top_faculty"}:
            return "A"
        name = str(cart[0].get("name") or "").casefold()
        if name in {"pro pack", "top faculty"}:
            return "A"
    return "B" if cart else ""


def _policy(cart: list[dict]):
    total = _total(cart)
    layer = _layer_for_cart(cart)
    if layer == "A":
        return layer, total, total

    # NEGOTIATION_PRICE_POLICY_V34
    # A single normal course at ₹200 or below remains fixed. A finalized
    # multi-course list is a fresh commercial object and gets the three-stage
    # bundle ladder whenever its combined total is above ₹200.
    if len(cart or []) > 1:
        max_discount = 100 if total > 200 else 0
    else:
        max_discount = 100 if any(
            max(0, int(x.get("price") or 0)) > 200
            for x in (cart or [])
        ) else 0
    floor = max(0, total - max_discount)
    return layer, total, floor


def _normalize_policy_state(production_db_path: str, chat_id: Any, state: dict):
    cart = list(state.get("cart") or [])
    if not cart:
        return state
    layer, total, floor = _policy(cart)
    changed = (
        str(state.get("layer") or "") != layer
        or int(state.get("original_total") or 0) != total
        or int(state.get("floor_price") or 0) != floor
    )
    state["layer"] = layer
    state["original_total"] = total
    state["floor_price"] = floor
    current = int(state.get("current_offer") or total)
    # Never retain an old offer below the newly-authoritative floor.
    state["current_offer"] = min(total, max(floor, current))
    if int(state.get("accepted_price") or 0) and int(state.get("accepted_price") or 0) < floor:
        state["accepted_price"] = 0
        state["status"] = "SELECTED"
        changed = True
    if changed:
        _save_state(production_db_path, chat_id, state)
    return state


def _canonical_combo(text: str):
    n = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if re.search(r"\b(?:pro\s*pack|propack|pro\s*combo|all\s+coaching\s+combo|1499\s+package)\b", n):
        return dict(LAYER_A_PRODUCTS["PRO_PACK"])
    if re.search(r"\b(?:top\s*faculty|topfaculty|top\s*faculty\s+combo)\b", n):
        return dict(LAYER_A_PRODUCTS["TOP_FACULTY"])
    return None


def _selection_items(production_db_path: str, text: str):
    combo = _canonical_combo(text)
    if combo:
        return [combo]
    try:
        return _items_from_text(production_db_path, text)
    except Exception:
        logger.exception("%s item resolver failed text=%r", MARKER, text)
        return []


def _same_cart(a: list[dict], b: list[dict]) -> bool:
    ka = [str(x.get("key") or x.get("name") or "") for x in (a or [])]
    kb = [str(x.get("key") or x.get("name") or "") for x in (b or [])]
    return ka == kb


def _price_int(value):
    raw = str(value or "").replace(",", "")
    m = re.search(r"(\d+)", raw)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def _candidate_key(item: dict | None) -> str:
    item = item or {}
    return str(item.get("key") or "").strip()


def _remember_recent_state(production_db_path: str, chat_id: Any, state: dict):
    """Persist a compact recent-negotiation ledger per target."""
    cart = [dict(x) for x in (state.get("cart") or []) if isinstance(x, dict)]
    if not cart:
        return
    now = time.time()
    runtime = ensure_schema(production_db_path)
    multi = len(cart) > 1
    current_single = int(state.get("accepted_price") or state.get("current_offer") or state.get("original_total") or 0)
    with _con(runtime) as db:
        for item in cart:
            key = _candidate_key(item)
            name = str(item.get("name") or "").strip()
            if not key or not name:
                continue
            catalog_price = int(item.get("catalog_price") or item.get("price") or 0)
            current_price = int(item.get("price") or 0) if multi else current_single
            if current_price <= 0:
                current_price = catalog_price
            try:
                phrases_json = json.dumps(list(item.get("phrases") or []), ensure_ascii=False, separators=(",", ":"))
            except Exception:
                phrases_json = "[]"
            db.execute(
                """
                INSERT INTO negotiation_recent_item(
                    chat_id,target_key,target_name,catalog_price,current_price,
                    negotiation_round,status,phrases_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,target_key) DO UPDATE SET
                    target_name=excluded.target_name,
                    catalog_price=CASE WHEN excluded.catalog_price>0 THEN excluded.catalog_price ELSE negotiation_recent_item.catalog_price END,
                    current_price=CASE WHEN excluded.current_price>0 THEN excluded.current_price ELSE negotiation_recent_item.current_price END,
                    negotiation_round=CASE WHEN excluded.negotiation_round>negotiation_recent_item.negotiation_round THEN excluded.negotiation_round ELSE negotiation_recent_item.negotiation_round END,
                    status=excluded.status,
                    phrases_json=CASE WHEN excluded.phrases_json!='[]' THEN excluded.phrases_json ELSE negotiation_recent_item.phrases_json END,
                    updated_at=excluded.updated_at
                """,
                (
                    str(chat_id), key, name, max(0, catalog_price), max(0, current_price),
                    int(state.get("negotiation_round") or 0) if not multi else 0,
                    str(state.get("status") or ""), phrases_json, now,
                ),
            )
        rows = db.execute(
            "SELECT target_key FROM negotiation_recent_item WHERE chat_id=? ORDER BY updated_at DESC",
            (str(chat_id),),
        ).fetchall()
        for row in rows[MAX_RECENT_NEGOTIATED_ITEMS:]:
            db.execute(
                "DELETE FROM negotiation_recent_item WHERE chat_id=? AND target_key=?",
                (str(chat_id), str(row["target_key"])),
            )
        db.commit()


def _load_recent_items(production_db_path: str, chat_id: Any) -> list[dict]:
    runtime = ensure_schema(production_db_path)
    cutoff = time.time() - RECENT_NEGOTIATION_TTL_SECONDS
    out = []
    with _con(runtime) as db:
        rows = db.execute(
            """
            SELECT * FROM negotiation_recent_item
            WHERE chat_id=? AND updated_at>=?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (str(chat_id), cutoff, MAX_RECENT_NEGOTIATED_ITEMS),
        ).fetchall()
    for row in rows:
        try:
            phrases = json.loads(row["phrases_json"] or "[]")
            if not isinstance(phrases, list):
                phrases = []
        except Exception:
            phrases = []
        out.append({
            "key": str(row["target_key"] or ""),
            "name": str(row["target_name"] or ""),
            "price": int(row["current_price"] or row["catalog_price"] or 0),
            "catalog_price": int(row["catalog_price"] or 0),
            "current_price": int(row["current_price"] or 0),
            "negotiation_round": int(row["negotiation_round"] or 0),
            "status": str(row["status"] or ""),
            "phrases": phrases,
            "source": "recent",
        })
    return out


def _current_cart_candidates(state: dict) -> list[dict]:
    cart = [dict(x) for x in (state.get("cart") or []) if isinstance(x, dict)]
    if len(cart) == 1:
        amount = int(state.get("accepted_price") or state.get("current_offer") or state.get("original_total") or cart[0].get("price") or 0)
        cart[0]["catalog_price"] = int(cart[0].get("catalog_price") or cart[0].get("price") or 0)
        if amount > 0:
            cart[0]["price"] = amount
        cart[0]["current_price"] = int(cart[0].get("price") or 0)
        cart[0]["negotiation_round"] = int(state.get("negotiation_round") or 0)
    else:
        for item in cart:
            item["catalog_price"] = int(item.get("catalog_price") or item.get("price") or 0)
            item["current_price"] = int(item.get("price") or 0)
    for item in cart:
        item["source"] = "current"
    return cart


def _cart_edit_candidates(production_db_path: str, chat_id: Any, state: dict, explicit_items: list[dict], ref: dict | None):
    by_key: dict[str, dict] = {}

    def put(item: dict, source: str):
        if not isinstance(item, dict):
            return
        key = str(item.get("key") or "").strip()
        name = str(item.get("name") or "").strip()
        if not key or not name:
            return
        old = by_key.get(key)
        x = dict(old or {})
        x.update(dict(item))
        if old and str(old.get("source") or "") in {"recent", "current"} and source == "saved":
            # Saved context supplies identity only; negotiated ledger/cart
            # prices remain authoritative for AI cart calculations.
            x["price"] = int(old.get("price") or item.get("price") or 0)
            x["current_price"] = int(old.get("current_price") or old.get("price") or 0)
            x["catalog_price"] = int(old.get("catalog_price") or item.get("price") or 0)
        x["key"] = key
        x["name"] = name
        if old and str(old.get("source") or "") in {"recent", "current"} and source == "explicit":
            source = str(old.get("source") or source)
        x["source"] = source
        x["price"] = int(x.get("current_price") or x.get("price") or 0)
        x["catalog_price"] = int(x.get("catalog_price") or x.get("price") or 0)
        by_key[key] = x

    for item in _load_recent_items(production_db_path, chat_id):
        put(item, "recent")
    for item in _current_cart_candidates(state):
        put(item, "current")
    # The structured matcher can briefly leave only the latest target in the
    # negotiation cart. Recover other active courses from the saved customer
    # context so explicit multi-select wording ("dono lunga") is interpreted
    # with the complete conversation state.
    try:
        with _con(production_db_path) as db:
            row = db.execute(
                "SELECT active_targets_json FROM unified_customer_context WHERE chat_id=? LIMIT 1",
                (str(chat_id),),
            ).fetchone()
        targets = json.loads(row["active_targets_json"] or "[]") if row else []
        for target in targets if isinstance(targets, list) else []:
            if not isinstance(target, dict):
                continue
            key = str(target.get("key") or "").strip()
            name = str(target.get("name") or "").strip()
            price = _price_int(target.get("price"))
            if key and name and price > 0:
                put({"key": key, "name": name, "price": price, "phrases": []}, "saved")
    except Exception:
        logger.exception("%s active target candidate recovery failed chat=%s", MARKER, chat_id)
    for item in explicit_items or []:
        key = str(item.get("key") or "").strip()
        if key and key in by_key:
            merged = dict(item)
            merged["current_price"] = int(by_key[key].get("price") or item.get("price") or 0)
            merged["catalog_price"] = int(item.get("price") or by_key[key].get("catalog_price") or 0)
            put(merged, str(by_key[key].get("source") or "explicit"))
        else:
            y = dict(item)
            y["catalog_price"] = int(y.get("price") or 0)
            y["current_price"] = int(y.get("price") or 0)
            put(y, "explicit")
    if ref and len(cart) <= 1:
        key = str(ref.get("target_key") or "").strip()
        name = str(ref.get("target_name") or ref.get("reference_label") or "").strip()
        price = int(ref.get("current_price") or ref.get("target_price") or 0)
        if key and name and price > 0:
            if key in by_key:
                rr = dict(by_key[key])
                if str(rr.get("source") or "") not in {"recent", "current"}:
                    rr["price"] = price
                put(rr, str(rr.get("source") or "reply"))
            else:
                put({
                    "key": key, "name": name, "price": price,
                    "catalog_price": int(ref.get("target_price") or price),
                    "current_price": price, "phrases": [],
                }, "reply")
    return list(by_key.values())


def _norm_cart_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _candidate_mentions(raw: str, candidates: list[dict]) -> list[str]:
    text = " " + _norm_cart_text(raw) + " "
    scored = []
    stop = {"sir","mam","maam","ias","batch","course","the","and","aur","wala","wali"}
    for item in candidates:
        key = str(item.get("key") or "")
        forms = [str(item.get("name") or "")] + [str(x) for x in (item.get("phrases") or [])]
        best = 0
        for form in forms:
            toks = [t for t in _norm_cart_text(form).split() if len(t) >= 3 and t not in stop]
            hits = sum(1 for t in set(toks) if f" {t} " in text)
            best = max(best, hits)
        if " optional " in text and "optional" in _norm_cart_text(item.get("name")):
            best = max(best, 1)
        if best:
            scored.append((best, key))
    if not scored:
        return []
    max_score = max(x[0] for x in scored)
    return [k for score, k in scored if score == max_score or score >= 2]


def _looks_like_cart_edit(raw: str, state: dict, ref: dict | None, explicit_items: list[dict], recent_items: list[dict]) -> bool:
    text = str(raw or "")
    if not (state.get("cart") or recent_items or ref):
        return False
    if NEGOTIATION_RE.search(text) and not re.search(r"\b(?:chahiye|chahie|want|need|add|include|remove|hata|replace|sirf|only|dono|both|teeno)\b", text, re.I):
        return False
    if REMOVE_RE.search(text) or ONLY_RE.search(text) or REPLACE_RE.search(text) or MULTI_SELECT_RE.search(text):
        return True
    if ref and re.search(r"\b(?:bhi|also|add|include)\b", text, re.I):
        return True
    return False


def _fallback_cart_intent(raw: str, current_keys: list[str], candidates: list[dict], ref: dict | None):
    by_key = {str(x.get("key") or ""): x for x in candidates}
    final = [k for k in current_keys if k in by_key]
    mentioned = _candidate_mentions(raw, candidates)
    ref_key = str((ref or {}).get("target_key") or "").strip()
    if ref_key and ref_key in by_key and re.search(r"\b(?:bhi|also|add|include|chahiye|want|need)\b", raw, re.I):
        if ref_key not in mentioned:
            mentioned.append(ref_key)
    if re.search(r"\b(?:dono|both)\b", raw, re.I):
        recentish = [str(x.get("key") or "") for x in candidates if str(x.get("source") or "") in {"recent","current","reply"}]
        recentish = list(dict.fromkeys(k for k in recentish if k))
        if len(recentish) == 2:
            mentioned = recentish
    if ONLY_RE.search(raw):
        # In phrases such as “Basava wala nahi chahiye, optional hi rakh do”,
        # the negative clause identifies the item to remove while the latter
        # clause identifies the item to keep. Preserve any current item that
        # is not the explicitly rejected target.
        if REMOVE_RE.search(raw) and current_keys:
            negative_part = re.split(r"\b(?:nahi|nhi)\b", raw, maxsplit=1, flags=re.I)[0]
            removed = set(_candidate_mentions(negative_part, candidates))
            final = [k for k in current_keys if k not in removed]
            for k in mentioned:
                if k not in removed and k not in final:
                    final.append(k)
        else:
            final = list(dict.fromkeys(mentioned))
        action = "KEEP_ONLY"
    elif REMOVE_RE.search(raw) or REPLACE_RE.search(raw):
        action = "REPLACE" if REPLACE_RE.search(raw) else "REMOVE"
        if len(mentioned) == 1 and action == "REMOVE":
            final = [k for k in final if k != mentioned[0]]
        else:
            return {"action": action, "final_keys": final, "route_to_matcher": True, "confidence": "low"}
    else:
        action = "ADD"
        for k in mentioned:
            if k not in final:
                final.append(k)
    return {"action": action, "final_keys": final, "route_to_matcher": False, "confidence": "fallback"}


async def _cart_intent_with_ai(context, production_db_path: str, chat_id: Any, raw: str, state: dict, ref: dict | None, candidates: list[dict]):
    current_keys = [str(x.get("key") or "") for x in (state.get("cart") or []) if str(x.get("key") or "")]
    fallback = _fallback_cart_intent(raw, current_keys, candidates, ref)
    ai = context.application.bot_data.get("ai")
    client = getattr(ai, "client", None) if ai is not None else None
    model = str(getattr(ai, "model", "") or "") if ai is not None else ""
    if client is None or not model:
        return fallback
    compact_candidates = [{
        "key": str(item.get("key") or ""),
        "name": str(item.get("name") or ""),
        "current_price": int(item.get("price") or 0),
        "catalog_price": int(item.get("catalog_price") or item.get("price") or 0),
        "negotiation_round": int(item.get("negotiation_round") or 0),
        "source": str(item.get("source") or ""),
    } for item in candidates[:MAX_RECENT_NEGOTIATED_ITEMS + 6]]
    reply_ref = None
    if ref:
        reply_ref = {
            "target_key": str(ref.get("target_key") or ""),
            "target_name": str(ref.get("target_name") or ref.get("reference_label") or ""),
            "current_price": int(ref.get("current_price") or ref.get("target_price") or 0),
        }
    bundle_state = {
        "round": int(state.get("negotiation_round") or 0),
        "original_total": int(state.get("original_total") or 0),
        "current_offer": int(state.get("current_offer") or 0),
        "status": str(state.get("status") or ""),
    }
    prompt = (
        "Interpret ONE Telegram customer message that may edit a selected course list. "
        "Return JSON only with keys action, final_keys, route_to_matcher, confidence. "
        "action must be ADD, REMOVE, REPLACE, KEEP_ONLY, SET_LIST, or NONE. "
        "final_keys must contain ONLY exact key values from CANDIDATES. Never invent a key, course, teacher, subject, or price. "
        "Use CURRENT_CART as the starting list. RECENT candidates are recently negotiated courses and their current_price is authoritative. "
        "REPLY_REFERENCE tells what ye/isko/this refers to. "
        "bhi/also/ye bhi/add means preserve existing selected items and ADD the referenced or mentioned item. "
        "dono/both/teeno/all means the customer wants multiple referenced or mentioned recent items together. "
        "X nahi chahiye/remove X/hata do removes X but preserves unrelated selected items. "
        "X nahi, Y chahiye or X ki jagah Y replaces X with Y while preserving unrelated selected items. "
        "sirf/only keeps only the positively requested items. "
        "A short generic label such as optional should resolve to a uniquely relevant RECENT negotiated Optional course when recent context makes it clear. "
        "Do not treat aur kam bargaining as an add action. Do not calculate discounts. "
        "Set route_to_matcher=true only if the message clearly needs a new course NOT represented in CANDIDATES. "
        "If route_to_matcher=true, do not delete understood current items.\n"
        f"CURRENT_CART={json.dumps(current_keys, ensure_ascii=False)}\n"
        f"BUNDLE_NEGOTIATION_STATE={json.dumps(bundle_state, ensure_ascii=False)}\n"
        f"REPLY_REFERENCE={json.dumps(reply_ref, ensure_ascii=False)}\n"
        f"CANDIDATES={json.dumps(compact_candidates, ensure_ascii=False)}\n"
        f"CUSTOMER_QUERY={raw}"
    )
    try:
        resp = await client.responses.create(
            model=model,
            instructions="Compact Telegram cart-intent interpreter. JSON only.",
            input=prompt,
            max_output_tokens=180,
        )
        out = str(getattr(resp, "output_text", "") or "").strip()
        try:
            data = json.loads(out)
        except Exception:
            m = re.search(r"\{.*\}", out, re.S)
            data = json.loads(m.group(0)) if m else {}
        action = str(data.get("action") or "NONE").upper()
        if action not in {"ADD","REMOVE","REPLACE","KEEP_ONLY","SET_LIST","NONE"}:
            action = "NONE"
        allowed = {str(x.get("key") or "") for x in candidates if str(x.get("key") or "")}
        final_keys = []
        for key in data.get("final_keys") or []:
            k = str(key or "").strip()
            if k in allowed and k not in final_keys:
                final_keys.append(k)
        route = bool(data.get("route_to_matcher"))
        confidence = str(data.get("confidence") or "").lower()
        # Never carry a stale combo into a named-course cart unless the user
        # explicitly requests that combo in the current message.
        combo_keys = {"neg_combo:pro_pack", "neg_combo:top_faculty"}
        if not re.search(r"\b(?:pro\s*pack|top\s*faculty|combo|all\s+coaching)\b", raw, re.I):
            final_keys = [k for k in final_keys if k not in combo_keys]
        # AI is authoritative for ambiguous multi-course and negative wording;
        # server-side validation below still restricts results to candidates.
        if route and fallback.get("action") in {"REMOVE", "KEEP_ONLY"}:
            return fallback
        if not final_keys and current_keys and action not in {"REMOVE","KEEP_ONLY"}:
            return fallback
        runtime = ensure_schema(production_db_path)
        usage = getattr(resp, "usage", None)
        inp = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        outtok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        total = int(getattr(usage, "total_tokens", 0) or 0) if usage else inp + outtok
        with _con(runtime) as db:
            db.execute(
                """
                INSERT INTO negotiation_cart_ai_audit(
                    chat_id,customer_query,action,final_keys_json,route_to_matcher,
                    model,input_tokens,output_tokens,total_tokens,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (str(chat_id), raw[:1200], action, json.dumps(final_keys, ensure_ascii=False), int(route), model, inp, outtok, total, time.time()),
            )
            db.commit()
        logger.info("%s cart AI chat=%s action=%s final=%r route=%s confidence=%s", MARKER, chat_id, action, final_keys, route, confidence)
        return {"action": action, "final_keys": final_keys, "route_to_matcher": route, "confidence": confidence or "ai"}
    except Exception:
        logger.exception("%s cart AI failed chat=%s", MARKER, chat_id)
        return fallback


def _apply_cart_final_keys(production_db_path: str, chat_id: Any, state: dict, candidates: list[dict], final_keys: list[str]):
    by_key = {str(x.get("key") or ""): dict(x) for x in candidates if str(x.get("key") or "")}
    cart = []
    for key in final_keys:
        item = by_key.get(str(key))
        if not item:
            continue
        price = int(item.get("price") or item.get("current_price") or item.get("catalog_price") or 0)
        if price <= 0:
            continue
        cart.append({
            "key": str(item.get("key") or ""),
            "name": str(item.get("name") or ""),
            "price": price,
            "catalog_price": int(item.get("catalog_price") or price),
            "phrases": list(item.get("phrases") or []),
        })
    if not cart:
        state.update(_empty_state())
        _save_state(production_db_path, chat_id, state)
        return state
    layer, total, floor = _policy(cart)
    state.update({
        "cart": cart,
        "layer": layer,
        "original_total": total,
        "current_offer": total,
        "floor_price": floor,
        "negotiation_round": 0,
        "status": "SELECTED",
        "accepted_price": 0,
        "last_seller_reply": "",
    })
    _save_state(production_db_path, chat_id, state)
    return state


def _cart_list_text(state: dict) -> str:
    cart = list(state.get("cart") or [])
    if not cart:
        return "No batch selected."
    lines = ["You selected:"]
    for item in cart:
        lines.append(f"• {str(item.get('name') or '').strip()} — ₹{int(item.get('price') or 0)}")
    lines += ["", f"Total: ₹{int(state.get('original_total') or _total(cart))}", "", "Is this correct?"]
    return "\n".join(lines)


async def _send_cart_list_reply(context, production_db_path: str, chat_id: Any, state: dict, text: str, reply_to_message_id: int | None = None):
    client = context.application.bot_data.get("main_account_client")
    sent = None
    if client is not None:
        kwargs = {"link_preview": False}
        if reply_to_message_id:
            kwargs["reply_to"] = int(reply_to_message_id)
        sent = await client.send_message(int(chat_id), str(text), **kwargs)
    else:
        kwargs = {"chat_id": int(chat_id), "text": str(text), "disable_web_page_preview": True}
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = int(reply_to_message_id)
        sent = await context.bot.send_message(**kwargs)
    try:
        sent_id = int(getattr(sent, "id", 0) or getattr(sent, "message_id", 0) or 0)
    except Exception:
        sent_id = 0
    if sent_id:
        try:
            _reply_message_context_save(
                production_db_path, chat_id, sent_id,
                direction="outgoing", context_type="multi_course_list",
                target_key="", target_name="Selected courses",
                target_price=int(state.get("original_total") or 0),
                negotiation_round=int(state.get("negotiation_round") or 0),
                current_price=int(state.get("current_offer") or state.get("original_total") or 0),
                parent_message_id=int(reply_to_message_id or 0),
                metadata={
                    "cart_keys": [str(x.get("key") or "") for x in state.get("cart") or []],
                    "cart_names": [str(x.get("name") or "") for x in state.get("cart") or []],
                    "item_prices": {str(x.get("key") or ""): int(x.get("price") or 0) for x in state.get("cart") or []},
                },
            )
        except Exception:
            logger.exception("%s multi-course reply context save failed chat=%s", MARKER, chat_id)
    return sent


async def _handle_cart_edit(update, context, production_db_path: str, chat_id: Any, raw: str, state: dict, ref: dict | None, explicit_items: list[dict]):
    recent = _load_recent_items(production_db_path, chat_id)
    if not _looks_like_cart_edit(raw, state, ref, explicit_items, recent):
        return state, False
    candidates = _cart_edit_candidates(production_db_path, chat_id, state, explicit_items, ref)
    if not candidates:
        return state, False
    decision = await _cart_intent_with_ai(context, production_db_path, chat_id, raw, state, ref, candidates)
    if bool(decision.get("route_to_matcher")):
        return state, False
    final_keys = [str(x) for x in (decision.get("final_keys") or []) if str(x)]
    if not final_keys and state.get("cart"):
        return state, False
    state = _apply_cart_final_keys(production_db_path, chat_id, state, candidates, final_keys)
    _clear_burst(production_db_path, chat_id)
    old_task = _TASKS.get(str(chat_id))
    if old_task and not old_task.done():
        old_task.cancel()
    msg = getattr(update, "message", None)
    try:
        incoming_mid = int(getattr(msg, "id", 0) or getattr(msg, "message_id", 0) or 0)
    except Exception:
        incoming_mid = 0
    reply = _cart_list_text(state)
    state["last_seller_reply"] = reply
    _save_state(production_db_path, chat_id, state)
    await _send_cart_list_reply(context, production_db_path, int(chat_id), state, reply, incoming_mid or None)
    logger.info("%s cart edit applied chat=%s action=%s cart=%r total=%s", MARKER, chat_id, decision.get("action"), [x.get("name") for x in state.get("cart") or []], state.get("original_total"))
    return state, True


def _hydrate_from_production_context(production_db_path: str, chat_id: Any, state: dict):
    """Recover an already-selected product after deploy/restart without asking again."""
    if state.get("cart"):
        return state
    cart = []
    try:
        with _con(production_db_path) as db:
            # Combo state is authoritative for Layer-A products.
            try:
                row = db.execute(
                    "SELECT current_state,active_combo FROM combo_conversation_context WHERE chat_id=? LIMIT 1",
                    (str(chat_id),),
                ).fetchone()
            except Exception:
                row = None
            combo_state = str((row["current_state"] if row and "current_state" in row.keys() else "") or "").upper() if row else ""
            active_combo = str((row["active_combo"] if row and "active_combo" in row.keys() else "") or "").casefold() if row else ""
            if combo_state == "PRO_PACK" or "pro pack" in active_combo:
                cart = [dict(LAYER_A_PRODUCTS["PRO_PACK"])]
            elif combo_state == "TOP_FACULTY" or "top faculty" in active_combo:
                cart = [dict(LAYER_A_PRODUCTS["TOP_FACULTY"])]

            # Small-batch state is used only when it is fresh enough to be part
            # of the current commercial conversation.
            if not cart:
                try:
                    u = db.execute(
                        "SELECT active_targets_json,last_active_at FROM unified_customer_context WHERE chat_id=? LIMIT 1",
                        (str(chat_id),),
                    ).fetchone()
                except Exception:
                    u = None
                if u and float(u["last_active_at"] or 0) > 0 and time.time() - float(u["last_active_at"] or 0) <= STATE_TTL_SECONDS:
                    try:
                        targets = json.loads(u["active_targets_json"] or "[]")
                    except Exception:
                        targets = []
                    for t in targets if isinstance(targets, list) else []:
                        if not isinstance(t, dict):
                            continue
                        name = str(t.get("name") or "").strip()
                        if not name:
                            teacher = str(t.get("teacher") or "").strip()
                            subject = str(t.get("subject") or t.get("folder_name") or "").strip()
                            name = " — ".join(x for x in (teacher, subject) if x)
                        price = _price_int(t.get("price"))
                        key = str(t.get("key") or "").strip()
                        if name and price > 0:
                            cart.append({
                                "key": key or ("unified:" + name.casefold()),
                                "name": name,
                                "price": price,
                                "phrases": [],
                            })
    except Exception:
        logger.exception("%s production context hydration failed chat=%s", MARKER, chat_id)

    if cart:
        layer, total, floor = _policy(cart)
        state.update({
            "cart": cart,
            "layer": layer,
            "original_total": total,
            "current_offer": total,
            "floor_price": floor,
            "negotiation_round": 0,
            "status": "SELECTED",
            "accepted_price": 0,
        })
        return _save_state(production_db_path, chat_id, state)
    return state


def _sync_selection(production_db_path: str, chat_id: Any, text: str, state: dict):
    if not SELECTION_RE.search(text):
        return state, False
    items = _selection_items(production_db_path, text)
    if not items:
        return state, False
    old = list(state.get("cart") or [])
    if not old:
        try:
            with _con(production_db_path) as db:
                row = db.execute("SELECT active_targets_json FROM unified_customer_context WHERE chat_id=? LIMIT 1", (str(chat_id),)).fetchone()
            saved = json.loads(row["active_targets_json"] or "[]") if row else []
            old = [{"key": str(x.get("key")), "name": str(x.get("name")), "price": _price_int(x.get("price")), "catalog_price": _price_int(x.get("price")), "phrases": []} for x in saved if isinstance(x, dict) and x.get("key") and x.get("name")]
            if old: state["cart"] = old
        except Exception: logger.exception("%s active cart recovery failed chat=%s", MARKER, chat_id)
    # Multi-course wording is inherently additive even when the user does not
    # say the literal "add" (for example: "dono", "teeno", "both", "all").
    # Preserve the existing cart so later remove/replace turns have full state.
    multi_add = bool(re.search(r"\b(?:dono|teeno|teenon|both|all|sabhi|sare|saare)\b", text, re.I))
    explicit_replace = bool(REMOVE_RE.search(text) or REPLACE_RE.search(text) or ONLY_RE.search(text))
    # Once a negotiation cart exists, a subsequent named course is additive by
    # default. Replacement requires explicit remove/replace/only wording.
    if old and (ADDITIVE_RE.search(text) or multi_add or not explicit_replace):
        cart = _merge_selected(old, items)
    else:
        cart = items
    if _same_cart(old, cart):
        return state, False
    layer, total, floor = _policy(cart)
    state.update({
        "cart": cart,
        "layer": layer,
        "original_total": total,
        "current_offer": total,
        "floor_price": floor,
        "negotiation_round": 0,
        "status": "SELECTED",
        "accepted_price": 0,
        "last_seller_reply": "",
    })
    _save_state(production_db_path, chat_id, state)
    logger.info("%s selection chat=%s layer=%s total=%s cart=%r", MARKER, chat_id, layer, total, [x.get("name") for x in cart])
    return state, True


async def sync_negotiation_after_normal_batch(
    production_db_path: str,
    chat_id,
):
    """
    SELECTION_CONTEXT_SYNC_V31

    Re-synchronize only per-chat negotiation selection after a normal batch
    has been selected.  Existing negotiation pricing/rules are unchanged.
    The cart is rebuilt through the module's existing production-context
    hydration, after Combo continuity has been cleared by bot.py.
    """
    cid = str(chat_id)

    async with _lock(cid):
        old_task = _TASKS.pop(cid, None)
        if old_task and not old_task.done():
            old_task.cancel()

        _clear_burst(production_db_path, cid)

        state = _hydrate_from_production_context(
            production_db_path,
            cid,
            _empty_state(),
        )

        if state.get("cart"):
            _save_state(production_db_path, cid, state)
            logger.info(
                "%s selection sync chat=%s cart=%r",
                MARKER,
                cid,
                [x.get("name") for x in state.get("cart") or []],
            )
            return state

        runtime = ensure_schema(production_db_path)
        with _con(runtime) as db:
            db.execute(
                "DELETE FROM negotiation_state WHERE chat_id=?",
                (cid,),
            )
            db.commit()

        logger.info(
            "%s selection sync cleared empty chat=%s",
            MARKER,
            cid,
        )
        return _empty_state()


def _heuristic_language(text: str) -> str:
    text = str(text or "")
    if re.search(r"[\u0900-\u097F]", text):
        return "hindi"
    hi = len(HINGLISH_HINT_RE.findall(text))
    en = len(ENGLISH_HINT_RE.findall(text))
    if hi > 0 and hi >= en:
        return "hinglish"
    return "english"


def _extract_offer(text: str, original_total: int = 0):
    raw = str(text or "")
    patterns = [
        r"(?:₹|rs\.?\s*|rupees?\s*)(\d{2,5})",
        r"\b(\d{2,5})\s*(?:me|mein|mai|rs|rupees?|kar\s*do|kardo|de\s*do|dedo|final|tak)\b",
        r"^\s*₹?\s*(\d{2,5})\s*$",
    ]
    for pat in patterns:
        for m in re.finditer(pat, raw, re.I):
            try:
                value = int(m.group(1))
            except Exception:
                continue
            if 2020 <= value <= 2035:
                continue
            if value < 50 or value > 100000:
                continue
            if original_total and value > max(100000, original_total * 4):
                continue
            return value
    return None


def _is_negotiation_message(text: str, state: dict) -> bool:
    if NEGOTIATION_RE.search(text):
        return True
    offer = _extract_offer(text, int(state.get("original_total") or 0))
    if offer is not None and int(state.get("original_total") or 0) > 0 and offer < int(state.get("original_total") or 0):
        return True
    return False


def _fallback_classification(messages: list[str], state: dict):
    text = "\n".join(messages).strip()
    offer = _extract_offer(text, int(state.get("original_total") or 0))
    language = _heuristic_language(text)
    postpone = bool(POSTPONE_RE.search(text))
    payment = bool(PAYMENT_RE.search(text))
    negotiation = _is_negotiation_message(text, state)
    current = int(state.get("current_offer") or state.get("original_total") or 0)
    accepted_amount = bool(current and re.search(rf"(?:₹\s*)?{re.escape(str(current))}\b", text))
    accepted = bool(ACCEPT_RE.search(text) and (accepted_amount or payment))
    if postpone:
        intent = "POSTPONE"
    elif negotiation:
        intent = "NEGOTIATE"
    elif accepted and payment:
        intent = "ACCEPT_PAYMENT"
    elif payment:
        intent = "PAYMENT"
    elif accepted:
        intent = "ACCEPT"
    else:
        intent = "OTHER"
    return {
        "intent": intent,
        "customer_offer": offer,
        "language": language,
        "financial_issue": bool(re.search(r"\b(?:financial|budget|student|paise?|paisa|afford)\b", text, re.I)),
        "immediate_payment": bool(payment and re.search(r"\b(?:abhi|now|immediately|right\s+now)\b", text, re.I)),
    }


async def _classify_with_ai(context, production_db_path: str, chat_id: Any, messages: list[str], state: dict):
    fallback = _fallback_classification(messages, state)
    ai = context.application.bot_data.get("ai")
    client = getattr(ai, "client", None) if ai is not None else None
    model = str(getattr(ai, "model", "") or "") if ai is not None else ""
    if client is None or not model:
        return fallback
    burst = "\n".join(messages)[-MAX_BURST_CHARS:]
    cart_names = [str(x.get("name") or "") for x in state.get("cart") or []]
    prompt = (
        "Classify a sales negotiation turn. Do not answer the customer and do not choose a price. "
        "Return JSON only with keys: intent, customer_offer, language, financial_issue, immediate_payment. "
        "intent must be one of NEGOTIATE, ACCEPT, ACCEPT_PAYMENT, PAYMENT, POSTPONE, OTHER. "
        "customer_offer is an integer rupee amount or null. language is english, hinglish, or hindi. "
        "POSTPONE means customer will buy/pay later, has no money now, will ask parents, or wants to think. "
        "NEGOTIATE includes discount/lower-price requests, counteroffers, budget/financial/student pleas, best/final-price bargaining. "
        "Do not infer acceptance merely from polite acknowledgement unless the current price is accepted or payment is requested.\n"
        f"CART={json.dumps(cart_names, ensure_ascii=False)}\n"
        f"ORIGINAL_TOTAL={int(state.get('original_total') or 0)}\n"
        f"CURRENT_OFFER={int(state.get('current_offer') or 0)}\n"
        f"ROUND={int(state.get('negotiation_round') or 0)}\n"
        f"CUSTOMER_BURST:\n{burst}"
    )
    resp = None
    try:
        resp = await client.responses.create(
            model=model,
            instructions="You are a compact negotiation intent extractor. Output JSON only.",
            input=prompt,
            max_output_tokens=100,
        )
        raw = str(getattr(resp, "output_text", "") or "").strip()
        try:
            data = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
        intent = str(data.get("intent") or "").upper()
        if intent not in {"NEGOTIATE", "ACCEPT", "ACCEPT_PAYMENT", "PAYMENT", "POSTPONE", "OTHER"}:
            intent = fallback["intent"]
        offer = data.get("customer_offer")
        try:
            offer = int(offer) if offer is not None else None
        except Exception:
            offer = fallback["customer_offer"]
        language = str(data.get("language") or "").lower()
        if language not in {"english", "hinglish", "hindi"}:
            language = fallback["language"]
        result = {
            "intent": intent,
            "customer_offer": offer,
            "language": language,
            "financial_issue": bool(data.get("financial_issue")),
            "immediate_payment": bool(data.get("immediate_payment")),
        }
        usage = getattr(resp, "usage", None)
        inp = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        out = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        total = int(getattr(usage, "total_tokens", 0) or 0) if usage else inp + out
        runtime = ensure_schema(production_db_path)
        with _con(runtime) as db:
            db.execute(
                """
                INSERT INTO negotiation_ai_audit(
                    chat_id,model,intent,language,customer_offer,financial_issue,
                    immediate_payment,input_tokens,output_tokens,total_tokens,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(chat_id), model, result["intent"], result["language"], result["customer_offer"],
                    int(result["financial_issue"]), int(result["immediate_payment"]), inp, out, total, time.time(),
                ),
            )
            db.commit()
        return result
    except Exception:
        logger.exception("%s AI classification failed chat=%s", MARKER, chat_id)
        return fallback



def _active_business_context(production_db_path: str, chat_id: Any, state: dict, ref: dict | None = None):
    """Compact current state for contextual AI. Matching tables are read-only."""
    payload = {
        "cart": list(state.get("cart") or []),
        "layer": str(state.get("layer") or ""),
        "original_total": int(state.get("original_total") or 0),
        "current_offer": int(state.get("current_offer") or 0),
        "floor_price": int(state.get("floor_price") or 0),
        "negotiation_round": int(state.get("negotiation_round") or 0),
        "status": str(state.get("status") or ""),
    }
    if ref:
        payload["reply_reference"] = {
            "target_key": str(ref.get("target_key") or ""),
            "target_name": str(ref.get("target_name") or ref.get("reference_label") or ""),
            "target_price": int(ref.get("target_price") or 0),
            "referenced_text": str(ref.get("referenced_text") or "")[:1400],
        }
    try:
        with _con(production_db_path) as db:
            row = db.execute(
                "SELECT active_targets_json FROM unified_customer_context WHERE chat_id=? LIMIT 1",
                (str(chat_id),),
            ).fetchone()
        if row:
            try:
                targets = json.loads(row["active_targets_json"] or "[]")
            except Exception:
                targets = []
            if isinstance(targets, list):
                # Keep token usage bounded; current state only, no chat history.
                payload["current_saved_targets"] = targets[:4]
    except Exception:
        pass
    return payload


def _fallback_context_answer(raw: str, state: dict):
    n = re.sub(r"\s+", " ", str(raw or "").casefold()).strip()
    current = int(state.get("current_offer") or state.get("original_total") or 0)
    if PRICE_QUESTION_RE.search(n) and current > 0:
        return f"₹{current}"
    if re.search(r"\b(?:validity|access\s+kab\s+tak|kitne\s+time|how\s+long)\b", n, re.I):
        return "Lifetime"
    if re.search(r"\b(?:notes?|pdf|material|lecture\s+notes?|recordings?)\b", n, re.I):
        return "Haan bro, lecture notes aur recordings milenge."
    if re.search(r"\b(?:android|download)\b", n, re.I):
        return "Haan bro, Android phone par lectures download kar sakte ho."
    return None


async def _contextual_ai_turn(
    context,
    production_db_path: str,
    chat_id: Any,
    raw: str,
    state: dict,
    ref: dict | None = None,
    *,
    force_known_faq: bool = False,
):
    """Use AI only for interpretation/natural wording during negotiation.

    AI never chooses a catalogue target, price, discount, negotiation round,
    cart mutation, or payment state. It receives only current business state,
    negotiation state, reply reference (if any), and the current customer query.
    """
    ai = context.application.bot_data.get("ai")
    client = getattr(ai, "client", None) if ai is not None else None
    model = str(getattr(ai, "model", "") or "") if ai is not None else ""
    if client is None or not model:
        fb = _fallback_context_answer(raw, state)
        return {"intent": "GENERIC_FAQ" if fb else "OTHER", "reply": fb or "", "route_to_matcher": False}

    payload = _active_business_context(production_db_path, chat_id, state, ref)
    prompt = (
        "You are handling ONE customer message inside an active Telegram sales conversation. "
        "Use only the CURRENT STATE, NEGOTIATION STATE, REPLY REFERENCE and CUSTOMER QUERY below. "
        "Do not use or request old chat history. Return JSON only with keys intent, reply, route_to_matcher. "
        "intent must be GENERIC_FAQ, NEW_BATCH_REFERENCE, OTHER. "
        "If the customer asks a generic question about the already-known active/replied batch, answer naturally and briefly in the customer's language/style. "
        "Questions such as whether lectures, lecture notes, PDFs/material, validity, download, demo, year, content or access are available are GENERIC_FAQ when a current target is already known. "
        "The response will be sent as a Telegram reply to the relevant batch message, so do not repeat the batch name unless clarity truly requires it. "
        "NEVER ask for batch name or teacher name when a current/replied target is already known. "
        "A generic FAQ must not change negotiation round, current price, cart, or payment state. "
        "If a factual detail is not supported by the supplied state/reference, do not invent it; give a useful short answer and, when necessary, tell the customer it can be checked in the demo. "
        "If the message genuinely refers to a DIFFERENT unidentified batch/teacher, set intent=NEW_BATCH_REFERENCE. "
        "Do not choose or invent a catalogue batch. Do not calculate or offer any discount. "
        "Set route_to_matcher=true only when the current message genuinely needs deterministic catalogue resolution.\n"
        f"KNOWN_CONTEXT_FAQ={'true' if force_known_faq else 'false'}\n"
        + (
            "IMPORTANT: KNOWN_CONTEXT_FAQ=true means the current message is already recognized as a property/access FAQ for the active/replied course. "
            "You MUST answer it as GENERIC_FAQ and MUST set route_to_matcher=false. Do not ask for batch/teacher identification.\n"
            if force_known_faq else ""
        )
        + f"STATE={json.dumps(payload, ensure_ascii=False, default=str)}\n"
        + f"CUSTOMER_QUERY={raw}"
    )
    try:
        resp = await client.responses.create(
            model=model,
            instructions="Compact Telegram sales-context interpreter. JSON only.",
            input=prompt,
            max_output_tokens=140,
        )
        out = str(getattr(resp, "output_text", "") or "").strip()
        try:
            data = json.loads(out)
        except Exception:
            m = re.search(r"\{.*\}", out, re.S)
            data = json.loads(m.group(0)) if m else {}
        intent = str(data.get("intent") or "OTHER").upper()
        if intent not in {"GENERIC_FAQ", "NEW_BATCH_REFERENCE", "OTHER"}:
            intent = "OTHER"
        reply = str(data.get("reply") or "").strip()
        route = bool(data.get("route_to_matcher"))
        # NEGOTIATION_GENERIC_FAQ_AI_V33
        # The deterministic layer has already established that this is a
        # property/access FAQ about a known active/replied target. AI is used
        # for interpretation + natural wording, but it is not allowed to send
        # the turn back into generic batch matching. This is the exact guard
        # against "lecture notes sab rhega na" becoming "batch/teacher name?".
        if force_known_faq:
            intent = "GENERIC_FAQ"
            route = False
            if not reply:
                reply = _fallback_context_answer(raw, state) or "Bro, demo mein check kar lo."
        elif intent == "GENERIC_FAQ" and not reply:
            reply = _fallback_context_answer(raw, state) or "Bro, demo mein check kar lo."
        return {"intent": intent, "reply": reply, "route_to_matcher": route}
    except Exception:
        logger.exception("%s contextual AI failed chat=%s", MARKER, chat_id)
        fb = _fallback_context_answer(raw, state)
        return {"intent": "GENERIC_FAQ" if fb else "OTHER", "reply": fb or "", "route_to_matcher": False}


def _enrich_reference_target_from_card(production_db_path: str, chat_id: Any, ref: dict | None):
    """Recover target metadata for pre-V32 Telegram cards when possible.

    Uses the existing deterministic item resolver only to identify the object
    represented by the replied message. It never asks the matcher to compose a
    customer response and never changes matcher behavior.
    """
    if not ref:
        return ref
    if str(ref.get("target_name") or "").strip() and int(ref.get("target_price") or 0) > 0:
        return ref
    text = str(ref.get("referenced_text") or "").strip()
    if not text:
        return ref
    try:
        items = _selection_items(production_db_path, text)
    except Exception:
        items = []
    if len(items) != 1:
        return ref
    item = items[0]
    name = str(item.get("name") or "").strip()
    price = int(item.get("price") or 0)
    key = str(item.get("key") or "").strip()
    if not name or price <= 0:
        return ref
    out = dict(ref)
    out.update({
        "target_key": key,
        "target_name": name,
        "target_price": price,
        "current_price": int(out.get("current_price") or price),
    })
    try:
        mid = int(out.get("referenced_message_id") or 0)
    except Exception:
        mid = 0
    if mid:
        try:
            _reply_message_context_save(
                production_db_path,
                chat_id,
                mid,
                direction=str(out.get("direction") or "unknown"),
                context_type=str(out.get("context_type") or "recovered_reference"),
                target_key=key,
                target_name=name,
                target_price=price,
                negotiation_round=int(out.get("negotiation_round") or 0),
                current_price=int(out.get("current_price") or price),
            )
        except Exception:
            pass
    return out


def _reference_for_turn(production_db_path: str, chat_id: Any, update, raw: str):
    direct = getattr(update, "_customer_reply_reference", None)
    if direct:
        return _enrich_reference_target_from_card(
            production_db_path, chat_id, direct
        )
    # A previous explicit Telegram reply remains useful for immediate short
    # follow-ups such as "aur kam?" or "notes bhi milenge?".
    if (
        _reply_is_followup(raw)
        or bool(NEGOTIATION_RE.search(raw))
        or bool(FAQ_RE.search(raw))
        or bool(PRICE_QUESTION_RE.search(raw))
    ):
        try:
            saved = _reply_reference_get(production_db_path, chat_id, touch=False)
            return _enrich_reference_target_from_card(
                production_db_path, chat_id, saved
            )
        except Exception:
            return None
    return None


def _apply_reference_target(production_db_path: str, chat_id: Any, state: dict, ref: dict | None):
    if not ref:
        return state, False
    name = str(ref.get("target_name") or "").strip()
    key = str(ref.get("target_key") or "").strip()
    price = int(ref.get("target_price") or 0)
    if not name or price <= 0:
        return state, False
    item = {
        "key": key or ("reply:" + name.casefold()),
        "name": name,
        "price": price,
        "phrases": [],
    }
    old = list(state.get("cart") or [])
    # A reply to one card inside a multi-course cart identifies the referenced
    # message; it must not collapse the active bundle. Cart edits such as
    # ADD/REMOVE/REPLACE are handled earlier by _handle_cart_edit and can make
    # an intentional mutation explicitly.
    if len(old) > 1:
        return state, False
    if len(old) == 1 and str(old[0].get("key") or "") == str(item["key"]):
        return state, False
    if len(old) == 1 and not key and str(old[0].get("name") or "").casefold() == name.casefold():
        return state, False
    layer, total, floor = _policy([item])
    state.update({
        "cart": [item],
        "layer": layer,
        "original_total": total,
        "current_offer": total,
        "floor_price": floor,
        "negotiation_round": 0,
        "status": "SELECTED",
        "accepted_price": 0,
        "last_seller_reply": "",
    })
    _save_state(production_db_path, chat_id, state)
    logger.info("%s reply target switch chat=%s target=%r", MARKER, chat_id, name)
    return state, True


def _target_anchor_for_state(production_db_path: str, chat_id: Any, state: dict, ref: dict | None = None):
    cart = list(state.get("cart") or [])
    if len(cart) == 1:
        item = cart[0]
        try:
            anchor = _reply_target_anchor(
                production_db_path,
                chat_id,
                target_key=str(item.get("key") or ""),
                target_name=str(item.get("name") or ""),
            )
            if anchor:
                return int(anchor)
        except Exception:
            pass
    if ref:
        try:
            mid = int(ref.get("referenced_message_id") or 0)
            if mid:
                return mid
        except Exception:
            pass
    try:
        anchor = _state_reply_anchor_get(production_db_path, chat_id)
        if anchor:
            return int(anchor)
    except Exception:
        pass
    return None


async def _send_target_reply(
    context,
    production_db_path: str,
    chat_id: Any,
    state: dict,
    text: str,
    *,
    ref: dict | None = None,
    context_type: str = "negotiation_reply",
):
    text = str(text or "").strip()
    if not text:
        return None
    anchor = _target_anchor_for_state(production_db_path, chat_id, state, ref)
    client = context.application.bot_data.get("main_account_client")
    sent = None
    if client is not None:
        kwargs = {"link_preview": False}
        if anchor:
            kwargs["reply_to"] = int(anchor)
        sent = await client.send_message(int(chat_id), text, **kwargs)
    else:
        kwargs = {
            "chat_id": int(chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if anchor:
            kwargs["reply_to_message_id"] = int(anchor)
        sent = await context.bot.send_message(**kwargs)

    cart = list(state.get("cart") or [])
    if len(cart) == 1 and sent is not None:
        item = cart[0]
        try:
            sent_id = int(getattr(sent, "id", 0) or getattr(sent, "message_id", 0) or 0)
        except Exception:
            sent_id = 0
        if sent_id:
            try:
                _reply_message_context_save(
                    production_db_path,
                    chat_id,
                    sent_id,
                    direction="outgoing",
                    context_type=context_type,
                    target_key=str(item.get("key") or ""),
                    target_name=str(item.get("name") or ""),
                    target_price=int(item.get("price") or 0),
                    negotiation_round=int(state.get("negotiation_round") or 0),
                    current_price=int(state.get("current_offer") or 0),
                    parent_message_id=int(anchor or 0),
                )
            except Exception:
                logger.exception("%s outgoing message context save failed chat=%s", MARKER, chat_id)
    return sent


def _looks_like_new_batch_reference(raw: str) -> bool:
    n = str(raw or "")
    return bool(re.search(
        r"\b(?:batch|course|teacher|faculty|sir|mam|maam|ma'am|wala|wali|optional|gs)\b",
        n,
        re.I,
    ))


def _reply_layer_a(state: dict, language: str, round_no: int):
    item = (state.get("cart") or [{}])[0]
    name = str(item.get("name") or "")
    is_pro = "pro pack" in name.casefold() or str(item.get("key") or "") == "neg_combo:pro_pack"
    profit = 500 if is_pro else 250
    if language == "english":
        if round_no == 1:
            return "The price is already kept almost at half, so I can't reduce it further."
        if round_no == 2:
            return f"My profit on this is only ₹{profit}, so I can't reduce the price further."
        return "The price is fixed and completely non-negotiable. No further discount is possible."
    if round_no == 1:
        return "Bro, price already almost half rakha hai, isliye aur discount possible nahi hai."
    if round_no == 2:
        return f"Bro, isme mera profit sirf ₹{profit} hai, isliye price aur kam nahi ho payega."
    return "Bro, price fixed hai. Isme aur discount possible nahi hai."


def _reply_layer_b(
    state: dict,
    language: str,
    round_no: int,
    *,
    customer_offer: int | None = None,
):
    """Deterministic normal-batch pricing ladder (V32).

    User-locked rules:
    - any individual normal course priced at ₹200 or below: zero discount;
    - eligible normal batches (>₹200):
        turn 1 = defend original price, no discount;
        turn 2 = original - ₹50;
        turn 3 = original - ₹100, WITHOUT "last price" wording;
        turn 4 = same floor + explicit last-price wording;
        turn 5+ = same floor + no-further-discount wording;
    - AI may understand intent/language, but never chooses the amount.
    """
    original = int(state.get("original_total") or 0)
    floor = int(state.get("floor_price") or original)

    # Fixed-price low-value cart (all individual items <= ₹200).
    if floor >= original:
        offer = original
        if language == "english":
            reply = f"The price is already at the lowest fixed price, ₹{offer}. I can't reduce it further."
        else:
            reply = f"Bro, ₹{offer} already lowest fixed price hai. Isme discount possible nahi hai."
        return offer, reply

    # Turn 1: always defend the original price. A numeric counteroffer does
    # not skip the first stage anymore.
    if round_no <= 1:
        offer = original
        if language == "english":
            reply = "The price is already kept low, and this is a genuine batch."
        else:
            reply = "Bro, price already kam rakha hai aur genuine batch hai."
        return offer, reply

    # Turn 2: first concession, ₹50 below original (bounded by floor).
    if round_no == 2:
        offer = max(floor, original - 50)
        reply = (
            f"I can make it ₹{offer}."
            if language == "english"
            else f"Bro, ₹{offer} kar dunga."
        )
        return offer, reply

    # Turn 3: full ₹100 total concession, but DO NOT call it final yet.
    if round_no == 3:
        offer = floor
        reply = (
            f"Okay, I can make it ₹{offer}."
            if language == "english"
            else f"Theek hai bro, ₹{offer} kar do."
        )
        return offer, reply

    # Turn 4: same floor, now explicitly mark it as the last price.
    if round_no == 4:
        offer = floor
        reply = (
            f"₹{offer} is the last price. I can't reduce it further."
            if language == "english"
            else f"Bro, ₹{offer} last price hai. Isse kam possible nahi hai."
        )
        return offer, reply

    # Turn 5+: never move the price again.
    offer = floor
    reply = (
        f"No further discount is possible. The price stays ₹{offer}."
        if language == "english"
        else f"Bro, isse kam possible nahi hai. Price ₹{offer} hi rahega."
    )
    return offer, reply


def _set_combo_stage(production_db_path: str, chat_id: Any, stage: str):
    try:
        with _con(production_db_path) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='combo_conversation_context' LIMIT 1"
            ).fetchone()
            if not exists:
                return
            cols = {r[1] for r in db.execute("PRAGMA table_info(combo_conversation_context)").fetchall()}
            if "combo_stage" not in cols:
                return
            db.execute(
                "UPDATE combo_conversation_context SET combo_stage=?, updated_at=CURRENT_TIMESTAMP WHERE chat_id=?",
                (str(stage or ""), str(chat_id)),
            )
            db.commit()
    except Exception:
        logger.exception("%s combo-stage sync failed chat=%s stage=%s", MARKER, chat_id, stage)


def _payment_amount(state: dict) -> int:
    accepted = int(state.get("accepted_price") or 0)
    if accepted > 0:
        return accepted
    current = int(state.get("current_offer") or 0)
    if current > 0:
        return current
    return int(state.get("original_total") or 0)


async def _handoff_payment(context, production_db_path: str, chat_id: Any, state: dict):
    cart = list(state.get("cart") or [])
    amount = _payment_amount(state)
    if not cart or amount <= 0:
        return False
    ok = await begin_payment_for_selected(
        context,
        production_db_path,
        int(chat_id),
        cart,
        final_amount=amount,
        negotiated=(amount != int(state.get("original_total") or 0)),
    )
    if ok:
        state["status"] = "PAYMENT_HANDOFF"
        state["accepted_price"] = amount
        _save_state(production_db_path, chat_id, state)
        # Prevent the older Combo Rules yes/no payment state from competing.
        if state.get("layer") == "A":
            _set_combo_stage(production_db_path, chat_id, "PAYMENT_STARTED")
    return bool(ok)


async def _process_burst(context, production_db_path: str, chat_id: Any, expected_generation: int):
    key = str(chat_id)
    try:
        while True:
            await asyncio.sleep(BURST_GAP_SECONDS)
            async with _lock(key):
                burst = _load_burst(production_db_path, key)
                if not burst or int(burst.get("generation") or 0) != int(expected_generation):
                    return
                remaining = BURST_GAP_SECONDS - (time.time() - float(burst.get("last_at") or 0))
                if remaining > 0.05:
                    # Sliding inactivity timer: another customer message arrived.
                    continue
                messages = list(burst.get("messages") or [])
                _clear_burst(production_db_path, key)
                break

        if not messages:
            return
        state = _load_state(production_db_path, key)
        if not state.get("cart"):
            return
        result = await _classify_with_ai(context, production_db_path, key, messages, state)
        language = result.get("language") or state.get("language") or _heuristic_language("\n".join(messages))
        state["language"] = language
        intent = str(result.get("intent") or "OTHER")

        if intent == "POSTPONE":
            state["status"] = "DEFERRED"
            _save_state(production_db_path, key, state)
            logger.info("%s postpone silent chat=%s", MARKER, key)
            return

        if intent in {"ACCEPT", "ACCEPT_PAYMENT"}:
            amount = _payment_amount(state)
            state["accepted_price"] = amount
            state["status"] = "ACCEPTED"
            _save_state(production_db_path, key, state)
            if intent == "ACCEPT_PAYMENT":
                await _handoff_payment(context, production_db_path, key, state)
            return

        if intent == "PAYMENT":
            await _handoff_payment(context, production_db_path, key, state)
            return

        if intent != "NEGOTIATE":
            # A pending negotiation burst can contain polite filler.  If AI says OTHER,
            # deterministic fallback gets the final say so genuine bargaining is not lost.
            fb = _fallback_classification(messages, state)
            if fb["intent"] != "NEGOTIATE":
                return

        round_no = int(state.get("negotiation_round") or 0) + 1
        state["negotiation_round"] = round_no
        state["status"] = "NEGOTIATING"
        if state.get("layer") == "A":
            state["current_offer"] = int(state.get("original_total") or 0)
            reply = _reply_layer_a(state, language, round_no)
        else:
            offer, reply = _reply_layer_b(
                state,
                language,
                round_no,
                customer_offer=result.get("customer_offer"),
            )
            state["current_offer"] = int(offer)
        state["last_seller_reply"] = reply
        _save_state(production_db_path, key, state)
        if state.get("layer") == "A":
            _set_combo_stage(production_db_path, key, "NEGOTIATING")
        # REPLY_THREAD_CONTEXT_V32: negotiation answers are native Telegram
        # replies to the relevant batch card whenever a target anchor exists.
        await _send_target_reply(
            context, production_db_path, int(key), state, reply,
            context_type="negotiation_reply",
        )
        logger.info(
            "%s reply chat=%s layer=%s round=%s original=%s current=%s floor=%s language=%s",
            MARKER, key, state.get("layer"), round_no, state.get("original_total"),
            state.get("current_offer"), state.get("floor_price"), language,
        )
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("%s burst processing failed chat=%s", MARKER, key)


def _schedule_burst(context, production_db_path: str, chat_id: Any, messages: list[str], first_at: float, last_at: float):
    key = str(chat_id)
    gen = int(_TASK_GENERATION.get(key, 0)) + 1
    _TASK_GENERATION[key] = gen
    old = _TASKS.get(key)
    if old and not old.done():
        old.cancel()
    _save_burst(production_db_path, key, messages, first_at, last_at, gen)
    task = asyncio.create_task(_process_burst(context, production_db_path, key, gen))
    _TASKS[key] = task


def _append_or_touch_burst(context, production_db_path: str, chat_id: Any, text: str, append: bool):
    now = time.time()
    burst = _load_burst(production_db_path, chat_id)
    if burst:
        messages = list(burst.get("messages") or [])
        first = float(burst.get("first_at") or now)
    else:
        messages = []
        first = now
    if append:
        messages.append(str(text or "").strip())
        messages = [x for x in messages if x][-MAX_BURST_MESSAGES:]
        while sum(len(x) for x in messages) > MAX_BURST_CHARS and len(messages) > 1:
            messages.pop(0)
    _schedule_burst(context, production_db_path, chat_id, messages, first, now)


def debug_pricing(
    original_total: int,
    layer: str,
    round_no: int,
    *,
    customer_offer: int | None = None,
    current_offer: int | None = None,
    combined_quote: bool = False,
    eligible_for_discount: bool | None = None,
):
    """Pure helper used by automatic installer tests."""
    original_total = int(original_total)
    round_no = int(round_no)
    if layer == "A":
        return original_total
    if eligible_for_discount is None:
        eligible_for_discount = original_total > 200
    floor = original_total if not eligible_for_discount else max(0, original_total - 100)
    if combined_quote:
        return floor
    if floor >= original_total:
        return original_total
    if round_no <= 1:
        return original_total
    if round_no == 2:
        return max(floor, original_total - 50)
    return floor


async def process_negotiation_layer(update, context, production_db_path: str, text: str):
    """
    Authoritative commercial layer between finalized selection and payment.

    V34 adds an AI-assisted recent-negotiation list/cart layer on top of V33.
    The mature catalogue matcher itself is not modified.
    """
    chat = getattr(update, "effective_chat", None)
    msg = getattr(update, "message", None)
    if chat is None or msg is None:
        return False
    raw = str(text or "").strip()
    if not raw or len(raw) > 150:
        return False
    chat_id = str(chat.id)

    async with _lock(chat_id):
        # Once existing Payment Architecture is active, it owns the transaction.
        try:
            payment = _payment_state_get(production_db_path, int(chat_id))
            if str(payment.get("stage") or "").strip():
                return False
        except Exception:
            logger.exception("%s payment snapshot failed chat=%s", MARKER, chat_id)

        state = _load_state(production_db_path, chat_id)
        state = _hydrate_from_production_context(production_db_path, chat_id, state)
        state = _normalize_policy_state(production_db_path, chat_id, state)

        # REPLY_THREAD_CONTEXT_V32
        # Telegram reply identifies WHAT the customer is referring to. It does
        # not itself imply add/remove/buy; current message intent still decides.
        ref = _reference_for_turn(production_db_path, chat_id, update, raw)

        # A property/access or price question about an already-known course is
        # contextual FAQ traffic. Words such as "bhi", "dono", or "sab" may
        # appear in the question, but they must never trigger catalogue
        # selection or rebuild the active cart.
        known_active_faq = bool(
            (FAQ_RE.search(raw) or PRICE_QUESTION_RE.search(raw))
            and (state.get("cart") or ref)
        )
        faq_only = bool(
            FAQ_RE.search(raw)
            and not re.search(
                r"\b(?:chahiye|chahie|want|need|buy|purchase|lena|leni|lunga|lungi|"
                r"select|choose|add|include|remove|delete|nahi\s+chahiye|replace)\b",
                raw,
                re.I,
            )
        )
        if faq_only and not state.get("cart") and not ref:
            reply = _fallback_context_answer(raw, state) or "Haan bro, demo mein check kar lo."
            await update.message.reply_text(reply)
            return True

        # If the current message explicitly identifies another saved item, let
        # the mature matcher/selection flow own it instead of forcing the older
        # replied target onto the turn.
        explicit_items = []
        if not faq_only and not known_active_faq and (SELECTION_RE.search(raw) or _looks_like_new_batch_reference(raw)):
            explicit_items = _selection_items(production_db_path, raw)

        # NEGOTIATION_LIST_AI_V34: interpret list mutation before reply-target
        # switching or legacy one-selection sync. This lets "Ye bhi chahiye"
        # ADD the replied course instead of replacing the active one.
        if known_active_faq or faq_only:
            _cart_edit_handled = False
        else:
            state, _cart_edit_handled = await _handle_cart_edit(
                update, context, production_db_path, chat_id, raw, state, ref, explicit_items
            )
        if _cart_edit_handled:
            return True

        if ref and not explicit_items:
            state, _ = _apply_reference_target(
                production_db_path,
                chat_id,
                state,
                ref,
            )
            state = _normalize_policy_state(production_db_path, chat_id, state)

        if known_active_faq:
            _selection_changed = False
        else:
            state, _selection_changed = _sync_selection(
                production_db_path,
                chat_id,
                raw,
                state,
            )
        state = _normalize_policy_state(production_db_path, chat_id, state)

        if _selection_changed:
            _clear_burst(production_db_path, chat_id)
            old_task = _TASKS.get(chat_id)
            if old_task and not old_task.done():
                old_task.cancel()

        # Selection turns are never swallowed. Existing catalogue/batch routers
        # continue to render the card and preserve the mature matching system.
        if SELECTION_RE.search(raw) and (explicit_items or _selection_items(production_db_path, raw)):
            return False

        cart = list(state.get("cart") or [])
        if not cart:
            return False

        # Customer explicitly postpones the currently referenced/active item:
        # silence, no persuasion/follow-up.
        if POSTPONE_RE.search(raw):
            state["status"] = "DEFERRED"
            _save_state(production_db_path, chat_id, state)
            _clear_burst(production_db_path, chat_id)
            old = _TASKS.get(chat_id)
            if old and not old.done():
                old.cancel()
            logger.info("%s postpone immediate silent chat=%s message=%r", MARKER, chat_id, raw)
            return True

        # Multi-item Layer-B combined quote. The floor now respects the ₹200
        # fixed-price rule: if no item is >₹200, combined_offer == total.
        if len(cart) > 1 and state.get("layer") == "B" and COMBINED_PRICE_RE.search(raw):
            total = int(state.get("original_total") or _total(cart))
            combined_offer = int(state.get("floor_price") or total)
            language = _heuristic_language(raw)
            if combined_offer >= total:
                reply = (
                    f"The total is ₹{total}. These are already fixed at the lowest price, so I can't reduce it further."
                    if language == "english"
                    else f"Bro, total ₹{total} hai. Ye already lowest fixed price hai, isme aur discount possible nahi hai."
                )
            else:
                reply = (
                    f"For all of them together, I can make it ₹{combined_offer}."
                    if language == "english"
                    else f"Bro, sab mila ke ₹{combined_offer} kar dunga."
                )
            state["language"] = language
            state["current_offer"] = combined_offer
            state["accepted_price"] = 0
            state["status"] = "BUNDLE_PRICE_OFFERED"
            state["last_seller_reply"] = reply
            _save_state(production_db_path, chat_id, state)
            await _send_target_reply(
                context,
                production_db_path,
                int(chat_id),
                state,
                reply,
                ref=ref,
                context_type="bundle_price_reply",
            )
            return True

        burst = _load_burst(production_db_path, chat_id)
        negotiation_like = _is_negotiation_message(raw, state)

        # A customer may send several short bargaining lines. Once one bargaining
        # burst is open, genuine negotiation joins it. Pricing remains deterministic.
        if negotiation_like:
            _append_or_touch_burst(
                context,
                production_db_path,
                chat_id,
                raw,
                append=True,
            )
            return True

        # Generic question in the middle of a bargaining burst must NOT consume a
        # negotiation turn or reset the active target/current offer. It does reset
        # the inactivity timer so the burst isn't finalized underneath the FAQ.
        if burst and FAQ_RE.search(raw):
            _append_or_touch_burst(
                context,
                production_db_path,
                chat_id,
                raw,
                append=False,
            )
            ai_turn = await _contextual_ai_turn(
                context,
                production_db_path,
                chat_id,
                raw,
                state,
                ref,
                force_known_faq=bool(KNOWN_CONTEXT_FAQ_RE.search(raw)),
            )
            reply = str(ai_turn.get("reply") or "").strip()
            if reply and not bool(ai_turn.get("route_to_matcher")):
                await _send_target_reply(
                    context,
                    production_db_path,
                    int(chat_id),
                    state,
                    reply,
                    ref=ref,
                    context_type="negotiation_faq_reply",
                )
                return True
            return False

        if burst:
            # "please bro", "student hu", etc. after a bargaining line may be
            # semantically incomplete alone; keep them in the same customer burst.
            if not PAYMENT_RE.search(raw):
                _append_or_touch_burst(
                    context,
                    production_db_path,
                    chat_id,
                    raw,
                    append=True,
                )
                return True

        # Accepted current price + payment can jump directly to handoff.
        current = _payment_amount(state)
        current_mentioned = bool(
            current and re.search(rf"(?:₹\s*)?{re.escape(str(current))}\b", raw)
        )
        if PAYMENT_RE.search(raw) and ACCEPT_RE.search(raw) and current_mentioned:
            state["accepted_price"] = current
            state["status"] = "ACCEPTED"
            _save_state(production_db_path, chat_id, state)
            return await _handoff_payment(context, production_db_path, chat_id, state)

        # A direct payment request after selection (with or without bargaining)
        # uses the last offered/accepted price, never a reloaded catalogue total.
        if PAYMENT_RE.search(raw) and not negotiation_like:
            return await _handoff_payment(context, production_db_path, chat_id, state)

        # Plain acceptance after a negotiation offer is remembered silently.
        if (
            int(state.get("negotiation_round") or 0) > 0
            and ACCEPT_RE.fullmatch(re.sub(r"[.!?]+$", "", raw).strip())
        ):
            state["accepted_price"] = current
            state["status"] = "ACCEPTED"
            _save_state(production_db_path, chat_id, state)
            return True

        # Active-negotiation FAQ / contextual query. This is the key disruption
        # fix: do not fall into a generic "batch/teacher name batao" clarification
        # when the target is already known. AI sees only current state + negotiation
        # state + current query (and direct reply reference when present).
        if FAQ_RE.search(raw) or PRICE_QUESTION_RE.search(raw):
            ai_turn = await _contextual_ai_turn(
                context,
                production_db_path,
                chat_id,
                raw,
                state,
                ref,
                force_known_faq=bool(KNOWN_CONTEXT_FAQ_RE.search(raw)),
            )
            reply = str(ai_turn.get("reply") or "").strip()
            if reply and not bool(ai_turn.get("route_to_matcher")):
                await _send_target_reply(
                    context,
                    production_db_path,
                    int(chat_id),
                    state,
                    reply,
                    ref=ref,
                    context_type="negotiation_faq_reply",
                )
                return True
            if bool(ai_turn.get("route_to_matcher")):
                return False

        # Possible different-batch reference that deterministic catalogue lookup
        # could not resolve. Ask AI to interpret the current query WITH current
        # and negotiation state before any generic matcher clarification appears.
        if _looks_like_new_batch_reference(raw) and not explicit_items:
            ai_turn = await _contextual_ai_turn(
                context,
                production_db_path,
                chat_id,
                raw,
                state,
                ref,
            )
            reply = str(ai_turn.get("reply") or "").strip()
            if reply and not bool(ai_turn.get("route_to_matcher")):
                await _send_target_reply(
                    context,
                    production_db_path,
                    int(chat_id),
                    state,
                    reply,
                    ref=ref,
                    context_type="context_ai_reply",
                )
                return True
            # route_to_matcher=True intentionally falls through to the mature
            # matching system, unchanged.
            return False

        # Non-FAQ unrelated conversational turns fall through unchanged.
        return False
