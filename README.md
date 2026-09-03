# Polymarket / Kalshi — Cross-Venue Prediction-Market Research System

A data-collection and research stack for US prediction markets (Polymarket US,
Kalshi). It discovers markets tied to real scheduled soccer matches across ~76
leagues on two venues, records tick-level pricing and order-book depth on a
schedule, archives concluded tournaments, and runs fee-aware analysis for genuine
cross-venue pricing gaps.

**This public repository is a curated slice: the Polymarket US collection layer
and the data-architecture design.** The live-trading components — order
execution, position management, the Kalshi client, the cross-venue scanner, the
funded account — are kept in a private repository. Prediction-market credentials
belong nowhere near a public remote, and this project has a documented incident
that taught that lesson. What's here is the part that's safe to show and,
arguably, the more interesting part: how a stack of collectors, ten SQLite
databases, and a dozen scheduled jobs stay correct and auditable while running
unattended.

### What's actually in this repository

- `collect_clean_triples.py` — the unattended Polymarket US collector, plus its
  `data/` layer (`client.py`, `markets.py`, `cache.py`).
- `cache/build_public_export.py` and `cache/polymarket_public.db` — the
  export builder and a sample exported database (account ledger + archived tick
  history in one shareable file).
- `docs/DATABASE_ARCHITECTURE.md` — the full ten-database inventory.
- `docs/polymarket-worldcup-paper.pdf` — a short research paper on 2026 World Cup
  player-prop pricing.

The Kalshi client, the cross-venue fee-aware scanner, the paper-trading loops,
every order-placement path, and the live account ledger live in a private
repository and are described below only for context.

---

## What the system does

| | |
|---|---|
| **Discovers** | soccer markets on Polymarket US and Kalshi tied to a real scheduled match — team winner, spread, total, both-teams-to-score, player props — matched *structurally* (a `"Team vs Team (date)"` event shape), not by a hand-maintained series list, because Kalshi alone lists 1,000+ soccer series. |
| **Collects** | tiered by time-to-kickoff: a slow routine cadence far out, a dense ±5-minute window at kickoff kept in its own table, bid/ask/last/volume/open-interest and top-of-book depth on every tracked market. Once a match is played, un-logged price data is gone for good — the collectors exist to close that gap. |
| **Archives** | a concluded tournament's tracking tables move from the live DB into a cold-archive DB by `game_slug` prefix — copy, verify row counts, full backup, *then* delete. The live DB went from ~445 MB to ~760 KB this way. |
| **Analyses** | a fee-aware scanner for Kalshi-vs-Polymarket pricing gaps on the same real-world outcome, net of both venues' real taker fees, with settlement-rule text pulled live so extra-time-eligible competitions are flagged as basis risk rather than treated as a clean lock. |

---

## Architecture

Three public APIs feed scheduled collectors that write **ten independent SQLite
databases**, deliberately not merged. The split is the point:

- **`cache/polymarket.db` — LIVE.** Only the current season's tracking data plus
  the permanent account ledger. Small, fast, always relevant.
- **`cache/historical.db` — COLD ARCHIVE.** Concluded tournaments, same schema.
  Large by design (hundreds of MB of tick history); never in the hot path.
- **Per-engine research DBs** (paper-trading ledgers, the cross-venue scanner,
  broad market capture) — each its own file, each its own schedule, none feeding
  the others or the published results.

A full inventory — every table, what writes it, what reads it, and a verdict on
each — is in **[`docs/DATABASE_ARCHITECTURE.md`](docs/DATABASE_ARCHITECTURE.md)**.

```
Polymarket Gateway API ─┐
Polymarket Trading API ─┼─▶  scheduled collectors  ─▶  cache/polymarket.db (LIVE)  ─▶  archive ─▶ historical.db ─▶ public export
Kalshi Public API      ─┘        (1–15 min)               + per-engine research DBs (private, no path to published output)
```

---

## Engineering worth a look

- **Live / archive DB split.** Unbounded row growth — not table count — was the
  real cost. `scripts/archive_to_historical.py` is a standing process, not a
  one-off: filter by slug, verify the copy matches the source exactly, back up,
  then delete, with SQLite cross-database rollback tested.
- **Structural market discovery.** Kalshi lists 1,000+ soccer series and its own
  data has copy-paste errors (a `*SPREAD` series titled `"… Game"`). Discovery
  matches on event *shape* and cross-checks tickers against every known
  market-type substring rather than trusting title text.
- **Read-only dashboards that can't contend with the writer.** Local stdlib HTTP
  servers, every DB connection opened `mode=ro` (enforced at the file level), so
  a monitoring page can never block the live collector.
- **Client-side rate limiting.** A thread-safe token bucket built into the Kalshi
  client after 65 real 429s in one session were root-caused to concurrent workers
  sharing one client with reactive-only backoff — smoothing the request rate
  under the ceiling beats bursting then sleeping.
- **Free-tier API budgeting.** Lineup-timestamp capture (api-football.com, 100
  req/day *and* 10 req/min) is spent strategically inside the ~60–100-min
  pre-kickoff announcement window and stops the moment lineups appear, rather
  than polled on a timer.
- **Honest results.** Where a scanned edge doesn't exist, it's reported as a zero,
  not forced into a trade. Data-quality bugs (a rounded `qty` field overstating
  partial fills by ~2%) are documented with the exact rows corrected.

---

## Setup

```bash
python -m venv venv && venv\Scripts\activate      # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                               # then fill in your own keys
```

`main.py` needs no credentials — Polymarket US market and event data is public
and unauthenticated. The collector's account lookups are **GET-only by design**
(`TradingClient` in `data/client.py` has no order-placement methods — an explicit
phase boundary, not an oversight).

## Run

```bash
python main.py                      # fetch current World Cup player-prop markets + prices, cache to SQLite
python main.py --raw                # print a raw event/market JSON sample (schema discovery), no caching
python collect_clean_triples.py     # the unattended collector — run on a 1-min timer
```

## Stack

Python 3.13 · `requests` · `pynacl` · SQLite (WAL, `mode=ro` readers) · Windows
Task Scheduler · no heavyweight dependencies in the collection path.

## Layout (this repo)

```
config.py                             .env-based config, base URLs, discovery filters
main.py                               World Cup player-prop fetch + ad-hoc schema discovery (--raw)
collect_clean_triples.py              the unattended live collector
data/client.py                        Polymarket US — public gateway client + GET-only TradingClient
data/markets.py                       World Cup event discovery + player-prop market filtering
data/cache.py                         append-only SQLite snapshot cache (cache/polymarket.db)
cache/build_public_export.py          live ledger + archive → one shareable export
cache/polymarket_public.db            sample public export
docs/DATABASE_ARCHITECTURE.md         full ten-database inventory
docs/polymarket-worldcup-paper.pdf    research paper
```

`signals/` and `execution/` are namespace placeholders in this slice — the
probability models and decision logic are in the private repository.

## License

MIT — see [LICENSE](LICENSE).
