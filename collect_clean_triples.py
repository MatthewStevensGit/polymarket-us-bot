"""
Standalone, unattended collector for the goals/assists/goals+assists
"clean triple" research dataset — meant to run on a timer (e.g. Windows
Task Scheduler), independent of any AI/Claude session.

A "clean triple" is a player's 1+ tier goals, assists, and goals+assists
last-traded prices, all captured from a single confirmed World Cup game that
has NOT started yet (not live, not closed) — so the three prices reflect the
same pre-kickoff market state rather than different moments of an in-progress
game. Rows are appended to cache/polymarket.db (clean_price_triples table);
re-running is safe, duplicates are skipped via a UNIQUE(game_slug, player)
constraint.

Usage:
    python collect_clean_triples.py
"""

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CACHE_DB_PATH, POLYMARKET_US_KEY_ID, POLYMARKET_US_SECRET_KEY, PROP_SPORTS_MARKET_TYPES
from data.client import GatewayClient, TradingClient
from data.markets import find_world_cup_events

# How many price levels per side to keep from the order book each snapshot.
ORDER_BOOK_DEPTH = 5

LOG_PATH = Path(__file__).resolve().parent / "cache" / "collect_triples.log"

TYPE_LABEL = {
    "soccer_player_goals": "goals",
    "soccer_player_assists": "assists",
    "soccer_player_goals_plus_assists": "goals+assists",
}
QUESTION_RE = re.compile(r"Will (.+?) record at least \d+")
GAME_SLUG_RE = re.compile(r"^fwc-([a-z]{3})-([a-z]{3})-\d{4}-\d{2}-\d{2}$")

# Players/games to track every single run (no tiering/skipping) -- goals,
# assists, and goals+assists 1+ markets. Derived live from clean_price_triples
# (see _get_tracked_positions) rather than a hardcoded held-positions list:
# any player who already has real goals/assists/goals+assists price data
# gets this denser tracking, whether or not an actual trade has been placed.
WATCHED_TYPES = ("goals", "assists", "goals+assists")


def _get_tracked_positions(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute("SELECT DISTINCT game_slug, player FROM clean_price_triples").fetchall()
    tracked: dict[str, list[str]] = {}
    for game_slug, player in rows:
        tracked.setdefault(game_slug, []).append(player)
    return tracked

# Tiered poll interval by time-to-kickoff (seconds), checked furthest-out first.
POLL_TIERS = (
    (6 * 3600, 20 * 60),   # more than 6h out -> every 20 min
    (1 * 3600, 5 * 60),    # 1h-6h out        -> every 5 min
    (30 * 60, 3 * 60),     # 30min-1h out     -> every 3 min
    (0, 60),               # final 30 min     -> every 1 min
)

# High-frequency layer on top of the tiers above, active only within +/- this
# many seconds of a watched game's kickoff -- investigating a specific
# unexplained price move, not routine monitoring, so it's logged separately
# from position_price_history rather than mixed in.
KICKOFF_WINDOW_SECONDS = 5 * 60
KICKOFF_WINDOW_POLL_INTERVAL = 10
# Stay comfortably under the 60s Task Scheduler cadence so this loop finishes
# before the next scheduled run would fire (MultipleInstances=IgnoreNew means
# an overlap would just get silently skipped rather than crash, but budgeting
# under 60s avoids relying on that).
KICKOFF_WINDOW_RUN_SECONDS = 45
KICKOFF_WINDOW_TYPES = ("goals", "goals+assists")

# Reaction-time measurement layer: once a watched game goes live (per the
# API's own `live` flag, not a time-since-kickoff guess), poll goals and
# goals+assists only -- assists deliberately excluded, too ambiguous to
# timestamp/confirm precisely -- at 1s intervals for the full live duration,
# not just the +/-5min kickoff window above. Purely a logging/measurement
# exercise: no trading logic, no automation reacts to this table.
REACTION_TIME_POLL_INTERVAL = 1
REACTION_TIME_RUN_SECONDS = 45  # same 60s-cadence budget reasoning as above
REACTION_TIME_TYPES = ("goals", "goals+assists")


def _poll_interval_seconds(time_to_kickoff: float) -> int:
    for threshold, interval in POLL_TIERS:
        if time_to_kickoff >= threshold:
            return interval
    return POLL_TIERS[-1][1]


def _log(line: str) -> None:
    stamped = f"{datetime.now(timezone.utc).isoformat()} {line}"
    # sys.stdout is None under pythonw.exe (no console attached) -- printing
    # there would raise and abort the run before the file write below. Even
    # with a console attached, Windows' default cp1252 codepage can't encode
    # many real player names (diacritics like Sorloth, Memic, Guler) -- a
    # crash here must never take down the whole run just because a name
    # didn't fit the console's encoding, so fall back to a replaced-chars
    # version rather than letting print() raise.
    if sys.stdout is not None:
        try:
            print(stamped)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "ascii"
            print(stamped.encode(encoding, errors="replace").decode(encoding))
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(stamped + "\n")


def _connect() -> sqlite3.Connection:
    # busy_timeout lets SQLite itself wait out short internal lock contention;
    # the outer retry in main() covers longer external locks (e.g. antivirus
    # or an OS file indexer holding the file open after a write).
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=10)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clean_price_triples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            player TEXT NOT NULL,
            goals_last REAL,
            assists_last REAL,
            ga_last REAL,
            formula REAL,
            gap REAL,
            UNIQUE(game_slug, player)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS game_poll_state (
            game_slug TEXT PRIMARY KEY,
            last_checked_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            player TEXT NOT NULL,
            market_type TEXT NOT NULL,
            tier REAL NOT NULL,
            best_bid REAL,
            best_ask REAL,
            last_trade_px REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_book_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            player TEXT NOT NULL,
            market_type TEXT NOT NULL,
            tier REAL NOT NULL,
            bids_json TEXT,
            offers_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL UNIQUE,
            logged_at TEXT NOT NULL,
            trade_time TEXT,
            game_slug TEXT,
            market_slug TEXT,
            player TEXT,
            market_type TEXT,
            tier REAL,
            side TEXT,
            is_aggressor INTEGER,
            price REAL,
            qty REAL,
            cost REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kickoff_window_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            player TEXT NOT NULL,
            market_type TEXT NOT NULL,
            bid REAL,
            ask REAL,
            bid_size REAL,
            ask_size REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reaction_time_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            player TEXT NOT NULL,
            market_type TEXT NOT NULL,
            bid REAL,
            ask REAL,
            bid_size REAL,
            ask_size REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS open_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player TEXT NOT NULL,
            market_type TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            tier REAL NOT NULL,
            remaining_size REAL NOT NULL,
            avg_cost_basis REAL,
            remaining_cost_basis REAL,
            strategy_tag TEXT,
            UNIQUE(player, market_type, game_slug, tier)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS closed_trades_pnl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sell_trade_id TEXT NOT NULL UNIQUE,
            player TEXT NOT NULL,
            market_type TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            tier REAL,
            shares_closed REAL NOT NULL,
            avg_buy_price REAL NOT NULL,
            sell_price REAL NOT NULL,
            dollar_pnl REAL NOT NULL,
            percent_pnl REAL NOT NULL,
            sell_time TEXT NOT NULL,
            strategy_tag TEXT
        )
        """
    )
    # Migration: both tables originally grouped positions by
    # (player, market_type, game_slug) only, which silently pooled different
    # tiers (e.g. "1+ goals" vs "2+ goals") of the same market together as if
    # they were the same instrument -- they are not, each tier is an
    # independently priced token. Add `tier` to the real key. Safe to drop
    # and rebuild open_positions (it's fully derived from trade_history and
    # currently has zero user-entered strategy_tag values); closed_trades_pnl
    # just gets the column added, since its real key (sell_trade_id) doesn't
    # need to change.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(open_positions)")}
    if "tier" not in existing_cols:
        if conn.execute("SELECT COUNT(*) FROM open_positions WHERE strategy_tag IS NOT NULL").fetchone()[0]:
            raise RuntimeError(
                "open_positions has tagged rows but predates the `tier` column -- "
                "refusing to auto-drop it. Migrate manually."
            )
        conn.execute("DROP TABLE open_positions")
        conn.execute(
            """
            CREATE TABLE open_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player TEXT NOT NULL,
                market_type TEXT NOT NULL,
                game_slug TEXT NOT NULL,
                tier REAL NOT NULL,
                remaining_size REAL NOT NULL,
                avg_cost_basis REAL,
                remaining_cost_basis REAL,
                strategy_tag TEXT,
                UNIQUE(player, market_type, game_slug, tier)
            )
            """
        )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(closed_trades_pnl)")}
    if "tier" not in existing_cols:
        conn.execute("ALTER TABLE closed_trades_pnl ADD COLUMN tier REAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discovered_games (
            game_slug TEXT PRIMARY KEY,
            title TEXT,
            start_date TEXT,
            players TEXT,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_discovery_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_run_date TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dropped_markets (
            market_slug TEXT PRIMARY KEY,
            player TEXT NOT NULL,
            market_type TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            dropped_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fanduel_comparison (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            player TEXT NOT NULL,
            game_slug TEXT NOT NULL,
            market_type TEXT NOT NULL,
            settlement_window TEXT NOT NULL,
            fanduel_odds_american REAL NOT NULL,
            fanduel_raw_implied_prob REAL NOT NULL,
            polymarket_price REAL,
            polymarket_quote_time TEXT,
            diff REAL,
            source_url TEXT,
            notes TEXT
        )
        """
    )
    _ensure_game_views(conn)
    _ensure_live_formula_gap_view(conn)
    return conn


def _game_short_name(game_slug: str) -> str:
    match = GAME_SLUG_RE.match(game_slug)
    if match:
        return f"{match.group(1)}_{match.group(2)}"
    return re.sub(r"[^a-z0-9]+", "_", game_slug.lower()).strip("_")


# market_type label -> column-name prefix used in the pivoted per-game views.
VIEW_TYPE_PREFIX = {"goals": "goals", "assists": "assists", "goals+assists": "ga"}


def _position_price_history_view_sql(escaped_slug: str) -> str:
    columns = []
    for market_type, prefix in VIEW_TYPE_PREFIX.items():
        columns.append(
            f"MAX(CASE WHEN market_type = '{market_type}' THEN best_bid END) AS {prefix}_bid"
        )
        columns.append(
            f"MAX(CASE WHEN market_type = '{market_type}' THEN best_ask END) AS {prefix}_ask"
        )
    return f"""
        SELECT fetched_at, player, {', '.join(columns)}
        FROM position_price_history
        WHERE game_slug = '{escaped_slug}'
        GROUP BY fetched_at, player
    """


def _order_book_snapshots_view_sql(escaped_slug: str) -> str:
    columns = []
    for market_type, prefix in VIEW_TYPE_PREFIX.items():
        for side, side_prefix in (("bids_json", "bid"), ("offers_json", "ask")):
            for field, suffix in (("px", "px"), ("qty", "sz")):
                expr = f"CAST(json_extract({side}, '$[0].{field}') AS REAL)"
                columns.append(
                    f"MAX(CASE WHEN market_type = '{market_type}' THEN {expr} END) "
                    f"AS {prefix}_{side_prefix}_{suffix}"
                )
    return f"""
        SELECT fetched_at, player, {', '.join(columns)}
        FROM order_book_snapshots
        WHERE game_slug = '{escaped_slug}'
        GROUP BY fetched_at, player
    """


def _live_formula_gap_view_sql(conn: sqlite3.Connection) -> str:
    tracked_players = sorted({p for players in _get_tracked_positions(conn).values() for p in players})
    # SQLite's `IN ()` is a syntax error with zero elements -- fall back to a
    # sentinel that can't match a real player name so the view still builds.
    escaped_players = ", ".join("'" + p.replace("'", "''") + "'" for p in tracked_players) or "'__none__'"
    return f"""
        -- LIVE CONVENIENCE VIEW, separate from clean_price_triples: no dedup,
        -- no pre-kickoff-only restriction, no minimum-instance threshold, and
        -- NOT a source of clean research instances. Recomputed fresh from
        -- position_price_history's most recent rows on every query, using
        -- last-trade price for all three legs -- same convention as
        -- clean_price_triples's own formula/gap, for direct comparability.
        -- Shows every currently tracked player at all times, live or not.
        WITH latest AS (
            SELECT player, market_type, last_trade_px, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY player, market_type ORDER BY fetched_at DESC) AS rn
            FROM position_price_history
            WHERE player IN ({escaped_players})
        ),
        pivoted AS (
            SELECT
                player,
                MAX(CASE WHEN market_type = 'goals' THEN last_trade_px END) AS goals_price,
                MAX(CASE WHEN market_type = 'assists' THEN last_trade_px END) AS assists_price,
                MAX(CASE WHEN market_type = 'goals+assists' THEN last_trade_px END) AS ga_price,
                MAX(fetched_at) AS most_recent_quote_time
            FROM latest
            WHERE rn = 1
            GROUP BY player
        )
        SELECT
            player, goals_price, assists_price, ga_price,
            goals_price + assists_price - goals_price * assists_price AS formula,
            ga_price - (goals_price + assists_price - goals_price * assists_price) AS gap,
            most_recent_quote_time
        FROM pivoted
    """


def _ensure_live_formula_gap_view(conn: sqlite3.Connection) -> None:
    conn.execute("DROP VIEW IF EXISTS live_formula_gap")
    conn.execute(f"CREATE VIEW live_formula_gap AS {_live_formula_gap_view_sql(conn)}")


def _ensure_game_views(conn: sqlite3.Connection) -> None:
    """Auto-create (and rebuild) per-game SQL views for every game currently
    tracked (per _get_tracked_positions), one row per (fetched_at, player)
    pivoted across market types. A view is a saved query, not a copy of the
    data -- it re-runs its SELECT against the live table on every read, so it
    is mechanically impossible for it to drift out of sync or hold stale
    data. Running this every time the collector connects means a newly
    tracked game gets its views created automatically on the very next run,
    with no manual SQL step required."""
    view_sql_builders = {
        "position_price_history": _position_price_history_view_sql,
        "order_book_snapshots": _order_book_snapshots_view_sql,
    }
    for game_slug in _get_tracked_positions(conn):
        short = _game_short_name(game_slug)
        escaped_slug = game_slug.replace("'", "''")
        for table, build_sql in view_sql_builders.items():
            view_name = f"view_{table}_{short}"
            conn.execute(f"DROP VIEW IF EXISTS {view_name}")
            conn.execute(f"CREATE VIEW {view_name} AS {build_sql(escaped_slug)}")


def _due_for_poll(conn: sqlite3.Connection, game_slug: str, interval_seconds: int, now: datetime) -> bool:
    row = conn.execute(
        "SELECT last_checked_at FROM game_poll_state WHERE game_slug = ?", (game_slug,)
    ).fetchone()
    if row is None:
        return True
    last_checked = datetime.fromisoformat(row[0])
    return (now - last_checked).total_seconds() >= interval_seconds


def _mark_checked(conn: sqlite3.Connection, game_slug: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO game_poll_state (game_slug, last_checked_at) VALUES (?, ?)
        ON CONFLICT(game_slug) DO UPDATE SET last_checked_at = excluded.last_checked_at
        """,
        (game_slug, now.isoformat()),
    )


def _not_started(event: dict) -> bool:
    if event.get("live") or event.get("closed"):
        return False
    start = event.get("startDate")
    if not start:
        return False
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return start_dt > datetime.now(timezone.utc)


def _collect_triples(client: GatewayClient, event: dict) -> list[dict]:
    by_player: dict[str, dict] = {}
    for market in event.get("markets") or []:
        smt = market.get("sportsMarketType")
        if smt not in PROP_SPORTS_MARKET_TYPES or market.get("line") != 1:
            continue
        match = QUESTION_RE.match(market.get("question") or "")
        if not match:
            continue
        player = match.group(1)
        slug = market.get("slug")
        if not slug:
            continue
        try:
            bbo = client.get_market_bbo(slug).get("marketData") or {}
        except Exception as exc:
            _log(f"  [warn] bbo failed for {slug}: {exc}")
            continue
        last = (bbo.get("lastTradePx") or {}).get("value")
        if last is not None:
            by_player.setdefault(player, {})[TYPE_LABEL[smt]] = float(last)

    triples = []
    for player, values in by_player.items():
        if {"goals", "assists", "goals+assists"} <= values.keys():
            g, a, ga = values["goals"], values["assists"], values["goals+assists"]
            formula = g + a - g * a
            triples.append({
                "player": player, "goals_last": g, "assists_last": a, "ga_last": ga,
                "formula": round(formula, 4), "gap": round(formula - ga, 4),
            })
    return triples


def _book_levels(book: dict, side: str) -> str:
    levels = [
        {"px": lvl.get("px", {}).get("value"), "qty": lvl.get("qty")}
        for lvl in (book.get(side) or [])[:ORDER_BOOK_DEPTH]
    ]
    return json.dumps(levels)


def _log_watched_positions(client: GatewayClient, conn: sqlite3.Connection, fetched_at: str) -> tuple[int, int]:
    """Log every watched player/market every run, unconditionally -- this is
    a dense timeline for actively watching open positions, not a deduped
    research sample, so it does not participate in the tiered polling.

    Logs both top-of-book (position_price_history) and order-book depth
    (order_book_snapshots) from the same market lookup each run. A price
    level disappearing between two snapshots could mean either a fill or a
    plain cancellation -- this data alone can't tell those apart; that would
    need matching a level's price against a last_trade_px change at that
    same level."""
    price_rows, book_rows = 0, 0
    for game_slug, players in _get_tracked_positions(conn).items():
        try:
            event = client.get_event_by_slug(game_slug).get("event") or {}
        except Exception as exc:
            _log(f"  [warn] could not fetch {game_slug} for position watch: {exc}")
            continue

        for market in event.get("markets") or []:
            smt = market.get("sportsMarketType")
            if smt not in TYPE_LABEL or TYPE_LABEL[smt] not in WATCHED_TYPES or market.get("line") != 1:
                continue
            match = QUESTION_RE.match(market.get("question") or "")
            if not match or match.group(1) not in players:
                continue
            slug = market.get("slug")
            if not slug:
                continue
            player, market_type, tier = match.group(1), TYPE_LABEL[smt], market.get("line")

            if market.get("closed"):
                if conn.execute("SELECT 1 FROM dropped_markets WHERE market_slug = ?", (slug,)).fetchone() is None:
                    conn.execute(
                        "INSERT INTO dropped_markets (market_slug, player, market_type, game_slug, dropped_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (slug, player, market_type, game_slug, fetched_at),
                    )
                    _log(f"  dropped {player}/{market_type} ({slug}) -- market closed, no longer polling")
                continue

            try:
                bbo = client.get_market_bbo(slug).get("marketData") or {}
            except Exception as exc:
                _log(f"  [warn] bbo failed for {slug}: {exc}")
                bbo = None
            if bbo is not None:
                conn.execute(
                    """
                    INSERT INTO position_price_history
                        (fetched_at, game_slug, player, market_type, tier, best_bid, best_ask, last_trade_px)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (fetched_at, game_slug, player, market_type, tier,
                     (bbo.get("bestBid") or {}).get("value"), (bbo.get("bestAsk") or {}).get("value"),
                     (bbo.get("lastTradePx") or {}).get("value")),
                )
                price_rows += 1

            try:
                book = client.get_market_book(slug).get("marketData") or {}
            except Exception as exc:
                _log(f"  [warn] book fetch failed for {slug}: {exc}")
                continue
            conn.execute(
                """
                INSERT INTO order_book_snapshots
                    (fetched_at, game_slug, player, market_type, tier, bids_json, offers_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (fetched_at, game_slug, player, market_type, tier,
                 _book_levels(book, "bids"), _book_levels(book, "offers")),
            )
            book_rows += 1
    conn.commit()
    return price_rows, book_rows


def _kickoff_window_targets(
    client: GatewayClient, conn: sqlite3.Connection, now: datetime
) -> dict[str, tuple[list[str], dict]]:
    """Return {game_slug: (players, event)} for every tracked game currently
    within +/- KICKOFF_WINDOW_SECONDS of its kickoff (startDate), EXCLUDING
    games that have already gone live -- those hand off to the reaction-time
    mechanism instead, which covers the full live duration, not just this
    window. Without this exclusion, a game in its first 5 live minutes would
    get polled by both loops, doubling API calls and time budget for no
    benefit."""
    active = {}
    for game_slug, players in _get_tracked_positions(conn).items():
        try:
            event = client.get_event_by_slug(game_slug).get("event") or {}
        except Exception as exc:
            _log(f"  [warn] could not fetch {game_slug} for kickoff-window check: {exc}")
            continue
        if event.get("live"):
            continue
        start = event.get("startDate")
        if not start:
            continue
        kickoff_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if abs((now - kickoff_dt).total_seconds()) <= KICKOFF_WINDOW_SECONDS:
            active[game_slug] = (players, event)
    return active


def _reaction_time_targets(client: GatewayClient, conn: sqlite3.Connection) -> dict[str, tuple[list[str], dict]]:
    """Return {game_slug: (players, event)} for every tracked game the API
    itself reports as currently live -- not inferred from time-since-kickoff,
    which can't account for stoppage time, halftime, or extra time."""
    active = {}
    for game_slug, players in _get_tracked_positions(conn).items():
        try:
            event = client.get_event_by_slug(game_slug).get("event") or {}
        except Exception as exc:
            _log(f"  [warn] could not fetch {game_slug} for reaction-time check: {exc}")
            continue
        if event.get("live") and not event.get("closed"):
            active[game_slug] = (players, event)
    return active


def _run_high_frequency_loop(
    client: GatewayClient,
    conn: sqlite3.Connection,
    active_games: dict[str, tuple[list[str], dict]],
    types: tuple[str, ...],
    table: str,
    poll_interval: float,
    run_seconds: float,
    context: str,
) -> int:
    """Shared tick loop for kickoff_window_snapshots and
    reaction_time_snapshots -- same mechanics (poll every `poll_interval`
    for up to `run_seconds`, one row per player/market_type per tick), just
    different targets/types/table/cadence."""
    if not active_games:
        return 0

    rows_logged = 0
    deadline = time.monotonic() + run_seconds
    while True:
        tick_start = time.monotonic()
        tick_at = datetime.now(timezone.utc).isoformat()
        for game_slug, (players, event) in active_games.items():
            for market in event.get("markets") or []:
                smt = market.get("sportsMarketType")
                if smt not in TYPE_LABEL or TYPE_LABEL[smt] not in types or market.get("line") != 1:
                    continue
                match = QUESTION_RE.match(market.get("question") or "")
                if not match or match.group(1) not in players:
                    continue
                slug = market.get("slug")
                if not slug:
                    continue
                try:
                    book = client.get_market_book(slug).get("marketData") or {}
                except Exception as exc:
                    _log(f"  [warn] book fetch failed for {slug} ({context}): {exc}")
                    continue
                bids, offers = book.get("bids") or [], book.get("offers") or []
                bid0 = bids[0] if bids else {}
                ask0 = offers[0] if offers else {}
                conn.execute(
                    f"""
                    INSERT INTO {table}
                        (fetched_at, game_slug, player, market_type, bid, ask, bid_size, ask_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tick_at, game_slug, match.group(1), TYPE_LABEL[smt],
                     (bid0.get("px") or {}).get("value"), (ask0.get("px") or {}).get("value"),
                     bid0.get("qty"), ask0.get("qty")),
                )
                rows_logged += 1
        conn.commit()

        if deadline - time.monotonic() <= 0:
            break
        # Sleep only the remainder of the interval after accounting for how
        # long the fetch itself took, so the tick cadence targets the real
        # poll_interval instead of poll_interval-plus-fetch-time. If the
        # fetch alone took longer than poll_interval, proceed immediately.
        sleep_for = poll_interval - (time.monotonic() - tick_start)
        if sleep_for > 0:
            time.sleep(sleep_for)

    return rows_logged


def _log_kickoff_window_snapshots(client: GatewayClient, conn: sqlite3.Connection) -> int:
    """If any watched game is within +/- 5 minutes of kickoff and not yet
    live, poll its goals and goals+assists 1+ markets every 10 seconds for
    up to KICKOFF_WINDOW_RUN_SECONDS, logging into kickoff_window_snapshots
    (kept separate from position_price_history -- this is investigating a
    specific unexplained price move, not routine position monitoring).
    No-op (and fast) outside any game's kickoff window."""
    active_games = _kickoff_window_targets(client, conn, datetime.now(timezone.utc))
    return _run_high_frequency_loop(
        client, conn, active_games, KICKOFF_WINDOW_TYPES, "kickoff_window_snapshots",
        KICKOFF_WINDOW_POLL_INTERVAL, KICKOFF_WINDOW_RUN_SECONDS, "kickoff window",
    )


def _log_reaction_time_snapshots(client: GatewayClient, conn: sqlite3.Connection) -> int:
    """If any watched game is currently live, poll its goals and
    goals+assists 1+ markets every 1 second for up to
    REACTION_TIME_RUN_SECONDS, logging into reaction_time_snapshots.
    Reaction-time measurement only -- no trading logic, no automation reads
    this table. No-op (and fast) when no watched game is live."""
    active_games = _reaction_time_targets(client, conn)
    return _run_high_frequency_loop(
        client, conn, active_games, REACTION_TIME_TYPES, "reaction_time_snapshots",
        REACTION_TIME_POLL_INTERVAL, REACTION_TIME_RUN_SECONDS, "reaction time",
    )


def _log_trade_history(trading_client: TradingClient | None, conn: sqlite3.Connection) -> int:
    """Log every real trade from the account's activity feed, unconditionally,
    every run -- trade capture has no dependency on tracking status. Tracking
    scope (_get_tracked_positions) governs what gets dense price/order-book
    polling; it must never gate which real trades get recorded, since those
    two concerns are unrelated. Read-only: only ever calls
    GET /v1/portfolio/activities."""
    if trading_client is None:
        return 0

    logged_at = datetime.now(timezone.utc).isoformat()
    inserted = 0
    cursor = None
    while True:
        params = {"limit": 100, "types": ["ACTIVITY_TYPE_TRADE"]}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = trading_client.get_activities(**params)
        except Exception as exc:
            _log(f"  [warn] get_activities failed: {exc}")
            break

        for activity in resp.get("activities") or []:
            trade = activity.get("trade") or {}
            market_obj = trade.get("market") or {}
            # Prefer the question-text-derived name (same QUESTION_RE used by
            # clean_price_triples/position_price_history/etc.) over
            # metadata.playerName -- Polymarket's own data is inconsistent
            # between the two (e.g. question text "Lautaro Martinez" vs
            # metadata.playerName "Lautaro Martinez" with accents), and using
            # two different conventions across tables breaks joining a
            # player's price data against their trade data. Falls back to
            # metadata.playerName for non-prop trades (team winner, futures)
            # where the question doesn't match this pattern at all.
            question_match = QUESTION_RE.match(market_obj.get("question") or "")
            player = question_match.group(1) if question_match else (market_obj.get("metadata") or {}).get("playerName")
            smt = (trade.get("market") or {}).get("sportsMarketType")
            is_aggressor = trade.get("isAggressor")
            mine = trade.get("aggressor") if is_aggressor else trade.get("passive")
            # trade.qty is a rounded display value (e.g. "21" for a true fill
            # of 20.54) -- lastShares on the matching *Execution object is the
            # exact fill quantity for this specific trade.
            mine_execution = trade.get("aggressorExecution") if is_aggressor else trade.get("passiveExecution")
            qty = (mine_execution or {}).get("lastShares")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO trade_history
                    (trade_id, logged_at, trade_time, game_slug, market_slug, player,
                     market_type, tier, side, is_aggressor, price, qty, cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trade.get("id"), logged_at, trade.get("createTime"),
                 ((mine or {}).get("marketMetadata") or {}).get("eventSlug"),
                 trade.get("marketSlug"), player, TYPE_LABEL.get(smt, smt),
                 (trade.get("market") or {}).get("line"), (mine or {}).get("side"),
                 int(bool(is_aggressor)), (trade.get("price") or {}).get("value"),
                 qty, (trade.get("cost") or {}).get("value")),
            )
            inserted += cur.rowcount

        cursor = resp.get("nextCursor")
        if resp.get("eof", True) or not cursor:
            break

    conn.commit()
    return inserted


def _recompute_position_pnl(conn: sqlite3.Connection) -> tuple[int, int]:
    """Rebuild open_positions and closed_trades_pnl from trade_history using
    average-cost matching: the running average buy price only moves on BUY
    fills, and a SELL realizes P&L against whatever that running average was
    at the moment of the sale, for just the shares sold. Pure local
    recomputation over already-logged trades -- no API calls, safe to run
    every collector invocation.

    Grouped by (player, market_type, game_slug, tier) -- tier is part of the
    real identity of a position: "1+ goals" and "2+ goals" are independently
    priced instruments, not the same fungible position, even for the same
    player/type/game. Full wipe-and-rebuild each call (not upsert) since
    changing the grouping key can move a row's identity entirely (e.g. a
    trade that used to be pooled into one group now belongs to a different
    one) -- upserting on the old key would leave orphaned rows behind, same
    failure mode as the player-name normalization did. No user-entered
    strategy_tag exists yet in either table (checked before enabling this),
    so wiping is safe; if that stops being true this needs to go back to
    upserting."""
    conn.execute("DELETE FROM open_positions")
    conn.execute("DELETE FROM closed_trades_pnl")

    rows = conn.execute(
        """
        SELECT trade_id, player, market_type, game_slug, tier, side, price, qty, trade_time
        FROM trade_history
        WHERE player IS NOT NULL AND market_type IS NOT NULL AND game_slug IS NOT NULL
              AND tier IS NOT NULL AND side IS NOT NULL AND price IS NOT NULL AND qty IS NOT NULL
        ORDER BY player, market_type, game_slug, tier, trade_time
        """
    ).fetchall()

    open_count, closed_count = 0, 0
    key = None
    total_buy_qty = total_buy_cost = remaining_qty = 0.0

    def flush(key):
        nonlocal open_count
        if key is None or remaining_qty <= 1e-9:
            return
        player, market_type, game_slug, tier = key
        avg_cost = total_buy_cost / total_buy_qty
        conn.execute(
            """
            INSERT INTO open_positions
                (player, market_type, game_slug, tier, remaining_size, avg_cost_basis, remaining_cost_basis)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (player, market_type, game_slug, tier, remaining_qty, avg_cost, remaining_qty * avg_cost),
        )
        open_count += 1

    for trade_id, player, market_type, game_slug, tier, side, price, qty, trade_time in rows:
        row_key = (player, market_type, game_slug, tier)
        if row_key != key:
            flush(key)
            key = row_key
            total_buy_qty = total_buy_cost = remaining_qty = 0.0

        if side == "ORDER_SIDE_BUY":
            total_buy_qty += qty
            total_buy_cost += qty * price
            remaining_qty += qty
        elif side == "ORDER_SIDE_SELL":
            if total_buy_qty <= 0:
                _log(f"  [warn] SELL with no prior BUY history for {player}/{market_type}/{game_slug}"
                     f"@{tier} (trade {trade_id}) -- skipping P&L calc for this fill")
                continue
            avg_buy_price = total_buy_cost / total_buy_qty
            dollar_pnl = (price - avg_buy_price) * qty
            percent_pnl = (price - avg_buy_price) / avg_buy_price * 100
            conn.execute(
                """
                INSERT INTO closed_trades_pnl
                    (sell_trade_id, player, market_type, game_slug, tier, shares_closed,
                     avg_buy_price, sell_price, dollar_pnl, percent_pnl, sell_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trade_id, player, market_type, game_slug, tier, qty, avg_buy_price, price,
                 dollar_pnl, percent_pnl, trade_time),
            )
            closed_count += 1
            remaining_qty -= qty

    flush(key)
    conn.commit()
    return open_count, closed_count


def _run_daily_game_discovery(client: GatewayClient, conn: sqlite3.Connection, force: bool = False) -> int:
    """Informational only -- purely a visibility log, doesn't feed into
    tracking itself. Once per UTC calendar day (or immediately if
    force=True), scans every confirmed World Cup game via the same broad,
    unrestricted find_world_cup_events() call clean_price_triples already
    uses, and logs any game_slug not seen before into discovered_games along
    with whichever players currently have prop markets on it. Note: actual
    dense tracking (position_price_history, order_book_snapshots,
    kickoff/reaction-time layers) is driven separately, by whatever
    clean_price_triples currently contains (_get_tracked_positions) -- this
    function does not add or remove anything from that."""
    today = datetime.now(timezone.utc).date().isoformat()
    if not force:
        row = conn.execute("SELECT last_run_date FROM daily_discovery_state WHERE id = 1").fetchone()
        if row and row[0] == today:
            return 0

    try:
        events = find_world_cup_events(client)
    except Exception as exc:
        _log(f"  [warn] daily game discovery failed: {exc}")
        return 0

    new_count = 0
    for event in events:
        game_slug = event.get("slug")
        if not game_slug:
            continue
        players = set()
        for market in event.get("markets") or []:
            if market.get("sportsMarketType") not in PROP_SPORTS_MARKET_TYPES:
                continue
            match = QUESTION_RE.match(market.get("question") or "")
            if match:
                players.add(match.group(1))
        players_str = ", ".join(sorted(players))

        if conn.execute("SELECT 1 FROM discovered_games WHERE game_slug = ?", (game_slug,)).fetchone() is None:
            conn.execute(
                "INSERT INTO discovered_games (game_slug, title, start_date, players, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (game_slug, event.get("title"), event.get("startDate"), players_str,
                 datetime.now(timezone.utc).isoformat()),
            )
            new_count += 1
            _log(f"  new game discovered: {event.get('title')} ({game_slug}), "
                 f"start {event.get('startDate')}, players: {players_str or '(none yet)'}")
        else:
            conn.execute(
                "UPDATE discovered_games SET players = ?, title = ?, start_date = ? WHERE game_slug = ?",
                (players_str, event.get("title"), event.get("startDate"), game_slug),
            )

    conn.execute(
        "INSERT INTO daily_discovery_state (id, last_run_date) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_run_date = excluded.last_run_date",
        (today,),
    )
    conn.commit()
    return new_count


def _connect_with_retry(max_attempts: int = 3, backoff_seconds: float = 3.0) -> sqlite3.Connection | None:
    for attempt in range(max_attempts):
        try:
            return _connect()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == max_attempts - 1:
                raise
            _log(f"[warn] cache DB locked (attempt {attempt + 1}/{max_attempts}), retrying: {exc}")
            time.sleep(backoff_seconds)
    return None


def main() -> None:
    client = GatewayClient()
    trading_client = (
        TradingClient(POLYMARKET_US_KEY_ID, POLYMARKET_US_SECRET_KEY)
        if POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY
        else None
    )
    try:
        conn = _connect_with_retry()
    except sqlite3.OperationalError as exc:
        _log(f"[error] cache DB still locked after retries, skipping this run: {exc}")
        return
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()

    new_games = _run_daily_game_discovery(client, conn)
    if new_games:
        _log(f"daily discovery -- {new_games} new game(s) found (see discovered_games table)")

    try:
        events = [e for e in find_world_cup_events(client) if _not_started(e)]
    except Exception as exc:
        _log(f"[error] could not reach Polymarket US API: {exc}")
        return

    due_events = []
    for event in events:
        start_dt = datetime.fromisoformat(event["startDate"].replace("Z", "+00:00"))
        time_to_kickoff = (start_dt - now).total_seconds()
        interval = _poll_interval_seconds(time_to_kickoff)
        if _due_for_poll(conn, event["slug"], interval, now):
            due_events.append(event)

    _log(f"checking {len(events)} not-yet-started confirmed game(s), "
         f"{len(due_events)} due for polling this run")
    inserted = 0
    for event in due_events:
        triples = _collect_triples(client, event)
        _mark_checked(conn, event["slug"], now)
        for t in triples:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clean_price_triples
                    (fetched_at, game_slug, player, goals_last, assists_last, ga_last, formula, gap)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fetched_at, event["slug"], t["player"], t["goals_last"], t["assists_last"],
                 t["ga_last"], t["formula"], t["gap"]),
            )
            inserted += cur.rowcount
        conn.commit()
        if triples:
            _log(f"  {event['slug']}: {len(triples)} clean triple(s) found")

    total = conn.execute("SELECT COUNT(*) FROM clean_price_triples").fetchone()[0]

    price_rows, book_rows = _log_watched_positions(client, conn, fetched_at)
    position_total = conn.execute("SELECT COUNT(*) FROM position_price_history").fetchone()[0]
    book_total = conn.execute("SELECT COUNT(*) FROM order_book_snapshots").fetchone()[0]

    trade_rows = _log_trade_history(trading_client, conn)
    trade_total = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]

    open_count, closed_count = _recompute_position_pnl(conn)

    kickoff_rows = _log_kickoff_window_snapshots(client, conn)
    kickoff_total = conn.execute("SELECT COUNT(*) FROM kickoff_window_snapshots").fetchone()[0]

    reaction_rows = _log_reaction_time_snapshots(client, conn)
    reaction_total = conn.execute("SELECT COUNT(*) FROM reaction_time_snapshots").fetchone()[0]

    conn.close()
    _log(f"done -- {inserted} new row(s) inserted this run, {total} total in dataset")
    _log(f"positions -- {price_rows} row(s) logged this run, {position_total} total in position_price_history")
    _log(f"order book -- {book_rows} row(s) logged this run, {book_total} total in order_book_snapshots")
    _log(f"trades -- {trade_rows} new row(s) inserted this run, {trade_total} total in trade_history")
    _log(f"pnl -- {open_count} open position(s), {closed_count} closed-trade record(s)")
    if kickoff_rows:
        _log(f"kickoff window -- {kickoff_rows} row(s) logged this run, {kickoff_total} total in kickoff_window_snapshots")
    if reaction_rows:
        _log(f"reaction time -- {reaction_rows} row(s) logged this run, {reaction_total} total in reaction_time_snapshots")


if __name__ == "__main__":
    main()
