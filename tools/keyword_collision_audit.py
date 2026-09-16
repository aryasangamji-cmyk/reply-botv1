#!/usr/bin/env python3

import re
import sqlite3
from collections import defaultdict

DB = "/opt/new-ai-bot/data/bot.sqlite3"

GENERIC_SUBJECT_WORDS = {
    "history",
    "economy",
    "economics",
    "geography",
    "environment",
    "polity",
    "ethics",
    "governance",
    "sociology",
    "psir",
    "essay",
    "security",
    "science",
    "technology",
    "ir",
    "society",
    "disaster",
    "management",
}

GENERIC_IDENTITY_WORDS = {
    "sir",
    "mam",
    "maam",
    "madam",
    "ias",
    "academy",
    "coaching",
    "batch",
    "course",
    "foundation",
    "optional",
    "gs",
    "gs1",
    "gs2",
    "gs3",
    "gs4",
}

def norm(x):
    x = str(x or "").lower()
    x = re.sub(r"[^a-z0-9]+", " ", x)
    return re.sub(r"\s+", " ", x).strip()

def split_aliases(x):
    return [
        y.strip()
        for y in re.split(
            r"[,;\n|]+",
            str(x or ""),
        )
        if y.strip()
    ]

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

teachers = db.execute("""
    SELECT *
    FROM structured_batch_teachers
    WHERE enabled=1
    ORDER BY id
""").fetchall()

subjects = db.execute("""
    SELECT
        s.*,
        t.name AS teacher_name,
        t.institute AS institute
    FROM structured_batch_subjects s
    JOIN structured_batch_teachers t
      ON t.id=s.teacher_id
    WHERE s.enabled=1
      AND t.enabled=1
    ORDER BY t.id,s.id
""").fetchall()

teacher_alias_map = defaultdict(list)
subject_alias_map = defaultdict(list)

dangerous_teacher_terms = []
dangerous_subject_aliases = []
short_aliases = []

for t in teachers:

    phrases = [
        t["name"],
        *split_aliases(
            t["aliases"]
        ),
    ]

    for phrase in phrases:

        n = norm(phrase)

        if not n:
            continue

        teacher_alias_map[n].append(
            (
                int(t["id"]),
                t["name"],
            )
        )

        words = n.split()

        if (
            len(n) <= 2
            or
            n in GENERIC_IDENTITY_WORDS
        ):
            short_aliases.append(
                (
                    "TEACHER",
                    t["name"],
                    phrase,
                )
            )

    keyword_tokens = set(
        norm(
            t["keywords"]
        ).split()
    )

    overlap = sorted(
        keyword_tokens
        & GENERIC_SUBJECT_WORDS
    )

    if overlap:

        dangerous_teacher_terms.append(
            (
                t["name"],
                ", ".join(overlap),
                t["keywords"],
            )
        )


for s in subjects:

    phrases = [
        s["name"],
        *split_aliases(
            s["aliases"]
        ),
    ]

    for phrase in phrases:

        n = norm(phrase)

        if not n:
            continue

        subject_alias_map[n].append(
            (
                int(s["teacher_id"]),
                s["teacher_name"],
                s["name"],
            )
        )

        tokens = set(
            n.split()
        )

        if (
            n in GENERIC_SUBJECT_WORDS
            or (
                len(tokens) <= 2
                and tokens
                and tokens.issubset(
                    GENERIC_SUBJECT_WORDS
                )
            )
        ):

            dangerous_subject_aliases.append(
                (
                    s["teacher_name"],
                    s["name"],
                    phrase,
                )
            )


teacher_collisions = {
    alias: rows
    for alias, rows
    in teacher_alias_map.items()
    if len(
        {
            x[0]
            for x in rows
        }
    ) > 1
}

subject_collisions = {
    alias: rows
    for alias, rows
    in subject_alias_map.items()
    if len(
        {
            x[0]
            for x in rows
        }
    ) > 1
}


print("=" * 90)
print("FULL STRUCTURED-BATCH KEYWORD COLLISION AUDIT")
print("=" * 90)

print()
print("Enabled teachers:", len(teachers))
print("Enabled subjects:", len(subjects))


print()
print("=" * 90)
print("TEACHER ALIAS COLLISIONS")
print("=" * 90)

if not teacher_collisions:
    print("NONE")
else:
    for alias, rows in sorted(
        teacher_collisions.items()
    ):
        print()
        print("ALIAS:", repr(alias))
        for row in rows:
            print(
                "  ",
                row[0],
                row[1],
            )


print()
print("=" * 90)
print("GENERIC SUBJECT WORDS INSIDE TEACHER KEYWORDS")
print("=" * 90)

if not dangerous_teacher_terms:
    print("NONE")
else:
    for teacher, overlap, keywords in (
        dangerous_teacher_terms
    ):
        print()
        print("TEACHER:", teacher)
        print("GENERIC:", overlap)
        print("KEYWORDS:", keywords)


print()
print("=" * 90)
print("SUBJECT ALIASES SHARED ACROSS DIFFERENT TEACHERS")
print("=" * 90)

if not subject_collisions:
    print("NONE")
else:
    for alias, rows in sorted(
        subject_collisions.items()
    ):
        print()
        print("ALIAS:", repr(alias))

        for row in rows:
            print(
                "  teacher_id=",
                row[0],
                "|",
                row[1],
                "—",
                row[2],
            )


print()
print("=" * 90)
print("GENERIC / DANGEROUS SUBJECT ALIASES")
print("=" * 90)

for teacher, subject, alias in (
    dangerous_subject_aliases
):

    print(
        f"{teacher} — {subject} "
        f"| alias={alias!r}"
    )


print()
print("=" * 90)
print("TOO-SHORT / GENERIC TEACHER ALIASES")
print("=" * 90)

if not short_aliases:
    print("NONE")
else:
    for row in short_aliases:
        print(
            f"{row[0]} | {row[1]} "
            f"| alias={row[2]!r}"
        )


print()
print("=" * 90)
print("AUDIT COMPLETE")
print("=" * 90)
