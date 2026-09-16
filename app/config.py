import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

def csv_ints(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]

@dataclass(frozen=True)
class Settings:
    telegram_token: str
    openai_api_key: str
    openai_model: str
    admin_user_ids: list[int]
    database_path: str
    media_root: str
    log_level: str

def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return Settings(
        telegram_token=token,
        openai_api_key=key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5"),
        admin_user_ids=csv_ints(os.getenv("ADMIN_USER_IDS", "")),
        database_path=os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
        media_root=os.getenv("MEDIA_ROOT", "data/media"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
