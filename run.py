import asyncio
import logging
from app.config import load_settings
from app.bot import run

settings = load_settings()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
    asyncio.run(run(settings))
