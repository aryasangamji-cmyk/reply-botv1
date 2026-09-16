import sqlite3
import time


# ============================================================
# STATE REPLY ANCHOR V1
#
# Stores the Telegram message that established the active state.
#
# Explicit teacher/batch request:
#   anchor = customer's state-setting message
#
# Direct Telegram reply:
#   anchor = original replied-to message
# ============================================================


def _con(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(path):
    with _con(path) as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS customer_state_reply_anchor (
            chat_id TEXT PRIMARY KEY,
            anchor_message_id INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0
        )
        """)
        db.commit()


def set_anchor(path, chat_id, message_id, source="state"):
    ensure_schema(path)

    try:
        message_id = int(message_id or 0)
    except Exception:
        message_id = 0

    if not message_id:
        return

    with _con(path) as db:
        db.execute("""
        INSERT INTO customer_state_reply_anchor
        (
            chat_id,
            anchor_message_id,
            source,
            updated_at
        )
        VALUES(?,?,?,?)

        ON CONFLICT(chat_id) DO UPDATE SET
            anchor_message_id=excluded.anchor_message_id,
            source=excluded.source,
            updated_at=excluded.updated_at
        """, (
            str(chat_id),
            message_id,
            str(source or ""),
            time.time(),
        ))
        db.commit()


def get_anchor(path, chat_id):
    ensure_schema(path)

    with _con(path) as db:
        row = db.execute("""
            SELECT anchor_message_id
            FROM customer_state_reply_anchor
            WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

    if not row:
        return None

    try:
        mid = int(row["anchor_message_id"] or 0)
    except Exception:
        return None

    return mid or None


def clear_anchor(path, chat_id):
    ensure_schema(path)

    with _con(path) as db:
        db.execute(
            "DELETE FROM customer_state_reply_anchor WHERE chat_id=?",
            (str(chat_id),)
        )
        db.commit()


async def send_anchored_reply(
    update,
    context,
    text,
    reply_to_message_id=None,
):
    """
    Main Account customer path:
        use Telethon reply_to=<anchor>

    Anything else:
        fall back to normal reply_text().
    """
    text = str(text or "").strip()

    if not text:
        return None

    is_main_private = bool(
        getattr(
            update,
            "_private_main_account_customer",
            False
        )
    )

    if is_main_private and reply_to_message_id:
        try:
            client = context.application.bot_data.get(
                "main_account_client"
            )

            chat = getattr(
                update,
                "effective_chat",
                None
            )

            chat_id = getattr(
                chat,
                "id",
                None
            )

            if client and chat_id:
                return await client.send_message(
                    int(chat_id),
                    text,
                    reply_to=int(reply_to_message_id),
                )

        except Exception:
            # Never break customer handling merely because Telegram
            # cannot attach a reply relation.
            pass

    return await update.message.reply_text(text)
