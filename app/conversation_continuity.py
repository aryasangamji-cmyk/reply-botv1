"""
CONVERSATION CONTINUITY V1

Purpose:
    Resolve what a new customer message refers to.

Important:
    This module DOES NOT send Telegram messages.
    It DOES NOT execute commands.
    It DOES NOT decide business facts.

It only determines:
    - active target continuity
    - topic continuity
    - pending-question answers
    - workflow interruption/resume
    - explicit target switches
    - neutral acknowledgements
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Optional


# ============================================================
# RELATION TYPES
# ============================================================

ANSWER_TO_PENDING = "ANSWER_TO_PENDING"
FOLLOWUP_SAME_TOPIC = "FOLLOWUP_SAME_TOPIC"
NEW_TOPIC_SAME_TARGET = "NEW_TOPIC_SAME_TARGET"
EXPLICIT_TARGET_SWITCH = "EXPLICIT_TARGET_SWITCH"
WORKFLOW_INTERRUPT = "WORKFLOW_INTERRUPT"
WORKFLOW_RESUME = "WORKFLOW_RESUME"
ACK_NEUTRAL = "ACK_NEUTRAL"
UNRESOLVED = "UNRESOLVED"


# ============================================================
# STATE
# ============================================================

@dataclass
class ContinuityState:
    active_target: str = ""
    active_target_type: str = ""
    active_topic: str = ""
    previous_topic: str = ""

    anchor_intent: str = ""
    anchor_rule_id: str = ""
    anchor_bot_reply: str = ""

    pending_question: str = ""
    pending_kind: str = ""
    pending_purpose: str = ""

    workflow_name: str = ""
    workflow_step: str = ""

    suspended_workflow_name: str = ""
    suspended_workflow_step: str = ""

    last_customer_intent: str = ""
    last_bot_action: str = ""
    last_bot_reply: str = ""

    recent_semantic_turns: list = field(default_factory=list)
    confidence: str = "low"


@dataclass
class ContinuityDecision:
    relationship: str
    target: str = ""
    topic: str = ""
    answer_value: str = ""
    consume_pending: bool = False
    confidence: str = "low"
    reason: str = ""


# ============================================================
# NORMALIZATION
# ============================================================

def norm(text: str) -> str:
    text = str(text or "").casefold()

    text = text.replace("’", "'")
    text = text.replace("`", "'")

    text = re.sub(
        r"[^\w₹+\-'\s]",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = re.sub(r"\s+", " ", text).strip()

    return text


def tokens(text: str) -> list[str]:
    return norm(text).split()


# ============================================================
# TARGET DETECTION
# ============================================================

_TARGET_PATTERNS = {
    "PRO_PACK": (
        r"\bpro\s*pack\b",
        r"\bpropack\b",
        r"\bpro\s*combo\b",
    ),

    "TOP_FACULTY": (
        r"\btop\s*faculty\b",
        r"\btopfaculty\b",
        r"\bfaculty\s*combo\b",
    ),

    "ALL_COMBO": (
        r"\ball\s*combo\b",
        r"\ball\s*combos\b",
        r"\ballcombo\b",
    ),
}


def target_mentions(text: str) -> set[str]:
    n = norm(text)

    found = set()

    for target, patterns in _TARGET_PATTERNS.items():
        if any(re.search(p, n) for p in patterns):
            found.add(target)

    return found


def explicit_target(text: str) -> str:
    """
    Only one strong explicit target = switch candidate.

    If Pro Pack and Top Faculty are BOTH mentioned, treat it as
    comparison/context instead of arbitrarily choosing one.
    """

    found = target_mentions(text)

    if len(found) == 1:
        return next(iter(found))

    return ""


# ============================================================
# MESSAGE TYPES
# ============================================================

def answer_value(text: str) -> str:
    n = norm(text)

    # Avoid consuming a polite acknowledgement as YES.
    if re.search(
        r"\b(thanks|thank you|thankyou|shukriya)\b",
        n,
    ):
        return ""

    words = n.split()

    if not words:
        return ""

    yes_words = {
        "haan",
        "han",
        "ha",
        "yes",
        "yeah",
        "yep",
        "yup",
        "ji",
        "sure",
        "okay",
        "ok",
    }

    no_words = {
        "no",
        "nahi",
        "nah",
        "nope",
        "nai",
        "nhi",
    }

    if words[0] in yes_words and len(words) <= 7:
        return "YES"

    if words[0] in no_words and len(words) <= 7:
        return "NO"

    if n in {
        "kar do",
        "bhej do",
        "send it",
        "send kar do",
        "de do",
    }:
        return "YES"

    return ""


def is_ack(text: str) -> bool:
    raw = str(text or "").strip()
    n = norm(raw)

    if raw in {
        "👍",
        "🙏",
        "✅",
        "👌",
        "👍🏻",
        "👍🏼",
        "👍🏽",
        "👍🏾",
        "👍🏿",
    }:
        return True

    patterns = (
        r"^(thanks|thank you|thankyou|thx|shukriya)( bro| bhai)?$",
        r"^(ok|okay) thanks?$",
        r"^(got it|understood)$",
        r"^(accha|acha) samajh gaya$",
        r"^(theek hai|thik hai) thanks?$",
        r"^okay bro thanks$",
    )

    return any(re.search(p, n) for p in patterns)


# ============================================================
# TOPIC DETECTION
# ============================================================

_OPTIONAL_SUBJECT_RE = (
    r"\b("
    r"anthropology|anthro|"
    r"psir|"
    r"maths|mathematics|"
    r"sociology|"
    r"philosophy|"
    r"psychology|"
    r"public\s+administration|pub\s*ad|"
    r"agriculture|"
    r"commerce|"
    r"chemistry"
    r")\b"
)


def detect_topic(
    text: str,
    current_topic: str = "",
) -> str:
    n = norm(text)
    mentions = target_mentions(n)

    # CONTINUITY_V1_PRICE_SHORTHAND_FIX
    # Human shorthand inherits the existing price topic.
    #
    # Example:
    #   Bot/customer discussion: Top Faculty price
    #   Customer: "final?"
    # means final price, not an unrelated unknown message.
    if (
        current_topic == "price"
        and re.fullmatch(
            r"(?:final|final hai|final kya|last|last price|final rate|best price)",
            n,
        )
    ):
        return "price"

    # REAL2000_DEVICE_DONO_PRIORITY_V1
    # "phone aur laptop dono me chalega?" is a DEVICE question,
    # not a product comparison. Product comparison still wins
    # whenever two actual combo targets are mentioned.
    if (
        len(mentions) < 2
        and re.search(
            r"\b(?:android|phone|mobile|laptop|macbook|"
            r"computer|pc|iphone|ios|ipad|tablet|tab)\b",
            n,
        )
    ):
        if current_topic in {
            "download",
            "download_device",
        }:
            return "download_device"

        return "device"

    # Comparison must come before individual target semantics.
    if (
        len(mentions) >= 2
        or re.search(
            r"\b("
            r"difference|different|compare|comparison|"
            r"dono|both|which\s+one|"
            r"better\s+which|"
            r"sasta\s+konsa|complete\s+wala\s+konsa"
            r")\b",
            n,
        )
    ):
        return "comparison"

    if re.search(
        r"\b("
        r"payment|pay|paying|phonepe|phone\s*pe|"
        r"gpay|google\s*pay|upi|qr|gift\s*card|"
        r"payment\s+process|payment\s+procedure"
        r")\b",
        n,
    ):
        return "payment"

    if re.search(
        r"\b("
        r"pre\s*access|preview\s*access|"
        r"five\s*minute|5\s*minute"
        r")\b",
        n,
    ):
        return "pre_access"

    if re.search(
        r"\b("
        r"discount|negotiate|negotiation|"
        r"kam\s+ho|less\s+price|"
        r"lowest\s+price|price\s+kam|"
        r"thoda\s+kam|final\s+rate"
        r")\b",
        n,
    ):
        return "negotiation"

    if re.search(
        r"\b("
        r"validity|valid|lifetime|"
        r"access\s+kab\s+tak|"
        r"kitne\s+time\s+ka\s+access|"
        r"duration"
        r")\b",
        n,
    ):
        return "validity"

    if re.search(
        r"\b("
        r"demo|sample|sample\s+lecture|sample\s+class"
        r")\b",
        n,
    ):
        return "demo"

    if re.search(
        r"\b("
        r"proof|genuine|authentic|original|"
        r"trust|real\s+hai"
        r")\b",
        n,
    ):
        return "proof"

    if re.search(
        r"\b("
        r"download|offline|save\s+kar|"
        r"save\s+ho|downloadable"
        r")\b",
        n,
    ):
        return "download"

    if re.search(
        r"\b("
        r"android|phone|mobile|"
        r"laptop|macbook|computer|pc|"
        r"iphone|ios|ipad|tablet|tab"
        r")\b",
        n,
    ):
        if current_topic in {
            "download",
            "download_device",
        }:
            return "download_device"

        return "device"

    if (
        re.search(
            r"\boptional\b",
            n,
        )
        or re.search(
            _OPTIONAL_SUBJECT_RE,
            n,
        )
    ):
        return "optional"

    if re.search(
        r"\b("
        r"update|updates|updated|latest|"
        r"new\s+lecture|new\s+class|"
        r"2026|2027|2028|"
        r"session\s+year|year\s+ka"
        r")\b",
        n,
    ):
        return "updates"

    if re.search(
        r"\b("
        r"notes|pdf|pdfs|material|materials|"
        r"study\s+material"
        r")\b",
        n,
    ):
        return "notes"

    if re.search(
        r"\b("
        r"group|groups|grp|"
        r"kitne\s+groups|how\s+many\s+groups"
        r")\b",
        n,
    ):
        return "groups"

    # CONTINUITY_V1_EXACT_FIX
    # Currency symbol is non-word; detect it before the word-boundary regex.
    if re.search(r"₹\s*\d+", n):
        return "price"

    if re.search(
        r"\b("
        r"price|cost|fee|fees|"
        r"kitne\s+ka|kitna\s+hai|"
        r"final\s+price|"
        r"₹\s*\d+"
        r")\b",
        n,
    ):
        return "price"

    if re.search(
        r"\b("
        r"included|include|includes|"
        r"kya\s+kya|"
        r"kya\s+milega|"
        r"milta\s+hai|milte\s+hain|"
        r"mil\s+jayega|"
        r"contents?|"
        r"list\s+bhejo|"
        r"list\s+bhej\s+do|"
        r"list\s+send\s+kar|"
        r"combo\s+ka\s+list|"
        r"course\s+ka\s+list|"
        r"exactly\s+kya|"
        r"matlab\s+kya|"
        r"missing|"
        r"faculty|faculties|"
        r"subjects?"
        r")\b",
        n,
    ):
        return "contents"

    if re.search(
        r"\b("
        r"better|best|sahi\s+rahega|sahi\s+hai|"
        r"useful|"
        r"konsa|kaunsa|which\s+combo|"
        r"choose|select|"
        r"chahiye|chaiye|"
        r"lena|join|"
        r"complete\s+combo|complete\s+course|"
        r"course\s+chahiye|combo\s+chahiye|"
        r"combo\s+batao|"
        r"upsc\s+combo\s+batao|"
        r"koi\s+(?:achha|accha|acha|good)\s+(?:upsc\s+)?combo|"
        r"(?:achha|accha|acha|good)\s+upsc\s+combo|"
        r"aage\s+kya"
        r")\b",
        n,
    ):
        return "selection"

    return ""


# ============================================================
# SHORT HUMAN FOLLOW-UP
# ============================================================

def is_short_reference(text: str) -> bool:
    n = norm(text)
    ts = n.split()

    if not ts or len(ts) > 7:
        return False

    return bool(
        re.search(
            r"\b("
            r"aur|also|same|"
            r"wo|woh|ye|yeh|"
            r"usme|isme|"
            r"iska|uska|"
            r"bhi|"
            r"then|phir|"
            r"this|that|"
            r"wala|wali"
            r")\b",
            n,
        )
    )


# ============================================================
# WORKFLOW TOPICS
# ============================================================

def workflow_topic(name: str) -> str:
    n = str(name or "").casefold().strip()

    if n == "payment":
        return "payment"

    if n in {
        "pre",
        "pre_access",
        "preview",
    }:
        return "pre_access"

    if n == "negotiation":
        return "negotiation"

    return ""


# ============================================================
# RESOLVER
# ============================================================

def resolve(
    state: ContinuityState,
    message: str,
    *,
    authoritative_target: str = "",
) -> ContinuityDecision:

    active = str(state.active_target or "")
    target = (
        str(authoritative_target or "").strip()
        or explicit_target(message)
    )

    mentions = target_mentions(message)

    topic = detect_topic(
        message,
        state.active_topic,
    )

    # Product definition/reference must not automatically switch target.
    _definition_reference = bool(
        len(mentions) == 1
        and topic == "contents"
        and re.search(
            r"(?:\bmatlab\s+kya\b|\bwhat\s+is\b|\bmeaning\s+of\b|\bwhat\s+does\b.*\bmean\b|\bmeans?\s+what\b)",
            norm(message),
        )
    )

    # --------------------------------------------------------
    # 1. STRONG EXPLICIT TARGET SWITCH
    # --------------------------------------------------------

    if (
        target
        and active
        and target != active
        and len(mentions) <= 1
        and not _definition_reference
    ):
        return ContinuityDecision(
            relationship=EXPLICIT_TARGET_SWITCH,
            target=target,
            topic=topic,
            confidence="high",
            reason="one strong explicit target differs from active target",
        )

    if target and not active:
        return ContinuityDecision(
            relationship=EXPLICIT_TARGET_SWITCH,
            target=target,
            topic=topic,
            confidence="high",
            reason="strong explicit target establishes conversation target",
        )

    # REAL2000_STRONG_ACK_PRIORITY_V1
    # A social acknowledgement like "okay bro" after a side
    # answer must not be interpreted as YES to an older
    # suspended payment question. Plain "okay" is deliberately
    # not included here because it may genuinely answer Yes/No.
    if re.fullmatch(
        r"(?:"
        r"ok(?:ay)?\s+(?:bro|brother|thanks?|thank\s+you)|"
        r"thanks?(?:\s+(?:bro|brother))?|"
        r"thank\s+you(?:\s+(?:bro|brother))?|"
        r"got\s+it|theek\s+hai\s+thanks?"
        r")",
        norm(message),
    ):
        return ContinuityDecision(
            relationship=ACK_NEUTRAL,
            target=active,
            topic=state.active_topic,
            confidence="high",
            reason="strong social acknowledgement",
        )

    # --------------------------------------------------------
    # 2. PENDING QUESTION ANSWER
    # --------------------------------------------------------

    av = answer_value(message)

    if (
        state.pending_question
        and state.pending_kind
        and av
    ):
        if state.suspended_workflow_name:
            return ContinuityDecision(
                relationship=WORKFLOW_RESUME,
                target=active,
                topic=workflow_topic(
                    state.suspended_workflow_name
                ) or state.active_topic,
                answer_value=av,
                consume_pending=True,
                confidence="high",
                reason="answer belongs to pending question and resumes suspended workflow",
            )

        return ContinuityDecision(
            relationship=ANSWER_TO_PENDING,
            target=active,
            topic=state.active_topic,
            answer_value=av,
            consume_pending=True,
            confidence="high",
            reason="short answer bound to most recent pending question",
        )

    # --------------------------------------------------------
    # 3. ACKNOWLEDGEMENT = NO STATE DAMAGE
    # --------------------------------------------------------

    if is_ack(message):
        return ContinuityDecision(
            relationship=ACK_NEUTRAL,
            target=active,
            topic=state.active_topic,
            confidence="high",
            reason="neutral acknowledgement",
        )

    # --------------------------------------------------------
    # 4. SUSPENDED WORKFLOW EXPLICIT RETURN
    # --------------------------------------------------------

    if state.suspended_workflow_name:
        sw_topic = workflow_topic(
            state.suspended_workflow_name
        )

        if topic and topic == sw_topic:
            return ContinuityDecision(
                relationship=WORKFLOW_RESUME,
                target=active,
                topic=topic,
                confidence="high",
                reason="customer returned to suspended workflow topic",
            )

    # --------------------------------------------------------
    # 5. ACTIVE WORKFLOW INTERRUPTED BY SIDE QUESTION
    # --------------------------------------------------------

    if state.workflow_name:
        wtopic = workflow_topic(
            state.workflow_name
        )

        if (
            topic
            and wtopic
            and topic != wtopic
        ):
            return ContinuityDecision(
                relationship=WORKFLOW_INTERRUPT,
                target=active,
                topic=topic,
                confidence="high",
                reason="side question temporarily interrupts active workflow",
            )

    # --------------------------------------------------------
    # 6. NEW TOPIC / SAME TARGET
    # --------------------------------------------------------

    if active and topic:
        if topic == state.active_topic:
            return ContinuityDecision(
                relationship=FOLLOWUP_SAME_TOPIC,
                target=active,
                topic=topic,
                confidence="high",
                reason="topic and target both continue",
            )

        return ContinuityDecision(
            relationship=NEW_TOPIC_SAME_TARGET,
            target=active,
            topic=topic,
            confidence="high",
            reason="new topic inherits active target",
        )

    # --------------------------------------------------------
    # 7. AMBIGUOUS SHORT REFERENCE
    # --------------------------------------------------------

    if (
        active
        and is_short_reference(message)
    ):
        return ContinuityDecision(
            relationship=FOLLOWUP_SAME_TOPIC,
            target=active,
            topic=state.active_topic,
            confidence="medium",
            reason="short reference inherits active target and anchor topic",
        )

    # --------------------------------------------------------
    # 8. MULTI-TARGET COMPARISON
    # --------------------------------------------------------

    if active and len(mentions) >= 2:
        return ContinuityDecision(
            relationship=NEW_TOPIC_SAME_TARGET,
            target=active,
            topic="comparison",
            confidence="high",
            reason="multiple product mentions are comparison, not target switch",
        )

    # --------------------------------------------------------
    # 9. NO RELIABLE CONTINUITY
    # --------------------------------------------------------

    return ContinuityDecision(
        relationship=UNRESOLVED,
        target=active,
        topic=topic,
        confidence="low",
        reason="insufficient trusted continuity signal",
    )


# ============================================================
# APPLY DECISION
# ============================================================

def apply_decision(
    state: ContinuityState,
    decision: ContinuityDecision,
) -> ContinuityState:

    s = replace(state)

    rel = decision.relationship

    if rel == ACK_NEUTRAL:
        return s

    if rel == EXPLICIT_TARGET_SWITCH:
        s.active_target = decision.target
        s.previous_topic = s.active_topic
        s.active_topic = decision.topic

        # Target switch invalidates target-specific workflows.
        s.pending_question = ""
        s.pending_kind = ""
        s.pending_purpose = ""

        s.workflow_name = ""
        s.workflow_step = ""

        s.suspended_workflow_name = ""
        s.suspended_workflow_step = ""

        s.confidence = decision.confidence

        return s

    if rel == WORKFLOW_INTERRUPT:
        s.previous_topic = s.active_topic
        s.active_topic = decision.topic

        if s.workflow_name:
            s.suspended_workflow_name = s.workflow_name
            s.suspended_workflow_step = s.workflow_step

            s.workflow_name = ""
            s.workflow_step = ""

        s.confidence = decision.confidence

        return s

    if rel == WORKFLOW_RESUME:
        if s.suspended_workflow_name:
            s.workflow_name = s.suspended_workflow_name
            s.workflow_step = s.suspended_workflow_step

            s.suspended_workflow_name = ""
            s.suspended_workflow_step = ""

        if decision.topic:
            s.previous_topic = s.active_topic
            s.active_topic = decision.topic

        if decision.consume_pending:
            s.pending_question = ""
            s.pending_kind = ""
            s.pending_purpose = ""

        s.confidence = decision.confidence

        return s

    if rel == ANSWER_TO_PENDING:
        if decision.consume_pending:
            s.pending_question = ""
            s.pending_kind = ""
            s.pending_purpose = ""

        s.confidence = decision.confidence

        return s

    if rel in {
        FOLLOWUP_SAME_TOPIC,
        NEW_TOPIC_SAME_TARGET,
    }:
        if decision.target:
            s.active_target = decision.target

        if decision.topic:
            if decision.topic != s.active_topic:
                s.previous_topic = s.active_topic

            s.active_topic = decision.topic

        s.confidence = decision.confidence

        return s

    return s


def with_pending(
    state: ContinuityState,
    *,
    question: str,
    kind: str,
    purpose: str,
) -> ContinuityState:

    s = replace(state)

    s.pending_question = question
    s.pending_kind = kind
    s.pending_purpose = purpose

    return s


def with_workflow(
    state: ContinuityState,
    *,
    name: str,
    step: str,
) -> ContinuityState:

    s = replace(state)

    s.workflow_name = name
    s.workflow_step = step

    return s
