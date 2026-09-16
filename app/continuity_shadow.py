"""
CONTINUITY LIVE SHADOW V1

STRICT SHADOW CONTRACT
----------------------
This module:
- does NOT send Telegram messages
- does NOT execute Telegram commands
- does NOT suppress production routing
- does NOT modify production conversation state
- does NOT call OpenAI
- does NOT alter payment state

It observes incoming private customer text, runs the tested
Conversation Continuity V1 resolver, and stores the result in a
SEPARATE SQLite database:

    data/continuity_shadow.sqlite3
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path

import aiosqlite

# REAL2000_SHADOW_PAYMENT_IMPORT_V1
from .payment_architecture import _state_get as _payment_state_get

from .conversation_continuity import (
    ContinuityState,
    resolve,
    apply_decision,
    norm,
    target_mentions,
)

logger = logging.getLogger(__name__)

DEFAULT_SHADOW_DB = (
    "/opt/new-ai-bot/data/continuity_shadow.sqlite3"
)

_SCHEMA_READY = set()
_SCHEMA_LOCK = asyncio.Lock()
_CHAT_LOCKS = {}


def _chat_lock(surface: str, chat_id: str):
    key = (str(surface), str(chat_id))

    lock = _CHAT_LOCKS.get(key)

    if lock is None:
        lock = asyncio.Lock()
        _CHAT_LOCKS[key] = lock

    return lock


async def ensure_schema(
    shadow_db_path: str = DEFAULT_SHADOW_DB,
):
    path = str(shadow_db_path)

    if path in _SCHEMA_READY:
        return

    async with _SCHEMA_LOCK:

        if path in _SCHEMA_READY:
            return

        Path(path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with aiosqlite.connect(
            path,
            timeout=2.0,
        ) as db:

            await db.execute(
                "PRAGMA journal_mode=WAL"
            )

            await db.execute(
                "PRAGMA busy_timeout=2000"
            )

            await db.execute("""
                CREATE TABLE IF NOT EXISTS shadow_state (
                    surface TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    last_production_target TEXT NOT NULL DEFAULT '',
                    last_production_stage TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(surface, chat_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS shadow_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,

                    surface TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0,
                    customer_text TEXT NOT NULL DEFAULT '',

                    in_scope INTEGER NOT NULL DEFAULT 0,

                    production_target_before TEXT NOT NULL DEFAULT '',
                    production_stage_before TEXT NOT NULL DEFAULT '',

                    shadow_target_before TEXT NOT NULL DEFAULT '',
                    shadow_topic_before TEXT NOT NULL DEFAULT '',
                    shadow_workflow_before TEXT NOT NULL DEFAULT '',
                    shadow_suspended_before TEXT NOT NULL DEFAULT '',
                    shadow_pending_before TEXT NOT NULL DEFAULT '',

                    relationship TEXT NOT NULL DEFAULT '',
                    decision_target TEXT NOT NULL DEFAULT '',
                    decision_topic TEXT NOT NULL DEFAULT '',

                    shadow_target_after TEXT NOT NULL DEFAULT '',
                    shadow_topic_after TEXT NOT NULL DEFAULT '',
                    shadow_workflow_after TEXT NOT NULL DEFAULT '',
                    shadow_suspended_after TEXT NOT NULL DEFAULT '',
                    shadow_pending_after TEXT NOT NULL DEFAULT '',

                    confidence TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_shadow_events_chat_time
                ON shadow_events(
                    surface,
                    chat_id,
                    created_at
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_shadow_events_relationship
                ON shadow_events(
                    relationship
                )
            """)

            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_shadow_events_message_unique
                ON shadow_events(
                    surface,
                    chat_id,
                    message_id
                )
                WHERE message_id > 0
            """)

            await db.commit()

        _SCHEMA_READY.add(path)


def _state_from_json(raw: str):
    try:
        obj = json.loads(
            str(raw or "{}")
        )

        if not isinstance(obj, dict):
            return ContinuityState()

        return ContinuityState(**obj)

    except Exception:
        return ContinuityState()


def _state_json(state: ContinuityState):
    return json.dumps(
        asdict(state),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _map_production_target(
    current_state: str,
    active_combo: str,
):
    raw = str(
        current_state
        or active_combo
        or ""
    ).strip()

    n = re.sub(
        r"[\s\-]+",
        "_",
        raw.casefold(),
    )

    if n in {
        "pro_pack",
        "propack",
    }:
        return "PRO_PACK"

    if n in {
        "top_faculty",
        "topfaculty",
    }:
        return "TOP_FACULTY"

    if n in {
        "combo",
        "all_combo",
        "allcombo",
    }:
        return "ALL_COMBO"

    return ""


async def _production_snapshot(
    production_db_path: str,
    chat_id: str,
):
    result = {
        "target": "",
        "stage": "",
        "last_bot_question": "",
    }

    try:
        async with aiosqlite.connect(
            str(production_db_path),
            timeout=0.75,
        ) as db:

            db.row_factory = aiosqlite.Row

            row = await (
                await db.execute(
                    """
                    SELECT
                        current_state,
                        active_combo,
                        combo_stage,
                        last_bot_question
                    FROM combo_conversation_context
                    WHERE chat_id=?
                    LIMIT 1
                    """,
                    (str(chat_id),),
                )
            ).fetchone()

            if row:
                result["target"] = (
                    _map_production_target(
                        row["current_state"],
                        row["active_combo"],
                    )
                )

                result["stage"] = str(
                    row["combo_stage"]
                    or ""
                ).strip().upper()

                result["last_bot_question"] = str(
                    row["last_bot_question"]
                    or ""
                ).strip()

    except Exception:
        # Production DB visibility failure must NEVER
        # affect production customer handling.
        logger.exception(
            "CONTINUITY SHADOW production snapshot failed "
            "chat_id=%s",
            chat_id,
        )

    # REAL2000_SHADOW_REAL_PAYMENT_SYNC_V1
    # Payment Architecture is the authoritative transaction
    # workflow. combo_conversation_context is not sufficient.
    try:
        _pay_state = _payment_state_get(
            production_db_path,
            int(chat_id),
        )

        _pay_stage = str(
            (_pay_state or {}).get("stage")
            or ""
        ).strip().upper()

        if _pay_stage:
            result["stage"] = (
                "PAYMENT:" + _pay_stage
            )

    except Exception:
        logger.exception(
            "CONTINUITY SHADOW real payment snapshot failed "
            "chat_id=%s",
            chat_id,
        )

    return result


def _generic_combo_seed(text: str):
    n = norm(text)

    if re.search(
        r"\b(?:"
        r"combo|"
        r"all\s+combo|"
        r"upsc\s+(?:course|combo|batch)|"
        r"general\s+studies\s+(?:course|combo)|"
        r"gs\s+(?:course|combo|batch|lecture|lectures)|"
        r"course\s+chahiye|"
        r"combo\s+chahiye|"
        r"interested\s+in\s+combo"
        r")\b",
        n,
    ):
        return "ALL_COMBO"

    if re.search(
        r"(?<!\d)(?:999|1499)(?!\d)",
        n,
    ):
        return "ALL_COMBO"

    return ""


def _sync_new_production_workflow(
    state: ContinuityState,
    *,
    stage: str,
    last_stage: str,
    last_bot_question: str,
):
    stage = str(stage or "").upper()
    last_stage = str(last_stage or "").upper()

    # Only synchronize when production has entered a NEW
    # transactional stage since the previous customer message.
    #
    # If stage has not changed, shadow state remains independent.
    if not stage or stage == last_stage:
        return state

    # REAL2000_SHADOW_REAL_PAYMENT_SYNC_V1
    if stage.startswith("PAYMENT:"):
        _ps = stage.split(":", 1)[1]

        state.workflow_name = "payment"
        state.workflow_step = _ps
        state.suspended_workflow_name = ""
        state.suspended_workflow_step = ""

        if _ps == "CONFIRM_ORDER":
            state.pending_question = "Confirm selected batches and amount?"
            state.pending_kind = "YES_NO"
            state.pending_purpose = "payment_confirm_order"

        elif _ps == "HIGH_PHONEPE":
            state.pending_question = "Do you use PhonePe?"
            state.pending_kind = "YES_NO"
            state.pending_purpose = "payment_phonepe"

        elif _ps == "HIGH_PHONEPE_ALTERNATIVE":
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

    if stage == "AWAITING_PAYMENT":
        state.workflow_name = "payment"
        state.workflow_step = "AWAITING_PAYMENT"

        state.pending_question = (
            last_bot_question
            or "Are you paying now?"
        )

        state.pending_kind = "YES_NO"
        state.pending_purpose = "payment_start"

        state.suspended_workflow_name = ""
        state.suspended_workflow_step = ""

    elif stage == "AWAITING_PHONEPE":
        state.workflow_name = "payment"
        state.workflow_step = "AWAITING_PHONEPE"

        state.pending_question = (
            last_bot_question
            or "Do you use PhonePe?"
        )

        state.pending_kind = "YES_NO"
        state.pending_purpose = "payment_phonepe"

        state.suspended_workflow_name = ""
        state.suspended_workflow_step = ""

    elif stage == "PRE_OFFERED":
        state.workflow_name = "pre_access"
        state.workflow_step = "PRE_OFFERED"

        state.pending_question = (
            last_bot_question
            or "Do you want PRE-access?"
        )

        state.pending_kind = "YES_NO"
        state.pending_purpose = "pre_access"

        state.suspended_workflow_name = ""
        state.suspended_workflow_step = ""

    elif stage == "PAYMENT_STARTED":
        state.workflow_name = "payment"
        state.workflow_step = "PAYMENT_STARTED"

        state.pending_question = ""
        state.pending_kind = ""
        state.pending_purpose = ""

    return state


async def observe_raw(
    *,
    surface: str,
    chat_id: int | str,
    message_id: int = 0,
    text: str,
    production_db_path: str,
    shadow_db_path: str = DEFAULT_SHADOW_DB,
):
    """
    Pure shadow observer entrypoint.

    Returns a small result dict for testing/diagnostics.
    It has no Telegram send capability.
    """

    surface = str(surface or "bot")
    chat_id = str(chat_id)

    try:
        message_id = int(message_id or 0)
    except Exception:
        message_id = 0

    text = str(text or "").strip()

    if not text:
        return {
            "observed": False,
            "reason": "empty",
        }

    # Match production Main Account safety behavior.
    if len(text) > 150:
        return {
            "observed": False,
            "reason": "over_150_chars",
        }

    await ensure_schema(
        shadow_db_path
    )

    async with _chat_lock(
        surface,
        chat_id,
    ):

        prod = await _production_snapshot(
            production_db_path,
            chat_id,
        )

        async with aiosqlite.connect(
            shadow_db_path,
            timeout=2.0,
        ) as db:

            db.row_factory = aiosqlite.Row

            if message_id > 0:
                existing = await (
                    await db.execute(
                        """
                        SELECT id
                        FROM shadow_events
                        WHERE surface=?
                          AND chat_id=?
                          AND message_id=?
                        LIMIT 1
                        """,
                        (
                            surface,
                            chat_id,
                            message_id,
                        ),
                    )
                ).fetchone()

                if existing:
                    return {
                        "observed": False,
                        "reason": "duplicate",
                    }

            state_row = await (
                await db.execute(
                    """
                    SELECT
                        state_json,
                        last_production_target,
                        last_production_stage
                    FROM shadow_state
                    WHERE surface=?
                      AND chat_id=?
                    LIMIT 1
                    """,
                    (
                        surface,
                        chat_id,
                    ),
                )
            ).fetchone()

            if state_row:
                state = _state_from_json(
                    state_row["state_json"]
                )

                last_prod_target = str(
                    state_row[
                        "last_production_target"
                    ]
                    or ""
                )

                last_prod_stage = str(
                    state_row[
                        "last_production_stage"
                    ]
                    or ""
                )

            else:
                state = ContinuityState()
                last_prod_target = ""
                last_prod_stage = ""

            # Bootstrap only when shadow has no target yet.
            if not state.active_target:
                state.active_target = (
                    prod["target"]
                    or _generic_combo_seed(text)
                )

            state = _sync_new_production_workflow(
                state,
                stage=prod["stage"],
                last_stage=last_prod_stage,
                last_bot_question=prod[
                    "last_bot_question"
                ],
            )

            before = ContinuityState(
                **asdict(state)
            )

            explicit_mentions = target_mentions(
                text
            )

            in_scope = bool(
                state.active_target
                or prod["target"]
                or explicit_mentions
                or _generic_combo_seed(text)
            )

            if in_scope:
                decision = resolve(
                    state,
                    text,
                )

                after = apply_decision(
                    state,
                    decision,
                )

                relationship = (
                    decision.relationship
                )

                decision_target = (
                    decision.target
                )

                decision_topic = (
                    decision.topic
                )

                confidence = (
                    decision.confidence
                )

                reason = decision.reason

            else:
                after = state

                relationship = (
                    "OUT_OF_SCOPE"
                )

                decision_target = ""
                decision_topic = ""
                confidence = ""
                reason = (
                    "no combo target or combo sales signal"
                )

            await db.execute(
                """
                INSERT INTO shadow_events(
                    created_at,
                    surface,
                    chat_id,
                    message_id,
                    customer_text,
                    in_scope,

                    production_target_before,
                    production_stage_before,

                    shadow_target_before,
                    shadow_topic_before,
                    shadow_workflow_before,
                    shadow_suspended_before,
                    shadow_pending_before,

                    relationship,
                    decision_target,
                    decision_topic,

                    shadow_target_after,
                    shadow_topic_after,
                    shadow_workflow_after,
                    shadow_suspended_after,
                    shadow_pending_after,

                    confidence,
                    reason
                )
                VALUES(
                    ?,?,?,?,?,?,
                    ?,?,
                    ?,?,?,?,?,
                    ?,?,?,
                    ?,?,?,?,?,
                    ?,?
                )
                """,
                (
                    time.time(),
                    surface,
                    chat_id,
                    message_id,
                    text[:150],
                    1 if in_scope else 0,

                    prod["target"],
                    prod["stage"],

                    before.active_target,
                    before.active_topic,
                    before.workflow_name,
                    before.suspended_workflow_name,
                    before.pending_purpose,

                    relationship,
                    decision_target,
                    decision_topic,

                    after.active_target,
                    after.active_topic,
                    after.workflow_name,
                    after.suspended_workflow_name,
                    after.pending_purpose,

                    confidence,
                    reason,
                ),
            )

            await db.execute(
                """
                INSERT INTO shadow_state(
                    surface,
                    chat_id,
                    state_json,
                    last_production_target,
                    last_production_stage,
                    updated_at
                )
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(surface,chat_id)
                DO UPDATE SET
                    state_json=excluded.state_json,
                    last_production_target=
                        excluded.last_production_target,
                    last_production_stage=
                        excluded.last_production_stage,
                    updated_at=excluded.updated_at
                """,
                (
                    surface,
                    chat_id,
                    _state_json(after),
                    prod["target"],
                    prod["stage"],
                    time.time(),
                ),
            )

            await db.commit()

        logger.info(
            "CONTINUITY_SHADOW_V1 "
            "surface=%s chat=%s msg=%s "
            "scope=%s prod_target=%s prod_stage=%s "
            "relationship=%s target=%s topic=%s "
            "confidence=%s",
            surface,
            chat_id,
            message_id,
            int(in_scope),
            prod["target"],
            prod["stage"],
            relationship,
            after.active_target,
            after.active_topic,
            confidence,
        )

        return {
            "observed": True,
            "in_scope": in_scope,
            "relationship": relationship,
            "target": after.active_target,
            "topic": after.active_topic,
            "workflow": after.workflow_name,
            "suspended": (
                after.suspended_workflow_name
            ),
        }


async def observe_update(
    update,
    context,
    production_db_path: str,
):
    """
    Telegram/PTB/MainAccountUpdate wrapper.

    ALL exceptions are swallowed here by design because shadow
    telemetry must never affect customer-facing production logic.
    """

    try:
        message = getattr(
            update,
            "message",
            None,
        )

        if message is None:
            return

        chat = getattr(
            update,
            "effective_chat",
            None,
        )

        if chat is None:
            return

        chat_type = str(
            getattr(
                chat,
                "type",
                "",
            )
            or ""
        ).casefold()

        if chat_type and chat_type != "private":
            return

        text = (
            getattr(
                message,
                "text",
                None,
            )
            or getattr(
                message,
                "caption",
                None,
            )
            or ""
        )

        if not str(text).strip():
            return

        surface = (
            "main_account"
            if bool(
                getattr(
                    update,
                    "_private_main_account_customer",
                    False,
                )
            )
            else "bot"
        )

        message_id = (
            getattr(
                message,
                "message_id",
                None,
            )
            or getattr(
                message,
                "id",
                None,
            )
            or getattr(
                update,
                "_main_account_event_id",
                None,
            )
            or 0
        )

        await observe_raw(
            surface=surface,
            chat_id=getattr(
                chat,
                "id",
            ),
            message_id=message_id,
            text=str(text),
            production_db_path=(
                str(production_db_path)
            ),
        )

    except Exception:
        logger.exception(
            "CONTINUITY_SHADOW_V1 observer failed"
        )
