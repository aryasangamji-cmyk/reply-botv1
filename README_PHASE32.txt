Phase 32 - AI Cache + Relevant Context

Install:
cd /opt/new-ai-bot && unzip -o phase32_ai_cache_relevant_context.zip && .venv/bin/python install_phase32.py

Validate:
cd /opt/new-ai-bot && .venv/bin/python -m py_compile app/bot.py app/context_cache.py

Restart:
cd /opt/new-ai-bot && pkill -9 -f 'run.py' 2>/dev/null || true && .venv/bin/python run.py
