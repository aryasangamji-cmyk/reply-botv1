#!/usr/bin/env python3

import re
import sqlite3
from pathlib import Path

from app import unified_customer_context as u

DB = "/opt/new-ai-bot/data/bot.sqlite3"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

teachers = con.execute(
    """
    SELECT *
    FROM structured_batch_teachers
    WHERE enabled=1
    ORDER BY id
    """
).fetchall()

subjects = con.execute(
    """
    SELECT
        s.*,
        t.name AS teacher_name
    FROM structured_batch_subjects s
    JOIN structured_batch_teachers t
      ON t.id=s.teacher_id
    WHERE s.enabled=1
      AND t.enabled=1
    ORDER BY t.id, s.id
    """
).fetchall()

def split_aliases(value):
    return [
        x.strip()
        for x in re.split(
            r"[,;|\n]+",
            str(value or ""),
        )
        if x.strip()
    ]

tests = 0
fails = []

print("=" * 72)
print("V18 SAVED BATCH RESOLUTION AUDIT")
print("=" * 72)
print("Teachers:", len(teachers))
print("Subjects:", len(subjects))
print()

# ------------------------------------------------------------
# 1. Every saved teacher and alias
# ------------------------------------------------------------

for t in teachers:

    candidates = [
        t["name"],
        *split_aliases(t["aliases"]),
    ]

    # Natural customer wording tests.
    candidates += [
        f'{t["name"]} ka hai kya',
        f'{t["name"]} ka koi batch hai kya',
        f'{t["name"]} ka course hai kya',
        f'{t["name"]} available hai kya',
    ]

    seen = set()

    for phrase in candidates:

        key = phrase.lower().strip()

        if not key or key in seen:
            continue

        seen.add(key)
        tests += 1

        got = u._v14_unique_teacher(
            DB,
            phrase,
        )

        got_id = (
            int(got["id"])
            if got
            else None
        )

        expected = int(t["id"])

        if got_id != expected:

            fails.append(
                (
                    "TEACHER",
                    t["name"],
                    phrase,
                    expected,
                    (
                        got.get("name")
                        if got
                        else "NO MATCH"
                    ),
                )
            )

# ------------------------------------------------------------
# 2. Every teacher + saved subject combination
# ------------------------------------------------------------

for s in subjects:

    tests += 1

    got = u._v13_target_for_teacher_subject(
        DB,
        int(s["teacher_id"]),
        str(s["name"]),
    )

    if not got:

        fails.append(
            (
                "SUBJECT",
                s["teacher_name"],
                s["name"],
                s["id"],
                "NO EXACT TARGET",
            )
        )

# ------------------------------------------------------------
# 3. Data-integrity report
# ------------------------------------------------------------

missing_year = [
    s
    for s in subjects
    if not str(s["year"] or "").strip()
]

missing_price = [
    s
    for s in subjects
    if not str(s["price"] or "").strip()
]

unavailable = [
    s
    for s in subjects
    if not int(s["availability"] or 0)
]

print("Recognition tests:", tests)
print("Recognition failures:", len(fails))
print()

if fails:

    print("FAILED RECOGNITION TESTS")
    print("-" * 72)

    for x in fails:
        print(
            f"{x[0]} | {x[1]} | input={x[2]!r} "
            f"| expected={x[3]} | got={x[4]}"
        )

else:
    print("ALL SAVED TEACHER/SUBJECT RECOGNITION TESTS PASSED.")

print()
print("=" * 72)
print("DATABASE DATA GAPS")
print("=" * 72)

print()
print("MISSING YEAR:", len(missing_year))

for s in missing_year:
    print(
        f'- {s["teacher_name"]} — {s["name"]}'
    )

print()
print("MISSING PRICE:", len(missing_price))

for s in missing_price:
    print(
        f'- {s["teacher_name"]} — {s["name"]}'
    )

print()
print("MARKED NOT AVAILABLE:", len(unavailable))

for s in unavailable:
    print(
        f'- {s["teacher_name"]} — {s["name"]}'
    )

print()
print("=" * 72)

if fails:
    raise SystemExit(2)

print("AUDIT RESULT: PASS")
