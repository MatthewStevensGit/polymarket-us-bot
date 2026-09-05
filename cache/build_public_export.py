"""
Builds cache/polymarket_public.db -- a lightweight export of this project's
research database containing every table small enough to publish, for anyone
reading the paper to independently verify its numbers. Excludes three
high-frequency tick-data tables (position_price_history, order_book_snapshots,
reaction_time_snapshots -- 2.2M+ rows combined, ~420MB) that back the paper's
aggregate statistics but are too large to publish directly; their conclusions
are fully reported in the paper's tables and charts. Re-run any time
cache/polymarket.db or cache/historical.db is updated.

World Cup tracking data now lives in cache/historical.db, archived out of
the live DB by scripts/archive_to_historical.py once the tournament
concluded -- the paper's figures are unaffected (same rows, same schema,
different file), but this export has to pull each table from wherever it
actually lives now.

Account-ledger tables (trade_history, closed_trades_pnl, settlement_history,
cash_activity) are PERMANENT and never archived -- the account kept trading
after the paper's window under entirely unrelated, still-private real-money
strategies (e.g. a live team-moneyline strategy), so those four tables are
filtered to the paper's own analysis window before being published. Nothing
outside [WINDOW_START, WINDOW_END_EXCLUSIVE) from those four tables should
ever reach this export.
"""
import sqlite3
from pathlib import Path

LIVE_DB = Path(__file__).parent / "polymarket.db"
HISTORICAL_DB = Path(__file__).parent / "historical.db"
DST = Path(__file__).parent / "polymarket_public.db"

# The paper's analysis window, inclusive: 2026-06-15 to 2026-07-19 (see
# worldcup-paper.tex, Results). End is exclusive so the last day is included
# in full without accidentally admitting anything from the 20th onward.
WINDOW_START = "2026-06-15T00:00:00Z"
WINDOW_END_EXCLUSIVE = "2026-07-20T00:00:00Z"

# (table, source db, date column to window-filter on -- None for tables that
# carry no post-window real-trading risk, either because they're inherently
# scoped to the archived tournament already or because they hold no
# financial/strategy data at all).
INCLUDE_TABLES = [
    ("trade_history", LIVE_DB, "trade_time"),
    ("closed_trades_pnl", LIVE_DB, "sell_time"),
    ("settlement_history", LIVE_DB, "resolved_at"),
    ("cash_activity", LIVE_DB, "create_time"),
    ("open_positions", LIVE_DB, None),
    ("daily_discovery_state", LIVE_DB, None),
    ("discovered_games", LIVE_DB, None),
    ("clean_price_triples", HISTORICAL_DB, None),
    ("dropped_markets", HISTORICAL_DB, None),
    ("fanduel_comparison", HISTORICAL_DB, None),
    ("kickoff_window_snapshots", HISTORICAL_DB, None),
    ("game_poll_state", HISTORICAL_DB, None),
]

if DST.exists():
    DST.unlink()

dst = sqlite3.connect(DST)

# One connection per distinct source DB, reused across all its tables,
# instead of a fresh open/close per table.
src_conns = {path: sqlite3.connect(path) for path in {LIVE_DB, HISTORICAL_DB}}

for table, src_path, date_col in INCLUDE_TABLES:
    src = src_conns[src_path]
    schema = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not schema:
        print(f"  [skip] {table} not found in {src_path.name}")
        continue
    dst.execute(schema[0])
    if date_col:
        rows = src.execute(
            f"SELECT * FROM {table} WHERE {date_col} >= ? AND {date_col} < ?",
            (WINDOW_START, WINDOW_END_EXCLUSIVE),
        ).fetchall()
    else:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if rows:
        placeholders = ",".join("?" * len(rows[0]))
        dst.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    tag = f" (windowed on {date_col})" if date_col else ""
    print(f"  {table} ({src_path.name}){tag}: {len(rows)} rows")

for conn in src_conns.values():
    conn.close()

dst.commit()
dst.close()

size_kb = DST.stat().st_size / 1024
print(f"\nwrote {DST} ({size_kb:.1f} KB)")
