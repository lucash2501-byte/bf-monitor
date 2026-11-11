"""Configurações carregadas do arquivo .env."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "sim", "y"}


def _num(name: str, default: str, cast=float, minimum=None):
    """Lê um número do ambiente com mensagem de erro amigável (em vez de traceback)."""
    raw = (os.getenv(name) or "").strip() or default
    try:
        value = cast(raw.replace(",", ".")) if cast is float else cast(raw)
    except ValueError:
        raise SystemExit(f"Valor inválido para {name} no .env: {raw!r} (esperado número)") from None
    if minimum is not None and value < minimum:
        raise SystemExit(f"Valor inválido para {name} no .env: {raw!r} (mínimo {minimum})")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    interval_minutes: float
    history_days: int
    min_history_samples: int
    min_history_span_hours: float
    min_discount_pct: float
    cooldown_hours: float
    allow_alert_without_history: bool
    alert_on_unknown_stock: bool
    confirm_alerts: bool
    fail_notice_after: int
    headless: bool
    page_timeout_ms: int
    max_retries: int
    db_path: str
    products_path: str
    log_file: str
    debug_dir: str
    notify_coupons: bool = False
    coupon_renotify_hours: float = 168.0
    compare_enabled: bool = True
    compare_min_diff_pct: float = 5.0
    compare_search_every_hours: float = 6.0
    compare_max_searches: int = 3
    compare_max_candidates: int = 4
    store_coupons_enabled: bool = True
    store_coupons_every_hours: float = 2.0


def load_settings() -> Settings:
    return Settings(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        interval_minutes=_num("INTERVAL_MINUTES", "20", float, 1),
        history_days=_num("HISTORY_DAYS", "14", int, 1),
        min_history_samples=_num("MIN_HISTORY_SAMPLES", "8", int, 1),
        min_history_span_hours=_num("MIN_HISTORY_SPAN_HOURS", "24", float, 0),
        min_discount_pct=_num("MIN_DISCOUNT_PCT", "10", float, 0),
        cooldown_hours=_num("ALERT_COOLDOWN_HOURS", "6", float, 0),
        allow_alert_without_history=_bool("ALLOW_ALERT_WITHOUT_HISTORY", "false"),
        alert_on_unknown_stock=_bool("ALERT_ON_UNKNOWN_STOCK", "true"),
        confirm_alerts=_bool("CONFIRM_ALERTS", "true"),
        fail_notice_after=_num("FAIL_NOTICE_AFTER", "6", int, 0),
        headless=_bool("HEADLESS", "true"),
        page_timeout_ms=_num("PAGE_TIMEOUT_MS", "45000", int, 1000),
        max_retries=_num("MAX_RETRIES", "3", int, 1),
        db_path=os.getenv("DB_PATH", "prices.db"),
        products_path=os.getenv("PRODUCTS_PATH", "products.json"),
        log_file=os.getenv("LOG_FILE", "monitor.log"),
        debug_dir=os.getenv("DEBUG_DIR", "").strip(),
        notify_coupons=_bool("NOTIFY_COUPONS", "false"),
        coupon_renotify_hours=_num("COUPON_RENOTIFY_HOURS", "168", float, 0),
        compare_enabled=_bool("COMPARE_ENABLED", "true"),
        compare_min_diff_pct=_num("COMPARE_MIN_DIFF_PCT", "5", float, 0),
        compare_search_every_hours=_num("COMPARE_SEARCH_EVERY_HOURS", "6", float, 0),
        compare_max_searches=_num("COMPARE_MAX_SEARCHES", "3", int, 0),
        compare_max_candidates=_num("COMPARE_MAX_CANDIDATES", "4", int, 1),
        store_coupons_enabled=_bool("STORE_COUPONS_ENABLED", "true"),
        store_coupons_every_hours=_num("STORE_COUPONS_EVERY_HOURS", "2", float, 0),
    )
