# Final Generic Intent System V2

Corrected installer for the new Generic Intent system.

Flow:
- Generic -> Add Intent
- Intent -> Add Command and/or Add Reply
- Command input `P` is stored as `/P`
- Reply is stored exactly
- AI identifies the configured intent once
- Server deterministically executes the saved reply or command

This V2 specifically anchors live routing to the live customer AI call, avoiding the previous bug where the installer accidentally modified the pending Test AI action block.

The installer validates a candidate with py_compile before replacing the live bot.py.
