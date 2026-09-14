# Database Architecture

*What's in `cache/polymarket_public.db`, and where it came from.*

This covers exactly the twelve tables `cache/build_public_export.py` ships —
nothing else. Everything here is scoped to the 2026 World Cup (June–July);
no other tournament, league, or venue appears anywhere in this repository.

## Where each table's data actually lives day to day

Two source databases feed the export, only one of which is in this repo:

- **`cache/polymarket.db`** (this repo, created by `collect_clean_triples.py`)
  — the live tracking DB. Account-ledger tables live here permanently.
- **`cache/historical.db`** (private, not in this repo) — where World Cup
  tracking tables were moved once the tournament concluded, by a private
  archive script. Same schema, same rows, different file — nothing about the
  paper's numbers depends on that move.

## Account ledger (from `cache/polymarket.db`, date-windowed to 2026-06-15 – 2026-07-19)

Real fills, real settlements, real cash movement — filtered to the paper's
analysis window because the account kept trading afterward under separate,
still-private strategies that have nothing to do with this paper.

| table | columns | what it holds |
|---|---|---|
| `trade_history` | `trade_id`, `trade_time`, `game_slug`, `market_slug`, `player`, `market_type`, `tier`, `side`, `is_aggressor`, `price`, `qty`, `cost`, `strategy` | every real fill, deduped on `trade_id` |
| `closed_trades_pnl` | `sell_trade_id`, `player`, `market_type`, `game_slug`, `tier`, `shares_closed`, `avg_buy_price`, `sell_price`, `dollar_pnl`, `percent_pnl`, `sell_time` | realized P&L per closing sell, derived from `trade_history` |
| `settlement_history` | `resolution_key`, `resolved_at`, `market_slug`, `game_slug`, `player`, `market_type`, `tier`, `resolution_side`, `realized_pnl` | real market resolutions (a position held to settlement rather than sold) |
| `cash_activity` | `transaction_id`, `activity_type`, `status`, `amount`, `currency`, `create_time`, `description` | deposits / withdrawals / transfers on the account |
| `open_positions` | `player`, `market_type`, `game_slug`, `tier`, `remaining_size`, `avg_cost_basis`, `remaining_cost_basis` | fully derived from `trade_history` — not date-windowed (exported as the live table's current state), and empty in this export |

`tier` matters because "1+ goals" and "2+ goals" are separately priced
tokens on the same player/market — an early version of this schema grouped
by `(player, market_type, game_slug)` alone and silently pooled different
tiers together; fixed before this export was built.

## World Cup tracking (from `cache/historical.db`, no window — the whole archived tournament)

| table | columns | what it holds |
|---|---|---|
| `clean_price_triples` | `fetched_at`, `game_slug`, `player`, `goals_last`, `assists_last`, `ga_last`, `formula`, `gap` | the strategy signal: one player's goals / assists / goals+assists last-traded prices, all read from the same pre-kickoff market state |
| `kickoff_window_snapshots` | `fetched_at`, `game_slug`, `player`, `market_type`, `bid`, `ask`, `bid_size`, `ask_size` | the dense ±5-minute, 10-second-cadence window around kickoff, kept separate from routine tracking |
| `discovered_games` | `game_slug`, `title`, `start_date`, `players`, `first_seen_at` | every World Cup game the collector ever found, via the tournament's own series ID |
| `game_poll_state` | `game_slug`, `last_checked_at` | per-game last-polled timestamp — gates the tiered polling interval |
| `dropped_markets` | `market_slug`, `player`, `market_type`, `game_slug`, `dropped_at` | markets the collector stopped tracking (e.g. a player prop that disappeared from the book) |
| `daily_discovery_state` | `last_run_date` | a single-row lock so the once-daily discovery pass doesn't re-run within the same day |
| `fanduel_comparison` | `fetched_at`, `player`, `game_slug`, `market_type`, `settlement_window`, `fanduel_odds_american`, `fanduel_raw_implied_prob`, `polymarket_price`, `diff`, `source_url`, `notes` | manual, one-off FanDuel-vs-Polymarket price comparisons (`record_fanduel_comparison.py`) — 2 rows, not a scheduled process |

## What's deliberately excluded

`position_price_history`, `order_book_snapshots`, and
`reaction_time_snapshots` — the dense, routine-cadence and 1-second-live
tick data — are 2.2M+ rows / ~420MB combined between them. Too large to
publish directly; every conclusion they support is already reported in the
paper's own tables and charts.

## Row counts in this export (as shipped)

| table | rows |
|---|---|
| `discovered_games` | 3,050 |
| `kickoff_window_snapshots` | 7,456 |
| `trade_history` | 879 |
| `dropped_markets` | 624 |
| `closed_trades_pnl` | 209 |
| `clean_price_triples` | 208 |
| `settlement_history` | 178 |
| `game_poll_state` | 25 |
| `cash_activity` | 8 |
| `fanduel_comparison` | 2 |
| `open_positions` | 0 |
| `daily_discovery_state` | 1 |
