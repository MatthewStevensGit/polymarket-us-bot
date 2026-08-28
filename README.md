# Polymarket US — World Cup Player Prop Bot

Pulls World Cup player prop markets (goals, assists, goals+assists) from the
Polymarket US public API and caches current pricing locally. Read-only —
no order placement.

## Setup

```bash
python -m venv venv
venv\Scripts\activate      # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
```

Create `.env` in the project root (see `.env.example`):
```
POLYMARKET_US_KEY_ID=your_key_id
POLYMARKET_US_SECRET_KEY=your_secret_key
```

Credentials aren't used by `main.py` (market/event data is public,
unauthenticated) but are used by `collect_clean_triples.py`'s `TradingClient`
for read-only portfolio lookups (see below).

## Run

```bash
python main.py            # fetch player prop markets + prices, cache to SQLite
python main.py --csv      # also export the full cache history to cache/prop_snapshots.csv
python main.py --raw      # print a raw sample event/market JSON, for schema discovery
```

World Cup events are discovered via the league's active series, not a
category filter: `GET /v2/leagues/fwc` gives `activeSeriesId`, then
`GET /v1/events?seriesId=...` (paginated) returns every event tagged to that
series — past, live, and upcoming games plus tournament-wide futures. This
picks up newly confirmed matchups automatically as the tournament progresses.
Player prop markets are matched by exact `sportsMarketType` value
(`soccer_player_goals`, `soccer_player_assists`,
`soccer_player_goals_plus_assists` — see `PROP_SPORTS_MARKET_TYPES` in
`config.py`), confirmed against live event data.

## Structure

```
config.py          .env-based config, base URLs, discovery filters
data/client.py      thin wrapper over the public gateway.polymarket.us API
data/markets.py      event/market discovery + player-prop filtering
data/cache.py         SQLite cache (cache/polymarket.db), append-only snapshots
signals/              (empty) probability/signal logic — future phase
execution/            (empty) order placement — future phase
main.py               CLI entry point
```

## API notes

- Public market data (events, markets, order book, BBO) lives at
  `https://gateway.polymarket.us` and needs no authentication.
- `/v1/events`'s `categories` param filters on a coarse content vertical —
  every sport reports `category: "sports"`, so `categories=soccer` silently
  matches nothing. Per-sport/league filtering lives under `/v2/sports` and
  `/v2/leagues/{slug}` instead; see `find_world_cup_events` in
  `data/markets.py`.
- Trading/account data lives at `https://api.polymarket.us` and needs the
  `X-PM-Access-Key` / `X-PM-Timestamp` / `X-PM-Signature` headers, Ed25519-signed
  over `{timestamp}{method}{path}` (query params and body are NOT signed).
  Implemented in `TradingClient` (`data/client.py`) — but **GET-only by
  design**: `get_positions` (`/v1/portfolio/positions`) and `get_activities`
  (`/v1/portfolio/activities`, real executed-trade history). No order
  creation/modification/cancellation/close-position methods exist anywhere
  in this class or file — that's an explicit execution-phase boundary, not
  an oversight.
- The report endpoints under `api.prod.polymarketexchange.com`
  (`search-trades`, `get-trade-stats`, `download-trades-csv`) use a
  *different* auth scheme entirely (Private Key JWT via Auth0, RSA PEM key
  from a separate onboarding flow) — confirmed by live testing (401 even
  with the docs claiming no auth needed). They're not reachable with the
  HMAC credentials above; `TradingClient` targets `api.polymarket.us`
  instead, which the existing `.env` credentials actually match.
- There's no public historical-price endpoint — only current book/BBO
  snapshots. `data/cache.py` appends a timestamped row per run so repeated
  pulls build a local price history over time.
- One doc page (`order-book/get-order-book.md`,
  `order-book/get-best-bidoffer.md`) references a different base URL
  (`api.prod.polymarketexchange.com`) and a `BTC-USD`-style symbol param —
  that looks like stale/unrelated content in the docs, not the prediction
  market order book. Use `markets/get-market-bbo` and `markets/get-market-book`
  instead (`gateway.polymarket.us/v1/markets/{slug}/bbo` and `.../book`),
  which match the rest of the market data model.

## collect_clean_triples.py

Standalone, unattended collector (no AI/Claude dependency) meant to run on a
timer, e.g. Windows Task Scheduler every 1 minute:

```bash
python collect_clean_triples.py
```

Writes to `cache/polymarket.db`:
- `clean_price_triples` — deduped research sample of goals/assists/goals+assists
  last-trade triples from confirmed pre-kickoff games (tiered polling by
  time-to-kickoff; state tracked in `game_poll_state`).
- `position_price_history` — dense, un-deduped bid/ask/last timeline, logged
  every single run regardless of tier. Tracked players/games are derived
  live from whatever `clean_price_triples` currently contains
  (`_get_tracked_positions`) -- a player only needs real
  goals/assists/goals+assists price data to get this denser tracking, not a
  confirmed held position. Tracks all three market types per player:
  `goals`, `assists`, and `goals+assists` (`WATCHED_TYPES`).
- `order_book_snapshots` — top `ORDER_BOOK_DEPTH` bid/offer levels (price +
  size, JSON) for the same watchlist, same cadence. **Caveat:** a price level
  disappearing between two snapshots could mean either a fill or a plain
  cancellation — this data alone can't distinguish the two. Doing so would
  require matching that level's price against a `last_trade_px` change in
  `position_price_history` at the same timestamp.
- `trade_history` — real executed trades for the watchlist, from
  `TradingClient.get_activities`, deduped on `trade_id`.
- `kickoff_window_snapshots` — high-frequency layer on top of the tiers
  above, active only within +/- `KICKOFF_WINDOW_SECONDS` (5 min) of a
  watched game's kickoff: polls `goals`/`goals+assists` 1+ markets every
  `KICKOFF_WINDOW_POLL_INTERVAL` (10s) via `get_market_book`, for up to
  `KICKOFF_WINDOW_RUN_SECONDS` (45s) per collector run. Kept in its own
  table rather than mixed into `position_price_history` — this is for
  investigating a specific unexplained price move, not routine monitoring.
  A no-op (fast, single API call per game) when no game is near kickoff.

### Per-game views

`_ensure_game_views` (called every time `_connect()` runs) auto-creates two
SQL views per currently-tracked game (per `_get_tracked_positions`), named
`view_<table>_<3-letter>_<3-letter>` (e.g. `view_position_price_history_eng_cod`,
`view_order_book_snapshots_bel_sen`). A new game gets its views on the next
collector run automatically — no manual SQL needed. These are true SQL
`VIEW` objects (confirmed via `sqlite_master.type`), not copied tables: each
is just a saved `SELECT`, re-run against the live base table on every read,
so it cannot hold stale data.

Both are pivoted to one row per `(fetched_at, player)`, columns per market
type instead of one row per `(fetched_at, player, market_type)`:
- `view_position_price_history_*`: `goals_bid`, `goals_ask`, `assists_bid`,
  `assists_ask`, `ga_bid`, `ga_ask`.
- `view_order_book_snapshots_*`: top-of-book only (index `[0]` of each side's
  JSON array) — `goals_bid_px`, `goals_bid_sz`, `goals_ask_px`,
  `goals_ask_sz`, `assists_bid_px`, `assists_bid_sz`, `assists_ask_px`,
  `assists_ask_sz`, `ga_bid_px`, `ga_bid_sz`, `ga_ask_px`, `ga_ask_sz`.

### Data-quality notes

- The first 12 `trade_history` rows (logged 2026-07-01, before the fix below)
  had their `qty` pulled from the API's `trade.qty` field, which is a
  *rounded* display value — e.g. one row showed `qty=21` for a fill that was
  actually `20.54` shares (confirmed via `cost / price`). That's a ~2%
  overstatement on partial fills where the rounding is visible; whole-number
  fills (e.g. `56`, `600`) were unaffected. Rows logged after the fix pull the
  exact fill size from the matching execution object's `lastShares` field
  instead.
- The 3 affected rows for the `AXDZT5Q445HS` order (trade IDs `AXG3KSV2T5GZ`,
  `AXG3KSV2M5GZ`, `AXG3KSV2E5GZ`) were manually corrected to `20.54`, `56`,
  `600`.
- 2026-07-03: full sweep completed. Cross-checked all 62 `trade_history` rows
  against a fresh, fully-paginated `get_activities()` pull (392 account
  trades) -- zero rows missing in either direction, so this was purely a qty
  precision issue, not a completeness gap. Found and corrected 6 total
  pre-fix rows carrying the rounded value (the 3 above already fixed, plus
  `ASGD0CQH04K4` 28→27.64, `AXF2GEXG65GZ` 14→13.7, `AZEE1PKXE5GZ` 161→160.8,
  `AZEF9A0KR5GZ` 142→142.25, `AZEGC45805GZ` 172→171.75, `AZESPGKSM5GZ`
  156→155.9). Every trade logged after the code fix has checked out clean in
  every sweep run so far -- as of this date, no pre-fix rows remain
  uncorrected.
