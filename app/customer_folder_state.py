import re
import sqlite3
import time

from app.reply_context import clear_reference
from app.state_reply_anchor import set_anchor


TTL_SECONDS = 2 * 60 * 60
MAX_MESSAGES = 100


def _con(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _norm(v):
    v = str(v or "").casefold().replace("_", " ")
    v = re.sub(r"[^\w₹\s.+/&-]+", " ", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()


def _phrase(text, value):
    t = _norm(text)
    v = _norm(value)
    if not v:
        return False
    return bool(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", t))


def _aliases(v):
    return [
        x.strip()
        for x in re.split(r"[,|;\n]+", str(v or ""))
        if x.strip()
    ]


def ensure_schema(path):
    with _con(path) as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS customer_folder_state (
            chat_id TEXT PRIMARY KEY,
            batch_id INTEGER NOT NULL,
            part_id INTEGER,
            started_at REAL NOT NULL,
            last_active_at REAL NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        )
        """)
        db.commit()


def clear_state(path, chat_id):
    ensure_schema(path)
    with _con(path) as db:
        db.execute(
            "DELETE FROM customer_folder_state WHERE chat_id=?",
            (str(chat_id),)
        )
        db.commit()


def _batch(path, bid):
    with _con(path) as db:
        r = db.execute("""
            SELECT *
            FROM new_batch_folder_batches
            WHERE id=? AND enabled=1
        """, (int(bid),)).fetchone()
    return dict(r) if r else None


def _part(path, pid):
    if not pid:
        return None
    with _con(path) as db:
        r = db.execute("""
            SELECT *
            FROM new_batch_folder_parts
            WHERE id=? AND enabled=1
        """, (int(pid),)).fetchone()
    return dict(r) if r else None


def _parts(path, bid):
    with _con(path) as db:
        return [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_folder_parts
                WHERE batch_id=? AND enabled=1
                ORDER BY sort_order,id
            """, (int(bid),)).fetchall()
        ]


def get_state(path, chat_id):
    ensure_schema(path)
    now = time.time()

    with _con(path) as db:
        r = db.execute("""
            SELECT *
            FROM customer_folder_state
            WHERE chat_id=?
        """, (str(chat_id),)).fetchone()

        if not r:
            return None

        x = dict(r)

        if (
            now - float(x["last_active_at"] or 0) >= TTL_SECONDS
            or int(x["message_count"] or 0) >= MAX_MESSAGES
        ):
            db.execute(
                "DELETE FROM customer_folder_state WHERE chat_id=?",
                (str(chat_id),)
            )
            db.commit()
            return None

        return x


def _save(path, chat_id, bid, pid=None):
    ensure_schema(path)
    now = time.time()
    old = get_state(path, chat_id)

    same = bool(
        old
        and int(old["batch_id"]) == int(bid)
        and int(old["part_id"] or 0) == int(pid or 0)
    )

    started = (
        float(old["started_at"])
        if same
        else now
    )
    count = (
        int(old["message_count"] or 0) + 1
        if same
        else 1
    )

    with _con(path) as db:
        db.execute("""
        INSERT INTO customer_folder_state
        (chat_id,batch_id,part_id,started_at,last_active_at,message_count)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            batch_id=excluded.batch_id,
            part_id=excluded.part_id,
            started_at=excluded.started_at,
            last_active_at=excluded.last_active_at,
            message_count=excluded.message_count
        """, (
            str(chat_id),
            int(bid),
            int(pid) if pid else None,
            started,
            now,
            count,
        ))
        db.commit()

    return get_state(path, chat_id)


def _touch(path, chat_id):
    x = get_state(path, chat_id)
    if not x:
        return None

    with _con(path) as db:
        db.execute("""
            UPDATE customer_folder_state
            SET last_active_at=?,
                message_count=message_count+1
            WHERE chat_id=?
        """, (time.time(), str(chat_id)))
        db.commit()

    return get_state(path, chat_id)


def _batch_aliases(b):
    vals = [
        b.get("name", ""),
        b.get("teacher", ""),
    ]
    vals += _aliases(b.get("aliases"))

    return sorted(
        {_norm(x) for x in vals if _norm(x)},
        key=len,
        reverse=True,
    )


def _part_aliases(p):
    vals = [
        p.get("name", ""),
        p.get("teacher", ""),
    ]
    vals += _aliases(p.get("aliases"))

    return sorted(
        {_norm(x) for x in vals if _norm(x)},
        key=len,
        reverse=True,
    )


def _find_batch(path, text):
    with _con(path) as db:
        rows = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_folder_batches
                WHERE enabled=1
            """).fetchall()
        ]

    scored = []

    for b in rows:
        best = 0

        for a in _batch_aliases(b):
            if _phrase(text, a):
                best = max(best, len(a.split()) * 10 + len(a))

        if best:
            scored.append((best, b))

    if not scored:
        return None

    scored.sort(key=lambda z: z[0], reverse=True)

    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None

    return scored[0][1]


def _matching_parts(path, bid, text):
    out = []

    for p in _parts(path, bid):
        score = 0

        for a in _part_aliases(p):
            if _phrase(text, a):
                score = max(score, len(a))

        if score:
            out.append((score, p))

    out.sort(key=lambda z: z[0], reverse=True)
    return [p for _, p in out]


def _price_intent(text):
    n = _norm(text)
    return any(x in n for x in (
        "price", "fee", "cost", "how much", "hwo much",
        "how mch", "kitna", "kitne",
    ))


def _year_intent(text):
    n = _norm(text)
    return "year" in n or "kis year" in n or "which year" in n


def _teacher_intent(text):
    n = _norm(text)
    return any(x in n for x in (
        "who teaches", "teacher", "faculty", "kaun padhata",
        "kaun padhati", "kaun padhate",
    ))


def _availability_intent(text):
    n = _norm(text)
    return any(x in n for x in (
        "available", "availability", "hai kya", "do you have",
    ))


def _money(x):
    x = str(x or "").strip()
    if not x:
        return ""
    return x if x.startswith("₹") else "₹" + x


def _part_line(b, p):
    line = f'{b["name"]} — {p["name"]}'

    if p.get("teacher"):
        line += f'\n{p["teacher"]}'

    if p.get("years"):
        line += f'\nYear: {p["years"]}'

    if p.get("price"):
        line += f'\nPrice: {_money(p["price"])}'

    elif not p.get("standalone", 1):
        line += "\nIncluded with complete batch."

    return line


# STATE_REPLY_ANCHOR_FOLDER_V1
def process(
    path,
    chat_id,
    text,
    direct_reply=False,
    message_id=None,
):
    """
    Folder-based state (Sociology etc.).
    Runs before stale reply-reference/history routing.
    """
    if direct_reply:
        return {"handled": False, "reply": "", "explicit": False}

    text = str(text or "").strip()
    if not text:
        return {"handled": False, "reply": "", "explicit": False}

    n = _norm(text)
    explicit = _find_batch(path, text)

    # DIRECT_FOLDER_ANCHOR_V8
    if explicit:
        if message_id:
            set_anchor(
                path,
                chat_id,
                message_id,
                source="folder_explicit",
            )
        # New explicit folder/batch becomes the new state anchor.
        if message_id:
            set_anchor(
                path,
                chat_id,
                message_id,
                source="folder_explicit"
            )

        # New explicit topic invalidates an older Telegram reference.
        clear_reference(path, chat_id)

        matches = _matching_parts(
            path,
            explicit["id"],
            text
        )

        # PW two-faculty price.
        if (
            explicit.get("slug") == "physics-wallah-sociology"
            and _price_intent(text)
            and re.search(r"\b(?:sekhar|shekhar)\b", n)
            and re.search(r"\bnitin\b", n)
        ):
            _save(path, chat_id, explicit["id"], None)
            return {
                "handled": True,
                "reply": "Sekhar Sir — ₹500\nNitin Sir — ₹500",
                "explicit": True,
            }

        # Level Up foundation + crash combined.
        if (
            explicit.get("slug") == "level-up-sociology"
            and _price_intent(text)
            and (
                "dono" in n
                or "both" in n
                or (
                    "foundation" in n
                    and "crash" in n
                )
            )
        ):
            _save(path, chat_id, explicit["id"], None)
            return {
                "handled": True,
                "reply": _money(explicit.get("combined_price")),
                "explicit": True,
            }

        chosen = matches[0] if len(matches) == 1 else None

        if chosen:
            _save(
                path,
                chat_id,
                explicit["id"],
                chosen["id"]
            )

            # Pranay Answer Writing is included, not separately priced.
            if (
                explicit.get("slug") == "pranay-agarwal"
                and "answer writing" in _norm(chosen.get("name"))
                and _price_intent(text)
            ):
                return {
                    "handled": True,
                    "reply": (
                        "Answer Writing complete Pranay Agarwal "
                        "Sociology batch ke saath included hai bro."
                    ),
                    "explicit": True,
                }

            # Answer direct price/year requests here.
            if _price_intent(text):
                if chosen.get("price"):
                    return {
                        "handled": True,
                        "reply": _money(chosen["price"]),
                        "explicit": True,
                    }

                if not chosen.get("standalone", 1):
                    if explicit.get("price"):
                        return {
                            "handled": True,
                            "reply": _money(explicit["price"]),
                            "explicit": True,
                        }

            if _year_intent(text):
                year = str(
                    chosen.get("years")
                    or explicit.get("year")
                    or ""
                ).strip()

                if year:
                    return {
                        "handled": True,
                        "reply": year,
                        "explicit": True,
                    }

        else:
            _save(path, chat_id, explicit["id"], None)

            if _price_intent(text):
                price = (
                    explicit.get("price")
                    or explicit.get("combined_price")
                )
                if price:
                    return {
                        "handled": True,
                        "reply": _money(price),
                        "explicit": True,
                    }

            if _year_intent(text) and explicit.get("year"):
                return {
                    "handled": True,
                    "reply": str(explicit["year"]),
                    "explicit": True,
                }

        # Existing folder router handles normal explicit details.
        return {
            "handled": False,
            "reply": "",
            "explicit": True,
        }

    # --------------------------------------------------------
    # SHORT FOLLOW-UP USING ACTIVE FOLDER STATE
    # --------------------------------------------------------
    state = get_state(path, chat_id)

    if not state:
        return {
            "handled": False,
            "reply": "",
            "explicit": False,
        }

    b = _batch(path, state["batch_id"])
    p = _part(path, state.get("part_id"))

    if not b:
        clear_state(path, chat_id)
        return {
            "handled": False,
            "reply": "",
            "explicit": False,
        }

    if _price_intent(text):
        _touch(path, chat_id)

        if p:
            if p.get("price"):
                return {
                    "handled": True,
                    "reply": _money(p["price"]),
                    "explicit": False,
                }

            if not p.get("standalone", 1) and b.get("price"):
                return {
                    "handled": True,
                    "reply": _money(b["price"]),
                    "explicit": False,
                }

        price = b.get("price") or b.get("combined_price")

        if price:
            return {
                "handled": True,
                "reply": _money(price),
                "explicit": False,
            }

    if _year_intent(text):
        _touch(path, chat_id)

        if p and p.get("years"):
            return {
                "handled": True,
                "reply": str(p["years"]).replace(",", ", "),
                "explicit": False,
            }

        if b.get("year"):
            return {
                "handled": True,
                "reply": str(b["year"]),
                "explicit": False,
            }

        years = []
        for x in _parts(path, b["id"]):
            for y in re.findall(r"\b20\d{2}\b", str(x.get("years") or "")):
                if y not in years:
                    years.append(y)

        if years:
            return {
                "handled": True,
                "reply": ", ".join(years),
                "explicit": False,
            }

    if _teacher_intent(text):
        _touch(path, chat_id)

        if p and p.get("teacher"):
            return {
                "handled": True,
                "reply": p["teacher"],
                "explicit": False,
            }

        if b.get("teacher"):
            return {
                "handled": True,
                "reply": b["teacher"],
                "explicit": False,
            }

        teachers = []
        for x in _parts(path, b["id"]):
            t = str(x.get("teacher") or "").strip()
            if t and t not in teachers:
                teachers.append(t)

        if teachers:
            return {
                "handled": True,
                "reply": ", ".join(teachers),
                "explicit": False,
            }

    if _availability_intent(text):
        _touch(path, chat_id)
        return {
            "handled": True,
            "reply": "Haan bro, available hai.",
            "explicit": False,
        }

    return {
        "handled": False,
        "reply": "",
        "explicit": False,
    }
