from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path

import aiosqlite

from .conversation_continuity import (
    ContinuityState,
    ACK_NEUTRAL,
    EXPLICIT_TARGET_SWITCH,
    FOLLOWUP_SAME_TOPIC,
    NEW_TOPIC_SAME_TARGET,
    WORKFLOW_INTERRUPT,
    WORKFLOW_RESUME,
    apply_decision,
    detect_topic,
    explicit_target,
    norm,
    resolve,
    target_mentions,
)

from .combo_rules import (
    _v6_context_set,
    _v8_sticky_state_set,
)

from .payment_architecture import (
    _state_clear as _payment_state_clear,
    _state_get as _payment_state_get,
)


logger = logging.getLogger(__name__)

_LOCKS = {}
_SCHEMA_READY = set()
_SCHEMA_LOCK = asyncio.Lock()

SAFE_TOPICS = {
    "price",
    "validity",
    "device",
    "download",
    "download_device",
    "notes",
    "updates",
    "demo",
    "proof",
    "comparison",
    "contents",
    "groups",
    "optional",
    "negotiation",
}

YES_NO_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|haan|han|ha|haa|"
    r"no|nope|nah|nahi|nhi|na)$",
    re.I,
)

PAYMENT_METHOD_RE = re.compile(
    r"\b(?:phone\s*pe|phonepe|gpay|google\s*pay|"
    r"paytm|amazon\s*pay|upi|qr)\b",
    re.I,
)

SPECIFIC_OPTIONAL_RE = re.compile(
    r"\b(?:"
    r"anthropology|sociology|psir|"
    r"geography\s+optional|history\s+optional|"
    r"maths|mathematics|philosophy|psychology|"
    r"public\s+administration|pub\s*ad|"
    r"agriculture|commerce|chemistry"
    r")\b",
    re.I,
)


def _runtime_path(production_db_path: str) -> str:
    return str(
        Path(production_db_path).with_name(
            "combo_continuity_runtime.sqlite3"
        )
    )


def _lock(chat_id: str):
    key = str(chat_id)

    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()

    return _LOCKS[key]


async def _ensure_schema(path: str):
    path = str(path)

    if path in _SCHEMA_READY:
        return

    async with _SCHEMA_LOCK:
        if path in _SCHEMA_READY:
            return

        async with aiosqlite.connect(path) as db:
            await db.execute("PRAGMA journal_mode=WAL")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS runtime_state(
                    chat_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0
                )
            """)

            await db.commit()

        _SCHEMA_READY.add(path)


async def _load_runtime(path: str, chat_id: str) -> ContinuityState:
    await _ensure_schema(path)

    async with aiosqlite.connect(path) as db:
        row = await (
            await db.execute(
                """
                SELECT state_json
                FROM runtime_state
                WHERE chat_id=?
                LIMIT 1
                """,
                (str(chat_id),),
            )
        ).fetchone()

    if not row:
        return ContinuityState()

    try:
        raw = json.loads(row[0] or "{}")

        if isinstance(raw, dict):
            return ContinuityState(**raw)

    except Exception:
        logger.exception(
            "REAL2000 bridge runtime decode failed chat=%s",
            chat_id,
        )

    return ContinuityState()


async def _save_runtime(
    path: str,
    chat_id: str,
    state: ContinuityState,
):
    await _ensure_schema(path)

    raw = json.dumps(
        asdict(state),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO runtime_state(
                chat_id,
                state_json,
                updated_at
            )
            VALUES(?,?,?)
            ON CONFLICT(chat_id)
            DO UPDATE SET
                state_json=excluded.state_json,
                updated_at=excluded.updated_at
            """,
            (
                str(chat_id),
                raw,
                time.time(),
            ),
        )

        await db.commit()


async def clear_combo_continuity_for_normal_batch(
    production_db_path: str,
    chat_id,
):
    """
    SELECTION_CONTEXT_SYNC_V31

    A normal structured/folder batch has just been selected and rendered.
    Remove only stale per-chat Combo continuity ownership so a previous
    Pro Pack / Top Faculty / All Combo cannot hijack the next generic FAQ.

    Saved Combo rules/products and Payment Architecture are untouched.
    """
    cid = str(chat_id)
    runtime_db = _runtime_path(production_db_path)

    async with _lock(cid):
        await _ensure_schema(runtime_db)

        async with aiosqlite.connect(runtime_db, timeout=2) as db:
            await db.execute(
                "DELETE FROM runtime_state WHERE chat_id=?",
                (cid,),
            )
            await db.commit()

        async with aiosqlite.connect(production_db_path, timeout=2) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table'
                      AND name IN ('combo_conversation_context','combo_sticky_state')
                    """
                )
            ).fetchall()
            existing = {str(row[0]) for row in rows}

            if "combo_conversation_context" in existing:
                await db.execute(
                    "DELETE FROM combo_conversation_context WHERE chat_id=?",
                    (cid,),
                )

            if "combo_sticky_state" in existing:
                await db.execute(
                    "DELETE FROM combo_sticky_state WHERE chat_id=?",
                    (cid,),
                )

            await db.commit()

    logger.info(
        "SELECTION_CONTEXT_SYNC_V31 combo reset chat=%s",
        cid,
    )


def _combo_to_continuity(raw: str) -> str:
    n = re.sub(
        r"[\s\-]+",
        "_",
        str(raw or "").strip().casefold(),
    )

    if n in {"pro_pack", "propack"}:
        return "PRO_PACK"

    if n in {"top_faculty", "topfaculty"}:
        return "TOP_FACULTY"

    if n in {"combo", "all_combo", "allcombo"}:
        return "ALL_COMBO"

    return ""


def _continuity_to_combo(target: str) -> tuple[str, str]:
    target = str(target or "")

    if target == "PRO_PACK":
        return "PRO_PACK", "Pro Pack"

    if target == "TOP_FACULTY":
        return "TOP_FACULTY", "Top Faculty"

    if target == "ALL_COMBO":
        return "COMBO", "COMBO"

    return "", ""


async def _production_target(
    production_db_path: str,
    chat_id: str,
) -> str:
    try:
        async with aiosqlite.connect(
            production_db_path,
            timeout=2,
        ) as db:

            row = await (
                await db.execute(
                    """
                    SELECT current_state,active_combo
                    FROM combo_conversation_context
                    WHERE chat_id=?
                    LIMIT 1
                    """,
                    (str(chat_id),),
                )
            ).fetchone()

        if not row:
            return ""

        return _combo_to_continuity(
            row[0] or row[1] or ""
        )

    except Exception:
        logger.exception(
            "REAL2000 bridge context read failed chat=%s",
            chat_id,
        )
        return ""


async def _persist_target(
    production_db_path: str,
    chat_id: str,
    target: str,
    *,
    message: str,
    intent: str,
    combo_stage: str = "SELECTED",
):
    state, active_combo = _continuity_to_combo(target)

    if not state:
        return

    await _v6_context_set(
        production_db_path,
        str(chat_id),
        active_group="combo",
        active_combo=active_combo,
        current_state=state,
        current_intent=str(intent or ""),
        last_customer_message=str(message or ""),
        combo_stage=combo_stage,
        negotiation_count=0,
    )

    if target in {
        "PRO_PACK",
        "TOP_FACULTY",
    }:
        await _v8_sticky_state_set(
            production_db_path,
            str(chat_id),
            target,
        )


def _trained_target(intent: str) -> str:
    x = str(intent or "")

    if x.startswith("TRAINED100|Pro Pack|"):
        return "PRO_PACK"

    if x.startswith("TRAINED100|Top Faculty|"):
        return "TOP_FACULTY"

    if x.startswith("TRAINED100|All Combo|"):
        return "ALL_COMBO"

    return ""


def _trained_number(intent: str) -> int:
    m = re.match(
        r"^TRAINED100\|[^|]+\|(\d{3})\|",
        str(intent or ""),
    )

    return int(m.group(1)) if m else 0


def _match_norm(text: str) -> str:
    x = str(text or "").casefold()

    x = re.sub(
        r"[^\w₹+\-'\s]",
        " ",
        x,
        flags=re.UNICODE,
    )

    return re.sub(r"\s+", " ", x).strip()


async def _find_exact_trained100(
    production_db_path: str,
    message: str,
    preferred_target: str = "",
):
    needle = _match_norm(message)

    if not needle:
        return None

    async with aiosqlite.connect(
        production_db_path,
        timeout=2,
    ) as db:
        db.row_factory = aiosqlite.Row

        rows = await (
            await db.execute(
                """
                SELECT
                    id,
                    intent,
                    reply_en,
                    reply_hi,
                    trigger,
                    keywords,
                    sort_order
                FROM combo_rules
                WHERE enabled=1
                  AND intent LIKE 'TRAINED100|%'
                ORDER BY sort_order,id
                """
            )
        ).fetchall()

    hits = []

    for row in rows:
        aliases = [
            x.strip()
            for x in str(
                row["keywords"] or ""
            ).split("|||")
            if x.strip()
        ]

        # The original training question is also embedded
        # in the intent after the third pipe.
        parts = str(
            row["intent"] or ""
        ).split("|", 3)

        if len(parts) == 4 and parts[3].strip():
            aliases.append(parts[3].strip())

        if any(
            _match_norm(x) == needle
            for x in aliases
        ):
            hits.append(dict(row))

    if not hits:
        return None

    if preferred_target:
        same = [
            r
            for r in hits
            if _trained_target(
                r.get("intent")
            ) == preferred_target
        ]

        if same:
            hits = same

    hits.sort(
        key=lambda r: (
            int(r.get("sort_order") or 0),
            int(r.get("id") or 0),
        )
    )

    return hits[0]


def _special_stage_for_trained(intent: str) -> str:
    q = _trained_number(intent)

    if q in {
        37,
        39,
        67,
        68,
        69,
        70,
        94,
    }:
        return "PRE_OFFERED"

    if q == 98:
        return "AWAITING_OPTIONAL"

    if q == 99:
        return "AWAITING_COMBO"

    return "SELECTED"


def _payment_snapshot(
    production_db_path: str,
    chat_id: str,
) -> dict:
    try:
        value = _payment_state_get(
            production_db_path,
            int(chat_id),
        )

        return dict(value or {})

    except Exception:
        logger.exception(
            "REAL2000 bridge payment snapshot failed chat=%s",
            chat_id,
        )
        return {}


def _sync_payment_workflow(
    state: ContinuityState,
    payment_stage: str,
):
    stage = str(
        payment_stage or ""
    ).strip().upper()

    if not stage:
        if state.workflow_name == "payment":
            state.workflow_name = ""
            state.workflow_step = ""

        if state.suspended_workflow_name == "payment":
            state.suspended_workflow_name = ""
            state.suspended_workflow_step = ""

        if state.pending_purpose.startswith("payment_"):
            state.pending_question = ""
            state.pending_kind = ""
            state.pending_purpose = ""

        return state

    # If payment is already suspended by a side question,
    # do not accidentally reactivate it before resolve().
    if state.suspended_workflow_name == "payment":
        return state

    state.workflow_name = "payment"
    state.workflow_step = stage

    if stage == "CONFIRM_ORDER":
        state.pending_question = "Confirm selected batches and amount?"
        state.pending_kind = "YES_NO"
        state.pending_purpose = "payment_confirm_order"

    elif stage == "HIGH_PHONEPE":
        state.pending_question = "Do you use PhonePe?"
        state.pending_kind = "YES_NO"
        state.pending_purpose = "payment_phonepe"

    elif stage == "HIGH_PHONEPE_ALTERNATIVE":
        state.pending_question = (
            "Can you download PhonePe or use your friend's PhonePe?"
        )
        state.pending_kind = "YES_NO"
        state.pending_purpose = "payment_phonepe_alternative"

    else:
        state.pending_question = ""
        state.pending_kind = ""
        state.pending_purpose = ""

    return state


def _payment_transactional(
    payment_stage: str,
    message: str,
    topic: str,
) -> bool:
    stage = str(
        payment_stage or ""
    ).strip().upper()

    n = norm(message)

    if not stage:
        return False

    if topic == "payment":
        return True

    if stage == "NEED_SELECTION":
        if (
            explicit_target(message)
            or re.search(
                r"\b(?:batch|batches|combo|pack|faculty)\b",
                n,
            )
        ):
            return True

    if stage in {
        "CONFIRM_ORDER",
        "HIGH_PHONEPE",
        "HIGH_PHONEPE_ALTERNATIVE",
    }:
        if YES_NO_RE.fullmatch(n):
            return True

    if stage == "PAYMENT_METHOD":
        if PAYMENT_METHOD_RE.search(n):
            return True

    return False


def _specific_optional_request(message: str) -> bool:
    n = norm(message)

    return bool(
        SPECIFIC_OPTIONAL_RE.search(n)
        or (
            re.search(r"\boptional\b", n)
            and re.search(
                r"\b(?:anthropology|sociology|psir|"
                r"geography|history|maths|mathematics|"
                r"philosophy|psychology|agriculture|"
                r"commerce|chemistry)\b",
                n,
            )
        )
    )


def _answer_for_topic(
    target: str,
    topic: str,
    message: str,
):
    target = str(target or "")
    topic = str(topic or "")

    if topic == "price":
        if target == "PRO_PACK":
            return "₹1499 fix price hai bro.", ""

        if target == "TOP_FACULTY":
            return "₹999 fix price hai bro.", ""

        return (
            "Top Faculty ₹999 hai aur Pro Pack ₹1499 hai bro.",
            "",
        )

    if topic == "validity":
        return "Lifetime access rahega bhai.", ""

    if topic in {
        "device",
        "download_device",
    }:
        return (
            "Haan bhai, same Telegram account se phone aur laptop dono me use kar sakte ho.",
            "",
        )

    if topic == "download":
        return (
            "Haan, Android phone me lectures save ho jayenge bhai.",
            "",
        )

    if topic == "notes":
        return (
            "Haan bhai, lectures ke saath notes aur PDFs bhi rahenge.",
            "",
        )

    if topic == "updates":
        return (
            "Mostly 2026-27 ke updated lectures hain aur free updates Mains 2027 tak milenge.",
            "",
        )

    if topic == "groups":
        if target == "PRO_PACK":
            return (
                "18 groups hain bro, aur groups me multiple topics properly arranged hain.",
                "",
            )

        if target == "TOP_FACULTY":
            return (
                "Top Faculty ek organised group me subject-wise topics aur lectures ke saath rahega.",
                "",
            )

        return (
            "Pro Pack me 18 groups hain; Top Faculty subject-wise organised rahega.",
            "",
        )

    if topic == "optional":
        if target == "PRO_PACK":
            return (
                "Haan bhai, Pro Pack me one Optional subject included hai.",
                "",
            )

        if target == "TOP_FACULTY":
            return (
                "Nahi bro, Top Faculty me Optional included nahi hai.",
                "",
            )

        return (
            "Optional chahiye to Pro Pack lena hoga bro; usme one Optional included hai.",
            "",
        )

    if topic == "negotiation":
        return (
            "Bhai price already lowest hai, discount possible nahi hai.",
            "",
        )

    if topic == "comparison":
        return (
            "Top Faculty me sirf subject-wise Top Faculty teachers rahenge. "
            "Pro Pack me Top Faculty + All Coaching GS + one Optional subject bhi rahega.",
            "",
        )

    if topic == "contents":
        mentioned = explicit_target(message)

        # Definition of another product is an informational
        # reference, not a target switch.
        answer_target = (
            mentioned
            if mentioned in {
                "PRO_PACK",
                "TOP_FACULTY",
            }
            else target
        )

        if answer_target == "PRO_PACK":
            return (
                "Pro Pack me Top Faculty + All Coaching GS + one Optional subject included hai bro.",
                "",
            )

        if answer_target == "TOP_FACULTY":
            return (
                "Top Faculty me subject-wise Top Faculty teachers aur lectures rahenge bro.",
                "",
            )

        return (
            "Do options hain bro: Top Faculty aur Pro Pack.",
            "",
        )

    if topic == "demo":
        return "", "demoall"

    if topic == "proof":
        return "Bilkul bhai, proof dekh lo.", "proof"

    return "", ""


async def _execute_action(
    execute_direct_command_fn,
    update,
    context,
    command: str,
) -> bool:
    if not command:
        return False

    if execute_direct_command_fn is None:
        return False

    command = str(command).strip()

    if command.startswith("/"):
        command = command[1:]

    try:
        return bool(
            await execute_direct_command_fn(
                update,
                context,
                command,
                internal=True,
            )
        )

    except Exception:
        logger.exception(
            "REAL2000 bridge action failed command=%r",
            command,
        )
        return False


async def process_combo_continuity_bridge(
    update,
    context,
    production_db_path: str,
    message: str,
    execute_direct_command_fn=None,
) -> bool:
    chat = getattr(
        update,
        "effective_chat",
        None,
    )

    msg_obj = getattr(
        update,
        "message",
        None,
    )

    if chat is None or msg_obj is None:
        return False

    raw = str(
        message or ""
    ).strip()

    if not raw or len(raw) > 150:
        return False

    chat_id = str(chat.id)
    runtime_db = _runtime_path(
        production_db_path
    )

    async with _lock(chat_id):
        state = await _load_runtime(
            runtime_db,
            chat_id,
        )

        prod_target = await _production_target(
            production_db_path,
            chat_id,
        )

        # Production selection from the previous turn wins.
        if (
            prod_target
            and prod_target != state.active_target
        ):
            state.active_target = prod_target

        preferred = (
            state.active_target
            or prod_target
        )

        # ====================================================
        # 1. Exact TRAINED100 owns its exact human training
        #    question before payment/optional/generic routers.
        # ====================================================

        trained = await _find_exact_trained100(
            production_db_path,
            raw,
            preferred,
        )

        if trained:
            trained_target = _trained_target(
                trained.get("intent")
            )

            # REAL2000_TRAINED_TARGET_PERSISTENCE_V1
            #
            # A TRAINED100 row may be stored under a product because
            # that product's training set contains the question.
            # That storage scope must NOT automatically become the
            # customer's active conversational target.
            #
            # Example:
            #   active target = TOP_FACULTY
            #   "Pro Pack ka matlab kya hai?"
            #
            # The bot should explain Pro Pack, but a definition/reference
            # question must not silently switch the ongoing conversation
            # away from TOP_FACULTY.
            trained_decision = resolve(
                state,
                raw,
            )

            if (
                trained_decision.relationship
                == EXPLICIT_TARGET_SWITCH
                and trained_decision.target
            ):
                context_target = (
                    trained_decision.target
                )

            else:
                context_target = (
                    state.active_target
                    or prod_target
                    or trained_target
                )

            if context_target:
                state.active_target = (
                    context_target
                )

            topic = (
                trained_decision.topic
                or detect_topic(
                    raw,
                    state.active_topic,
                )
            )

            if topic:
                state.previous_topic = (
                    state.active_topic
                )
                state.active_topic = topic

            intent = str(
                trained.get("intent") or ""
            )

            # Special workflow stages belong to the trained product
            # only when that product is also the conversational target.
            # A cross-product informational reference must not start
            # another product's workflow.
            stage = (
                _special_stage_for_trained(
                    intent
                )
                if trained_target == context_target
                else "SELECTED"
            )

            if context_target:
                await _persist_target(
                    production_db_path,
                    chat_id,
                    context_target,
                    message=raw,
                    intent=intent,
                    combo_stage=stage,
                )

            reply = str(
                trained.get("reply_en")
                or trained.get("reply_hi")
                or ""
            ).strip()

            trigger = str(
                trained.get("trigger")
                or ""
            ).strip()

            if reply:
                await msg_obj.reply_text(
                    reply
                )

            action_ok = False

            if trigger:
                action_ok = await _execute_action(
                    execute_direct_command_fn,
                    update,
                    context,
                    trigger,
                )

            await _save_runtime(
                runtime_db,
                chat_id,
                state,
            )

            logger.info(
                "REAL2000_TRAINED100_OWNER "
                "chat=%s intent=%r target=%s "
                "topic=%s trigger=%r action_ok=%s",
                chat_id,
                intent,
                trained_target,
                topic,
                trigger,
                action_ok,
            )

            # A text reply is already definitive. For trigger-only
            # rows, return handled only if the command succeeded.
            return bool(
                reply
                or action_ok
            )

        # Keep Optional catalogue requests out of the Combo bridge.
        if _specific_optional_request(raw):
            return False

        payment = _payment_snapshot(
            production_db_path,
            chat_id,
        )

        payment_stage = str(
            payment.get("stage")
            or ""
        ).strip().upper()

        state = _sync_payment_workflow(
            state,
            payment_stage,
        )

        decision = resolve(
            state,
            raw,
        )

        after = apply_decision(
            state,
            decision,
        )

        # ====================================================
        # 2. Real payment architecture retains ownership of
        #    confirmations/method selections.
        # ====================================================

        if _payment_transactional(
            payment_stage,
            raw,
            decision.topic,
        ):
            await _save_runtime(
                runtime_db,
                chat_id,
                after,
            )

            logger.info(
                "REAL2000_CONTINUITY_DEFER_PAYMENT "
                "chat=%s stage=%s relationship=%s message=%r",
                chat_id,
                payment_stage,
                decision.relationship,
                raw,
            )

            return False

        # ====================================================
        # 3. Explicit factual product switch.
        #    Strong switch invalidates old payment workflow.
        # ====================================================

        if (
            decision.relationship
            == EXPLICIT_TARGET_SWITCH
            and decision.target
        ):
            previous = (
                state.active_target
            )

            if (
                payment_stage
                and previous
                and decision.target != previous
            ):
                try:
                    _payment_state_clear(
                        production_db_path,
                        int(chat_id),
                    )

                    payment_stage = ""

                except Exception:
                    logger.exception(
                        "REAL2000 bridge payment clear failed "
                        "chat=%s",
                        chat_id,
                    )

            await _persist_target(
                production_db_path,
                chat_id,
                decision.target,
                message=raw,
                intent="Continuity explicit target switch",
                combo_stage="SELECTED",
            )

        # ====================================================
        # 4. Acknowledgement stays silent.
        # ====================================================

        if decision.relationship == ACK_NEUTRAL:
            await _save_runtime(
                runtime_db,
                chat_id,
                after,
            )

            logger.info(
                "REAL2000_CONTINUITY_ACK "
                "chat=%s target=%s topic=%s message=%r",
                chat_id,
                after.active_target,
                after.active_topic,
                raw,
            )

            return bool(
                after.active_target
            )

        # ====================================================
        # 5. Safe contextual FAQ/action.
        # ====================================================

        target = (
            decision.target
            or after.active_target
            or state.active_target
        )

        topic = (
            decision.topic
            or after.active_topic
        )

        if (
            target
            and topic in SAFE_TOPICS
            and decision.relationship in {
                EXPLICIT_TARGET_SWITCH,
                FOLLOWUP_SAME_TOPIC,
                NEW_TOPIC_SAME_TARGET,
                WORKFLOW_INTERRUPT,
                WORKFLOW_RESUME,
            }
        ):
            reply, action = _answer_for_topic(
                target,
                topic,
                raw,
            )

            if reply:
                await msg_obj.reply_text(
                    reply
                )

            action_ok = False

            if action:
                action_ok = await _execute_action(
                    execute_direct_command_fn,
                    update,
                    context,
                    action,
                )

            await _save_runtime(
                runtime_db,
                chat_id,
                after,
            )

            logger.info(
                "REAL2000_CONTINUITY_HANDLED "
                "chat=%s relationship=%s "
                "target=%s topic=%s action=%r action_ok=%s "
                "message=%r",
                chat_id,
                decision.relationship,
                target,
                topic,
                action,
                action_ok,
                raw,
            )

            return bool(
                reply
                or action_ok
            )

        await _save_runtime(
            runtime_db,
            chat_id,
            after,
        )

        return False
