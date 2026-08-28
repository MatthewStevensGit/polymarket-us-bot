"""
On-demand recorder for a single FanDuel vs. Polymarket price comparison.

This is NOT a scheduled scraper. FanDuel's research articles (the only
source found so far with parseable, server-rendered odds) have no
predictable per-player/game URL and no confirmed live-refresh signal --
polling them on a timer would very plausibly just re-read the same stale
snapshot every run. Instead: do the research (web search + fetch) for a
player/game by hand, then invoke this script once with what was found. It
computes the raw implied probability from the American odds, looks up the
matching current Polymarket price from position_price_history, and logs a
timestamped row to cache/polymarket.db (fanduel_comparison).

IMPORTANT: "fanduel_raw_implied_prob" is exactly that -- raw implied
probability from a single-sided price. FanDuel does not publish a "No" side
for these player props, so proper sum-to-1 de-vigging is not computable from
this source; the raw number is known to overstate true probability by the
book's margin. Do not treat this as a de-vigged fair value.

Usage:
    python record_fanduel_comparison.py \\
        --player "Harry Kane" \\
        --game-slug fwc-eng-cod-2026-07-01 \\
        --odds -180 \\
        --window 90min_stoppage \\
        --source-url "https://www.fanduel.com/research/..." \\
        [--market-type goals+assists] [--notes "..."]

    --window must be one of: 90min_stoppage, incl_et
    (Polymarket's goals+assists 1+ contracts settle on 90min_stoppage --
    confirmed from live market description text, "Goals or assists during
    extra time or penalty shootouts do not count." Recording the window
    explicitly on every row avoids ever silently comparing mismatched
    settlement terms.)
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect_clean_triples import _connect  # reuses the same schema/connection setup

VALID_WINDOWS = ("90min_stoppage", "incl_et")


def american_odds_to_implied_prob(odds: float) -> float:
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--player", required=True)
    parser.add_argument("--game-slug", required=True)
    parser.add_argument("--odds", required=True, type=float, help="American odds, e.g. -180 or +135")
    parser.add_argument("--window", required=True, choices=VALID_WINDOWS)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--market-type", default="goals+assists")
    parser.add_argument("--notes", default=None)
    args = parser.parse_args()

    if args.window != "90min_stoppage":
        print(
            f"[warn] Polymarket's {args.market_type} 1+ contracts settle on 90min_stoppage. "
            f"Recording a comparison against '{args.window}' will NOT be an apples-to-apples "
            f"settlement match -- proceeding anyway since you explicitly specified it."
        )

    implied_prob = american_odds_to_implied_prob(args.odds)
    fetched_at = datetime.now(timezone.utc).isoformat()

    conn = _connect()
    row = conn.execute(
        """
        SELECT last_trade_px, fetched_at FROM position_price_history
        WHERE player = ? AND game_slug = ? AND market_type = ?
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (args.player, args.game_slug, args.market_type),
    ).fetchone()
    polymarket_price, polymarket_quote_time = row if row else (None, None)
    diff = (polymarket_price - implied_prob) if polymarket_price is not None else None

    notes = args.notes or "raw implied probability, NOT de-vigged -- FanDuel publishes no 'No' side for this prop"

    conn.execute(
        """
        INSERT INTO fanduel_comparison
            (fetched_at, player, game_slug, market_type, settlement_window,
             fanduel_odds_american, fanduel_raw_implied_prob, polymarket_price,
             polymarket_quote_time, diff, source_url, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (fetched_at, args.player, args.game_slug, args.market_type, args.window,
         args.odds, implied_prob, polymarket_price, polymarket_quote_time, diff,
         args.source_url, notes),
    )
    conn.commit()

    print(f"logged: {args.player} / {args.market_type} / {args.game_slug}")
    print(f"  FanDuel {args.odds:+.0f} -> raw implied prob {implied_prob:.4f}")
    if polymarket_price is not None:
        print(f"  Polymarket last-trade {polymarket_price:.4f} (as of {polymarket_quote_time}) -> diff {diff:+.4f}")
    else:
        print("  [warn] no matching Polymarket price found in position_price_history -- diff left null")
    conn.close()


if __name__ == "__main__":
    main()
