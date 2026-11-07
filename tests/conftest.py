import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Settings  # noqa: E402


def make_settings(tmp_path=None, **overrides) -> Settings:
    values = dict(
        telegram_token="123:ABC", telegram_chat_id="42",
        interval_minutes=20, history_days=14, min_history_samples=8, min_history_span_hours=24,
        min_discount_pct=5, cooldown_hours=6, allow_alert_without_history=False,
        alert_on_unknown_stock=True, confirm_alerts=True, fail_notice_after=3,
        headless=True, page_timeout_ms=15000, max_retries=1,
        db_path=str(tmp_path / "t.db") if tmp_path else ":memory:",
        products_path="products.json", log_file="", debug_dir="",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)
