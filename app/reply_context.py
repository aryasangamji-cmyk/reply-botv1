import re
import time
import sqlite3
import json


# ============================================================
# CUSTOMER REPLY / QUOTED BATCH CONTEXT V1
# ============================================================

# REPLY_CONTEXT_LONG_STATE_V1
TTL_SECONDS = 2 * 60 * 60
MAX_MESSAGES = 100


def _con(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _norm(v):
    v = str(v or "").casefold()
    v = v.replace("_", " ")
    v = re.sub(r"[^\w₹\s.+/-]+", " ", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()


def ensure_schema(path):
    with _con(path) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS customer_reply_reference (
            chat_id TEXT PRIMARY KEY,

            referenced_message_id INTEGER NOT NULL DEFAULT 0,
            referenced_text TEXT NOT NULL DEFAULT '',
            reference_label TEXT NOT NULL DEFAULT '',

            started_at REAL NOT NULL DEFAULT 0,
            last_used_at REAL NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0
        )
        """)

        # REPLY_THREAD_CONTEXT_V32
        # Persistent bidirectional Telegram message -> business target mapping.
        # This table is additive only; the mature matching tables are untouched.
        con.execute("""
        CREATE TABLE IF NOT EXISTS telegram_message_business_context (
            chat_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            direction TEXT NOT NULL DEFAULT '',
            context_type TEXT NOT NULL DEFAULT '',
            target_key TEXT NOT NULL DEFAULT '',
            target_name TEXT NOT NULL DEFAULT '',
            target_price INTEGER NOT NULL DEFAULT 0,
            negotiation_round INTEGER NOT NULL DEFAULT 0,
            current_price INTEGER NOT NULL DEFAULT 0,
            parent_message_id INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, message_id)
        )
        """)
        con.execute("""
        CREATE INDEX IF NOT EXISTS idx_tmbc_target
        ON telegram_message_business_context(chat_id, target_key, updated_at)
        """)
        con.execute("""
        CREATE INDEX IF NOT EXISTS idx_tmbc_target_name
        ON telegram_message_business_context(chat_id, target_name, updated_at)
        """)

        con.commit()


def _clean_line(v):
    v = str(v or "").strip()

    # remove common leading emoji / bullets without destroying text
    v = re.sub(
        r"^[\s•●▪▫►▶➤➜✅🎓📚📖🏆💰💵🪙⏱⏰❓🎬]+",
        "",
        v
    ).strip()

    return v


def extract_label(text):
    """
    Pick a concise course/batch title from the referenced
    outgoing message/caption.

    We deliberately DO NOT trust customer text for this.
    """
    text = str(text or "").strip()

    if not text:
        return ""

    lines = [
        _clean_line(x)
        for x in text.splitlines()
        if _clean_line(x)
    ]

    if not lines:
        return ""

    bad_prefixes = (
        "fee",
        "price",
        "duration",
        "timing",
        "demo",
        "purchase",
        "click here",
        "faq",
        "year",
    )

    # Prefer title-looking first lines.
    for line in lines[:8]:
        n = _norm(line)

        if not n:
            continue

        if any(
            n.startswith(x)
            for x in bad_prefixes
        ):
            continue

        # don't use huge descriptive paragraphs as label
        if len(line) <= 100:
            return line

    return lines[0][:100]


def extract_price(text):
    """
    Extract advertised price from the REFERENCED MESSAGE only.

    Supported examples:
      Fee: 400
      Fee ₹400
      Price: 500
      ₹1499
    """
    text = str(text or "")

    patterns = [
        r"(?i)\b(?:fee|price|cost)\s*[:\-~]?\s*₹?\s*([0-9]{2,6})\b",
        r"₹\s*([0-9]{2,6})\b",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)

        if m:
            return "₹" + m.group(1)

    return ""


def extract_years(text):
    years = []

    for y in re.findall(
        r"\b20(?:2[0-9]|3[0-9])\b",
        str(text or "")
    ):
        if y not in years:
            years.append(y)

    return years


# ============================================================
# REPLY THREAD / MESSAGE BUSINESS CONTEXT V32
# ============================================================

def _safe_int(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return int(default or 0)


def save_message_context(
    path,
    chat_id,
    message_id,
    *,
    direction="",
    context_type="",
    target_key="",
    target_name="",
    target_price=0,
    negotiation_round=0,
    current_price=0,
    parent_message_id=0,
    metadata=None,
):
    """Attach business context to one real Telegram message ID.

    This does not change matching, selection, negotiation or payment state.
    It only remembers what a Telegram message was about so a future reply can
    recover that context without spending AI tokens on target identification.
    """
    ensure_schema(path)
    mid = _safe_int(message_id)
    if not mid:
        return None
    now = time.time()
    try:
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        meta_json = "{}"
    with _con(path) as con:
        con.execute(
            """
            INSERT INTO telegram_message_business_context(
                chat_id,message_id,direction,context_type,target_key,target_name,
                target_price,negotiation_round,current_price,parent_message_id,
                metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,message_id) DO UPDATE SET
                direction=excluded.direction,
                context_type=excluded.context_type,
                target_key=excluded.target_key,
                target_name=excluded.target_name,
                target_price=excluded.target_price,
                negotiation_round=excluded.negotiation_round,
                current_price=excluded.current_price,
                parent_message_id=excluded.parent_message_id,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                str(chat_id), mid, str(direction or ""), str(context_type or ""),
                str(target_key or ""), str(target_name or ""), _safe_int(target_price),
                _safe_int(negotiation_round), _safe_int(current_price),
                _safe_int(parent_message_id), meta_json, now, now,
            ),
        )
        con.commit()
    return get_message_context(path, chat_id, mid)


def get_message_context(path, chat_id, message_id):
    ensure_schema(path)
    mid = _safe_int(message_id)
    if not mid:
        return None
    with _con(path) as con:
        row = con.execute(
            """
            SELECT * FROM telegram_message_business_context
            WHERE chat_id=? AND message_id=? LIMIT 1
            """,
            (str(chat_id), mid),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.get("metadata_json") or "{}")
    except Exception:
        item["metadata"] = {}
    return item


def get_target_anchor(path, chat_id, *, target_key="", target_name=""):
    """Return the best visible message to reply to for one target.

    Prefer the original outgoing batch card so the customer can visually see
    which batch the answer belongs to.  Fall back to any mapped message for the
    same target if a card is unavailable.
    """
    ensure_schema(path)
    key = str(target_key or "").strip()
    name = str(target_name or "").strip()
    if not key and not name:
        return None
    where = "target_key=?" if key else "lower(target_name)=lower(?)"
    value = key if key else name
    with _con(path) as con:
        row = con.execute(
            f"""
            SELECT message_id FROM telegram_message_business_context
            WHERE chat_id=? AND {where}
            ORDER BY
                CASE WHEN context_type IN ('batch_card','combo_card') THEN 0 ELSE 1 END,
                updated_at DESC
            LIMIT 1
            """,
            (str(chat_id), value),
        ).fetchone()
    if not row:
        return None
    return _safe_int(row["message_id"]) or None


def _attach_business_context(path, chat_id, ref):
    if not ref:
        return ref
    out = dict(ref)
    ctx = get_message_context(
        path,
        chat_id,
        out.get("referenced_message_id"),
    )
    if not ctx:
        return out
    out["business_context"] = ctx
    # Duplicate the commonly-used target fields at the top level for simple,
    # backward-compatible consumers. Existing reply-context logic ignores them.
    for key in (
        "target_key", "target_name", "target_price", "context_type",
        "negotiation_round", "current_price", "parent_message_id", "direction",
    ):
        out[key] = ctx.get(key)
    return out


def save_reference(
    path,
    chat_id,
    referenced_message_id,
    referenced_text,
):
    ensure_schema(path)

    text = str(referenced_text or "").strip()

    if not text:
        return None

    label = extract_label(text)
    now = time.time()

    with _con(path) as con:
        con.execute("""
        INSERT INTO customer_reply_reference
        (
            chat_id,
            referenced_message_id,
            referenced_text,
            reference_label,
            started_at,
            last_used_at,
            message_count
        )
        VALUES(?,?,?,?,?,?,0)

        ON CONFLICT(chat_id) DO UPDATE SET
            referenced_message_id=excluded.referenced_message_id,
            referenced_text=excluded.referenced_text,
            reference_label=excluded.reference_label,
            started_at=excluded.started_at,
            last_used_at=excluded.last_used_at,
            message_count=0
        """, (
            str(chat_id),
            int(referenced_message_id or 0),
            text,
            label,
            now,
            now,
        ))

        con.commit()

    return _attach_business_context(path, chat_id, {
        "chat_id": str(chat_id),
        "referenced_message_id": int(
            referenced_message_id or 0
        ),
        "referenced_text": text,
        "reference_label": label,
        "message_count": 0,
        "started_at": now,
    })


def get_reference(path, chat_id, touch=False):
    ensure_schema(path)

    now = time.time()

    with _con(path) as con:
        row = con.execute("""
        SELECT
            chat_id,
            referenced_message_id,
            referenced_text,
            reference_label,
            started_at,
            last_used_at,
            message_count
        FROM customer_reply_reference
        WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

        if not row:
            return None

        item = dict(row)

        expired = (
            now - float(
                item.get("last_used_at")
                or item.get("started_at")
                or 0
            )
            >= TTL_SECONDS
        )

        exhausted = (
            int(item["message_count"] or 0)
            >= MAX_MESSAGES
        )

        if expired or exhausted:
            con.execute(
                "DELETE FROM customer_reply_reference WHERE chat_id=?",
                (str(chat_id),)
            )
            con.commit()
            return None

        if touch:
            count = int(item["message_count"] or 0) + 1

            if count > MAX_MESSAGES:
                con.execute(
                    "DELETE FROM customer_reply_reference WHERE chat_id=?",
                    (str(chat_id),)
                )
                con.commit()
                return None

            con.execute("""
            UPDATE customer_reply_reference
            SET
                message_count=?,
                last_used_at=?
            WHERE chat_id=?
            """, (
                count,
                now,
                str(chat_id),
            ))

            con.commit()
            item["message_count"] = count
            item["last_used_at"] = now

        return _attach_business_context(path, chat_id, item)


def clear_reference(path, chat_id):
    ensure_schema(path)

    with _con(path) as con:
        con.execute(
            "DELETE FROM customer_reply_reference WHERE chat_id=?",
            (str(chat_id),)
        )
        con.commit()


def is_reference_followup(message):
    """
    True when the customer is speaking comparatively /
    referentially without naming the batch again.
    """
    n = _norm(message)

    if not n:
        return False

    # Direct reference language.
    phrases = (
        "this",
        "this one",
        "only this",
        "i want this",
        "i only want this",
        "want this",
        "it",
        "that",
        "that one",

        "ye",
        "yeh",
        "yahi",
        "ye wala",
        "ye wali",
        "yehi",
        "iska",
        "iski",
        "iske",
        "isme",
        "iss",
        "is batch",
        "is course",
        "wahi",
        "wo wala",
        "woh wala",
        "uska",
        "uski",
        "usme",
    )

    if any(
        re.search(
            r"(?<!\w)"
            + re.escape(_norm(p))
            + r"(?!\w)",
            n
        )
        for p in phrases
    ):
        return True

    # Common short follow-ups where current reference is useful.
    short_intents = (
        "price",
        "price kya hai",
        "kitne ka",
        "kitna hai",
        "year",
        "which year",
        "kis year",
        "demo",
        "demo hai",
        "validity",
        "available",
        "available hai",
        "latest",
        "notes",
        "lectures",
        "pdf",
        "pdfs",

        # common referential conversational follow-ups
        "how much",
        "hwo much",
        "how mch",
        "howmuch",
        "much for it",
        "batao",
        "batao bro",
        "batao brother",
        "tell me",
        "tell me bro",
    )

    if len(n) <= 70:
        if any(x in n for x in short_intents):
            return True

    return False


def deterministic_answer(message, ref):
    """
    Answer only facts that can safely be extracted from the
    referenced outgoing message.

    Never infer or overwrite DB data from customer statements.
    """
    if not ref:
        return None

    n = _norm(message)

    reference_text = ref.get(
        "referenced_text",
        ""
    )

    label = ref.get(
        "reference_label",
        ""
    )

    price = extract_price(reference_text)
    years = extract_years(reference_text)

    # REPLY_CONTEXT_TEACHER_V2
    # --------------------------------------------------------
    # TEACHER / FACULTY FROM REFERENCED POST
    # --------------------------------------------------------
    if (
        "who teaches" in n
        or "teacher" in n
        or "faculty" in n
        or "kaun padhata" in n
        or "kaun padhati" in n
        or "kaun padhate" in n
    ):
        teachers = []

        for line in str(reference_text or "").splitlines():
            # Typical generated batch line:
            # "• Paper 1 — Sunil Sir — Included"
            parts = [
                x.strip()
                for x in re.split(r"[—|]", line)
                if x.strip()
            ]

            for item in parts:
                if re.search(
                    r"(?i)\b(?:sir|mam|maam|ma'am)\b",
                    item
                ):
                    cleaned = re.sub(
                        r"^[•\-\s]+",
                        "",
                        item
                    ).strip()

                    if (
                        cleaned
                        and cleaned not in teachers
                    ):
                        teachers.append(cleaned)

        if teachers:
            return ", ".join(teachers)

        return ""

    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------
    # REPLY_CONTEXT_V2
    if (
        "price" in n
        or "fee" in n
        or "cost" in n
        or "kitne ka" in n
        or "kitna ka" in n
        or "kitni ki" in n
        or "how much" in n
        or "hwo much" in n
        or "how mch" in n
        or "howmuch" in n
        or "much for it" in n
        or "price iska" in n
        or "iska price" in n
        or "uska price" in n
    ):
        if price:
            return price

        # Referenced post exists but price isn't in it.
        # Stay silent rather than inventing.
        return ""

    # --------------------------------------------------------
    # YEAR
    # --------------------------------------------------------
    if (
        "year" in n
        or "kis year" in n
        or "which year" in n
    ):
        if years:
            return ", ".join(years)

        return ""

    # --------------------------------------------------------
    # WHAT IS THIS / NAME
    # --------------------------------------------------------
    if (
        "which batch" in n
        or "which course" in n
        or "batch name" in n
        or "course name" in n
        or "ye konsa" in n
        or "ye kaunsa" in n
        or "iska naam" in n
    ):
        return label or ""

    # --------------------------------------------------------
    # CUSTOMER CHOOSES THE REFERENCED ITEM
    # --------------------------------------------------------
    selection_phrases = (
        "i want this",
        "i only want this",
        "only this",
        "want this",
        "ye chahiye",
        "yeh chahiye",
        "yahi chahiye",
        "ye wala chahiye",
        "yehi chahiye",
        "wahi chahiye",
    )

    if any(
        _norm(x) in n
        for x in selection_phrases
    ):
        # Keep response intentionally short.
        return "Okay bro."

    return None


def concise_context(ref):
    if not ref:
        return ""

    label = str(
        ref.get("target_name")
        or ref.get("reference_label")
        or ""
    ).strip()

    mapped_price = _safe_int(ref.get("current_price") or ref.get("target_price"))
    price = (
        ("₹" + str(mapped_price))
        if mapped_price > 0
        else extract_price(ref.get("referenced_text") or "")
    )

    years = extract_years(
        ref.get("referenced_text")
        or ""
    )

    bits = []

    if label:
        bits.append(
            "Referenced batch/course: " + label
        )

    if price:
        bits.append(
            "Advertised price in referenced post: " + price
        )

    if years:
        bits.append(
            "Year(s) visible in referenced post: "
            + ", ".join(years)
        )

    return " | ".join(bits)
