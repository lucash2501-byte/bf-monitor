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

CREATE TABLE IF NOT EXISTS coupons_seen (
    host TEXT NOT NULL,
    text TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_notified TEXT,
    PRIMARY KEY (host, text)
);

CREATE TABLE IF NOT EXISTS compare_candidates (
    product_id TEXT NOT NULL,
    host TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    color_ok INTEGER,           -- 1 = cor confere, NULL = a listagem não diz
    found_ts TEXT NOT NULL,
    PRIMARY KEY (product_id, url)
);
CREATE TABLE IF NOT EXISTS store_checks (
    name TEXT PRIMARY KEY,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coupon_cache (
    store TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,          -- loja | produto | agregador
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (store, text)
);
CREATE TABLE IF NOT EXISTS store_status (
    store TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS compare_searches (
    product_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL
);
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


def get_last_price(db_path: str, product_id: str) -> Optional[float]:
    """Última leitura de preço válida (ignora leituras com erro)."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT price_cash FROM prices WHERE product_id = ? AND price_cash IS NOT NULL AND price_cash > 0 "
            "ORDER BY id DESC LIMIT 1", (product_id,)).fetchone()
    return float(row["price_cash"]) if row else None


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


def _coupon_key(text: str) -> str:
    return " ".join(text.split()).lower()


def new_coupons(db_path: str, host: str, texts: list[str], renotify_hours: float) -> list[str]:
    """Cupons que ainda não foram avisados (ou que já passaram de `renotify_hours`; 0 = nunca repetir)."""
    fresh = []
    with connect(db_path) as conn:
        for text in texts:
            row = conn.execute("SELECT last_notified FROM coupons_seen WHERE host = ? AND text = ?",
                               (host, _coupon_key(text))).fetchone()
            if row is None or row["last_notified"] is None:
                fresh.append(text)
            elif renotify_hours > 0 and hours_since(row["last_notified"]) >= renotify_hours:
                fresh.append(text)
    return fresh


def mark_coupons_notified(db_path: str, host: str, texts: list[str]) -> None:
    now = _now()
    with connect(db_path) as conn:
        for text in texts:
            conn.execute(
                "INSERT INTO coupons_seen (host, text, first_seen, last_notified) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(host, text) DO UPDATE SET last_notified = excluded.last_notified",
                (host, _coupon_key(text), now, now),
            )


# ------------------------------------------------------------------ comparação entre lojas
def hours_since_candidates(db_path: str, product_id: str) -> Optional[float]:
    """Horas desde a última busca de candidatos deste produto (None = nunca buscou)."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT ts FROM compare_searches WHERE product_id = ?", (product_id,)).fetchone()
    return hours_since(row["ts"]) if row else None


def save_candidates(db_path: str, product_id: str, items: list[tuple]) -> None:
    """Substitui os candidatos do produto por (host, url, title, color_ok) e marca a busca como feita agora."""
    now = _now()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM compare_candidates WHERE product_id = ?", (product_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO compare_candidates (product_id, host, url, title, color_ok, found_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(product_id, h, u, t, None if c is None else int(c), now) for h, u, t, c in items],
        )
        conn.execute("INSERT OR REPLACE INTO compare_searches (product_id, ts) VALUES (?, ?)", (product_id, now))


def get_candidates(db_path: str, product_id: str) -> list[tuple]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT host, url, title, color_ok FROM compare_candidates WHERE product_id = ?",
                            (product_id,)).fetchall()
    return [(r["host"], r["url"], r["title"], None if r["color_ok"] is None else bool(r["color_ok"])) for r in rows]


# ------------------------------------------------------------------ cupons por loja (sem produto)
def hours_since_store_check(db_path: str, name: str) -> Optional[float]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT ts FROM store_checks WHERE name = ?", (name,)).fetchone()
    return hours_since(row["ts"]) if row else None


def mark_store_checked(db_path: str, name: str) -> None:
    with connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO store_checks (name, ts) VALUES (?, ?)", (name, _now()))


# ------------------------------------------------------------------ cache de cupons (consultado pelo bot do Telegram)
def cache_coupons(db_path: str, store: str, texts: list[str], source: str) -> None:
    """Guarda/atualiza os cupons vistos agora numa loja. Um cupom que some da página para de ser exportado
    (ver `get_cached_coupons`), sem precisar apagar nada."""
    now = _now()
    with connect(db_path) as conn:
        for text in texts:
            t = " ".join(text.split())
            if t:
                conn.execute(
                    "INSERT INTO coupon_cache (store, text, source, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(store, text) DO UPDATE SET last_seen = excluded.last_seen, source = excluded.source",
                    (store, t, source, now, now))


def set_store_status(db_path: str, store: str, error: Optional[str]) -> None:
    with connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO store_status (store, ts, error) VALUES (?, ?, ?)", (store, _now(), error))


def get_store_statuses(db_path: str) -> dict[str, tuple[str, Optional[str]]]:
    with connect(db_path) as conn:
        return {r["store"]: (r["ts"], r["error"]) for r in conn.execute("SELECT store, ts, error FROM store_status")}


def get_cached_coupons(db_path: str, max_age_hours: float) -> list[tuple[str, str, str, str]]:
    """(loja, texto, origem, visto_em) dos cupons vistos nas últimas `max_age_hours` horas."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT store, text, source, last_seen FROM coupon_cache ORDER BY store, last_seen DESC").fetchall()
    return [(r["store"], r["text"], r["source"], r["last_seen"]) for r in rows if hours_since(r["last_seen"]) <= max_age_hours]
