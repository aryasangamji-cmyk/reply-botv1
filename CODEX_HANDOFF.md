# CODEX HANDOFF — Telegram AI Sales Bot

Last updated: 2026-09-16

## 0. Purpose

This file is the continuity handoff from a long ChatGPT coding/debugging session into Codex.

**Important:** Do not broadly rewrite the project. The user wants minimal/surgical patches, exact backups, compile checks, regression tests, hash guards, and preservation of working subsystems.

The immediate project is a Telegram AI sales bot with:
- batch/course recognition
- conversation state
- negotiation
- multi-course cart/list handling
- reply-context handling
- payment flow
- combo products
- demo/access flows
- real Telegram E2E regression testing

The user wants future coding to continue from the current production state described below.

---

# 1. Production/server facts

- Server IP: `64.227.177.137`
- Project: `/opt/new-ai-bot`
- Service: `new-ai-bot.service`
- Main DB: `/opt/new-ai-bot/data/bot.sqlite3`
- Main Telegram Account ID: `7916022713`
- Dedicated real-test worker ID: `7285849417`
- Telethon test session: `/opt/new-ai-bot/data/test_runner/test_customer.session`
- Matching resolver: `/opt/new-ai-bot/app/unified_customer_context.py`

Other runtime DBs used:
- `/opt/new-ai-bot/data/negotiation_runtime.sqlite3`
- `/opt/new-ai-bot/data/combo_continuity_runtime.sqlite3`

Do not expose or commit secrets, `.env`, bot tokens, session files, gift-card data, or credentials.

---

# 2. Current production version — VERY IMPORTANT

The last successful installed version is **V34**, not V35.

## V34 successful install

Only changed:
- `app/negotiation_layer.py`

Current known V34 negotiation hash:
`95acdc99d70be15d4b8f13fe521c59fb6e3f89e7b5364c306aefac29065ac493`

Protected hashes after V34:
- Matching resolver: `1f7bcb14880846d0073c3176e7cf3c0662bfe9e1698edc9792eeffe5220b5082`
- Payment architecture: `1a2088814505a0faa858ab925fc601b2b5bb978d149250c026f3e75d2cb3cfa5`
- Combo bridge: `3041525478826898c24b13ae9cc6b73a85077248277e6aaa7fca248621fe5132`
- Reply context: `e671e9f79da3b900ee734851889c86fc4b3120f9294c57fffe0eedad0c0b5d45`
- Main bridge: `eb22d4b941ec269d591d98e219e717a014b98a247e3be35042c9da672b2035d3`

## V35 DID NOT INSTALL

Attempted package:
`NEGOTIATION_INTENT_GATE_AI_V35_SAFE_PACKAGE.zip`

Installer output:
- Exact V34/hash pre-scan passed
- DB backups succeeded
- isolated compile passed
- **V35 self-test FAILED**
- therefore production should still be unchanged V34

Failure:
```text
AssertionError
expected cart keys = {Jayant key, Ayushi key}
actual cart =
- Jayant Parikshit — Economy Module, price 350
- Vision IAS PSIR, price 500
```

Actual failing state:
```python
{
  'cart': [
    {
      'key': 'subject:62',
      'name': 'Jayant Parikshit — Economy Module',
      'price': 350,
      'catalog_price': 400
    },
    {
      'key': 'folder_batch:12',
      'name': 'Vision IAS PSIR',
      'price': 500,
      ...
      'phrases': [..., 'ayushi']
    }
  ],
  'original_total': 850,
  'current_offer': 850,
  'floor_price': 750,
  'negotiation_round': 0,
  'status': 'SELECTED'
}
```

This means the proposed V35 replacement/new-target path still had an alias/candidate problem: `"Ayushi"` resolved to `Vision IAS PSIR` instead of the intended Ayushi IR target in the self-test.

**Do not claim V35 is installed. Fix from V34.**

Backup directory created during failed V35 attempt:
`/root/NEGOTIATION_INTENT_GATE_AI_V35_BACKUP_20260916_123527`

---

# 3. User's core engineering preference

1. Preserve working systems.
2. Minimal/surgical changes.
3. Never broadly rewrite matching/payment/combo unless explicitly necessary.
4. Always:
   - pre-scan hashes
   - create backups
   - test isolated copy first
   - compile
   - run regression/self-tests
   - only then touch production
   - provide rollback
5. Never say “fixed/passed” without real evidence.
6. Button-driven admin is preferred elsewhere in the ecosystem.
7. Exact copy-paste commands are preferred.
8. Server-side tests should continue if MacBook is off.
9. Avoid rebooting during long tests unless necessary.

---

# 4. Matching rules — preserve

The matching system was heavily worked on earlier and is considered stable enough that later patches intentionally avoided changing it.

Authoritative rules:
- Explicit subject is a hard boundary.
- Search only within the explicit subject.
- No cross-subject fallback.
- GS vs Optional are separate, especially Geography/History/Economy.
- Optional must be explicit when needed.
- Explicit current subject overrides stale old subject while retaining compatible faculty.
- PSIR and IR are distinct.
- Subject filter before faculty.
- Bare ambiguous names like `Ranjan` / `Piyush` must not auto-select.
- Deterministic filtering before AI conflict resolution.
- If genuine multiple candidates remain:
  1. AI gets remaining candidate Name + Description.
  2. If unresolved, AI gets Name + Keywords/Aliases.
  3. If still unresolved, show sorted shortlist.
- AI must not search entire catalogue or invent candidates.
- Parent mappings collapse only real parent/components.
- Use exact saved canonical names.
- Send full saved batch details after match.
- Subject-only queries such as `CSAT`, `IR`, `History` should ask for teacher/coaching, not auto-pick.
- Teacher→subject and subject→teacher both supported.
- Unknown explicit teacher/batch should not hallucinate/substitute.
- Reply reference has highest priority.
- Explicit new entities beat incompatible stale context.
- Inherit only missing information.
- Resolve exact DB target before intent where appropriate.

Corrected names:
- `Shruti Joshi` = International Relations
- `Smriti` is Sociology context
- avoid incorrect “Smruti Joshi”

Known catalogue cleanup exceptions:
- Mrunal Patel and Vision IAS Sociology had some data/validation caveats.
- PCB/QEP are components/descriptions, not subjects.

---

# 5. Conversation-state principles

Separate:
- course target
- workflow
- temporary context
- negotiation state
- payment state
- reply context

Rules:
- Reply reference has highest contextual priority.
- Generic follow-ups use active target.
- Validity fixed answer: Lifetime.
- Download:
  - Android supported
  - short iOS/laptop/tablet follow-up should continue the device conversation.
- Generic `link` asks demo vs payment when exact batch is known.
- Unknown factual batch detail can say `Bhai demo mein check kar lo.` and send relevant demo.
- Demo can be resent if not found.
- Stale state should expire.
- Conversation course state historically around ~30 minutes.
- Negotiation state TTL around 45 minutes.

---

# 6. Reply Context Layer (V32 era) — preserve

Goal: Telegram replies work both directions.

## Customer → bot reply
If customer replies to an earlier Telegram message:
- recover which course/message/thread they refer to
- do not force them to repeat batch/teacher
- support old-message references even after cart changes

## Bot → customer reply
If bot answers about a specific batch:
- reply natively to the relevant batch/card/negotiation message
- do not unnecessarily repeat the batch name in every sentence
- this makes the UI clear to the customer
- map outgoing message IDs back to business context

## Message context mapping
Conceptually:
`(chat_id, telegram_message_id) -> target/business context`

Remember context for both:
- incoming customer messages that resolved to a target
- outgoing batch cards
- outgoing negotiation messages
- relevant cart/payment messages

Reply identifies the OBJECT, not the ACTION.

Examples:
- reply to Jayant + `iska price?` => QUERY_PRICE(Jayant)
- reply to Jayant + `isko bhi add kar do` => ADD(Jayant)
- reply to Jayant + `ye nahi chahiye` => REMOVE(Jayant)
- reply to Jayant + `baad me lunga` => DEFER(Jayant)
- reply to Jayant + `aur kam karo` => NEGOTIATE(Jayant)

Never equate `reply_to Jayant` with automatically selecting Jayant.

---

# 7. Negotiation rules — current desired behavior

There are three product classes.

## A. Top Faculty / Pro Pack
Fixed-price combo negotiation. No normal-batch discount ladder.
- Top Faculty: ₹999
- Pro Pack: ₹1499

Combo behavior:
- Round 1: explain already low/latest/genuine, no discount
- Round 2: profitability explanation
- Round 3+: fixed/non-negotiable
- no discount

## B. Normal course priced ₹200 or below
- ZERO discount
- first negotiation attempt: deny reduction, explain already lowest/fixed
- every later attempt: same fixed price, no discount

## C. Normal course above ₹200
New ladder:
- Turn 1: no discount, justify original price
- Turn 2: original - ₹50
- Turn 3: original - ₹100, **do NOT say “last price”**
- Turn 4: remain original - ₹100 and now say final/lowest
- Turn 5+: remain same; no further discount

Discount is always from ORIGINAL, not recursively from prior price.

Example original ₹500:
- T1 ₹500
- T2 ₹450
- T3 ₹400
- T4+ ₹400 final

Generic FAQ during negotiation:
- does NOT consume a negotiation turn
- does NOT change current offer
- does NOT reset batch
- does NOT reset cart

---

# 8. Generic FAQ — this is a critical bug area

A successful V33 fix already improved some FAQ handling.

Examples that must be treated as FAQ/question, not selection/cart operations:
- `Lecture notes milenge?`
- `Lecture notes sab rahenge na?`
- `Android me download ho jayega na?`
- `Download ho jayega?`
- `Validity kitni hai?`
- `Recorded lectures hain?`
- `Demo milega?`
- `Access kab tak hai?`
- `Dono me notes milenge?`
- `Sab batches lifetime rahenge?`
- `Ayushi maam ke lectures complete aur notes bhi milenge na?`

Key principle:
**Words like `sab`, `dono`, `all`, `bhi`, teacher names, or subject names are evidence only. They must never independently trigger a cart mutation.**

The previous V34 bug was overly keyword-driven:
- `sab` sometimes caused list rebuild
- `dono` could trigger multi-select
- teacher name inside a FAQ could route to matching
- FAQ could reset negotiation state

Desired routing:
1. recover reply/current/recent context
2. understand the full intent
3. if FAQ:
   - answer with contextual AI
   - no matching unless genuinely required
   - no cart mutation
   - no list resend
   - no negotiation reset
4. only real ADD/REMOVE/REPLACE intent can mutate cart

If uncertain:
- NO cart mutation
- NO list resend
- NO negotiation reset
- ask concise clarification only if truly necessary

The user explicitly wants AI used here when deterministic understanding is not confident.

---

# 9. AI intent gate — desired design

For cart/list area, use AI for ambiguous/natural-language intent rather than bare keywords.

Compact context sent to AI:
- current customer query
- reply target, if any
- current selected list
- recent negotiated courses
- current negotiated price of each
- negotiation round/status of each
- current whole-list negotiation state, if any

Strict possible outputs:
- FAQ
- ADD
- ADD_MULTIPLE
- REMOVE
- REMOVE_MULTIPLE
- REPLACE
- KEEP_ONLY
- NEGOTIATE_CURRENT_ITEM
- NEGOTIATE_WHOLE_LIST
- PAYMENT
- NEW_BATCH_REQUEST
- GENERAL/UNKNOWN
- NEEDS_CLARIFICATION
- NO_CART_ACTION

AI responsibilities:
- understand conversational meaning
- resolve pronouns/references using supplied context
- classify intent
- identify which supplied targets are affected

AI must NOT:
- invent catalogue items
- invent price
- decide discount amount
- alter payment amounts directly
- bypass deterministic pricing

If a new target must be resolved, use existing matcher after AI identifies that a new batch request/replacement is actually intended.

---

# 10. Recent Negotiation Ledger — desired feature

Need a short per-chat record of recently negotiated targets.

Conceptual data per target:
- key
- canonical name
- teacher
- subject
- catalogue/original price
- current negotiated price
- negotiation round
- status
- updated_at

This allows:
- negotiate Course A
- switch to Course B
- say `dono chahiye`
- combine A + B using their current negotiated prices

A recently negotiated batch remains referable even after customer temporarily switches to another batch.

Recent negotiated price has priority over old card/catalogue price when re-adding an old batch.

Example:
- Jayant original ₹400
- negotiated ₹350
- removed
- customer replies to old Jayant card: `Ye wala bhi wapas add kar do`
- re-add Jayant at ₹350, not ₹400

---

# 11. Multi-course cart/list semantics

User wants a live editable course list.

## ADD
Examples:
- `ye bhi chahiye`
- `isko bhi add kar do`
- `Basava aur optional dono lunga`
- `X bhi aur Y bhi`
- `saath me ye bhi`

Must add without removing existing items.

## REMOVE
Examples:
- `Basava wala nahi chahiye`
- `X hata do`
- `XYZ course nahi chahiye`

Remove item and its current amount, then send fresh list.

## REPLACE
Example:
`Rahul nahi, Ayushi wala chahiye`

Expected:
- remove Rahul
- preserve unrelated existing items
- resolve/add Ayushi
- send fresh list

Need a pending cart-edit bridge if the new target requires existing matcher:
```text
action = REPLACE
remove = Rahul
preserve = unrelated cart items
awaiting_new_target = Ayushi
```
After matcher resolves Ayushi, complete the pending cart edit rather than allowing normal single selection to replace whole cart.

## KEEP_ONLY
Example:
`Basava nahi, optional hi rakh do`
Should remove Basava and keep the referenced Optional.

## Duplicate safety
Repeated ADD must be idempotent.
No duplicate course rows.

## Fresh list
After every successful ADD/REMOVE/REPLACE:
- send complete fresh list
- show current per-item prices
- show total
- ask for confirmation if appropriate

---

# 12. Whole-list/bundle negotiation

Once current list is formed, customer may negotiate the whole list:
- `itna sara le raha hu thoda kam karo`
- `whole list pe discount?`
- `aur discount milega?`

Bundle negotiation is separate from individual negotiations.

Bundle base total = sum of **current negotiated item prices**, not catalogue prices.

Bundle ladder:
- Turn 1: base total, no discount
- Turn 2: base - ₹50
- Turn 3: base - ₹100, do not say last price
- Turn 4+: base - ₹100 final

Example:
- Geography current ₹450
- Basava current ₹300
- bundle base ₹750
- bundle T1 ₹750
- T2 ₹700
- T3 ₹650
- T4+ ₹650 final

If cart composition changes after bundle negotiation started:
- invalidate/reset only bundle negotiation
- recalc base total
- preserve per-item negotiated prices
- send fresh list
- new bundle negotiation may start

---

# 13. Payment architecture — preserve

Payment boundary:
- final amount > ₹999 => HIGH_PHONEPE flow
- final amount ≤ ₹999 => normal payment method flow

Payment state:
- selected_json
- final_amount
- amount_confirmed
- payment_method
- stage

Stages:
- CONFIRM_ORDER
- HIGH_PHONEPE (for >999)
- HIGH_PHONEPE_ALTERNATIVE
- PAYMENT_METHOD

PhonePe routing:
- >999 => `/phonepe`
- ₹500–₹999 => `/phonepeFK`
- <₹500 => `/phonepeAP`

Paytm => `/paytm`
Amazon => `/amazonpay`
GPay:
- ₹300 => `/gpay300`
- ₹800 => `/gpay300` then `/gpay500`
- otherwise `/gpay500`

Natural payment-method switching should be understood:
- `PhonePe se nahi ho, Paytm se karunga`
- `Friend ka PhonePe bhi nahi, Amazon Pay hi use karunga`

Only Yes/No belongs to confirmation/high-phonepe stages.
Unrelated queries should fall through to normal routing while preserving payment state.

Successful payment route clears payment state.

Important: Payment must receive the exact final cart and the exact final negotiated bundle amount.

Do not touch payment architecture while fixing list/intent unless evidence requires it.

---

# 14. Combo products — preserve

Top Faculty:
- ₹999 fixed
- profit explanation around ₹250
- Optional not included

Pro Pack:
- ₹1499 fixed
- includes Top Faculty + All Coaching GS + one Optional
- one Optional included
- extra Optional ₹500
- profit explanation around ₹500

Pre-access:
- 5 minutes
- Top Faculty directly
- Pro Pack may offer Top Faculty pre-access because many groups

Updates till Mains 2027.

All Combo contains Top Faculty + Pro Pack route.

---

# 15. V33 generic FAQ fix — what worked

V33 changed only `app/negotiation_layer.py`.

V33 negotiation hash:
`a528c34e27c411307a36fe5ff10760663e5ca1ab2e71965305a15cfd8d859b9e`

Behavior:
known lecture/notes/material/access FAQ during active negotiation:
- use contextual AI
- cannot fall through to batch/teacher clarification
- preserve negotiation round/offer

Test evidence showed:
`Lecture notes sab rahenge na?`
could respond:
`Bro, demo mein check kar lo.`
and preserve negotiation state.

V34 later introduced new cart/list routing regressions around generic FAQs; keep V33 behavior as a protected regression requirement.

---

# 16. V34 list/cart integration — what worked and what broke

V34 attempted AI-assisted:
- ADD
- REMOVE
- REPLACE
- KEEP_ONLY
- recent negotiated courses/prices/rounds
- bundle list edits

## Confirmed working portions
- exact batch recognition
- Telegram reply target recognition
- simple reply-based `Ye bhi chahiye` ADD in some flows
- `Basava + optional dono` could build two-item list in some flows
- recent negotiated price could carry into ADD in some normal cases
- individual ₹0/₹50/₹100 negotiation works if not reset
- payment flow works if handed a correct cart

## Confirmed failures from hard regression
1. Generic FAQ could trigger cart/list handling.
2. `sab`, `dono`, `all`, `bhi` were treated too keyword-first.
3. Generic FAQ could reset negotiation round.
4. Teacher name in FAQ could route to matcher instead of answering FAQ.
5. REMOVE could fail or behave like selection.
6. REPLACE could lose unrelated existing cart items.
7. Old-card re-add could restore catalogue price instead of recent negotiated price.
8. Bundle negotiation would then act on damaged/single-item cart.
9. A hard-test fixed timeout could race the 12-second negotiation burst + AI latency.

---

# 17. Hard regression test evidence

A 3-test real Telegram hard regression was run:
Service:
`new-ai-bot-e2e3-hard.service`

Run:
`RUN3_HARD_20260916_103411`

Two tests completed and failed; test 3 was stopped.

Test 1:
`ADD → REMOVE → REPLY-ADD → BUNDLE NEGOTIATION → PAYTM`
FAIL

Test 2:
`REPLY-ADD → REPLACE → REMOVE → REPLY-RESTORE → PAYMENT`
FAIL

Observed examples:

## Generic FAQ incorrectly rebuilt list
Customer:
`Lecture notes aur recordings sab rahenge na?`

Wrong bot behavior:
re-sent `You selected...` list

Expected:
contextual FAQ answer, no state mutation

## Generic FAQ reset negotiation
Geography:
- T1 ₹500
- FAQ happened
- negotiation reset
- next bargain was treated as T1 again instead of T2 ₹450

## `Basava sir and optional dono lunga`
V34 eventually could build:
- Basava ₹300
- Geography ₹500
but Geography should have been ₹450; earlier FAQ reset destroyed its negotiated price progression.

## REMOVE broken
Customer:
`Basava sir wala nahi chahiye, optional hi rakh do`

Wrong:
bot sent Basava card

Expected:
remove Basava, keep Optional, show fresh list

## Teacher-name FAQ broken
Customer:
`Ayushi maam ke lectures complete aur notes bhi milenge na?`

Wrong:
sent Ayushi/related batch card

Expected:
FAQ answer about current/referenced Ayushi course

## REPLACE broken
Current:
- Rahul Puri PSIR
- Jayant Economy ₹350

Customer:
`Rahul Puri wala nahi, Ayushi maam IR wala chahiye`

Wrong:
whole cart collapsed to new target

Expected:
remove Rahul, keep Jayant ₹350, add Ayushi IR

## Reply-add good case
Reply to old Jayant card:
`Ye bhi chahiye`
could correctly build:
- Rahul ₹500
- Jayant ₹350

## Reply-restore price bug
After removing Jayant and later replying:
`Ye wala bhi wapas add kar do`

Wrong:
Jayant restored at catalogue ₹400

Expected:
recent negotiated ₹350

---

# 18. Hard regression runner note

The test harness itself had one issue:
- negotiation system uses deliberate 12-second burst grouping
- AI/network latency can push actual reply >20s
- fixed test timeout may proceed too early
- next test message can interfere with still-pending negotiation burst

Future E2E runner should wait for **state transition / negotiation round advancement**, not only fixed message timeout.

---

# 19. 10,000-test runner history

A separate large runner was built:
`new-ai-bot-e2e10000.service`

It was run and later manually stopped.

One observed checkpoint:
- Recognition completed: 2359
- PASS: 2212
- FAIL: 147
- ERROR: 0
- pass rate ~93.77%

This large runner is not the current immediate task. Do not accidentally restart old E2E services while coding.

Other prior test services:
- `new-ai-bot-e2e50.service`
- `new-ai-bot-e2e20-list.service`
- `new-ai-bot-e2e3-hard.service`

Before any install, confirm they are inactive.

---

# 20. V35 failure root cause to investigate first

The proposed V35 intent-gate self-test failed because replacing a target with “Ayushi” resolved to:
`Vision IAS PSIR` (`folder_batch:12`)
instead of intended Ayushi IR target.

This is important because matching itself is supposed to remain protected.

Likely issue to inspect:
- V35 cart-intent layer may be passing a broad `Ayushi` hint or using phrase aliases from recent candidate objects without enough subject constraint.
- It may be selecting a candidate object from cart/recent candidates before the existing deterministic resolver can apply IR-vs-PSIR constraints.
- Do NOT “fix” this by altering `unified_customer_context.py` unless absolutely necessary.
- Prefer:
  1. AI intent determines `REPLACE` and semantic request `Ayushi + IR`.
  2. Pending cart edit stores `remove Rahul`, `preserve Jayant`, `requested_new_text = Ayushi maam IR`.
  3. Existing matcher gets the original/new-target text with explicit IR.
  4. Matcher resolves exact target.
  5. Cart layer completes pending replacement.

The self-test should use the exact same path production uses for new-target resolution rather than shortcutting target choice inside negotiation layer.

---

# 21. Recommended next engineering approach

Do NOT patch blindly.

## Step A — inspect current V34 code first
Read:
- `app/negotiation_layer.py`
- `app/bot.py`
- `app/main_account_bridge.py`
- `app/reply_context.py`
- `app/unified_customer_context.py`
- `app/payment_architecture.py`
- `app/combo_continuity_bridge.py`

Trace:
- where V34 intercepts messages
- ordering relative to FAQ handling
- ordering relative to selection sync
- how it writes recent negotiations
- how it chooses cart intent
- how it invokes matcher for new target
- how pending burst tasks are cancelled/reset
- how reply target is mapped
- how outgoing list messages are anchored

## Step B — add tests BEFORE production patch
Regression cases should include at minimum:

FAQ/no mutation:
1. `Lecture notes sab rahenge na?`
2. `Android me download ho jayega na?`
3. `Dono me notes milenge?`
4. `Sab batches lifetime rahenge?`
5. `Ayushi maam ke lectures complete aur notes bhi milenge na?`

No-action bare words:
6. `sab`
7. `dono`
8. `optional`
9. `Basava`
10. `bhi`

ADD:
11. reply to old card + `Ye bhi chahiye`
12. `Basava sir and optional dono lunga`
13. duplicate `Ye bhi chahiye` does not duplicate item

REMOVE:
14. `Basava wala nahi chahiye`
15. `Basava nahi, optional hi rakh do`

REPLACE:
16. `Rahul nahi, Ayushi maam IR wala chahiye`
   - preserves unrelated Jayant
   - matcher resolves Ayushi IR, not Vision PSIR

Recent price:
17. re-add previously negotiated Jayant at ₹350, not ₹400

Negotiation preservation:
18. T1 bargain → FAQ → T2 bargain must become original-₹50
19. bundle T1 → FAQ → bundle T2 remains correct

Bundle:
20. current negotiated item prices sum correctly
21. cart edit resets only bundle negotiation

Payment:
22. final exact cart + final bundle amount handed to payment
23. ≤999 normal route
24. >999 high-PhonePe route

Reply:
25. bot answers specific-course FAQ as Telegram reply to relevant course/thread
26. customer reply target + `ye bhi` adds exactly referenced item

## Step C — safe production patch
- hash-guard exact current V34
- backup source + DBs
- isolated copy test
- compile
- self-tests
- only then install
- rollback script

---

# 22. Useful exact commands

Check main service:
```bash
systemctl status new-ai-bot.service --no-pager
```

Live log:
```bash
journalctl -u new-ai-bot.service -f
```

Check old test services:
```bash
systemctl is-active new-ai-bot-e2e10000.service
systemctl is-active new-ai-bot-e2e50.service
systemctl is-active new-ai-bot-e2e20-list.service
systemctl is-active new-ai-bot-e2e3-hard.service
```

Production hashes:
```bash
sha256sum \
  /opt/new-ai-bot/app/negotiation_layer.py \
  /opt/new-ai-bot/app/unified_customer_context.py \
  /opt/new-ai-bot/app/payment_architecture.py \
  /opt/new-ai-bot/app/combo_continuity_bridge.py \
  /opt/new-ai-bot/app/reply_context.py \
  /opt/new-ai-bot/app/main_account_bridge.py
```

---

# 23. Catalogue/data examples frequently used in tests

- Vision IAS Geography Optional — Rajesh Govindaraj — ₹500
- Basava Uppin Sir — Economy — ₹300
- Jayant Parikshit — Economy Module — ₹400
- Rahul Puri PSIR — ₹500
- Ayushi Ma'am IR — expected separate IR target, not PSIR
- Top Faculty — ₹999
- Pro Pack — ₹1499

Important:
Do not infer target from name alone when explicit subject exists.

---

# 24. User intent for future Codex work

The user wants Codex to continue as the primary coding environment from here.

Please:
- inspect source before proposing patches
- proactively search for adjacent regressions, not only the one explicitly reported
- explain root cause
- test before install
- preserve working modules
- give copy-paste install/rollback commands
- create test packages when needed
- avoid broad rewrites

The user is specifically frustrated by patches that solve one visible issue while introducing new keyword-routing regressions.

The desired architectural principle is:

> Understand the full conversational intent first; only then mutate state.

Especially:
> A single word or token such as `sab`, `dono`, `all`, `bhi`, `optional`, or a teacher name must never by itself cause a cart mutation or list resend.

And:
> If intent is uncertain, preserve state and use AI/clarification rather than mutating the cart.

---

# 25. Suggested Codex opening task

Use this as the first task in Codex:

```text
Read CODEX_HANDOFF.md completely. Inspect the current production/project source corresponding to V34, especially app/negotiation_layer.py and its call ordering with bot.py, reply_context.py, main_account_bridge.py, unified_customer_context.py, and payment_architecture.py.

Do not change production yet.

First:
1. explain the exact root causes of the V34 generic-FAQ/cart-mutation regressions,
2. explain why the attempted V35 self-test resolved "Ayushi maam IR" to Vision IAS PSIR,
3. identify any additional nearby regressions proactively,
4. design regression tests for FAQ/no-mutation, ADD, REMOVE, REPLACE, reply-add, negotiated-price restore, bundle negotiation, and payment handoff,
5. only after the tests expose the current failures, create a minimal hash-guarded patch based on V34.

Do not modify unified_customer_context.py, payment_architecture.py, combo_continuity_bridge.py, reply_context.py, or main_account_bridge.py unless source inspection proves a change is strictly necessary.
```

---

# 26. Final state summary

- Production: V34
- V35: failed isolated self-test, not installed
- Main current problem: intent ordering in V34 cart/list layer
- Matching resolver: preserve
- Payment architecture: preserve
- Reply infrastructure: preserve
- Need: AI-first intent gate for ambiguous list/cart language
- Need: preserve FAQ state
- Need: safe pending replacement through existing matcher
- Need: recent negotiated price restoration
- Need: stronger proactive regression suite before next patch
