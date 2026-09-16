import asyncio
import html
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# ============================================================
# ACCESS COMMAND SYSTEM V1
#
# NORMAL:
#   /xyz
#   -> fresh 24h invite
#   -> member_limit = 1
#   -> revoke immediately after first join
#
# DEMO:
#   /demo xyz
#   -> saved demo link
#
# PRE:
#   /pre xyz
#   -> fresh 24h invite
#   -> member_limit = 1
#   -> revoke immediately after first join
#   -> PRE timer begins from JOIN time
#   -> remove member at expiry
#   -> immediately unban after removal
#
# Main-account/admin only.
# Customer slash messages do NOT invoke this subsystem.
# ============================================================

LINK_TTL_SECONDS = 24 * 60 * 60
FUZZY_MIN_SCORE = 78.0
FUZZY_MIN_MARGIN = 7.0
RECOVERY_INTERVAL = 60


def _con(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _norm(v):
    v = str(v or "").casefold().replace("_", " ")
    v = re.sub(r"[^\w\s&+.-]+", " ", v, flags=re.UNICODE)
    return re.sub(r"\s+", " ", v).strip()


def _split_aliases(v):
    return [
        x.strip()
        for x in re.split(r"[,|;\n]+", str(v or ""))
        if x.strip()
    ]


# ACCESS_COMMAND_ADMIN_AUTH_FIX_V2
def _admin_ids(settings):
    """
    Use exactly the same admin source as the main bot:
        settings.admin_user_ids

    This keeps Access Commands authorization identical
    to the existing /admin panel.
    """
    raw = getattr(
        settings,
        "admin_user_ids",
        []
    ) or []

    if isinstance(raw, str):
        raw = re.findall(r"\d+", raw)

    out = []

    for value in raw:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue

    return list(dict.fromkeys(out))


def ensure_schema(path):
    with _con(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS access_command_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',

            normal_chat_id INTEGER,
            demo_link TEXT NOT NULL DEFAULT '',
            pre_chat_id INTEGER,

            pre_minutes INTEGER NOT NULL DEFAULT 5,

            enabled INTEGER NOT NULL DEFAULT 1,

            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_access_command_name
        ON access_command_mappings(name);

        CREATE TABLE IF NOT EXISTS access_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            mapping_id INTEGER NOT NULL,
            mode TEXT NOT NULL,

            customer_chat_id INTEGER,
            requested_by INTEGER,

            target_chat_id INTEGER NOT NULL,

            invite_link TEXT NOT NULL UNIQUE,

            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,

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

            last_error TEXT NOT NULL DEFAULT '',

            FOREIGN KEY(mapping_id)
                REFERENCES access_command_mappings(id)
        );

        CREATE INDEX IF NOT EXISTS idx_access_invites_status
        ON access_invites(status);

        CREATE INDEX IF NOT EXISTS idx_access_invites_link
        ON access_invites(invite_link);

        CREATE INDEX IF NOT EXISTS idx_access_invites_remove
        ON access_invites(remove_at);
        """)

        db.commit()


# ============================================================
# MAPPINGS
# ============================================================

def list_mappings(path, enabled_only=False):
    ensure_schema(path)

    sql = """
        SELECT *
        FROM access_command_mappings
    """

    if enabled_only:
        sql += " WHERE enabled=1 "

    sql += " ORDER BY lower(name),id "

    with _con(path) as db:
        return [
            dict(x)
            for x in db.execute(sql).fetchall()
        ]


def get_mapping(path, mapping_id):
    ensure_schema(path)

    with _con(path) as db:
        r = db.execute("""
            SELECT *
            FROM access_command_mappings
            WHERE id=?
        """, (int(mapping_id),)).fetchone()

    return dict(r) if r else None


def create_mapping(path, name):
    ensure_schema(path)

    now = time.time()

    with _con(path) as db:
        cur = db.execute("""
            INSERT INTO access_command_mappings
            (
                name,
                aliases,
                pre_minutes,
                enabled,
                created_at,
                updated_at
            )
            VALUES(?,?,5,1,?,?)
        """, (
            str(name).strip(),
            "",
            now,
            now,
        ))

        db.commit()
        return int(cur.lastrowid)


def update_mapping(path, mapping_id, field, value):
    allowed = {
        "name",
        "aliases",
        "normal_chat_id",
        "demo_link",
        "pre_chat_id",
        "pre_minutes",
        "enabled",
    }

    if field not in allowed:
        raise ValueError("Unsupported mapping field")

    with _con(path) as db:
        db.execute(
            f"""
            UPDATE access_command_mappings
            SET {field}=?,
                updated_at=?
            WHERE id=?
            """,
            (
                value,
                time.time(),
                int(mapping_id),
            )
        )
        db.commit()


def delete_mapping(path, mapping_id):
    with _con(path) as db:
        active = db.execute("""
            SELECT COUNT(*)
            FROM access_invites
            WHERE mapping_id=?
              AND status IN (
                  'active',
                  'joined',
                  'used_pending_revoke',
                  'pre_active'
              )
        """, (int(mapping_id),)).fetchone()[0]

        if active:
            raise RuntimeError(
                "This mapping has active access links. "
                "Disable it instead of deleting it."
            )

        db.execute(
            "DELETE FROM access_command_mappings WHERE id=?",
            (int(mapping_id),)
        )
        db.commit()


# ============================================================
# FUZZY RESOLVER
# ============================================================

def _candidate_terms(mapping):
    vals = [
        mapping.get("name", ""),
    ]

    vals += _split_aliases(
        mapping.get("aliases", "")
    )

    return [
        _norm(x)
        for x in vals
        if _norm(x)
    ]


def _score(query, candidate):
    q = _norm(query)
    c = _norm(candidate)

    if not q or not c:
        return 0.0

    if q == c:
        return 100.0

    q_tokens = set(q.split())
    c_tokens = set(c.split())

    if q in c or c in q:
        substring = 92.0
    else:
        substring = 0.0

    seq = SequenceMatcher(
        None,
        q,
        c
    ).ratio() * 100.0

    if q_tokens and c_tokens:
        overlap = (
            len(q_tokens & c_tokens)
            / len(q_tokens | c_tokens)
        ) * 100.0
    else:
        overlap = 0.0

    # Subject/batch token overlap matters more than raw typo similarity.
    return max(
        substring,
        (seq * 0.58) + (overlap * 0.42),
    )


def resolve_mapping(path, query):
    mappings = list_mappings(
        path,
        enabled_only=True
    )

    scored = []

    for m in mappings:
        best = 0.0

        for term in _candidate_terms(m):
            best = max(
                best,
                _score(query, term)
            )

        if best:
            scored.append(
                (best, m)
            )

    scored.sort(
        key=lambda x: x[0],
        reverse=True
    )

    if not scored:
        return {
            "status": "none",
            "mapping": None,
            "score": 0.0,
            "alternatives": [],
        }

    top_score, top = scored[0]

    second_score = (
        scored[1][0]
        if len(scored) > 1
        else 0.0
    )

    if top_score < FUZZY_MIN_SCORE:
        return {
            "status": "none",
            "mapping": None,
            "score": top_score,
            "alternatives": scored[:3],
        }

    if (
        len(scored) > 1
        and top_score - second_score < FUZZY_MIN_MARGIN
    ):
        return {
            "status": "ambiguous",
            "mapping": None,
            "score": top_score,
            "alternatives": scored[:3],
        }

    return {
        "status": "ok",
        "mapping": top,
        "score": top_score,
        "alternatives": scored[:3],
    }


# ============================================================
# NOTIFICATIONS
# ============================================================

async def notify_admins(
    bot,
    settings,
    text,
):
    for uid in _admin_ids(settings):
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception(
                "ACCESS NOTIFY FAILED admin_id=%r",
                uid
            )


# ============================================================
# INVITE CREATION / STORAGE
# ============================================================

async def _create_one_person_link(
    bot,
    path,
    mapping,
    mode,
    customer_chat_id,
    requested_by,
    target_chat_id,
):
    now = time.time()
    expires_at = now + LINK_TTL_SECONDS

    expire_dt = datetime.now(
        timezone.utc
    ) + timedelta(
        seconds=LINK_TTL_SECONDS
    )

    invite = await bot.create_chat_invite_link(
        chat_id=int(target_chat_id),
        expire_date=expire_dt,
        member_limit=1,
        creates_join_request=False,
        name=(
            f"{mode.upper()} "
            f"{mapping['name'][:20]} "
            f"{int(now)}"
        )[:32],
    )

    link = str(invite.invite_link)

    with _con(path) as db:
        cur = db.execute("""
            INSERT INTO access_invites
            (
                mapping_id,
                mode,

                customer_chat_id,
                requested_by,

                target_chat_id,

                invite_link,

                created_at,
                expires_at,

                pre_minutes,

                status,
                revoke_status
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(mapping["id"]),
            str(mode),

            (
                int(customer_chat_id)
                if customer_chat_id
                else None
            ),
            (
                int(requested_by)
                if requested_by
                else None
            ),

            int(target_chat_id),

            link,

            now,
            expires_at,

            (
                int(mapping.get("pre_minutes") or 5)
                if mode == "pre"
                else None
            ),

            "active",
            "pending",
        ))

        invite_id = int(
            cur.lastrowid
        )

        db.commit()

    return {
        "invite_id": invite_id,
        "invite_link": link,
        "expires_at": expires_at,
    }


def _parse_command(text):
    raw = str(text or "").strip()

    if not raw.startswith("/"):
        return None, None

    body = raw[1:].strip()

    if not body:
        return None, None

    lower = body.casefold()

    if lower == "demo":
        return "demo", ""

    if lower.startswith("demo "):
        return (
            "demo",
            body[5:].strip()
        )

    if lower == "pre":
        return "pre", ""

    if lower.startswith("pre "):
        return (
            "pre",
            body[4:].strip()
        )

    # Anything else after / is the normal access lookup.
    return "normal", body


# ============================================================
# SEND ACCESS
# ============================================================

async def execute_access(
    *,
    application,
    settings,
    mode,
    query,
    customer_chat_id,
    requested_by=None,
    main_account_client=None,
):
    path = settings.database_path

    result = resolve_mapping(
        path,
        query
    )

    if result["status"] != "ok":
        return {
            "ok": False,
            "reason": result["status"],
            "alternatives": result.get(
                "alternatives",
                []
            ),
        }

    mapping = result["mapping"]
    bot = application.bot

    if mode == "demo":
        link = str(
            mapping.get("demo_link")
            or ""
        ).strip()

        if not link:
            return {
                "ok": False,
                "reason": "demo_not_configured",
                "mapping": mapping,
            }

        if main_account_client:
            await main_account_client.send_message(
                int(customer_chat_id),
                link,
            )
        else:
            await bot.send_message(
                int(customer_chat_id),
                link,
                disable_web_page_preview=True,
            )

        return {
            "ok": True,
            "mode": "demo",
            "mapping": mapping,
            "link": link,
        }

    if mode == "normal":
        target_chat_id = mapping.get(
            "normal_chat_id"
        )

    elif mode == "pre":
        target_chat_id = mapping.get(
            "pre_chat_id"
        )

    else:
        return {
            "ok": False,
            "reason": "invalid_mode",
        }

    if not target_chat_id:
        return {
            "ok": False,
            "reason": f"{mode}_target_not_configured",
            "mapping": mapping,
        }

    try:
        created = await _create_one_person_link(
            bot,
            path,
            mapping,
            mode,
            customer_chat_id,
            requested_by,
            target_chat_id,
        )

    except Exception as e:
        logger.exception(
            "ACCESS LINK CREATE FAILED mode=%r mapping=%r",
            mode,
            mapping.get("name")
        )

        return {
            "ok": False,
            "reason": "telegram_create_failed",
            "error": str(e),
            "mapping": mapping,
        }

    link = created[
        "invite_link"
    ]

    if main_account_client:
        await main_account_client.send_message(
            int(customer_chat_id),
            link,
        )
    else:
        await bot.send_message(
            int(customer_chat_id),
            link,
            disable_web_page_preview=True,
        )

    return {
        "ok": True,
        "mode": mode,
        "mapping": mapping,
        "link": link,
        "invite_id": created["invite_id"],
        "expires_at": created["expires_at"],
    }


# ============================================================
# MAIN ACCOUNT OUTGOING COMMAND
# ============================================================

async def handle_main_account_outgoing(
    event,
    application,
    settings,
    client,
):
    if not bool(
        getattr(event, "is_private", False)
    ):
        return False

    text = str(
        getattr(event, "raw_text", "")
        or ""
    ).strip()

    mode, query = _parse_command(
        text
    )

    if not mode:
        return False

    # Do not hijack known global/admin commands unless they actually
    # resolve to an enabled access mapping.
    if mode == "normal" and _norm(query) in {
        "admin",
        "help",
        "start",
    }:
        return False

    if not query:
        return False

    path = settings.database_path

    lookup = resolve_mapping(
        path,
        query
    )

    # Only consume the outgoing slash command when Access Commands
    # recognises or nearly recognises it.
    if lookup["status"] == "none":
        return False

    customer_chat_id = int(
        getattr(event, "chat_id", 0)
        or 0
    )

    sender_id = int(
        getattr(event, "sender_id", 0)
        or 0
    )

    # Delete admin slash command from customer's chat.
    try:
        await event.delete()
    except Exception:
        logger.exception(
            "ACCESS COMMAND DELETE FAILED"
        )

    if lookup["status"] == "ambiguous":
        alts = []

        for score, m in lookup[
            "alternatives"
        ]:
            alts.append(
                f"• {m['name']} ({score:.0f}%)"
            )

        await notify_admins(
            application.bot,
            settings,
            "⚠️ Access command was ambiguous\n\n"
            f"Typed: /{html.escape(query)}\n\n"
            + "\n".join(alts)
        )

        return True

    result = await execute_access(
        application=application,
        settings=settings,
        mode=mode,
        query=query,
        customer_chat_id=customer_chat_id,
        requested_by=sender_id,
        main_account_client=client,
    )

    if not result.get("ok"):
        mapping = result.get(
            "mapping"
        ) or lookup.get(
            "mapping"
        ) or {}

        await notify_admins(
            application.bot,
            settings,
            "⚠️ Access link failed\n\n"
            f"Command: {html.escape(text)}\n"
            f"Batch: {html.escape(str(mapping.get('name') or query))}\n"
            f"Reason: {html.escape(str(result.get('reason') or 'unknown'))}"
        )

        return True

    logger.info(
        "ACCESS COMMAND SENT: mode=%s mapping=%r customer=%r",
        mode,
        result["mapping"]["name"],
        customer_chat_id,
    )

    return True


# ============================================================
# JOIN / REVOKE
# ============================================================

async def _revoke_row(
    bot,
    settings,
    row,
    reason,
):
    path = settings.database_path

    try:
        await bot.revoke_chat_invite_link(
            chat_id=int(row["target_chat_id"]),
            invite_link=row["invite_link"],
        )

        now = time.time()

        with _con(path) as db:
            db.execute("""
                UPDATE access_invites
                SET
                    revoke_status='revoked',
                    revoked_at=?,
                    last_error=''
                WHERE id=?
            """, (
                now,
                int(row["id"]),
            ))

            db.commit()

        mapping = get_mapping(
            path,
            row["mapping_id"]
        ) or {}

        await notify_admins(
            bot,
            settings,
            "🔒 Invite Link Revoked\n\n"
            f"Batch: {mapping.get('name','-')}\n"
            f"Mode: {str(row['mode']).upper()}\n"
            f"Reason: {reason}\n"
            "Status: Permanently revoked"
        )

        return True

    except Exception as e:
        with _con(path) as db:
            db.execute("""
                UPDATE access_invites
                SET
                    revoke_status='pending_retry',
                    last_error=?
                WHERE id=?
            """, (
                str(e)[:500],
                int(row["id"]),
            ))

            db.commit()

        logger.exception(
            "ACCESS REVOKE FAILED invite_id=%r",
            row["id"]
        )

        return False


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
        None
    )

    if not invite_obj:
        return

    invite_link = str(
        getattr(
            invite_obj,
            "invite_link",
            ""
        )
        or ""
    )

    if not invite_link:
        return

    old_status = str(
        getattr(
            change.old_chat_member,
            "status",
            ""
        )
    )

    new_status = str(
        getattr(
            change.new_chat_member,
            "status",
            ""
        )
    )

    joined = (
        old_status in {
            "left",
            "kicked",
        }
        and new_status in {
            "member",
            "administrator",
            "creator",
            "restricted",
        }
    )

    if not joined:
        return

    application = context.application

    settings = application.bot_data.get(
        "access_settings"
    )

    if settings is None:
        return

    path = settings.database_path

    with _con(path) as db:
        r = db.execute("""
            SELECT *
            FROM access_invites
            WHERE invite_link=?
            LIMIT 1
        """, (
            invite_link,
        )).fetchone()

    if not r:
        return

    row = dict(r)

    # Idempotent: don't duplicate join processing.
    if row.get("joined_at"):
        return

    user = change.new_chat_member.user

    name = " ".join(
        x
        for x in [
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        ]
        if x
    ).strip()

    username = str(
        getattr(user, "username", "")
        or ""
    )

    now = time.time()

    remove_at = None

    status = "joined"

    if row["mode"] == "pre":
        mins = int(
            row.get("pre_minutes")
            or 5
        )

        remove_at = (
            now + (mins * 60)
        )

        status = "pre_active"

    with _con(path) as db:
        db.execute("""
            UPDATE access_invites
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

    mapping = get_mapping(
        path,
        row["mapping_id"]
    ) or {}

    joined_text = (
        "✅ Customer Joined\n\n"
        f"Name: {name or '-'}\n"
        f"Telegram ID: {user.id}\n"
        f"Username: {'@' + username if username else '-'}\n"
        f"Batch: {mapping.get('name','-')}\n"
        f"Mode: {str(row['mode']).upper()}"
    )

    if row["mode"] == "pre":
        joined_text += (
            f"\nPRE Access: "
            f"{int(row.get('pre_minutes') or 5)} minutes"
        )

    await notify_admins(
        context.bot,
        settings,
        joined_text,
    )

    # Refresh after DB update.
    with _con(path) as db:
        r = db.execute("""
            SELECT *
            FROM access_invites
            WHERE id=?
        """, (
            int(row["id"]),
        )).fetchone()

    await _revoke_row(
        context.bot,
        settings,
        dict(r),
        "First member joined",
    )


# ============================================================
# PRE REMOVE + UNBAN
# ============================================================

async def _remove_pre_member(
    bot,
    settings,
    row,
):
    path = settings.database_path

    user_id = row.get(
        "joined_user_id"
    )

    if not user_id:
        return False

    chat_id = int(
        row["target_chat_id"]
    )

    # Telegram removal uses ban -> immediately unban.
    try:
        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=int(user_id),
        )

        with _con(path) as db:
            db.execute("""
                UPDATE access_invites
                SET
                    removal_status='removed',
                    removed_at=?,
                    status='pre_removed'
                WHERE id=?
            """, (
                time.time(),
                int(row["id"]),
            ))
            db.commit()

    except Exception as e:
        with _con(path) as db:
            db.execute("""
                UPDATE access_invites
                SET
                    removal_status='pending_retry',
                    last_error=?
                WHERE id=?
            """, (
                str(e)[:500],
                int(row["id"]),
            ))
            db.commit()

        logger.exception(
            "PRE REMOVE FAILED invite_id=%r",
            row["id"]
        )

        return False

    unban_ok = False

    try:
        await bot.unban_chat_member(
            chat_id=chat_id,
            user_id=int(user_id),
            only_if_banned=True,
        )

        unban_ok = True

        with _con(path) as db:
            db.execute("""
                UPDATE access_invites
                SET
                    unban_status='unbanned',
                    unbanned_at=?,
                    status='completed',
                    last_error=''
                WHERE id=?
            """, (
                time.time(),
                int(row["id"]),
            ))
            db.commit()

    except Exception as e:
        with _con(path) as db:
            db.execute("""
                UPDATE access_invites
                SET
                    unban_status='pending_retry',
                    last_error=?
                WHERE id=?
            """, (
                str(e)[:500],
                int(row["id"]),
            ))
            db.commit()

        logger.exception(
            "PRE UNBAN FAILED invite_id=%r",
            row["id"]
        )

    mapping = get_mapping(
        path,
        row["mapping_id"]
    ) or {}

    await notify_admins(
        bot,
        settings,
        "🚪 PRE Access Removed\n\n"
        f"Name: {row.get('joined_name') or '-'}\n"
        f"Telegram ID: {user_id}\n"
        f"Batch: {mapping.get('name','-')}\n"
        f"Access: {int(row.get('pre_minutes') or 5)} minutes\n"
        "Removal: Completed\n"
        f"Ban cleanup: {'Unbanned successfully' if unban_ok else 'Pending retry'}"
    )

    return unban_ok


# ============================================================
# CRASH / NETWORK RECOVERY
# ============================================================

async def reconcile(
    application,
    settings,
):
    path = settings.database_path
    ensure_schema(path)

    bot = application.bot
    now = time.time()

    # Revoke expired links that never got used.
    with _con(path) as db:
        rows = [
            dict(x)
            for x in db.execute("""
                SELECT *
                FROM access_invites
                WHERE
                    expires_at <= ?
                    AND revoke_status != 'revoked'
            """, (
                now,
            )).fetchall()
        ]

    for row in rows:
        ok = await _revoke_row(
            bot,
            settings,
            row,
            "24-hour expiry",
        )

        if ok:
            with _con(path) as db:
                db.execute("""
                    UPDATE access_invites
                    SET status=
                        CASE
                            WHEN joined_at IS NULL
                            THEN 'expired'
                            ELSE status
                        END
                    WHERE id=?
                """, (
                    int(row["id"]),
                ))
                db.commit()

    # Retry immediate revoke after joins.
    with _con(path) as db:
        rows = [
            dict(x)
            for x in db.execute("""
                SELECT *
                FROM access_invites
                WHERE joined_at IS NOT NULL
                  AND revoke_status != 'revoked'
            """).fetchall()
        ]

    for row in rows:
        await _revoke_row(
            bot,
            settings,
            row,
            "Recovery after first member joined",
        )

    # PRE expiry / crash recovery.
    with _con(path) as db:
        rows = [
            dict(x)
            for x in db.execute("""
                SELECT *
                FROM access_invites
                WHERE mode='pre'
                  AND joined_user_id IS NOT NULL
                  AND remove_at IS NOT NULL
                  AND remove_at <= ?
                  AND status != 'completed'
            """, (
                now,
            )).fetchall()
        ]

    for row in rows:
        # If already removed but unban failed, only retry unban.
        if row.get("removal_status") == "removed":
            try:
                await bot.unban_chat_member(
                    chat_id=int(row["target_chat_id"]),
                    user_id=int(row["joined_user_id"]),
                    only_if_banned=True,
                )

                with _con(path) as db:
                    db.execute("""
                        UPDATE access_invites
                        SET
                            unban_status='unbanned',
                            unbanned_at=?,
                            status='completed',
                            last_error=''
                        WHERE id=?
                    """, (
                        time.time(),
                        int(row["id"]),
                    ))
                    db.commit()

                mapping = get_mapping(
                    path,
                    row["mapping_id"]
                ) or {}

                await notify_admins(
                    bot,
                    settings,
                    "✅ PRE Ban Cleanup Completed\n\n"
                    f"Name: {row.get('joined_name') or '-'}\n"
                    f"Telegram ID: {row['joined_user_id']}\n"
                    f"Batch: {mapping.get('name','-')}\n"
                    "Status: Unbanned successfully"
                )

            except Exception:
                logger.exception(
                    "PRE RECOVERY UNBAN FAILED invite_id=%r",
                    row["id"]
                )

        else:
            await _remove_pre_member(
                bot,
                settings,
                row,
            )


async def _recovery_loop(
    application,
    settings,
):
    # Let Telegram/PTB finish startup.
    await asyncio.sleep(5)

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
                "ACCESS RECOVERY LOOP FAILED"
            )

        await asyncio.sleep(
            RECOVERY_INTERVAL
        )


# ============================================================
# ADMIN PANEL
# ============================================================

_ADMIN_PENDING = {}


def _admin_home_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Add Mapping",
                callback_data="access:add",
            ),
            InlineKeyboardButton(
                "📋 Mappings",
                callback_data="access:list",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin:home",
            )
        ],
    ])


async def access_admin_home(
    query,
):
    await query.edit_message_text(
        "🔗 Access Commands\n\n"
        "Normal: /xyz\n"
        "Demo: /demo xyz\n"
        "PRE: /pre xyz\n\n"
        "Normal + PRE links:\n"
        "• 24 hours\n"
        "• 1 person\n"
        "• revoked immediately after first join",
        reply_markup=_admin_home_keyboard(),
    )


def _mapping_keyboard(m):
    enabled = bool(
        m.get("enabled")
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ Name",
                callback_data=f"access:edit:{m['id']}:name",
            ),
            InlineKeyboardButton(
                "🏷 Aliases",
                callback_data=f"access:edit:{m['id']}:aliases",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔐 Normal Group",
                callback_data=f"access:edit:{m['id']}:normal_chat_id",
            ),
            InlineKeyboardButton(
                "🎬 Demo Link",
                callback_data=f"access:edit:{m['id']}:demo_link",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏱ PRE Group",
                callback_data=f"access:edit:{m['id']}:pre_chat_id",
            ),
            InlineKeyboardButton(
                "⌛ PRE Duration",
                callback_data=f"access:edit:{m['id']}:pre_minutes",
            ),
        ],
        [
            InlineKeyboardButton(
                "✅ Enabled" if enabled else "⛔ Disabled",
                callback_data=f"access:toggle:{m['id']}",
            ),
            InlineKeyboardButton(
                "🗑 Delete",
                callback_data=f"access:deleteask:{m['id']}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ Mappings",
                callback_data="access:list",
            )
        ],
    ])


async def _show_mapping(
    query,
    path,
    mapping_id,
):
    m = get_mapping(
        path,
        mapping_id
    )

    if not m:
        await query.answer(
            "Mapping not found",
            show_alert=True,
        )
        return

    text = (
        f"🔗 {html.escape(m['name'])}\n\n"
        f"Aliases: {html.escape(m.get('aliases') or '-')}\n"
        f"Normal group: {m.get('normal_chat_id') or '-'}\n"
        f"Demo link: {html.escape(m.get('demo_link') or '-')}\n"
        f"PRE group: {m.get('pre_chat_id') or '-'}\n"
        f"PRE duration: {m.get('pre_minutes') or 5} min\n"
        f"Enabled: {'Yes' if m.get('enabled') else 'No'}"
    )

    await query.edit_message_text(
        text,
        reply_markup=_mapping_keyboard(m),
        disable_web_page_preview=True,
    )


async def admin_callback(
    update,
    context,
):
    query = update.callback_query

    if not query:
        return

    settings = context.application.bot_data.get(
        "access_settings"
    )

    if settings is None:
        return

    uid = int(
        update.effective_user.id
    )

    if uid not in _admin_ids(settings):
        await query.answer(
            "Admin only",
            show_alert=True,
        )
        raise ApplicationHandlerStop

    data = str(
        query.data
        or ""
    )

    if not data.startswith("access:"):
        return

    await query.answer()

    path = settings.database_path

    if data == "access:home":
        await access_admin_home(
            query
        )
        raise ApplicationHandlerStop

    if data == "access:add":
        _ADMIN_PENDING[uid] = {
            "action": "add",
        }

        await query.edit_message_text(
            "Send the batch/course name.\n\n"
            "Example:\n"
            "Jatin Gupta Polity"
        )

        raise ApplicationHandlerStop

    if data == "access:list":
        mappings = list_mappings(
            path
        )

        rows = []

        for m in mappings:
            rows.append([
                InlineKeyboardButton(
                    (
                        "✅ " if m["enabled"]
                        else "⛔ "
                    ) + m["name"][:45],
                    callback_data=f"access:view:{m['id']}",
                )
            ])

        rows.append([
            InlineKeyboardButton(
                "➕ Add Mapping",
                callback_data="access:add",
            )
        ])

        rows.append([
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="access:home",
            )
        ])

        await query.edit_message_text(
            "📋 Access Command Mappings\n\n"
            f"Total: {len(mappings)}",
            reply_markup=InlineKeyboardMarkup(rows),
        )

        raise ApplicationHandlerStop

    parts = data.split(":")

    if len(parts) >= 3 and parts[1] == "view":
        await _show_mapping(
            query,
            path,
            int(parts[2]),
        )
        raise ApplicationHandlerStop

    if len(parts) >= 4 and parts[1] == "edit":
        mapping_id = int(
            parts[2]
        )

        field = parts[3]

        labels = {
            "name": "Send new batch/course name.",
            "aliases": (
                "Send aliases separated by commas.\n\n"
                "Example:\n"
                "jatin polity, jatin gupta polity, polity jatin"
            ),
            "normal_chat_id": (
                "Send the NORMAL private group/channel numeric ID.\n\n"
                "Example:\n"
                "-1001234567890\n\n"
                "The AI bot must be admin there with Invite Users permission."
            ),
            "demo_link": (
                "Send the saved DEMO invite link.\n\n"
                "Example:\n"
                "https://t.me/+xxxx"
            ),
            "pre_chat_id": (
                "Send the PRE private group/channel numeric ID.\n\n"
                "The AI bot must be admin there with Invite Users and Ban Users permissions."
            ),
            "pre_minutes": (
                "Send PRE access duration in minutes.\n\n"
                "Examples: 5, 10, 15"
            ),
        }

        if field not in labels:
            raise ApplicationHandlerStop

        _ADMIN_PENDING[uid] = {
            "action": "edit",
            "mapping_id": mapping_id,
            "field": field,
        }

        await query.edit_message_text(
            labels[field]
        )

        raise ApplicationHandlerStop

    if len(parts) >= 3 and parts[1] == "toggle":
        mapping_id = int(
            parts[2]
        )

        m = get_mapping(
            path,
            mapping_id
        )

        if m:
            update_mapping(
                path,
                mapping_id,
                "enabled",
                0 if m["enabled"] else 1,
            )

        await _show_mapping(
            query,
            path,
            mapping_id,
        )

        raise ApplicationHandlerStop

    if len(parts) >= 3 and parts[1] == "deleteask":
        mapping_id = int(
            parts[2]
        )

        m = get_mapping(
            path,
            mapping_id
        )

        if not m:
            raise ApplicationHandlerStop

        await query.edit_message_text(
            f"Delete mapping?\n\n{html.escape(m['name'])}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Delete",
                        callback_data=f"access:delete:{mapping_id}",
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"access:view:{mapping_id}",
                    ),
                ]
            ])
        )

        raise ApplicationHandlerStop

    if len(parts) >= 3 and parts[1] == "delete":
        mapping_id = int(
            parts[2]
        )

        try:
            delete_mapping(
                path,
                mapping_id
            )

        except Exception as e:
            await query.edit_message_text(
                f"Cannot delete:\n{html.escape(str(e))}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data=f"access:view:{mapping_id}",
                        )
                    ]
                ])
            )

            raise ApplicationHandlerStop

        await access_admin_home(
            query
        )

        raise ApplicationHandlerStop


async def admin_text_input(
    update,
    context,
):
    settings = context.application.bot_data.get(
        "access_settings"
    )

    if settings is None:
        return

    uid = int(
        update.effective_user.id
    )

    if uid not in _admin_ids(settings):
        return

    pending = _ADMIN_PENDING.get(
        uid
    )

    if not pending:
        return

    text = str(
        update.message.text
        or ""
    ).strip()

    path = settings.database_path

    if pending["action"] == "add":
        if not text:
            raise ApplicationHandlerStop

        mapping_id = create_mapping(
            path,
            text
        )

        _ADMIN_PENDING.pop(
            uid,
            None
        )

        await update.message.reply_text(
            "✅ Mapping created.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "Open Mapping",
                        callback_data=f"access:view:{mapping_id}",
                    )
                ]
            ])
        )

        raise ApplicationHandlerStop

    if pending["action"] == "edit":
        mapping_id = int(
            pending["mapping_id"]
        )

        field = pending[
            "field"
        ]

        try:
            if field in {
                "normal_chat_id",
                "pre_chat_id",
            }:
                value = int(
                    text
                )

            elif field == "pre_minutes":
                value = int(
                    text
                )

                if value < 1 or value > 1440:
                    raise ValueError(
                        "PRE duration must be 1–1440 minutes."
                    )

            else:
                value = text

            update_mapping(
                path,
                mapping_id,
                field,
                value,
            )

            _ADMIN_PENDING.pop(
                uid,
                None
            )

            await update.message.reply_text(
                "✅ Saved.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "Open Mapping",
                            callback_data=f"access:view:{mapping_id}",
                        )
                    ]
                ])
            )

        except Exception as e:
            await update.message.reply_text(
                f"Invalid value: {e}"
            )

        raise ApplicationHandlerStop


# ============================================================
# BOT-SIDE ADMIN COMMANDS
#
# Admin may also test from the AI Bot itself.
# Customers cannot invoke these.
# ============================================================

async def bot_admin_access_command(
    update,
    context,
):
    if not update.message or not update.message.text:
        return

    settings = context.application.bot_data.get(
        "access_settings"
    )

    if settings is None:
        return

    uid = int(
        update.effective_user.id
    )

    if uid not in _admin_ids(settings):
        # Hard ignore. No customer-facing error.
        return

    mode, query = _parse_command(
        update.message.text
    )

    if not mode or not query:
        return

    lookup = resolve_mapping(
        settings.database_path,
        query
    )

    if lookup["status"] == "none":
        return

    result = await execute_access(
        application=context.application,
        settings=settings,
        mode=mode,
        query=query,
        customer_chat_id=update.effective_chat.id,
        requested_by=uid,
        main_account_client=None,
    )

    if not result.get("ok"):
        await update.message.reply_text(
            f"Access command failed: {result.get('reason')}"
        )

    raise ApplicationHandlerStop


# ============================================================
# REGISTRATION
# ============================================================

def register_access_link_system(
    application,
    settings,
):
    ensure_schema(
        settings.database_path
    )

    application.bot_data[
        "access_settings"
    ] = settings

    # Trusted internal callable for future AI rules.
    async def _internal_execute(
        *,
        mode,
        query,
        customer_chat_id,
        requested_by=None,
    ):
        main_client = application.bot_data.get(
            "main_account_client"
        )

        return await execute_access(
            application=application,
            settings=settings,
            mode=mode,
            query=query,
            customer_chat_id=customer_chat_id,
            requested_by=requested_by,
            main_account_client=main_client,
        )

    application.bot_data[
        "access_execute"
    ] = _internal_execute

    # Admin UI.
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^access:",
        ),
        group=-50,
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            admin_text_input,
        ),
        group=-49,
    )

    # Admin-only bot slash command path.
    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            bot_admin_access_command,
        ),
        group=-48,
    )

    # Join detector.
    application.add_handler(
        ChatMemberHandler(
            chat_member_update,
            ChatMemberHandler.CHAT_MEMBER,
        ),
        group=-47,
    )

    # Preserve any existing post_init hook.
    old_post_init = getattr(
        application,
        "post_init",
        None
    )

    async def _access_post_init(app):
        if old_post_init:
            await old_post_init(app)

        # One immediate reconciliation after startup.
        await reconcile(
            app,
            settings,
        )

        app.create_task(
            _recovery_loop(
                app,
                settings,
            ),
            name="access-link-recovery",
        )

        logger.info(
            "ACCESS COMMAND SYSTEM V1 READY"
        )

    application.post_init = (
        _access_post_init
    )

    logger.info(
        "ACCESS COMMAND SYSTEM V1 REGISTERED"
    )
