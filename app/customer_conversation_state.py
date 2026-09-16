import re
import sqlite3
import time

from app.reply_context import clear_reference
from app.customer_folder_state import process as _folder_state_preflight
from app.state_reply_anchor import set_anchor


# ============================================================
# CUSTOMER CONVERSATION STATE V1
#
# Server-side only.
# No OpenAI calls.
#
# Active state expires after 2 HOURS OF INACTIVITY.
# Related messages refresh it.
# Hard safety cap: 100 contextual customer messages.
# ============================================================

TTL_SECONDS = 2 * 60 * 60
MAX_MESSAGES = 100


def _con(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _norm(v):
    v = str(v or "").casefold()
    v = v.replace("_", " ")
    v = re.sub(r"[^\w₹\s.+/&-]+", " ", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()


def _phrase(text, value):
    text = _norm(text)
    value = _norm(value)

    if not value:
        return False

    return bool(
        re.search(
            r"(?<!\w)"
            + re.escape(value)
            + r"(?!\w)",
            text
        )
    )


def _split_aliases(v):
    return [
        x.strip()
        for x in re.split(
            r"[,|;\n]+",
            str(v or "")
        )
        if x.strip()
    ]


def _is_hinglish(text):
    raw = str(text or "")
    n = _norm(raw)

    if re.search(r"[\u0900-\u097f]", raw):
        return True

    markers = {
        "hai", "hain", "h",
        "nahi", "nhi", "ni",
        "kya",
        "ka", "ki", "ke", "ko",
        "me", "mein",
        "chahiye", "chahie",
        "kaha", "kahaan", "kahan",
        "kahi", "kahin",
        "milega", "milegi", "mile",
        "mil", "sakta", "sakte",
        "pata", "batao",
        "kitna", "kitne", "kitni",
        "konsa", "konsi",
        "kaunsa", "kaunsi",
        "kis",
        "dono",
        "mere", "paas", "pas",
        "wala", "wali",
    }

    return bool(
        set(n.split()) & markers
    )


def ensure_schema(path):
    with _con(path) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS customer_conversation_state (
            chat_id TEXT PRIMARY KEY,

            teacher_id INTEGER,
            teacher_name TEXT NOT NULL DEFAULT '',
            teacher_slug TEXT NOT NULL DEFAULT '',

            subject_id INTEGER,
            subject_name TEXT NOT NULL DEFAULT '',

            availability INTEGER,

            source TEXT NOT NULL DEFAULT '',
            last_customer_message TEXT NOT NULL DEFAULT '',

            started_at REAL NOT NULL DEFAULT 0,
            last_active_at REAL NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0
        )
        """)

        con.commit()


def clear_state(path, chat_id):
    ensure_schema(path)

    with _con(path) as con:
        con.execute(
            "DELETE FROM customer_conversation_state WHERE chat_id=?",
            (str(chat_id),)
        )
        con.commit()


def get_state(path, chat_id):
    ensure_schema(path)

    now = time.time()

    with _con(path) as con:
        row = con.execute("""
            SELECT *
            FROM customer_conversation_state
            WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

        if not row:
            return None

        item = dict(row)

        last_active = float(
            item.get("last_active_at")
            or item.get("started_at")
            or 0
        )

        expired = (
            now - last_active
            >= TTL_SECONDS
        )

        exhausted = (
            int(item.get("message_count") or 0)
            >= MAX_MESSAGES
        )

        if expired or exhausted:
            con.execute(
                "DELETE FROM customer_conversation_state WHERE chat_id=?",
                (str(chat_id),)
            )
            con.commit()
            return None

        return item


def _save_state(
    path,
    chat_id,
    teacher,
    subject=None,
    source="explicit",
    customer_message="",
):
    ensure_schema(path)

    now = time.time()

    current = get_state(
        path,
        chat_id
    )

    same_teacher = bool(
        current
        and int(current.get("teacher_id") or 0)
        == int(teacher.get("id") or 0)
    )

    same_subject = bool(
        (
            not subject
            and not int(
                (current or {}).get("subject_id")
                or 0
            )
        )
        or (
            subject
            and current
            and int(current.get("subject_id") or 0)
            == int(subject.get("id") or 0)
        )
    )

    if same_teacher and same_subject:
        started_at = float(
            current.get("started_at")
            or now
        )
        count = int(
            current.get("message_count")
            or 0
        ) + 1

    else:
        started_at = now
        count = 1

    availability = None

    if subject is not None:
        availability = int(
            bool(subject.get("availability"))
        )

    with _con(path) as con:
        con.execute("""
        INSERT INTO customer_conversation_state
        (
            chat_id,

            teacher_id,
            teacher_name,
            teacher_slug,

            subject_id,
            subject_name,

            availability,

            source,
            last_customer_message,

            started_at,
            last_active_at,
            message_count
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)

        ON CONFLICT(chat_id) DO UPDATE SET
            teacher_id=excluded.teacher_id,
            teacher_name=excluded.teacher_name,
            teacher_slug=excluded.teacher_slug,

            subject_id=excluded.subject_id,
            subject_name=excluded.subject_name,

            availability=excluded.availability,

            source=excluded.source,
            last_customer_message=excluded.last_customer_message,

            started_at=excluded.started_at,
            last_active_at=excluded.last_active_at,
            message_count=excluded.message_count
        """, (
            str(chat_id),

            int(teacher.get("id") or 0),
            str(teacher.get("name") or ""),
            str(teacher.get("slug") or ""),

            (
                int(subject.get("id"))
                if subject
                else None
            ),
            (
                str(subject.get("name") or "")
                if subject
                else ""
            ),

            availability,

            str(source or ""),
            str(customer_message or ""),

            started_at,
            now,
            count,
        ))

        con.commit()

    return get_state(
        path,
        chat_id
    )


def _touch_state(
    path,
    chat_id,
    message,
):
    state = get_state(
        path,
        chat_id
    )

    if not state:
        return None

    now = time.time()
    count = int(
        state.get("message_count")
        or 0
    ) + 1

    if count > MAX_MESSAGES:
        clear_state(
            path,
            chat_id
        )
        return None

    with _con(path) as con:
        con.execute("""
        UPDATE customer_conversation_state
        SET
            last_active_at=?,
            message_count=?,
            last_customer_message=?
        WHERE chat_id=?
        """, (
            now,
            count,
            str(message or ""),
            str(chat_id),
        ))

        con.commit()

    return get_state(
        path,
        chat_id
    )


# ============================================================
# STRUCTURED NEW-BATCH LOOKUP
# ============================================================

def _teachers(path):
    try:
        with _con(path) as con:
            return [
                dict(r)
                for r in con.execute("""
                    SELECT *
                    FROM structured_batch_teachers
                    WHERE enabled=1
                    ORDER BY id
                """).fetchall()
            ]
    except Exception:
        return []


def _subjects(path, teacher_id):
    try:
        with _con(path) as con:
            return [
                dict(r)
                for r in con.execute("""
                    SELECT *
                    FROM structured_batch_subjects
                    WHERE teacher_id=?
                      AND enabled=1
                    ORDER BY id
                """, (
                    int(teacher_id),
                )).fetchall()
            ]
    except Exception:
        return []


def _teacher_aliases(t):
    vals = [
        t.get("name", ""),
        t.get("slug", ""),
    ]

    vals += _split_aliases(
        t.get("aliases", "")
    )

    vals += _split_aliases(
        t.get("keywords", "")
    )

    # Short safe aliases for existing New Batches.
    slug = str(
        t.get("slug")
        or ""
    )

    extras = {
        "sudarshan-gurjar": [
            "sudarshan",
            "sudarshan sir",
            "gurjar sir",
            "gujar sir",
        ],
        "mrunal-patel": [
            "mrunal",
            "mrunal sir",
        ],
        "jatin-gupta": [
            "jatin",
            "jatin sir",
            "jatin gupta",
        ],
        "shivin-sir": [
            "shivin",
            "sivin",
            "shivin sir",
            "sivin sir",
        ],
        "ayushi-mam": [
            "ayushi",
            "ayushi mam",
            "ayushi maam",
        ],
        "sarmad-mehraj": [
            "sarmad",
            "sarmad sir",
            "sarmad mehraj",
        ],
        "basava-oppin": [
            "basava",
            "basava sir",
            "basava oppin",
        ],
        "smriti-mam": [
            "smriti",
            "smriti mam",
            "smriti maam",
            "smriti shah",
        ],
    }

    vals += extras.get(
        slug,
        []
    )

    return sorted(
        {
            _norm(x)
            for x in vals
            if _norm(x)
        },
        key=len,
        reverse=True
    )


def _subject_aliases(s):
    vals = [
        s.get("name", ""),
        s.get("part_name", ""),
        s.get("batch_code", ""),
    ]

    vals += _split_aliases(
        s.get("aliases", "")
    )

    n = _norm(
        s.get("name")
    )

    if n == "internal security":
        vals += [
            "internal security",
            "security",
        ]

    elif n == "governance":
        vals += [
            "governance",
        ]

    elif n == "polity":
        vals += [
            "polity",
        ]

    elif n in {
        "science technology",
        "science & technology",
    }:
        vals += [
            "science and technology",
            "science technology",
            "sci tech",
        ]

    elif n == "environment":
        vals += [
            "environment",
            "env",
        ]

    elif n == "international relations":
        vals += [
            "international relations",
            "international relation",
            "ir",
        ]

    elif n == "psir":
        vals += [
            "psir",
            "political science optional",
        ]

    return sorted(
        {
            _norm(x)
            for x in vals
            if _norm(x)
        },
        key=len,
        reverse=True
    )


def _find_teacher(path, text):
    n = _norm(text)

    matches = []

    for t in _teachers(path):
        score = 0

        for alias in _teacher_aliases(t):
            if _phrase(n, alias):
                score = max(
                    score,
                    len(alias) + 10
                )

        if score:
            matches.append(
                (score, t)
            )

    if not matches:
        return None

    matches.sort(
        key=lambda x: x[0],
        reverse=True
    )

    if (
        len(matches) > 1
        and matches[0][0] == matches[1][0]
    ):
        return None

    return matches[0][1]


def _find_subjects_for_teacher(
    path,
    teacher_id,
    text,
):
    n = _norm(text)

    matches = []

    for s in _subjects(
        path,
        teacher_id
    ):
        score = 0

        for alias in _subject_aliases(s):
            if _phrase(n, alias):
                score = max(
                    score,
                    len(alias)
                )

        if score:
            matches.append(
                (score, s)
            )

    if not matches:
        return []

    # Unique subject IDs only.
    matches.sort(
        key=lambda x: x[0],
        reverse=True
    )

    seen = set()
    out = []

    for score, s in matches:
        sid = int(
            s.get("id")
            or 0
        )

        if sid in seen:
            continue

        seen.add(sid)
        out.append(s)

    return out


def _subject_by_id(
    path,
    sid,
):
    if not sid:
        return None

    try:
        with _con(path) as con:
            r = con.execute("""
                SELECT *
                FROM structured_batch_subjects
                WHERE id=?
            """, (
                int(sid),
            )).fetchone()

            return dict(r) if r else None

    except Exception:
        return None


def _teacher_by_id(
    path,
    tid,
):
    if not tid:
        return None

    try:
        with _con(path) as con:
            r = con.execute("""
                SELECT *
                FROM structured_batch_teachers
                WHERE id=?
            """, (
                int(tid),
            )).fetchone()

            return dict(r) if r else None

    except Exception:
        return None


# ============================================================
# INTENT / CONTEXT RULES
# ============================================================

def _possession_statement(text):
    n = _norm(text)

    phrases = (
        "i have",
        "already have",
        "i already have",
        "mere pas",
        "mere paas",
        "mere pass",
        "already hai",
        "to i have",
    )

    return any(
        p in n
        for p in phrases
    )


def _where_find_intent(text):
    n = _norm(text)

    phrases = (
        "kaha milega",
        "kahan milega",
        "kahaan milega",
        "kaha mil",
        "kahan mil",
        "kahaan mil",
        "kahi mil",
        "kahin mil",
        "mil sakta",
        "mil sakte",
        "where can i get",
        "where can i find",
        "where to get",
        "where will i get",
        "where do i get",
        "any idea where",
        "any clue",
    )

    return any(
        p in n
        for p in phrases
    )


def _price_intent(text):
    n = _norm(text)

    return any(
        p in n
        for p in (
            "price",
            "fee",
            "cost",
            "how much",
            "hwo much",
            "how mch",
            "kitne ka",
            "kitna ka",
            "kitni ki",
        )
    )


def _year_intent(text):
    n = _norm(text)

    return any(
        p in n
        for p in (
            "year",
            "kis year",
            "which year",
        )
    )


def _availability_intent(text):
    n = _norm(text)

    return any(
        p in n
        for p in (
            "available",
            "availability",
            "do you have",
            "hai kya",
            "milega",
            "milegi",
        )
    )


def _unavailable_followup(text):
    n = _norm(text)

    if _where_find_intent(text):
        return "where"

    words = (
        "lecture",
        "lectures",
        "latest",
        "available",
        "availability",
        "price",
        "fee",
        "cost",
        "year",
        "demo",
        "batch",
        "course",
        "just ",
        "only ",
    )

    if any(
        x in n
        for x in words
    ):
        return "unavailable"

    return None


def _clear_followup(text):
    n = _norm(text)

    if not n:
        return False

    if (
        _where_find_intent(text)
        or _price_intent(text)
        or _year_intent(text)
        or _availability_intent(text)
    ):
        return True

    phrases = (
        "lectures",
        "lecture",
        "latest",
        "any clue",
        "any idea",
        "just governance",
        "only governance",
        "just this",
        "only this",
    )

    return (
        len(n) <= 80
        and any(
            p in n
            for p in phrases
        )
    )


def _available_reply(text):
    return (
        "Haan bro, available hai."
        if _is_hinglish(text)
        else "Yes bro, available."
    )


def _not_available_reply(text):
    return (
        "Available nahi h bro."
        if _is_hinglish(text)
        else "Not available bro."
    )


def _no_idea_reply(text):
    return (
        "Pata nahi bro."
        if _is_hinglish(text)
        else "No idea bro."
    )


# ============================================================
# MAIN PROCESSOR
# ============================================================

# STATE_REPLY_ANCHOR_STRUCTURED_V1
def process(
    path,
    chat_id,
    message,
    direct_reply_reference=False,
    message_id=None,
):
    """
    Returns:
      {
        "handled": bool,
        "reply": str,
        "state": dict|None
      }

    handled=False:
      continue normal routing.

    handled=True:
      this state layer owns the message.
      Empty reply means intentionally stay silent.
    """
    ensure_schema(path)

    text = str(
        message
        or ""
    ).strip()

    if not text:
        return {
            "handled": False,
            "reply": "",
            "state": None,
        }

    # Telegram direct-reply context has absolute priority.
    if direct_reply_reference:
        return {
            "handled": False,
            "reply": "",
            "state": get_state(
                path,
                chat_id
            ),
        }

    # STATE_PRIORITY_FOLDER_PREEMPT_V3
    # A newly named folder batch/course (PW Sociology, Level Up,
    # Vision Sociology, etc.) has priority over an older structured
    # state such as Sudarshan / Jatin / Mrunal.
    #
    # This preflight is zero-AI.
    try:
        _folder_pre = _folder_state_preflight(
            path,
            chat_id,
            message,
            direct_reply=False,
            message_id=message_id,
        )

        if _folder_pre.get("explicit"):
            # Folder processor already set its own fresh state + anchor.
            # If it can answer immediately (price/year/etc.), bubble
            # that answer directly through the main state router.
            if _folder_pre.get("handled"):
                return {
                    "handled": True,
                    "reply": str(_folder_pre.get("reply") or ""),
                    "state": None,
                }

            # Explicit folder/batch identified, but normal folder
            # presentation will be handled by the later folder router.
            # Crucially: do NOT let old structured state answer.
            return {
                "handled": False,
                "reply": "",
                "state": None,
            }

    except Exception:
        # Never break the working structured router because of preflight.
        pass

    old_state = get_state(
        path,
        chat_id
    )

    explicit_teacher = _find_teacher(
        path,
        text
    )

    # --------------------------------------------------------
    # EXPLICIT TEACHER
    # --------------------------------------------------------
    # CUSTOMER_STATE_ROUTING_V2
    # STATE_ANCHOR_SIGNAL_V7
    # DIRECT_STATE_ANCHOR_V8
    if explicit_teacher:
        if message_id:
            set_anchor(
                path,
                chat_id,
                message_id,
                source="structured_explicit",
            )
        # This customer message explicitly establishes/switches state.
        _anchor_new = True

        # Naming a teacher/course explicitly means a new topic.
        if message_id:
            set_anchor(
                path,
                chat_id,
                message_id,
                source="structured_explicit"
            )
        # Never allow an old Telegram reply-reference to override it.
        clear_reference(path, chat_id)

        subjects = _find_subjects_for_teacher(
            path,
            explicit_teacher["id"],
            text,
        )

        chosen = (
            subjects[0]
            if len(subjects) == 1
            else None
        )

        # Multi-part filtered request such as:
        # "Shivin sir prelims me kya available hai"
        if len(subjects) > 1:
            n = _norm(text)

            if (
                "available" in n
                or "prelims" in n
                or "mains" in n
            ):
                lines = []

                for _s in subjects:
                    if not bool(_s.get("availability")):
                        continue

                    _line = str(_s.get("name") or "")

                    _part = str(_s.get("part_name") or "").strip()
                    _code = str(_s.get("batch_code") or "").strip()

                    if _part:
                        _line += " — " + _part

                    if _code:
                        _line += " (" + _code + ")"

                    _price = str(_s.get("price") or "").strip()
                    if _price:
                        _line += " — " + (
                            _price
                            if _price.startswith("₹")
                            else "₹" + _price
                        )

                    lines.append(_line)

                if lines:
                    _save_state(
                        path,
                        chat_id,
                        explicit_teacher,
                        None,
                        source="explicit_teacher_multi",
                        customer_message=text,
                    )

                    return {
                        "handled": True,
                        "reply": "\n".join(lines),
                        "state": get_state(path, chat_id),
                        "anchor_new": True,
                    }

        state = _save_state(
            path,
            chat_id,
            explicit_teacher,
            chosen,
            source="explicit_teacher",
            customer_message=text,
        )

        if chosen:
            # Explicit unavailable subject is terminal.
            if not bool(chosen.get("availability")):
                return {
                    "handled": True,
                    "reply": _not_available_reply(text),
                    "state": state,
                    "anchor_new": True,
                }

            if _price_intent(text):
                price = str(chosen.get("price") or "").strip()

                if price:
                    return {
                        "handled": True,
                        "reply": (
                            price
                            if price.startswith("₹")
                            else "₹" + price
                        ),
                        "state": state,
                    }

            if _year_intent(text):
                year = str(chosen.get("year") or "").strip()

                if year:
                    return {
                        "handled": True,
                        "reply": year,
                        "state": state,
                    }

        # Let existing New Batches router produce normal details.
        return {
            "handled": False,
            "reply": "",
            "state": state,
            "anchor_new": True,
        }

    # --------------------------------------------------------
    # NO EXPLICIT TEACHER: use active teacher.
    # --------------------------------------------------------
    if not old_state:
        return {
            "handled": False,
            "reply": "",
            "state": None,
        }

    active_teacher = _teacher_by_id(
        path,
        old_state.get("teacher_id")
    )

    if not active_teacher:
        clear_state(
            path,
            chat_id
        )

        return {
            "handled": False,
            "reply": "",
            "state": None,
        }

    # MRUNAL_COMBINED_PRICE_V2_1
    # PCB + QEP together = ₹400.
    # Handle this BEFORE individual PCB/QEP subject selection.
    _active_n = _norm(text)
    _active_teacher_name = _norm(
        active_teacher.get("name")
        or ""
    )

    if (
        "mrunal" in _active_teacher_name
        and _price_intent(text)
        and "pcb" in _active_n
        and "qep" in _active_n
        and (
            "dono" in _active_n
            or "both" in _active_n
            or "together" in _active_n
        )
    ):
        _combined = str(
            active_teacher.get("combined_price")
            or active_teacher.get("all_price")
            or active_teacher.get("price_all")
            or "400"
        ).strip()

        clear_reference(
            path,
            chat_id
        )

        return {
            "handled": True,
            "reply": (
                _combined
                if _combined.startswith("₹")
                else "₹" + _combined
            ),
            "state": _touch_state(
                path,
                chat_id,
                text
            ),
        }

    subject_matches = _find_subjects_for_teacher(
        path,
        active_teacher["id"],
        text,
    )

    # Mrunal / multi-part combined price such as:
    # "PCB QEP dono ka price" / "Price for both PCB and QEP"
    if (
        len(subject_matches) > 1
        and _price_intent(text)
        and (
            "dono" in _norm(text)
            or "both" in _norm(text)
        )
    ):
        combined = str(
            active_teacher.get("combined_price")
            or ""
        ).strip()

        if combined:
            clear_reference(path, chat_id)

            return {
                "handled": True,
                "reply": (
                    combined
                    if combined.startswith("₹")
                    else "₹" + combined
                ),
                "state": _touch_state(path, chat_id, text),
            }

    # Customer says they ALREADY HAVE another subject.
    # Do NOT destroy/switch current requested state.
    possession = _possession_statement(
        text
    )

    if (
        subject_matches
        and not possession
        and len(subject_matches) == 1
    ):
        chosen = subject_matches[0]

        # Explicitly naming/selecting another subject changes
        # the visual Telegram state anchor as well.
        if message_id:
            set_anchor(
                path,
                chat_id,
                message_id,
                source="structured_subject",
            )

        if message_id:
            set_anchor(
                path,
                chat_id,
                message_id,
                source="structured_subject"
            )

        # A specific subject/part invalidates an older reply-reference.
        clear_reference(path, chat_id)

        state = _save_state(
            path,
            chat_id,
            active_teacher,
            chosen,
            source="active_teacher_subject",
            customer_message=text,
        )

        # If explicitly unavailable, answer immediately.
        if not bool(
            chosen.get("availability")
        ):
            return {
                "handled": True,
                "reply": _not_available_reply(text),
                "state": state,
            }

        if _price_intent(text):
            price = str(chosen.get("price") or "").strip()

            if price:
                return {
                    "handled": True,
                    "reply": (
                        price
                        if price.startswith("₹")
                        else "₹" + price
                    ),
                    "state": state,
                }

        if _year_intent(text):
            year = str(chosen.get("year") or "").strip()

            if year:
                return {
                    "handled": True,
                    "reply": year,
                    "state": state,
                }

        # Teacherless shorthand like "prelims wala?" / "mains wala?"
        n = _norm(text)

        if (
            "prelims" in n
            or "mains" in n
            or "pcb" in n
            or "qep" in n
        ):
            line = str(chosen.get("name") or "")

            part_name = str(chosen.get("part_name") or "").strip()
            batch_code = str(chosen.get("batch_code") or "").strip()
            year = str(chosen.get("year") or "").strip()
            price = str(chosen.get("price") or "").strip()

            if part_name:
                line += " — " + part_name

            if batch_code:
                line += " (" + batch_code + ")"

            if year:
                line += " | Year: " + year

            if price:
                line += " | Price: " + (
                    price
                    if price.startswith("₹")
                    else "₹" + price
                )

            return {
                "handled": True,
                "reply": line,
                "state": state,
            }

        return {
            "handled": False,
            "reply": "",
            "state": state,
        }

    # Keep existing state when customer merely says:
    # "polity security to i have"
    if possession:
        state = _touch_state(
            path,
            chat_id,
            text
        )

        return {
            "handled": True,
            "reply": "",
            "state": state,
        }

    active_subject = _subject_by_id(
        path,
        old_state.get("subject_id")
    )

    # Teacher-only state with no active subject:
    # do not guess.
    if not active_subject:
        return {
            "handled": False,
            "reply": "",
            "state": old_state,
        }

    related = _clear_followup(
        text
    )

    if not related:
        # Greetings/thanks/random messages don't clear the
        # active state, but also don't refresh it.
        return {
            "handled": False,
            "reply": "",
            "state": old_state,
        }

    state = _touch_state(
        path,
        chat_id,
        text
    )

    unavailable = not bool(
        active_subject.get("availability")
    )

    # --------------------------------------------------------
    # ACTIVE SUBJECT IS NOT AVAILABLE
    # --------------------------------------------------------
    if unavailable:
        kind = _unavailable_followup(
            text
        )

        if kind == "where":
            return {
                "handled": True,
                "reply": _no_idea_reply(text),
                "state": state,
            }

        if kind == "unavailable":
            return {
                "handled": True,
                "reply": _not_available_reply(text),
                "state": state,
            }

        return {
            "handled": True,
            "reply": "",
            "state": state,
        }

    # --------------------------------------------------------
    # ACTIVE SUBJECT IS AVAILABLE
    # Answer deterministic facts directly.
    # --------------------------------------------------------
    if _price_intent(text):
        price = str(
            active_subject.get("price")
            or ""
        ).strip()

        return {
            "handled": bool(price),
            "reply": (
                (
                    price
                    if price.startswith("₹")
                    else "₹" + price
                )
                if price
                else ""
            ),
            "state": state,
        }

    if _year_intent(text):
        year = str(
            active_subject.get("year")
            or ""
        ).strip()

        return {
            "handled": bool(year),
            "reply": year,
            "state": state,
        }

    if _availability_intent(text):
        return {
            "handled": True,
            "reply": _available_reply(text),
            "state": state,
        }

    return {
        "handled": False,
        "reply": "",
        "state": state,
    }
