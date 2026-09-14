# Polymarket US — 2026 World Cup Player-Prop Research

A data-collection system and real-money trade ledger built for the 2026 FIFA
World Cup (June–July), tracking Polymarket US player-prop markets — goals,
assists, goals+assists — for a single tournament, plus the paper analyzing
the results.

**This repository is the complete World Cup slice**: the collector, the
account ledger it produced, and the paper. Nothing here places, modifies, or
cancels a real order — every authenticated call in this repo is read-only by
design (see `TradingClient` in `data/client.py`). Any trading done during the
tournament was placed by hand; this code observed and logged it.

### What's actually in this repository

- `main.py` / `data/markets.py` — the original, simple fetch: pull World Cup
  events, find their goals/assists/goals+assists markets, snapshot current
  price. `data/cache.py` appends each pull to `cache/polymarket.db`
  (`prop_snapshots`).
- `collect_clean_triples.py` — the unattended version of the same idea, built
  to run on a timer: a "clean triple" is one player's goals / assists /
  goals+assists last-traded prices, all read from the same pre-kickoff market
  state so the three numbers are comparable. Also tracks bid/ask depth, the
  account's real trade history, and derived P&L, all scoped to World Cup
  games only (`find_world_cup_events`, one tournament, one series ID — no
  other league or venue is touched anywhere in this repo).
- `record_fanduel_comparison.py` — a manual, one-off-per-invocation script:
  look up a player's FanDuel odds by hand, log the comparison against
  Polymarket's current price. Not a scheduled job — FanDuel's research pages
  have no reliable per-player URL or refresh signal to poll.
- `cache/build_public_export.py` and `cache/polymarket_public.db` — the
  export builder and its output: every table from the tournament small enough
  to publish, so the paper's numbers can be checked against the real data
  behind them.
- [`docs/DATABASE_ARCHITECTURE.md`](docs/DATABASE_ARCHITECTURE.md) — what's
  in that export, table by table.
- [`docs/polymarket-worldcup-paper.pdf`](docs/polymarket-worldcup-paper.pdf)
  (+ `docs/paper/` source) — the paper.

---

## What the collector did

| | |
|---|---|
| **Discovered** | World Cup goal/assist/goals+assists player-prop markets on Polymarket US, matched to confirmed games via the tournament's own `activeSeriesId` rather than a hardcoded game list — newly confirmed matchups appeared automatically as the bracket progressed (`data/markets.py::find_world_cup_events`). |
| **Collected** | tiered by time-to-kickoff — every 20 min more than 6h out, down to every 1 min inside the final 30 minutes (`POLL_TIERS`) — plus a separate ±5-minute, 10-second-cadence window right at kickoff, and a 1-second reaction-time layer once a game went live, kept in its own table so it never mixes with routine tracking. |
| **Logged** | the account's real fills, closed-trade P&L, market settlements, and cash activity from actual trading during the tournament — `trade_history`, `closed_trades_pnl`, `settlement_history`, `cash_activity`. |
| **Published** | a filtered export (`cache/build_public_export.py`) covering exactly the paper's analysis window, 2026-06-15 to 2026-07-19. |

---

## Architecture

```
Polymarket Gateway API (public, no auth) ─┐
Polymarket Trading API (GET-only)         ─┴─▶ collect_clean_triples.py / main.py
                                                          │
                                                          ▼
                                              cache/polymarket.db (live, this tournament)
                                                          │  (tournament concluded)
                                                          ▼
                                              cache/historical.db (archived, private — not in this repo)
                                                          │
                                                          ▼
                                        cache/build_public_export.py ──▶ cache/polymarket_public.db (published)
```

`cache/historical.db` and the script that moves data into it aren't part of
this repository — `build_public_export.py` (which is) reads from wherever
each table currently lives and says so in its own comments. Nothing about
the paper's numbers depends on that script; the archived rows are the same
rows, same schema, just moved out of the live file once the tournament ended.

## Notes on the code itself

- **Rate-limit handling.** A 429 from Polymarket triggers an exponential
  backoff retry (`data/client.py`, both the public `GatewayClient` and the
  read-only `TradingClient`) rather than hammering the API or crashing the
  run.
- **GET-only by design.** `TradingClient` exposes exactly three read
  endpoints — positions, activity history, balances. There is no order
  creation, modification, cancellation, or close-position method anywhere in
  this repository; that's an explicit boundary, not something left out by
  accident.
- **The published export leaves three tables out on purpose.**
  `position_price_history`, `order_book_snapshots`, and
  `reaction_time_snapshots` are 2.2M+ rows / ~420MB combined — too large to
  publish directly. Everything they support is already aggregated into the
  paper's own tables and figures. The four account-ledger tables
  (`trade_history`, `closed_trades_pnl`, `settlement_history`,
  `cash_activity`) are date-windowed to the paper's own analysis period, since
  the account kept trading afterward under separate, still-private strategies
  that have nothing to do with this paper.

---

## Setup

```bash
python -m venv venv && venv\Scripts\activate      # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                               # then fill in your own keys
```

`main.py` needs no credentials — Polymarket US market and event data is
public and unauthenticated. The account-ledger scripts need
`POLYMARKET_US_KEY_ID` / `POLYMARKET_US_SECRET_KEY` (used only for the
GET-only `TradingClient` calls in `config.py`).

## Run

```bash
python main.py                      # fetch current World Cup player-prop markets + prices, cache to SQLite
python main.py --raw                # print a raw event/market JSON sample (schema discovery), no caching
python collect_clean_triples.py     # the unattended collector — run on a 1-min timer
```

## Stack

Python 3.13 · `requests` · `pynacl` (Ed25519 request signing) · SQLite · no
heavyweight dependencies.

## Layout (this repo)

```
config.py                             .env-based config, base URLs, World Cup discovery filter
main.py                               World Cup player-prop fetch + ad-hoc schema discovery (--raw)
collect_clean_triples.py              the unattended collector, tiered polling + account ledger
data/client.py                        Polymarket US — public gateway client + GET-only TradingClient
data/markets.py                       World Cup event discovery + player-prop market filtering
data/cache.py                         append-only SQLite snapshot cache (cache/polymarket.db)
record_fanduel_comparison.py          on-demand: log one FanDuel-vs-Polymarket price comparison by hand
cache/build_public_export.py          builds the published export from the live + archived DBs
cache/polymarket_public.db            the published export
docs/DATABASE_ARCHITECTURE.md         what's in the export, table by table
docs/polymarket-worldcup-paper.pdf    the paper
```

`signals/` and `execution/` are empty namespace placeholders in this
repository — no code lives there.

## License

MIT — see [LICENSE](LICENSE).
