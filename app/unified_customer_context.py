import json
import re
import sqlite3
import time
import logging
from difflib import SequenceMatcher

from app.state_reply_anchor import (
    get_anchor as _state_reply_get_anchor,
    set_anchor as _state_reply_set_anchor,
    send_anchored_reply as _send_anchored_reply,
)
from app.new_batch_access import execute_new_batch_access

logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

STATE_TTL_SECONDS = 30 * 60


# ============================================================
# NORMALIZATION
# ============================================================

def _norm(value):
    value = str(value or "").casefold()

    value = (
        value
        .replace("’", "'")
        .replace("–", "-")
        .replace("—", "-")
    )

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    # ========================================================
    # MATCHING_HONORIFIC_NORMALIZATION_V6
    #
    # These transformations are ONLY for matching.
    # Database/display names remain untouched.
    # ========================================================

    # Common spelling variations.
    value = re.sub(
        r"\bmahapatra\b",
        "mohapatra",
        value,
    )

    value = re.sub(
        r"\bbajiram\b",
        "vajiram",
        value,
    )

    value = re.sub(
        r"\bsudharshan\b",
        "sudarshan",
        value,
    )

    # Honorifics carry no identity information.
    #
    # "Mohapatra Sir Sociology"
    # "Mohapatra Sociology"
    #
    # must normalize identically.
    value = re.sub(
        r"\b(?:"
        r"sir|"
        r"mam|"
        r"maam|"
        r"madam|"
        r"miss|"
        r"mr|"
        r"mrs|"
        r"ms"
        r")\b",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value

def _tokens(value):
    return set(
        _norm(value).split()
    )


def _row_value(row, *names):
    for name in names:
        if name in row.keys():
            value = row[name]

            if value not in (
                None,
                "",
            ):
                return value

    return ""


def _aliases(value):
    raw = str(
        value
        or ""
    )

    return [
        x.strip()
        for x in re.split(
            r"[,;\n|]+",
            raw,
        )
        if x.strip()
    ]


# ============================================================
# DATABASE
# ============================================================

def _con(path):
    con = sqlite3.connect(
        path,
        timeout=20,
    )

    con.row_factory = sqlite3.Row

    con.execute(
        "PRAGMA busy_timeout=20000"
    )

    return con


def ensure_schema(path):
    with _con(path) as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS
            unified_customer_context (
                chat_id TEXT PRIMARY KEY,

                active_targets_json
                    TEXT NOT NULL DEFAULT '[]',

                pending_intent
                    TEXT NOT NULL DEFAULT '',

                pending_unknown
                    TEXT NOT NULL DEFAULT '',

                started_at
                    REAL NOT NULL DEFAULT 0,

                last_active_at
                    REAL NOT NULL DEFAULT 0,

                message_count
                    INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.commit()


# ============================================================
# TARGET LOADING
# ============================================================

def _structured_targets(db):
    result = []

    try:
        teachers = {
            int(r["id"]): dict(r)
            for r in db.execute("""
                SELECT *
                FROM structured_batch_teachers
                WHERE enabled=1
            """).fetchall()
        }

        subjects = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM structured_batch_subjects
                WHERE enabled=1
            """).fetchall()
        ]

    except Exception:
        return result

    for subject in subjects:

        teacher = teachers.get(
            int(
                subject.get(
                    "teacher_id"
                )
            )
        )

        if not teacher:
            continue

        teacher_name = str(
            teacher.get("name")
            or ""
        ).strip()

        subject_name = str(
            subject.get("name")
            or ""
        ).strip()

        if not teacher_name or not subject_name:
            continue

        name = (
            teacher_name
            + " — "
            + subject_name
        )

        phrases = [
            teacher_name + " " + subject_name,
            name,
        ]

        teacher_aliases = _aliases(
            teacher.get(
                "aliases"
            )
        )

        subject_aliases = _aliases(
            subject.get(
                "aliases"
            )
        )

        for ta in teacher_aliases:
            phrases.append(
                ta
                + " "
                + subject_name
            )

        for sa in subject_aliases:
            phrases.append(
                teacher_name
                + " "
                + sa
            )

        for ta in teacher_aliases:
            for sa in subject_aliases:
                phrases.append(
                    ta
                    + " "
                    + sa
                )

        institute = str(
            _row_value(
                teacher,
                "institute",
                "coaching",
            )
            or ""
        ).strip()

        if institute:
            phrases.extend([
                teacher_name
                + " "
                + institute
                + " "
                + subject_name,

                institute
                + " "
                + teacher_name
                + " "
                + subject_name,
            ])

        result.append({
            "source":
                "structured",

            "key":
                "subject:"
                + str(subject["id"]),

            "teacher_id":
                int(teacher["id"]),

            "subject_id":
                int(subject["id"]),

            "teacher":
                teacher_name,

            "subject":
                subject_name,

            "name":
                name,

            "price":
                str(
                    subject.get("price")
                    or ""
                ),

            "year":
                str(
                    _row_value(
                        subject,
                        "year",
                        "years",
                        "session",
                    )
                    or ""
                ),

            "institute":
                institute,

            "availability":
                int(
                    subject.get(
                        "availability",
                        1,
                    )
                    or 0
                ),

            "notes":
                str(
                    _row_value(
                        subject,
                        "notes",
                        "details",
                        "description",
                        "special_rule",
                    )
                    or ""
                ),

            "phrases":
                list(
                    dict.fromkeys(
                        x
                        for x in phrases
                        if _norm(x)
                    )
                ),
        })

    return result


def _folder_targets(db):
    result = []

    try:
        folders = {
            int(r["id"]): dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_folders
                WHERE enabled=1
            """).fetchall()
        }

        batches = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_folder_batches
                WHERE enabled=1
            """).fetchall()
        ]

        parts = [
            dict(r)
            for r in db.execute("""
                SELECT *
                FROM new_batch_folder_parts
                WHERE enabled=1
            """).fetchall()
        ]

    except Exception:
        return result

    batch_map = {
        int(b["id"]): b
        for b in batches
    }

    # ========================================================
    # FOLDER_CHILD_TEACHER_INHERIT_V4
    #
    # A folder batch can have teacher names stored only inside
    # its child parts.
    #
    # Example:
    #   Vajiram Sociology
    #       Foundation Sociology — Mohapatra Sir
    #       QEP Sociology        — Mohapatra Sir
    #
    # Customer:
    #   "Mahapatra sir Sociology"
    #
    # must resolve to the parent:
    #   Vajiram Sociology
    # ========================================================

    child_teachers = {}

    for _part in parts:

        _batch_id = int(
            _part.get("batch_id")
            or 0
        )

        _teacher = str(
            _part.get("teacher")
            or ""
        ).strip()

        if not _batch_id or not _teacher:
            continue

        child_teachers.setdefault(
            _batch_id,
            []
        )

        if _teacher not in child_teachers[_batch_id]:
            child_teachers[_batch_id].append(
                _teacher
            )

    for batch in batches:

        folder = folders.get(
            int(
                batch.get(
                    "folder_id"
                )
            )
        )

        if not folder:
            continue

        name = str(
            batch.get("name")
            or ""
        ).strip()

        if not name:
            continue

        teacher = str(
            batch.get("teacher")
            or ""
        ).strip()

        institute = str(
            batch.get("institute")
            or ""
        ).strip()

        folder_name = str(
            folder.get("name")
            or ""
        ).strip()

        phrases = [
            name,
        ]

        phrases.extend(
            _aliases(
                batch.get(
                    "aliases"
                )
            )
        )

        if teacher:
            phrases.append(
                teacher
                + " "
                + folder_name
            )

        # Child-part teachers also identify the parent batch.
        #
        # Mohapatra Sir + Sociology
        # → Vajiram Sociology
        for _child_teacher in child_teachers.get(
            int(batch["id"]),
            [],
        ):
            phrases.extend([
                _child_teacher
                + " "
                + folder_name,

                folder_name
                + " "
                + _child_teacher,

                _child_teacher
                + " "
                + name,
            ])

        if institute:
            phrases.append(
                institute
                + " "
                + name
            )

        result.append({
            "source":
                "folder_batch",

            "key":
                "folder_batch:"
                + str(batch["id"]),

            "folder_id":
                int(folder["id"]),

            "folder_name":
                folder_name,

            "batch_id":
                int(batch["id"]),

            "teacher":
                teacher,

            "subject":
                folder_name,

            "name":
                name,

            "price":
                str(
                    batch.get("price")
                    or ""
                ),

            "year":
                str(
                    batch.get("year")
                    or ""
                ),

            "institute":
                institute,

            "availability":
                int(
                    batch.get(
                        "availability",
                        1,
                    )
                    or 0
                ),

            "notes":
                str(
                    batch.get("notes")
                    or ""
                ),

            "phrases":
                list(
                    dict.fromkeys(
                        x
                        for x in phrases
                        if _norm(x)
                    )
                ),
        })


    for part in parts:

        parent = batch_map.get(
            int(
                part.get(
                    "batch_id"
                )
            )
        )

        if not parent:
            continue

        folder = folders.get(
            int(
                parent.get(
                    "folder_id"
                )
            )
        )

        if not folder:
            continue

        parent_name = str(
            parent.get("name")
            or ""
        ).strip()

        part_name = str(
            part.get("name")
            or ""
        ).strip()

        if not part_name:
            continue

        name = (
            parent_name
            + " — "
            + part_name
        )

        phrases = [
            name,
            parent_name
            + " "
            + part_name,
        ]

        phrases.extend(
            _aliases(
                part.get(
                    "aliases"
                )
            )
        )

        teacher = str(
            part.get("teacher")
            or parent.get("teacher")
            or ""
        ).strip()

        folder_name = str(
            folder.get("name")
            or ""
        ).strip()

        if teacher:
            phrases.append(
                teacher
                + " "
                + folder_name
            )

            phrases.append(
                teacher
                + " "
                + part_name
            )

        result.append({
            "source":
                "folder_part",

            "key":
                "folder_part:"
                + str(part["id"]),

            "folder_id":
                int(folder["id"]),

            "folder_name":
                folder_name,

            "batch_id":
                int(parent["id"]),

            "part_id":
                int(part["id"]),

            "teacher":
                teacher,

            "subject":
                folder_name,

            "name":
                name,

            "price":
                str(
                    part.get("price")
                    or ""
                ),

            "year":
                str(
                    _row_value(
                        part,
                        "years",
                        "year",
                    )
                    or ""
                ),

            "institute":
                str(
                    parent.get("institute")
                    or ""
                ),

            "availability":
                int(
                    part.get(
                        "availability",
                        1,
                    )
                    or 0
                ),

            "notes":
                str(
                    part.get("notes")
                    or ""
                ),

            "phrases":
                list(
                    dict.fromkeys(
                        x
                        for x in phrases
                        if _norm(x)
                    )
                ),
        })

    return result


def _all_targets(path):
    ensure_schema(path)

    with _con(path) as db:
        base_targets = (
            _structured_targets(db)
            + _folder_targets(db)
        )

    # CONTEXT_BUSINESS_RULES_V12
    # Combos are also genuine sellable state targets.
    return (
        base_targets
        + _v12_combo_targets(path)
    )


# ============================================================
# MULTI-TARGET SPLITTING
# ============================================================

def _segments(text):
    raw = str(
        text
        or ""
    )

    # Protect subject names containing conjunctions.
    protections = {
        "science and tech":
            "science__and__tech",

        "science & tech":
            "science__and__tech",

        "science and technology":
            "science__and__technology",

        "art and culture":
            "art__and__culture",
    }

    protected = raw

    for src, dst in protections.items():
        protected = re.sub(
            re.escape(src),
            dst,
            protected,
            flags=re.I,
        )

    parts = re.split(
        r"\s*(?:"
        r"\band\b|"
        r"\baur\b|"
        r"\+|"
        r",|"
        r"\n"
        r")\s*",
        protected,
        flags=re.I,
    )

    result = []

    for part in parts:

        part = (
            part
            .replace(
                "science__and__tech",
                "science and tech",
            )
            .replace(
                "science__and__technology",
                "science and technology",
            )
            .replace(
                "art__and__culture",
                "art and culture",
            )
            .strip()
        )

        if part:
            result.append(
                part
            )

    return (
        result
        or [raw]
    )


# ============================================================
# MATCHING
# ============================================================

def _phrase_score(query, phrase):
    q = _norm(query)
    p = _norm(phrase)

    if not q or not p:
        return 0

    if q == p:
        return 100000 + len(p)

    q_tokens = _tokens(q)
    p_tokens = _tokens(p)

    if not p_tokens:
        return 0

    if p in q:
        base = 20000 + len(p) * 20

        # Specific multi-word target beats generic subject.
        if len(p_tokens) >= 4:
            base += 6000
        elif len(p_tokens) == 3:
            base += 4000
        elif len(p_tokens) == 2:
            base += 1500
        else:
            base += 10

        return base

    if p_tokens.issubset(
        q_tokens
    ):
        base = 8000 + len(p_tokens) * 500

        if len(p_tokens) >= 3:
            base += 2000

        return base

    return 0


# DIRECT_TARGET_METADATA_MATCH_V5

def _target_score(
    query,
    target,
):
    """
    Strong deterministic scoring from actual DB metadata.

    Explicit teacher + subject/folder always outranks a
    generic subject match.

    Examples:
      Mahapatra Sir Sociology
        -> Vajiram Sociology

      Jatin Gupta Polity
        -> Jatin Gupta Polity

      Sudharshan Gurjar Geography
        -> Sudarshan Gurjar Geography
    """

    q = _norm(
        query
    )

    if not q:
        return 0

    best = 0

    name = str(
        target.get("name")
        or ""
    ).strip()

    teacher = str(
        target.get("teacher")
        or ""
    ).strip()

    subject = str(
        target.get("subject")
        or ""
    ).strip()

    folder = str(
        target.get("folder_name")
        or ""
    ).strip()

    institute = str(
        target.get("institute")
        or ""
    ).strip()


    # --------------------------------------------------------
    # Full canonical saved target name
    # --------------------------------------------------------

    if name:
        score = _phrase_score(
            query,
            name,
        )

        if score:
            best = max(
                best,
                score + 10000,
            )


    # --------------------------------------------------------
    # Teacher + subject
    # --------------------------------------------------------

    if teacher and subject:

        combinations = [
            teacher + " " + subject,
            subject + " " + teacher,
        ]

        for phrase in combinations:

            p = _norm(
                phrase
            )

            if not p:
                continue

            if p in q:
                best = max(
                    best,
                    90000 + len(p),
                )

            else:
                pt = set(
                    p.split()
                )

                qt = set(
                    q.split()
                )

                if (
                    len(pt) >= 2
                    and pt.issubset(qt)
                ):
                    best = max(
                        best,
                        85000 + len(pt),
                    )


    # --------------------------------------------------------
    # Teacher + folder
    #
    # Critical for Optional folders:
    #
    # Mohapatra Sir + Sociology
    # -> Vajiram Sociology
    # --------------------------------------------------------

    if teacher and folder:

        combinations = [
            teacher + " " + folder,
            folder + " " + teacher,
        ]

        for phrase in combinations:

            p = _norm(
                phrase
            )

            if not p:
                continue

            if p in q:
                best = max(
                    best,
                    95000 + len(p),
                )

            else:
                pt = set(
                    p.split()
                )

                qt = set(
                    q.split()
                )

                if (
                    len(pt) >= 2
                    and pt.issubset(qt)
                ):
                    best = max(
                        best,
                        90000 + len(pt),
                    )


    # --------------------------------------------------------
    # Teacher + exact saved batch name
    # --------------------------------------------------------

    if teacher and name:

        phrase = (
            teacher
            + " "
            + name
        )

        p = _norm(
            phrase
        )

        if p and p in q:
            best = max(
                best,
                100000 + len(p),
            )


    # --------------------------------------------------------
    # Institute + batch name
    # --------------------------------------------------------

    if institute and name:

        for phrase in (
            institute + " " + name,
            name + " " + institute,
        ):

            p = _norm(
                phrase
            )

            if p and p in q:
                best = max(
                    best,
                    80000 + len(p),
                )


    # --------------------------------------------------------
    # Existing saved aliases remain supported.
    # --------------------------------------------------------

    for phrase in target.get(
        "phrases",
        [],
    ):

        score = _phrase_score(
            query,
            phrase,
        )

        if score:
            best = max(
                best,
                score,
            )


    return best

# PRECISE_TARGET_TIE_BREAK_V7

def _resolve_one(
    targets,
    text,
):
    """
    Resolve ONE customer segment to ONE DB target.

    Rules:
      1. Exact saved name/alias wins.
      2. Longer specific alias wins over generic subject.
      3. Same folder/teacher ambiguity:
         parent folder_batch wins over folder_part.
      4. Truly generic ambiguous subject remains unresolved.
    """

    q = _norm(text)

    if not q:
        return None

    scored = []

    source_priority = {
        "folder_batch": 40,
        "structured": 35,
        "folder_part": 20,
    }

    for index, target in enumerate(
        targets
    ):
        score = _target_score(
            text,
            target,
        )

        if not score:
            continue

        exact_rank = 0
        phrase_specificity = 0

        phrases = []

        name = str(
            target.get("name")
            or ""
        ).strip()

        if name:
            phrases.append(
                name
            )

        phrases.extend(
            target.get(
                "phrases",
                []
            )
            or []
        )

        for phrase in phrases:

            p = _norm(
                phrase
            )

            if not p:
                continue

            words = len(
                p.split()
            )

            # Exact alias/name.
            if q == p:
                exact_rank = max(
                    exact_rank,
                    1000,
                )

                phrase_specificity = max(
                    phrase_specificity,
                    words,
                )

            # Explicit alias contained inside a natural sentence:
            #
            # "Mahapatra sir sociology optional chahiye"
            #
            # contains:
            # "mahapatra sociology optional"
            elif (
                words >= 2
                and p in q
            ):
                exact_rank = max(
                    exact_rank,
                    700,
                )

                phrase_specificity = max(
                    phrase_specificity,
                    words,
                )

            else:
                pt = set(
                    p.split()
                )

                qt = set(
                    q.split()
                )

                if (
                    len(pt) >= 2
                    and pt.issubset(qt)
                ):
                    exact_rank = max(
                        exact_rank,
                        500,
                    )

                    phrase_specificity = max(
                        phrase_specificity,
                        len(pt),
                    )

        scored.append({
            "score":
                int(score),

            "exact_rank":
                exact_rank,

            "specificity":
                phrase_specificity,

            "source_priority":
                source_priority.get(
                    str(
                        target.get(
                            "source"
                        )
                        or ""
                    ),
                    0,
                ),

            "index":
                index,

            "target":
                target,
        })

    if not scored:
        return None


    # --------------------------------------------------------
    # FIRST: exact/specific saved aliases.
    # --------------------------------------------------------

    scored.sort(
        key=lambda x: (
            x["exact_rank"],
            x["specificity"],
            x["score"],
            x["source_priority"],
            -x["index"],
        ),
        reverse=True,
    )

    best = scored[0]


    # Strong saved alias/name:
    # accept it directly.
    if best["exact_rank"] >= 700:

        # If multiple candidates have the same strong alias,
        # prefer parent folder_batch over child folder_part.
        strong = [
            x
            for x in scored
            if (
                x["exact_rank"]
                == best["exact_rank"]
                and x["specificity"]
                == best["specificity"]
            )
        ]

        strong.sort(
            key=lambda x: (
                x["source_priority"],
                x["score"],
                -x["index"],
            ),
            reverse=True,
        )

        return dict(
            strong[0]["target"]
        )


    # --------------------------------------------------------
    # SECOND: normal metadata scoring.
    # --------------------------------------------------------

    scored.sort(
        key=lambda x: (
            x["score"],
            x["source_priority"],
            x["specificity"],
            -x["index"],
        ),
        reverse=True,
    )

    best = scored[0]

    if len(scored) == 1:
        return dict(
            best["target"]
        )

    second = scored[1]


    # --------------------------------------------------------
    # SAME FOLDER / SAME TEACHER / SAME SUBJECT:
    #
    # Parent batch should win over its component part.
    #
    # Example:
    # Mohapatra Sociology
    #
    # parent:
    #   Vajiram Sociology
    #
    # children:
    #   Foundation Sociology
    #   QEP Sociology
    # --------------------------------------------------------

    top_score = best["score"]

    tied = [
        x
        for x in scored
        if x["score"] == top_score
    ]

    if len(tied) > 1:

        folder_batches = [
            x
            for x in tied
            if str(
                x["target"].get(
                    "source"
                )
                or ""
            ) == "folder_batch"
        ]

        if folder_batches:

            parent = folder_batches[0][
                "target"
            ]

            parent_folder = str(
                parent.get(
                    "folder_id"
                )
                or ""
            )

            parent_teacher = _norm(
                parent.get(
                    "teacher"
                )
            )

            parent_subject = _norm(
                parent.get(
                    "subject"
                )
            )

            compatible = True

            for item in tied:

                target = item[
                    "target"
                ]

                source = str(
                    target.get(
                        "source"
                    )
                    or ""
                )

                if source not in (
                    "folder_batch",
                    "folder_part",
                ):
                    compatible = False
                    break

                if (
                    str(
                        target.get(
                            "folder_id"
                        )
                        or ""
                    )
                    != parent_folder
                ):
                    compatible = False
                    break

                t_teacher = _norm(
                    target.get(
                        "teacher"
                    )
                )

                t_subject = _norm(
                    target.get(
                        "subject"
                    )
                )

                if (
                    parent_teacher
                    and t_teacher
                    and parent_teacher
                    != t_teacher
                ):
                    compatible = False
                    break

                if (
                    parent_subject
                    and t_subject
                    and parent_subject
                    != t_subject
                ):
                    compatible = False
                    break

            if compatible:
                return dict(
                    parent
                )


    # --------------------------------------------------------
    # REAL AMBIGUITY:
    #
    # "Sociology"
    # "Polity"
    # "Geography"
    #
    # must not randomly choose a teacher.
    # --------------------------------------------------------

    if (
        best["score"]
        == second["score"]
    ):
        return None

    if (
        best["score"] < 20000
        and best["score"]
        - second["score"]
        < 2500
    ):
        return None

    return dict(
        best["target"]
    )

# ============================================================
# RESOLVER_REPAIR_V25 — HIGH-CONFIDENCE CATALOGUE PRECEDENCE
# ============================================================

_V25_SELECTION_FILLERS = {
    "i", "me", "my", "need", "needs", "want", "wants", "wanted",
    "please", "plz", "bhai", "bro", "mujhe", "mereko",
    "ka", "ki", "ke", "ko", "se",
    "course", "batch", "lecture", "lectures", "class", "classes",
    "latest", "new", "updated", "update",
    "chahiye", "chahie", "chaiye", "lena", "leni", "join", "buy",
    "purchase", "send", "show", "do", "de", "dedo",
}

def _v25_selection_core(value):
    """
    Normalize a target-selection sentence without deleting identity-bearing
    words such as Optional, Foundation, QEP, Paper, institute, teacher, or
    subject names.
    """
    n = _norm(value)

    if not n:
        return ""

    tokens = []

    for token in n.split():
        if token in _V25_SELECTION_FILLERS:
            continue

        if re.fullmatch(r"20\d{2}", token):
            continue

        tokens.append(token)

    return " ".join(tokens).strip()


def _v25_base_subject_value(value):
    n = _norm(value)

    if not n:
        return ""

    n = re.sub(
        r"\boptional\b",
        " ",
        n,
    )

    return re.sub(
        r"\s+",
        " ",
        n,
    ).strip()


def _v25_source_priority(target):
    return {
        "folder_batch": 40,
        "structured": 35,
        "combo": 30,
        "folder_part": 20,
    }.get(
        str(target.get("source") or ""),
        0,
    )


def _v25_same_semantic_subject(candidates):
    subjects = {
        _v25_base_subject_value(
            x.get("subject")
            or x.get("folder_name")
            or ""
        )
        for x in candidates
        if _v25_base_subject_value(
            x.get("subject")
            or x.get("folder_name")
            or ""
        )
    }

    return len(subjects) <= 1


def _v25_collapse_candidates(candidates):
    """
    Collapse duplicate semantic owners safely.

    Parent folder batches win over their child components when there is only
    one possible parent.  Two unrelated parents remain ambiguous.
    """
    unique = {}

    for target in candidates:
        key = str(target.get("key") or "")

        if key:
            unique[key] = target

    items = list(unique.values())

    if not items:
        return None

    if len(items) == 1:
        return dict(items[0])

    names = {
        _norm(x.get("name"))
        for x in items
        if _norm(x.get("name"))
    }

    if len(names) == 1:
        items.sort(
            key=_v25_source_priority,
            reverse=True,
        )
        return dict(items[0])

    parents = [
        x
        for x in items
        if str(x.get("source") or "")
        == "folder_batch"
    ]

    # Parent/child/structured duplicates for the same semantic subject:
    # Sunil/Smriti Sociology -> Vision IAS Sociology.
    if (
        len(parents) == 1
        and _v25_same_semantic_subject(items)
    ):
        parent_folder = str(
            parents[0].get("folder_id")
            or ""
        )

        other_folders = {
            str(x.get("folder_id") or "")
            for x in items
            if (
                str(x.get("source") or "")
                in ("folder_batch", "folder_part")
                and str(x.get("folder_id") or "")
            )
        }

        if (
            not other_folders
            or other_folders == {parent_folder}
        ):
            return dict(parents[0])

    # Duplicate structured rows for the same teacher + subject can safely
    # preserve the existing deterministic first-row behavior.
    if all(
        str(x.get("source") or "")
        == "structured"
        for x in items
    ):
        teachers = {
            _norm(x.get("teacher"))
            for x in items
            if _norm(x.get("teacher"))
        }

        subjects = {
            _v25_base_subject_value(x.get("subject"))
            for x in items
            if _v25_base_subject_value(x.get("subject"))
        }

        if len(teachers) <= 1 and len(subjects) <= 1:
            return dict(items[0])

    return None


def _v25_whole_exact_phrase_match(targets, text):
    """
    Match the WHOLE customer message against saved names/aliases before the
    old segment splitter runs.  This protects legitimate saved phrases that
    contain '+' or other separators.

    Returns:
      (target, False) -> unique/collapsible exact owner
      (None, True)    -> exact phrase is genuinely ambiguous
      (None, False)   -> no exact whole-message phrase
    """
    q = _norm(text)

    if not q:
        return None, False

    owners = []

    for target in targets:
        values = [
            target.get("name"),
        ]

        values.extend(
            target.get("phrases", [])
            or []
        )

        if any(
            _norm(value) == q
            for value in values
            if _norm(value)
        ):
            owners.append(target)

    if not owners:
        return None, False

    collapsed = _v25_collapse_candidates(
        owners
    )

    if collapsed:
        return collapsed, False

    return None, True


def _v25_fuzzy_name_target(targets, text):
    """
    High-confidence typo recovery against CANONICAL saved target names only.

    It deliberately does not fuzzy-match generic one-word aliases.  The
    threshold/margin is intentionally strict so an unknown customer course
    cannot be guessed into an unrelated paid batch.
    """
    q = _v25_selection_core(text)

    if not q:
        return None

    q_tokens = q.split()

    if len(q_tokens) < 2:
        return None

    scored = []

    for index, target in enumerate(targets):
        name = str(
            target.get("name")
            or ""
        ).strip()

        c = _v25_selection_core(
            name
        )

        if not c:
            continue

        c_tokens = c.split()

        if len(c_tokens) < 2:
            continue

        score = SequenceMatcher(
            None,
            q,
            c,
        ).ratio()

        scored.append(
            (
                score,
                index,
                target,
                c,
            )
        )

    if not scored:
        return None

    scored.sort(
        key=lambda x: (
            x[0],
            _v25_source_priority(
                x[2]
            ),
            -x[1],
        ),
        reverse=True,
    )

    top_score = scored[0][0]

    if top_score < 0.90:
        return None

    # All targets with effectively the same top score are considered
    # together so duplicate semantic rows do not create fake ambiguity.
    top_group = [
        x[2]
        for x in scored
        if abs(x[0] - top_score) <= 0.002
    ]

    collapsed = _v25_collapse_candidates(
        top_group
    )

    if not collapsed:
        return None

    second_distinct = None
    top_name = _v25_selection_core(
        collapsed.get("name")
    )

    for score, _, target, c in scored:
        if c == top_name:
            continue

        second_distinct = score
        break

    if (
        second_distinct is not None
        and top_score - second_distinct < 0.03
    ):
        return None

    return dict(collapsed)


def _v25_dual_role_optional_guard(targets, target, text):
    """
    If the same faculty has a GS/non-Optional structured course and an
    Optional folder course, Optional must be explicit before selecting the
    Optional target.

    This is the generic form of the Sabir Geography rule.
    """
    source = str(
        target.get("source")
        or ""
    )

    if source not in (
        "folder_batch",
        "folder_part",
    ):
        return False

    subject = str(
        target.get("subject")
        or target.get("folder_name")
        or ""
    )

    if "optional" not in _norm(subject):
        return False

    if re.search(
        r"\boptional\b",
        _norm(text),
    ):
        return False

    teacher = _norm(
        target.get("teacher")
    )

    base_subject = _v25_base_subject_value(
        subject
    )

    if not teacher or not base_subject:
        return False

    for other in targets:
        if str(other.get("source") or "") != "structured":
            continue

        other_subject = _v25_base_subject_value(
            other.get("subject")
        )

        if other_subject != base_subject:
            continue

        other_teacher = _norm(
            other.get("teacher")
        )

        if not other_teacher:
            continue

        similarity = SequenceMatcher(
            None,
            teacher,
            other_teacher,
        ).ratio()

        if similarity >= 0.86:
            return True

    return False


def _v25_strong_phrase_target(targets, text):
    """
    Resolve a multi-word saved identity phrase before global teacher fuzziness.

    Single-token aliases such as "Shabir" are intentionally excluded because
    they can represent a GS/Optional dual-role teacher and need normal teacher
    logic plus subject context.
    """
    q = _v25_selection_core(
        text
    )

    if not q:
        return None

    q_tokens = q.split()

    if len(q_tokens) < 2:
        return None

    candidates = []

    for target in targets:
        best = 0.0

        values = [
            target.get("name"),
        ]

        values.extend(
            target.get("phrases", [])
            or []
        )

        for value in values:
            p = _v25_selection_core(
                value
            )

            if not p:
                continue

            p_tokens = p.split()

            if len(p_tokens) < 2:
                continue

            ratio = SequenceMatcher(
                None,
                q,
                p,
            ).ratio()

            if q == p:
                ratio = 1.0

            # Optional-only folders can be requested without literally
            # saying "Optional" when no same-faculty GS target exists.
            p_without_optional = re.sub(
                r"\boptional\b",
                " ",
                p,
            )
            p_without_optional = re.sub(
                r"\s+",
                " ",
                p_without_optional,
            ).strip()

            if (
                p_without_optional
                and q == p_without_optional
                and not _v25_dual_role_optional_guard(
                    targets,
                    target,
                    text,
                )
            ):
                ratio = max(
                    ratio,
                    0.995,
                )

            best = max(
                best,
                ratio,
            )

        if best >= 0.94:
            if _v25_dual_role_optional_guard(
                targets,
                target,
                text,
            ):
                continue

            candidates.append(
                (
                    best,
                    target,
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            x[0],
            _v25_source_priority(
                x[1]
            ),
        ),
        reverse=True,
    )

    top_score = candidates[0][0]

    top = [
        x[1]
        for x in candidates
        if abs(x[0] - top_score) <= 0.002
    ]

    collapsed = _v25_collapse_candidates(
        top
    )

    if not collapsed:
        return None

    second = None
    top_key = str(
        collapsed.get("key")
        or ""
    )

    for score, target in candidates:
        if str(target.get("key") or "") == top_key:
            continue

        if target in top:
            continue

        second = score
        break

    if (
        second is not None
        and top_score - second < 0.03
    ):
        return None

    return dict(collapsed)


STATE_RESTORE_VERSION = "STATE_RESTORE_V30_MINIMAL"


def _v30_target_is_optional(target):
    value = _norm(target.get("subject") or target.get("folder_name") or "")
    return "optional" in value


def _v30_filter_gs_optional(candidates, text, subject):
    base = _norm(subject)
    if base not in {"geography", "history", "economy"}:
        return list(candidates or [])
    optional_requested = bool(re.search(r"\\boptional\\b", _norm(text)))
    if optional_requested:
        return [x for x in (candidates or []) if _v30_target_is_optional(x)]
    return [x for x in (candidates or []) if not _v30_target_is_optional(x)]


def _v30_exact_saved_target_lock(path, text):
    """Return (key, source) only for a unique exact canonical saved target name.

    This is intentionally NOT an alias/fuzzy resolver.  It exists only so the
    downstream renderer can honor the exact target already selected by the old
    proven state engine instead of rematching it through another catalogue.
    """
    q = _norm(text)
    if not q:
        return "", ""
    matches = [t for t in _all_targets(path) if _norm(t.get("name")) == q]
    if not matches:
        return "", ""
    collapsed = _v25_collapse_candidates(matches)
    if not collapsed:
        return "", ""
    return str(collapsed.get("key") or ""), str(collapsed.get("source") or "")


def _v30_subject_allowed_targets(path, text, subject):
    allowed = [
        t for t in _all_targets(path)
        if _v24_target_matches_subject(t, subject)
    ]
    base = _norm(subject)
    if base in {"geography", "history", "economy"}:
        optional_requested = bool(re.search(r"\boptional\b", _norm(text)))
        if optional_requested:
            allowed = [t for t in allowed if _v30_target_is_optional(t)]
        else:
            allowed = [t for t in allowed if not _v30_target_is_optional(t)]
    if base == "psir":
        allowed = [
            t for t in allowed
            if _norm(t.get("subject") or t.get("folder_name") or "")
            in ("psir", "psir optional")
        ]
    return allowed


def _v30_identity_candidates(allowed, text, subject):
    tokens = _v24_subject_identity_tokens(text, subject)
    if not tokens:
        return []
    exact = []
    fuzzy = []
    for target in allowed:
        values = [target.get("teacher"), target.get("institute"), target.get("name")]
        values.extend(target.get("phrases", []) or [])
        words = set()
        normalized_values = []
        for value in values:
            nv = _norm(value)
            if not nv:
                continue
            normalized_values.append(nv)
            words.update(nv.split())
        if all(token in words for token in tokens):
            exact.append(target)
            continue
        query_identity = " ".join(tokens)
        best = 0.0
        for value in normalized_values:
            best = max(best, SequenceMatcher(None, query_identity, value).ratio())
            for part in value.split():
                best = max(best, SequenceMatcher(None, query_identity, part).ratio())
        if best >= 0.90:
            fuzzy.append((best, target))
    candidates = exact
    if not candidates:
        overlap = []
        for target in allowed:
            values = [target.get("teacher"), target.get("institute"), target.get("name")]
            values += list(target.get("phrases", []) or [])
            words = set()
            for value in values:
                words.update(_norm(value).split())
            matched = {tok for tok in tokens if len(tok) >= 4 and tok in words}
            if matched:
                overlap.append((len(matched), target))
        if overlap:
            best_count = max(x[0] for x in overlap)
            candidates = [t for count, t in overlap if count == best_count]
    if not candidates and fuzzy:
        fuzzy.sort(key=lambda x: x[0], reverse=True)
        top = fuzzy[0][0]
        candidates = [t for score, t in fuzzy if top - score <= 0.03]
    unique = {}
    for target in candidates:
        key = str(target.get("key") or "")
        if key:
            unique[key] = target
    return list(unique.values())


def _v30_parent_normalize(candidates):
    unique = {}
    for target in candidates:
        key = str(target.get("key") or "")
        if key:
            unique[key] = target
    items = list(unique.values())
    if len(items) <= 1:
        return items
    parents_by_folder = {
        str(x.get("folder_id") or ""): x
        for x in items
        if str(x.get("source") or "") == "folder_batch"
        and str(x.get("folder_id") or "")
    }
    out = []
    consumed = set()
    for folder_id, parent in parents_by_folder.items():
        group = [
            x for x in items
            if str(x.get("folder_id") or "") == folder_id
            and str(x.get("source") or "") in ("folder_batch", "folder_part")
        ]
        parent_batch_id = str(parent.get("batch_id") or "")
        related = [
            x for x in group
            if x is parent or str(x.get("batch_id") or "") == parent_batch_id
        ]
        if len(related) > 1:
            out.append(dict(parent))
            consumed.update(str(x.get("key") or "") for x in related)
    for x in items:
        if str(x.get("key") or "") not in consumed:
            out.append(x)
    return out


def _v30_subject_first_saved_candidate(path, text):
    """Minimal hard-boundary selection for fully specified new messages.

    The original V25/V24 state engine remains untouched.  This helper only
    constrains the EARLY saved-target matcher when the current message itself
    explicitly names a subject.
    """
    subject = _v12_explicit_subject(text)
    if not subject:
        return None, ""
    identity = _v24_subject_identity_tokens(text, subject)
    if not identity:
        return None, "subject_only_defer"
    allowed = _v30_subject_allowed_targets(path, text, subject)
    if not allowed:
        return None, "subject_scoped_unresolved"
    exact, ambiguous = _v25_whole_exact_phrase_match(allowed, text)
    if exact:
        return exact, "subject_scoped_exact"
    fuzzy = _v25_fuzzy_name_target(allowed, text)
    if fuzzy:
        return fuzzy, "subject_scoped_fuzzy"
    strong = _v25_strong_phrase_target(allowed, text)
    if strong:
        return strong, "subject_scoped_phrase"
    candidates = _v30_identity_candidates(allowed, text, subject)
    candidates = _v30_parent_normalize(candidates)
    if len(candidates) == 1:
        return dict(candidates[0]), "subject_scoped"
    return None, "subject_scoped_unresolved"


def _v25_selection_candidate(path, text):
    # STATE_RESTORE_V30_MINIMAL: an explicit subject is a hard boundary for
    # this EARLY matcher only.  The original subject/teacher state machine
    # below process_unified_customer_context remains untouched.
    _v30_subject = _v12_explicit_subject(text)
    if _v30_subject:
        _v30_candidate, _v30_reason = _v30_subject_first_saved_candidate(path, text)
        if _v30_candidate:
            return _v30_candidate, _v30_reason
        # Never fall back to global exact/fuzzy matching after an explicit
        # subject.  Subject-only and unresolved identity cases continue into
        # the original state engine.
        return None, _v30_reason

    targets = _all_targets(
        path
    )

    exact, ambiguous = _v25_whole_exact_phrase_match(
        targets,
        text,
    )

    if exact:
        if _v25_dual_role_optional_guard(
            targets,
            exact,
            text,
        ):
            return None, "dual_role_optional_defer"

        return exact, "exact_phrase"

    if ambiguous:
        return None, "ambiguous_exact_phrase"

    fuzzy_name = _v25_fuzzy_name_target(
        targets,
        text,
    )

    if fuzzy_name:
        if _v25_dual_role_optional_guard(
            targets,
            fuzzy_name,
            text,
        ):
            return None, "dual_role_optional_defer"

        return fuzzy_name, "canonical_name"

    phrase_target = _v25_strong_phrase_target(
        targets,
        text,
    )

    if phrase_target:
        return phrase_target, "strong_phrase"

    return None, ""


def _v25_is_selection_message(text, target, reason):
    """
    Do not steal payment/demo/price/validity/fact questions.  This gate is
    only for batch-selection utterances.
    """
    n = _norm(text)

    if not n:
        return False

    # Exact saved name/alias alone is itself a selection.
    values = [
        target.get("name"),
    ]
    values.extend(
        target.get("phrases", [])
        or []
    )

    if any(
        _norm(value) == n
        for value in values
        if _norm(value)
    ):
        return True

    # Hard transactional/fact boundaries stay owned by their existing flows.
    if re.search(
        r"\b(?:"
        r"payment|pay|phonepe|phone pay|paytm|amazon pay|"
        r"price|cost|amount|validity|valid|kab tak|"
        r"demo|sample|preview|link|download|"
        r"notes|pdf|recorded|recording|live"
        r")\b",
        n,
    ):
        return False

    # A high-confidence canonical-name typo or strong multi-word saved
    # identity is itself a valid bare selection:
    #   "Piyush Chabey PSIR"
    #   "Sarthi IAS Anthroology"
    if reason in (
        "canonical_name",
        "strong_phrase",
    ):
        return True

    # Natural selection language.
    return bool(
        re.search(
            r"\b(?:"
            r"chahiye|chahie|chaiye|"
            r"need|want|looking for|"
            r"lena|leni|join|buy|purchase"
            r")\b",
            n,
        )
    )


def _v25_commit_single_target(
    path,
    chat_id,
    target,
):
    _save_state(
        path,
        chat_id,
        targets=[target],
        pending_intent="",
        pending_unknown="",
    )

    selected_subject = str(
        target.get("subject")
        or target.get("folder_name")
        or ""
    ).strip()

    _v12_aux_set(
        path,
        chat_id,
        subject=selected_subject,
        special_intent="",
    )

    if str(target.get("source") or "") == "structured":
        _v13_set(
            path,
            chat_id,
            teacher_id=target.get("teacher_id"),
            teacher_name=str(
                target.get("teacher")
                or ""
            ),
            subject=selected_subject,
        )

    else:
        _v13_set(
            path,
            chat_id,
            teacher_id=None,
            teacher_name="",
            subject=selected_subject,
        )

    try:
        _final_unknown_clear(
            path,
            chat_id,
        )
    except Exception:
        pass


def resolve_targets(
    path,
    text,
):
    targets = _all_targets(
        path
    )

    # RESOLVER_REPAIR_V25
    #
    # 1. Whole-message exact saved phrase first.  This prevents the legacy
    #    segment splitter from breaking legitimate aliases containing "+".
    # 2. High-confidence canonical-name typo recovery before weaker partial
    #    institute/teacher matches can steal the request.
    _v25_exact, _v25_ambiguous = _v25_whole_exact_phrase_match(
        targets,
        text,
    )

    if _v25_exact:
        return [
            _v25_exact
        ]

    if _v25_ambiguous:
        # A saved phrase such as bare "foundation" / "qep" belongs to
        # multiple unrelated parents.  Never pick the first database row.
        return []

    _v25_fuzzy = _v25_fuzzy_name_target(
        targets,
        text,
    )

    if _v25_fuzzy:
        return [
            _v25_fuzzy
        ]

    found = []
    seen = set()

    for segment in _segments(
        text
    ):
        target = _resolve_one(
            targets,
            segment,
        )

        if not target:
            continue

        key = target[
            "key"
        ]

        if key in seen:
            continue

        seen.add(
            key
        )

        found.append(
            target
        )

    return found


# ============================================================
# STATE
# ============================================================

def _load_state(
    path,
    chat_id,
):
    ensure_schema(path)

    with _con(path) as db:

        row = db.execute("""
            SELECT *
            FROM unified_customer_context
            WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

    if not row:
        return {
            "targets": [],
            "pending_intent": "",
            "pending_unknown": "",
            "fresh": False,
        }

    now = time.time()

    last_active = float(
        row["last_active_at"]
        or 0
    )

    fresh = (
        last_active > 0
        and now - last_active
        <= STATE_TTL_SECONDS
    )

    try:
        targets = json.loads(
            row[
                "active_targets_json"
            ]
            or "[]"
        )

    except Exception:
        targets = []

    if not fresh:
        targets = []

    return {
        "targets":
            targets,

        "pending_intent":
            str(
                row[
                    "pending_intent"
                ]
                or ""
            ),

        "pending_unknown":
            str(
                row[
                    "pending_unknown"
                ]
                or ""
            ),

        "fresh":
            fresh,
    }


def _save_state(
    path,
    chat_id,
    targets=None,
    pending_intent=None,
    pending_unknown=None,
    refresh=True,
):
    ensure_schema(path)

    current = _load_state(
        path,
        chat_id,
    )

    if targets is None:
        targets = current[
            "targets"
        ]

    if pending_intent is None:
        pending_intent = current[
            "pending_intent"
        ]

    if pending_unknown is None:
        pending_unknown = current[
            "pending_unknown"
        ]

    now = time.time()

    with _con(path) as db:

        row = db.execute("""
            SELECT
                started_at,
                message_count
            FROM unified_customer_context
            WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

        started_at = (
            float(
                row[
                    "started_at"
                ]
                or now
            )
            if row
            else now
        )

        count = (
            int(
                row[
                    "message_count"
                ]
                or 0
            )
            if row
            else 0
        )

        if refresh:
            count += 1

        db.execute("""
            INSERT INTO unified_customer_context (
                chat_id,
                active_targets_json,
                pending_intent,
                pending_unknown,
                started_at,
                last_active_at,
                message_count
            )
            VALUES (
                ?,?,?,?,?,?,?
            )

            ON CONFLICT(chat_id)
            DO UPDATE SET
                active_targets_json=
                    excluded.active_targets_json,

                pending_intent=
                    excluded.pending_intent,

                pending_unknown=
                    excluded.pending_unknown,

                last_active_at=
                    excluded.last_active_at,

                message_count=
                    excluded.message_count
        """, (
            str(chat_id),

            json.dumps(
                targets,
                ensure_ascii=False,
            ),

            str(
                pending_intent
                or ""
            ),

            str(
                pending_unknown
                or ""
            ),

            started_at,

            (
                now
                if refresh
                else 0
            ),

            count,
        ))

        db.commit()


# ============================================================
# INTENT HELPERS
# ============================================================


# ============================================================
# GLOBAL_VALIDITY_LIFETIME_V9
#
# Validity/access-duration is business policy:
# lifetime access.
#
# It must never inherit a random combo state such as Pro Pack.
# It must never be sent to the demo-fallback logic.
# ============================================================

def _is_validity_query(text):
    n = _norm(text)

    if not n:
        return False

    patterns = [
        r"\bvalidity\b",
        r"\bvalid\b",
        r"\blifetime\b",

        r"\baccess kab tak\b",
        r"\bkab tak access\b",
        r"\bkitne time tak\b",
        r"\bkitna time tak\b",
        r"\bkitne din tak\b",
        r"\bkitne month\b",
        r"\bkitne months\b",
        r"\bkitne mahine\b",

        r"\bfor how long\b",
        r"\bhow long.*access\b",
        r"\bhow much time.*access\b",
        r"\baccess duration\b",

        r"\bcourse kab tak\b",
        r"\bbatch kab tak\b",
        r"\blecture.*kab tak\b",
        r"\blectures.*kab tak\b",
    ]

    return any(
        re.search(
            pattern,
            n,
            re.I,
        )
        for pattern in patterns
    )


async def _send_lifetime_access(
    update,
    context,
):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Lifetime access rahega.",
    )

def _is_demo(text):
    return bool(
        re.search(
            r"\b(?:"
            r"demo|"
            r"sample|"
            r"preview"
            r")\b",
            _norm(text),
            re.I,
        )
    )


def _is_link(text):
    n = _norm(text)

    return bool(
        re.fullmatch(
            r"(?:"
            r"link|"
            r"link please|"
            r"link plz|"
            r"send link|"
            r"bhejo link|"
            r"link bhejo"
            r")",
            n,
        )
    )


def _is_payment(text):
    return bool(
        re.search(
            r"\b(?:"
            r"payment|"
            r"pay|"
            r"phonepe|"
            r"phone pay|"
            r"paytm|"
            r"amazon pay"
            r")\b",
            _norm(text),
        )
    )


def _context_intent(text):
    n = _norm(text)

    # STATE_ISOLATION_FIX_V1
    # Clearly unrelated/general queries must not become facts about a
    # remembered batch merely because they contain "latest", "live", or
    # "class".
    if re.search(
        r"\b(?:"
        r"weather|temperature|forecast|"
        r"joke|capital|"
        r"news|cricket|football|score|match|"
        r"ssc|cgl|chsl|banking|railway|railways|ntpc|"
        r"neet|jee|gate|nda|cds"
        r")\b",
        n,
    ):
        return ""

    if re.search(r"\bclass\s*(?:[1-9]|1[0-2])\b", n):
        return ""

    if _is_demo(text):
        return "demo"

    if _is_payment(text):
        return "payment"

    if re.search(
        r"\b(?:"
        r"year|"
        r"session|"
        r"kis year|"
        r"kaunse year"
        r")\b",
        n,
    ):
        return "year"

    if re.search(
        r"\b(?:"
        r"price|"
        r"cost|"
        r"kitne ka|"
        r"amount|"
        r"fixed price"
        r")\b",
        n,
    ):
        return "price"

    if re.search(
        r"\b(?:"
        r"validity|"
        r"valid|"
        r"access kab|"
        r"kab tak"
        r")\b",
        n,
    ):
        return "validity"

    if re.search(
        r"\b(?:"
        r"recorded|"
        r"live|"
        r"ongoing|"
        r"running|"
        r"completed|"
        r"latest"
        r")\b",
        n,
    ):
        return "format"

    if re.search(
        r"\b(?:"
        r"notes|"
        r"class notes|"
        r"pdf"
        r")\b",
        n,
    ):
        return "notes"

    if re.search(
        r"\b(?:"
        r"lecture|"
        r"lectures|"
        r"class|"
        r"classes"
        r")\b",
        n,
    ):
        return "lectures"

    # DEMO_FACT_FALLBACK_V8
    #
    # Questions for which we do not have reliable saved data.
    if re.search(
        r"\b(?:"
        r"duration|"
        r"medium|"
        r"language|"
        r"timing|"
        r"schedule|"
        r"quality|"
        r"complete|"
        r"completed|"
        r"completion|"
        r"covered|"
        r"coverage|"
        r"started|"
        r"starting|"
        r"start date|"
        r"end date|"
        r"upload|"
        r"uploaded|"
        r"updated|"
        r"kitna complete|"
        r"kitne lecture|"
        r"kitne lectures|"
        r"how many lecture|"
        r"how many lectures|"
        r"full course"
        r")\b",
        n,
    ):
        return "batch_fact"

    return ""


def _neutral(text):
    n = _norm(text)

    return n in {
        "",
        ".",
        "..",
        "...",
        "?",
        "ok",
        "okay",
        "okk",
        "okkk",
        "yes",
        "yeah",
        "yup",
        "han",
        "haan",
        "hmm",
        "hm",
        "thanks",
        "thank you",
        "thx",
    }


def _specific_unknown_request(
    text,
):
    n = _norm(text)

    # Requires a reasonably specific named-teacher/batch shape.
    title = bool(
        re.search(
            r"\b(?:"
            r"sir|"
            r"mam|"
            r"maam|"
            r"madam"
            r")\b",
            n,
        )
    )

    institution = bool(
        re.search(
            r"\b(?:"
            r"ias|"
            r"academy|"
            r"vision|"
            r"vajiram|"
            r"unacademy|"
            r"pw|"
            r"physics wallah|"
            r"only ias|"
            r"sarthi|"
            r"lukman|"
            r"level up"
            r")\b",
            n,
        )
    )

    batch_word = bool(
        re.search(
            r"\b(?:"
            r"batch|"
            r"course|"
            r"optional|"
            r"class|"
            r"classes|"
            r"lecture|"
            r"lectures"
            r")\b",
            n,
        )
    )

    words = n.split()

    return (
        len(words) >= 3
        and (
            title
            or (
                institution
                and batch_word
            )
        )
    )


def _same_targets(
    a,
    b,
):
    return {
        str(x.get("key"))
        for x in a
        if x.get("key")
    } == {
        str(x.get("key"))
        for x in b
        if x.get("key")
    }


def _target_text(targets):
    return " and ".join(
        str(
            x.get("name")
            or ""
        ).strip()
        for x in targets
        if str(
            x.get("name")
            or ""
        ).strip()
    )


# ============================================================
# REPLY CONTEXT
# ============================================================

def _reply_reference(text):
    raw = str(
        text
        or ""
    )

    match = re.search(
        r"\[SERVER REPLY CONTEXT:\s*"
        r"Referenced batch/course:\s*"
        r"(.+?)\]",
        raw,
        re.I | re.S,
    )

    if not match:
        return ""

    return (
        match
        .group(1)
        .strip()
    )



# ============================================================
# DIRECT_REPLY_REFERENCE_V11
#
# Main Account bridge already attaches:
#
#   update._customer_reply_reference
#
# when Telegram says the incoming message is a reply.
#
# Unified Context must consume that object DIRECTLY.
#
# Priority:
#   1. actual Telegram replied-to message/reference
#   2. old SERVER REPLY CONTEXT text marker
#   3. fresh active state
#
# Therefore replying to an OLD batch always switches state,
# even if another batch is currently active.
# ============================================================

def _direct_reply_reference(
    update,
):
    ref = getattr(
        update,
        "_customer_reply_reference",
        None,
    )

    if not ref:
        return ""

    # save_reference() currently returns a dict.
    if isinstance(
        ref,
        dict,
    ):
        candidates = (
            ref.get("reference_label"),
            ref.get("label"),
            ref.get("reference_text"),
            ref.get("text"),
            ref.get("message"),
        )

        for value in candidates:
            value = str(
                value
                or ""
            ).strip()

            if value:
                return value

        return ""

    # Future-safe fallback if bridge representation changes.
    for name in (
        "reference_label",
        "label",
        "reference_text",
        "text",
        "message",
    ):
        value = str(
            getattr(
                ref,
                name,
                "",
            )
            or ""
        ).strip()

        if value:
            return value

    return ""


def _without_server_context(
    text,
):
    return re.sub(
        r"\s*\[SERVER REPLY CONTEXT:.*?\]\s*",
        "",
        str(
            text
            or ""
        ),
        flags=re.I | re.S,
    ).strip()


# ============================================================
# SEND
# ============================================================

async def _send(
    update,
    context,
    text,
):
    chat_id = (
        update.effective_chat.id
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
    )


async def _send_fact_anchored(
    update,
    context,
    path,
    text,
):
    """Send price/year only, attached to active batch anchor."""
    chat_id = update.effective_chat.id

    anchor = _state_reply_get_anchor(
        path,
        chat_id,
    )

    return await _send_anchored_reply(
        update,
        context,
        str(text),
        reply_to_message_id=anchor,
    )


async def _send_selected_demo(
    update,
    context,
    targets,
):
    """
    Use the existing production demo architecture:

    exact batch demo
        ->
    folder combined demo
        ->
    /demoall fallback
    """

    if not targets:
        return False

    settings = context.application.bot_data.get(
        "settings"
    )

    if settings is None:
        return False

    main_client = context.application.bot_data.get(
        "main_account_client"
    )

    result = await execute_new_batch_access(
        context.application,
        settings,
        "demo",
        _target_text(targets),
        update.effective_chat.id,
        requested_by=(
            update.effective_user.id
            if update.effective_user
            else None
        ),
        main_account_client=main_client,
    )

    return bool(
        result.get("ok")
    )


# ============================================================
# MAIN UNIFIED CONTEXT PROCESSOR
# ============================================================


# ============================================================
# DEMO_FACT_FALLBACK_V8
#
# If customer asks a fact about the ACTIVE batch that is not
# stored in our DB:
#
#   "Recorded hai?"
#   "Running hai?"
#   "Kitne lectures hain?"
#   "Kitna complete hua?"
#   "Latest hai?"
#   "Notes hain?"
#   "Duration?"
#   etc.
#
# Reply:
#   "Bhai demo mein check kar lo."
#
# If the demo has not already been sent for THIS active target,
# send the relevant demo immediately as well.
#
# Explicit "Demo bhejo" ALWAYS sends demo again.
# ============================================================

def _demo_state_key(targets):
    keys = sorted(
        str(x.get("key") or "").strip()
        for x in (targets or [])
        if str(x.get("key") or "").strip()
    )

    return "|".join(keys)


def _ensure_demo_sent_schema(path):
    with _con(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS
            unified_customer_demo_state (
                chat_id TEXT PRIMARY KEY,
                target_key TEXT NOT NULL DEFAULT '',
                demo_sent_at REAL NOT NULL DEFAULT 0
            )
        """)

        db.commit()


def _demo_already_sent(
    path,
    chat_id,
    targets,
):
    _ensure_demo_sent_schema(path)

    key = _demo_state_key(
        targets
    )

    if not key:
        return False

    with _con(path) as db:
        row = db.execute("""
            SELECT
                target_key,
                demo_sent_at
            FROM unified_customer_demo_state
            WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

    if not row:
        return False

    return (
        str(row["target_key"] or "")
        == key
        and float(row["demo_sent_at"] or 0) > 0
    )


def _mark_demo_sent(
    path,
    chat_id,
    targets,
):
    _ensure_demo_sent_schema(path)

    key = _demo_state_key(
        targets
    )

    if not key:
        return

    with _con(path) as db:
        db.execute("""
            INSERT INTO unified_customer_demo_state (
                chat_id,
                target_key,
                demo_sent_at
            )
            VALUES (?,?,?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                target_key=excluded.target_key,
                demo_sent_at=excluded.demo_sent_at
        """, (
            str(chat_id),
            key,
            time.time(),
        ))

        db.commit()


def _target_has_saved_fact(
    targets,
    intent,
):
    """
    Only return True for facts we actually store reliably.

    price → saved price
    year  → saved year

    We deliberately do NOT claim to know:
      recorded/live/running status
      lecture count
      completion status
      duration
      class schedule
      notes availability
      language/medium
      latest upload status
      etc.
    """

    targets = list(
        targets
        or []
    )

    if not targets:
        return False

    if intent == "price":
        return all(
            str(
                x.get("price")
                or ""
            ).strip()
            not in (
                "",
                "0",
            )
            for x in targets
        )

    if intent == "year":
        return all(
            str(
                x.get("year")
                or ""
            ).strip()
            for x in targets
        )

    return False


def _must_use_demo_for_fact(
    targets,
    intent,
):
    """
    True when the customer is asking a batch-specific fact
    that our DB does not reliably contain.
    """

    if intent in (
        "",
        "demo",
        "payment",
        "price",
        "year",
    ):
        if intent in (
            "price",
            "year",
        ):
            return not _target_has_saved_fact(
                targets,
                intent,
            )

        return False

    if intent in (
        "format",
        "notes",
        "lectures",
        "batch_fact",
    ):
        return True

    return False



# ============================================================
# CONTEXT_BUSINESS_RULES_V12
# ============================================================

def _ensure_v12_context_schema(path):
    with _con(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS
            unified_customer_aux_context (
                chat_id TEXT PRIMARY KEY,

                subject_state
                    TEXT NOT NULL DEFAULT '',

                special_intent
                    TEXT NOT NULL DEFAULT '',

                updated_at
                    REAL NOT NULL DEFAULT 0
            )
        """)

        db.commit()


def _v12_aux_get(
    path,
    chat_id,
):
    _ensure_v12_context_schema(
        path
    )

    with _con(path) as db:
        row = db.execute("""
            SELECT
                subject_state,
                special_intent,
                updated_at
            FROM unified_customer_aux_context
            WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

    if not row:
        return {
            "subject": "",
            "special_intent": "",
            "fresh": False,
        }

    updated = float(
        row["updated_at"]
        or 0
    )

    return {
        "subject":
            str(
                row["subject_state"]
                or ""
            ).strip(),

        "special_intent":
            str(
                row["special_intent"]
                or ""
            ).strip(),

        "fresh":
            (
                updated > 0
                and time.time() - updated
                <= STATE_TTL_SECONDS
            ),
    }


def _v12_aux_set(
    path,
    chat_id,
    subject=None,
    special_intent=None,
):
    current = _v12_aux_get(
        path,
        chat_id,
    )

    if subject is None:
        subject = current[
            "subject"
        ]

    if special_intent is None:
        special_intent = current[
            "special_intent"
        ]

    with _con(path) as db:
        db.execute("""
            INSERT INTO unified_customer_aux_context (
                chat_id,
                subject_state,
                special_intent,
                updated_at
            )
            VALUES (?,?,?,?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                subject_state=
                    excluded.subject_state,

                special_intent=
                    excluded.special_intent,

                updated_at=
                    excluded.updated_at
        """, (
            str(chat_id),
            str(subject or ""),
            str(special_intent or ""),
            time.time(),
        ))

        db.commit()


def _v12_aux_clear_special(
    path,
    chat_id,
):
    _v12_aux_set(
        path,
        chat_id,
        special_intent="",
    )


# ============================================================
# SUBJECT STATE
# ============================================================

_V12_SUBJECT_ALIASES = {
    "history": ["history", "hist"],
    "polity": ["polity", "indian polity", "constitution"],
    "geography": ["geography", "geo", "geog"],
    "environment": ["environment", "environmental", "ecology", "env"],
    "science & tech": ["science tech", "science and tech", "science technology", "science and technology", "sci tech"],
    "internal security": ["internal security", "internal sec"],
    "international relations": ["international relations", "international relation", "ir"],
    "ethics": ["ethics"],
    "economy": ["economy", "economic", "economics", "econ"],
    "sociology": ["sociology", "sociology optional"],
    "psir": ["psir", "political science optional", "political science and international relations optional"],
    "governance": ["governance"],
    "essay": ["essay", "essay writing"],
    "disaster management": ["disaster management", "disaster mgmt", "disaster"],
    "society": ["society", "indian society", "social issues"],
    "csat": ["csat"],
}


def _v12_explicit_subject(text):
    n = _norm(text)

    if not n:
        return ""

    matches = []

    for canonical, aliases in (
        _V12_SUBJECT_ALIASES.items()
    ):
        for alias in aliases:

            a = _norm(
                alias
            )

            if not a:
                continue

            if re.search(
                r"(?<![a-z0-9])"
                + re.escape(a)
                + r"(?![a-z0-9])",
                n,
            ):
                matches.append(
                    (
                        len(a),
                        canonical,
                    )
                )
                break

    if not matches:
        return ""

    matches.sort(
        reverse=True
    )

    return matches[0][1]


def _v12_is_broad_subject_request(
    text,
    subject,
):
    if not subject:
        return False

    n = _norm(text)

    # Explicit teacher names normally contain 2+ identity words
    # or title words. If present, don't treat it as subject-only.
    if re.search(
        r"\b(?:"
        r"sir|"
        r"mam|"
        r"maam|"
        r"madam"
        r")\b",
        n,
    ):
        return False

    # Coaching/institute clues mean this may already be a
    # specific batch request.
    if re.search(
        r"\b(?:"
        r"ias|"
        r"academy|"
        r"unacademy|"
        r"vision|"
        r"vajiram|"
        r"sarthi|"
        r"saarthi|"
        r"only ias|"
        r"physics wallah|"
        r"pw|"
        r"level up|"
        r"lukman|"
        r"next ias|"
        r"study iq"
        r")\b",
        n,
    ):
        return False

    # Subject + generic sales words still means subject state.
    return bool(
        re.search(
            r"\b"
            + re.escape(
                _norm(subject)
            )
            + r"\b",
            n,
        )
    )


def _v24_subject_identity_tokens(
    text,
    subject,
):
    """
    Return only identity-bearing words from a subject request.

    Examples:
      "PSIR Subhra Ranjan latest batch" -> ["subhra", "ranjan"]
      "PSIR latest course chahiye"       -> []

    These tokens are used only INSIDE an already-known subject, so a
    surname like "Ranjan" can be accepted when it is unique in PSIR
    without becoming a dangerous global teacher alias.
    """

    n = _norm(text)
    canonical = (
        _v12_explicit_subject(subject)
        or _norm(subject)
    )

    aliases = list(
        _V12_SUBJECT_ALIASES.get(
            canonical,
            [],
        )
    )

    aliases.append(subject)
    aliases.append(canonical)

    # Remove the longest subject phrases first.
    for alias in sorted(
        {
            _norm(x)
            for x in aliases
            if _norm(x)
        },
        key=len,
        reverse=True,
    ):
        n = re.sub(
            r"(?<![a-z0-9])"
            + re.escape(alias)
            + r"(?![a-z0-9])",
            " ",
            n,
        )

    # Years, prices and ordinary sales/request language carry no identity.
    n = re.sub(r"\b20\d{2}\b", " ", n)
    n = re.sub(r"\b\d{2,5}\b", " ", n)
    n = re.sub(
        r"\b(?:"
        r"bhai|bhaiya|bro|please|plz|mujhe|mereko|"
        r"chahiye|chahie|chahiye tha|want|need|looking|for|"
        r"ka|ki|ke|hai|hain|h|kya|batao|do|dedo|de|"
        r"batch|batches|course|courses|class|classes|"
        r"lecture|lectures|video|videos|recorded|recording|"
        r"latest|new|updated|update|available|availability|"
        r"optional|subject|year|price|demo|access|notes|pdf|"
        r"sir|mam|maam|madam|teacher|faculty|coaching"
        r")\b",
        " ",
        n,
    )

    n = re.sub(r"\s+", " ", n).strip()

    return [
        token
        for token in n.split()
        if len(token) >= 4
    ]


def _v24_target_matches_subject(
    target,
    subject,
):
    canonical = (
        _v12_explicit_subject(subject)
        or _norm(subject)
    )

    if not canonical:
        return False

    for value in (
        target.get("subject"),
        target.get("folder_name"),
    ):
        value_norm = _norm(value)

        if not value_norm:
            continue

        if value_norm == _norm(subject):
            return True

        detected = _v12_explicit_subject(value)

        if detected and detected == canonical:
            return True

    return False


def _v12_resolve_within_subject(
    path,
    text,
    subject,
):
    """
    Resolve teacher/coaching ONLY within the active subject.

    V24 fixes two important cases:
      * canonical subject "psir" also matches folder "PSIR Optional"
      * a unique partial identity such as "Subhra" or "Ranjan"
        can resolve inside PSIR without becoming a global alias.
    """

    subject_norm = _norm(subject)

    if not subject_norm:
        return []

    targets = _all_targets(path)

    allowed = [
        target
        for target in targets
        if _v24_target_matches_subject(
            target,
            subject,
        )
    ]

    if not allowed:
        return []

    result = []
    seen = set()

    # First preserve the existing exact/alias resolver.
    for segment in _segments(text):
        target = _resolve_one(
            allowed,
            segment,
        )

        if not target:
            continue

        key = str(target.get("key") or "")

        if key and key not in seen:
            seen.add(key)
            result.append(target)

    if result:
        return result

    # Subject-scoped unique partial identity fallback.
    identity_tokens = _v24_subject_identity_tokens(
        text,
        subject,
    )

    if not identity_tokens:
        return []

    candidates = []

    for target in allowed:
        identity_values = [
            target.get("teacher"),
            target.get("institute"),
            target.get("name"),
        ]
        identity_values.extend(
            target.get("phrases", [])
            or []
        )

        identity_words = set()

        for value in identity_values:
            identity_words.update(
                _norm(value).split()
            )

        if all(
            token in identity_words
            for token in identity_tokens
        ):
            candidates.append(target)

    # De-duplicate target keys.
    unique = {}

    for target in candidates:
        key = str(target.get("key") or "")
        if key:
            unique[key] = target

    candidates = list(unique.values())

    if len(candidates) == 1:
        return [dict(candidates[0])]

    if not candidates:
        return []

    # Parent folder batch wins only when every candidate represents the
    # same teacher in the same folder (parent + component duplicates).
    teachers = {
        _norm(x.get("teacher"))
        for x in candidates
        if _norm(x.get("teacher"))
    }
    folders = {
        str(x.get("folder_id") or "")
        for x in candidates
        if str(x.get("folder_id") or "")
    }

    if len(teachers) <= 1 and len(folders) <= 1:
        parents = [
            x
            for x in candidates
            if str(x.get("source") or "")
            == "folder_batch"
        ]

        if len(parents) == 1:
            return [dict(parents[0])]

    # Real ambiguity stays unresolved.
    return []


# ============================================================
# DOWNLOADABILITY
# ============================================================

def _v12_download_intent(text):
    n = _norm(text)

    return bool(
        re.search(
            r"\b(?:"
            r"download|"
            r"downloadable|"
            r"offline save|"
            r"save lecture|"
            r"save lectures|"
            r"lecture save|"
            r"lectures save|"
            r"video save|"
            r"videos save"
            r")\b",
            n,
        )
    )


def _v12_device(text):
    n = _norm(text)

    # Specific non-phone devices first.
    if re.search(
        r"\b(?:"
        r"laptop|"
        r"computer|"
        r"pc|"
        r"desktop|"
        r"mac|"
        r"macbook|"
        r"iphone|"
        r"ios|"
        r"ipad|"
        r"tablet|"
        r"tab"
        r")\b",
        n,
    ):
        return "unsupported"

    if re.search(
        r"\b(?:"
        r"android phone|"
        r"android mobile|"
        r"android"
        r")\b",
        n,
    ):
        return "android_phone"

    return ""


async def _v12_send_download_answer(
    update,
    context,
    device="",
):
    if device == "unsupported":
        text = "No bro."

    else:
        text = (
            "Yes bro, lectures can be downloaded "
            "on an Android phone."
        )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
    )


# ============================================================
# COMBO TARGETS
# ============================================================

def _v12_combo_targets(path):
    result = []

    try:
        with _con(path) as db:
            rows = db.execute("""
                SELECT *
                FROM combos
                WHERE enabled=1
                ORDER BY id
            """).fetchall()

    except Exception:
        return result

    for row in rows:

        name = str(
            row["name"]
            or ""
        ).strip()

        if not name:
            continue

        result.append({
            "source": "combo",

            "key":
                "combo:"
                + str(row["id"]),

            "combo_id":
                int(row["id"]),

            "teacher": "",

            "subject": "",

            "name":
                name,

            "price":
                str(
                    row["price"]
                    or ""
                ),

            "year": "",

            "availability": 1,

            "notes":
                str(
                    row["details"]
                    or ""
                )
                if "details" in row.keys()
                else "",

            "phrases": [
                name,
                name + " combo",
            ],
        })

    return result


def _v12_resolve_combo(
    path,
    text,
):
    targets = _v12_combo_targets(
        path
    )

    if not targets:
        return []

    found = []
    seen = set()

    for segment in _segments(
        text
    ):
        target = _resolve_one(
            targets,
            segment,
        )

        if not target:
            continue

        key = str(
            target.get("key")
            or ""
        )

        if key in seen:
            continue

        seen.add(key)
        found.append(target)

    return found


# ============================================================
# INCLUSION / CONTENT QUERY
# ============================================================

def _v12_inclusion_query(text):
    n = _norm(text)

    if not n:
        return False

    # Clear inclusion phrasing.
    if re.search(
        r"\b(?:"
        r"included|"
        r"include|"
        r"contains|"
        r"contain|"
        r"milta hai|"
        r"milegi|"
        r"milega|"
        r"hai isme|"
        r"isme hai|"
        r"bhi hai|"
        r"available in"
        r")\b",
        n,
    ):
        return True

    # Short component query while target already exists:
    #
    # "CA lectures?"
    # "CSAT?"
    # "Essay lectures?"
    # "Ethics bhi?"
    #
    # Treat short lecture/component questions as inclusion.
    words = n.split()

    if (
        len(words) <= 5
        and re.search(
            r"\b(?:"
            r"lecture|"
            r"lectures|"
            r"ca|"
            r"current affairs|"
            r"csat|"
            r"essay|"
            r"ethics|"
            r"notes|"
            r"module"
            r")\b",
            n,
        )
    ):
        return True

    return False


async def _v12_send_inclusion_guidance(
    update,
    context,
):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "Bro, list/demo check kar lo. "
            "Jo mentioned hai wahi included hai."
        ),
    )



# ============================================================
# STATE_CONSOLIDATION_V13
#
# ONE authoritative course state remains:
#     unified_customer_context.active_targets_json
#
# V13 partial state exists ONLY while resolving:
#     teacher -> subject
#     subject -> teacher
#
# Once both identify a DB row, the full target is written back
# into the original unified_customer_context.
#
# It must never replace a valid full target with a standalone
# subject state.
# ============================================================


def _v13_ensure_schema(path):
    with _con(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS
            unified_customer_resolution_state (
                chat_id TEXT PRIMARY KEY,

                teacher_id INTEGER,
                teacher_name TEXT NOT NULL DEFAULT '',

                subject_state TEXT NOT NULL DEFAULT '',

                updated_at REAL NOT NULL DEFAULT 0
            )
        """)

        db.commit()


def _v13_get(
    path,
    chat_id,
):
    _v13_ensure_schema(
        path
    )

    with _con(path) as db:
        row = db.execute("""
            SELECT
                teacher_id,
                teacher_name,
                subject_state,
                updated_at
            FROM unified_customer_resolution_state
            WHERE chat_id=?
        """, (
            str(chat_id),
        )).fetchone()

    if not row:
        return {
            "teacher_id": None,
            "teacher_name": "",
            "subject": "",
            "fresh": False,
        }

    updated = float(
        row["updated_at"]
        or 0
    )

    return {
        "teacher_id":
            (
                int(row["teacher_id"])
                if row["teacher_id"] is not None
                else None
            ),

        "teacher_name":
            str(
                row["teacher_name"]
                or ""
            ).strip(),

        "subject":
            str(
                row["subject_state"]
                or ""
            ).strip(),

        "fresh":
            (
                updated > 0
                and time.time() - updated
                <= STATE_TTL_SECONDS
            ),
    }


def _v13_set(
    path,
    chat_id,
    teacher_id=None,
    teacher_name=None,
    subject=None,
):
    current = _v13_get(
        path,
        chat_id,
    )

    if teacher_id is None:
        teacher_id = current.get(
            "teacher_id"
        )

    if teacher_name is None:
        teacher_name = current.get(
            "teacher_name",
            "",
        )

    if subject is None:
        subject = current.get(
            "subject",
            "",
        )

    with _con(path) as db:
        db.execute("""
            INSERT INTO unified_customer_resolution_state (
                chat_id,
                teacher_id,
                teacher_name,
                subject_state,
                updated_at
            )
            VALUES (?,?,?,?,?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                teacher_id=
                    excluded.teacher_id,

                teacher_name=
                    excluded.teacher_name,

                subject_state=
                    excluded.subject_state,

                updated_at=
                    excluded.updated_at
        """, (
            str(chat_id),
            teacher_id,
            str(teacher_name or ""),
            str(subject or ""),
            time.time(),
        ))

        db.commit()


def _v13_clear(
    path,
    chat_id,
):
    _v13_ensure_schema(
        path
    )

    with _con(path) as db:
        db.execute("""
            DELETE FROM unified_customer_resolution_state
            WHERE chat_id=?
        """, (
            str(chat_id),
        ))

        db.commit()


# ============================================================
# TEACHER NAME MATCHING
# ============================================================

def _v13_name_norm(value):
    n = _norm(
        value
    )

    # Remove conversational honorifics.
    n = re.sub(
        r"\b(?:"
        r"sir|"
        r"mam|"
        r"maam|"
        r"madam|"
        r"mr|"
        r"mrs|"
        r"ms|"
        r"ji"
        r")\b",
        " ",
        n,
    )

    # Remove common filler words customers append.
    n = re.sub(
        r"\b(?:"
        r"bhai|"
        r"bhaiya|"
        r"please|"
        r"plz|"
        r"chahiye|"
        r"chahie|"
        r"chahiyeh|"
        r"ka|"
        r"ki|"
        r"ke|"
        r"hai|"
        r"hain|"
        r"wala|"
        r"wali|"
        r"batao|"
        r"do|"
        r"de|"
        r"mujhe|"
        r"mereko"
        r")\b",
        " ",
        n,
    )

    return re.sub(
        r"\s+",
        " ",
        n,
    ).strip()


def _v13_teacher_rows(
    path,
):
    with _con(path) as db:

        cols = {
            str(r["name"])
            for r in db.execute(
                "PRAGMA table_info(structured_batch_teachers)"
            ).fetchall()
        }

        aliases_expr = (
            "aliases"
            if "aliases" in cols
            else "'' AS aliases"
        )

        rows = db.execute(
            f"""
            SELECT
                id,
                name,
                {aliases_expr}
            FROM structured_batch_teachers
            WHERE enabled=1
            ORDER BY id
            """
        ).fetchall()

    return [
        dict(x)
        for x in rows
    ]


def _v13_teacher_ids_for_subject(
    path,
    subject,
):
    wanted = _norm(
        subject
    )

    if not wanted:
        return set()

    result = set()

    with _con(path) as db:

        rows = db.execute("""
            SELECT
                teacher_id,
                name
            FROM structured_batch_subjects
            WHERE enabled=1
        """).fetchall()

    for row in rows:

        saved = _norm(
            row["name"]
        )

        # Exact semantic subject.
        if saved == wanted:
            result.add(
                int(
                    row["teacher_id"]
                )
            )
            continue

        # Science & Tech naming variants.
        aliases = {
            "science tech": {
                "science tech",
                "science technology",
            },

            "science technology": {
                "science tech",
                "science technology",
            },

            "international relations": {
                "international relations",
                "ir",
            },

            "ir": {
                "international relations",
                "ir",
            },
        }

        wanted_set = aliases.get(
            wanted,
            {wanted},
        )

        saved_set = aliases.get(
            saved,
            {saved},
        )

        if wanted_set & saved_set:
            result.add(
                int(
                    row["teacher_id"]
                )
            )

    return result


def _v13_teacher_score(
    query,
    candidate,
):
    q = _v13_name_norm(
        query
    )

    c = _v13_name_norm(
        candidate
    )

    if not q or not c:
        return 0.0

    if q == c:
        return 100.0

    qt = q.split()
    ct = c.split()

    qset = set(qt)
    cset = set(ct)

    # Complete saved name appears.
    if c in q:
        return 99.0

    # Customer typed all teacher tokens.
    if (
        len(cset) >= 2
        and cset.issubset(qset)
    ):
        return 97.0

    # One distinctive first/surname token is enough IF unique
    # within the already-known subject.
    overlap = qset & cset

    if overlap:

        longest = max(
            (
                len(x)
                for x in overlap
            ),
            default=0,
        )

        if longest >= 5:
            base = 86.0

            if len(overlap) >= 2:
                base = 95.0

            return base

    ratio = SequenceMatcher(
        None,
        q,
        c,
    ).ratio()

    # Token-level typo comparison:
    # Nikhil Sait -> Nikhil Seth
    # Himanshu Khatri -> Himansu Khatri
    # Pratik Nayak -> Prateek Nayak
    best_token_matches = []

    for qtoken in qt:

        if len(qtoken) < 4:
            continue

        best = 0.0

        for ctoken in ct:
            if len(ctoken) < 4:
                continue

            r = SequenceMatcher(
                None,
                qtoken,
                ctoken,
            ).ratio()

            best = max(
                best,
                r,
            )

        if best:
            best_token_matches.append(
                best
            )

    token_score = 0.0

    if best_token_matches:
        best_token_matches.sort(
            reverse=True
        )

        token_score = (
            sum(
                best_token_matches[:2]
            )
            / min(
                2,
                len(best_token_matches)
            )
        )

    return max(
        ratio * 100.0,
        token_score * 92.0,
    )


def _v13_unique_teacher(
    path,
    text,
    subject="",
):
    rows = _v13_teacher_rows(
        path
    )

    allowed = None

    if subject:
        allowed = _v13_teacher_ids_for_subject(
            path,
            subject,
        )

    scored = []

    for row in rows:

        tid = int(
            row["id"]
        )

        if (
            allowed is not None
            and tid not in allowed
        ):
            continue

        names = [
            str(
                row.get("name")
                or ""
            )
        ]

        aliases = str(
            row.get("aliases")
            or ""
        )

        names.extend(
            x.strip()
            for x in re.split(
                r"[,;\n|]+",
                aliases,
            )
            if x.strip()
        )

        best = max(
            (
                _v13_teacher_score(
                    text,
                    name,
                )
                for name in names
            ),
            default=0.0,
        )

        if best:
            scored.append(
                (
                    best,
                    row,
                )
            )

    scored.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    if not scored:
        return None

    best_score = scored[0][0]

    # Require a meaningful teacher match.
    if best_score < 72.0:
        return None

    if len(scored) > 1:

        second = scored[1][0]

        # Do not guess if two teachers are genuinely close.
        if (
            best_score < 92.0
            and best_score - second < 8.0
        ):
            return None

    return dict(
        scored[0][1]
    )


# ============================================================
# SUBJECT TARGET UNDER ONE TEACHER
# ============================================================

def _v13_subject_similarity(
    requested,
    saved,
):
    a = _norm(
        requested
    )

    b = _norm(
        saved
    )

    if not a or not b:
        return 0.0

    if a == b:
        return 100.0

    aliases = {
        "science tech":
            "science technology",

        "science and tech":
            "science technology",

        "science technology":
            "science technology",

        "science and technology":
            "science technology",

        "ir":
            "international relations",

        "international relations":
            "international relations",
    }

    aa = aliases.get(
        a,
        a,
    )

    bb = aliases.get(
        b,
        b,
    )

    if aa == bb:
        return 100.0

    # CONSOLIDATED_STATE_V23: teacher-scoped subject aliases may be
    # shorter than the saved subject name (Economy -> Economy Foundation).
    if len(aa) >= 4 and (aa in bb or bb in aa):
        return 97.0

    a_tokens = set(aa.split())
    b_tokens = set(bb.split())
    if a_tokens and b_tokens and (
        a_tokens.issubset(b_tokens) or b_tokens.issubset(a_tokens)
    ):
        return 97.0

    return (
        SequenceMatcher(
            None,
            aa,
            bb,
        ).ratio()
        * 100.0
    )


def _v13_target_for_teacher_subject(
    path,
    teacher_id,
    subject,
):
    """Resolve only inside one teacher; refuse ambiguous matches."""
    if not teacher_id or not subject:
        return None

    requested = _norm(subject)
    if not requested:
        return None

    with _con(path) as db:
        rows = db.execute("""
            SELECT *
            FROM structured_batch_subjects
            WHERE enabled=1
              AND availability=1
              AND teacher_id=?
        """, (int(teacher_id),)).fetchall()

    scored = []

    for row in rows:
        phrases = [
            str(row["name"] or ""),
            str(row["part_name"] or ""),
            str(row["batch_code"] or ""),
        ]

        phrases.extend([
            x.strip()
            for x in re.split(r"[,;|\n]+", str(row["aliases"] or ""))
            if x.strip()
        ])

        best = 0.0
        exact = False

        for phrase in phrases:
            p = _norm(phrase)
            if not p:
                continue

            if requested == p:
                score = 100.0
                exact = True
            else:
                score = _v13_subject_similarity(
                    requested,
                    p,
                )

            best = max(best, score)

        scored.append((best, exact, int(row["id"])))

    if not scored:
        return None

    scored.sort(reverse=True)
    top_score = scored[0][0]

    if top_score < 82.0:
        return None

    # Never guess between two near-equal batches of the same teacher.
    contenders = [
        item for item in scored
        if item[0] >= top_score - 1.0
    ]

    if len(contenders) != 1:
        logger.info(
            "FINAL SUBJECT AMBIGUOUS teacher_id=%s subject=%r contenders=%r",
            teacher_id,
            subject,
            contenders,
        )
        return None

    subject_id = contenders[0][2]

    for target in _all_targets(path):
        if str(target.get("key") or "") == "subject:" + str(subject_id):
            return dict(target)

    return None


def _v13_target_for_subject_teacher_text(
    path,
    subject,
    teacher_text,
):
    teacher = _v13_unique_teacher(
        path,
        teacher_text,
        subject=subject,
    )

    if not teacher:
        return None

    return _v13_target_for_teacher_subject(
        path,
        int(teacher["id"]),
        subject,
    )


# ============================================================
# PARTIAL TEACHER QUERY DETECTION
# ============================================================

def _v13_teacherish(text):
    n = _norm(
        text
    )

    if not n:
        return False

    if len(
        n.split()
    ) > 8:
        return False

    if re.search(
        r"\b(?:"
        r"sir|"
        r"mam|"
        r"maam|"
        r"madam"
        r")\b",
        n,
    ):
        return True

    # Short names like:
    # Nikhil Seth
    # Neeraj Rao
    # Prateek Nayak
    if 1 <= len(n.split()) <= 4:
        return True

    return False


# ============================================================
# DEMO RECOVERY / RESEND
# ============================================================

def _v13_demo_recovery(text):
    n = _norm(
        text
    )

    if not n:
        return False

    # Explicit demo language.
    if re.search(
        r"\b(?:"
        r"demo|"
        r"sample|"
        r"preview"
        r")\b",
        n,
    ):
        return True

    # Follow-up after:
    # "Bhai demo mein check kar lo."
    return bool(
        re.search(
            r"\b(?:"
            r"nahi mil raha|"
            r"nhi mil raha|"
            r"nahi mila|"
            r"nhi mila|"
            r"mil nahi raha|"
            r"mil nhi raha|"
            r"kaha hai|"
            r"kahan hai|"
            r"kidhar hai|"
            r"where|"
            r"cant find|"
            r"cannot find|"
            r"can't find|"
            r"not finding|"
            r"firse|"
            r"phir se|"
            r"dobara|"
            r"resend"
            r")\b",
            n,
        )
    )


def _v13_demo_query(
    targets,
):
    targets = list(
        targets
        or []
    )

    if not targets:
        return "demo"

    first = targets[0]

    source = str(
        first.get("source")
        or ""
    )

    folder = str(
        first.get("folder_name")
        or ""
    ).strip()

    if (
        source in (
            "folder_batch",
            "folder_part",
        )
        and folder
    ):
        return (
            "demo "
            + folder
        )

    return (
        "demo "
        + _target_text(
            targets
        )
    ).strip()



# ============================================================
# SUBJECT_PENDING_AND_TEACHER_CANONICAL_V14
#
# Important distinction:
#
# subject_state = informational subject context
#
# special_intent = "subject_teacher_pending"
#     means we explicitly asked:
#     "Teacher aur coaching name?"
#
# ONLY that pending flag is allowed to constrain the next
# teacher/coaching message.
#
# A stale subject_state alone must NEVER block:
#   Sudarshan Sir
#   Prateek Nayak
#   Neeraj Rao
#   Nikhil Seth
#   Aarti Maam
#   Himansu Khatri
# ============================================================


def _v14_teacher_rows(path):
    with _con(path) as db:

        cols = {
            str(x["name"])
            for x in db.execute(
                "PRAGMA table_info(structured_batch_teachers)"
            ).fetchall()
        }

        aliases_expr = (
            "aliases"
            if "aliases" in cols
            else "'' AS aliases"
        )

        rows = db.execute(
            f"""
            SELECT
                id,
                name,
                {aliases_expr}
            FROM structured_batch_teachers
            WHERE enabled=1
            ORDER BY id
            """
        ).fetchall()

    return [
        dict(x)
        for x in rows
    ]


def _v14_clean_teacher_text(value):
    n = _norm(value)

    # Remove conversational words.
    n = re.sub(
        r"\b(?:"
        r"bhai|"
        r"bhaiya|"
        r"please|"
        r"plz|"
        r"mujhe|"
        r"mereko|"
        r"chahiye|"
        r"chahie|"
        r"ka|"
        r"ki|"
        r"ke|"
        r"hai|"
        r"hain|"
        r"batao|"
        r"de do|"
        r"dedo|"
        r"kya|"
        r"koi|"
        r"batch|"
        r"batches|"
        r"course|"
        r"courses|"
        r"available|"
        r"availability|"
        r"mil sakta|"
        r"mil sakti|"
        r"milegi|"
        r"milega|"
        r"wala|"
        r"wali|"
        r"hai kya"
        r")\b",
        " ",
        n,
    )

    # _norm already normalizes/removes common honorifics
    # in this project, but normalize spaces again.
    return re.sub(
        r"\s+",
        " ",
        n,
    ).strip()


def _v14_similarity(a, b):
    from difflib import SequenceMatcher

    a = _v14_clean_teacher_text(a)
    b = _v14_clean_teacher_text(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 100.0

    if b in a:
        return 99.0

    at = a.split()
    bt = b.split()

    aset = set(at)
    bset = set(bt)

    overlap = aset & bset

    # "Nikhil" is enough if unique.
    if overlap:
        longest = max(
            len(x)
            for x in overlap
        )

        if longest >= 5:
            if len(overlap) >= 2:
                return 97.0

            return 88.0

    # Whole-name typo matching.
    whole = (
        SequenceMatcher(
            None,
            a,
            b,
        ).ratio()
        * 100.0
    )

    # Token typo matching:
    # Himanshu -> Himansu
    # Pratik -> Prateek
    # Sait/Sheth -> Seth
    token_scores = []

    for x in at:

        if len(x) < 4:
            continue

        best = 0.0

        for y in bt:

            if len(y) < 4:
                continue

            score = SequenceMatcher(
                None,
                x,
                y,
            ).ratio()

            best = max(
                best,
                score,
            )

        if best:
            token_scores.append(
                best
            )

    token_score = 0.0

    if token_scores:
        token_scores.sort(
            reverse=True
        )

        use = token_scores[:2]

        token_score = (
            sum(use)
            / len(use)
            * 94.0
        )

    return max(
        whole,
        token_score,
    )


def _v14_unique_teacher(
    path,
    text,
):
    rows = _v14_teacher_rows(
        path
    )

    scored = []

    for row in rows:

        names = [
            str(
                row.get("name")
                or ""
            )
        ]

        aliases = str(
            row.get("aliases")
            or ""
        )

        names.extend(
            x.strip()
            for x in re.split(
                r"[,;\n|]+",
                aliases,
            )
            if x.strip()
        )

        best = max(
            (
                _v14_similarity(
                    text,
                    candidate,
                )
                for candidate in names
            ),
            default=0.0,
        )

        scored.append(
            (
                best,
                row,
            )
        )

    scored.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    if not scored:
        return None

    best_score = scored[0][0]

    if best_score < 72.0:
        return None

    if len(scored) > 1:

        second = scored[1][0]

        # Avoid random teacher selection.
        if (
            best_score < 94.0
            and best_score - second < 8.0
        ):
            return None

    return dict(
        scored[0][1]
    )


def _v14_teacherish(text):
    n = _norm(text)

    if not n:
        return False

    # RESOLVER_REPAIR_V24
    #
    # Words such as "latest", "lectures", "2026" and "demo" are
    # modifiers, not reasons to discard a teacher name.  Earlier code
    # rejected the whole sentence whenever one of those words appeared.
    # Judge only the identity-bearing residue instead.
    _teacher_clean = _v14_clean_teacher_text(text)

    _teacher_clean = re.sub(
        r"\b20\d{2}\b",
        " ",
        _teacher_clean,
    )

    _teacher_clean = re.sub(
        r"\b(?:"
        r"price|year|validity|demo|payment|download|"
        r"lecture|lectures|recorded|recording|running|latest|"
        r"new|updated|update|notes|pdf|access"
        r")\b",
        " ",
        _teacher_clean,
    )

    _teacher_clean = re.sub(
        r"\s+",
        " ",
        _teacher_clean,
    ).strip()

    words = _teacher_clean.split()

    if not words:
        return False

    if len(words) > 6:
        return False

    # At least one plausible identity token must remain.
    return any(
        len(word) >= 4
        and not word.isdigit()
        for word in words
    )


# ============================================================
# COACHING_STATE_FIX_V1
#
# Explicit coaching/institute is a NEW identity constraint.
# It invalidates an old exact teacher/batch target.
#
# Examples:
#   Amit Garg -> CSAT
#   "Aur Saarthi IAS"
#       -> clear Amit target
#       -> ask "Sarthi IAS ka kaunsa subject?"
#
#   "Saarthi IAS ka CSAT hai?"
#       -> search ONLY Sarthi IAS + CSAT
#       -> exact unique hit = send
#       -> no hit / ambiguous = SILENT
#
# Generic "koi aur faculty?" never resends the old target.
# ============================================================

def _final_coaching_aliases_for_saved(institute):
    saved = str(institute or "").strip()
    if not saved:
        return set()

    n = _norm(saved)
    aliases = {n}

    # Slash-separated institutes are individually recognizable.
    for part in re.split(r"[/|,]+", saved):
        p = _norm(part)
        if p:
            aliases.add(p)

    manual = {
        "sarthi ias": {
            "sarthi ias", "saarthi ias", "sarthi", "saarthi",
        },
        "ias setu": {
            "ias setu", "iassetu",
        },
        "vision ias": {
            "vision ias",
        },
        "vajiram": {
            "vajiram", "vajiram ravi",
        },
        "unacademy": {
            "unacademy",
        },
        "next ias": {
            "next ias",
        },
        "lukman ias": {
            "lukman ias",
        },
        "pmf ias": {
            "pmf ias",
        },
        "eden ias": {
            "eden ias",
        },
        "physics wallah only ias": {
            "physics wallah", "only ias", "pw only ias",
        },
    }

    for key, vals in manual.items():
        if key == n or key in n or n in key:
            aliases.update(vals)

    return {x for x in aliases if x and x != "ias"}


def _final_detect_coaching(path, text):
    """
    Return one exact saved institute string only when the customer's
    message clearly identifies exactly one enabled coaching/institute.
    """
    q = _norm(text)
    if not q:
        return None

    with _con(path) as db:
        rows = db.execute("""
            SELECT DISTINCT institute
            FROM structured_batch_teachers
            WHERE enabled=1
              AND TRIM(COALESCE(institute,'')) <> ''
            ORDER BY institute
        """).fetchall()

    hits = []

    for row in rows:
        saved = str(row["institute"] or "").strip()
        if not saved:
            continue

        for alias in _final_coaching_aliases_for_saved(saved):
            # Require token boundaries; never match a fragment inside a name.
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", q):
                hits.append(saved)
                break

    # Collapse equivalent spellings/case.
    unique = []
    seen = set()
    for saved in hits:
        k = _norm(saved)
        if k not in seen:
            seen.add(k)
            unique.append(saved)

    return unique[0] if len(unique) == 1 else None


def _final_targets_for_coaching_subject(path, institute, subject):
    """
    Resolve subject only inside the explicitly selected institute.
    Return a target only when there is exactly one unique DB match.
    """
    if not institute or not subject:
        return []

    wanted_inst = _norm(institute)

    with _con(path) as db:
        rows = db.execute("""
            SELECT id, name, institute
            FROM structured_batch_teachers
            WHERE enabled=1
              AND TRIM(COALESCE(institute,'')) <> ''
            ORDER BY id
        """).fetchall()

    targets = []
    keys = set()

    for row in rows:
        if _norm(row["institute"]) != wanted_inst:
            continue

        target = _v13_target_for_teacher_subject(
            path,
            int(row["id"]),
            subject,
        )

        if not target:
            continue

        key = str(target.get("key") or "")
        if key and key not in keys:
            keys.add(key)
            targets.append(target)

    return targets


def _final_coaching_pending_get(aux):
    special = str((aux or {}).get("special_intent") or "")
    prefix = "coaching_subject_pending|"
    if special.startswith(prefix):
        return special[len(prefix):].strip()
    return ""


def _final_other_faculty_request(text):
    n = _norm(text)

    # Only generic alternative-faculty requests. Explicit teacher/coaching
    # messages are handled elsewhere and must not be swallowed here.
    patterns = (
        r"\bkoi\s+aur\s+faculty\b",
        r"\baur\s+faculty\b",
        r"\bkoi\s+aur\s+teacher\b",
        r"\baur\s+teacher\b",
        r"\bany\s+other\s+faculty\b",
        r"\bany\s+other\s+teacher\b",
        r"\bother\s+faculty\b",
        r"\bother\s+teacher\b",
    )

    return any(re.search(p, n) for p in patterns)

# ============================================================
# FINAL_CONVERSATION_STATE_V2
#
# Adds:
# - subject-scoped "other faculty" discovery
# - teacher + multiple-subject selection
# - additive subject intent ("Environment bhi saath mein")
# - combined price total
# - one-hour unknown-batch state with ONE clarification only
# - hard no-random-AI fallback
#
# No saved batches/commands are deleted or rewritten here.
# ============================================================

_FINAL_UNKNOWN_TTL_SECONDS = 60 * 60


def _final_unknown_ensure_schema(path):
    with _con(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS unified_customer_unknown_state (
                chat_id TEXT PRIMARY KEY,
                asked_at REAL NOT NULL DEFAULT 0,
                last_unknown_at REAL NOT NULL DEFAULT 0
            )
        """)
        db.commit()


def _final_unknown_get(path, chat_id):
    _final_unknown_ensure_schema(path)

    with _con(path) as db:
        row = db.execute("""
            SELECT asked_at, last_unknown_at
            FROM unified_customer_unknown_state
            WHERE chat_id=?
        """, (str(chat_id),)).fetchone()

    if not row:
        return {
            "active": False,
            "asked_at": 0.0,
            "last_unknown_at": 0.0,
        }

    asked_at = float(row["asked_at"] or 0)
    last_unknown_at = float(row["last_unknown_at"] or 0)

    active = (
        asked_at > 0
        and time.time() - asked_at <= _FINAL_UNKNOWN_TTL_SECONDS
    )

    if not active:
        _final_unknown_clear(path, chat_id)

    return {
        "active": active,
        "asked_at": asked_at,
        "last_unknown_at": last_unknown_at,
    }


def _final_unknown_mark(path, chat_id):
    _final_unknown_ensure_schema(path)
    now = time.time()

    with _con(path) as db:
        row = db.execute("""
            SELECT asked_at
            FROM unified_customer_unknown_state
            WHERE chat_id=?
        """, (str(chat_id),)).fetchone()

        asked_at = float(row["asked_at"] or 0) if row else 0.0

        if (
            not asked_at
            or now - asked_at > _FINAL_UNKNOWN_TTL_SECONDS
        ):
            asked_at = now

        db.execute("""
            INSERT INTO unified_customer_unknown_state (
                chat_id,
                asked_at,
                last_unknown_at
            )
            VALUES (?,?,?)
            ON CONFLICT(chat_id)
            DO UPDATE SET
                asked_at=excluded.asked_at,
                last_unknown_at=excluded.last_unknown_at
        """, (
            str(chat_id),
            asked_at,
            now,
        ))
        db.commit()


def _final_unknown_clear(path, chat_id):
    _final_unknown_ensure_schema(path)
    with _con(path) as db:
        db.execute("""
            DELETE FROM unified_customer_unknown_state
            WHERE chat_id=?
        """, (str(chat_id),))
        db.commit()


def _final_untrusted_business_request(text):
    """
    Unknown course/batch/faculty inquiry that must never go to generic AI.

    This deliberately catches short requests too:
      SSC course?
      Banking batch?
      ye mil jayega kya?
      iska course kaha milega?
    """
    n = _norm(text)
    if not n:
        return False

    if _neutral(text):
        return False

    if re.search(
        r"\b(?:batch|course|class|classes|lecture|lectures|faculty|teacher|sir|mam|maam|madam)\b",
        n,
    ):
        return True

    if re.search(
        r"\b(?:ssc|cgl|chsl|banking|railway|railways|nda|cds|neet|jee|gate)\b",
        n,
    ):
        return True

    if re.search(
        r"\b(?:"
        r"mil\s+(?:jayega|jaega|jayegi|jaegi)|"
        r"kaha\s+(?:milega|milegi)|"
        r"available\s+(?:hai|h)|"
        r"hai\s+kya"
        r")\b",
        n,
    ):
        return True

    return False


def _final_allowed_sales_fallback(text):
    """
    Preserve the owner's existing generic UPSC/GS combo-sales flow,
    but do not allow random/unknown courses into generic AI.
    """
    n = _norm(text)
    if not n:
        return False

    has_domain = bool(
        re.search(
            r"\b(?:upsc|civil services|cse|gs|general studies)\b",
            n,
        )
    )

    has_purchase = bool(
        re.search(
            r"\b(?:"
            r"course|batch|combo|lecture|lectures|"
            r"chahiye|chahie|lena|leni|join|buy|purchase|interested"
            r")\b",
            n,
        )
    )

    return has_domain and has_purchase


def _final_explicit_subjects(text):
    """
    Return every recognized subject explicitly present in the message.
    Longest aliases win, duplicates are removed, order follows the text.
    """
    n = _norm(text)
    if not n:
        return []

    hits = []

    for canonical, aliases in _V12_SUBJECT_ALIASES.items():
        best_start = None
        best_len = 0

        for alias in aliases:
            a = _norm(alias)
            if not a:
                continue

            m = re.search(
                r"(?<![a-z0-9])"
                + re.escape(a)
                + r"(?![a-z0-9])",
                n,
            )

            if not m:
                continue

            if (
                best_start is None
                or m.start() < best_start
                or (
                    m.start() == best_start
                    and len(a) > best_len
                )
            ):
                best_start = m.start()
                best_len = len(a)

        if best_start is not None:
            hits.append((best_start, -best_len, canonical))

    hits.sort()

    out = []
    seen = set()

    for _, _, canonical in hits:
        key = _norm(canonical)
        if key in seen:
            continue
        seen.add(key)
        out.append(canonical)

    return out


def _final_additive_subject_intent(text):
    n = _norm(text)

    return bool(
        re.search(
            r"\b(?:bhi|also|along\s+with|saath(?:\s+mein)?|add|include)\b",
            n,
        )
        or re.search(
            r"\baur\b.*\b(?:bhi|chahiye|chahie)\b",
            n,
        )
    )


def _final_structured_teacher_id(target):
    try:
        if str(target.get("source") or "") != "structured":
            return None
        return int(target.get("teacher_id"))
    except Exception:
        return None


def _final_unique_active_teacher_id(targets):
    ids = {
        _final_structured_teacher_id(t)
        for t in (targets or [])
        if _final_structured_teacher_id(t) is not None
    }
    return next(iter(ids)) if len(ids) == 1 else None


def _final_unique_active_subject(targets):
    subjects = {
        str(t.get("subject") or "").strip()
        for t in (targets or [])
        if str(t.get("subject") or "").strip()
    }
    return next(iter(subjects)) if len(subjects) == 1 else ""


def _final_targets_for_teacher_subjects(path, teacher_id, subjects):
    result = []
    seen = set()

    for subject in subjects:
        target = _v13_target_for_teacher_subject(
            path,
            teacher_id,
            subject,
        )

        if not target:
            continue

        key = str(target.get("key") or "")
        if key and key in seen:
            continue

        if key:
            seen.add(key)

        result.append(target)

    return result


def _final_merge_targets(existing, additions):
    out = []
    seen = set()

    for target in list(existing or []) + list(additions or []):
        key = str(target.get("key") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(target)

    return out


def _final_price_number(value):
    raw = str(value or "").replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)", raw)
    if not m:
        return None

    try:
        amount = float(m.group(1))
    except Exception:
        return None

    return int(amount) if amount.is_integer() else amount


def _final_combined_selection_text(targets):
    targets = list(targets or [])
    if not targets:
        return ""

    teachers = {
        str(t.get("teacher") or "").strip()
        for t in targets
        if str(t.get("teacher") or "").strip()
    }

    lines = []

    if len(teachers) == 1:
        lines.append(next(iter(teachers)))
        lines.append("")

    all_prices_known = True
    total = 0

    for target in targets:
        subject = str(target.get("subject") or target.get("name") or "").strip()
        price = str(target.get("price") or "").strip()

        if price:
            shown_price = price if price.startswith("₹") else "₹" + price
            lines.append(f"• {subject} — {shown_price}")

            number = _final_price_number(price)
            if number is None:
                all_prices_known = False
            else:
                total += number
        else:
            all_prices_known = False
            lines.append(f"• {subject}")

    if len(targets) > 1 and all_prices_known:
        if isinstance(total, float) and total.is_integer():
            total = int(total)
        lines.extend(["", f"Total: ₹{total}"])

    return "\n".join(lines).strip()


def _final_faculty_choices_for_subject(path, subject, exclude_teacher_ids=None):
    exclude_teacher_ids = {
        int(x)
        for x in (exclude_teacher_ids or set())
        if x is not None
    }

    teacher_ids = _v13_teacher_ids_for_subject(
        path,
        subject,
    )

    rows_by_id = {
        int(row["id"]): row
        for row in _v13_teacher_rows(path)
    }

    choices = []

    for teacher_id in sorted(teacher_ids):
        if teacher_id in exclude_teacher_ids:
            continue

        teacher = rows_by_id.get(int(teacher_id))
        if not teacher:
            continue

        target = _v13_target_for_teacher_subject(
            path,
            int(teacher_id),
            subject,
        )

        if not target:
            continue

        choices.append({
            "teacher_id": int(teacher_id),
            "teacher_name": str(teacher.get("name") or "").strip(),
            "target": target,
        })

    choices.sort(
        key=lambda x: _norm(x["teacher_name"])
    )

    return choices


def _final_faculty_pending_subject(aux):
    special = str((aux or {}).get("special_intent") or "")
    prefix = "faculty_choice_pending|"

    if special.startswith(prefix):
        return special[len(prefix):].strip()

    return ""


def _final_other_faculty_request_v2(text):
    n = _norm(text)

    patterns = (
        r"\bkoi\s+aur\s+faculty\b",
        r"\baur\s+koi\s+faculty\b",
        r"\baur\s+faculty\b",
        r"\bdusre?\s+faculty\b",
        r"\bdusra\s+faculty\b",
        r"\bkoi\s+aur\s+teacher\b",
        r"\baur\s+koi\s+teacher\b",
        r"\baur\s+teacher\b",
        r"\bdusre?\s+teacher\b",
        r"\bdusra\s+teacher\b",
        r"\bany\s+other\s+faculty\b",
        r"\bany\s+other\s+teacher\b",
        r"\bother\s+faculty\b",
        r"\bother\s+teacher\b",
        r"\baur\s+koi\s+dusra\b",
        r"\baur\s+koi\s+dusre\b",
        r"\bkoi\s+aur\s+dusra\b",
        r"\bkoi\s+aur\s+dusre\b",
    )

    return any(re.search(p, n) for p in patterns)

# ============================================================
# FACULTY_ACTION_MESSAGE_FIX_V3
#
# Teacher identity must still be detected when the same message
# contains an action/intent word such as "demo".
#
# Examples:
#   "Pallavi ma'am ka bhejna bhai demo"
#   "Rajesh sir ka bhejna bhai demo"
#
# The old V14 guard intentionally refused teacher detection for
# messages containing "demo", which caused the PREVIOUS exact
# teacher target to be reused.  This helper strips action/request
# words first, then performs the existing saved-teacher matcher.
# It NEVER creates a teacher from subject words or generic text.
# ============================================================

def _final_teacher_from_action_message(path, text):
    n = _norm(text)

    if not n:
        return None

    # Only activate this path for an explicit action/fact request.
    if not re.search(
        r"\b(?:"
        r"demo|price|year|validity|payment|link|download|"
        r"bhej|bhejo|bhejna|send|show|dikhao|dikhana"
        r")\b",
        n,
    ):
        return None

    cleaned = n

    # Remove intent/action/customer filler while preserving the actual
    # teacher identity and honorifics (sir/mam/maam).
    cleaned = re.sub(
        r"\b(?:"
        r"demo|price|year|validity|payment|link|download|"
        r"bhej|bhejo|bhejna|bhejiye|send|show|dikhao|dikhana|"
        r"bhai|bro|please|plz|mujhe|mereko|"
        r"ka|ki|ke|wala|wali|wla|"
        r"chahiye|chahie|de|do|dedo|de do|"
        r"hai|hain|h|kya|batao|bata|"
        r"batch|course|class|classes|lecture|lectures"
        r")\b",
        " ",
        cleaned,
    )

    cleaned = re.sub(
        r"\s+",
        " ",
        cleaned,
    ).strip()

    if not cleaned:
        return None

    # Avoid treating a bare subject/GS word as a teacher.
    if _v12_explicit_subject(cleaned):
        subject_only = _v12_explicit_subject(cleaned)
        if _norm(cleaned) in {
            _norm(subject_only),
            "gs1", "gs 1", "gs2", "gs 2",
            "gs3", "gs 3", "gs4", "gs 4",
        }:
            return None

    return _v14_unique_teacher(
        path,
        cleaned,
    )

async def process_unified_customer_context(
    update,
    context,
    path,
    customer_text,
):
    """
    Unified customer state V3.

    Priority:
      1. replied-to known batch
      2. explicit known DB batch in current message
      3. fresh active batch
      4. clarification
      5. unknown DB batch -> silence

    Returns:
        (handled, effective_customer_text)
    """

    if not getattr(
        update,
        "effective_chat",
        None,
    ):
        return False, customer_text

    chat_id = update.effective_chat.id

    raw_text = str(
        customer_text
        or ""
    )

    visible_text = _without_server_context(
        raw_text
    )

    # ========================================================
    # ACKNOWLEDGEMENT HARD STOP V17
    #
    # Acknowledgements/end-of-turn messages must NEVER:
    #   - resend the selected batch
    #   - trigger old course state
    #   - trigger AI
    #   - trigger structured batch discovery
    #
    # Keep existing state intact for its normal TTL, but send
    # absolutely no response for this message.
    # ========================================================

    _v17_ack = _norm(
        visible_text
    )

    _v17_acknowledgements = {
        "",
        ".",
        "..",
        "...",
        "ok",
        "okay",
        "okk",
        "okkk",
        "ok bhai",
        "okay bhai",
        "alright",
        "all right",
        "fine",
        "acha",
        "achha",
        "acha hai",
        "achha hai",
        "accha hai",
        "thik",
        "theek",
        "thik hai",
        "theek hai",
        "thik h",
        "theek h",
        "ठीक",
        "ठीक है",
        "ठीक भाई",
        "ठीक है भाई",
        "अच्छा",
        "अच्छा है",
        "अच्छा भाई",
        "अच्छा है भाई",
        "haan theek hai",
        "haan thik hai",
        "ha theek hai",
        "ha thik hai",
        "got it",
        "samajh gaya",
        "samajh gya",
        "samajh gaya bhai",
        "samajh gya bhai",
        "thanks",
        "thank you",
        "thx",
    }

    if (
        _v17_ack in _v17_acknowledgements
        or _v17_ack in {
            "okay thanks",
            "ok thanks",
            "okay thank you",
            "ok thank you",
            "thanks bhai",
            "thank you bhai",
            "thanks bro",
            "thank you bro",
        }
    ):

        logger.info(
            "V17 ACK SILENT "
            "chat=%s message=%r",
            chat_id,
            visible_text,
        )

        return True, visible_text

    # ========================================================
    # DIRECT_REPLY_REFERENCE_V11
    #
    # Real Telegram reply context has absolute priority.
    # ========================================================

    _telegram_reply_text = _direct_reply_reference(
        update
    )

    _server_reply_text = _reply_reference(
        raw_text
    )

    reply_text = (
        _telegram_reply_text
        or _server_reply_text
    )

    if reply_text:
        logger.info(
            "DIRECT REPLY REFERENCE V11 "
            "chat=%s telegram=%r server=%r selected=%r",
            chat_id,
            _telegram_reply_text,
            _server_reply_text,
            reply_text,
        )

    # ========================================================
    # VALIDITY_REPLY_STATE_V10
    #
    # Validity is a global business rule:
    #   Lifetime access rahega.
    #
    # BUT if the validity question is sent as a reply to a
    # known batch/course message, that replied target must
    # become the active state BEFORE we return.
    #
    # Example:
    #
    # Reply to:
    #   Jatin Gupta — Polity
    #
    # Customer:
    #   "Bhaiya iska validity kab tak rahega?"
    #
    # Result:
    #   active state = Jatin Gupta — Polity
    #   reply        = Lifetime access rahega.
    #
    # Next:
    #   "Aur price?"
    #
    # continues with Jatin Gupta — Polity.
    # ========================================================

    if _is_validity_query(
        visible_text
    ):

        # DIRECT_REPLY_REFERENCE_V11
        #
        # Use the already selected REAL Telegram reply target.
        # Do not re-read only the legacy text marker.
        _validity_reply_text = reply_text

        _validity_reply_targets = []

        if _validity_reply_text:
            _validity_reply_targets = resolve_targets(
                path,
                _validity_reply_text,
            )

        if _validity_reply_targets:

            _save_state(
                path,
                chat_id,
                targets=_validity_reply_targets,
                pending_intent="",
                pending_unknown="",
            )

            logger.info(
                "VALIDITY REPLY STATE V10 "
                "chat=%s target=%r reference=%r",
                chat_id,
                _target_text(
                    _validity_reply_targets
                ),
                _validity_reply_text,
            )

        else:
            # ------------------------------------------------
            # If there is no reply target, keep any existing
            # fresh state untouched. Do NOT invent or switch
            # to Pro Pack or any other combo.
            # ------------------------------------------------
            _existing_validity_state = _load_state(
                path,
                chat_id,
            )

            if (
                _existing_validity_state.get("fresh")
                and _existing_validity_state.get("targets")
            ):
                _save_state(
                    path,
                    chat_id,
                    targets=_existing_validity_state[
                        "targets"
                    ],
                    pending_intent="",
                    pending_unknown="",
                )

        await _send_lifetime_access(
            update,
            context,
        )

        logger.info(
            "GLOBAL VALIDITY LIFETIME V10 "
            "chat=%s message=%r",
            chat_id,
            visible_text,
        )

        return (
            True,
            visible_text,
        )


    state = _load_state(
        path,
        chat_id,
    )

    old_targets = state.get(
        "targets",
        [],
    )

    pending = state.get(
        "pending_intent",
        "",
    )

    _final_unknown_state = _final_unknown_get(
        path,
        chat_id,
    )
    _final_unknown_active = bool(
        _final_unknown_state.get("active")
    )

    # ========================================================
    # STATE_CONSOLIDATION_V13_DISABLED
    #
    # V13 partial teacher/subject interception is disabled
    # because it was blocking the original working resolver.
    #
    # Existing V8-V12 behavior continues below.
    # ========================================================

    # ========================================================
    # CONTEXT_BUSINESS_RULES_V12
    # ========================================================

    _v12_aux = _v12_aux_get(
        path,
        chat_id,
    )


    # ========================================================
    # RESOLVER_REPAIR_V25 — AUTHORITATIVE SAVED TARGET FIRST
    #
    # A fully specified saved target must beat:
    #   * coaching-only clarification,
    #   * global fuzzy teacher rewriting,
    #   * "latest/lectures" fact/demo fallback.
    #
    # Examples:
    #   Sarthi IAS Anthropology
    #   Rankers Classes Commerce Optional
    #   Vision IAS Sociology — Paper 1
    #   Shivin Sir — GS3 Module
    #   Piyush Chaubey PSIR
    #
    # Transactional/fact questions remain owned by their existing flows.
    # Subject-teacher clarification state also remains subject-scoped.
    # ========================================================

    _v25_candidate = None
    _v25_candidate_reason = ""

    if (
        not pending
        and _v12_aux.get("special_intent")
            != "subject_teacher_pending"
    ):
        (
            _v25_candidate,
            _v25_candidate_reason,
        ) = _v25_selection_candidate(
            path,
            visible_text,
        )

    if (
        _v25_candidate
        and _v25_is_selection_message(
            visible_text,
            _v25_candidate,
            _v25_candidate_reason,
        )
    ):
        _v25_commit_single_target(
            path,
            chat_id,
            _v25_candidate,
        )

        # STATE_RESTORE_V30_MINIMAL route lock metadata.  This does not alter
        # state; it only tells bot.py which renderer owns the already-selected
        # target so folder/structured routers cannot rematch it globally.
        setattr(update, "_subject_boundary_target_key", str(_v25_candidate.get("key") or ""))
        setattr(update, "_subject_boundary_target_source", str(_v25_candidate.get("source") or ""))

        logger.info(
            "RESOLVER_REPAIR_V25 EARLY SAVED TARGET "
            "chat=%s reason=%s target=%r message=%r",
            chat_id,
            _v25_candidate_reason,
            _target_text([_v25_candidate]),
            visible_text,
        )

        return (
            False,
            _target_text(
                [_v25_candidate]
            ),
        )

    # ========================================================
    # COACHING_STATE_FIX_V1 — EXPLICIT COACHING TRANSITION
    #
    # A newly named coaching/institute is authoritative.
    # It must invalidate any old exact batch/teacher target.
    # ========================================================

    _final_coaching = _final_detect_coaching(
        path,
        visible_text,
    )
    _final_subject_for_coaching = _v12_explicit_subject(
        visible_text
    )
    _final_pending_coaching = _final_coaching_pending_get(
        _v12_aux
    )

    # Explicit new coaching name always clears old exact teacher/batch state.
    if _final_coaching:

        _final_unknown_clear(
            path,
            chat_id,
        )
        _final_unknown_active = False

        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent="",
            pending_unknown="",
        )

        _v13_clear(
            path,
            chat_id,
        )

        # Coaching + subject in the same message:
        # search ONLY inside this coaching. No hit/ambiguity => silence.
        if _final_subject_for_coaching:

            _coaching_targets = _final_targets_for_coaching_subject(
                path,
                _final_coaching,
                _final_subject_for_coaching,
            )

            if len(_coaching_targets) == 1:

                _coaching_target = _coaching_targets[0]

                _save_state(
                    path,
                    chat_id,
                    targets=[_coaching_target],
                    pending_intent="",
                    pending_unknown="",
                )

                _v13_set(
                    path,
                    chat_id,
                    teacher_id=_coaching_target.get("teacher_id"),
                    teacher_name=str(
                        _coaching_target.get("teacher") or ""
                    ),
                    subject=str(
                        _coaching_target.get("subject")
                        or _final_subject_for_coaching
                    ),
                )

                _v12_aux_set(
                    path,
                    chat_id,
                    subject=str(
                        _coaching_target.get("subject")
                        or _final_subject_for_coaching
                    ),
                    special_intent="",
                )

                logger.info(
                    "COACHING STATE EXACT HIT "
                    "chat=%s coaching=%r subject=%r target=%r",
                    chat_id,
                    _final_coaching,
                    _final_subject_for_coaching,
                    _target_text([_coaching_target]),
                )

                return False, _target_text([_coaching_target])

            _v12_aux_set(
                path,
                chat_id,
                subject=_final_subject_for_coaching,
                special_intent=(
                    "coaching_subject_pending|"
                    + _final_coaching
                ),
            )

            logger.info(
                "COACHING STATE SUBJECT MISS/AMBIGUOUS SILENT "
                "chat=%s coaching=%r subject=%r matches=%s",
                chat_id,
                _final_coaching,
                _final_subject_for_coaching,
                len(_coaching_targets),
            )

            return True, visible_text

        # Coaching only: ask for subject; never resend old target.
        _v12_aux_set(
            path,
            chat_id,
            subject="",
            special_intent=(
                "coaching_subject_pending|"
                + _final_coaching
            ),
        )

        await _send(
            update,
            context,
            f"{_final_coaching} ka kaunsa subject?",
        )

        logger.info(
            "COACHING STATE PENDING "
            "chat=%s coaching=%r message=%r",
            chat_id,
            _final_coaching,
            visible_text,
        )

        return True, visible_text

    # Coaching was selected in the immediately preceding context and
    # the customer now gives a subject. Search only that coaching.
    if (
        _final_pending_coaching
        and _final_subject_for_coaching
    ):

        _coaching_targets = _final_targets_for_coaching_subject(
            path,
            _final_pending_coaching,
            _final_subject_for_coaching,
        )

        if len(_coaching_targets) == 1:

            _coaching_target = _coaching_targets[0]

            _save_state(
                path,
                chat_id,
                targets=[_coaching_target],
                pending_intent="",
                pending_unknown="",
            )

            _v13_set(
                path,
                chat_id,
                teacher_id=_coaching_target.get("teacher_id"),
                teacher_name=str(
                    _coaching_target.get("teacher") or ""
                ),
                subject=str(
                    _coaching_target.get("subject")
                    or _final_subject_for_coaching
                ),
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=str(
                    _coaching_target.get("subject")
                    or _final_subject_for_coaching
                ),
                special_intent="",
            )

            logger.info(
                "COACHING PENDING SUBJECT HIT "
                "chat=%s coaching=%r subject=%r target=%r",
                chat_id,
                _final_pending_coaching,
                _final_subject_for_coaching,
                _target_text([_coaching_target]),
            )

            return False, _target_text([_coaching_target])

        # Wrong/unavailable subject for selected coaching:
        # stay silent and retain coaching constraint.
        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent="",
            pending_unknown="",
        )

        _v13_clear(
            path,
            chat_id,
        )

        _v12_aux_set(
            path,
            chat_id,
            subject=_final_subject_for_coaching,
            special_intent=(
                "coaching_subject_pending|"
                + _final_pending_coaching
            ),
        )

        logger.info(
            "COACHING PENDING SUBJECT MISS/AMBIGUOUS SILENT "
            "chat=%s coaching=%r subject=%r matches=%s",
            chat_id,
            _final_pending_coaching,
            _final_subject_for_coaching,
            len(_coaching_targets),
        )

        return True, visible_text

    # ========================================================
    # OTHER FACULTY V2 — SUBJECT-SCOPED DISCOVERY
    #
    # Known subject:
    #   Sudarshan Geography -> "koi aur faculty?"
    #   -> list every OTHER enabled Geography faculty.
    #
    # Unknown subject:
    #   "koi aur faculty?"
    #   -> SILENT.
    # ========================================================

    if _final_other_faculty_request_v2(visible_text):

        _other_subjects_now = _final_explicit_subjects(
            visible_text
        )

        _other_subject = (
            _other_subjects_now[0]
            if len(_other_subjects_now) == 1
            else _final_unique_active_subject(old_targets)
        )

        if not _other_subject:
            _other_partial = _v13_get(
                path,
                chat_id,
            )

            if _other_partial.get("fresh"):
                _other_subject = str(
                    _other_partial.get("subject")
                    or ""
                ).strip()

        if not _other_subject:
            logger.info(
                "OTHER FACULTY NO SUBJECT SILENT "
                "chat=%s message=%r",
                chat_id,
                visible_text,
            )
            return True, visible_text

        _exclude_ids = {
            _final_structured_teacher_id(t)
            for t in old_targets
            if _final_structured_teacher_id(t) is not None
        }

        _faculty_choices = _final_faculty_choices_for_subject(
            path,
            _other_subject,
            exclude_teacher_ids=_exclude_ids,
        )

        if not _faculty_choices:
            logger.info(
                "OTHER FACULTY NO ALTERNATIVE SILENT "
                "chat=%s subject=%r",
                chat_id,
                _other_subject,
            )
            return True, visible_text

        # One alternative: exact answer immediately.
        if len(_faculty_choices) == 1:
            _alternative_target = _faculty_choices[0]["target"]

            _save_state(
                path,
                chat_id,
                targets=[_alternative_target],
                pending_intent="",
                pending_unknown="",
            )

            _v13_set(
                path,
                chat_id,
                teacher_id=_alternative_target.get("teacher_id"),
                teacher_name=str(
                    _alternative_target.get("teacher")
                    or ""
                ),
                subject=str(
                    _alternative_target.get("subject")
                    or _other_subject
                ),
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=str(
                    _alternative_target.get("subject")
                    or _other_subject
                ),
                special_intent="",
            )

            _final_unknown_clear(
                path,
                chat_id,
            )

            return False, _target_text([_alternative_target])

        # Multiple alternatives: list teachers and keep ONLY the subject.
        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent="",
            pending_unknown="",
        )

        _v13_set(
            path,
            chat_id,
            teacher_id=None,
            teacher_name="",
            subject=_other_subject,
        )

        _v12_aux_set(
            path,
            chat_id,
            subject=_other_subject,
            special_intent=(
                "faculty_choice_pending|"
                + _other_subject
            ),
        )

        _names = [
            c["teacher_name"]
            for c in _faculty_choices
            if c["teacher_name"]
        ]

        await _send(
            update,
            context,
            (
                f"{_other_subject} ke available faculties:\n\n"
                + "\n".join(
                    f"• {name}"
                    for name in _names
                )
                + "\n\nChoose faculty."
            ),
        )

        logger.info(
            "OTHER FACULTY LIST "
            "chat=%s subject=%r choices=%r",
            chat_id,
            _other_subject,
            _names,
        )

        return True, visible_text

    # ========================================================
    # STATE_ISOLATION_FIX_V2
    #
    # Payment is a workflow intent, not a course-state mutation.
    # Unsupported exam/course families must reach unknown handling
    # before V15 teacher fuzziness/state can remap them.
    # ========================================================

    _v2_intent = _context_intent(visible_text)

    if _v2_intent == "payment":
        logger.info(
            "STATE_ISOLATION_V2 PAYMENT BYPASS chat=%s message=%r",
            chat_id,
            visible_text,
        )
        return False, visible_text

    _v2_unsupported_exam = bool(
        re.search(
            r"\b(?:"
            r"ssc|cgl|chsl|banking|railway|railways|ntpc|"
            r"neet|jee|gate|nda|cds"
            r")\b",
            _norm(visible_text),
        )
    )

    if _v2_unsupported_exam:
        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent="",
            pending_unknown=visible_text,
            refresh=False,
        )

        _v13_clear(path, chat_id)

        _v12_aux_set(
            path,
            chat_id,
            subject="",
            special_intent="",
        )

        if _final_unknown_active:
            _final_unknown_mark(path, chat_id)

            logger.info(
                "STATE_ISOLATION_V2 UNSUPPORTED REPEAT SILENT "
                "chat=%s message=%r",
                chat_id,
                visible_text,
            )

            return True, visible_text

        _final_unknown_mark(path, chat_id)

        await _send(
            update,
            context,
            "Bhai, batch name aur teacher name bata dijiye.",
        )

        logger.info(
            "STATE_ISOLATION_V2 UNSUPPORTED ASK ONCE "
            "chat=%s message=%r",
            chat_id,
            visible_text,
        )

        return True, visible_text


    # ========================================================
    # RESOLVER_REPAIR_V24_2 — PENDING SUBJECT FIRST
    #
    # If we explicitly asked "Teacher aur coaching name?", the next
    # identity-bearing message MUST be resolved inside that subject before
    # any global fuzzy teacher matcher runs.
    #
    # Example:
    #   PSIR chahiye -> ask once
    #   Ranjan      -> Subhra Ranjan PSIR
    #
    # Without this guard, global structured-teacher fuzziness can wrongly
    # interpret "Ranjan" as a different teacher such as "Pranjal" and
    # jump to another subject before the PSIR-scoped resolver gets a turn.
    # ========================================================

    _v24_2_pending_subject = (
        str(_v12_aux.get("subject") or "").strip()
        if (
            _v12_aux.get("fresh")
            and _v12_aux.get("special_intent")
                == "subject_teacher_pending"
        )
        else ""
    )

    _v24_2_current_subject = _v12_explicit_subject(
        visible_text
    )

    # Explicit ONE-subject messages also get subject-scoped identity
    # resolution before global teacher fuzziness.  This is what makes
    # "Ranjan PSIR" resolve Subhra Ranjan instead of being fuzzily
    # interpreted as an unrelated structured teacher such as Pranjal.
    _v24_2_subjects_now = _final_explicit_subjects(
        visible_text
    )

    if (
        _v24_2_current_subject
        and len(_v24_2_subjects_now) == 1
    ):
        _v24_2_direct_scoped = _v12_resolve_within_subject(
            path,
            visible_text,
            _v24_2_current_subject,
        )

        if _v24_2_direct_scoped:
            _save_state(
                path,
                chat_id,
                targets=_v24_2_direct_scoped,
                pending_intent="",
                pending_unknown="",
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=_v24_2_current_subject,
                special_intent="",
            )

            try:
                _v13_clear(path, chat_id)
            except Exception:
                pass

            logger.info(
                "RESOLVER_REPAIR_V24_2 EARLY SUBJECT TARGET "
                "chat=%s subject=%r target=%r message=%r",
                chat_id,
                _v24_2_current_subject,
                _target_text(_v24_2_direct_scoped),
                visible_text,
            )

            return False, _target_text(_v24_2_direct_scoped)

    if (
        _v24_2_pending_subject
        and not _v24_2_current_subject
    ):

        _v24_2_scoped = _v12_resolve_within_subject(
            path,
            visible_text,
            _v24_2_pending_subject,
        )

        if _v24_2_scoped:
            _save_state(
                path,
                chat_id,
                targets=_v24_2_scoped,
                pending_intent="",
                pending_unknown="",
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=_v24_2_pending_subject,
                special_intent="",
            )

            try:
                _v13_clear(
                    path,
                    chat_id,
                )
            except Exception:
                pass

            logger.info(
                "RESOLVER_REPAIR_V24_2 PENDING SUBJECT TARGET "
                "chat=%s subject=%r target=%r message=%r",
                chat_id,
                _v24_2_pending_subject,
                _target_text(_v24_2_scoped),
                visible_text,
            )

            return (
                False,
                _target_text(_v24_2_scoped),
            )

        # The customer has supplied identity-bearing text in answer to the
        # clarification, but it is not valid inside the pending subject.
        # Stay silent instead of allowing global fuzzy matching to jump to a
        # different subject or repeating the same clarification.
        _v24_2_identity = _v24_subject_identity_tokens(
            visible_text,
            _v24_2_pending_subject,
        )

        if _v24_2_identity:
            logger.info(
                "RESOLVER_REPAIR_V24_2 PENDING SUBJECT UNKNOWN SILENT "
                "chat=%s subject=%r identity=%r message=%r",
                chat_id,
                _v24_2_pending_subject,
                _v24_2_identity,
                visible_text,
            )
            return True, visible_text

    # ========================================================
    # AUTHORITATIVE_CONSTRAINT_RESOLVER_V15
    #
    # Consolidates teacher + subject BEFORE broad V12 subject
    # interception.  V13's table is used only as a temporary
    # constraint store; unified_customer_context remains the
    # authority once an exact DB target is resolved.
    # ========================================================

    _v15_subject = _v12_explicit_subject(visible_text)
    _v15_partial = _v13_get(path, chat_id)

    # STATE_RESTORE_V30_MINIMAL
    # A pure subject-only message (CSAT, IR, Geography, History, etc.) is a
    # subject constraint, not a teacher identity.  In particular, aliases like
    # "IAS Setu CSAT" must not make bare "CSAT" look like Amit Garg Sir.
    _v30_broad_subject_only = bool(
        _v15_subject
        and _v12_is_broad_subject_request(visible_text, _v15_subject)
    )

    _v15_teacher = (
        None
        if _v30_broad_subject_only
        else _final_teacher_from_action_message(path, visible_text)
    )

    if (
        not _v30_broad_subject_only
        and _v15_teacher is None
        and _v14_teacherish(visible_text)
    ):
        _v15_teacher = _v14_unique_teacher(path, visible_text)

    _v15_teacher_id = (
        int(_v15_teacher["id"])
        if _v15_teacher
        else (
            _v15_partial.get("teacher_id")
            if _v15_partial.get("fresh")
            else None
        )
    )

    _v15_teacher_name = (
        str(_v15_teacher.get("name") or "").strip()
        if _v15_teacher
        else (
            str(_v15_partial.get("teacher_name") or "").strip()
            if _v15_partial.get("fresh")
            else ""
        )
    )

    # Any recognized teacher is trusted and exits the unknown state.
    if _v15_teacher:
        _final_unknown_clear(
            path,
            chat_id,
        )
        _final_unknown_active = False

    # --------------------------------------------------------
    # FACULTY CHOICE PENDING
    #
    # Geography alternatives were shown. If customer now says
    # "Pallavi ma'am", resolve Pallavi ONLY inside Geography.
    # --------------------------------------------------------

    _faculty_pending_subject = _final_faculty_pending_subject(
        _v12_aux
    )

    if _faculty_pending_subject and _v15_teacher:

        _faculty_exact = _v13_target_for_teacher_subject(
            path,
            int(_v15_teacher["id"]),
            _faculty_pending_subject,
        )

        if _faculty_exact:

            _save_state(
                path,
                chat_id,
                targets=[_faculty_exact],
                pending_intent="",
                pending_unknown="",
            )

            _v13_set(
                path,
                chat_id,
                teacher_id=int(_v15_teacher["id"]),
                teacher_name=_v15_teacher_name,
                subject=str(
                    _faculty_exact.get("subject")
                    or _faculty_pending_subject
                ),
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=str(
                    _faculty_exact.get("subject")
                    or _faculty_pending_subject
                ),
                special_intent="",
            )

            logger.info(
                "FACULTY CHOICE EXACT "
                "chat=%s teacher=%r subject=%r target=%r",
                chat_id,
                _v15_teacher_name,
                _faculty_pending_subject,
                _target_text([_faculty_exact]),
            )

            return False, _target_text([_faculty_exact])

        # Chosen teacher doesn't teach this subject: keep subject pending.
        logger.info(
            "FACULTY CHOICE MISS SILENT "
            "chat=%s teacher=%r subject=%r",
            chat_id,
            _v15_teacher_name,
            _faculty_pending_subject,
        )

        return True, visible_text

    # --------------------------------------------------------
    # MULTI-SUBJECT / ADDITIVE SUBJECT SELECTION
    # --------------------------------------------------------

    _all_subjects_now = _final_explicit_subjects(
        visible_text
    )

    _active_structured_teacher_id = _final_unique_active_teacher_id(
        old_targets
    )

    _multi_teacher_id = (
        int(_v15_teacher["id"])
        if _v15_teacher
        else (
            int(_v15_partial.get("teacher_id"))
            if (
                _v15_partial.get("fresh")
                and _v15_partial.get("teacher_id") is not None
            )
            else _active_structured_teacher_id
        )
    )

    _multi_teacher_name = (
        _v15_teacher_name
        or (
            str(old_targets[0].get("teacher") or "").strip()
            if (
                old_targets
                and _active_structured_teacher_id
            )
            else ""
        )
    )

    # "Geography and Environment chahiye" under one teacher.
    if _multi_teacher_id and len(_all_subjects_now) >= 2:

        _multi_targets = _final_targets_for_teacher_subjects(
            path,
            _multi_teacher_id,
            _all_subjects_now,
        )

        if _multi_targets:

            _save_state(
                path,
                chat_id,
                targets=_multi_targets,
                pending_intent="",
                pending_unknown="",
            )

            _v13_set(
                path,
                chat_id,
                teacher_id=_multi_teacher_id,
                teacher_name=_multi_teacher_name,
                subject=str(
                    _multi_targets[-1].get("subject")
                    or ""
                ),
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=str(
                    _multi_targets[-1].get("subject")
                    or ""
                ),
                special_intent="",
            )

            await _send(
                update,
                context,
                _final_combined_selection_text(
                    _multi_targets
                ),
            )

            logger.info(
                "MULTI SUBJECT EXACT "
                "chat=%s teacher=%r subjects=%r targets=%r",
                chat_id,
                _multi_teacher_name,
                _all_subjects_now,
                _target_text(_multi_targets),
            )

            return True, visible_text

        return True, visible_text

    # "Environment bhi saath mein" adds to existing Geography.
    if (
        old_targets
        and _active_structured_teacher_id
        and _all_subjects_now
        and _final_additive_subject_intent(visible_text)
    ):

        _additions = _final_targets_for_teacher_subjects(
            path,
            _active_structured_teacher_id,
            _all_subjects_now,
        )

        if _additions:

            _combined_targets = _final_merge_targets(
                old_targets,
                _additions,
            )

            _save_state(
                path,
                chat_id,
                targets=_combined_targets,
                pending_intent="",
                pending_unknown="",
            )

            _v13_set(
                path,
                chat_id,
                teacher_id=_active_structured_teacher_id,
                teacher_name=str(
                    old_targets[0].get("teacher")
                    or ""
                ),
                subject=str(
                    _combined_targets[-1].get("subject")
                    or ""
                ),
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=str(
                    _combined_targets[-1].get("subject")
                    or ""
                ),
                special_intent="",
            )

            await _send(
                update,
                context,
                _final_combined_selection_text(
                    _combined_targets
                ),
            )

            logger.info(
                "ADDITIVE SUBJECT EXACT "
                "chat=%s teacher_id=%s subjects=%r targets=%r",
                chat_id,
                _active_structured_teacher_id,
                _all_subjects_now,
                _target_text(_combined_targets),
            )

            return True, visible_text

    _v15_subject_for_resolution = (
        _v15_subject
        or (
            str(_v12_aux.get("subject") or "").strip()
            if (
                _v12_aux.get("fresh")
                and _v12_aux.get("special_intent")
                    == "subject_teacher_pending"
            )
            else ""
        )
        or (
            str(_v15_partial.get("subject") or "").strip()
            if _v15_partial.get("fresh")
            else ""
        )
    )

    # A new teacher plus a new/retained subject, or a retained
    # teacher plus a new subject, must resolve as one DB target.
    if _v15_teacher_id and _v15_subject_for_resolution:
        _v15_exact = _v13_target_for_teacher_subject(
            path,
            _v15_teacher_id,
            _v15_subject_for_resolution,
        )

        # V15.2 CONTEXTUAL INTENT GUARD
        #
        # A pure follow-up such as:
        #   "Year kya hai batch ka"
        #   "Price kya h iska"
        #   "Demo vejna"
        #
        # may inherit teacher+subject from V12/V13 state.  That inherited
        # combination must NOT be treated as a NEW explicit batch selection,
        # otherwise this branch returns the canonical batch name and the
        # structured batch router sends the complete batch card again.
        #
        # Let the later authoritative intent handlers operate on old_targets.
        _v15_context_intent = _context_intent(
            visible_text
        )

        # STATE_ISOLATION_FIX_V1
        # If the current message contributes neither teacher nor subject,
        # teacher+subject coming only from V13 are remembered context, not
        # a new authoritative batch selection.
        _v15_inherited_only = (
            bool(old_targets)
            and not _v15_teacher
            and not _v15_subject
        )

        _v15_pure_followup = (
            _v15_inherited_only
            and bool(_v15_context_intent)
        )

        if _v15_pure_followup:
            logger.info(
                "V15_2 CONTEXT FOLLOWUP BYPASS "
                "chat=%s intent=%s target=%r message=%r",
                chat_id,
                _v15_context_intent,
                _target_text(old_targets),
                visible_text,
            )

        if _v15_exact and not _v15_inherited_only:
            _save_state(
                path,
                chat_id,
                targets=[_v15_exact],
                pending_intent="",
                pending_unknown="",
            )
            _v12_aux_set(
                path,
                chat_id,
                subject=str(
                    _v15_exact.get("subject")
                    or _v15_subject_for_resolution
                    or ""
                ).strip(),
                special_intent="",
            )
            _v13_set(
                path,
                chat_id,
                teacher_id=_v15_teacher_id,
                teacher_name=_v15_teacher_name,
                subject=str(
                    _v15_exact.get("subject")
                    or _v15_subject_for_resolution
                    or ""
                ).strip(),
            )
            logger.info(
                "V15 AUTHORITATIVE EXACT TARGET "
                "chat=%s teacher=%r subject=%r target=%r",
                chat_id,
                _v15_teacher_name,
                _v15_subject_for_resolution,
                _target_text([_v15_exact]),
            )
            return False, _target_text([_v15_exact])

        # Explicit teacher+subject that cannot be resolved must
        # not fall through and get substituted by another batch.
        if _v15_teacher and _v15_subject:
            logger.info(
                "V15 EXPLICIT TEACHER SUBJECT UNKNOWN SILENT "
                "chat=%s teacher=%r subject=%r message=%r",
                chat_id,
                _v15_teacher_name,
                _v15_subject,
                visible_text,
            )
            return True, visible_text

    # ========================================================
    # BATCH_INTEGRITY_V18 — EXPLICIT TEACHER TRANSITION
    #
    # A newly mentioned teacher is authoritative.
    #
    # It must NEVER leave an old exact batch (for example
    # Sudarshan Environment) active behind a newly selected
    # teacher such as Neeraj Rao.
    #
    # Single-subject teacher:
    #   teacher -> exact saved target automatically.
    #
    # Multi-subject teacher:
    #   clear old exact target -> teacher constraint -> choices.
    # ========================================================

    if _v15_teacher and not _v15_subject:

        _v18_teacher_id = int(
            _v15_teacher["id"]
        )

        with _con(path) as _v18_db:

            _v18_subject_rows = _v18_db.execute(
                """
                SELECT
                    id,
                    name,
                    availability
                FROM structured_batch_subjects
                WHERE teacher_id=?
                  AND enabled=1
                ORDER BY
                    sort_order,
                    id
                """,
                (_v18_teacher_id,),
            ).fetchall()

        _v18_subjects = [
            dict(x)
            for x in _v18_subject_rows
        ]

        # New explicit teacher invalidates the previous exact
        # course immediately.
        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent="",
            pending_unknown="",
        )

        _v13_clear(
            path,
            chat_id,
        )

        _v12_aux_set(
            path,
            chat_id,
            subject="",
            special_intent="",
        )

        # ----------------------------------------------------
        # ONE saved subject:
        # Neeraj Rao -> History
        #
        # Establish the exact target immediately so following
        # Price/Year/Demo cannot fall back to an old batch.
        # ----------------------------------------------------
        if len(_v18_subjects) == 1:

            _v18_subject_name = str(
                _v18_subjects[0].get("name")
                or ""
            ).strip()

            _v18_exact = (
                _v13_target_for_teacher_subject(
                    path,
                    _v18_teacher_id,
                    _v18_subject_name,
                )
            )

            if _v18_exact:

                _save_state(
                    path,
                    chat_id,
                    targets=[_v18_exact],
                    pending_intent="",
                    pending_unknown="",
                )

                _v13_set(
                    path,
                    chat_id,
                    teacher_id=_v18_teacher_id,
                    teacher_name=_v15_teacher_name,
                    subject=_v18_subject_name,
                )

                _v12_aux_set(
                    path,
                    chat_id,
                    subject=_v18_subject_name,
                    special_intent="",
                )

                logger.info(
                    "V18 SINGLE SUBJECT EXACT TARGET "
                    "chat=%s teacher=%r subject=%r target=%r",
                    chat_id,
                    _v15_teacher_name,
                    _v18_subject_name,
                    _target_text([_v18_exact]),
                )

                return (
                    False,
                    _target_text(
                        [_v18_exact]
                    ),
                )

        # ----------------------------------------------------
        # MULTIPLE subjects:
        # Sudarshan -> Geography / Environment / DM
        #
        # No exact batch is active until customer chooses one.
        # ----------------------------------------------------

        _v13_set(
            path,
            chat_id,
            teacher_id=_v18_teacher_id,
            teacher_name=_v15_teacher_name,
            subject="",
        )

        logger.info(
            "V18 TEACHER CONSTRAINT "
            "chat=%s input=%r teacher=%r subjects=%s",
            chat_id,
            visible_text,
            _v15_teacher_name,
            len(_v18_subjects),
        )

        return (
            False,
            _v15_teacher_name,
        )



    # CONSOLIDATED_STATE_V23
    # V19 teacher-scoped interceptor removed. Its intended behavior is
    # enforced centrally by V15 + the strict subject gate below.

    # --------------------------------------------------------
    # A. LECTURE DOWNLOADABILITY
    # --------------------------------------------------------

    _v12_device_now = _v12_device(
        visible_text
    )

    if _v12_download_intent(
        visible_text
    ):

        await _v12_send_download_answer(
            update,
            context,
            (
                _v12_device_now
                or "android_phone"
            ),
        )

        _v12_aux_set(
            path,
            chat_id,
            special_intent=
                "lecture_download_device",
        )

        logger.info(
            "V12 DOWNLOAD INTENT "
            "chat=%s device=%s message=%r",
            chat_id,
            (
                _v12_device_now
                or "android_phone"
            ),
            visible_text,
        )

        return True, visible_text


    # Device-only follow-up:
    # "Laptop mein?"
    # "iOS?"
    # "Android mein?"
    if (
        _v12_aux.get("fresh")
        and _v12_aux.get(
            "special_intent"
        ) == "lecture_download_device"
        and _v12_device_now
    ):

        await _v12_send_download_answer(
            update,
            context,
            _v12_device_now,
        )

        _v12_aux_set(
            path,
            chat_id,
            special_intent=
                "lecture_download_device",
        )

        logger.info(
            "V12 DOWNLOAD DEVICE FOLLOWUP "
            "chat=%s device=%s",
            chat_id,
            _v12_device_now,
        )

        return True, visible_text



    # CONSOLIDATED_STATE_V23
    # V21 duplicate subject interceptor removed.

    # --------------------------------------------------------
    # B. BROAD SUBJECT STATE
    # --------------------------------------------------------

    _v12_current_subject = _v12_explicit_subject(
        visible_text
    )

    _v12_old_subject = (
        _v12_aux.get("subject")
        if _v12_aux.get("fresh")
        else ""
    )

    # A truly broad subject request:
    #
    # "History subject mein add karo"
    #
    # should NOT pick a random History teacher.
    # CONSOLIDATED_STATE_V23 HARD RULE:
    # A subject without a teacher/coaching identity is never a batch match.
    # V15 has already had the opportunity to combine this subject with a
    # fresh active teacher. If we reached here, request identity instead of
    # allowing any later global subject matcher to choose a faculty.
    if _v12_current_subject:

        # RESOLVER_REPAIR_V24
        #
        # Specific teacher/batch identity has priority over the broad-subject
        # clarification.  This fixes:
        #   PSIR Subhra Ranjan
        #   Subhra Ranjan mam PSIR
        #   PSIR Subhra
        # and, inside PSIR context, the unique surname "Ranjan".
        _v24_subject_targets = _v12_resolve_within_subject(
            path,
            visible_text,
            _v12_current_subject,
        )

        if _v24_subject_targets:
            _save_state(
                path,
                chat_id,
                targets=_v24_subject_targets,
                pending_intent="",
                pending_unknown="",
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=_v12_current_subject,
                special_intent="",
            )

            try:
                _v13_clear(
                    path,
                    chat_id,
                )
            except Exception:
                pass

            logger.info(
                "RESOLVER_REPAIR_V24 DIRECT SUBJECT TARGET "
                "chat=%s subject=%r target=%r",
                chat_id,
                _v12_current_subject,
                _target_text(_v24_subject_targets),
            )

            return (
                False,
                _target_text(_v24_subject_targets),
            )

        _v24_same_pending = bool(
            _v12_old_subject
            and _norm(_v12_old_subject)
            == _norm(_v12_current_subject)
            and _v12_aux.get("special_intent")
            == "subject_teacher_pending"
        )

        _v24_identity_tokens = _v24_subject_identity_tokens(
            visible_text,
            _v12_current_subject,
        )

        _v12_aux_set(
            path,
            chat_id,
            subject=_v12_current_subject,
            special_intent="subject_teacher_pending",
        )

        # A fresh broad subject starts a new partial constraint.
        _v13_clear(
            path,
            chat_id,
        )
        _v13_set(
            path,
            chat_id,
            teacher_id=None,
            teacher_name="",
            subject=_v12_current_subject,
        )

        # Ask the clarification ONLY ONCE for a truly broad subject.
        # If the customer already supplied a teacher-like identity but it
        # did not match the catalogue, repeating "teacher/coaching name?"
        # is redundant; preserve the subject constraint and stay silent.
        if _v24_same_pending or _v24_identity_tokens:
            logger.info(
                "RESOLVER_REPAIR_V24 SUBJECT PENDING SILENT "
                "chat=%s subject=%r identity=%r repeated=%s message=%r",
                chat_id,
                _v12_current_subject,
                _v24_identity_tokens,
                _v24_same_pending,
                visible_text,
            )

            return True, visible_text

        await context.bot.send_message(
            chat_id=chat_id,
            text="Teacher aur coaching name?",
        )

        logger.info(
            "RESOLVER_REPAIR_V24 SUBJECT ASK ONCE "
            "chat=%s subject=%r",
            chat_id,
            _v12_current_subject,
        )

        return True, visible_text


    # --------------------------------------------------------
    # V14 NORMAL TEACHER REQUEST
    #
    # When there is NO explicit "Teacher aur coaching name?"
    # pending question, normal teacher names must go directly
    # to the original working batch resolver.
    #
    # Examples:
    #   Sudarshan sir
    #   Prateek Nayak
    #   Neeraj Rao
    #   Nikhil Seth
    #   Nikhil Sait
    #   Aarti maam
    #   Himanshu Khatri
    # --------------------------------------------------------

    if (
        _v12_aux.get(
            "special_intent"
        ) != "subject_teacher_pending"
        and not _v12_current_subject
        and _v14_teacherish(
            visible_text
        )
    ):

        _v14_teacher = _v14_unique_teacher(
            path,
            visible_text,
        )

        if _v14_teacher:

            # Remove stale broad-subject state.
            _v12_aux_set(
                path,
                chat_id,
                subject="",
                special_intent="",
            )

            # Also remove stale partial V13 resolution state.
            try:
                _v13_clear(
                    path,
                    chat_id,
                )
            except Exception:
                pass

            _canonical_teacher = str(
                _v14_teacher.get(
                    "name"
                )
                or ""
            ).strip()

            logger.info(
                "V14 TEACHER CANONICAL "
                "chat=%s input=%r teacher=%r",
                chat_id,
                visible_text,
                _canonical_teacher,
            )

            # Let the existing structured-batch system process
            # the canonical saved teacher.
            return (
                False,
                _canonical_teacher,
            )


    # --------------------------------------------------------
    # C. SUBJECT-CONSTRAINED RESOLUTION
    #
    # Existing History state +:
    # "Nikhil Seth, Level Up IAS"
    #
    # Current message has NO new explicit subject,
    # therefore search ONLY within History.
    # --------------------------------------------------------

    if (
        _v12_old_subject
        and not _v12_current_subject
        and _v12_aux.get(
            "special_intent"
        ) == "subject_teacher_pending"
    ):

        _v12_subject_matches = (
            _v12_resolve_within_subject(
                path,
                visible_text,
                _v12_old_subject,
            )
        )

        # If this looks like teacher/coaching clarification,
        # do not allow global resolver to jump subjects.
        _v12_identity_clue = bool(
            re.search(
                r"\b(?:"
                r"sir|"
                r"mam|"
                r"maam|"
                r"ias|"
                r"academy|"
                r"unacademy|"
                r"vision|"
                r"vajiram|"
                r"sarthi|"
                r"saarthi|"
                r"level up|"
                r"lukman|"
                r"study iq|"
                r"next ias|"
                r"physics wallah|"
                r"only ias"
                r")\b",
                _norm(visible_text),
            )
        )

        if _v12_subject_matches:

            _save_state(
                path,
                chat_id,
                targets=
                    _v12_subject_matches,
                pending_intent="",
                pending_unknown="",
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=
                    _v12_old_subject,
                special_intent="",
            )

            logger.info(
                "V12 SUBJECT-CONSTRAINED TARGET "
                "chat=%s subject=%r target=%r",
                chat_id,
                _v12_old_subject,
                _target_text(
                    _v12_subject_matches
                ),
            )

            return (
                False,
                _target_text(
                    _v12_subject_matches
                ),
            )

        if _v12_identity_clue:

            logger.info(
                "V12 SUBJECT-CONSTRAINED UNKNOWN SILENT "
                "chat=%s subject=%r message=%r",
                chat_id,
                _v12_old_subject,
                visible_text,
            )

            return True, visible_text


    # New explicit subject replaces old subject-state.
    if (
        _v12_current_subject
        and _v12_current_subject
        != _v12_old_subject
    ):
        _v12_aux_set(
            path,
            chat_id,
            subject=
                _v12_current_subject,
            special_intent="",
        )


    # ========================================================
    # HELPERS
    # ========================================================

    def canonical(targets):
        return _target_text(
            targets
        )

    def demo_query(targets):
        """
        Folder optional batches use their FOLDER combined demo.

        Example:
          ACTIVE = Vajiram Sociology / Mohapatra Sir
          demo request -> "demo Sociology"

        This prevents /demoall from being used for Sociology.
        """

        if not targets:
            return "demo"

        first = targets[0]

        source = str(
            first.get("source")
            or ""
        )

        folder_name = str(
            first.get("folder_name")
            or ""
        ).strip()

        if (
            source in (
                "folder_batch",
                "folder_part",
            )
            and folder_name
        ):
            return (
                "demo "
                + folder_name
            )

        return (
            "demo "
            + canonical(targets)
        ).strip()


    # ========================================================
    # 1. REPLY-TO CONTEXT OVERRIDES CURRENT STATE
    #
    # Works whether user replies to:
    # - AI generated batch message
    # - their OWN earlier batch message
    # ========================================================

    reply_targets = []

    if reply_text:
        reply_targets = resolve_targets(
            path,
            reply_text,
        )

    if reply_targets:

        _save_state(
            path,
            chat_id,
            targets=reply_targets,
            pending_intent="",
            pending_unknown="",
        )

        old_targets = reply_targets
        state["targets"] = reply_targets
        state["fresh"] = True
        pending = ""

        # CONTEXT_BUSINESS_RULES_V12
        #
        # Replying to an older batch must restore its subject too.
        if len(reply_targets) == 1:

            _reply_subject = str(
                reply_targets[0].get(
                    "subject"
                )
                or reply_targets[0].get(
                    "folder_name"
                )
                or ""
            ).strip()

            _v12_aux_set(
                path,
                chat_id,
                subject=
                    _reply_subject,
                special_intent="",
            )

            # STATE_CONSOLIDATION_V13
            if str(
                reply_targets[0].get(
                    "source"
                )
                or ""
            ) == "structured":

                _v13_set(
                    path,
                    chat_id,
                    teacher_id=
                        reply_targets[0].get(
                            "teacher_id"
                        ),
                    teacher_name=str(
                        reply_targets[0].get(
                            "teacher"
                        )
                        or ""
                    ),
                    subject=
                        _reply_subject,
                )

            else:
                _v13_set(
                    path,
                    chat_id,
                    teacher_id=None,
                    teacher_name="",
                    subject=
                        _reply_subject,
                )

        else:
            _v12_aux_set(
                path,
                chat_id,
                subject="",
                special_intent="",
            )

        # DIRECT_REPLY_ANCHOR_V16
        #
        # If the customer explicitly replies to an older Telegram
        # batch card, that exact replied-to AI message becomes the
        # new anchor for all subsequent contextual answers.
        try:
            _v16_direct_ref = getattr(
                update,
                "_customer_reply_reference",
                None,
            )

            _v16_reply_message_id = 0

            if isinstance(_v16_direct_ref, dict):
                _v16_reply_message_id = int(
                    _v16_direct_ref.get("referenced_message_id")
                    or _v16_direct_ref.get("message_id")
                    or 0
                )

            if _v16_reply_message_id:
                _state_reply_set_anchor(
                    path,
                    chat_id,
                    _v16_reply_message_id,
                    source="direct_reply_ai_card",
                )

                logger.info(
                    "DIRECT REPLY ANCHOR V16 "
                    "chat=%s message_id=%s target=%r",
                    chat_id,
                    _v16_reply_message_id,
                    canonical(reply_targets),
                )

        except Exception:
            logger.exception(
                "DIRECT REPLY ANCHOR V16 save failed"
            )

        logger.info(
            "UNIFIED STATE REPLY OVERRIDE V3 "
            "chat=%s target=%r reference=%r",
            chat_id,
            canonical(reply_targets),
            reply_text,
        )

        # "." / OK / ? / Yes on replied batch means:
        # restore state only.
        # NEVER resend full batch card.
        if _neutral(
            visible_text
        ):
            return True, visible_text


    # ========================================================
    # 2. EXPLICIT KNOWN TARGET IN CURRENT MESSAGE
    # ========================================================

    # ========================================================
    # FINAL_KEYWORD_ROUTER_V1
    #
    # A subject/GS label alone is NEVER an exact batch.
    # It can resolve only inside an already selected teacher.
    # ========================================================

    _final_subject = _v12_explicit_subject(visible_text)

    if _final_subject and not _v15_teacher:

        if _final_unknown_active:
            logger.info(
                "UNKNOWN STATE SUBJECT-ONLY SILENT "
                "chat=%s subject=%r",
                chat_id,
                _final_subject,
            )
            return True, visible_text

        _final_partial = _v13_get(
            path,
            chat_id,
        )

        _final_teacher_id = (
            _final_partial.get("teacher_id")
            if _final_partial.get("fresh")
            else None
        )

        _final_teacher_name = str(
            _final_partial.get("teacher_name")
            or ""
        ).strip()

        if _final_teacher_id:

            _final_exact = _v13_target_for_teacher_subject(
                path,
                int(_final_teacher_id),
                _final_subject,
            )

            if _final_exact:

                _save_state(
                    path,
                    chat_id,
                    targets=[_final_exact],
                    pending_intent="",
                    pending_unknown="",
                )

                _v13_set(
                    path,
                    chat_id,
                    teacher_id=int(_final_teacher_id),
                    teacher_name=_final_teacher_name,
                    subject=str(
                        _final_exact.get("subject")
                        or _final_subject
                    ),
                )

                _v12_aux_set(
                    path,
                    chat_id,
                    subject=str(
                        _final_exact.get("subject")
                        or _final_subject
                    ),
                    special_intent="",
                )

                logger.info(
                    "FINAL TEACHER-SCOPED SUBJECT HIT chat=%s teacher=%r subject=%r target=%r",
                    chat_id,
                    _final_teacher_name,
                    _final_subject,
                    _target_text([_final_exact]),
                )

                return False, _target_text([_final_exact])

            # Wrong subject for active teacher: never search another teacher.
            _save_state(
                path,
                chat_id,
                targets=[],
                pending_intent="",
                pending_unknown="",
            )

            _v13_set(
                path,
                chat_id,
                teacher_id=None,
                teacher_name="",
                subject=_final_subject,
            )

            _v12_aux_set(
                path,
                chat_id,
                subject=_final_subject,
                special_intent="subject_teacher_pending",
            )

            logger.info(
                "FINAL TEACHER-SCOPED SUBJECT MISS SILENT chat=%s teacher=%r subject=%r",
                chat_id,
                _final_teacher_name,
                _final_subject,
            )

            return True, visible_text

        # No teacher/coaching identity: subject alone is only a constraint.
        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent="",
            pending_unknown="",
        )

        _v13_set(
            path,
            chat_id,
            teacher_id=None,
            teacher_name="",
            subject=_final_subject,
        )

        _v12_aux_set(
            path,
            chat_id,
            subject=_final_subject,
            special_intent="subject_teacher_pending",
        )

        await _send(
            update,
            context,
            "Teacher aur coaching name?",
        )

        logger.info(
            "FINAL SUBJECT-ONLY BLOCKED chat=%s subject=%r message=%r",
            chat_id,
            _final_subject,
            visible_text,
        )

        return True, visible_text

    # Bare GS paper is also not a unique batch.
    _final_gs = _norm(visible_text)
    _final_gs = re.sub(
        r"\b(?:bhai|bhaiya|bro|please|plz|class|classes|batch|course|chahiye|chahie|hai|h|kya|ka|ki|ke|do|dedo|de)\b",
        " ",
        _final_gs,
    )
    _final_gs = re.sub(r"\s+", " ", _final_gs).strip()

    if re.fullmatch(r"gs\s*[1-4]", _final_gs):
        await _send(
            update,
            context,
            "Teacher aur coaching name?",
        )
        return True, visible_text

    current_targets = resolve_targets(
        path,
        visible_text,
    )

    if current_targets:

        _final_unknown_clear(
            path,
            chat_id,
        )
        _final_unknown_active = False

        same = (
            bool(old_targets)
            and _same_targets(
                old_targets,
                current_targets,
            )
        )

        _save_state(
            path,
            chat_id,
            targets=current_targets,
            pending_unknown="",
        )

        _selection_message_id = getattr(
            update,
            "_main_account_event_id",
            None,
        )

        if _selection_message_id:
            _state_reply_set_anchor(
                path,
                chat_id,
                _selection_message_id,
                source="unified_exact_target",
            )

        state["targets"] = current_targets
        state["fresh"] = True

        # CONTEXT_BUSINESS_RULES_V12
        #
        # Keep subject state synchronized with an explicitly
        # selected database target.
        if len(current_targets) == 1:

            _selected_subject = str(
                current_targets[0].get(
                    "subject"
                )
                or current_targets[0].get(
                    "folder_name"
                )
                or ""
            ).strip()

            _v12_aux_set(
                path,
                chat_id,
                subject=
                    _selected_subject,
                special_intent="",
            )

            # STATE_CONSOLIDATION_V13
            if str(
                current_targets[0].get(
                    "source"
                )
                or ""
            ) == "structured":

                _v13_set(
                    path,
                    chat_id,
                    teacher_id=
                        current_targets[0].get(
                            "teacher_id"
                        ),
                    teacher_name=str(
                        current_targets[0].get(
                            "teacher"
                        )
                        or ""
                    ),
                    subject=
                        _selected_subject,
                )

            else:
                _v13_set(
                    path,
                    chat_id,
                    teacher_id=None,
                    teacher_name="",
                    subject=
                        _selected_subject,
                )

        else:
            # Multi-target selection should not constrain the
            # next message to a single subject.
            _v12_aux_set(
                path,
                chat_id,
                subject="",
                special_intent="",
            )

        logger.info(
            "UNIFIED STATE TARGET V3 "
            "chat=%s same=%s target=%r",
            chat_id,
            same,
            canonical(current_targets),
        )

        # -----------------------------------------------
        # Customer previously asked a contextual question
        # and we asked which batch.
        # Now attach that pending intent to the DB target.
        # -----------------------------------------------

        if pending:

            if pending == "link_choice":
                # Explicit target alone does not answer
                # demo-vs-payment choice.
                _save_state(
                    path,
                    chat_id,
                    targets=current_targets,
                    pending_intent="link_choice",
                )

            else:
                _save_state(
                    path,
                    chat_id,
                    targets=current_targets,
                    pending_intent="",
                    pending_unknown="",
                )

                if pending == "demo":

                    _mark_demo_sent(
                        path,
                        chat_id,
                        current_targets,
                    )

                    return (
                        False,
                        demo_query(
                            current_targets
                        ),
                    )

                return (
                    False,
                    (
                        pending
                        + " "
                        + canonical(
                            current_targets
                        )
                    ).strip(),
                )


        intent = _context_intent(
            visible_text
        )

        # Explicit batch + demo in same sentence.
        if intent == "demo":

            # Explicit demo request always sends demo again.
            #
            # Existing execute_new_batch_access already handles:
            # exact demo -> folder demo -> /demoall fallback.

            _mark_demo_sent(
                path,
                chat_id,
                current_targets,
            )

            if await _send_selected_demo(
                update,
                context,
                current_targets,
            ):
                return True, visible_text

            return (
                False,
                demo_query(
                    current_targets
                ),
            )

        # ----------------------------------------------------
        # Explicit target + fact that is NOT saved reliably.
        # ----------------------------------------------------

        if _must_use_demo_for_fact(
            current_targets,
            intent,
        ):

            await _send(
                update,
                context,
                "Bhai demo mein check kar lo.",
            )

            already_sent = _demo_already_sent(
                path,
                chat_id,
                current_targets,
            )

            if not already_sent:

                _mark_demo_sent(
                    path,
                    chat_id,
                    current_targets,
                )

                logger.info(
                    "UNIFIED DEMO FACT FALLBACK V8 "
                    "chat=%s target=%r "
                    "intent=%s demo_sent=1",
                    chat_id,
                    canonical(current_targets),
                    intent,
                )

                return (
                    False,
                    demo_query(
                        current_targets
                    ),
                )

            logger.info(
                "UNIFIED DEMO FACT FALLBACK V8 "
                "chat=%s target=%r "
                "intent=%s demo_sent=0 already_sent=1",
                chat_id,
                canonical(current_targets),
                intent,
            )

            return True, visible_text


        # Same batch repeated with no new question:
        # don't resend the card/list again.
        if same and not intent:
            return True, visible_text

        # Explicit known DB target becomes canonical before
        # old routers see it.
        #
        # Mahapatra Sociology
        # -> Vajiram Sociology
        #
        # Jatin Gupta Polity
        # -> Jatin Gupta — Polity
        if intent:
            return (
                False,
                (
                    visible_text
                    + " "
                    + canonical(
                        current_targets
                    )
                ).strip(),
            )

        return (
            False,
            canonical(
                current_targets
            ),
        )


    # ========================================================
    # 3. UNKNOWN BATCH / COURSE — ONE CLARIFICATION PER HOUR
    #
    # First unknown:
    #   "Bhai, batch name aur teacher name bata dijiye."
    #
    # Further unknown/generic follow-ups for 1 hour:
    #   SILENT.
    #
    # A future trusted DB teacher/batch can still exit this state.
    # ========================================================

    if (
        _specific_unknown_request(visible_text)
        or _final_untrusted_business_request(visible_text)
    ):

        # Explicit generic UPSC/GS sales requests belong to the existing
        # configured combo flow, not the unknown-course state.
        if not _final_allowed_sales_fallback(visible_text):

            _save_state(
                path,
                chat_id,
                targets=[],
                pending_intent="",
                pending_unknown=visible_text,
                refresh=False,
            )

            _v13_clear(
                path,
                chat_id,
            )

            _v12_aux_set(
                path,
                chat_id,
                subject="",
                special_intent="",
            )

            if _final_unknown_active:

                _final_unknown_mark(
                    path,
                    chat_id,
                )

                logger.info(
                    "UNKNOWN STATE REPEAT SILENT "
                    "chat=%s message=%r",
                    chat_id,
                    visible_text,
                )

                return True, visible_text

            _final_unknown_mark(
                path,
                chat_id,
            )

            await _send(
                update,
                context,
                "Bhai, batch name aur teacher name bata dijiye.",
            )

            logger.info(
                "UNKNOWN STATE ASK ONCE "
                "chat=%s message=%r",
                chat_id,
                visible_text,
            )

            return True, visible_text


    # ========================================================
    # 4. FRESH ACTIVE TARGET
    # ========================================================

    if (
        state.get("fresh")
        and old_targets
    ):

        target_text = canonical(
            old_targets
        )


        # ----------------------------------------------------
        # LINK REQUEST
        #
        # Do NOT ask:
        # "Which subject/course?"
        #
        # Batch already known.
        # ----------------------------------------------------

        if _is_link(
            visible_text
        ):

            await _send(
                update,
                context,
                "Demo link or payment link?",
            )

            _save_state(
                path,
                chat_id,
                targets=old_targets,
                pending_intent=
                    "link_choice",
                pending_unknown="",
            )

            logger.info(
                "UNIFIED STATE LINK CHOICE V3 "
                "chat=%s target=%r",
                chat_id,
                target_text,
            )

            return True, visible_text


        # ----------------------------------------------------
        # ANSWER TO LINK CHOICE
        # ----------------------------------------------------

        if pending == "link_choice":

            n = _norm(
                visible_text
            )

            if re.search(
                r"\bdemo\b",
                n,
            ):

                _save_state(
                    path,
                    chat_id,
                    targets=old_targets,
                    pending_intent="",
                )

                _mark_demo_sent(
                    path,
                    chat_id,
                    old_targets,
                )

                if await _send_selected_demo(
                    update,
                    context,
                    old_targets,
                ):
                    return True, visible_text

                return (
                    False,
                    demo_query(
                        old_targets
                    ),
                )

            if re.search(
                r"\b(?:"
                r"payment|"
                r"pay"
                r")\b",
                n,
            ):

                _save_state(
                    path,
                    chat_id,
                    targets=old_targets,
                    pending_intent="",
                )

                return (
                    False,
                    (
                        "payment "
                        + target_text
                    ).strip(),
                )


        # ----------------------------------------------------
        # DEMO REQUEST
        #
        # Folder target:
        #   specific demo
        #   -> folder combined demo
        #
        # For Sociology this deliberately sends:
        #   "demo Sociology"
        # not /demoall.
        # ----------------------------------------------------

        if _is_demo(
            visible_text
        ):

            _save_state(
                path,
                chat_id,
                targets=old_targets,
                pending_intent="",
            )

            # Explicit demo request ALWAYS sends demo.
            _mark_demo_sent(
                path,
                chat_id,
                old_targets,
            )

            query = demo_query(
                old_targets
            )

            logger.info(
                "UNIFIED STATE DEMO V15_1 "
                "chat=%s target=%r query=%r",
                chat_id,
                target_text,
                query,
            )

            if await _send_selected_demo(
                update,
                context,
                old_targets,
            ):
                return True, visible_text

            return False, query


        # ----------------------------------------------------
        # V12 ACTIVE-TARGET INCLUSION QUERY
        #
        # Example:
        # ACTIVE = Top Faculty
        # Customer = "CA lectures?"
        #
        # This is NOT a request for another batch.
        # ----------------------------------------------------

        if _v12_inclusion_query(
            visible_text
        ):

            await _v12_send_inclusion_guidance(
                update,
                context,
            )

            # Send relevant demo/list once if not previously sent.
            if not _demo_already_sent(
                path,
                chat_id,
                old_targets,
            ):

                _mark_demo_sent(
                    path,
                    chat_id,
                    old_targets,
                )

                logger.info(
                    "V12 INCLUSION DEMO "
                    "chat=%s target=%r",
                    chat_id,
                    target_text,
                )

                return (
                    False,
                    demo_query(
                        old_targets
                    ),
                )

            logger.info(
                "V12 INCLUSION GUIDANCE "
                "chat=%s target=%r",
                chat_id,
                target_text,
            )

            return True, visible_text


        # ----------------------------------------------------
        # CONTEXTUAL QUESTION
        # year / price / validity / recorded / latest /
        # notes / lectures etc.
        # ----------------------------------------------------

        intent = _context_intent(
            visible_text
        )

        if intent:

            _save_state(
                path,
                chat_id,
                targets=old_targets,
                pending_intent="",
            )

            # ------------------------------------------------
            # We do not know this fact from DB.
            # Never invent it.
            # ------------------------------------------------

            if _must_use_demo_for_fact(
                old_targets,
                intent,
            ):

                await _send(
                    update,
                    context,
                    "Bhai demo mein check kar lo.",
                )

                # STATE_CONSOLIDATION_V13
                # If customer says:
                # "nahi mil raha", "demo kaha hai", "can't find"
                # resend the current target demo.
                _v12_aux_set(
                    path,
                    chat_id,
                    special_intent=
                        "demo_reference",
                )

                already_sent = _demo_already_sent(
                    path,
                    chat_id,
                    old_targets,
                )

                if not already_sent:

                    _mark_demo_sent(
                        path,
                        chat_id,
                        old_targets,
                    )

                    logger.info(
                        "UNIFIED DEMO FACT FALLBACK V8 "
                        "chat=%s target=%r "
                        "intent=%s demo_sent=1",
                        chat_id,
                        target_text,
                        intent,
                    )

                    return (
                        False,
                        demo_query(
                            old_targets
                        ),
                    )

                logger.info(
                    "UNIFIED DEMO FACT FALLBACK V8 "
                    "chat=%s target=%r "
                    "intent=%s demo_sent=0 already_sent=1",
                    chat_id,
                    target_text,
                    intent,
                )

                return True, visible_text


            # ------------------------------------------------
            # Fact exists in DB (price/year).
            #
            # Send ONLY requested fact.
            # Never resend complete batch card.
            # Attach answer to active Telegram batch anchor.
            # ------------------------------------------------

            if intent in (
                "price",
                "year",
            ):

                values = []

                for _target in old_targets:

                    value = str(
                        _target.get(intent)
                        or ""
                    ).strip()

                    if not value:
                        continue

                    if (
                        intent == "price"
                        and not value.startswith("₹")
                    ):
                        value = "₹" + value

                    values.append(value)

                if values:

                    _price_reply = " / ".join(values)

                    if len(old_targets) > 1:
                        _combined_price_text = _final_combined_selection_text(
                            old_targets
                        )
                        if _combined_price_text:
                            _price_reply = _combined_price_text

                    await _send_fact_anchored(
                        update,
                        context,
                        path,
                        _price_reply,
                    )

                    logger.info(
                        "UNIFIED FACT ONLY V15_1 "
                        "chat=%s target=%r intent=%s value=%r",
                        chat_id,
                        target_text,
                        intent,
                        values,
                    )

                    return True, visible_text

            return (
                False,
                (
                    visible_text
                    + " "
                    + target_text
                ).strip(),
            )


        # ----------------------------------------------------
        # NEUTRAL FOLLOW-UP
        # Keep state, don't resend details.
        # ----------------------------------------------------

        if _neutral(
            visible_text
        ):

            _save_state(
                path,
                chat_id,
                targets=old_targets,
            )

            return True, visible_text


        # Random/untrusted text must never reach generic AI merely
        # because an old batch is active. Preserve only the owner's
        # configured generic UPSC/GS combo-sales handoff.
        _save_state(
            path,
            chat_id,
            targets=old_targets,
        )

        if _final_allowed_sales_fallback(
            visible_text
        ):
            return False, visible_text

        logger.info(
            "ACTIVE STATE RANDOM SILENT "
            "chat=%s message=%r target=%r",
            chat_id,
            visible_text,
            _target_text(old_targets),
        )

        return True, visible_text


    # ========================================================
    # UNKNOWN STATE HARD SILENCE
    #
    # We already tried trusted resolution above. If still unresolved
    # during the one-hour unknown window, do not ask again and do not
    # send the message to generic AI.
    # ========================================================

    if _final_unknown_active:

        logger.info(
            "UNKNOWN STATE GENERIC FOLLOWUP SILENT "
            "chat=%s message=%r",
            chat_id,
            visible_text,
        )

        return True, visible_text

    # ========================================================
    # 5. NO FRESH STATE / STATE EXPIRED
    #
    # Generic contextual message cannot assume the old batch.
    # ========================================================

    intent = _context_intent(
        visible_text
    )

    if intent:

        if intent == "demo":
            question = (
                "Bro, kis batch ka demo chahiye?"
            )

        elif intent == "payment":
            question = (
                "Bro, kis batch ke liye payment karna hai?"
            )

        else:
            question = (
                "Bro, kaunse batch ki baat kar rahe ho?"
            )

        await _send(
            update,
            context,
            question,
        )

        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent=intent,
            pending_unknown="",
        )

        logger.info(
            "UNIFIED STATE NEED TARGET V3 "
            "chat=%s intent=%s",
            chat_id,
            intent,
        )

        return True, visible_text


    # ========================================================
    # 6. WE ASKED FOR TARGET BUT CUSTOMER STILL PROVIDED
    # SOMETHING THAT IS NOT IN DATABASE.
    #
    # SILENT.
    # ========================================================

    if pending:

        _save_state(
            path,
            chat_id,
            targets=[],
            pending_intent="",
            pending_unknown=visible_text,
            refresh=False,
        )

        logger.info(
            "UNIFIED STATE PENDING UNKNOWN SILENT V3 "
            "chat=%s message=%r",
            chat_id,
            visible_text,
        )

        return True, visible_text


    # ========================================================
    # FINAL TRUST GATE
    #
    # No trusted DB target, deterministic workflow, or configured
    # UPSC/GS sales handoff matched. Stay silent. This prevents
    # invented SSC availability and random Top Faculty prompts.
    # ========================================================

    if _final_allowed_sales_fallback(
        visible_text
    ):
        _final_unknown_clear(
            path,
            chat_id,
        )
        return False, visible_text

    logger.info(
        "FINAL TRUST GATE SILENT "
        "chat=%s message=%r",
        chat_id,
        visible_text,
    )

    return True, visible_text

