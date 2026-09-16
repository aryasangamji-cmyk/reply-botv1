import html
import re
import sqlite3
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# GLOBAL_NEW_BATCH_MESSAGE_CONFIG_V1
from app.new_batch_access import (
    ensure_access_schema,
    validate_private_target,
    get_global_message,
    save_global_message,
    save_global_link_text,
    render_global_message_html,
)

# NEW_BATCH_ACCESS_FINAL_V1
from app.new_batch_access import (
    ensure_access_schema,
    validate_private_target,
)


# ============================================================
# STRUCTURED NEW BATCHES V1
# ============================================================

def _con(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _norm(v):
    v = str(v or "").casefold().replace("_", " ")
    v = re.sub(r"[^\w\s]+", " ", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()


def _money(v):
    v = str(v or "").strip()
    if not v:
        return "—"
    return v if v.startswith("₹") else "₹" + v


def ensure_schema(path):
    with _con(path) as con:

        con.execute("""
        CREATE TABLE IF NOT EXISTS structured_batch_teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            institute TEXT NOT NULL DEFAULT '',
            aliases TEXT NOT NULL DEFAULT '',
            keywords TEXT NOT NULL DEFAULT '',
            all_subjects_price TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS structured_batch_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER NOT NULL,

            name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',

            gs_paper TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',

            part_name TEXT NOT NULL DEFAULT '',
            batch_code TEXT NOT NULL DEFAULT '',

            year TEXT NOT NULL DEFAULT '',
            price TEXT NOT NULL DEFAULT '',

            availability INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT '',

            sort_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,

            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY(teacher_id)
                REFERENCES structured_batch_teachers(id)
                ON DELETE CASCADE
        )
        """)

        con.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_structured_subject_teacher
        ON structured_batch_subjects(teacher_id,sort_order,id)
        """)

        con.commit()


def _upsert_teacher(
    con,
    slug,
    name,
    institute="",
    aliases="",
    keywords="",
    all_price=""
):
    now = int(time.time())

    con.execute("""
    INSERT INTO structured_batch_teachers
    (
        slug,name,institute,aliases,keywords,
        all_subjects_price,enabled,created_at,updated_at
    )
    VALUES(?,?,?,?,?,?,1,?,?)

    ON CONFLICT(slug) DO UPDATE SET
        name=excluded.name,
        institute=excluded.institute,
        aliases=excluded.aliases,
        keywords=excluded.keywords,
        all_subjects_price=excluded.all_subjects_price,
        enabled=1,
        updated_at=excluded.updated_at
    """, (
        slug,
        name,
        institute,
        aliases,
        keywords,
        all_price,
        now,
        now
    ))

    return int(
        con.execute(
            "SELECT id FROM structured_batch_teachers WHERE slug=?",
            (slug,)
        ).fetchone()[0]
    )


def _replace_subjects(con, teacher_id, rows):

    con.execute(
        "DELETE FROM structured_batch_subjects WHERE teacher_id=?",
        (teacher_id,)
    )

    now = int(time.time())

    for order, r in enumerate(rows, 1):

        con.execute("""
        INSERT INTO structured_batch_subjects
        (
            teacher_id,
            name,
            aliases,
            gs_paper,
            category,
            part_name,
            batch_code,
            year,
            price,
            availability,
            notes,
            sort_order,
            enabled,
            created_at,
            updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
        """, (
            teacher_id,
            r.get("name", ""),
            r.get("aliases", ""),
            r.get("gs_paper", ""),
            r.get("category", ""),
            r.get("part_name", ""),
            r.get("batch_code", ""),
            r.get("year", ""),
            r.get("price", ""),
            int(bool(r.get("availability", 1))),
            r.get("notes", ""),
            order,
            now,
            now
        ))


# ============================================================
# YOUR CURRENT 8 TEACHER / BATCH GROUPS
# ============================================================

def seed_initial_data(path):

    ensure_schema(path)

    rows = [

        # ----------------------------------------------------
        # 1. SUDARSHAN GURJAR
        # ----------------------------------------------------
        (
            "sudarshan-gurjar",
            "Sudarshan Gurjar Sir",
            "Unacademy",

            "Sudarshan Gujar,"
            "Sudarshan Gurjar,"
            "Sudarshan Sir,"
            "Gurjar Sir,"
            "Gujar Sir",

            "sudarshan gurjar gujar sir unacademy "
            "geography environment disaster management "
            "gs1 gs3",

            "",

            [
                {
                    "name": "Geography",
                    "aliases": "geography,geo,geography batch",
                    "gs_paper": "GS1",
                    "year": "2027",
                    "price": "400",
                },
                {
                    "name": "Environment",
                    "aliases": "environment,env,environment batch",
                    "gs_paper": "GS3",
                    "year": "2027",
                    "price": "200",
                },
                {
                    "name": "Disaster Management",
                    "aliases":
                        "disaster management,disaster,dm,"
                        "disaster management batch",
                    "gs_paper": "GS3",
                    "year": "2027",
                    "price": "100",
                },
            ]
        ),

        # ----------------------------------------------------
        # 2. MRUNAL PATEL
        # ----------------------------------------------------
        (
            "mrunal-patel",
            "Mrunal Patel",
            "Unacademy",

            "Mrunal Sir,"
            "Mrunal Patel Sir,"
            "Mrunal",

            "mrunal patel sir unacademy economy "
            "pcb pcb16 pcb 16 prelims "
            "qep qep9 qep 9 mains gs3",

            "400",

            [
                {
                    "name": "Economy",
                    "aliases":
                        "economy prelims,prelims economy,"
                        "pcb,pcb 16,pcb16",
                    "gs_paper": "GS3",
                    "category": "GS",
                    "part_name": "Prelims",
                    "batch_code": "PCB 16",
                    "year": "2027",
                    "price": "300",
                },
                {
                    "name": "Economy",
                    "aliases":
                        "economy mains,mains economy,"
                        "qep,qep 9,qep9",
                    "gs_paper": "GS3",
                    "category": "GS",
                    "part_name": "Mains",
                    "batch_code": "QEP 9",
                    "year": "2027",
                    "price": "200",
                },
            ]
        ),

        # ----------------------------------------------------
        # 3. JATIN GUPTA
        # ----------------------------------------------------
        (
            "jatin-gupta",
            "Jatin Gupta",
            "",

            "Jatin Gupta Sir,"
            "Jatin Sir",

            "jatin gupta sir polity internal security "
            "governance gs2 gs3",

            "",

            [
                {
                    "name": "Polity",
                    "aliases": "polity,polity batch,gs2",
                    "gs_paper": "GS2",
                    "year": "2026",
                    "price": "300",
                },
                {
                    "name": "Internal Security",
                    "aliases":
                        "internal security,security,"
                        "internal security batch,gs3",
                    "gs_paper": "GS3",
                    "year": "2026",
                    "price": "200",
                },
                {
                    "name": "Governance",
                    "aliases": "governance,governance batch,gs2 governance",
                    "gs_paper": "GS2",
                    "availability": 0,
                    "notes": "Lectures not available",
                },
            ]
        ),

        # ----------------------------------------------------
        # 4. SHIVIN / SIVIN SIR
        # ----------------------------------------------------
        (
            "shivin-sir",
            "Shivin Sir",
            "",

            "Sivin Sir,"
            "Shivin Sir,"
            "Shivin,"
            "Sivin",

            "shivin sivin sir economy foundation "
            "gs3 module write smart answer writing "
            "science technology environment prelims",

            "",

            [
                {
                    "name": "Economy Foundation",
                    "aliases":
                        "economy,economy foundation,"
                        "economy foundation batch",
                    "gs_paper": "GS3",
                    "year": "2026",
                    "price": "200",
                },
                {
                    "name": "GS3 Module",
                    "aliases": "gs3,gs3 module,gs 3 module",
                    "gs_paper": "GS3",
                    "year": "2027",
                    "price": "200",
                },
                {
                    "name": "Write Smart",
                    "aliases":
                        "write smart,answer writing,"
                        "answer writing batch",
                    "category": "Answer Writing",
                    "year": "2027",
                    "price": "200",
                },
                {
                    "name": "Science & Technology",
                    "aliases":
                        "science technology,"
                        "science and technology,"
                        "sci tech,"
                        "prelims science technology",
                    "gs_paper": "GS3",
                    "category": "Prelims",
                    "year": "2026",
                    "price": "150",
                },
                {
                    "name": "Environment",
                    "aliases":
                        "environment,env,"
                        "prelims environment",
                    "gs_paper": "GS3",
                    "category": "Prelims",
                    "year": "2026",
                    "price": "150",
                },
            ]
        ),

        # ----------------------------------------------------
        # 5. AYUSHI MA'AM
        # ----------------------------------------------------
        (
            "ayushi-mam",
            "Ayushi Ma'am",
            "",

            "Ayushi Mam,"
            "Ayushi Maam,"
            "Ayushi Madam",

            "ayushi mam maam "
            "psir political science international relations "
            "optional ir international relation gs2",

            "",

            [
                {
                    "name": "PSIR",
                    "aliases":
                        "psir,"
                        "political science optional,"
                        "political science and international relations optional",
                    "category": "Optional",
                    "year": "2025",
                    "price": "500",
                },
                {
                    "name": "IR",
                    "aliases":
                        "ir,"
                        "international relation,"
                        "international relations",
                    "gs_paper": "GS2",
                    "category": "GS",
                    "year": "2026",
                    "price": "300",
                },
            ]
        ),

        # ----------------------------------------------------
        # 6. SARMAD MEHRAJ
        # ----------------------------------------------------
        (
            "sarmad-mehraj",
            "Sarmad Mehraj",
            "Unacademy",

            "Sarmad Sir,"
            "Sarmad Mehraj Sir",

            "sarmad mehraj sir unacademy "
            "polity governance gs2",

            "",

            [
                {
                    "name": "Polity",
                    "aliases": "polity,polity batch,gs2",
                    "gs_paper": "GS2",
                    "year": "2027",
                    "price": "400",
                },
                {
                    "name": "Governance",
                    "aliases":
                        "governance,"
                        "governance batch,"
                        "gs2 governance",
                    "gs_paper": "GS2",
                    "availability": 0,
                    "notes": "Lectures not available",
                },
            ]
        ),

        # ----------------------------------------------------
        # 7. BASAVA OPPIN
        # ----------------------------------------------------
        (
            "basava-oppin",
            "Basava Oppin Sir",
            "Forum IAS",

            "Basava Oppin,"
            "Basava Uppin,"
            "Bassava Uppin,"
            "Basava Sir,"
            "Bassava Sir",

            "basava oppin uppin bassava "
            "forum ias economy gs3",

            "",

            [
                {
                    "name": "Economy",
                    "aliases": "economy,economy batch,gs3 economy",
                    "gs_paper": "GS3",
                    "category": "GS",
                    "year": "2026",
                    "price": "300",
                },
            ]
        ),

        # ----------------------------------------------------
        # 8. SMRITI MA'AM / SMRITI SHAH
        # ----------------------------------------------------
        (
            "smriti-mam",
            "Smriti Ma'am",
            "Vision IAS",

            "Smriti Mam,"
            "Smriti Maam,"
            "Smriti Shah,"
            "Smriti Shah Mam,"
            "Smriti Shah Maam",

            "smriti mam maam shah vision ias "
            "society social justice social issues "
            "ethics essay sociology paper 2 "
            "gs1 gs2 gs4 optional",

            "",

            [
                {
                    "name": "Society",
                    "aliases":
                        "society,"
                        "social justice,"
                        "social issues",
                    "gs_paper": "GS1,GS2",
                    "category": "GS",
                    "year": "2025",
                    "price": "300",
                    "notes":
                        "Social Justice and Social Issues "
                        "are included under Society",
                },
                {
                    "name": "Ethics",
                    "aliases": "ethics,gs4",
                    "gs_paper": "GS4",
                    "category": "GS",
                    "year": "2026",
                    "price": "250",
                },
                {
                    "name": "Essay",
                    "aliases": "essay,essay batch",
                    "category": "Essay",
                    "year": "2026",
                    "price": "300",
                },
                {
                    "name": "Sociology",
                    "aliases":
                        "sociology,"
                        "sociology paper 2,"
                        "paper 2",
                    "category": "Optional",
                    "part_name": "Paper 2",
                    "year": "2026",
                    "price": "500",
                },
            ]
        ),
    ]

    with _con(path) as con:

        for (
            slug,
            name,
            institute,
            aliases,
            keywords,
            all_price,
            subject_rows
        ) in rows:

            tid = _upsert_teacher(
                con,
                slug,
                name,
                institute,
                aliases,
                keywords,
                all_price
            )

            _replace_subjects(
                con,
                tid,
                subject_rows
            )

        con.commit()


# ============================================================
# DATABASE GETTERS
# ============================================================

def teachers(path):
    ensure_schema(path)
    ensure_access_schema(path)
    ensure_access_schema(path)

    with _con(path) as con:
        return [
            dict(r)
            for r in con.execute("""
                SELECT *
                FROM structured_batch_teachers
                ORDER BY name COLLATE NOCASE
            """).fetchall()
        ]


def teacher(path, tid):

    with _con(path) as con:

        r = con.execute("""
            SELECT *
            FROM structured_batch_teachers
            WHERE id=?
        """, (int(tid),)).fetchone()

        return dict(r) if r else None


def subjects(path, tid, enabled_only=False):

    with _con(path) as con:

        sql = """
            SELECT *
            FROM structured_batch_subjects
            WHERE teacher_id=?
        """

        args = [int(tid)]

        if enabled_only:
            sql += " AND enabled=1"

        sql += " ORDER BY sort_order,id"

        return [
            dict(r)
            for r in con.execute(sql, args).fetchall()
        ]


def subject(path, sid):

    with _con(path) as con:

        r = con.execute("""
            SELECT *
            FROM structured_batch_subjects
            WHERE id=?
        """, (int(sid),)).fetchone()

        return dict(r) if r else None


# ============================================================
# EDITING
# ============================================================

def _set_teacher(path, tid, field, value):

    allowed = {
        "name",
        "institute",
        "aliases",
        "keywords",
        "all_subjects_price",
        "enabled",
        "demo_link",
        "private_message_link",
        "private_chat_id",
        "demo_link",
        "private_message_link",
        "private_chat_id",
        "normal_message",
        "normal_link_text",
        "demo_message",
        "demo_link_text",
        "pre_message",
        "pre_link_text",
    }

    if field not in allowed:
        raise ValueError("Invalid teacher field")

    with _con(path) as con:

        con.execute(
            f"""
            UPDATE structured_batch_teachers
            SET {field}=?,updated_at=?
            WHERE id=?
            """,
            (
                value,
                int(time.time()),
                int(tid)
            )
        )

        con.commit()


def _set_subject(path, sid, field, value):

    allowed = {
        "name",
        "aliases",
        "gs_paper",
        "category",
        "part_name",
        "batch_code",
        "year",
        "price",
        "availability",
        "enabled",
        "notes",
        "demo_link",
        "private_message_link",
        "private_chat_id",
        "demo_link",
        "private_message_link",
        "private_chat_id",
        "normal_message",
        "normal_link_text",
        "demo_message",
        "demo_link_text",
        "pre_message",
        "pre_link_text",
    }

    if field not in allowed:
        raise ValueError("Invalid subject field")

    with _con(path) as con:

        con.execute(
            f"""
            UPDATE structured_batch_subjects
            SET {field}=?,updated_at=?
            WHERE id=?
            """,
            (
                value,
                int(time.time()),
                int(sid)
            )
        )

        con.commit()


def _add_teacher(path, name):

    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        _norm(name)
    ).strip("-")

    if not slug:
        slug = f"teacher-{int(time.time())}"

    base = slug

    with _con(path) as con:

        i = 2

        while con.execute(
            "SELECT 1 FROM structured_batch_teachers WHERE slug=?",
            (slug,)
        ).fetchone():

            slug = f"{base}-{i}"
            i += 1

        return _upsert_teacher(
            con,
            slug,
            name,
            keywords=_norm(name)
        )


def _add_subject(path, tid, name):

    now = int(time.time())

    with _con(path) as con:

        order = int(
            con.execute("""
                SELECT COALESCE(MAX(sort_order),0)+1
                FROM structured_batch_subjects
                WHERE teacher_id=?
            """, (int(tid),)).fetchone()[0]
        )

        cur = con.execute("""
            INSERT INTO structured_batch_subjects
            (
                teacher_id,
                name,
                aliases,
                sort_order,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?)
        """, (
            int(tid),
            name,
            _norm(name),
            order,
            now,
            now
        ))

        con.commit()

        return int(cur.lastrowid)


def _delete_teacher(path, tid):

    with _con(path) as con:

        con.execute(
            "DELETE FROM structured_batch_teachers WHERE id=?",
            (int(tid),)
        )

        con.commit()


def _delete_subject(path, sid):

    with _con(path) as con:

        con.execute(
            "DELETE FROM structured_batch_subjects WHERE id=?",
            (int(sid),)
        )

        con.commit()


# ============================================================
# ADMIN UI
# ============================================================

def menu_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "➕ Add Teacher / Batch Group",
                callback_data="admin:nb:add_teacher"
            )
        ],

        [
            InlineKeyboardButton(
                "👨‍🏫 Teachers / Batch Groups",
                callback_data="admin:nb:list"
            )
        ],

        [
            InlineKeyboardButton(
                "✉️ Message Configuration",
                callback_data="admin:nb:msgcfg"
            )
        ],

        # BATCH_FOLDERS_V1_MENU
        [
            InlineKeyboardButton(
                "📁 Folders",
                callback_data="admin:nbfolders"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ Admin Home",
                callback_data="admin:home"
            )
        ],
    ])


async def show_menu(q, path):

    rows = teachers(path)

    total_subjects = sum(
        len(subjects(path, r["id"]))
        for r in rows
    )

    await q.edit_message_text(
        "🆕 <b>New Batches</b>\n\n"
        f"Teacher / batch groups: <b>{len(rows)}</b>\n"
        f"Subjects / parts: <b>{total_subjects}</b>\n\n"
        "• Subject-wise Year\n"
        "• Subject-wise Price\n"
        "• GS Paper mapping\n"
        "• Teacher-level combined price\n"
        "• Local aliases / keywords\n"
        "• Available / Not Available status",
        parse_mode="HTML",
        reply_markup=menu_keyboard()
    )


async def show_list(q, path):

    rows = teachers(path)

    kb = []

    for r in rows:

        kb.append([
            InlineKeyboardButton(
                ("✅ " if r["enabled"] else "⛔ ") + r["name"],
                callback_data=f'admin:nb:teacher:{r["id"]}'
            )
        ])

    kb += [

        [
            InlineKeyboardButton(
                "➕ Add Teacher / Batch Group",
                callback_data="admin:nb:add_teacher"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ New Batches",
                callback_data="admin:new_batches"
            )
        ],
    ]

    await q.edit_message_text(
        "👨‍🏫 <b>New Batch Groups</b>\n\n"
        "Choose a teacher / group:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def show_teacher(q, path, tid):

    t = teacher(path, tid)

    if not t:
        await show_list(q, path)
        return

    ss = subjects(path, tid)

    lines = []

    for s in ss:

        status = "✅" if s["availability"] else "❌"

        label = s["name"]

        if s.get("part_name"):
            label += f' — {s["part_name"]}'

        if s.get("batch_code"):
            label += f' ({s["batch_code"]})'

        lines.append(
            f'{status} {html.escape(label)}'
            f' — {_money(s.get("price"))}'
            f' — {html.escape(s.get("year") or "—")}'
        )

    text = (
        f'👨‍🏫 <b>{html.escape(t["name"])}</b>\n\n'
        f'🏫 Institute: '
        f'<b>{html.escape(t.get("institute") or "—")}</b>\n'
        f'💰 All Subjects Price: '
        f'<b>{html.escape(_money(t.get("all_subjects_price")))}</b>\n'
        f'🔑 Aliases: '
        f'{html.escape(t.get("aliases") or "—")}\n\n'
        f'📚 <b>Subjects</b>\n'
        + ("\n".join(lines) if lines else "No subjects yet.")
    )

    kb = [

        [
            InlineKeyboardButton(
                "➕ Add Subject",
                callback_data=f"admin:nb:add_subject:{tid}"
            ),
            InlineKeyboardButton(
                "📚 Subjects",
                callback_data=f"admin:nb:subjects:{tid}"
            )
        ],

        [
            InlineKeyboardButton(
                "✏️ Name",
                callback_data=f"admin:nb:tf:{tid}:name"
            ),
            InlineKeyboardButton(
                "🏫 Institute",
                callback_data=f"admin:nb:tf:{tid}:institute"
            )
        ],

        [
            InlineKeyboardButton(
                "💰 All Subjects Price",
                callback_data=
                    f"admin:nb:tf:{tid}:all_subjects_price"
            )
        ],

        [
            InlineKeyboardButton(
                "🔑 Aliases",
                callback_data=f"admin:nb:tf:{tid}:aliases"
            ),
            InlineKeyboardButton(
                "🔎 Keywords",
                callback_data=f"admin:nb:tf:{tid}:keywords"
            )
        ],

        [
            InlineKeyboardButton(
                "🎬 Demo Link",
                callback_data=f"admin:nb:tf:{tid}:demo_link"
            ),
            InlineKeyboardButton(
                "🔐 Private Group",
                callback_data=f"admin:nb:private_t:{tid}"
            )
        ],

        [
            InlineKeyboardButton(
                "🔴 Disable" if t["enabled"] else "🟢 Enable",
                callback_data=f"admin:nb:toggle_teacher:{tid}"
            )
        ],

        [
            InlineKeyboardButton(
                "🗑 Delete Group",
                callback_data=f"admin:nb:delete_teacher:{tid}"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ New Batches",
                callback_data="admin:nb:list"
            )
        ],
    ]

    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def show_subjects(q, path, tid):

    t = teacher(path, tid)
    ss = subjects(path, tid)

    if not t:
        await show_list(q, path)
        return

    kb = []

    for s in ss:

        label = (
            ("✅ " if s["availability"] else "❌ ")
            + s["name"]
        )

        if s.get("part_name"):
            label += f' — {s["part_name"]}'

        if s.get("batch_code"):
            label += f' ({s["batch_code"]})'

        kb.append([
            InlineKeyboardButton(
                label,
                callback_data=f'admin:nb:subject:{s["id"]}'
            )
        ])

    kb += [

        [
            InlineKeyboardButton(
                "➕ Add Subject",
                callback_data=f"admin:nb:add_subject:{tid}"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ " + t["name"],
                callback_data=f"admin:nb:teacher:{tid}"
            )
        ],
    ]

    await q.edit_message_text(
        f'📚 <b>{html.escape(t["name"])}</b> — Subjects',
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def show_subject(q, path, sid):

    s = subject(path, sid)

    if not s:
        await show_menu(q, path)
        return

    t = teacher(path, s["teacher_id"])

    def v(x):
        return (
            html.escape(str(x))
            if x not in (None, "")
            else "—"
        )

    text = (
        f'📘 <b>{v(t["name"])}</b>\n\n'
        f'<b>{v(s["name"])}</b>\n'
        f'📌 Part: {v(s.get("part_name"))}\n'
        f'🏷 Batch Code: {v(s.get("batch_code"))}\n'
        f'📄 Category: {v(s.get("category"))}\n'
        f'🧭 GS Paper: {v(s.get("gs_paper"))}\n'
        f'📅 Year: <b>{v(s.get("year"))}</b>\n'
        f'💰 Price: <b>{v(_money(s.get("price")))}</b>\n'
        f'📦 Status: '
        f'<b>{"Available ✅" if s.get("availability") else "Not Available ❌"}</b>\n'
        f'🔑 Aliases: {v(s.get("aliases"))}\n'
        f'📝 Notes: {v(s.get("notes"))}'
    )

    kb = [

        [
            InlineKeyboardButton(
                "✏️ Subject Name",
                callback_data=f"admin:nb:sf:{sid}:name"
            )
        ],

        [
            InlineKeyboardButton(
                "📅 Year",
                callback_data=f"admin:nb:sf:{sid}:year"
            ),
            InlineKeyboardButton(
                "💰 Price",
                callback_data=f"admin:nb:sf:{sid}:price"
            )
        ],

        [
            InlineKeyboardButton(
                "🧭 GS Paper",
                callback_data=f"admin:nb:sf:{sid}:gs_paper"
            ),
            InlineKeyboardButton(
                "📄 Category",
                callback_data=f"admin:nb:sf:{sid}:category"
            )
        ],

        [
            InlineKeyboardButton(
                "📌 Part",
                callback_data=f"admin:nb:sf:{sid}:part_name"
            ),
            InlineKeyboardButton(
                "🏷 Batch Code",
                callback_data=f"admin:nb:sf:{sid}:batch_code"
            )
        ],

        [
            InlineKeyboardButton(
                "🔑 Aliases",
                callback_data=f"admin:nb:sf:{sid}:aliases"
            ),
            InlineKeyboardButton(
                "📝 Notes",
                callback_data=f"admin:nb:sf:{sid}:notes"
            )
        ],

        [
            InlineKeyboardButton(
                "🎬 Demo Link",
                callback_data=f"admin:nb:sf:{sid}:demo_link"
            ),
            InlineKeyboardButton(
                "🔐 Private Group",
                callback_data=f"admin:nb:private_s:{sid}"
            )
        ],

        [
            InlineKeyboardButton(
                (
                    "❌ Mark Not Available"
                    if s["availability"]
                    else "✅ Mark Available"
                ),
                callback_data=f"admin:nb:toggle_avail:{sid}"
            )
        ],

        [
            InlineKeyboardButton(
                "🗑 Delete Subject",
                callback_data=f"admin:nb:delete_subject:{sid}"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ Subjects",
                callback_data=
                    f'admin:nb:subjects:{s["teacher_id"]}'
            )
        ],
    ]

    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# GLOBAL ACCESS MESSAGE CONFIGURATION
# ============================================================

async def show_global_access_message_menu(q, path):
    ensure_access_schema(path)

    text = (
        "✉️ <b>Global Message Configuration</b>\n\n"
        "These three messages are shared by every saved "
        "New Batch and every subject.\n\n"
        "Formatting is preserved:\n"
        "• Bold\n"
        "• Italic\n"
        "• Underline\n"
        "• Strikethrough\n"
        "• Spoiler / highlighted text\n"
        "• Code / preformatted text\n"
        "• Quote / expandable quote\n"
        "• Existing text links\n\n"
        "🔗 Link preview is always <b>OFF</b>."
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔐 Normal",
                callback_data="admin:nb:msgmode:normal"
            )
        ],
        [
            InlineKeyboardButton(
                "🎬 Demo",
                callback_data="admin:nb:msgmode:demo"
            )
        ],
        [
            InlineKeyboardButton(
                "🔓 PRE",
                callback_data="admin:nb:msgmode:pre"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ New Batches",
                callback_data="admin:new_batches"
            )
        ],
    ])

    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def show_global_access_message_mode(
    q,
    path,
    mode,
):
    row = get_global_message(
        path,
        mode
    )

    if not row:
        return

    message = str(
        row.get("message_text")
        or ""
    )

    link_text = str(
        row.get("link_text")
        or ""
    )

    try:
        preview = render_global_message_html(
            path,
            mode,
            "https://t.me/example"
        )
    except Exception as exc:
        preview = (
            "⚠️ "
            + html.escape(
                str(exc)
            )
        )

    text = (
        f"✉️ <b>{html.escape(mode.upper())} Message</b>\n\n"
        f"<b>Current link word/text:</b> "
        f"{html.escape(link_text or '—')}\n\n"
        f"<b>Preview:</b>\n{preview}\n\n"
        "The same message is used for every New Batch.\n"
        "Only the embedded destination link changes.\n\n"
        "Link preview: <b>OFF</b>"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ Set Formatted Message",
                callback_data=f"admin:nb:msgtext:{mode}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔗 Set Link Word / Text",
                callback_data=f"admin:nb:msglink:{mode}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Message Configuration",
                callback_data="admin:nb:msgcfg"
            )
        ],
    ])

    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )



# ============================================================
# ADMIN CALLBACKS
# ============================================================

async def handle_callback(q, context, path, action):

    ensure_schema(path)

    if action == "new_batches":
        await show_menu(q, path)
        return True

    if not action.startswith("nb:"):
        return False

    parts = action.split(":")
    cmd = parts[1]

    if cmd == "list":
        await show_list(q, path)
        return True

    if cmd == "add_teacher":

        context.user_data["new_batch_pending"] = {
            "kind": "add_teacher"
        }

        await q.edit_message_text(
            "➕ <b>Add Teacher / Batch Group</b>\n\n"
            "Send the teacher/group name.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="admin:new_batches"
                )
            ]])
        )

        return True

    if cmd == "teacher":

        await show_teacher(
            q,
            path,
            int(parts[2])
        )

        return True

    if cmd == "subjects":

        await show_subjects(
            q,
            path,
            int(parts[2])
        )

        return True

    if cmd == "subject":

        await show_subject(
            q,
            path,
            int(parts[2])
        )

        return True

    if cmd == "add_subject":

        tid = int(parts[2])

        context.user_data["new_batch_pending"] = {
            "kind": "add_subject",
            "teacher_id": tid
        }

        await q.edit_message_text(
            "➕ <b>Add Subject</b>\n\n"
            "Send the subject name.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"admin:nb:teacher:{tid}"
                )
            ]])
        )

        return True

    if cmd == "tf":

        tid = int(parts[2])
        field = parts[3]

        context.user_data["new_batch_pending"] = {
            "kind": "teacher_field",
            "id": tid,
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
                    callback_data=f"admin:nb:teacher:{tid}"
                )
            ]])
        )

        return True

    if cmd == "sf":

        sid = int(parts[2])
        field = parts[3]

        context.user_data["new_batch_pending"] = {
            "kind": "subject_field",
            "id": sid,
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
                    callback_data=f"admin:nb:subject:{sid}"
                )
            ]])
        )

        return True


    if cmd == "private_t":

        tid = int(parts[2])

        context.user_data[
            "new_batch_pending"
        ] = {
            "kind": "private_link_teacher",
            "id": tid,
        }

        await q.edit_message_text(
            "🔐 <b>Private Group</b>\n\n"
            "Send any private message link from the target "
            "group/channel.\n\n"
            "Example:\n"
            "<code>https://t.me/c/1234567890/55</code>\n\n"
            "The AI bot must already be admin there with:\n"
            "• Invite Users\n"
            "• Ban / Restrict Users",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=
                        f"admin:nb:teacher:{tid}"
                )
            ]])
        )

        return True


    if cmd == "private_s":

        sid = int(parts[2])

        context.user_data[
            "new_batch_pending"
        ] = {
            "kind": "private_link_subject",
            "id": sid,
        }

        await q.edit_message_text(
            "🔐 <b>Subject Private Group</b>\n\n"
            "Send any private message link from the target "
            "group/channel.\n\n"
            "Example:\n"
            "<code>https://t.me/c/1234567890/55</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=
                        f"admin:nb:subject:{sid}"
                )
            ]])
        )

        return True


    if cmd == "msgcfg":

        await show_global_access_message_menu(
            q,
            path
        )

        return True


    if cmd == "msgmode":

        mode = parts[2]

        if mode not in (
            "normal",
            "demo",
            "pre",
        ):
            return True

        await show_global_access_message_mode(
            q,
            path,
            mode,
        )

        return True


    if cmd == "msgtext":

        mode = parts[2]

        if mode not in (
            "normal",
            "demo",
            "pre",
        ):
            return True

        context.user_data[
            "new_batch_pending"
        ] = {
            "kind": "global_access_message",
            "mode": mode,
        }

        await q.edit_message_text(
            "✏️ <b>Send the formatted customer message now.</b>\n\n"
            "Format it directly in Telegram exactly as you want:\n"
            "bold, italic, underline, spoiler, quote, etc.\n\n"
            "After saving it, use <b>Set Link Word / Text</b> "
            "to choose the clickable phrase.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=
                        f"admin:nb:msgmode:{mode}"
                )
            ]])
        )

        return True


    if cmd == "msglink":

        mode = parts[2]

        if mode not in (
            "normal",
            "demo",
            "pre",
        ):
            return True

        context.user_data[
            "new_batch_pending"
        ] = {
            "kind": "global_access_link_text",
            "mode": mode,
        }

        row = get_global_message(
            path,
            mode
        )

        await q.edit_message_text(
            "🔗 <b>Set Embedded Link Word / Text</b>\n\n"
            "Send the exact word or phrase from the saved message "
            "that should become clickable.\n\n"
            "Current message:\n"
            + html.escape(
                str(
                    (row or {}).get(
                        "message_text"
                    )
                    or "—"
                )
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=
                        f"admin:nb:msgmode:{mode}"
                )
            ]])
        )

        return True


    if cmd == "private_t":

        tid = int(
            parts[2]
        )

        context.user_data[
            "new_batch_pending"
        ] = {
            "kind": "private_link_teacher",
            "id": tid,
        }

        await q.edit_message_text(
            "🔐 <b>Private Group</b>\n\n"
            "Send any message link copied from inside "
            "the target private group/channel.\n\n"
            "Example:\n"
            "<code>https://t.me/c/1234567890/55</code>\n\n"
            "The AI bot must already be admin there with:\n"
            "• Invite Users\n"
            "• Ban / Restrict Users",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=
                        f"admin:nb:teacher:{tid}"
                )
            ]])
        )

        return True


    if cmd == "private_s":

        sid = int(
            parts[2]
        )

        context.user_data[
            "new_batch_pending"
        ] = {
            "kind": "private_link_subject",
            "id": sid,
        }

        await q.edit_message_text(
            "🔐 <b>Subject Private Group</b>\n\n"
            "Send any message link copied from inside "
            "the target private group/channel.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=
                        f"admin:nb:subject:{sid}"
                )
            ]])
        )

        return True


    if cmd == "toggle_teacher":

        tid = int(parts[2])
        t = teacher(path, tid)

        _set_teacher(
            path,
            tid,
            "enabled",
            0 if t["enabled"] else 1
        )

        await show_teacher(q, path, tid)
        return True

    if cmd == "toggle_avail":

        sid = int(parts[2])
        s = subject(path, sid)

        _set_subject(
            path,
            sid,
            "availability",
            0 if s["availability"] else 1
        )

        await show_subject(q, path, sid)
        return True

    if cmd == "delete_teacher":

        tid = int(parts[2])
        t = teacher(path, tid)

        await q.edit_message_text(
            "🗑 <b>Delete Teacher / Batch Group?</b>\n\n"
            f'{html.escape(t["name"] if t else "Unknown")}\n\n'
            "All its subjects will also be deleted.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⚠️ Yes, Delete",
                        callback_data=
                            f"admin:nb:delete_teacher_confirm:{tid}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"admin:nb:teacher:{tid}"
                    )
                ],
            ])
        )

        return True

    if cmd == "delete_teacher_confirm":

        tid = int(parts[2])

        _delete_teacher(path, tid)

        await show_list(q, path)
        return True

    if cmd == "delete_subject":

        sid = int(parts[2])
        s = subject(path, sid)

        if not s:
            await show_menu(q, path)
            return True

        await q.edit_message_text(
            "🗑 <b>Delete Subject?</b>\n\n"
            f'{html.escape(s["name"])}',
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⚠️ Yes, Delete",
                        callback_data=
                            f"admin:nb:delete_subject_confirm:{sid}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"admin:nb:subject:{sid}"
                    )
                ],
            ])
        )

        return True

    if cmd == "delete_subject_confirm":

        sid = int(parts[2])
        s = subject(path, sid)

        tid = (
            int(s["teacher_id"])
            if s
            else 0
        )

        _delete_subject(path, sid)

        if tid:
            await show_subjects(q, path, tid)
        else:
            await show_menu(q, path)

        return True

    return True


# ============================================================
# ADMIN TEXT INPUT
# ============================================================

async def handle_message(update, context, path):

    state = context.user_data.get("new_batch_pending")

    if not state:
        return False

    if not getattr(update.message, "text", None):
        return False

    raw = update.message.text.strip()

    if not raw:
        return True

    value = (
        ""
        if raw.casefold() in (
            "clear",
            "skip",
            "none",
            "-"
        )
        else raw
    )

    kind = state.get("kind")

    try:

        if kind == "add_teacher":

            tid = _add_teacher(
                path,
                raw
            )

            context.user_data.pop(
                "new_batch_pending",
                None
            )

            await update.message.reply_text(
                "✅ Teacher / batch group added.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Open",
                        callback_data=f"admin:nb:teacher:{tid}"
                    )
                ]])
            )

            return True

        if kind == "add_subject":

            tid = int(state["teacher_id"])

            sid = _add_subject(
                path,
                tid,
                raw
            )

            context.user_data.pop(
                "new_batch_pending",
                None
            )

            await update.message.reply_text(
                "✅ Subject added.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Open",
                        callback_data=f"admin:nb:subject:{sid}"
                    )
                ]])
            )

            return True


        if kind in (
            "private_link_teacher",
            "private_link_subject",
        ):

            item_id = int(
                state["id"]
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

            if kind == "private_link_teacher":
                _set_teacher(
                    path,
                    item_id,
                    "private_message_link",
                    parsed["message_link"],
                )

                _set_teacher(
                    path,
                    item_id,
                    "private_chat_id",
                    parsed["chat_id"],
                )

                back = (
                    f"admin:nb:teacher:{item_id}"
                )

            else:
                _set_subject(
                    path,
                    item_id,
                    "private_message_link",
                    parsed["message_link"],
                )

                _set_subject(
                    path,
                    item_id,
                    "private_chat_id",
                    parsed["chat_id"],
                )

                back = (
                    f"admin:nb:subject:{item_id}"
                )

            context.user_data.pop(
                "new_batch_pending",
                None,
            )

            await update.message.reply_text(
                "✅ Private Group registered.\n\n"
                f"Target: {parsed['title']}\n"
                f"Chat ID: {parsed['chat_id'] or '—'}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data=back,
                    )
                ]])
            )

            return True


        if kind == "global_access_message":

            mode = state[
                "mode"
            ]

            # Preserve the exact Telegram-native formatting.
            save_global_message(
                path,
                mode,
                update.message.text,
                update.message.entities or [],
            )

            context.user_data.pop(
                "new_batch_pending",
                None,
            )

            await update.message.reply_text(
                "✅ Formatted message saved.\n\n"
                "Now set the Link Word / Text.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🔗 Set Link Word / Text",
                        callback_data=
                            f"admin:nb:msglink:{mode}"
                    ),
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data=
                            f"admin:nb:msgmode:{mode}"
                    )
                ]])
            )

            return True


        if kind == "global_access_link_text":

            mode = state[
                "mode"
            ]

            save_global_link_text(
                path,
                mode,
                raw,
            )

            context.user_data.pop(
                "new_batch_pending",
                None,
            )

            await update.message.reply_text(
                "✅ Embedded link text saved.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ Open Configuration",
                        callback_data=
                            f"admin:nb:msgmode:{mode}"
                    )
                ]])
            )

            return True


        if kind in (
            "private_link_teacher",
            "private_link_subject",
        ):

            item_id = int(
                state["id"]
            )

            if raw.casefold() in {
                "clear",
                "skip",
                "none",
                "-",
            }:
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

            if kind == "private_link_teacher":
                _set_teacher(
                    path,
                    item_id,
                    "private_message_link",
                    parsed[
                        "message_link"
                    ],
                )

                _set_teacher(
                    path,
                    item_id,
                    "private_chat_id",
                    parsed[
                        "chat_id"
                    ],
                )

                back = (
                    f"admin:nb:teacher:{item_id}"
                )

            else:
                _set_subject(
                    path,
                    item_id,
                    "private_message_link",
                    parsed[
                        "message_link"
                    ],
                )

                _set_subject(
                    path,
                    item_id,
                    "private_chat_id",
                    parsed[
                        "chat_id"
                    ],
                )

                back = (
                    f"admin:nb:subject:{item_id}"
                )

            context.user_data.pop(
                "new_batch_pending",
                None,
            )

            await update.message.reply_text(
                "✅ Private Group registered.\n\n"
                f"Target: {parsed['title']}\n"
                f"Chat ID: {parsed['chat_id'] or '—'}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data=back,
                    )
                ]])
            )

            return True


        if kind == "teacher_field":

            tid = int(state["id"])

            _set_teacher(
                path,
                tid,
                state["field"],
                value
            )

            context.user_data.pop(
                "new_batch_pending",
                None
            )

            await update.message.reply_text(
                "✅ Updated.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ Teacher / Group",
                        callback_data=f"admin:nb:teacher:{tid}"
                    )
                ]])
            )

            return True

        if kind == "subject_field":

            sid = int(state["id"])

            _set_subject(
                path,
                sid,
                state["field"],
                value
            )

            context.user_data.pop(
                "new_batch_pending",
                None
            )

            await update.message.reply_text(
                "✅ Updated.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ Subject",
                        callback_data=f"admin:nb:subject:{sid}"
                    )
                ]])
            )

            return True

    except Exception as e:

        await update.message.reply_text(
            "❌ Could not save: "
            + html.escape(str(e)),
            parse_mode="HTML"
        )

        return True

    return False


# ============================================================
# ZERO-AI CUSTOMER MATCHING
# ============================================================
# ============================================================
# ZERO-AI CUSTOMER MATCHING — V2
# ============================================================

# The structured New Batches database is the source of truth.
# No OpenAI call is used anywhere in this router.


def _has_devanagari(v):
    return bool(re.search(r'[\u0900-\u097f]', str(v or "")))


def _is_hinglish(v):
    q = _norm(v)

    if _has_devanagari(v):
        return True

    words = set(q.split())

    markers = {
        "hai", "hain", "h", "nahi", "nhi", "ni",
        "kya", "ka", "ki", "ke", "ko",
        "me", "mein", "se",
        "chahiye", "chahie",
        "milega", "milegi", "mile",
        "kitna", "kitne", "kitni",
        "kaunsa", "kaunsi", "konsa", "konsi",
        "kis", "sir", "mam", "maam",
        "bhai", "bhaiya", "bro",
        "padhate", "padhati",
        "available", "batao",
        "dono", "sab", "wala", "wali",
    }

    return bool(words & markers)


def _unavailable_reply(query):
    if _is_hinglish(query):
        return "Available nahi h bro."

    return "Not available bro."


def _available_reply(query):
    if _is_hinglish(query):
        return "Haan bro, available hai."

    return "Yes bro, available."


def _teacher_aliases(t):
    vals = [
        t.get("name", ""),
        t.get("aliases", ""),
    ]

    out = []

    for v in vals:
        out.extend([
            x.strip()
            for x in re.split(r"[,|;\n]+", str(v or ""))
            if x.strip()
        ])

    # Useful short teacher names.
    short_aliases = {
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
            "ayushi madam",
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
            "basava uppin",
            "bassava",
            "bassava sir",
            "bassava uppin",
        ],

        "smriti-mam": [
            "smriti",
            "smriti mam",
            "smriti maam",
            "smriti shah",
            "smriti shah mam",
            "smriti shah maam",
        ],
    }

    out.extend(
        short_aliases.get(
            str(t.get("slug") or ""),
            []
        )
    )

    out.append(t.get("name", ""))

    cleaned = {
        _norm(x)
        for x in out
        if _norm(x)
    }

    return sorted(
        cleaned,
        key=len,
        reverse=True
    )


def _contains_phrase(text, phrase):
    text = _norm(text)
    phrase = _norm(phrase)

    if not phrase:
        return False

    return bool(
        re.search(
            r"(?<!\w)"
            + re.escape(phrase)
            + r"(?!\w)",
            text
        )
    )


def _teacher_match(text, t):
    n = _norm(text)

    for alias in _teacher_aliases(t):
        if _contains_phrase(n, alias):
            return True

    return False


def _subject_aliases(s):
    out = []

    if s.get("name"):
        out.append(s["name"])

    if s.get("aliases"):
        out.extend([
            x.strip()
            for x in re.split(
                r"[,|;\n]+",
                str(s["aliases"])
            )
            if x.strip()
        ])

    if s.get("batch_code"):
        out.append(s["batch_code"])

    if s.get("part_name"):
        out.append(s["part_name"])

    name = _norm(s.get("name"))

    # Common safe short forms.
    if name == "internal security":
        out.extend(["is", "security"])

    elif name == "disaster management":
        out.extend(["dm", "disaster"])

    elif name == "environment":
        out.extend(["env"])

    elif name == "science technology":
        out.extend([
            "science and technology",
            "sci tech",
            "science tech",
        ])

    elif name == "international relations":
        out.extend(["ir"])

    return sorted(
        {
            _norm(x)
            for x in out
            if _norm(x)
        },
        key=len,
        reverse=True
    )


def _subject_score(text, s):
    n = _norm(text)

    score = 0

    # Batch code is strongest.
    if s.get("batch_code"):
        code = _norm(s["batch_code"])

        if _contains_phrase(n, code):
            score += 100

        compact_code = re.sub(
            r"\s+",
            "",
            code
        )

        compact_n = re.sub(
            r"\s+",
            "",
            n
        )

        if compact_code and compact_code in compact_n:
            score += 80

    # Exact aliases / subject names.
    for alias in _subject_aliases(s):
        if not alias:
            continue

        if _contains_phrase(n, alias):
            score += 30 + len(alias.split()) * 3

    # Part/category.
    if s.get("part_name"):
        if _contains_phrase(
            n,
            s["part_name"]
        ):
            score += 30

    if s.get("category"):
        if _contains_phrase(
            n,
            s["category"]
        ):
            score += 15

    return score


def _price_only(s):
    p = str(s.get("price") or "").strip()

    return _money(p) if p else ""


def _year_only(s):
    return str(s.get("year") or "").strip()


def _subject_label(s):
    label = str(s.get("name") or "").strip()

    if s.get("part_name"):
        label += f' — {s["part_name"]}'

    if s.get("batch_code"):
        label += f' ({s["batch_code"]})'

    return label


def _short_subject_line(s, include_year=False):
    label = _subject_label(s)

    if not s.get("availability"):
        return f"{label} — Not Available"

    parts = [label]

    if include_year and s.get("year"):
        parts.append(str(s["year"]))

    if s.get("price"):
        parts.append(_money(s["price"]))

    return " — ".join(parts)


def _subject_card(t, s):
    if not s.get("availability"):
        return None

    lines = [
        f'<b>{html.escape(t["name"])} — '
        f'{html.escape(_subject_label(s))}</b>'
    ]

    if s.get("year"):
        lines.append(
            "Year: <b>"
            + html.escape(str(s["year"]))
            + "</b>"
        )

    if s.get("price"):
        lines.append(
            "Price: <b>"
            + html.escape(_money(s["price"]))
            + "</b>"
        )

    return "\n".join(lines)


def _subject_list(
    t,
    rows,
    include_year=False,
    include_institute=False,
    include_all_price=True,
):
    lines = []

    if include_institute and t.get("institute"):
        lines.append(
            html.escape(str(t["institute"]))
        )

    for s in rows:
        lines.append(
            html.escape(
                _short_subject_line(
                    s,
                    include_year=include_year
                )
            )
        )

    if (
        include_all_price
        and t.get("all_subjects_price")
    ):
        lines += [
            "",
            "<b>Both — "
            + html.escape(
                _money(
                    t["all_subjects_price"]
                )
            )
            + "</b>"
        ]

    return "\n".join(lines)


def _detect_dimension(text):
    n = _norm(text)

    # PRICE
    if (
        re.search(
            r"\b(price|prices|fee|fees|cost|rate|"
            r"kitne ka|kitna ka|kitni ki|kitne ki|"
            r"how much)\b",
            n
        )
        or "kitne ka" in n
        or "kitni ki" in n
    ):
        return "price"

    # YEAR
    if re.search(
        r"\b(year|year ki|year ka|kis year|which year)\b",
        n
    ):
        return "year"

    # INSTITUTE
    if (
        "institute" in n
        or "where does" in n
        or "kahan padhate" in n
        or "kis institute" in n
    ):
        return "institute"

    # AVAILABILITY
    if (
        "available" in n
        or "do you have" in n
        or "milega" in n
        or "milegi" in n
        or "hai kya" in n
    ):
        return "availability"

    # GS COVERAGE
    if (
        "which gs papers" in n
        or "gs papers" in n
        or "gs paper" in n
    ):
        return "gs_papers"

    # SUBJECTS
    if (
        "what subjects" in n
        or "which subjects" in n
        or "subjects kya" in n
        or "kya kya padhate" in n
        or "kya kya padhati" in n
        or "what does" in n
        or "ka subject" in n
        or n.endswith(" subject")
    ):
        return "subjects"

    return "details"


def _wanted_year(text):
    years = re.findall(
        r"\b20\d{2}\b",
        str(text or "")
    )

    return years[0] if years else None


def _wanted_gs(text):
    n = _norm(text)

    found = []

    for g in ("gs1", "gs2", "gs3", "gs4"):
        if _contains_phrase(n, g):
            found.append(g.upper())

    # Handle "GS 2" format.
    for x in re.findall(
        r"\bgs\s*([1-4])\b",
        n
    ):
        g = "GS" + x

        if g not in found:
            found.append(g)

    return found


def _wanted_category(text):
    n = _norm(text)

    if "optional" in n:
        return "optional"

    if (
        "prelims" in n
        or "prelim" in n
    ):
        return "prelims"

    if "mains" in n:
        return "mains"

    if (
        "answer writing" in n
        or "write smart" in n
    ):
        return "answer writing"

    # "GS subject kaunsa hai"
    if (
        "gs subject" in n
        or "gs ka subject" in n
    ):
        return "gs"

    return None


def _filter_year(rows, year):
    if not year:
        return rows

    return [
        s
        for s in rows
        if str(s.get("year") or "") == str(year)
    ]


def _filter_gs(rows, wanted):
    if not wanted:
        return rows

    out = []

    for s in rows:
        paper = _norm(s.get("gs_paper"))

        if any(
            _norm(g) in paper
            for g in wanted
        ):
            out.append(s)

    return out


def _filter_category(rows, category):
    if not category:
        return rows

    out = []

    for s in rows:
        cat = _norm(s.get("category"))
        part = _norm(s.get("part_name"))

        if category == "optional":
            if "optional" in cat:
                out.append(s)

        elif category == "prelims":
            if (
                "prelims" in cat
                or "prelims" in part
            ):
                out.append(s)

        elif category == "mains":
            if (
                "mains" in cat
                or "mains" in part
            ):
                out.append(s)

        elif category == "answer writing":
            if (
                "answer writing" in cat
                or _norm(s.get("name")) == "write smart"
            ):
                out.append(s)

        elif category == "gs":
            if "gs" in cat:
                out.append(s)

    return out


def _gs_coverage(rows):
    mapping = {}

    for s in rows:
        if not s.get("availability"):
            continue

        papers = re.findall(
            r"GS[1-4]",
            str(s.get("gs_paper") or "").upper()
        )

        for paper in papers:
            mapping.setdefault(
                paper,
                []
            ).append(
                _subject_label(s)
            )

    if not mapping:
        return ""

    lines = []

    for paper in (
        "GS1",
        "GS2",
        "GS3",
        "GS4",
    ):
        if paper not in mapping:
            continue

        lines.append(
            f"{paper} — "
            + ", ".join(mapping[paper])
        )

    return "\n".join(lines)


def _unique_code_teacher(path, text):
    """
    Allow unique codes such as PCB / QEP without teacher name.
    """
    n = _norm(text)

    with _con(path) as con:
        rows = [
            dict(r)
            for r in con.execute("""
                SELECT
                    s.*,
                    t.name AS teacher_name,
                    t.slug AS teacher_slug,
                    t.institute AS teacher_institute,
                    t.aliases AS teacher_aliases,
                    t.keywords AS teacher_keywords,
                    t.all_subjects_price AS teacher_all_price,
                    t.enabled AS teacher_enabled
                FROM structured_batch_subjects s
                JOIN structured_batch_teachers t
                    ON t.id=s.teacher_id
                WHERE
                    s.enabled=1
                    AND t.enabled=1
                    AND s.batch_code<>''
            """).fetchall()
        ]

    found = []

    for s in rows:
        code = _norm(s.get("batch_code"))

        compact_code = re.sub(
            r"\s+",
            "",
            code
        )

        compact_n = re.sub(
            r"\s+",
            "",
            n
        )

        # SHORT_BATCH_CODES_V3
        # Match full code:
        #   PCB 16
        #   QEP 9
        #
        # Also match the stable short code:
        #   PCB
        #   QEP
        #
        # This allows queries such as:
        #   "Price for both PCB and QEP"
        # without requiring the teacher name.
        short_code = (
            code.split()[0]
            if code
            else ""
        )

        if (
            _contains_phrase(n, code)
            or (
                compact_code
                and compact_code in compact_n
            )
            or (
                len(short_code) >= 3
                and _contains_phrase(n, short_code)
            )
        ):
            found.append(s)

    if not found:
        return None

    tids = {
        int(x["teacher_id"])
        for x in found
    }

    if len(tids) != 1:
        return None

    return teacher(
        path,
        list(tids)[0]
    )


def _all_subject_rows(path, tid):
    return subjects(
        path,
        tid,
        enabled_only=True
    )


def customer_reply(path, text):
    ensure_schema(path)

    n = _norm(text)

    if not n:
        return None

    all_teachers = [
        t
        for t in teachers(path)
        if t.get("enabled")
    ]

    matched_teachers = [
        t
        for t in all_teachers
        if _teacher_match(n, t)
    ]

    # If teacher was omitted but a unique batch code such as
    # PCB/QEP identifies the teacher, use it.
    if not matched_teachers:
        code_teacher = _unique_code_teacher(
            path,
            text
        )

        if code_teacher:
            matched_teachers = [
                code_teacher
            ]

    if len(matched_teachers) != 1:
        return None

    t = matched_teachers[0]

    rows = _all_subject_rows(
        path,
        t["id"]
    )

    if not rows:
        return None

    dimension = _detect_dimension(text)
    wanted_year = _wanted_year(text)
    wanted_gs = _wanted_gs(text)
    wanted_category = _wanted_category(text)

    # --------------------------------------------------------
    # COMBINED / BOTH PRICE
    # --------------------------------------------------------
    combined_request = bool(
        re.search(
            r"\b(both|dono|combined|together|all subjects|"
            r"all batches)\b",
            n
        )
    )

    # Mrunal-specific teacherless/teacher combined PCB+QEP.
    has_pcb = (
        "pcb" in n
        or "pcb16" in re.sub(r"\s+", "", n)
    )

    has_qep = (
        "qep" in n
        or "qep9" in re.sub(r"\s+", "", n)
    )

    if (
        t.get("all_subjects_price")
        and (
            combined_request
            or (has_pcb and has_qep)
        )
        and dimension == "price"
    ):
        return html.escape(
            _money(
                t["all_subjects_price"]
            )
        )

    # --------------------------------------------------------
    # Find explicit subject/part/code matches.
    # --------------------------------------------------------
    scored = []

    for s in rows:
        score = _subject_score(
            text,
            s
        )

        if score:
            scored.append(
                (score, s)
            )

    # Important:
    # plain "GS3" must mean the GS paper,
    # not automatically the subject called "GS3 Module".
    if wanted_gs:
        explicit_module = bool(
            re.search(
                r"\bgs\s*3\s+module\b",
                n
            )
        )

        if not explicit_module:
            scored = [
                (score, s)
                for score, s in scored
                if _norm(s.get("name")) != "gs3 module"
            ]

    # --------------------------------------------------------
    # Filters requested directly by customer.
    # --------------------------------------------------------
    filtered = rows

    if wanted_year:
        yr = _filter_year(
            rows,
            wanted_year
        )

        if yr:
            filtered = yr

    if wanted_category:
        cat = _filter_category(
            filtered,
            wanted_category
        )

        if cat:
            filtered = cat

    if wanted_gs:
        gs = _filter_gs(
            filtered,
            wanted_gs
        )

        if gs:
            filtered = gs

    # --------------------------------------------------------
    # EXPLICIT SUBJECT SELECTION
    # --------------------------------------------------------
    chosen = None

    if scored:
        top_score = max(
            score
            for score, _ in scored
        )

        top = [
            s
            for score, s in scored
            if score == top_score
        ]

        if len(top) == 1:
            chosen = top[0]

    # If year/category/GS filter uniquely identifies one subject,
    # use it.
    if (
        not chosen
        and len(filtered) == 1
        and (
            wanted_year
            or wanted_category
            or wanted_gs
        )
    ):
        chosen = filtered[0]

    # --------------------------------------------------------
    # UNAVAILABLE IS TERMINAL.
    # --------------------------------------------------------
    if chosen and not chosen.get("availability"):
        return _unavailable_reply(text)

    # --------------------------------------------------------
    # SINGLE-SUBJECT DIMENSION RESPONSES
    # --------------------------------------------------------
    if chosen:

        if dimension == "price":
            price = _price_only(chosen)

            if price:
                return html.escape(price)

            return ""

        if dimension == "year":
            year = _year_only(chosen)

            if year:
                return html.escape(year)

            return ""

        if dimension == "availability":
            return _available_reply(text)

        # General subject request.
        return _subject_card(
            t,
            chosen
        )

    # --------------------------------------------------------
    # INSTITUTE
    # --------------------------------------------------------
    if dimension == "institute":
        institute = str(
            t.get("institute")
            or ""
        ).strip()

        return (
            html.escape(institute)
            if institute
            else ""
        )

    # --------------------------------------------------------
    # GS PAPER COVERAGE QUESTION
    # --------------------------------------------------------
    if dimension == "gs_papers":
        ans = _gs_coverage(rows)

        return html.escape(ans) if ans else ""

    # --------------------------------------------------------
    # YEAR FILTER LIST
    # --------------------------------------------------------
    if wanted_year and filtered:
        return _subject_list(
            t,
            filtered,
            include_year=False,
            include_institute=False,
            include_all_price=False,
        )

    # --------------------------------------------------------
    # CATEGORY FILTER LIST
    # --------------------------------------------------------
    if wanted_category and filtered:
        return _subject_list(
            t,
            filtered,
            include_year=False,
            include_institute=False,
            include_all_price=False,
        )

    # --------------------------------------------------------
    # GS PAPER FILTER LIST
    # --------------------------------------------------------
    if wanted_gs and filtered:
        return _subject_list(
            t,
            filtered,
            include_year=False,
            include_institute=False,
            include_all_price=False,
        )

    # --------------------------------------------------------
    # MULTIPLE EXPLICIT SUBJECTS
    # Example: IR + PSIR / PCB + QEP
    # --------------------------------------------------------
    if scored:
        top_score = max(
            score
            for score, _ in scored
        )

        top = [
            s
            for score, s in scored
            if score >= max(
                20,
                top_score - 20
            )
        ]

        # unique by subject ID
        seen = set()
        top_unique = []

        for s in top:
            if s["id"] in seen:
                continue

            seen.add(s["id"])
            top_unique.append(s)

        if len(top_unique) > 1:

            if (
                dimension == "price"
                and t.get(
                    "all_subjects_price"
                )
                and len(top_unique)
                == len(rows)
            ):
                return html.escape(
                    _money(
                        t["all_subjects_price"]
                    )
                )

            return _subject_list(
                t,
                top_unique,
                include_year=False,
                include_institute=False,
                include_all_price=True,
            )

    # --------------------------------------------------------
    # TEACHER-LEVEL PRICE QUESTION
    # Multiple subjects -> show subject-wise prices.
    # --------------------------------------------------------
    if dimension == "price":

        available_rows = [
            s
            for s in rows
            if s.get("availability")
        ]

        if len(available_rows) == 1:
            p = _price_only(
                available_rows[0]
            )

            return html.escape(p) if p else ""

        return _subject_list(
            t,
            rows,
            include_year=False,
            include_institute=False,
            include_all_price=True,
        )

    # --------------------------------------------------------
    # TEACHER-LEVEL YEAR QUESTION
    # --------------------------------------------------------
    if dimension == "year":

        years = sorted({
            str(s.get("year"))
            for s in rows
            if s.get("availability")
            and s.get("year")
        })

        if len(years) == 1:
            return html.escape(
                years[0]
            )

        return _subject_list(
            t,
            [
                s
                for s in rows
                if s.get("availability")
            ],
            include_year=True,
            include_institute=False,
            include_all_price=False,
        )

    # --------------------------------------------------------
    # TEACHER AVAILABILITY / SUBJECT LIST
    # --------------------------------------------------------
    if dimension in (
        "availability",
        "subjects",
    ):
        return _subject_list(
            t,
            rows,
            include_year=False,
            include_institute=False,
            include_all_price=True,
        )

    # --------------------------------------------------------
    # DEFAULT TEACHER OVERVIEW:
    # concise human-style list with prices.
    # --------------------------------------------------------
    return _subject_list(
        t,
        rows,
        include_year=False,
        include_institute=False,
        include_all_price=True,
    )
