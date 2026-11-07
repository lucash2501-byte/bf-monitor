"""Persistência em SQLite: histórico de preços e alertas enviados."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    price_cash REAL,
    price_installment REAL,
    in_stock INTEGER,           -- 1 = em estoque, 0 = sem estoque, NULL = desconhecido
    coupons TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_prices_product_ts ON prices(product_id, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_product_ts ON alerts(product_id, ts);
"""

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def save_snapshot(db_path: str, product_id: str, snap) -> None:
    in_stock = None if snap.in_stock is None else int(snap.in_stock)
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO prices (product_id, ts, price_cash, price_installment, in_stock, coupons, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                product_id,
                _now(),
                snap.price_cash,
                snap.price_installment,
                in_stock,
                " | ".join(snap.coupons) if snap.coupons else None,
                snap.error,
            ),
        )


def get_price_history(db_path: str, product_id: str, days: int) -> list[tuple[str, float]]:
    """Leituras válidas (ts, preço à vista) dos últimos `days` dias, da mais antiga para a mais nova.

    Chame ANTES de salvar a leitura atual, para ela não entrar na própria referência.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, price_cash FROM prices "
            "WHERE product_id = ? AND price_cash IS NOT NULL AND price_cash > 0 "
            "AND ts >= datetime('now', ?) ORDER BY ts, id",
            (product_id, f"-{int(days)} days"),
        ).fetchall()
    return [(r["ts"], r["price_cash"]) for r in rows]


def get_last_reading(db_path: str, product_id: str) -> Optional[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT ts, price_cash, in_stock, error FROM prices WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()


def count_errors(db_path: str, product_id: str, hours: float) -> int:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM prices WHERE product_id = ? AND error IS NOT NULL "
            "AND ts >= datetime('now', ?)",
            (product_id, f"-{int(hours * 60)} minutes"),
        ).fetchone()
    return int(row["n"])


def get_consecutive_errors(db_path: str, product_id: str) -> int:
    """Quantas leituras seguidas (da mais recente para trás) terminaram em erro."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT error FROM prices WHERE product_id = ? ORDER BY id DESC LIMIT 500",
            (product_id,),
        ).fetchall()
    n = 0
    for r in rows:
        if r["error"] is None:
            break
        n += 1
    return n


def get_last_alert(db_path: str, product_id: str) -> Optional[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT ts, price FROM alerts WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()


def save_alert(db_path: str, product_id: str, price: float) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO alerts (product_id, ts, price) VALUES (?, ?, ?)",
            (product_id, _now(), price),
        )


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)


def hours_since(ts: str) -> float:
    return (datetime.now(timezone.utc) - parse_ts(ts)).total_seconds() / 3600
