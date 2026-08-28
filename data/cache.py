"""
Local SQLite cache for player-prop market snapshots.

Each run appends a timestamped row per market rather than overwriting, so
repeated pulls build a local price history you can later join against your
own goal/assist probability estimates (Polymarket US doesn't expose a public
historical-price endpoint — only current book/BBO — so this is how we build
one ourselves).
"""

import csv
import os
import sqlite3
from datetime import datetime, timezone

from config import CACHE_DB_PATH, CACHE_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS prop_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    event_id TEXT,
    event_title TEXT,
    market_id TEXT,
    market_slug TEXT,
    question TEXT,
    subject TEXT,
    sports_market_type TEXT,
    line REAL,
    best_bid REAL,
    best_ask REAL,
    last_trade_px REAL,
    shares_traded REAL
);
CREATE INDEX IF NOT EXISTS idx_prop_snapshots_slug_time
    ON prop_snapshots (market_slug, fetched_at);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def save_snapshot(rows: list[dict]) -> int:
    if not rows:
        return 0
    fetched_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    with conn:
        conn.executemany(
            """
            INSERT INTO prop_snapshots (
                fetched_at, event_id, event_title, market_id, market_slug,
                question, subject, sports_market_type, line,
                best_bid, best_ask, last_trade_px, shares_traded
            ) VALUES (
                :fetched_at, :event_id, :event_title, :market_id, :market_slug,
                :question, :subject, :sports_market_type, :line,
                :best_bid, :best_ask, :last_trade_px, :shares_traded
            )
            """,
            [{**row, "fetched_at": fetched_at} for row in rows],
        )
    conn.close()
    return len(rows)


def export_csv(path: str = f"{CACHE_DIR}/prop_snapshots.csv") -> None:
    conn = _connect()
    cur = conn.execute("SELECT * FROM prop_snapshots ORDER BY fetched_at")
    cols = [d[0] for d in cur.description]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(cur.fetchall())
    conn.close()
    print(f"  [export] wrote {path}")
