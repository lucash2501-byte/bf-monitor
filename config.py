"""Configurações carregadas do arquivo .env."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "sim", "y"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    interval_minutes: float
    history_days: int
    min_history_samples: int
    min_discount_pct: float
    cooldown_hours: float
    allow_alert_without_history: bool
    headless: bool
    page_timeout_ms: int
    max_retries: int
    db_path: str
    products_path: str


def load_settings() -> Settings:
    return Settings(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        interval_minutes=float(os.getenv("INTERVAL_MINUTES", "20")),
        history_days=int(os.getenv("HISTORY_DAYS", "14")),
        min_history_samples=int(os.getenv("MIN_HISTORY_SAMPLES", "8")),
        min_discount_pct=float(os.getenv("MIN_DISCOUNT_PCT", "5")),
        cooldown_hours=float(os.getenv("ALERT_COOLDOWN_HOURS", "6")),
        allow_alert_without_history=_bool("ALLOW_ALERT_WITHOUT_HISTORY", "false"),
        headless=_bool("HEADLESS", "true"),
        page_timeout_ms=int(os.getenv("PAGE_TIMEOUT_MS", "45000")),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
        db_path=os.getenv("DB_PATH", "prices.db"),
        products_path=os.getenv("PRODUCTS_PATH", "products.json"),
    )
