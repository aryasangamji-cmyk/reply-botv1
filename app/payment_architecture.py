import asyncio
import json
import logging
import re
import sqlite3
import time

from app.new_batch_access import (
    _main_shortcut_lookup,
    _send_main_account_shortcut,
)

logger = logging.getLogger(__name__)

MARKER = "PAYMENT_ARCHITECTURE_V1"

# STATE_ISOLATION_FIX_V1
PAYMENT_STATE_TTL_SECONDS = 15 * 60


# ============================================================
# DATABASE
# ============================================================

def _con(path):
    db = sqlite3.connect(
        path,
        timeout=30,
    )

    db.row_factory = sqlite3.Row

    try:
        db.execute(
            "PRAGMA busy_timeout=30000"
        )
    except Exception:
        pass

    return db


def _table_exists(db, table):
    return (
        db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table'
              AND name=?
            LIMIT 1
            """,
            (table,),
        ).fetchone()
        is not None
    )


def _columns(db, table):
    if not _table_exists(
        db,
        table,
    ):
        return set()

    return {
        str(r["name"])
        for r in db.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


def ensure_payment_schema(path):
    with _con(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS
            payment_conversation_state
            (
                chat_id TEXT PRIMARY KEY,

                selected_json TEXT NOT NULL
                    DEFAULT '[]',

                final_amount INTEGER NOT NULL
                    DEFAULT 0,

                amount_confirmed INTEGER NOT NULL
                    DEFAULT 0,

                payment_method TEXT NOT NULL
                    DEFAULT '',

                stage TEXT NOT NULL
                    DEFAULT '',

                updated_at REAL NOT NULL
                    DEFAULT 0
            )
        """)

        db.commit()


def _state_get(
    path,
    chat_id,
):
    ensure_payment_schema(path)

    with _con(path) as db:
        row = db.execute("""
            SELECT *
            FROM payment_conversation_state
            WHERE chat_id=?
            LIMIT 1
        """, (
            str(chat_id),
        )).fetchone()

    if not row:
        return {
            "selected": [],
            "final_amount": 0,
            "amount_confirmed": False,
            "payment_method": "",
            "stage": "",
        }

    # STATE_ISOLATION_FIX_V1
    # Abandoned payment conversations must not survive indefinitely.
    try:
        updated_at = float(row["updated_at"] or 0)
    except Exception:
        updated_at = 0.0

    if (
        updated_at <= 0
        or (
            time.time() - updated_at
        ) > PAYMENT_STATE_TTL_SECONDS
    ):
        with _con(path) as db:
            db.execute(
                "DELETE FROM payment_conversation_state WHERE chat_id=?",
                (str(chat_id),),
            )
            db.commit()

        return {
            "selected": [],
            "final_amount": 0,
            "amount_confirmed": False,
            "payment_method": "",
            "stage": "",
        }

    try:
        selected = json.loads(
            row["selected_json"]
            or "[]"
        )
    except Exception:
        selected = []

    return {
        "selected": (
            selected
            if isinstance(
                selected,
                list,
            )
            else []
        ),
        "final_amount": int(
            row["final_amount"]
            or 0
        ),
        "amount_confirmed": bool(
            row["amount_confirmed"]
        ),
        "payment_method": str(
            row["payment_method"]
            or ""
        ),
        "stage": str(
            row["stage"]
            or ""
        ),
    }


def _state_save(
    path,
    chat_id,
    state,
):
    ensure_payment_schema(path)

    with _con(path) as db:
        db.execute("""
            INSERT INTO
            payment_conversation_state
            (
                chat_id,
                selected_json,
                final_amount,
                amount_confirmed,
                payment_method,
                stage,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                selected_json=
                    excluded.selected_json,

                final_amount=
                    excluded.final_amount,

                amount_confirmed=
                    excluded.amount_confirmed,

                payment_method=
                    excluded.payment_method,

                stage=
                    excluded.stage,

                updated_at=
                    excluded.updated_at
        """, (
            str(chat_id),

            json.dumps(
                state.get(
                    "selected",
                    [],
                ),
                ensure_ascii=False,
            ),

            int(
                state.get(
                    "final_amount",
                    0,
                )
                or 0
            ),

            1 if state.get(
                "amount_confirmed"
            ) else 0,

            str(
                state.get(
                    "payment_method",
                    "",
                )
            ),

            str(
                state.get(
                    "stage",
                    "",
                )
            ),

            time.time(),
        ))

        db.commit()


def _state_clear(
    path,
    chat_id,
):
    ensure_payment_schema(path)

    with _con(path) as db:
        db.execute(
            """
            DELETE FROM
            payment_conversation_state
            WHERE chat_id=?
            """,
            (
                str(chat_id),
            ),
        )

        db.commit()


# ============================================================
# TEXT HELPERS
# ============================================================

def _norm(value):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(
            value
            or ""
        ).casefold(),
    ).strip()


def _price(value):
    found = re.search(
        r"\d+(?:\.\d+)?",
        str(
            value
            or ""
        ).replace(",", ""),
    )

    if not found:
        return 0

    try:
        return int(
            float(
                found.group(0)
            )
        )
    except Exception:
        return 0


def _split_aliases(value):
    return [
        x.strip()
        for x in re.split(
            r"[,|;\n]+",
            str(
                value
                or ""
            ),
        )
        if x.strip()
    ]


def _contains(
    text,
    phrase,
):
    t = " " + _norm(text) + " "
    p = _norm(phrase)

    if len(p) < 3:
        return False

    return (
        " " + p + " "
    ) in t


def _yes(text):
    n = _norm(text)

    return bool(
        re.fullmatch(
            r"(?:"
            r"yes|yeah|yup|yep|"
            r"haan|ha|han|haa|"
            r"ji|correct|right|"
            r"ok|okay|done|"
            r"yes bro|haan bro|"
            r"ha bro|yes bhai"
            r")",
            n,
            re.I,
        )
    )


def _no(text):
    n = _norm(text)

    return bool(
        re.search(
            r"\b(?:"
            r"no|nope|nah|"
            r"nahi|nhi|"
            r"not possible|"
            r"cannot|cant|can't|"
            r"possible nahi|"
            r"nahi ho sakta|"
            r"nhi ho sakta"
            r")\b",
            n,
            re.I,
        )
    )


def _payment_intent(text):
    n = _norm(text)

    return bool(
        re.search(
            r"\b(?:"
            r"payment|"
            r"pay|"
            r"paying|"
            r"purchase|"
            r"buy|"
            r"phonepe|"
            r"phone pay|"
            r"gpay|"
            r"google pay|"
            r"paytm|"
            r"amazon pay|"
            r"gift card|"
            r"kaise pay|"
            r"payment kaise|"
            r"payment process|"
            r"payment method"
            r")\b",
            n,
            re.I,
        )
    )


def _payment_method(text):
    n = _norm(text)

    if re.search(
        r"\b(?:gpay|google pay)\b",
        n,
        re.I,
    ):
        return "gpay"

    if re.search(
        r"\bpaytm\b",
        n,
        re.I,
    ):
        return "paytm"

    if re.search(
        r"\bamazon pay\b",
        n,
        re.I,
    ):
        return "amazonpay"

    if re.search(
        r"\b(?:phonepe|phone pay)\b",
        n,
        re.I,
    ):
        return "phonepe"

    return ""


# ============================================================
# LOAD PURCHASABLE ITEMS
# ============================================================

def _all_items(path):
    out = []

    with _con(path) as db:

        # ----------------------------------------------------
        # STRUCTURED TEACHER SUBJECTS
        # ----------------------------------------------------

        if (
            _table_exists(
                db,
                "structured_batch_teachers",
            )
            and _table_exists(
                db,
                "structured_batch_subjects",
            )
        ):
            rows = db.execute("""
                SELECT
                    s.id AS subject_id,
                    s.name AS subject_name,
                    s.aliases AS subject_aliases,
                    s.price AS price,

                    t.id AS teacher_id,
                    t.name AS teacher_name,
                    t.aliases AS teacher_aliases

                FROM structured_batch_subjects s

                JOIN structured_batch_teachers t
                  ON t.id=s.teacher_id

                WHERE
                    s.enabled=1
                    AND t.enabled=1
            """).fetchall()

            counts = {}

            for row in rows:
                tid = int(
                    row["teacher_id"]
                )

                counts[tid] = (
                    counts.get(
                        tid,
                        0,
                    )
                    + 1
                )

            for row in rows:
                teacher = str(
                    row["teacher_name"]
                    or ""
                ).strip()

                subject = str(
                    row["subject_name"]
                    or ""
                ).strip()

                amount = _price(
                    row["price"]
                )

                phrases = [
                    teacher
                    + " "
                    + subject,
                ]

                phrases += _split_aliases(
                    row["subject_aliases"]
                )

                # Teacher name alone is safe when
                # teacher has exactly one subject.
                if (
                    counts.get(
                        int(
                            row["teacher_id"]
                        ),
                        0,
                    )
                    == 1
                ):
                    phrases.append(
                        teacher
                    )

                    phrases += (
                        _split_aliases(
                            row[
                                "teacher_aliases"
                            ]
                        )
                    )

                out.append({
                    "key":
                        "subject:"
                        + str(
                            row[
                                "subject_id"
                            ]
                        ),

                    "name":
                        teacher
                        + " — "
                        + subject,

                    "price": amount,

                    "phrases":
                        list(
                            dict.fromkeys(
                                x
                                for x in phrases
                                if x
                            )
                        ),
                })



        # ----------------------------------------------------
        # OPTIONAL FOLDER BATCHES
        # ----------------------------------------------------

        if _table_exists(
            db,
            "new_batch_folder_batches",
        ):
            for row in db.execute("""
                SELECT *
                FROM new_batch_folder_batches
                WHERE
                    enabled=1
                    AND availability=1
            """).fetchall():

                amount = _price(
                    row["price"]
                )

                phrases = [
                    str(
                        row["name"]
                        or ""
                    ).strip()
                ]

                phrases += _split_aliases(
                    row["aliases"]
                )

                out.append({
                    "key":
                        "folder_batch:"
                        + str(row["id"]),

                    "name":
                        str(
                            row["name"]
                            or ""
                        ).strip(),

                    "price": amount,

                    "phrases": phrases,
                })


        # ----------------------------------------------------
        # OPTIONAL FOLDER PARTS
        # ----------------------------------------------------

        if (
            _table_exists(
                db,
                "new_batch_folder_parts",
            )
            and _table_exists(
                db,
                "new_batch_folder_batches",
            )
        ):
            part_rows = db.execute("""
                SELECT
                    p.*,
                    b.name AS batch_name

                FROM new_batch_folder_parts p

                JOIN new_batch_folder_batches b
                  ON b.id=p.batch_id

                WHERE
                    p.enabled=1
                    AND p.availability=1
                    AND b.enabled=1
                    AND b.availability=1
            """).fetchall()

            for row in part_rows:

                amount = _price(
                    row["price"]
                )

                part_name = str(
                    row["name"]
                    or ""
                ).strip()

                batch_name = str(
                    row["batch_name"]
                    or ""
                ).strip()

                phrases = [
                    batch_name
                    + " "
                    + part_name,
                ]

                phrases += _split_aliases(
                    row["aliases"]
                )

                out.append({
                    "key":
                        "folder_part:"
                        + str(row["id"]),

                    "name":
                        batch_name
                        + " — "
                        + part_name,

                    "price": amount,

                    "phrases": phrases,
                })


        # ----------------------------------------------------
        # COMBOS
        # ----------------------------------------------------

        if _table_exists(
            db,
            "combos",
        ):
            cols = _columns(
                db,
                "combos",
            )

            if (
                "name" in cols
                and "price" in cols
            ):
                sql = """
                    SELECT *
                    FROM combos
                """

                if "enabled" in cols:
                    sql += " WHERE enabled=1"

                for row in db.execute(
                    sql
                ).fetchall():

                    amount = _price(
                        row["price"]
                    )

                    phrases = [
                        str(
                            row["name"]
                            or ""
                        ).strip()
                    ]

                    if (
                        "aliases"
                        in cols
                    ):
                        phrases += (
                            _split_aliases(
                                row["aliases"]
                            )
                        )

                    out.append({
                        "key":
                            "combo:"
                            + str(
                                row["id"]
                            ),

                        "name":
                            str(
                                row["name"]
                                or ""
                            ).strip(),

                        "price":
                            amount,

                        "phrases":
                            phrases,
                    })


        # ----------------------------------------------------
        # LEGACY BATCHES
        # ----------------------------------------------------

        if _table_exists(
            db,
            "batches",
        ):
            cols = _columns(
                db,
                "batches",
            )

            if (
                "name" in cols
                and (
                    "fee" in cols
                    or "price" in cols
                )
            ):
                sql = """
                    SELECT *
                    FROM batches
                """

                if "enabled" in cols:
                    sql += " WHERE enabled=1"

                for row in db.execute(
                    sql
                ).fetchall():

                    amount = _price(
                        row[
                            (
                                "fee"
                                if "fee"
                                in cols
                                else "price"
                            )
                        ]
                    )

                    phrases = [
                        str(
                            row["name"]
                            or ""
                        ).strip()
                    ]

                    if (
                        "search_keywords"
                        in cols
                    ):
                        phrases += (
                            _split_aliases(
                                row[
                                    "search_keywords"
                                ]
                            )
                        )

                    out.append({
                        "key":
                            "legacy_batch:"
                            + str(
                                row["id"]
                            ),

                        "name":
                            str(
                                row["name"]
                                or ""
                            ).strip(),

                        "price":
                            amount,

                        "phrases":
                            phrases,
                    })

    return out


# EXACT_PAYMENT_BATCH_RESOLVER_V3

def _payment_identity(value):
    """
    Normalize one purchasable batch name so harmless title
    variations do not create duplicate purchases.
    """

    value = _norm(value)

    value = re.sub(
        r"\b(?:sir|maam|mam|madam|ji)\b",
        " ",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


def _payment_segments(text):
    """
    Split a customer's explicit multiple-batch selection.

    Examples:
      A and B
      A aur B
      A + B
      A, B
      A & B
    """

    raw = str(
        text
        or ""
    ).strip()

    if not raw:
        return []

    parts = re.split(
        r"\s*(?:"
        r"\band\b|"
        r"\baur\b|"
        r"\+|"
        r"&|"
        r",|"
        r"\n"
        r")\s*",
        raw,
        flags=re.I,
    )

    return [
        x.strip()
        for x in parts
        if x.strip()
    ] or [raw]


def _score_payment_item(
    segment,
    item,
    source_order,
):
    """
    Score ONE database item against ONE customer-selected segment.

    Critical rule:
    an explicit teacher/batch name beats a generic subject alias.
    """

    seg = _norm(segment)

    if not seg:
        return None

    name = str(
        item.get("name")
        or ""
    ).strip()

    name_norm = _norm(name)

    phrases = []

    if name:
        phrases.append(name)

    phrases.extend(
        item.get(
            "phrases",
            []
        )
        or []
    )

    best = None

    for phrase in phrases:

        phrase_norm = _norm(
            phrase
        )

        if not phrase_norm:
            continue

        # Require the complete saved phrase to occur.
        if not _contains(
            segment,
            phrase,
        ):
            continue

        phrase_words = phrase_norm.split()

        score = len(
            phrase_norm
        ) * 10

        # Exact full course-name match is strongest.
        if seg == name_norm:
            score += 100000

        # Exact saved alias/name match.
        elif seg == phrase_norm:
            score += 50000

        # Longer multiword teacher+subject phrases strongly
        # outrank generic subject-only words.
        if len(phrase_words) >= 4:
            score += 8000
        elif len(phrase_words) == 3:
            score += 5000
        elif len(phrase_words) == 2:
            score += 1500
        else:
            # Generic one-word subject such as Economy,
            # Geography, Environment etc. is deliberately weak.
            score += 10

        candidate = (
            score,
            -source_order,
        )

        if (
            best is None
            or candidate > best
        ):
            best = candidate

    return best


def _best_payment_item(
    all_items,
    segment,
):
    """
    Resolve one customer segment to ONE best database item only.
    """

    candidates = []

    for order, item in enumerate(
        all_items
    ):
        score = _score_payment_item(
            segment,
            item,
            order,
        )

        if score is None:
            continue

        candidates.append(
            (
                score,
                order,
                item,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            x[0][0],
            x[0][1],
        ),
        reverse=True,
    )

    # Preserve earliest database/source record if two entries
    # have the exact same name/score.
    top_score = candidates[0][0][0]

    tied = [
        x
        for x in candidates
        if x[0][0] == top_score
    ]

    tied.sort(
        key=lambda x: x[1]
    )

    return dict(
        tied[0][2]
    )


def _items_from_text(
    path,
    text,
):
    """
    Payment selection resolver.

    One customer segment = maximum ONE selected purchase.

    Example:
      "Sudarshan Gurjar Sir Geography and Mrunal Patel Economy"

    resolves to exactly:
      Sudarshan Gurjar Geography
      Mrunal Patel Economy

    It must NOT append every Geography/Economy record.
    """

    all_items = _all_items(
        path
    )

    segments = _payment_segments(
        text
    )

    selected = []
    seen_keys = set()
    seen_identity = set()

    for segment in segments:

        item = _best_payment_item(
            all_items,
            segment,
        )

        if not item:
            continue

        key = str(
            item.get("key")
            or ""
        ).strip()

        identity = _payment_identity(
            item.get("name")
            or ""
        )

        if key and key in seen_keys:
            continue

        if (
            identity
            and identity in seen_identity
        ):
            continue

        if key:
            seen_keys.add(
                key
            )

        if identity:
            seen_identity.add(
                identity
            )

        selected.append(
            item
        )

    return selected


def _merge_selected(
    current,
    new_items,
):
    result = []
    seen = set()

    for item in (
        list(current or [])
        + list(new_items or [])
    ):
        key = str(
            item.get("key")
            or ""
        )

        if not key:
            continue

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            item
        )

    return result


def _total(selected):
    return sum(
        int(
            x.get(
                "price",
                0,
            )
            or 0
        )
        for x in selected
    )


# ============================================================
# CUSTOMER SEND
# ============================================================

async def _send_text(
    context,
    chat_id,
    text,
):
    client = (
        context.application
        .bot_data
        .get(
            "main_account_client"
        )
    )

    if client is not None:
        await client.send_message(
            int(chat_id),
            text,
            parse_mode="html",
            link_preview=False,
        )

        return

    await context.bot.send_message(
        chat_id=int(chat_id),
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _trigger(
    context,
    settings,
    chat_id,
    trigger,
):
    shortcut = (
        _main_shortcut_lookup(
            settings.database_path,
            trigger,
        )
    )

    if not shortcut:
        logger.error(
            "PAYMENT ARCHITECTURE: "
            "shortcut missing trigger=%r",
            trigger,
        )

        await _send_text(
            context,
            chat_id,
            "This payment option is not configured yet.",
        )

        return False

    client = (
        context.application
        .bot_data
        .get(
            "main_account_client"
        )
    )

    if client is None:
        logger.error(
            "PAYMENT ARCHITECTURE: "
            "main account client unavailable "
            "trigger=%r",
            trigger,
        )

        return False

    await _send_main_account_shortcut(
        settings.database_path,
        client,
        int(chat_id),
        shortcut,
    )

    logger.info(
        "PAYMENT ARCHITECTURE TRIGGER: "
        "chat=%s trigger=%s",
        chat_id,
        trigger,
    )

    return True


# ============================================================
# ORDER CONFIRMATION
# ============================================================

async def _ask_confirmation(
    context,
    chat_id,
    state,
    *,
    final_amount_override=None,
    negotiated=False,
):
    selected = state.get(
        "selected",
        [],
    )

    catalog_total = _total(
        selected
    )

    if final_amount_override is None:
        total = catalog_total
    else:
        try:
            total = int(final_amount_override)
        except Exception:
            total = catalog_total

    if total <= 0:
        total = catalog_total

    state[
        "final_amount"
    ] = total

    state[
        "amount_confirmed"
    ] = False

    state[
        "stage"
    ] = "CONFIRM_ORDER"

    lines = []

    if len(selected) == 1:
        item = selected[0]

        lines.append(
            "You selected:"
        )

        lines.append(
            "• "
            + str(
                item["name"]
            )
            + " — ₹"
            + str(
                total if negotiated else item["price"]
            )
        )

        lines.append("")
        lines.append(
            "You only need this batch?"
        )

    else:
        lines.append(
            "You selected these batches:"
        )

        for item in selected:
            if negotiated:
                lines.append(
                    "• " + str(item["name"])
                )
            else:
                lines.append(
                    "• "
                    + str(
                        item["name"]
                    )
                    + " — ₹"
                    + str(
                        item["price"]
                    )
                )

    lines.append("")
    lines.append(
        "<b>Total amount: ₹"
        + str(total)
        + "</b>"
    )

    lines.append("")
    lines.append(
        "Is this correct?"
    )

    await _send_text(
        context,
        chat_id,
        "\n".join(
            lines
        ),
    )


async def begin_payment_for_selected(
    context,
    path,
    chat_id,
    selected,
    *,
    final_amount=None,
    negotiated=False,
):
    """
    Start the existing Payment Architecture from an already-finalized cart.

    This is the handoff used by Negotiation Layer V1.  The selected cart is
    preserved, while final_amount may be the last negotiated price.
    """
    clean = []
    seen = set()

    for item in list(selected or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        name = str(item.get("name") or "").strip()
        try:
            price = int(item.get("price") or 0)
        except Exception:
            price = 0

        if not name or price <= 0:
            continue

        dedupe = key or name.casefold()
        if dedupe in seen:
            continue
        seen.add(dedupe)

        clean.append({
            "key": key or ("negotiation:" + name.casefold()),
            "name": name,
            "price": price,
            "phrases": list(item.get("phrases") or []),
        })

    if not clean:
        return False

    catalog_total = _total(clean)
    try:
        amount = int(final_amount) if final_amount is not None else catalog_total
    except Exception:
        amount = catalog_total

    if amount <= 0:
        amount = catalog_total

    state = {
        "selected": clean,
        "final_amount": amount,
        "amount_confirmed": False,
        "payment_method": "",
        "stage": "CONFIRM_ORDER",
    }

    await _ask_confirmation(
        context,
        int(chat_id),
        state,
        final_amount_override=amount,
        negotiated=bool(negotiated),
    )

    _state_save(
        path,
        int(chat_id),
        state,
    )

    logger.info(
        "PAYMENT ARCHITECTURE NEGOTIATION HANDOFF: chat=%s catalog_total=%s final_amount=%s negotiated=%s items=%s",
        chat_id,
        catalog_total,
        amount,
        bool(negotiated),
        [x.get("name") for x in clean],
    )

    return True


# ============================================================
# FINAL PAYMENT ROUTING
# ============================================================

async def _route_method(
    context,
    settings,
    chat_id,
    state,
    method,
):
    amount = int(
        state.get(
            "final_amount",
            0,
        )
        or 0
    )

    # Safety rule:
    # no payment-specific shortcut can run
    # before amount confirmation.
    if not state.get(
        "amount_confirmed"
    ):
        await _send_text(
            context,
            chat_id,
            "Please confirm the selected batch and total amount first.",
        )

        return True


    # --------------------------------------------------------
    # PHONEPE
    # --------------------------------------------------------

    if method == "phonepe":

        if amount > 999:
            trigger = "/phonepe"

        elif amount >= 500:
            trigger = "/phonepeFK"

        else:
            trigger = "/phonepeAP"

        ok = await _trigger(
            context,
            settings,
            chat_id,
            trigger,
        )

        if ok:
            _state_clear(
                settings.database_path,
                chat_id,
            )

        return True


    # --------------------------------------------------------
    # PAYTM
    # --------------------------------------------------------

    if method == "paytm":

        ok = await _trigger(
            context,
            settings,
            chat_id,
            "/paytm",
        )

        if ok:
            _state_clear(
                settings.database_path,
                chat_id,
            )

        return True


    # --------------------------------------------------------
    # AMAZON PAY
    # --------------------------------------------------------

    if method == "amazonpay":

        ok = await _trigger(
            context,
            settings,
            chat_id,
            "/amazonpay",
        )

        if ok:
            _state_clear(
                settings.database_path,
                chat_id,
            )

        return True


    # --------------------------------------------------------
    # GPAY
    #
    # ₹300 -> GPay300
    #
    # ₹800 -> GPay300 then GPay500
    #
    # EVERY OTHER AMOUNT -> GPay500
    # --------------------------------------------------------

    if method == "gpay":

        if amount == 300:

            ok = await _trigger(
                context,
                settings,
                chat_id,
                "/gpay300",
            )

            if ok:
                _state_clear(
                    settings.database_path,
                    chat_id,
                )

            return True


        if amount == 800:

            first = await _trigger(
                context,
                settings,
                chat_id,
                "/gpay300",
            )

            if first:
                await asyncio.sleep(
                    0.5
                )

                second = await _trigger(
                    context,
                    settings,
                    chat_id,
                    "/gpay500",
                )

                if second:
                    _state_clear(
                        settings.database_path,
                        chat_id,
                    )

            return True


        ok = await _trigger(
            context,
            settings,
            chat_id,
            "/gpay500",
        )

        if ok:
            _state_clear(
                settings.database_path,
                chat_id,
            )

        return True

    return False


# ============================================================
# MAIN PAYMENT STATE MACHINE
# ============================================================

async def process_customer_payment(
    update,
    context,
    settings,
    text,
):
    if not getattr(
        update,
        "effective_chat",
        None,
    ):
        return False

    chat_id = int(
        update.effective_chat.id
    )

    raw = str(
        text
        or ""
    ).strip()

    if not raw:
        return False

    state = _state_get(
        settings.database_path,
        chat_id,
    )

    active = bool(
        state.get(
            "stage"
        )
    )

    intent = _payment_intent(
        raw
    )

    # Ignore unrelated conversations when
    # there is no payment state.
    if (
        not active
        and not intent
    ):
        return False


    # --------------------------------------------------------
    # Customer mentions batch/batches at any active
    # payment stage.
    #
    # This automatically recalculates the amount
    # and REQUIRES confirmation again.
    # --------------------------------------------------------

    found = _items_from_text(
        settings.database_path,
        raw,
    )

    stage_now = str(
        state.get("stage")
        or ""
    )

    if (
        found
        and active
        and stage_now != "NEED_SELECTION"
    ):
        _state_clear(
            settings.database_path,
            chat_id,
        )
        return False

    if (
        found
        and (
            not active
            or stage_now == "NEED_SELECTION"
        )
    ):
        state[
            "selected"
        ] = _merge_selected(
            state.get(
                "selected",
                [],
            ),
            found,
        )

        missing = [
            x
            for x in state[
                "selected"
            ]
            if int(
                x.get(
                    "price",
                    0,
                )
                or 0
            )
            <= 0
        ]

        if missing:
            state[
                "amount_confirmed"
            ] = False

            state[
                "stage"
            ] = "NEED_SELECTION"

            _state_save(
                settings.database_path,
                chat_id,
                state,
            )

            await _send_text(
                context,
                chat_id,
                "Price is not configured for: "
                + ", ".join(
                    x["name"]
                    for x in missing
                )
                + ".",
            )

            return True

        await _ask_confirmation(
            context,
            chat_id,
            state,
        )

        _state_save(
            settings.database_path,
            chat_id,
            state,
        )

        return True


    # --------------------------------------------------------
    # NEW PAYMENT REQUEST
    # --------------------------------------------------------

    if (
        not active
        and intent
    ):
        state = {
            "selected": [],
            "final_amount": 0,
            "amount_confirmed": False,
            "payment_method": "",
            "stage": "NEED_SELECTION",
        }

        _state_save(
            settings.database_path,
            chat_id,
            state,
        )

        await _send_text(
            context,
            chat_id,
            "Which batch or batches do you want to purchase? "
            "Please send the batch name.",
        )

        return True


    stage = state.get(
        "stage"
    )


    # --------------------------------------------------------
    # WAITING FOR BATCH SELECTION
    # --------------------------------------------------------

    if stage == "NEED_SELECTION":
        # STATE_ISOLATION_FIX_V1
        # A valid saved batch was already handled above. Anything else
        # belongs to normal routing instead of being swallowed by payment.
        return False


    # --------------------------------------------------------
    # CONFIRM SELECTED BATCHES + AMOUNT
    # --------------------------------------------------------

    if stage == "CONFIRM_ORDER":

        if _yes(
            raw
        ):
            state[
                "amount_confirmed"
            ] = True

            amount = int(
                state.get(
                    "final_amount",
                    0,
                )
                or 0
            )

            # ================================================
            # ABOVE ₹999
            # ================================================

            if amount > 999:

                state[
                    "stage"
                ] = "HIGH_PHONEPE"

                _state_save(
                    settings.database_path,
                    chat_id,
                    state,
                )

                await _send_text(
                    context,
                    chat_id,
                    "Do you use PhonePe?",
                )

                return True


            # ================================================
            # ₹999 OR BELOW
            # /pay is allowed now because amount is confirmed.
            # ================================================

            state[
                "stage"
            ] = "PAYMENT_METHOD"

            _state_save(
                settings.database_path,
                chat_id,
                state,
            )

            await _trigger(
                context,
                settings,
                chat_id,
                "/pay",
            )

            return True


        if _no(
            raw
        ):
            state = {
                "selected": [],
                "final_amount": 0,
                "amount_confirmed": False,
                "payment_method": "",
                "stage": "NEED_SELECTION",
            }

            _state_save(
                settings.database_path,
                chat_id,
                state,
            )

            await _send_text(
                context,
                chat_id,
                "Okay. Please send the correct batch or batches you want.",
            )

            return True


        # STATE_ISOLATION_FIX_V1
        # Only Yes/No belongs to CONFIRM_ORDER.
        return False


    # --------------------------------------------------------
    # ABOVE ₹999
    # "Do you use PhonePe?"
    # --------------------------------------------------------

    if stage == "HIGH_PHONEPE":

        if _yes(
            raw
        ):
            return await _route_method(
                context,
                settings,
                chat_id,
                state,
                "phonepe",
            )


        if _no(
            raw
        ):
            state[
                "stage"
            ] = "HIGH_PHONEPE_ALTERNATIVE"

            _state_save(
                settings.database_path,
                chat_id,
                state,
            )

            await _send_text(
                context,
                chat_id,
                "Can you download PhonePe or use your friend's PhonePe?",
            )

            return True


        # STATE_ISOLATION_FIX_V1
        # Only Yes/No belongs to HIGH_PHONEPE.
        return False


    # --------------------------------------------------------
    # ABOVE ₹999
    # friend/download PhonePe fallback
    # --------------------------------------------------------

    if stage == "HIGH_PHONEPE_ALTERNATIVE":

        if _yes(
            raw
        ):
            return await _route_method(
                context,
                settings,
                chat_id,
                state,
                "phonepe",
            )


        if _no(
            raw
        ):
            state[
                "stage"
            ] = "PAYMENT_METHOD"

            _state_save(
                settings.database_path,
                chat_id,
                state,
            )

            # /pay is the allowed fallback.
            await _trigger(
                context,
                settings,
                chat_id,
                "/pay",
            )

            return True


        # STATE_ISOLATION_FIX_V1
        # Only Yes/No belongs to HIGH_PHONEPE_ALTERNATIVE.
        return False


    # --------------------------------------------------------
    # PAYMENT METHOD SELECTION AFTER /pay
    # --------------------------------------------------------

    if stage == "PAYMENT_METHOD":

        method = _payment_method(
            raw
        )

        if not method:

            await _send_text(
                context,
                chat_id,
                "Please choose PhonePe, GPay, Paytm, or Amazon Pay.",
            )

            return True

        state[
            "payment_method"
        ] = method

        _state_save(
            settings.database_path,
            chat_id,
            state,
        )

        return await _route_method(
            context,
            settings,
            chat_id,
            state,
            method,
        )


    return False
