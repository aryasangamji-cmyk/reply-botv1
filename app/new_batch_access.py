import asyncio
import html
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from telegram.ext import ChatMemberHandler

logger = logging.getLogger(__name__)

LINK_TTL_SECONDS = 24 * 60 * 60
RECOVERY_SECONDS = 60
FUZZY_MIN_SCORE = 74.0
FUZZY_MARGIN = 5.0


# ============================================================
# DATABASE
# ============================================================

ACCESS_COLUMNS = {
    "demo_link": "TEXT NOT NULL DEFAULT ''",
    "private_message_link": "TEXT NOT NULL DEFAULT ''",
    "private_chat_id": "INTEGER",
}


def _con(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _ensure_columns(db, table, columns):
    existing = {
        str(r["name"])
        for r in db.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }

    for name, definition in columns.items():
        if name not in existing:
            db.execute(
                f"ALTER TABLE {table} "
                f"ADD COLUMN {name} {definition}"
            )


def ensure_access_schema(path):
    with _con(path) as db:
        _ensure_columns(
            db,
            "structured_batch_teachers",
            ACCESS_COLUMNS,
        )

        _ensure_columns(
            db,
            "structured_batch_subjects",
            ACCESS_COLUMNS,
        )

        # FOLDER_ACCESS_TARGETS_V1
        _ensure_columns(
            db,
            "new_batch_folder_batches",
            ACCESS_COLUMNS,
        )

        _ensure_columns(
            db,
            "new_batch_folder_parts",
            ACCESS_COLUMNS,
        )

        # SUBJECT_FOLDER_COMBINED_DEMO_V1
        _ensure_columns(
            db,
            "new_batch_folders",
            {
                "combined_demo_link":
                    "TEXT NOT NULL DEFAULT ''"
            },
        )

        db.executescript("""
        CREATE TABLE IF NOT EXISTS new_batch_access_messages (
            mode TEXT PRIMARY KEY,
            message_text TEXT NOT NULL DEFAULT '',
            entities_json TEXT NOT NULL DEFAULT '[]',
            link_text TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS new_batch_access_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            scope TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            target_name TEXT NOT NULL DEFAULT '',

            mode TEXT NOT NULL,

            customer_chat_id INTEGER,
            requested_by INTEGER,

            target_chat_id INTEGER NOT NULL,
            invite_link TEXT NOT NULL UNIQUE,

            created_at REAL NOT NULL,
            telegram_expires_at REAL NOT NULL,

            joined_user_id INTEGER,
            joined_name TEXT NOT NULL DEFAULT '',
            joined_username TEXT NOT NULL DEFAULT '',
            joined_at REAL,

            revoke_status TEXT NOT NULL DEFAULT 'pending',
            revoked_at REAL,

            pre_minutes INTEGER,
            remove_at REAL,

            removal_status TEXT NOT NULL DEFAULT '',
            removed_at REAL,

            unban_status TEXT NOT NULL DEFAULT '',
            unbanned_at REAL,

            status TEXT NOT NULL DEFAULT 'active',
            last_error TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_new_batch_access_invite
        ON new_batch_access_sessions(invite_link);

        CREATE INDEX IF NOT EXISTS idx_new_batch_access_status
        ON new_batch_access_sessions(status);

        CREATE INDEX IF NOT EXISTS idx_new_batch_access_remove
        ON new_batch_access_sessions(remove_at);
        """)

        defaults = {
            "normal": (
                "Your course access is ready. Join here",
                "here",
            ),
            "demo": (
                "Check the demo here",
                "here",
            ),
            "pre": (
                "Your PRE access is ready. Join here",
                "here",
            ),
        }

        for mode, (message, link_text) in defaults.items():
            db.execute("""
                INSERT OR IGNORE INTO new_batch_access_messages
                (
                    mode,
                    message_text,
                    entities_json,
                    link_text,
                    updated_at
                )
                VALUES(?,?,?,?,?)
            """, (
                mode,
                message,
                "[]",
                link_text,
                time.time(),
            ))

        db.commit()


# ============================================================
# GLOBAL MESSAGE CONFIGURATION
# ============================================================

def serialize_entities(entities):
    result = []

    for e in entities or []:
        typ = getattr(e, "type", "")

        if hasattr(typ, "value"):
            typ = typ.value

        item = {
            "type": str(typ),
            "offset": int(
                getattr(e, "offset", 0) or 0
            ),
            "length": int(
                getattr(e, "length", 0) or 0
            ),
        }

        url = getattr(e, "url", None)

        if url:
            item["url"] = str(url)

        user = getattr(e, "user", None)

        if user is not None:
            try:
                item["user_id"] = int(user.id)
            except Exception:
                pass

        language = getattr(e, "language", None)

        if language:
            item["language"] = str(language)

        result.append(item)

    return result


def get_global_message(path, mode):
    ensure_access_schema(path)

    with _con(path) as db:
        row = db.execute("""
            SELECT *
            FROM new_batch_access_messages
            WHERE mode=?
            LIMIT 1
        """, (str(mode),)).fetchone()

    return dict(row) if row else None


def save_global_message(
    path,
    mode,
    message_text,
    entities,
):
    ensure_access_schema(path)

    with _con(path) as db:
        db.execute("""
            UPDATE new_batch_access_messages
            SET
                message_text=?,
                entities_json=?,
                updated_at=?
            WHERE mode=?
        """, (
            str(message_text or ""),
            json.dumps(
                serialize_entities(entities),
                ensure_ascii=False,
            ),
            time.time(),
            str(mode),
        ))

        db.commit()


def save_global_link_text(
    path,
    mode,
    link_text,
):
    row = get_global_message(
        path,
        mode,
    )

    if not row:
        raise ValueError(
            "Message configuration not found."
        )

    text = str(
        row.get("message_text")
        or ""
    )

    link_text = str(
        link_text
        or ""
    ).strip()

    if not link_text:
        raise ValueError(
            "Link word/text cannot be empty."
        )

    if link_text not in text:
        raise ValueError(
            "That exact word or phrase is not present "
            "inside the saved message."
        )

    with _con(path) as db:
        db.execute("""
            UPDATE new_batch_access_messages
            SET
                link_text=?,
                updated_at=?
            WHERE mode=?
        """, (
            link_text,
            time.time(),
            str(mode),
        ))

        db.commit()


# ============================================================
# TELEGRAM ENTITY -> HTML
# ============================================================

def _utf16_len(text):
    return len(
        str(text).encode("utf-16-le")
    ) // 2


def _utf16_to_py_index(text, target):
    if target <= 0:
        return 0

    used = 0

    for i, ch in enumerate(text):
        units = _utf16_len(ch)

        if used + units > target:
            return i

        used += units

        if used == target:
            return i + 1

    return len(text)


def _entity_tags(entity):
    typ = str(
        entity.get("type")
        or ""
    ).casefold()

    if typ == "bold":
        return "<b>", "</b>"

    if typ == "italic":
        return "<i>", "</i>"

    if typ == "underline":
        return "<u>", "</u>"

    if typ in {
        "strikethrough",
        "strike",
    }:
        return "<s>", "</s>"

    if typ == "spoiler":
        return "<tg-spoiler>", "</tg-spoiler>"

    if typ == "code":
        return "<code>", "</code>"

    if typ == "pre":
        language = str(
            entity.get("language")
            or ""
        ).strip()

        if language:
            return (
                '<pre><code class="language-'
                + html.escape(
                    language,
                    quote=True,
                )
                + '">',
                "</code></pre>",
            )

        return "<pre>", "</pre>"

    if typ == "blockquote":
        return "<blockquote>", "</blockquote>"

    if typ == "expandable_blockquote":
        return (
            "<blockquote expandable>",
            "</blockquote>",
        )

    if typ == "text_link":
        url = str(
            entity.get("url")
            or ""
        )

        if url:
            return (
                '<a href="'
                + html.escape(
                    url,
                    quote=True,
                )
                + '">',
                "</a>",
            )

    if typ == "text_mention":
        user_id = entity.get("user_id")

        if user_id:
            return (
                f'<a href="tg://user?id={int(user_id)}">',
                "</a>",
            )

    return None


# DYNAMIC_ACCESS_TEMPLATE_V1
def _replace_template_token(
    text,
    entities,
    token,
    replacement,
):
    """
    Replace one template token while keeping Telegram entity
    offsets/lengths aligned in UTF-16 units.
    """
    while token in text:
        py_start = text.find(token)

        prefix = text[:py_start]

        start_u = _utf16_len(prefix)
        old_len_u = _utf16_len(token)
        new_len_u = _utf16_len(replacement)

        end_u = start_u + old_len_u
        delta = new_len_u - old_len_u

        for entity in entities:
            try:
                e_start = int(
                    entity.get("offset", 0)
                )
                e_len = int(
                    entity.get("length", 0)
                )
                e_end = e_start + e_len

                # Entity completely after placeholder.
                if e_start >= end_u:
                    entity["offset"] = (
                        e_start + delta
                    )

                # Entity completely contains placeholder.
                elif (
                    e_start <= start_u
                    and e_end >= end_u
                ):
                    entity["length"] = (
                        e_len + delta
                    )

                # Entity is completely inside placeholder.
                elif (
                    e_start >= start_u
                    and e_end <= end_u
                ):
                    entity["offset"] = start_u
                    entity["length"] = new_len_u

                # Partial overlap: expand it safely across
                # the replacement.
                elif (
                    e_start < end_u
                    and e_end > start_u
                ):
                    new_start = min(
                        e_start,
                        start_u
                    )

                    new_end = max(
                        e_end + delta,
                        start_u + new_len_u
                    )

                    entity["offset"] = new_start
                    entity["length"] = max(
                        0,
                        new_end - new_start
                    )

            except Exception:
                continue

        text = (
            text[:py_start]
            + replacement
            + text[
                py_start + len(token):
            ]
        )

    return text, entities


def render_global_message_html(
    path,
    mode,
    url,
    batch_name="",
    pre_minutes=None,
):
    row = get_global_message(
        path,
        mode,
    )

    if not row:
        raise ValueError(
            "Global message is not configured."
        )

    text = str(
        row.get("message_text")
        or ""
    )

    try:
        entities = json.loads(
            row.get("entities_json")
            or "[]"
        )
    except Exception:
        entities = []

    entities = [
        dict(x)
        for x in entities
        if isinstance(x, dict)
    ]

    if not text:
        raise ValueError(
            f"{mode.title()} message is empty."
        )

    # --------------------------------------------------------
    # BATCH NAME
    # --------------------------------------------------------

    text, entities = _replace_template_token(
        text,
        entities,
        "{BATCH_NAME}",
        str(batch_name or ""),
    )

    # Also accept the spaced form requested by admin.
    text, entities = _replace_template_token(
        text,
        entities,
        "{BATCH NAME}",
        str(batch_name or ""),
    )

    # --------------------------------------------------------
    # PRE DURATION
    # --------------------------------------------------------

    if pre_minutes is None:
        pre_minutes = get_pre_duration(
            path
        )

    text, entities = _replace_template_token(
        text,
        entities,
        "{PRE_MINUTES}",
        str(int(pre_minutes)),
    )

    # --------------------------------------------------------
    # ACCESS LINK PLACEHOLDER
    #
    # Example:
    # {ACCESS_LINK:Click Here}
    #
    # Customer sees only:
    # Click Here
    # --------------------------------------------------------

    access_match = re.search(
        r"\{ACCESS_LINK:([^{}]+)\}",
        text,
        re.IGNORECASE,
    )

    dynamic_link_used = False

    if access_match:
        token = access_match.group(0)
        display_text = (
            access_match.group(1).strip()
        )

        if not display_text:
            raise ValueError(
                "ACCESS_LINK display text is empty."
            )

        token_py_start = text.find(token)

        # Replace token first and adjust formatting entities.
        text, entities = _replace_template_token(
            text,
            entities,
            token,
            display_text,
        )

        # Find UTF-16 position of the replacement.
        link_start_u = _utf16_len(
            text[:token_py_start]
        )

        entities.append({
            "type": "text_link",
            "offset": link_start_u,
            "length": _utf16_len(
                display_text
            ),
            "url": str(url),
        })

        dynamic_link_used = True

    # --------------------------------------------------------
    # OLD LINK-WORD SYSTEM FALLBACK
    # --------------------------------------------------------

    if not dynamic_link_used:
        link_text = str(
            row.get("link_text")
            or ""
        ).strip()

        if not link_text:
            raise ValueError(
                f"{mode.title()} link word/text "
                "is not configured."
            )

        idx = text.find(
            link_text
        )

        if idx < 0:
            raise ValueError(
                f'Configured link text "{link_text}" '
                "is no longer present in the message."
            )

        entities.append({
            "type": "text_link",
            "offset": _utf16_len(
                text[:idx]
            ),
            "length": _utf16_len(
                link_text
            ),
            "url": str(url),
        })

    # --------------------------------------------------------
    # RENDER TELEGRAM ENTITIES TO HTML
    # --------------------------------------------------------

    spans = []

    for entity in entities:
        try:
            start_u = int(
                entity.get(
                    "offset",
                    0
                )
            )

            length_u = int(
                entity.get(
                    "length",
                    0
                )
            )

            if length_u <= 0:
                continue

            start_py = _utf16_to_py_index(
                text,
                start_u,
            )

            end_py = _utf16_to_py_index(
                text,
                start_u + length_u,
            )

            tags = _entity_tags(
                entity
            )

            if not tags:
                continue

            spans.append({
                "start": start_py,
                "end": end_py,
                "open": tags[0],
                "close": tags[1],
            })

        except Exception:
            continue

    opens = {}
    closes = {}

    for span in spans:
        opens.setdefault(
            span["start"],
            []
        ).append(span)

        closes.setdefault(
            span["end"],
            []
        ).append(span)

    for key in opens:
        opens[key].sort(
            key=lambda x: (
                -(x["end"] - x["start"]),
                x["end"],
            )
        )

    for key in closes:
        closes[key].sort(
            key=lambda x: (
                -x["start"],
                x["end"] - x["start"],
            )
        )

    out = []

    for i in range(
        len(text) + 1
    ):
        if i in closes:
            for span in closes[i]:
                out.append(
                    span["close"]
                )

        if i in opens:
            for span in opens[i]:
                out.append(
                    span["open"]
                )

        if i < len(text):
            out.append(
                html.escape(text[i])
            )

    return "".join(out)


# ============================================================
# PRIVATE GROUP REGISTRATION
# ============================================================

def parse_private_message_link(link):
    raw = str(
        link
        or ""
    ).strip()

    match = re.search(
        r"https?://(?:www\.)?t\.me/c/(\d+)/(\d+)",
        raw,
        re.I,
    )

    if not match:
        raise ValueError(
            "Send a private Telegram message link like "
            "https://t.me/c/1234567890/55"
        )

    internal = match.group(1)

    return {
        "message_link": raw,
        "message_id": int(
            match.group(2)
        ),
        "chat_id": int(
            "-100" + internal
        ),
    }


async def validate_private_target(
    bot,
    link,
):
    parsed = parse_private_message_link(
        link
    )

    chat_id = parsed[
        "chat_id"
    ]

    me = await bot.get_me()

    # WORKER_AUTO_ADMIN_V1
    # Before Bot API validation, automatically find a saved worker
    # that can access this target, add the AI bot, and promote it.
    from app.worker_admin_promoter import auto_prepare_bot_admin

    try:
        parsed["worker_setup"] = await auto_prepare_bot_admin(
            getattr(me, "username", None),
            chat_id,
        )

        # Give Telegram a moment to propagate the new admin rights.
        await asyncio.sleep(1.5)

    except Exception as worker_exc:
        logger.warning(
            "WORKER AUTO ADMIN failed target=%s error=%s",
            chat_id,
            worker_exc,
        )

        # Continue: if the bot was already correctly configured,
        # the normal validation below will still succeed.

    try:
        chat = await bot.get_chat(
            chat_id
        )
    except Exception as exc:
        raise ValueError(
            "AI bot cannot access this private group/channel. "
            "Add it as admin first."
        ) from exc

    try:
        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=me.id,
        )
    except Exception as exc:
        raise ValueError(
            "Could not verify bot permissions."
        ) from exc

    status = str(
        getattr(
            member,
            "status",
            "",
        )
    ).casefold()

    creator = "creator" in status

    admin = (
        creator
        or "administrator" in status
    )

    can_invite = (
        creator
        or bool(
            getattr(
                member,
                "can_invite_users",
                False,
            )
        )
    )

    can_restrict = (
        creator
        or bool(
            getattr(
                member,
                "can_restrict_members",
                False,
            )
        )
    )

    if not admin:
        raise ValueError(
            "AI bot is not admin in this group/channel."
        )

    if not can_invite:
        raise ValueError(
            "AI bot needs Invite Users permission."
        )

    if not can_restrict:
        raise ValueError(
            "AI bot needs Ban/Restrict Users permission "
            "for PRE removal."
        )

    parsed["title"] = (
        getattr(
            chat,
            "title",
            None,
        )
        or str(chat_id)
    )

    return parsed


# ============================================================
# PRE DURATION — EXISTING SETTING
# ============================================================

def get_pre_duration(path):
    try:
        with _con(path) as db:
            row = db.execute("""
                SELECT value
                FROM pre_access_settings
                WHERE key='duration_minutes'
                LIMIT 1
            """).fetchone()

        if row:
            duration = int(
                row[0]
            )

            if duration > 0:
                return duration

    except Exception:
        pass

    return 5


# ============================================================
# LOCAL MATCHING
# ============================================================

def _norm(value):
    value = str(
        value
        or ""
    ).casefold()

    value = value.replace(
        "_",
        " ",
    )

    value = re.sub(
        r"[^\w\s]+",
        " ",
        value,
        flags=re.UNICODE,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def _aliases(value):
    return [
        _norm(x)
        for x in re.split(
            r"[,|;\n]+",
            str(value or ""),
        )
        if _norm(x)
    ]


def _score(query, values):
    q = _norm(query)

    if not q:
        return 0.0

    qt = set(
        q.split()
    )

    best = 0.0

    for candidate in values:
        c = _norm(candidate)

        if not c:
            continue

        if q == c:
            best = max(
                best,
                100.0,
            )
            continue

        ct = set(
            c.split()
        )

        if (
            qt
            and qt.issubset(ct)
        ):
            best = max(
                best,
                94.0,
            )

        if (
            ct
            and ct.issubset(qt)
        ):
            best = max(
                best,
                91.0,
            )

        overlap = len(
            qt & ct
        )

        union = len(
            qt | ct
        ) or 1

        token_score = (
            overlap / union
        ) * 100.0

        seq_score = SequenceMatcher(
            None,
            q.replace(" ", ""),
            c.replace(" ", ""),
        ).ratio() * 100.0

        best = max(
            best,
            token_score * 0.62
            + seq_score * 0.38,
        )

    return best


def _teacher_values(t):
    result = [
        t.get("name"),
        t.get("slug"),
    ]

    result += _aliases(
        t.get("aliases")
    )

    result += _aliases(
        t.get("keywords")
    )

    return [
        x
        for x in result
        if x
    ]


def _subject_values(t, s):
    values = [
        s.get("name"),
        s.get("batch_code"),
        s.get("part_name"),
        s.get("category"),
        s.get("gs_paper"),
    ]

    values += _aliases(
        s.get("aliases")
    )

    teacher_name = str(
        t.get("name")
        or ""
    )

    combined = []

    for value in values:
        if value:
            combined.append(
                teacher_name
                + " "
                + str(value)
            )

    return [
        x
        for x in (
            values + combined
        )
        if x
    ]


# FOLDER_ACCESS_RESOLVER_V1
def _folder_batch_values(b):
    values = [
        b.get("name"),
        b.get("slug"),
        b.get("teacher"),
        b.get("institute"),
    ]

    values += _aliases(
        b.get("aliases")
    )

    return [
        x for x in values if x
    ]


def _folder_part_values(b, p):
    values = [
        p.get("name"),
        p.get("teacher"),
    ]

    values += _aliases(
        p.get("aliases")
    )

    parent_name = str(
        b.get("name")
        or ""
    ).strip()

    combined = []

    if parent_name:
        for value in values:
            if value:
                combined.append(
                    parent_name
                    + " "
                    + str(value)
                )

    return [
        x
        for x in (
            values + combined
        )
        if x
    ]


def resolve_new_batch(
    path,
    query,
):
    ensure_access_schema(path)

    with _con(path) as db:
        teachers = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM structured_batch_teachers
                WHERE enabled=1
            """).fetchall()
        ]

        subjects = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM structured_batch_subjects
                WHERE enabled=1
            """).fetchall()
        ]

        folder_batches = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_folder_batches
                WHERE enabled=1
            """).fetchall()
        ]

        folder_parts = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_folder_parts
                WHERE enabled=1
            """).fetchall()
        ]

    teacher_map = {
        int(t["id"]): t
        for t in teachers
    }

    folder_batch_map = {
        int(b["id"]): b
        for b in folder_batches
    }

    q = _norm(query)

    scored = []


    def add_match(
        scope,
        item_id,
        name,
        values,
        **extra,
    ):
        score = _score(
            query,
            values,
        )

        if not score:
            return

        exact = any(
            q == _norm(v)
            for v in values
            if v
        )

        specificity = {
            "teacher": 1,
            "folder_batch": 2,
            "subject": 3,
            "folder_part": 4,
        }.get(scope, 0)

        scored.append({
            "scope": scope,
            "id": int(item_id),
            "name": name,
            "score": score,
            "exact": exact,
            "specificity": specificity,
            **extra,
        })


    for t in teachers:
        add_match(
            "teacher",
            t["id"],
            t["name"],
            _teacher_values(t),
            teacher=t,
            subject=None,
        )


    for subject in subjects:
        teacher_row = teacher_map.get(
            int(subject["teacher_id"])
        )

        if not teacher_row:
            continue

        name = (
            str(teacher_row["name"])
            + " — "
            + str(subject["name"])
        )

        if subject.get("part_name"):
            name += (
                " — "
                + str(subject["part_name"])
            )

        if subject.get("batch_code"):
            name += (
                " ("
                + str(subject["batch_code"])
                + ")"
            )

        add_match(
            "subject",
            subject["id"],
            name,
            _subject_values(
                teacher_row,
                subject,
            ),
            teacher=teacher_row,
            subject=subject,
        )


    for b in folder_batches:
        add_match(
            "folder_batch",
            b["id"],
            b["name"],
            _folder_batch_values(b),
            folder_batch=b,
            folder_part=None,
        )


    for part_row in folder_parts:
        parent = folder_batch_map.get(
            int(part_row["batch_id"])
        )

        if not parent:
            continue

        name = (
            str(parent["name"])
            + " — "
            + str(part_row["name"])
        )

        add_match(
            "folder_part",
            part_row["id"],
            name,
            _folder_part_values(
                parent,
                part_row,
            ),
            folder_batch=parent,
            folder_part=part_row,
        )


    scored.sort(
        key=lambda x: (
            bool(x.get("exact")),
            float(x.get("score") or 0),
            int(x.get("specificity") or 0),
        ),
        reverse=True,
    )

    if not scored:
        return {
            "status": "none",
            "match": None,
            "alternatives": [],
        }

    best = scored[0]

    if float(best["score"]) < FUZZY_MIN_SCORE:
        return {
            "status": "none",
            "match": None,
            "alternatives": scored[:4],
        }

    if (
        len(scored) > 1
        and not best.get("exact")
        and not scored[1].get("exact")
        and (
            float(best["score"])
            - float(scored[1]["score"])
        ) < FUZZY_MARGIN
    ):
        return {
            "status": "ambiguous",
            "match": None,
            "alternatives": scored[:4],
        }

    return {
        "status": "ok",
        "match": best,
        "alternatives": scored[:4],
    }


# ============================================================
# SUBJECT OVERRIDE -> TEACHER FALLBACK
# ============================================================

# FOLDER_ACCESS_EFFECTIVE_TARGET_V1
def effective_target(match):
    scope = match.get(
        "scope"
    )

    if scope in (
        "folder_batch",
        "folder_part",
    ):
        parent = match.get(
            "folder_batch"
        ) or {}

        child = match.get(
            "folder_part"
        )

        def value(field):
            if child:
                child_value = child.get(
                    field
                )

                if child_value not in (
                    None,
                    "",
                ):
                    return child_value

            return parent.get(
                field
            )

        return {
            "scope": scope,
            "target_id": match["id"],
            "name": match["name"],
            "folder_id": parent.get("folder_id"),
            "demo_link": (
                value("demo_link")
                or ""
            ),
            "private_message_link": (
                value(
                    "private_message_link"
                )
                or ""
            ),
            "private_chat_id": value(
                "private_chat_id"
            ),
        }

    teacher = match[
        "teacher"
    ]

    subject = match.get(
        "subject"
    )

    def value(field):
        if subject:
            subject_value = subject.get(
                field
            )

            if subject_value not in (
                None,
                "",
            ):
                return subject_value

        return teacher.get(
            field
        )

    return {
        "scope": scope,
        "target_id": match["id"],
        "name": match["name"],
        "demo_link": (
            value("demo_link")
            or ""
        ),
        "private_message_link": (
            value(
                "private_message_link"
            )
            or ""
        ),
        "private_chat_id": value(
            "private_chat_id"
        ),
    }


# ============================================================
# ADMIN NOTIFICATIONS
# ============================================================

def _admin_ids(settings):
    return [
        int(x)
        for x in (
            getattr(
                settings,
                "admin_user_ids",
                [],
            )
            or []
        )
    ]


# ACCESS_NOTIFICATION_AUTO_DELETE_V1
NOTIFICATION_DELETE_SECONDS = 3 * 60 * 60


async def _delete_access_notification_later(
    bot,
    path,
    chat_id,
    message_id,
    delete_at,
):
    delay = max(
        0,
        float(delete_at) - time.time()
    )

    if delay:
        await asyncio.sleep(delay)

    try:
        await bot.delete_message(
            chat_id=int(chat_id),
            message_id=int(message_id),
        )

        with _con(path) as db:
            db.execute(
                """
                DELETE FROM new_batch_notification_cleanup
                WHERE chat_id=? AND message_id=?
                """,
                (
                    int(chat_id),
                    int(message_id),
                )
            )
            db.commit()

        logger.info(
            "ACCESS NOTIFICATION AUTO-DELETED "
            "chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )

    except Exception:
        logger.exception(
            "Access notification auto-delete failed "
            "chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )


def _save_notification_cleanup(
    path,
    chat_id,
    message_id,
    delete_at,
):
    with _con(path) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS
            new_batch_notification_cleanup (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at REAL NOT NULL,
                PRIMARY KEY(chat_id,message_id)
            )
            """
        )

        db.execute(
            """
            INSERT OR REPLACE INTO
            new_batch_notification_cleanup
            (chat_id,message_id,delete_at)
            VALUES(?,?,?)
            """,
            (
                int(chat_id),
                int(message_id),
                float(delete_at),
            )
        )

        db.commit()


async def notify_admins(
    bot,
    settings,
    text,
):
    for admin_id in _admin_ids(
        settings
    ):
        try:
            sent = await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

            delete_at = (
                time.time()
                + NOTIFICATION_DELETE_SECONDS
            )

            _save_notification_cleanup(
                settings.database_path,
                admin_id,
                sent.message_id,
                delete_at,
            )

            asyncio.create_task(
                _delete_access_notification_later(
                    bot,
                    settings.database_path,
                    admin_id,
                    sent.message_id,
                    delete_at,
                )
            )

        except Exception:
            logger.exception(
                "New Batch access admin notification failed"
            )


# ============================================================
# CUSTOMER DELIVERY
# ============================================================

async def send_customer(
    bot,
    customer_chat_id,
    html_text,
    main_account_client=None,
):
    if main_account_client is not None:
        await main_account_client.send_message(
            int(customer_chat_id),
            html_text,
            parse_mode="html",
            link_preview=False,
        )

        return

    await bot.send_message(
        chat_id=int(
            customer_chat_id
        ),
        text=html_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============================================================
# INVITE CREATION
# ============================================================

async def create_one_person_invite(
    application,
    settings,
    target,
    mode,
    customer_chat_id,
    requested_by,
):
    chat_id = target.get(
        "private_chat_id"
    )

    if not chat_id:
        raise ValueError(
            "Private Group is not configured."
        )

    expiry_dt = datetime.now(
        timezone.utc
    ) + timedelta(
        seconds=LINK_TTL_SECONDS
    )

    invite = await application.bot.create_chat_invite_link(
        chat_id=int(chat_id),
        expire_date=expiry_dt,
        member_limit=1,
        creates_join_request=False,
        name=(
            f"{mode.upper()} "
            f"{target['name']} "
            f"{int(time.time())}"
        )[:32],
    )

    link = str(
        invite.invite_link
    )

    now = time.time()

    pre_minutes = (
        get_pre_duration(
            settings.database_path
        )
        if mode == "pre"
        else None
    )

    with _con(
        settings.database_path
    ) as db:
        db.execute("""
            INSERT INTO new_batch_access_sessions
            (
                scope,
                target_id,
                target_name,
                mode,
                customer_chat_id,
                requested_by,
                target_chat_id,
                invite_link,
                created_at,
                telegram_expires_at,
                pre_minutes,
                status,
                revoke_status
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            target["scope"],
            int(
                target["target_id"]
            ),
            target["name"],
            mode,
            int(
                customer_chat_id
            ),
            (
                int(requested_by)
                if requested_by
                else None
            ),
            int(chat_id),
            link,
            now,
            now
            + LINK_TTL_SECONDS,
            pre_minutes,
            "active",
            "pending",
        ))

        db.commit()

    return link



# SUBJECT_FOLDER_COMBINED_DEMO_V1

def _folder_combined_demo(
    path,
    target,
):
    if str(
        target.get("scope")
        or ""
    ) not in {
        "folder_batch",
        "folder_part",
    }:
        return None

    folder_id = target.get(
        "folder_id"
    )

    if not folder_id:
        return None

    with _con(path) as db:
        row = db.execute("""
            SELECT
                id,
                name,
                combined_demo_link
            FROM new_batch_folders
            WHERE id=?
              AND enabled=1
            LIMIT 1
        """, (
            int(folder_id),
        )).fetchone()

    if not row:
        return None

    link = str(
        row["combined_demo_link"]
        or ""
    ).strip()

    if not link:
        return None

    return {
        "folder_id": int(row["id"]),
        "folder_name": str(
            row["name"]
            or ""
        ),
        "link": link,
    }


def _demoall_shortcut(
    path,
):
    def key(v):
        return re.sub(
            r"[^a-z0-9]+",
            "",
            str(v or "")
            .casefold()
            .lstrip("/")
        )

    wanted = key("demoall")

    with _con(path) as db:
        rows = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM shortcuts
                WHERE enabled=1
                ORDER BY id
            """).fetchall()
        ]

    for row in rows:
        trigger = (
            row.get("trigger")
            or row.get("name")
            or ""
        )

        if key(trigger) == wanted:
            return row

    return None


async def _send_demoall_fallback(
    application,
    settings,
    customer_chat_id,
    main_account_client=None,
):
    shortcut = _demoall_shortcut(
        settings.database_path
    )

    if not shortcut:
        logger.warning(
            "DEMO FALLBACK: /demoall shortcut not found"
        )
        return False

    sid = int(
        shortcut["id"]
    )

    with _con(
        settings.database_path
    ) as db:

        columns = {
            str(r["name"])
            for r in db.execute(
                "PRAGMA table_info(shortcut_items)"
            ).fetchall()
        }

        order_col = (
            "item_order"
            if "item_order" in columns
            else "sort_order"
        )

        items = [
            dict(r)
            for r in db.execute(
                f"""
                SELECT *
                FROM shortcut_items
                WHERE shortcut_id=?
                  AND enabled=1
                ORDER BY {order_col}, id
                """,
                (sid,),
            ).fetchall()
        ]

    if not items:
        logger.warning(
            "DEMO FALLBACK: /demoall has no enabled items"
        )
        return False

    for item in items:

        text = str(
            item.get("text_content")
            or item.get("content")
            or ""
        )

        caption = str(
            item.get("caption")
            or ""
        )

        content = caption or text

        media = str(
            item.get("media_path")
            or item.get("file_path")
            or ""
        ).strip()

        raw_button = (
            item.get("button_data_json")
            or ""
        )

        url = ""
        visible = "Click Here"

        if raw_button:
            try:
                data = json.loads(
                    raw_button
                )

                url = str(
                    data.get("url")
                    or ""
                ).strip()

                visible = str(
                    data.get("text")
                    or "Click Here"
                ).strip()

            except Exception:
                pass

        if url:

            safe_url = html.escape(
                url,
                quote=True,
            )

            safe_visible = html.escape(
                visible
                or "Click Here"
            )

            if (
                visible
                and visible in content
            ):
                content = content.replace(
                    visible,
                    (
                        '<a href="'
                        + safe_url
                        + '">'
                        + safe_visible
                        + "</a>"
                    ),
                    1,
                )
            else:
                if content:
                    content += "\n"

                content += (
                    '<a href="'
                    + safe_url
                    + '">Click Here</a>'
                )

        if main_account_client is not None:

            if media:
                await main_account_client.send_file(
                    int(customer_chat_id),
                    media,
                    caption=content or None,
                    parse_mode=(
                        "html"
                        if content
                        else None
                    ),
                )

            elif content:
                await main_account_client.send_message(
                    int(customer_chat_id),
                    content,
                    parse_mode="html",
                    link_preview=False,
                )

            continue


        bot = application.bot

        if media:

            low = media.casefold()

            if low.endswith(
                (".jpg", ".jpeg", ".png", ".webp")
            ):
                await bot.send_photo(
                    chat_id=int(customer_chat_id),
                    photo=media,
                    caption=content or None,
                    parse_mode="HTML",
                    disable_notification=True,
                )

            elif low.endswith(
                (".mp4", ".mov", ".mkv")
            ):
                await bot.send_video(
                    chat_id=int(customer_chat_id),
                    video=media,
                    caption=content or None,
                    parse_mode="HTML",
                    disable_notification=True,
                )

            else:
                await bot.send_document(
                    chat_id=int(customer_chat_id),
                    document=media,
                    caption=content or None,
                    parse_mode="HTML",
                    disable_notification=True,
                )

        elif content:
            await bot.send_message(
                chat_id=int(customer_chat_id),
                text=content,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    logger.info(
        "DEMO FALLBACK: /demoall shortcut sent "
        "customer=%s shortcut_id=%s",
        customer_chat_id,
        sid,
    )

    return True



# ============================================================
# ACCESS EXECUTION
# ============================================================

async def execute_new_batch_access(
    application,
    settings,
    mode,
    query,
    customer_chat_id,
    requested_by=None,
    main_account_client=None,
):
    resolved = resolve_new_batch(
        settings.database_path,
        query,
    )

    if resolved[
        "status"
    ] != "ok":
        return {
            "ok": False,
            "reason": resolved[
                "status"
            ],
            "alternatives": resolved[
                "alternatives"
            ],
        }

    target = effective_target(
        resolved["match"]
    )

    if mode == "demo":
        link = str(
            target.get(
                "demo_link"
            )
            or ""
        ).strip()

        # Priority 2:
        # exact parent Optional folder's combined demo.
        if not link:
            folder_demo = _folder_combined_demo(
                settings.database_path,
                target,
            )

            if folder_demo:
                link = folder_demo["link"]

                logger.info(
                    "FOLDER COMBINED DEMO HIT: "
                    "folder_id=%s folder=%r "
                    "requested_target=%r",
                    folder_demo["folder_id"],
                    folder_demo["folder_name"],
                    target.get("name"),
                )

        # Priority 3:
        # Batch exists but neither its own demo nor its
        # folder-specific combined demo exists -> /demoall.
        if not link:
            sent = await _send_demoall_fallback(
                application,
                settings,
                customer_chat_id,
                main_account_client=
                    main_account_client,
            )

            if sent:
                return {
                    "ok": True,
                    "mode": "demo",
                    "target": target,
                    "fallback": "demoall",
                }

            return {
                "ok": False,
                "reason": "demo_not_configured",
                "target": target,
            }

    elif mode in {
        "normal",
        "pre",
    }:
        link = await create_one_person_invite(
            application,
            settings,
            target,
            mode,
            customer_chat_id,
            requested_by,
        )

    else:
        return {
            "ok": False,
            "reason": "invalid_mode",
        }

    rendered = render_global_message_html(
        settings.database_path,
        mode,
        link,
        batch_name=target["name"],
        pre_minutes=(
            get_pre_duration(
                settings.database_path
            )
            if mode == "pre"
            else None
        ),
    )

    await send_customer(
        application.bot,
        customer_chat_id,
        rendered,
        main_account_client=
            main_account_client,
    )

    return {
        "ok": True,
        "mode": mode,
        "target": target,
    }


# MAIN_ACCOUNT_SHORTCUT_PRIORITY_V1

def _main_shortcut_key(value):
    value = str(
        value or ""
    ).strip().lstrip("/").casefold()

    value = re.sub(
        r"[^a-z0-9]+",
        "",
        value,
    )

    return value


def _main_shortcut_lookup(
    path,
    raw_command,
):
    key = _main_shortcut_key(
        raw_command
    )

    if not key:
        return None

    with _con(path) as db:
        cols = {
            str(r["name"])
            for r in db.execute(
                "PRAGMA table_info(shortcuts)"
            ).fetchall()
        }

        if "trigger" not in cols:
            return None

        rows = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM shortcuts
                WHERE enabled=1
                ORDER BY id
            """).fetchall()
        ]

    for row in rows:
        trigger = (
            row.get("trigger")
            or row.get("name")
            or ""
        )

        if _main_shortcut_key(
            trigger
        ) == key:
            return row

    return None


async def _send_main_account_shortcut(
    path,
    client,
    customer_chat_id,
    shortcut,
):
    sid = int(
        shortcut["id"]
    )

    with _con(path) as db:
        item_cols = {
            str(r["name"])
            for r in db.execute(
                "PRAGMA table_info(shortcut_items)"
            ).fetchall()
        }

        order_col = (
            "item_order"
            if "item_order" in item_cols
            else "sort_order"
        )

        items = [
            dict(r)
            for r in db.execute(
                f"""
                SELECT *
                FROM shortcut_items
                WHERE shortcut_id=?
                  AND enabled=1
                ORDER BY {order_col}, id
                """,
                (sid,)
            ).fetchall()
        ]

    for item in items:
        text = str(
            item.get("text_content")
            or item.get("content")
            or ""
        )

        caption = str(
            item.get("caption")
            or ""
        )

        media = str(
            item.get("media_path")
            or item.get("file_path")
            or ""
        ).strip()

        content = caption or text

        raw_button = (
            item.get("button_data_json")
            or ""
        )

        url = ""
        visible = "Click Here"

        if raw_button:
            try:
                data = json.loads(
                    raw_button
                )

                url = str(
                    data.get("url")
                    or ""
                ).strip()

                visible = str(
                    data.get("text")
                    or "Click Here"
                ).strip()

            except Exception:
                pass

        if url:
            safe_url = html.escape(
                url,
                quote=True,
            )

            safe_visible = html.escape(
                visible
                or "Click Here"
            )

            if (
                visible
                and visible in content
            ):
                content = content.replace(
                    visible,
                    (
                        '<a href="'
                        + safe_url
                        + '">'
                        + safe_visible
                        + "</a>"
                    ),
                    1,
                )

            else:
                if content:
                    content += "\n"

                content += (
                    '<a href="'
                    + safe_url
                    + '">Click Here</a>'
                )

        if media:
            try:
                await client.send_file(
                    int(customer_chat_id),
                    media,
                    caption=content or None,
                    parse_mode=(
                        "html"
                        if content
                        else None
                    ),
                )

            except Exception:
                logger.exception(
                    "MAIN SHORTCUT media send failed "
                    "shortcut_id=%s media=%r",
                    sid,
                    media,
                )

                if content:
                    await client.send_message(
                        int(customer_chat_id),
                        content,
                        parse_mode="html",
                        link_preview=False,
                    )

        elif content:
            await client.send_message(
                int(customer_chat_id),
                content,
                parse_mode="html",
                link_preview=False,
            )

    return True



# ============================================================
# MAIN ACCOUNT COMMANDS
# ============================================================

def parse_command(text):
    raw = str(
        text
        or ""
    ).strip()

    if not raw.startswith("/"):
        return None, None

    body = raw[1:].strip()

    if not body:
        return None, None

    low = body.casefold()

    if low.startswith(
        "demo "
    ):
        return (
            "demo",
            body[5:].strip(),
        )

    if low.startswith(
        "pre "
    ):
        return (
            "pre",
            body[4:].strip(),
        )

    if low in {
        "admin",
        "help",
        "start",
    }:
        return None, None

    return (
        "normal",
        body,
    )


async def handle_main_account_outgoing(
    event,
    application,
    settings,
    client,
):
    if not bool(
        getattr(
            event,
            "is_private",
            False,
        )
    ):
        return False

    # MAIN_ACCOUNT_SHORTCUT_PRIORITY_V1
    # Exact saved Shortcut commands always win before
    # New Batch Normal/Demo/PRE command matching.
    _raw_outgoing = str(
        getattr(
            event,
            "raw_text",
            "",
        )
        or ""
    ).strip()

    if _raw_outgoing.startswith("/"):
        _shortcut = _main_shortcut_lookup(
            settings.database_path,
            _raw_outgoing,
        )

        if _shortcut:
            try:
                await event.delete()

            except Exception:
                logger.exception(
                    "Could not delete Main Account shortcut command"
                )

            await _send_main_account_shortcut(
                settings.database_path,
                client,
                int(
                    getattr(
                        event,
                        "chat_id",
                        0,
                    )
                    or 0
                ),
                _shortcut,
            )

            logger.info(
                "MAIN ACCOUNT SHORTCUT SENT "
                "trigger=%r shortcut_id=%s customer=%s",
                _raw_outgoing,
                _shortcut.get("id"),
                getattr(
                    event,
                    "chat_id",
                    None,
                ),
            )

            return True

    mode, query = parse_command(
        getattr(
            event,
            "raw_text",
            "",
        )
    )

    if not mode or not query:
        return False

    resolved = resolve_new_batch(
        settings.database_path,
        query,
    )

    if resolved[
        "status"
    ] == "none":
        return False

    customer_chat_id = int(
        getattr(
            event,
            "chat_id",
            0,
        )
        or 0
    )

    # Remove admin command from customer chat.
    try:
        await event.delete()
    except Exception:
        logger.exception(
            "Could not delete New Batch slash command"
        )

    if resolved[
        "status"
    ] == "ambiguous":
        lines = []

        for item in resolved[
            "alternatives"
        ]:
            lines.append(
                "• "
                + html.escape(
                    item["name"]
                )
                + " ("
                + f'{item["score"]:.0f}%'
                + ")"
            )

        await notify_admins(
            application.bot,
            settings,
            "⚠️ <b>Access command ambiguous</b>\n\n"
            f"Query: <code>{html.escape(query)}</code>\n\n"
            + "\n".join(lines),
        )

        return True

    try:
        result = await execute_new_batch_access(
            application,
            settings,
            mode,
            query,
            customer_chat_id,
            requested_by=getattr(
                event,
                "sender_id",
                None,
            ),
            main_account_client=client,
        )

    except Exception as exc:
        logger.exception(
            "New Batch access execution failed"
        )

        await notify_admins(
            application.bot,
            settings,
            "❌ <b>Access command failed</b>\n\n"
            f"Mode: <b>{html.escape(mode.upper())}</b>\n"
            f"Query: <code>{html.escape(query)}</code>\n"
            f"Error: {html.escape(str(exc))}",
        )

        return True

    if not result.get(
        "ok"
    ):
        await notify_admins(
            application.bot,
            settings,
            "⚠️ <b>Access not configured</b>\n\n"
            f"Mode: <b>{html.escape(mode.upper())}</b>\n"
            f"Query: <code>{html.escape(query)}</code>\n"
            f"Reason: <code>{html.escape(str(result.get('reason')))}</code>",
        )

        return True

    logger.info(
        "NEW BATCH ACCESS SENT "
        "mode=%s query=%r customer=%s",
        mode,
        query,
        customer_chat_id,
    )

    return True


# ============================================================
# REVOKE
# ============================================================

async def revoke_session(
    application,
    settings,
    row,
    reason,
):
    try:
        await application.bot.revoke_chat_invite_link(
            chat_id=int(
                row[
                    "target_chat_id"
                ]
            ),
            invite_link=row[
                "invite_link"
            ],
        )

        with _con(
            settings.database_path
        ) as db:
            db.execute("""
                UPDATE new_batch_access_sessions
                SET
                    revoke_status='revoked',
                    revoked_at=?,
                    last_error=''
                WHERE id=?
            """, (
                time.time(),
                int(
                    row["id"]
                ),
            ))

            db.commit()

        await notify_admins(
            application.bot,
            settings,
            "🔒 <b>Invite Link Revoked</b>\n\n"
            f"Batch: {html.escape(row['target_name'])}\n"
            f"Mode: <b>{html.escape(row['mode'].upper())}</b>\n"
            f"Reason: {html.escape(reason)}\n"
            "Status: <b>Permanently revoked</b>",
        )

        return True

    except Exception as exc:
        with _con(
            settings.database_path
        ) as db:
            db.execute("""
                UPDATE new_batch_access_sessions
                SET
                    revoke_status='pending_retry',
                    last_error=?
                WHERE id=?
            """, (
                str(exc)[:500],
                int(
                    row["id"]
                ),
            ))

            db.commit()

        logger.exception(
            "Invite revoke failed"
        )

        return False


# ============================================================
# JOIN DETECTION
# ============================================================

def _status(value):
    return str(
        value
        or ""
    ).split(".")[-1].casefold()


async def chat_member_update(
    update,
    context,
):
    change = update.chat_member

    if not change:
        return

    invite_obj = getattr(
        change,
        "invite_link",
        None,
    )

    if not invite_obj:
        return

    invite_link = str(
        getattr(
            invite_obj,
            "invite_link",
            "",
        )
        or ""
    )

    if not invite_link:
        return

    old_status = _status(
        getattr(
            change.old_chat_member,
            "status",
            "",
        )
    )

    new_status = _status(
        getattr(
            change.new_chat_member,
            "status",
            "",
        )
    )

    if not (
        old_status
        in {
            "left",
            "kicked",
        }
        and new_status
        in {
            "member",
            "restricted",
            "administrator",
            "creator",
        }
    ):
        return

    settings = context.application.bot_data.get(
        "new_batch_access_settings"
    )

    if settings is None:
        return

    ensure_access_schema(
        settings.database_path
    )

    with _con(
        settings.database_path
    ) as db:
        dbrow = db.execute("""
            SELECT *
            FROM new_batch_access_sessions
            WHERE invite_link=?
            LIMIT 1
        """, (
            invite_link,
        )).fetchone()

    if not dbrow:
        return

    row = dict(
        dbrow
    )

    if row.get(
        "joined_at"
    ):
        return

    user = change.new_chat_member.user

    name = " ".join(
        x
        for x in [
            getattr(
                user,
                "first_name",
                "",
            ),
            getattr(
                user,
                "last_name",
                "",
            ),
        ]
        if x
    ).strip() or "Unknown"

    username = str(
        getattr(
            user,
            "username",
            "",
        )
        or ""
    )

    now = time.time()

    remove_at = None
    status = "joined"

    if row[
        "mode"
    ] == "pre":
        duration = int(
            row.get(
                "pre_minutes"
            )
            or get_pre_duration(
                settings.database_path
            )
        )

        remove_at = (
            now
            + duration * 60
        )

        status = "pre_active"

    with _con(
        settings.database_path
    ) as db:
        db.execute("""
            UPDATE new_batch_access_sessions
            SET
                joined_user_id=?,
                joined_name=?,
                joined_username=?,
                joined_at=?,
                remove_at=?,
                status=?,
                revoke_status='used_pending_revoke'
            WHERE id=?
        """, (
            int(user.id),
            name,
            username,
            now,
            remove_at,
            status,
            int(row["id"]),
        ))

        db.commit()

    extra = ""

    if row[
        "mode"
    ] == "pre":
        extra = (
            "\nPRE Access: <b>"
            + str(
                int(
                    row.get(
                        "pre_minutes"
                    )
                    or get_pre_duration(
                        settings.database_path
                    )
                )
            )
            + " minutes</b>"
        )

    await notify_admins(
        context.bot,
        settings,
        "✅ <b>Customer Joined</b>\n\n"
        f"Name: {html.escape(name)}\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Username: "
        f"{html.escape('@' + username) if username else '—'}\n"
        f"Batch: {html.escape(row['target_name'])}\n"
        f"Mode: <b>{html.escape(row['mode'].upper())}</b>"
        + extra,
    )

    with _con(
        settings.database_path
    ) as db:
        refreshed = db.execute("""
            SELECT *
            FROM new_batch_access_sessions
            WHERE id=?
        """, (
            int(
                row["id"]
            ),
        )).fetchone()

    if refreshed:
        await revoke_session(
            context.application,
            settings,
            dict(refreshed),
            "First person joined",
        )


# ============================================================
# PRE REMOVE + UNBAN
# ============================================================

async def remove_pre_member(
    application,
    settings,
    row,
):
    user_id = row.get(
        "joined_user_id"
    )

    if not user_id:
        return

    chat_id = int(
        row[
            "target_chat_id"
        ]
    )

    try:
        await application.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=int(
                user_id
            ),
        )

        with _con(
            settings.database_path
        ) as db:
            db.execute("""
                UPDATE new_batch_access_sessions
                SET
                    removal_status='removed',
                    removed_at=?,
                    status='pre_removed'
                WHERE id=?
            """, (
                time.time(),
                int(
                    row["id"]
                ),
            ))

            db.commit()

    except Exception as exc:
        with _con(
            settings.database_path
        ) as db:
            db.execute("""
                UPDATE new_batch_access_sessions
                SET
                    removal_status='pending_retry',
                    last_error=?
                WHERE id=?
            """, (
                str(exc)[:500],
                int(
                    row["id"]
                ),
            ))

            db.commit()

        logger.exception(
            "PRE removal failed"
        )
        return

    unban_ok = False

    try:
        await application.bot.unban_chat_member(
            chat_id=chat_id,
            user_id=int(
                user_id
            ),
            only_if_banned=True,
        )

        unban_ok = True

        with _con(
            settings.database_path
        ) as db:
            db.execute("""
                UPDATE new_batch_access_sessions
                SET
                    unban_status='unbanned',
                    unbanned_at=?,
                    status='completed',
                    last_error=''
                WHERE id=?
            """, (
                time.time(),
                int(
                    row["id"]
                ),
            ))

            db.commit()

    except Exception as exc:
        with _con(
            settings.database_path
        ) as db:
            db.execute("""
                UPDATE new_batch_access_sessions
                SET
                    unban_status='pending_retry',
                    last_error=?
                WHERE id=?
            """, (
                str(exc)[:500],
                int(
                    row["id"]
                ),
            ))

            db.commit()

        logger.exception(
            "PRE unban failed"
        )

    await notify_admins(
        application.bot,
        settings,
        "🚪 <b>PRE Access Removed</b>\n\n"
        f"Name: {html.escape(row.get('joined_name') or 'Unknown')}\n"
        f"Telegram ID: <code>{user_id}</code>\n"
        f"Batch: {html.escape(row['target_name'])}\n"
        f"Access Duration: <b>{int(row.get('pre_minutes') or 5)} minutes</b>\n"
        "Removal: <b>Completed</b>\n"
        f"Ban cleanup: "
        f"<b>{'Unbanned successfully' if unban_ok else 'Pending retry'}</b>",
    )


# ============================================================
# CRASH / NETWORK RECOVERY
# ============================================================

async def reconcile(
    application,
    settings,
):
    ensure_access_schema(
        settings.database_path
    )

    now = time.time()

    with _con(
        settings.database_path
    ) as db:
        rows = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_access_sessions
                WHERE revoke_status != 'revoked'
                  AND (
                       telegram_expires_at <= ?
                    OR joined_at IS NOT NULL
                  )
            """, (
                now,
            )).fetchall()
        ]

    for row in rows:
        reason = (
            "First person joined"
            if row.get(
                "joined_at"
            )
            else "24-hour expiry"
        )

        ok = await revoke_session(
            application,
            settings,
            row,
            reason,
        )

        if (
            ok
            and not row.get(
                "joined_at"
            )
        ):
            with _con(
                settings.database_path
            ) as db:
                db.execute("""
                    UPDATE new_batch_access_sessions
                    SET status='expired'
                    WHERE id=?
                """, (
                    int(
                        row["id"]
                    ),
                ))

                db.commit()

    with _con(
        settings.database_path
    ) as db:
        pre_rows = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_access_sessions
                WHERE mode='pre'
                  AND joined_user_id IS NOT NULL
                  AND remove_at IS NOT NULL
                  AND remove_at <= ?
                  AND status != 'completed'
            """, (
                now,
            )).fetchall()
        ]

    for row in pre_rows:
        if (
            row.get(
                "removal_status"
            )
            == "removed"
            and row.get(
                "unban_status"
            )
            != "unbanned"
        ):
            try:
                await application.bot.unban_chat_member(
                    chat_id=int(
                        row[
                            "target_chat_id"
                        ]
                    ),
                    user_id=int(
                        row[
                            "joined_user_id"
                        ]
                    ),
                    only_if_banned=True,
                )

                with _con(
                    settings.database_path
                ) as db:
                    db.execute("""
                        UPDATE new_batch_access_sessions
                        SET
                            unban_status='unbanned',
                            unbanned_at=?,
                            status='completed',
                            last_error=''
                        WHERE id=?
                    """, (
                        time.time(),
                        int(
                            row["id"]
                        ),
                    ))

                    db.commit()

            except Exception:
                logger.exception(
                    "PRE recovery unban failed"
                )

        elif (
            row.get(
                "removal_status"
            )
            != "removed"
        ):
            await remove_pre_member(
                application,
                settings,
                row,
            )


async def recovery_loop(
    application,
    settings,
):
    await asyncio.sleep(8)

    logger.info(
        "GLOBAL NEW BATCH ACCESS recovery ready"
    )

    while True:
        try:
            await reconcile(
                application,
                settings,
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "GLOBAL NEW BATCH ACCESS recovery failed"
            )

        await asyncio.sleep(
            RECOVERY_SECONDS
        )


# ============================================================
# REGISTRATION
# ============================================================

# ACCESS_NOTIFICATION_RECOVERY_V1
async def recover_access_notification_cleanup(
    bot,
    settings,
):
    path = settings.database_path

    with _con(path) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS
            new_batch_notification_cleanup (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at REAL NOT NULL,
                PRIMARY KEY(chat_id,message_id)
            )
            """
        )

        rows = db.execute(
            """
            SELECT chat_id,message_id,delete_at
            FROM new_batch_notification_cleanup
            """
        ).fetchall()

        db.commit()

    for row in rows:
        asyncio.create_task(
            _delete_access_notification_later(
                bot,
                path,
                row["chat_id"],
                row["message_id"],
                row["delete_at"],
            )
        )


def register_new_batch_access_system(
    application,
    settings,
):
    ensure_access_schema(
        settings.database_path
    )

    application.bot_data[
        "new_batch_access_settings"
    ] = settings

    asyncio.create_task(
        recover_access_notification_cleanup(
            application.bot,
            settings,
        )
    )

    async def internal_execute(
        *,
        mode,
        query,
        customer_chat_id,
        requested_by=None,
    ):
        return await execute_new_batch_access(
            application,
            settings,
            mode,
            query,
            customer_chat_id,
            requested_by=requested_by,
            main_account_client=
                application.bot_data.get(
                    "main_account_client"
                ),
        )

    # Trusted internal AI/bot hook.
    application.bot_data[
        "new_batch_access_execute"
    ] = internal_execute

    application.add_handler(
        ChatMemberHandler(
            chat_member_update,
            ChatMemberHandler.CHAT_MEMBER,
        ),
        group=-46,
    )

    asyncio.create_task(
        recovery_loop(
            application,
            settings,
        )
    )

    logger.info(
        "GLOBAL NEW BATCH ACCESS SYSTEM REGISTERED"
    )
