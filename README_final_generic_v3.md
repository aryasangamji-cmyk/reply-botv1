FINAL GENERIC INTENT SYSTEM V3

Fixes V2 startup failure:
- Restores the legacy ensure_generic_replies_table() startup symbol.
- Creates the legacy generic_replies table if needed for compatibility.
- Keeps the new final_generic_intents/final_generic_actions system independent.
- Candidate is compiled before live bot.py replacement.

Install:
  unzip -o final_generic_intent_system_v3.zip
  .venv/bin/python install_final_generic_v3.py

Then compile:
  .venv/bin/python -m py_compile app/bot.py
