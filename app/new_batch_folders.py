import html
import re
import sqlite3
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# FOLDER_ACCESS_ADMIN_V1
from app.new_batch_access import (
    ensure_access_schema,
    validate_private_target,
)



# ============================================================
# NEW BATCH FOLDERS V1
# ============================================================

def _con(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _set_folder_access(
    path,
    scope,
    item_id,
    field,
    value,
):
    ensure_access_schema(path)

    tables = {
        "batch": "new_batch_folder_batches",
        "part": "new_batch_folder_parts",
    }

    allowed = {
        "demo_link",
        "private_message_link",
        "private_chat_id",
    }

    table = tables.get(scope)

    if not table or field not in allowed:
        raise ValueError(
            "Invalid folder access field."
        )

    with _con(path) as db:
        db.execute(
            f"UPDATE {table} "
            f"SET {field}=?, updated_at=? "
            "WHERE id=?",
            (
                value,
                int(time.time()),
                int(item_id),
            )
        )
        db.commit()


def _norm(v):
    v = str(v or "").casefold().replace("_", " ")
    v = re.sub(r"[^\w\s]+", " ", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()


def _money(v):
    v = str(v or "").strip()

    if not v:
        return ""

    return v if v.startswith("₹") else "₹" + v


def _slug(v):
    s = re.sub(
        r"[^a-z0-9]+",
        "-",
        _norm(v)
    ).strip("-")

    return s or f"item-{int(time.time())}"


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


def ensure_schema(path):
    with _con(path) as con:

        con.execute("""
        CREATE TABLE IF NOT EXISTS new_batch_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS new_batch_folder_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,

            institute TEXT NOT NULL DEFAULT '',
            teacher TEXT NOT NULL DEFAULT '',

            aliases TEXT NOT NULL DEFAULT '',

            year TEXT NOT NULL DEFAULT '',
            price TEXT NOT NULL DEFAULT '',
            combined_price TEXT NOT NULL DEFAULT '',

            availability INTEGER NOT NULL DEFAULT 1,

            notes TEXT NOT NULL DEFAULT '',
            special_rule TEXT NOT NULL DEFAULT '',

            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,

            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY(folder_id)
                REFERENCES new_batch_folders(id)
                ON DELETE CASCADE,

            UNIQUE(folder_id,slug)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS new_batch_folder_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,

            name TEXT NOT NULL,
            teacher TEXT NOT NULL DEFAULT '',

            aliases TEXT NOT NULL DEFAULT '',

            years TEXT NOT NULL DEFAULT '',
            price TEXT NOT NULL DEFAULT '',

            availability INTEGER NOT NULL DEFAULT 1,
            standalone INTEGER NOT NULL DEFAULT 1,

            notes TEXT NOT NULL DEFAULT '',

            sort_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,

            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY(batch_id)
                REFERENCES new_batch_folder_batches(id)
                ON DELETE CASCADE
        )
        """)

        con.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_nbf_batch_folder
        ON new_batch_folder_batches(folder_id,sort_order,id)
        """)

        con.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_nbf_part_batch
        ON new_batch_folder_parts(batch_id,sort_order,id)
        """)

        con.commit()


# ============================================================
# SEED HELPERS
# ============================================================

def _upsert_folder(con, slug, name, aliases=""):
    now = int(time.time())

    con.execute("""
    INSERT INTO new_batch_folders
    (
        slug,name,aliases,enabled,created_at,updated_at
    )
    VALUES(?,?,?,1,?,?)

    ON CONFLICT(slug) DO UPDATE SET
        name=excluded.name,
        aliases=excluded.aliases,
        enabled=1,
        updated_at=excluded.updated_at
    """, (
        slug,
        name,
        aliases,
        now,
        now
    ))

    return int(
        con.execute(
            "SELECT id FROM new_batch_folders WHERE slug=?",
            (slug,)
        ).fetchone()[0]
    )


def _upsert_batch(
    con,
    folder_id,
    slug,
    name,
    institute="",
    teacher="",
    aliases="",
    year="",
    price="",
    combined_price="",
    availability=1,
    notes="",
    special_rule="",
    sort_order=0,
):
    now = int(time.time())

    con.execute("""
    INSERT INTO new_batch_folder_batches
    (
        folder_id,slug,name,
        institute,teacher,aliases,
        year,price,combined_price,
        availability,notes,special_rule,
        enabled,sort_order,
        created_at,updated_at
    )
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)

    ON CONFLICT(folder_id,slug) DO UPDATE SET
        name=excluded.name,
        institute=excluded.institute,
        teacher=excluded.teacher,
        aliases=excluded.aliases,
        year=excluded.year,
        price=excluded.price,
        combined_price=excluded.combined_price,
        availability=excluded.availability,
        notes=excluded.notes,
        special_rule=excluded.special_rule,
        enabled=1,
        sort_order=excluded.sort_order,
        updated_at=excluded.updated_at
    """, (
        int(folder_id),
        slug,
        name,
        institute,
        teacher,
        aliases,
        year,
        price,
        combined_price,
        int(bool(availability)),
        notes,
        special_rule,
        int(sort_order),
        now,
        now
    ))

    return int(
        con.execute("""
            SELECT id
            FROM new_batch_folder_batches
            WHERE folder_id=? AND slug=?
        """, (
            int(folder_id),
            slug
        )).fetchone()[0]
    )


def _replace_parts(con, batch_id, parts):
    con.execute(
        "DELETE FROM new_batch_folder_parts WHERE batch_id=?",
        (int(batch_id),)
    )

    now = int(time.time())

    for i, p in enumerate(parts, 1):

        con.execute("""
        INSERT INTO new_batch_folder_parts
        (
            batch_id,
            name,
            teacher,
            aliases,
            years,
            price,
            availability,
            standalone,
            notes,
            sort_order,
            enabled,
            created_at,
            updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)
        """, (
            int(batch_id),
            p.get("name", ""),
            p.get("teacher", ""),
            p.get("aliases", ""),
            p.get("years", ""),
            p.get("price", ""),
            int(bool(p.get("availability", 1))),
            int(bool(p.get("standalone", 1))),
            p.get("notes", ""),
            i,
            now,
            now
        ))


# ============================================================
# INITIAL FOLDERS + SOCIOLOGY DATA
# ============================================================

def seed_initial_data(path):
    ensure_schema(path)

    with _con(path) as con:

        sociology_id = _upsert_folder(
            con,
            "sociology",
            "Sociology",
            "sociology,sociology optional,socio"
        )

        _upsert_folder(
            con,
            "psir",
            "PSIR",
            "psir,political science optional"
        )

        _upsert_folder(
            con,
            "current-affairs",
            "Current Affairs",
            "current affairs,ca"
        )

        _upsert_folder(
            con,
            "test-series",
            "Test Series",
            "test series,tests"
        )

        # ----------------------------------------------------
        # 1. PRANAY AGARWAL / SARTHI IAS
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "pranay-agarwal",
            "Pranay Agarwal Sir Sociology",
            institute="Sarthi IAS",
            teacher="Pranay Agarwal Sir",
            aliases=(
                "Pranay Agarwal Sociology,"
                "Pranay Sir Sociology,"
                "Pranay Sociology,"
                "Sarthi IAS Sociology,"
                "Saarthi IAS Sociology,"
                "Pranay Sociology QEP,"
                "Pranay Sociology Enrichment"
            ),
            year="2026",
            price="500",
            notes=(
                "Complete Sociology batch including Foundation, "
                "QEP/Enrichment and Answer Writing Practice."
            ),
            special_rule=(
                "Answer Writing Practice is available only with "
                "the complete Pranay Agarwal Sociology batch and "
                "is not sold independently."
            ),
            sort_order=1
        )

        _replace_parts(con, bid, [
            {
                "name": "Foundation Sociology",
                "aliases": (
                    "foundation,"
                    "foundation sociology,"
                    "pranay foundation"
                ),
                "years": "2026",
                "standalone": 0,
                "notes": "Included in complete Pranay Agarwal Sociology."
            },
            {
                "name": "Sociology QEP / Enrichment",
                "aliases": (
                    "qep,"
                    "sociology qep,"
                    "enrichment,"
                    "sociology enrichment,"
                    "pranay qep,"
                    "pranay enrichment"
                ),
                "years": "2026",
                "standalone": 0,
                "notes": (
                    "QEP and Enrichment refer to this included part."
                )
            },
            {
                "name": "Answer Writing Practice",
                "aliases": (
                    "answer writing,"
                    "answer writing practice,"
                    "pranay answer writing"
                ),
                "years": "2026",
                "standalone": 0,
                "notes": (
                    "Only with complete Pranay Agarwal Sociology batch."
                )
            },
        ])

        # ----------------------------------------------------
        # 2. VISION IAS SOCIOLOGY
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "vision-ias-sociology",
            "Vision IAS Sociology",
            institute="Vision IAS",
            aliases=(
                "Vision Sociology,"
                "Vision IAS Sociology,"
                "Vision Sociology Optional"
            ),
            price="500",
            notes=(
                "Paper 1 by Sunil Sir. "
                "Paper 2 by Smriti Ma'am."
            ),
            sort_order=2
        )

        _replace_parts(con, bid, [
            {
                "name": "Paper 1",
                "teacher": "Sunil Sir",
                "aliases": (
                    "paper 1,"
                    "sociology paper 1,"
                    "sunil sir sociology"
                ),
                "standalone": 0
            },
            {
                "name": "Paper 2",
                "teacher": "Smriti Ma'am",
                "aliases": (
                    "paper 2,"
                    "sociology paper 2,"
                    "smriti sociology,"
                    "smriti mam sociology,"
                    "smriti maam sociology"
                ),
                "standalone": 0
            },
        ])

        # ----------------------------------------------------
        # 3. LEVEL UP SOCIOLOGY
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "level-up-sociology",
            "Level Up Sociology",
            institute="Level Up IAS",
            teacher="Nishant Singh Sir",
            aliases=(
                "Level Up Sociology,"
                "LevelUp Sociology,"
                "Label Up Sociology,"
                "Level Up,"
                "LevelUp,"
                "Label Up,"
                "Nishant Sociology,"
                "Nisant Sociology,"
                "Nisant Singh Sociology,"
                "Nishant Sir Sociology"
            ),
            combined_price="400",
            notes=(
                "Foundation + Crash Course combined price ₹400."
            ),
            sort_order=3
        )

        _replace_parts(con, bid, [
            {
                "name": "Foundation Sociology",
                "teacher": "Nishant Singh Sir",
                "aliases": (
                    "foundation,"
                    "foundation sociology,"
                    "level up foundation"
                ),
                "years": "2021",
                "price": "300"
            },
            {
                "name": "Crash Course",
                "teacher": "Nishant Singh Sir",
                "aliases": (
                    "crash,"
                    "crash course,"
                    "sociology crash course"
                ),
                "years": "2024,2025",
                "price": "300"
            },
        ])

        # ----------------------------------------------------
        # 4. SLEEPY IAS / SLEEPY CLASSES
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "sleepy-sociology",
            "Sleepy IAS Sociology",
            institute="Sleepy IAS",
            teacher="Sekhar Sir",
            aliases=(
                "Sleepy IAS Sociology,"
                "Sleepy Classes Sociology,"
                "Sleepy Sociology,"
                "Sekhar Sir Sleepy Sociology,"
                "Shekhar Sir Sleepy Sociology"
            ),
            year="2024",
            price="400",
            sort_order=4
        )

        _replace_parts(con, bid, [])

        # ----------------------------------------------------
        # 5. VIKAS RANJAN / TRIUMPH IAS
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "vikas-ranjan",
            "Vikas Ranjan Sir Sociology",
            institute="Triumph IAS",
            teacher="Vikas Ranjan Sir",
            aliases=(
                "Vikas Ranjan Sociology,"
                "Vikas Sir Sociology,"
                "Triumph IAS Sociology,"
                "Triumph Sociology"
            ),
            year="2024",
            price="400",
            sort_order=5
        )

        _replace_parts(con, bid, [])

        # ----------------------------------------------------
        # 6. VAJIRAM SOCIOLOGY
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "vajiram-sociology",
            "Vajiram Sociology",
            institute="Vajiram",
            teacher="Mohapatra Sir",
            aliases=(
                "Vajiram Sociology,"
                "Vajiram Sociology Optional,"
                "Mohapatra Sir Sociology,"
                "Mohapatra Sociology"
            ),
            year="2021",
            price="300",
            notes=(
                "Includes Foundation Sociology and QEP Sociology."
            ),
            sort_order=6
        )

        _replace_parts(con, bid, [
            {
                "name": "Foundation Sociology",
                "teacher": "Mohapatra Sir",
                "aliases": "foundation,foundation sociology",
                "years": "2021",
                "standalone": 0
            },
            {
                "name": "QEP Sociology",
                "teacher": "Mohapatra Sir",
                "aliases": "qep,qep sociology",
                "years": "2021",
                "standalone": 0
            },
        ])

        # ----------------------------------------------------
        # 7. SUNYA IAS
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "sunya-ias-sociology",
            "Sunya IAS Sociology",
            institute="Sunya IAS",
            aliases=(
                "Sunya Sociology,"
                "Sunya IAS Sociology"
            ),
            year="2024",
            price="300",
            sort_order=7
        )

        _replace_parts(con, bid, [])

        # ----------------------------------------------------
        # 8. FORUM IAS
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "forum-ias-sociology",
            "Forum IAS Sociology",
            institute="Forum IAS",
            aliases=(
                "Forum Sociology,"
                "Forum IAS Sociology"
            ),
            year="2024",
            price="400",
            sort_order=8
        )

        _replace_parts(con, bid, [])

        # ----------------------------------------------------
        # 9. STUDY IQ
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "study-iq-sociology",
            "Study IQ Sociology",
            institute="Study IQ",
            teacher="Diwakar Bothra Sir",
            aliases=(
                "StudyIQ Sociology,"
                "Study IQ Sociology,"
                "Diwakar Bothra Sociology,"
                "Diwakar Sir Sociology,"
                "Bothra Sir Sociology"
            ),
            year="2026",
            price="500",
            sort_order=9
        )

        _replace_parts(con, bid, [])

        # ----------------------------------------------------
        # 10. PHYSICS WALLAH SOCIOLOGY
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "physics-wallah-sociology",
            "Physics Wallah Sociology",
            institute="Physics Wallah",
            aliases=(
                "Physics Wallah Sociology,"
                "Physics Wala Sociology,"
                "PW Sociology"
            ),
            year="2026",
            notes=(
                "Two separate faculty batches. "
                "Sekhar Sir ₹500 and Nitin Sir ₹500."
            ),
            sort_order=10
        )

        _replace_parts(con, bid, [
            {
                "name": "Sekhar Sir Sociology",
                "teacher": "Sekhar Sir",
                "aliases": (
                    "sekhar sir,"
                    "shekhar sir,"
                    "sekhar sociology,"
                    "shekhar sociology"
                ),
                "years": "2026",
                "price": "500",
                "standalone": 1
            },
            {
                "name": "Nitin Sir Sociology",
                "teacher": "Nitin Sir",
                "aliases": (
                    "nitin sir,"
                    "nitin sociology"
                ),
                "years": "2026",
                "price": "500",
                "standalone": 1
            },
        ])

        # ----------------------------------------------------
        # 11. VISION IAS SOCIOLOGY TEST DISCUSSION
        # ----------------------------------------------------
        bid = _upsert_batch(
            con,
            sociology_id,
            "vision-test-discussion",
            "Vision IAS Sociology Test Discussion",
            institute="Vision IAS",
            aliases=(
                "Vision Sociology Test Discussion,"
                "Vision Test Discussion Sociology,"
                "Vision Sociology Test,"
                "Sociology Test Discussion Vision"
            ),
            notes=(
                "Year and price not provided yet."
            ),
            sort_order=11
        )

        _replace_parts(con, bid, [])

        con.commit()


# ============================================================
# GETTERS
# ============================================================

def folders(path):
    ensure_schema(path)

    with _con(path) as con:
        return [
            dict(r)
            for r in con.execute("""
                SELECT *
                FROM new_batch_folders
                ORDER BY id
            """).fetchall()
        ]


def folder(path, fid):
    with _con(path) as con:
        r = con.execute("""
            SELECT *
            FROM new_batch_folders
            WHERE id=?
        """, (int(fid),)).fetchone()

        return dict(r) if r else None


def folder_by_slug(path, slug):
    with _con(path) as con:
        r = con.execute("""
            SELECT *
            FROM new_batch_folders
            WHERE slug=?
        """, (slug,)).fetchone()

        return dict(r) if r else None


def batches(path, fid):
    with _con(path) as con:
        return [
            dict(r)
            for r in con.execute("""
                SELECT *
                FROM new_batch_folder_batches
                WHERE folder_id=?
                ORDER BY sort_order,id
            """, (int(fid),)).fetchall()
        ]


def batch(path, bid):
    with _con(path) as con:
        r = con.execute("""
            SELECT *
            FROM new_batch_folder_batches
            WHERE id=?
        """, (int(bid),)).fetchone()

        return dict(r) if r else None


def parts(path, bid):
    with _con(path) as con:
        return [
            dict(r)
            for r in con.execute("""
                SELECT *
                FROM new_batch_folder_parts
                WHERE batch_id=?
                ORDER BY sort_order,id
            """, (int(bid),)).fetchall()
        ]


def part(path, pid):
    with _con(path) as con:
        r = con.execute("""
            SELECT *
            FROM new_batch_folder_parts
            WHERE id=?
        """, (int(pid),)).fetchone()

        return dict(r) if r else None


# ============================================================
# EDIT HELPERS
# ============================================================

def set_batch_field(path, bid, field, value):
    allowed = {
        "name",
        "institute",
        "teacher",
        "aliases",
        "year",
        "price",
        "combined_price",
        "availability",
        "notes",
        "special_rule",
        "enabled",
    }

    if field not in allowed:
        raise ValueError("Invalid batch field")

    with _con(path) as con:
        con.execute(
            f"""
            UPDATE new_batch_folder_batches
            SET {field}=?,updated_at=?
            WHERE id=?
            """,
            (
                value,
                int(time.time()),
                int(bid)
            )
        )
        con.commit()


def set_part_field(path, pid, field, value):
    allowed = {
        "name",
        "teacher",
        "aliases",
        "years",
        "price",
        "availability",
        "standalone",
        "notes",
        "enabled",
    }

    if field not in allowed:
        raise ValueError("Invalid part field")

    with _con(path) as con:
        con.execute(
            f"""
            UPDATE new_batch_folder_parts
            SET {field}=?,updated_at=?
            WHERE id=?
            """,
            (
                value,
                int(time.time()),
                int(pid)
            )
        )
        con.commit()


def add_batch(path, fid, name):
    now = int(time.time())

    with _con(path) as con:
        order = int(
            con.execute("""
                SELECT COALESCE(MAX(sort_order),0)+1
                FROM new_batch_folder_batches
                WHERE folder_id=?
            """, (int(fid),)).fetchone()[0]
        )

        slug = _slug(name)
        base = slug
        i = 2

        while con.execute("""
            SELECT 1
            FROM new_batch_folder_batches
            WHERE folder_id=? AND slug=?
        """, (
            int(fid),
            slug
        )).fetchone():
            slug = f"{base}-{i}"
            i += 1

        cur = con.execute("""
            INSERT INTO new_batch_folder_batches
            (
                folder_id,slug,name,
                enabled,sort_order,
                created_at,updated_at
            )
            VALUES(?,?,?,1,?,?,?)
        """, (
            int(fid),
            slug,
            name,
            order,
            now,
            now
        ))

        con.commit()

        return int(cur.lastrowid)


def add_part(path, bid, name):
    now = int(time.time())

    with _con(path) as con:
        order = int(
            con.execute("""
                SELECT COALESCE(MAX(sort_order),0)+1
                FROM new_batch_folder_parts
                WHERE batch_id=?
            """, (int(bid),)).fetchone()[0]
        )

        cur = con.execute("""
            INSERT INTO new_batch_folder_parts
            (
                batch_id,name,
                sort_order,enabled,
                created_at,updated_at
            )
            VALUES(?,?,?,1,?,?)
        """, (
            int(bid),
            name,
            order,
            now,
            now
        ))

        con.commit()

        return int(cur.lastrowid)


# ============================================================
# ADMIN UI
# ============================================================

def folders_keyboard(path):
    rows = folders(path)

    kb = []

    for r in rows:
        kb.append([
            InlineKeyboardButton(
                "📁 " + r["name"],
                callback_data=f'admin:nbf:folder:{r["id"]}'
            )
        ])

    kb.append([
        InlineKeyboardButton(
            "⬅️ New Batches",
            callback_data="admin:new_batches"
        )
    ])

    return InlineKeyboardMarkup(kb)


async def show_folders(q, path):
    rows = folders(path)

    await q.edit_message_text(
        "📁 <b>Batch Folders</b>\n\n"
        f"Folders: <b>{len(rows)}</b>\n\n"
        "Choose a folder:",
        parse_mode="HTML",
        reply_markup=folders_keyboard(path)
    )


# FOLDER_COMBINED_DEMO_ADMIN_V1
async def show_folder(q, path, fid):
    ensure_access_schema(path)

    f = folder(path, fid)

    if not f:
        await show_folders(q, path)
        return

    rows = batches(path, fid)

    combined_demo = str(
        f.get("combined_demo_link")
        or ""
    ).strip()

    kb = []

    for r in rows:
        prefix = (
            "✅ "
            if r["availability"]
            else "❌ "
        )

        kb.append([
            InlineKeyboardButton(
                prefix + r["name"],
                callback_data=
                    f'admin:nbf:batch:{r["id"]}'
            )
        ])

    kb += [
        [
            InlineKeyboardButton(
                (
                    "🎬 Combined Demo ✅"
                    if combined_demo
                    else "🎬 Add Combined Demo"
                ),
                callback_data=
                    f"admin:nbf:combined_demo:{fid}"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Add Batch",
                callback_data=
                    f"admin:nbf:add_batch:{fid}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Folders",
                callback_data="admin:nbfolders"
            )
        ]
    ]

    await q.edit_message_text(
        f'📁 <b>{html.escape(f["name"])}</b>\n\n'
        f'Batches: <b>{len(rows)}</b>\n'
        f'🎬 Combined Demo: '
        f'<b>{"SET ✅" if combined_demo else "NOT SET"}</b>',
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )




async def show_batch(q, path, bid):
    b = batch(path, bid)

    if not b:
        await show_folders(q, path)
        return

    ps = parts(path, bid)

    def v(x):
        return html.escape(str(x)) if x not in (None, "") else "—"

    text = (
        f'📚 <b>{v(b["name"])}</b>\n\n'
        f'🏫 Institute: {v(b.get("institute"))}\n'
        f'👨‍🏫 Teacher: {v(b.get("teacher"))}\n'
        f'📅 Year: {v(b.get("year"))}\n'
        f'💰 Price: {v(_money(b.get("price")))}\n'
        f'💰 Combined Price: {v(_money(b.get("combined_price")))}\n'
        f'📦 Status: '
        f'{"Available ✅" if b["availability"] else "Not Available ❌"}\n'
        f'🔑 Aliases: {v(b.get("aliases"))}\n'
        f'📝 Notes: {v(b.get("notes"))}\n'
        f'⚙️ Rule: {v(b.get("special_rule"))}\n\n'
        f'📑 Parts: <b>{len(ps)}</b>'
    )

    kb = [
        [
            InlineKeyboardButton(
                "🎬 Demo Link",
                callback_data=f"admin:nbf:access_demo_batch:{bid}"
            ),
            InlineKeyboardButton(
                "🔐 Private Group",
                callback_data=f"admin:nbf:access_private_batch:{bid}"
            )
        ],
        [
            InlineKeyboardButton(
                "📑 Parts",
                callback_data=f"admin:nbf:parts:{bid}"
            ),
            InlineKeyboardButton(
                "➕ Add Part",
                callback_data=f"admin:nbf:add_part:{bid}"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Name",
                callback_data=f"admin:nbf:bf:{bid}:name"
            ),
            InlineKeyboardButton(
                "🏫 Institute",
                callback_data=f"admin:nbf:bf:{bid}:institute"
            )
        ],
        [
            InlineKeyboardButton(
                "👨‍🏫 Teacher",
                callback_data=f"admin:nbf:bf:{bid}:teacher"
            ),
            InlineKeyboardButton(
                "📅 Year",
                callback_data=f"admin:nbf:bf:{bid}:year"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Price",
                callback_data=f"admin:nbf:bf:{bid}:price"
            ),
            InlineKeyboardButton(
                "💰 Combined Price",
                callback_data=f"admin:nbf:bf:{bid}:combined_price"
            )
        ],
        [
            InlineKeyboardButton(
                "🔑 Aliases",
                callback_data=f"admin:nbf:bf:{bid}:aliases"
            )
        ],
        [
            InlineKeyboardButton(
                "📝 Notes",
                callback_data=f"admin:nbf:bf:{bid}:notes"
            ),
            InlineKeyboardButton(
                "⚙️ Special Rule",
                callback_data=f"admin:nbf:bf:{bid}:special_rule"
            )
        ],
        [
            InlineKeyboardButton(
                (
                    "❌ Mark Not Available"
                    if b["availability"]
                    else "✅ Mark Available"
                ),
                callback_data=f"admin:nbf:toggle_batch:{bid}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Folder",
                callback_data=f'admin:nbf:folder:{b["folder_id"]}'
            )
        ]
    ]

    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def show_parts(q, path, bid):
    b = batch(path, bid)
    rows = parts(path, bid)

    if not b:
        await show_folders(q, path)
        return

    kb = []

    for r in rows:
        kb.append([
            InlineKeyboardButton(
                ("✅ " if r["availability"] else "❌ ")
                + r["name"],
                callback_data=f'admin:nbf:part:{r["id"]}'
            )
        ])

    kb += [
        [
            InlineKeyboardButton(
                "➕ Add Part",
                callback_data=f"admin:nbf:add_part:{bid}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Batch",
                callback_data=f"admin:nbf:batch:{bid}"
            )
        ]
    ]

    await q.edit_message_text(
        f'📑 <b>{html.escape(b["name"])}</b> — Parts',
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def show_part(q, path, pid):
    p = part(path, pid)

    if not p:
        await show_folders(q, path)
        return

    b = batch(path, p["batch_id"])

    def v(x):
        return html.escape(str(x)) if x not in (None, "") else "—"

    text = (
        f'📑 <b>{v(p["name"])}</b>\n\n'
        f'📚 Batch: {v(b["name"] if b else "")}\n'
        f'👨‍🏫 Teacher: {v(p.get("teacher"))}\n'
        f'📅 Year(s): {v(p.get("years"))}\n'
        f'💰 Price: {v(_money(p.get("price")))}\n'
        f'📦 Available: {"YES ✅" if p["availability"] else "NO ❌"}\n'
        f'🛒 Standalone: {"YES ✅" if p["standalone"] else "NO ❌"}\n'
        f'🔑 Aliases: {v(p.get("aliases"))}\n'
        f'📝 Notes: {v(p.get("notes"))}'
    )

    kb = [
        [
            InlineKeyboardButton(
                "🎬 Demo Link",
                callback_data=f"admin:nbf:access_demo_part:{pid}"
            ),
            InlineKeyboardButton(
                "🔐 Private Group",
                callback_data=f"admin:nbf:access_private_part:{pid}"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Name",
                callback_data=f"admin:nbf:pf:{pid}:name"
            ),
            InlineKeyboardButton(
                "👨‍🏫 Teacher",
                callback_data=f"admin:nbf:pf:{pid}:teacher"
            )
        ],
        [
            InlineKeyboardButton(
                "📅 Year(s)",
                callback_data=f"admin:nbf:pf:{pid}:years"
            ),
            InlineKeyboardButton(
                "💰 Price",
                callback_data=f"admin:nbf:pf:{pid}:price"
            )
        ],
        [
            InlineKeyboardButton(
                "🔑 Aliases",
                callback_data=f"admin:nbf:pf:{pid}:aliases"
            ),
            InlineKeyboardButton(
                "📝 Notes",
                callback_data=f"admin:nbf:pf:{pid}:notes"
            )
        ],
        [
            InlineKeyboardButton(
                (
                    "❌ Mark Not Available"
                    if p["availability"]
                    else "✅ Mark Available"
                ),
                callback_data=f"admin:nbf:toggle_part:{pid}"
            )
        ],
        [
            InlineKeyboardButton(
                (
                    "🛒 Standalone: ON"
                    if p["standalone"]
                    else "🛒 Standalone: OFF"
                ),
                callback_data=f"admin:nbf:toggle_standalone:{pid}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Parts",
                callback_data=f'admin:nbf:parts:{p["batch_id"]}'
            )
        ]
    ]

    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ============================================================
# ADMIN CALLBACK HANDLER
# ============================================================

async def handle_callback(q, context, path, action):
    ensure_schema(path)

    if action == "nbfolders":
        await show_folders(q, path)
        return True

    if not action.startswith("nbf:"):
        return False

    x = action.split(":")
    cmd = x[1]

    if cmd == "folder":
        await show_folder(q, path, int(x[2]))
        return True

    if cmd == "batch":
        await show_batch(q, path, int(x[2]))
        return True

    if cmd == "parts":
        await show_parts(q, path, int(x[2]))
        return True

    if cmd == "part":
        await show_part(q, path, int(x[2]))
        return True

    if cmd == "access_demo_batch":
        bid = int(x[2])

        context.user_data[
            "new_batch_folder_pending"
        ] = {
            "kind": "folder_access_demo_batch",
            "id": bid,
        }

        await q.edit_message_text(
            "🎬 <b>Demo Link</b>\n\n"
            "Send the permanent Demo link.\n\n"
            "Send <code>clear</code> to remove it.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:batch:{bid}"
                )
            ]])
        )

        return True


    if cmd == "access_private_batch":
        bid = int(x[2])

        context.user_data[
            "new_batch_folder_pending"
        ] = {
            "kind": "folder_access_private_batch",
            "id": bid,
        }

        await q.edit_message_text(
            "🔐 <b>Private Group / Channel</b>\n\n"
            "Send any message link copied from inside "
            "the private group/channel.\n\n"
            "The worker sessions will automatically add/promote "
            "the AI bot when possible.\n\n"
            "Send <code>clear</code> to remove it.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:batch:{bid}"
                )
            ]])
        )

        return True


    if cmd == "access_demo_part":
        pid = int(x[2])

        context.user_data[
            "new_batch_folder_pending"
        ] = {
            "kind": "folder_access_demo_part",
            "id": pid,
        }

        await q.edit_message_text(
            "🎬 <b>Part Demo Link</b>\n\n"
            "Send the permanent Demo link.\n\n"
            "If a Part has its own Demo link, it overrides "
            "the parent Batch Demo link.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:part:{pid}"
                )
            ]])
        )

        return True


    if cmd == "access_private_part":
        pid = int(x[2])

        context.user_data[
            "new_batch_folder_pending"
        ] = {
            "kind": "folder_access_private_part",
            "id": pid,
        }

        await q.edit_message_text(
            "🔐 <b>Part Private Group / Channel</b>\n\n"
            "Send any message link copied from inside "
            "the private group/channel.\n\n"
            "The worker sessions will automatically prepare "
            "the AI bot.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:part:{pid}"
                )
            ]])
        )

        return True


    if cmd == "combined_demo":
        fid = int(x[2])

        ensure_access_schema(path)

        f = folder(
            path,
            fid,
        )

        if not f:
            await show_folders(
                q,
                path,
            )
            return True

        context.user_data[
            "new_batch_folder_pending"
        ] = {
            "kind": "folder_combined_demo",
            "folder_id": fid,
        }

        current = str(
            f.get("combined_demo_link")
            or ""
        ).strip()

        await q.edit_message_text(
            "🎬 <b>Combined Folder Demo</b>\n\n"
            f"Folder: <b>{html.escape(str(f['name']))}</b>\n\n"
            "Send the demo link that should be used for "
            "batches inside this folder when their own "
            "specific Demo Link is not configured.\n\n"
            + (
                "Current demo: <code>"
                + html.escape(current)
                + "</code>\n\n"
                if current
                else ""
            )
            + "Send <code>clear</code> to remove it.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=
                        f"admin:nbf:folder:{fid}"
                )
            ]])
        )

        return True


    if cmd == "add_batch":
        fid = int(x[2])

        context.user_data["new_batch_folder_pending"] = {
            "kind": "add_batch",
            "folder_id": fid
        }

        await q.edit_message_text(
            "➕ <b>Add Folder Batch</b>\n\n"
            "Send the batch name.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:folder:{fid}"
                )
            ]])
        )
        return True

    if cmd == "add_part":
        bid = int(x[2])

        context.user_data["new_batch_folder_pending"] = {
            "kind": "add_part",
            "batch_id": bid
        }

        await q.edit_message_text(
            "➕ <b>Add Part</b>\n\n"
            "Send the part name.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:batch:{bid}"
                )
            ]])
        )
        return True

    if cmd == "bf":
        bid = int(x[2])
        field = x[3]

        context.user_data["new_batch_folder_pending"] = {
            "kind": "batch_field",
            "id": bid,
            "field": field
        }

        await q.edit_message_text(
            "✏️ Send new "
            f'<b>{html.escape(field.replace("_"," ").title())}</b>.\n\n'
            "Send <code>clear</code> to empty it.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:batch:{bid}"
                )
            ]])
        )
        return True

    if cmd == "pf":
        pid = int(x[2])
        field = x[3]

        context.user_data["new_batch_folder_pending"] = {
            "kind": "part_field",
            "id": pid,
            "field": field
        }

        await q.edit_message_text(
            "✏️ Send new "
            f'<b>{html.escape(field.replace("_"," ").title())}</b>.\n\n'
            "Send <code>clear</code> to empty it.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nbf:part:{pid}"
                )
            ]])
        )
        return True

    if cmd == "toggle_batch":
        bid = int(x[2])
        b = batch(path, bid)

        set_batch_field(
            path,
            bid,
            "availability",
            0 if b["availability"] else 1
        )

        await show_batch(q, path, bid)
        return True

    if cmd == "toggle_part":
        pid = int(x[2])
        p = part(path, pid)

        set_part_field(
            path,
            pid,
            "availability",
            0 if p["availability"] else 1
        )

        await show_part(q, path, pid)
        return True

    if cmd == "toggle_standalone":
        pid = int(x[2])
        p = part(path, pid)

        set_part_field(
            path,
            pid,
            "standalone",
            0 if p["standalone"] else 1
        )

        await show_part(q, path, pid)
        return True

    return True


# ============================================================
# ADMIN MESSAGE HANDLER
# ============================================================

async def handle_message(update, context, path):
    state = context.user_data.get(
        "new_batch_folder_pending"
    )

    if not state:
        return False

    if not getattr(update.message, "text", None):
        return False

    raw = update.message.text.strip()

    if not raw:
        return True

    value = (
        ""
        if raw.casefold() in {
            "clear",
            "skip",
            "none",
            "-"
        }
        else raw
    )

    kind = state.get("kind")

    if kind in (
        "folder_access_demo_batch",
        "folder_access_demo_part",
    ):
        item_id = int(
            state["id"]
        )

        scope = (
            "batch"
            if kind.endswith("_batch")
            else "part"
        )

        _set_folder_access(
            path,
            scope,
            item_id,
            "demo_link",
            value,
        )

        context.user_data.pop(
            "new_batch_folder_pending",
            None,
        )

        await update.message.reply_text(
            "✅ Demo Link saved."
            if value
            else "✅ Demo Link cleared.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=(
                        f"admin:nbf:batch:{item_id}"
                        if scope == "batch"
                        else f"admin:nbf:part:{item_id}"
                    )
                )
            ]])
        )

        return True


    if kind in (
        "folder_access_private_batch",
        "folder_access_private_part",
    ):
        item_id = int(
            state["id"]
        )

        scope = (
            "batch"
            if kind.endswith("_batch")
            else "part"
        )

        if not value:
            parsed = {
                "message_link": "",
                "chat_id": None,
                "title": "Cleared",
            }

        else:
            parsed = await validate_private_target(
                context.bot,
                raw,
            )

        _set_folder_access(
            path,
            scope,
            item_id,
            "private_message_link",
            parsed["message_link"],
        )

        _set_folder_access(
            path,
            scope,
            item_id,
            "private_chat_id",
            parsed["chat_id"],
        )

        context.user_data.pop(
            "new_batch_folder_pending",
            None,
        )

        await update.message.reply_text(
            "✅ Private Group registered.\n\n"
            f"Target: {parsed['title']}\n"
            f"Chat ID: {parsed['chat_id'] or '—'}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=(
                        f"admin:nbf:batch:{item_id}"
                        if scope == "batch"
                        else f"admin:nbf:part:{item_id}"
                    )
                )
            ]])
        )

        return True


    if kind == "folder_combined_demo":
        fid = int(
            state["folder_id"]
        )

        ensure_access_schema(path)

        with _con(path) as db:
            db.execute("""
                UPDATE new_batch_folders
                SET
                    combined_demo_link=?,
                    updated_at=?
                WHERE id=?
            """, (
                value,
                int(time.time()),
                fid,
            ))

            db.commit()

        context.user_data.pop(
            "new_batch_folder_pending",
            None,
        )

        await update.message.reply_text(
            (
                "✅ Combined Demo saved."
                if value
                else "✅ Combined Demo cleared."
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Folder",
                    callback_data=
                        f"admin:nbf:folder:{fid}"
                )
            ]])
        )

        return True


    if kind == "add_batch":
        fid = int(state["folder_id"])
        bid = add_batch(path, fid, raw)

        context.user_data.pop(
            "new_batch_folder_pending",
            None
        )

        await update.message.reply_text(
            "✅ Batch added.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Open",
                    callback_data=f"admin:nbf:batch:{bid}"
                )
            ]])
        )
        return True

    if kind == "add_part":
        bid = int(state["batch_id"])
        pid = add_part(path, bid, raw)

        context.user_data.pop(
            "new_batch_folder_pending",
            None
        )

        await update.message.reply_text(
            "✅ Part added.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Open",
                    callback_data=f"admin:nbf:part:{pid}"
                )
            ]])
        )
        return True

    if kind == "batch_field":
        bid = int(state["id"])

        set_batch_field(
            path,
            bid,
            state["field"],
            value
        )

        context.user_data.pop(
            "new_batch_folder_pending",
            None
        )

        await update.message.reply_text(
            "✅ Updated.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Batch",
                    callback_data=f"admin:nbf:batch:{bid}"
                )
            ]])
        )
        return True

    if kind == "part_field":
        pid = int(state["id"])

        set_part_field(
            path,
            pid,
            state["field"],
            value
        )

        context.user_data.pop(
            "new_batch_folder_pending",
            None
        )

        await update.message.reply_text(
            "✅ Updated.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Part",
                    callback_data=f"admin:nbf:part:{pid}"
                )
            ]])
        )
        return True

    return False


# ============================================================
# CUSTOMER ROUTING
# ============================================================

# SOCIOLOGY_ROUTING_V4_1_FIXED
def _is_hinglish(text):
    n = _norm(text)

    # Hindi / Devanagari
    if re.search(r'[\u0900-\u097f]', str(text or "")):
        return True

    # Strong Hindi/Hinglish markers only.
    # Neutral English words such as "available",
    # and titles such as sir/mam do NOT make a message Hinglish.
    markers = {
        "hai", "hain", "h",
        "nahi", "nhi", "ni",
        "kya",
        "ka", "ki", "ke", "ko",
        "me", "mein",
        "chahiye", "chahie",
        "kitna", "kitne", "kitni",
        "konsa", "konsi",
        "kaunsa", "kaunsi",
        "kis",
        "dono",
        "batao",
        "milega", "milegi", "mile",
        "padhata", "padhate", "padhati",
    }

    return bool(
        set(n.split()) & markers
    )

def _batch_aliases(b):
    vals = [
        b.get("name", ""),
        b.get("teacher", ""),
    ]

    vals += [
        x.strip()
        for x in re.split(
            r"[,|;\n]+",
            str(b.get("aliases") or "")
        )
        if x.strip()
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


def _part_aliases(p):
    vals = [
        p.get("name", ""),
        p.get("teacher", ""),
    ]

    vals += [
        x.strip()
        for x in re.split(
            r"[,|;\n]+",
            str(p.get("aliases") or "")
        )
        if x.strip()
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


def _batch_match_score(text, b):
    """
    Higher score = more specific batch identity.

    Exact batch/alias phrases beat broad teacher aliases.
    This prevents e.g. Vision IAS Test Discussion from stealing
    ordinary Vision IAS Sociology questions.
    """
    n = _norm(text)

    best = 0

    batch_name = _norm(b.get("name"))

    if batch_name and _phrase(n, batch_name):
        best = max(
            best,
            100 + len(batch_name.split())
        )

    aliases = [
        _norm(x)
        for x in _batch_aliases(b)
        if _norm(x)
    ]

    for alias in aliases:

        if not _phrase(n, alias):
            continue

        words = len(alias.split())

        # Teacher-only aliases are useful, but less specific.
        if alias == _norm(b.get("teacher")):
            score = 40 + words
        else:
            score = 60 + words

        best = max(
            best,
            score
        )

    return best


def _batch_match(text, b):
    return _batch_match_score(text, b) > 0


def _part_match(text, p):
    n = _norm(text)

    return any(
        _phrase(n, a)
        for a in _part_aliases(p)
        if len(a) >= 2
    )


# SOCIOLOGY_ROUTING_V4
def _dimension(text):
    n = _norm(text)

    if (
        "available" in n
        or "do you have" in n
        or "hai kya" in n
        or "milega" in n
        or "milegi" in n
    ):
        return "availability"

    if (
        "price" in n
        or "fee" in n
        or "cost" in n
        or "how much" in n
        or "kitne ka" in n
        or "kitna ka" in n
        or "kitni ki" in n
    ):
        return "price"

    if (
        "year" in n
        or "kis year" in n
        or "which year" in n
    ):
        return "year"

    if (
        "teacher" in n
        or "faculty" in n
        or "who teaches" in n
        or "kaun padhata" in n
        or "kaun padhate" in n
    ):
        return "teacher"

    if (
        "institute" in n
        or "where does" in n
        or "kis coaching" in n
        or "kis institute" in n
    ):
        return "institute"

    return "details"


def _available_reply(text):
    if _is_hinglish(text):
        return "Haan bro, available hai."

    return "Yes bro, available."


def _batch_line(b):
    line = str(b["name"])

    if b.get("year"):
        line += " — " + str(b["year"])

    if b.get("price"):
        line += " — " + _money(b["price"])

    elif b.get("combined_price"):
        line += " — " + _money(b["combined_price"])

    return line


def _batch_overview(b, ps):
    lines = [
        f'<b>{html.escape(b["name"])}</b>'
    ]

    if b.get("institute"):
        lines.append(
            html.escape(b["institute"])
        )

    if b.get("teacher"):
        lines.append(
            html.escape(b["teacher"])
        )

    if b.get("year"):
        lines.append(
            "Year: "
            + html.escape(str(b["year"]))
        )

    if b.get("price"):
        lines.append(
            "Price: "
            + html.escape(_money(b["price"]))
        )

    if ps:
        lines.append("")

        for p in ps:
            x = "• " + p["name"]

            if p.get("teacher"):
                x += " — " + p["teacher"]

            if p.get("years"):
                x += " — " + p["years"]

            if p.get("price"):
                x += " — " + _money(p["price"])

            if not p.get("standalone"):
                x += " — Included"

            lines.append(
                html.escape(x)
            )

    if b.get("combined_price"):
        lines += [
            "",
            "<b>Both — "
            + html.escape(_money(b["combined_price"]))
            + "</b>"
        ]

    return "\n".join(lines)


def customer_route(path, text):
    """
    Folder-first customer router.

    Explicit folder/subject aliases select only that folder.
    They never directly select a faculty.
    """

    ensure_schema(path)

    n = _norm(text)

    if not n:
        return {
            "handled": False,
            "reply": ""
        }

    # OPTIONAL_FOLDER_ROUTER_V1
    #
    # Resolve explicit folder aliases from DB instead of hard-coding
    # Sociology. This keeps Optional vs GS boundaries deterministic.
    with _con(path) as _db:
        _folder_rows = [
            dict(r)
            for r in _db.execute("""
                SELECT *
                FROM new_batch_folders
                WHERE enabled=1
                ORDER BY id
            """).fetchall()
        ]

    def _folder_alias_values(_f):
        _vals = [
            _f.get("name", ""),
            _f.get("slug", ""),
        ]

        _vals += [
            x.strip()
            for x in str(
                _f.get("aliases", "") or ""
            ).split(",")
            if x.strip()
        ]

        return [
            _norm(x)
            for x in _vals
            if _norm(x)
        ]

    def _phrase_hit(_haystack, _phrase):
        if not _phrase:
            return False

        return bool(
            re.search(
                r"(?<!\w)"
                + re.escape(_phrase)
                + r"(?!\w)",
                _haystack,
                re.IGNORECASE,
            )
        )

    _folder_hits = []

    # OPTIONAL_SUBJECT_BOUNDARY_FIX_V1
    #
    # Dual-use subjects require explicit "optional":
    # Geography / History / Economy-Economics.
    #
    # Optional-only subjects can resolve without the word optional:
    # Anthropology, Sociology, PSIR, Philosophy, Psychology,
    # Agriculture, Maths/Mathematics, Public Administration,
    # Commerce, Chemistry.
    _has_optional_word = bool(
        re.search(
            r"(?<!\\w)optional(?!\\w)",
            n,
            re.IGNORECASE,
        )
    )

    _dual_optional_slugs = {
        "geography-optional",
        "history-optional",
        "economics-optional",
        "economy-optional",
    }

    for _f in _folder_rows:
        _slug = _norm(
            str(_f.get("slug") or "")
        ).replace(" ", "-")

        # Hard GS/Optional boundary.
        if (
            _slug in _dual_optional_slugs
            and not _has_optional_word
        ):
            continue

        _best = 0

        for _a in _folder_alias_values(_f):
            if _phrase_hit(n, _a):
                _best = max(
                    _best,
                    len(_a)
                )

        if _best:
            _folder_hits.append(
                (_best, _f)
            )

    _explicit_folder = None

    if _folder_hits:
        _folder_hits.sort(
            key=lambda x: x[0],
            reverse=True
        )

        _top_folder_score = _folder_hits[0][0]

        _top_folders = [
            _f
            for _score, _f in _folder_hits
            if _score == _top_folder_score
        ]

        if len(_top_folders) == 1:
            _explicit_folder = _top_folders[0]
        else:
            # Ambiguous folder request: never guess.
            return {
                "handled": False,
                "reply": ""
            }

    # Preserve the existing safety guard for unrelated folders/categories.
    if not _explicit_folder:
        _other_folder_request = any(
            phrase in n
            for phrase in (
                "current affairs",
                "current affair",
                "currentaffairs",
                "test series",
                "testseries",
            )
        )

        if _other_folder_request:
            return {
                "handled": False,
                "reply": ""
            }

    # OPTIONAL_GLOBAL_UNIQUE_BATCH_V1
    #
    # If no folder was explicitly selected, allow a unique saved
    # teacher/batch alias to identify its own Optional folder.
    # Never guess when multiple candidates tie.
    if not _explicit_folder:
        with _con(path) as _db:
            _all_batches = [
                dict(r)
                for r in _db.execute("""
                    SELECT
                        b.*,
                        f.id AS _folder_id,
                        f.name AS _folder_name,
                        f.slug AS _folder_slug
                    FROM new_batch_folder_batches b
                    JOIN new_batch_folders f
                        ON f.id=b.folder_id
                    WHERE b.enabled=1
                      AND f.enabled=1
                    ORDER BY b.id
                """).fetchall()
            ]

        _global_scored = [
            (_batch_match_score(text, _b), _b)
            for _b in _all_batches
        ]

        _global_scored = [
            (_score, _b)
            for _score, _b in _global_scored
            if _score > 0
        ]

        if _global_scored:
            _global_scored.sort(
                key=lambda x: x[0],
                reverse=True
            )

            _global_top_score = _global_scored[0][0]

            _global_top = [
                _b
                for _score, _b in _global_scored
                if _score == _global_top_score
            ]

            if len(_global_top) == 1:
                _winner = _global_top[0]

                _explicit_folder = {
                    "id": _winner["_folder_id"],
                    "name": _winner["_folder_name"],
                    "slug": _winner["_folder_slug"],
                }

    # Existing Sociology behaviour remains the fallback when no new
    # explicit Optional folder is present.
    target_folder = _explicit_folder

    if not target_folder:
        target_folder = folder_by_slug(
            path,
            "sociology"
        )

    if not target_folder:
        return {
            "handled": False,
            "reply": ""
        }

    rows = [
        b
        for b in batches(
            path,
            target_folder["id"]
        )
        if b.get("enabled")
    ]

    # Folder-only request:
    # History Optional -> list History Optional choices
    # PSIR -> list PSIR choices
    # Anthropology -> list Anthropology choices
    #
    # OPTIONAL_ONLY_PLAIN_SUBJECT_FIX_V2
    #
    # A folder alias by itself, plus harmless conversational
    # filler, is a catalogue request. This is checked BEFORE
    # batch scoring so PSIR does not tie every PSIR batch.

    _folder_alias_norms = (
        _folder_alias_values(_explicit_folder)
        if _explicit_folder
        else []
    )

    _generic_folder = False

    if _explicit_folder:
        _generic_fillers = {
            "optional",
            "batch",
            "batches",
            "course",
            "courses",
            "lecture",
            "lectures",
            "class",
            "classes",
            "chahiye",
            "chaiye",
            "chahie",
            "chahiy",
            "wala",
            "wali",
            "wale",
            "ka",
            "ki",
            "ke",
            "hai",
            "h",
            "kya",
            "which",
            "show",
            "list",
            "available",
            "availability",
            "price",
            "fees",
            "fee",
            "cost",
            "need",
            "want",
            "i",
            "me",
            "mujhe",
            "batao",
            "bata",
            "bro",
            "bhai",
            "bhaiya",
            "bhaiyaa",
            "brother",
            "do",
            "does",
            "you",
            "have",
            "got",
            "please",
            "pls",
            "plz",
            "tell",
            "give",
            "send",
            "show",
            "where",
            "kaha",
            "kahaan",
            "kahan",
            "milega",
            "milegi",
            "milenga",
            "milta",
            "milti",
            "hai",
            "hain",
            "hoga",
            "available",
            "availability",
            "course",
            "courses",
            "batch",
            "batches",
            "chahiye",
            "chaiye",
            "chahie",
            "chahiyeh",
            "optional",
            "ya",
            "nahi",
            "nahin",
            "nhi",
            "dikhao",
            "dikha",
            "dikhaiye",
            "dikhaao",
            "show",
            "list",
            "milega",
            "milegi",
            "milenge",
            "milta",
            "milti",
            "kya",
        }

        for _fa in sorted(
            _folder_alias_norms,
            key=len,
            reverse=True
        ):
            if not _phrase_hit(n, _fa):
                continue

            _remaining = re.sub(
                r"(?<!\w)"
                + re.escape(_fa)
                + r"(?!\w)",
                " ",
                n,
                count=1,
                flags=re.IGNORECASE,
            )

            _remaining_tokens = {
                x
                for x in re.findall(
                    r"[a-z0-9]+",
                    _remaining
                )
                if x
            }

            if (
                not _remaining_tokens
                or _remaining_tokens.issubset(
                    _generic_fillers
                )
            ):
                _generic_folder = True
                break

    # Preserve old generic Sociology catalogue handling.
    if (
        not _explicit_folder
        and (
            "sociology" in n
            or "socio" in n
        )
        and not any(
            _batch_match_score(
                text,
                b
            ) > 0
            for b in rows
        )
    ):
        _generic_folder = True

    if _generic_folder:
        lines = [
            "<b>"
            + html.escape(
                target_folder["name"]
            )
            + " Batches</b>",
            ""
        ]

        for b in rows:
            if not b.get("availability"):
                continue

            line = "• " + b["name"]

            # OPTIONAL_FOLDER_DISPLAY_V1
            # Show saved faculty/coaching details in catalogue.
            _teacher = str(
                b.get("teacher") or ""
            ).strip()

            _institute = str(
                b.get("institute") or ""
            ).strip()

            if _teacher:
                line += " — " + _teacher

            # Show institute when it adds useful information and is
            # not already obvious from the batch name.
            if (
                _institute
                and _norm(_institute)
                not in _norm(b["name"])
            ):
                line += " — " + _institute

            if b.get("price"):
                line += (
                    " — "
                    + _money(b["price"])
                )

            elif b.get("combined_price"):
                line += (
                    " — "
                    + _money(
                        b["combined_price"]
                    )
                )

            lines.append(
                html.escape(line)
            )

        return {
            "handled": True,
            "reply": "\n".join(lines)
        }

    scored_batches = [
        (
            _batch_match_score(text, b),
            b
        )
        for b in rows
    ]

    scored_batches = [
        (score, b)
        for score, b in scored_batches
        if score > 0
    ]

    if not scored_batches:
        # OPTIONAL_COACHING_FOLDER_CHOICES_V2
        #
        # Example:
        #   Sarthi IAS PSIR Optional
        #   Unacademy Anthropology Optional
        #   NEXT IAS History Optional
        #
        # Folder is already known. If the query clearly names a
        # coaching/institute that exists inside this folder, show
        # those matching batches instead of guessing one faculty.
        if target_folder:
            _coaching_matches = []

            for _b in rows:
                _inst = _norm(
                    str(_b.get("institute") or "")
                )

                if not _inst:
                    continue

                if _phrase_hit(n, _inst):
                    _coaching_matches.append(_b)

            if _coaching_matches:
                lines = [
                    "<b>"
                    + html.escape(target_folder["name"])
                    + " — available choices</b>",
                    ""
                ]

                for _b in _coaching_matches:
                    _line = "• " + str(
                        _b.get("name") or ""
                    )

                    if _b.get("teacher"):
                        _line += " — " + str(
                            _b["teacher"]
                        )

                    if _b.get("year"):
                        _line += " — " + str(
                            _b["year"]
                        )

                    if _b.get("price"):
                        _line += " — " + _money(
                            _b["price"]
                        )

                    elif _b.get("combined_price"):
                        _line += " — " + _money(
                            _b["combined_price"]
                        )

                    lines.append(
                        html.escape(_line)
                    )

                return {
                    "handled": True,
                    "reply": "\\n".join(lines)
                }

        return {
            "handled": False,
            "reply": ""
        }

    scored_batches.sort(
        key=lambda x: x[0],
        reverse=True
    )

    top_score = scored_batches[0][0]

    top_batches = [
        b
        for score, b in scored_batches
        if score == top_score
    ]

    if len(top_batches) != 1:
        # OPTIONAL_AMBIGUOUS_BATCH_CHOICES_V1
        #
        # Folder is already known, therefore ambiguity is safe to
        # expose as a list of valid choices instead of guessing.
        if target_folder and top_batches:
            lines = [
                "<b>"
                + html.escape(target_folder["name"])
                + " — available choices</b>",
                ""
            ]

            for _b in top_batches:
                _line = "• " + str(_b.get("name") or "")

                if _b.get("teacher"):
                    _line += " — " + str(_b["teacher"])

                if _b.get("year"):
                    _line += " — " + str(_b["year"])

                if _b.get("price"):
                    _line += " — " + _money(_b["price"])

                elif _b.get("combined_price"):
                    _line += " — " + _money(
                        _b["combined_price"]
                    )

                lines.append(
                    html.escape(_line)
                )

            return {
                "handled": True,
                "reply": "\n".join(lines)
            }

        return {
            "handled": False,
            "reply": ""
        }

    b = top_batches[0]

    if not b.get("availability"):
        return {
            "handled": True,
            "reply": (
                "Available nahi h bro."
                if _is_hinglish(text)
                else "Not available bro."
            )
        }

    ps = [
        p
        for p in parts(path, b["id"])
        if p.get("enabled")
    ]

    d = _dimension(text)

    # SHANKAR_PDF_DISPLAY_FIX_V1
    # Agriculture Optional: Shankar IAS is PDF-only.
    if b.get("slug") == "shankar-ias-agriculture-optional":
        if d == "price":
            return {
                "handled": True,
                "reply": "₹200 — Only PDF available."
            }

        if d == "availability":
            return {
                "handled": True,
                "reply": "Only PDF available — ₹200."
            }

        return {
            "handled": True,
            "reply": (
                "<b>Shankar IAS Agriculture Optional</b>\n"
                "Shankar IAS\n"
                "Only PDF available\n"
                "Price: ₹200"
            )
        }

    # --------------------------------------------------------
    # PW DUAL FACULTY PRICE FIX
    #
    # Sekhar and Nitin are separate ₹500 faculty batches.
    # Never collapse this into one ₹500 combined price.
    # --------------------------------------------------------
    _pw_sekhar = bool(
        re.search(
# PW_DUAL_PRICE_REGEX_FIX
            r"\b(?:sekhar|shekhar|shekar)\b",
            n
        )
    )

    _pw_nitin = bool(
        re.search(
            r"\bnitin\b",
            n
        )
    )

    if (
        b.get("slug") == "physics-wallah-sociology"
        and d == "price"
        and _pw_sekhar
        and _pw_nitin
    ):
        return {
            "handled": True,
            "reply": (
                "Sekhar Sir — ₹500\n"
                "Nitin Sir — ₹500"
            )
        }

    matched_parts = [
        p
        for p in ps
        if _part_match(text, p)
    ]

    # --------------------------------------------------------
    # Specific sub-part
    # --------------------------------------------------------
    if len(matched_parts) == 1:
        p = matched_parts[0]

        if not p.get("availability"):
            return {
                "handled": True,
                "reply": (
                    "Available nahi h bro."
                    if _is_hinglish(text)
                    else "Not available bro."
                )
            }

        if d == "price":
            if not p.get("standalone"):
                # Special Pranay Answer Writing / included parts.
                if "answer writing" in _norm(p["name"]):
                    return {
                        "handled": True,
                        "reply": (
                            "Answer Writing complete Pranay Agarwal "
                            "Sociology batch ke saath included hai bro."
                            if _is_hinglish(text)
                            else
                            "Answer Writing is included only with the "
                            "complete Pranay Agarwal Sociology batch."
                        )
                    }

                # Included sub-part with no independent price.
                return {
                    "handled": True,
                    "reply": (
                        html.escape(_money(b["price"]))
                        if b.get("price")
                        else ""
                    )
                }

            return {
                "handled": True,
                "reply": (
                    html.escape(_money(p["price"]))
                    if p.get("price")
                    else ""
                )
            }

        if d == "year":
            return {
                "handled": True,
                "reply": html.escape(
                    p.get("years")
                    or b.get("year")
                    or ""
                )
            }

        if d == "teacher":
            return {
                "handled": True,
                "reply": html.escape(
                    p.get("teacher")
                    or b.get("teacher")
                    or ""
                )
            }

        if d == "availability":
            return {
                "handled": True,
                "reply": _available_reply(text)
            }

        lines = [
            f'<b>{html.escape(b["name"])} — '
            f'{html.escape(p["name"])}</b>'
        ]

        if p.get("teacher"):
            lines.append(
                html.escape(p["teacher"])
            )

        if p.get("years"):
            lines.append(
                "Year: " + html.escape(p["years"])
            )

        if p.get("price") and p.get("standalone"):
            lines.append(
                "Price: " + html.escape(_money(p["price"]))
            )

        elif not p.get("standalone"):
            lines.append(
                "Included with complete batch."
            )

        return {
            "handled": True,
            "reply": "\n".join(lines)
        }

    # --------------------------------------------------------
    # Level Up BOTH / COMBINED
    # --------------------------------------------------------
    combined_words = bool(
        re.search(
            r"\b(both|dono|combined|together|"
            r"foundation.*crash|crash.*foundation)\b",
            n
        )
    )

    if (
        combined_words
        and b.get("combined_price")
        and d == "price"
    ):
        return {
            "handled": True,
            "reply": html.escape(
                _money(b["combined_price"])
            )
        }

    # Physics Wallah has two independent ₹500 faculty batches.
    # Asking for both must show both prices separately.
    if (
        b.get("slug") == "physics-wallah-sociology"
        and d == "price"
        and "sekhar" in n
        and "nitin" in n
    ):
        priced_parts = [
            p
            for p in ps
            if p.get("availability")
            and p.get("price")
        ]

        return {
            "handled": True,
            "reply": "\n".join(
                html.escape(
                    p["teacher"]
                    + " — "
                    + _money(p["price"])
                )
                for p in priced_parts
            )
        }

    # --------------------------------------------------------
    # Batch-level dimension
    # --------------------------------------------------------
    if d == "price":
        if b.get("price"):
            return {
                "handled": True,
                "reply": html.escape(
                    _money(b["price"])
                )
            }

        if b.get("combined_price"):
            return {
                "handled": True,
                "reply": html.escape(
                    _money(b["combined_price"])
                )
            }

        # PW: multiple independently priced teacher parts.
        priced_parts = [
            p
            for p in ps
            if p.get("price")
            and p.get("availability")
        ]

        if priced_parts:
            return {
                "handled": True,
                "reply": "\n".join(
                    html.escape(
                        p["name"]
                        + " — "
                        + _money(p["price"])
                    )
                    for p in priced_parts
                )
            }

        return {
            "handled": True,
            "reply": ""
        }

    if d == "year":
        if b.get("year"):
            return {
                "handled": True,
                "reply": html.escape(b["year"])
            }

        years = []

        for p in ps:
            for y in re.findall(
                r"20\d{2}",
                str(p.get("years") or "")
            ):
                if y not in years:
                    years.append(y)

        return {
            "handled": True,
            "reply": ", ".join(years)
        }

    if d == "teacher":
        if b.get("teacher"):
            return {
                "handled": True,
                "reply": html.escape(b["teacher"])
            }

        teachers = []

        for p in ps:
            if p.get("teacher") and p["teacher"] not in teachers:
                teachers.append(p["teacher"])

        return {
            "handled": True,
            "reply": html.escape(
                ", ".join(teachers)
            )
        }

    if d == "institute":
        return {
            "handled": True,
            "reply": html.escape(
                b.get("institute")
                or ""
            )
        }

    if d == "availability":
        return {
            "handled": True,
            "reply": _available_reply(text)
        }

    # --------------------------------------------------------
    # Special Answer Writing rule
    # --------------------------------------------------------
    if (
        b["slug"] == "pranay-agarwal"
        and "answer writing" in n
    ):
        return {
            "handled": True,
            "reply": (
                "Answer Writing complete Pranay Agarwal "
                "Sociology batch ke saath included hai bro."
                if _is_hinglish(text)
                else
                "Answer Writing is included only with the "
                "complete Pranay Agarwal Sociology batch."
            )
        }

    return {
        "handled": True,
        "reply": _batch_overview(
            b,
            ps
        )
    }
