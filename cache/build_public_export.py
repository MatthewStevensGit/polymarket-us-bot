"""
Builds cache/polymarket_public.db -- a lightweight export of this project's
research database containing every table small enough to publish, for anyone
reading PAPER.md to independently verify its numbers. Excludes three
high-frequency tick-data tables (position_price_history, order_book_snapshots,
reaction_time_snapshots -- 2.2M+ rows combined, ~420MB) that back the paper's
aggregate statistics but are too large to publish directly; their conclusions
are fully reported in PAPER.md's tables and charts. Re-run any time
cache/polymarket.db is updated.
"""
import sqlite3
from pathlib import Path

SRC = Path(__file__).parent / "polymarket.db"
DST = Path(__file__).parent / "polymarket_public.db"

INCLUDE_TABLES = [
    "trade_history",
    "closed_trades_pnl",
    "clean_price_triples",
    "discovered_games",
    "open_positions",
    "dropped_markets",
    "fanduel_comparison",
    "prop_snapshots",
    "kickoff_window_snapshots",
    "game_poll_state",
    "daily_discovery_state",
]

if DST.exists():
    DST.unlink()

src = sqlite3.connect(SRC)
dst = sqlite3.connect(DST)

for table in INCLUDE_TABLES:
    schema = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not schema:
        print(f"  [skip] {table} not found")
        continue
    dst.execute(schema[0])
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if rows:
        placeholders = ",".join("?" * len(rows[0]))
        dst.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    print(f"  {table}: {len(rows)} rows")

dst.commit()
src.close()
dst.close()

size_kb = DST.stat().st_size / 1024
print(f"\nwrote {DST} ({size_kb:.1f} KB)")
