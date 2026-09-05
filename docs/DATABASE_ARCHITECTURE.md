# Database Architecture

*Where every row comes from, and where it goes.*

The system writes to **ten SQLite databases**. They are deliberately not merged —
the separation is a design decision that keeps the live path small, the research
data private, and every table's purpose auditable.

---

## The split

| database | role | size | in the hot path? |
|---|---|---|---|
| `cache/polymarket.db` | **LIVE** — current season's tracking + the permanent account ledger | ~760 KB | yes |
| `cache/historical.db` | **COLD ARCHIVE** — concluded tournaments, same schema | ~450 MB | never |
| `cache/kalshi_market_data.db` | broad Kalshi soccer-market capture (all series) | growing | own schedule |
| `cache/lineup_data.db` | lineup-announcement timestamps (3rd-party source) | small | own schedule |
| `cache/kalshi_paper.db` | Kalshi paper-trading ledger — simulated orders only | small | own schedule |
| `cache/polymarket_paper.db` | Polymarket paper-trading ledger — simulated orders only | small | own schedule |
| `cache/cross_platform_arb.db` | fee-aware Kalshi-vs-Polymarket gap scanner — observation only | small | own schedule |
| `cache/team_arb_paper.db` | team-market arb paper simulation | growing | own schedule |
| `cache/fleet.db` | read-only research layer — consumes every DB above, produces no orders | small | offline |
| `cache/polymarket_public.db` | the one shareable export (live ledger + archive → single file) | small | build step |

Solid rule: **the account ledger and the archive → export chain are the only path
to published results.** The paper-trading and research databases never reach it.

---

## `cache/polymarket.db` — the live database

19 tables + 1 view. Three zones:

**Account ledger** (permanent — never archived):

| table | written by | read by |
|---|---|---|
| `trade_history` | real fills, via a GET-only trading client (deduped on `trade_id`) | P&L recompute, published results, latency validation |
| `settlement_history` | real market resolutions | P&L recompute, published results |
| `cash_activity` | deposits / withdrawals / transfers | NAV / Modified-Dietz return calc |
| `closed_trades_pnl` / `open_positions` | fully derived — rebuilt from the three tables above every run | dashboards, published results |

> The public repository's `collect_clean_triples.py` is an earlier snapshot
> that only creates `trade_history` and `closed_trades_pnl`; `settlement_history`,
> `cash_activity`, and `event_latency_validation` were added in a later,
> private revision. `cache/polymarket_public.db` ships all four, sourced from
> the live account and window-filtered to the paper's 2026-06-15–2026-07-19
> analysis period.

**Active-season tracking** (archived when a tournament concludes):

| table | what it holds | cadence |
|---|---|---|
| `clean_price_triples` | deduped pre-kickoff goals / assists / G+A last-trade triples — the strategy signal | tiered by time-to-kickoff |
| `position_price_history` | dense bid/ask/last timeline for the watchlist | every run |
| `order_book_snapshots` | top-5 book depth (JSON) for the watchlist | every run |
| `kickoff_window_snapshots` | ±5 min of kickoff, 10 s cadence — kept separate from routine tracking | only near kickoff |
| `market_price_history` | **broad capture** — every market type, every league, ~76 leagues, with the live score/clock logged alongside | tiered, own poll-state table |

**Operational state** — small tables (single digits to low hundreds of rows) that
gate collector behaviour: per-game last-polled timestamps, once-per-day discovery
locks, dropped-market diagnostics. They look like clutter by name; removing them
breaks polling logic.

---

## `cache/historical.db` — the cold archive

Same schema as the live DB, populated only where a `game_slug` matches an
archived prefix. Nearly all of its size is three tables —
`position_price_history`, `order_book_snapshots`, `reaction_time_snapshots` —
each with 500k–1.1M rows of tick history. This file being large is the
live/archive reorganisation *working*, not clutter. It gets a new sibling every
time a tournament wraps:

```bash
python scripts/archive_to_historical.py --prefix <league> --dry-run   # copy + verify only
python scripts/archive_to_historical.py --prefix <league>             # copy, verify, back up, delete
```

The script filters every `game_slug`-keyed table, verifies the copied row count
matches the source *exactly* before deleting anything, and takes a full backup
before the first delete. SQLite's cross-database rollback is tested — a failed
archive leaves both databases untouched.

---

## The paper-trading and research databases

Four databases, each a fully separate file, each on its own schedule, **none of
which feeds the published results**:

- **`kalshi_paper.db` / `polymarket_paper.db`** — simulated-order ledgers. The
  Kalshi one drives an untested single-game reversion rule; the Polymarket one
  drives the *real, already-published* strategy logic, so it's a preview of what
  the live system would do once the new season's markets list, before any real
  capital is involved.
- **`cross_platform_arb.db`** — one table. The scanner never places or simulates
  an order; it measures. A gap counts only when Kalshi's ask + Polymarket's
  implicit opposite-side ask total under $1.00 by *more than both venues' real
  taker fees*. Settlement-rule text is pulled live, so extra-time-eligible
  competitions are flagged as basis risk, not a clean lock.
- **`team_arb_paper.db`** — a $1,000 paper simulation on team moneyline markets,
  scanning every ~2 seconds (concurrent fetch), flat position sizing, with a
  "lock integrity" check that verifies realised P&L exactly matches the profit
  computed at entry (a real correctness test — this strategy has no probabilistic
  losers by construction).

---

## `cache/fleet.db` — the read-only research layer

Consumes every database above and produces no orders. A backtest harness, a
strategy-decorrelation view, and a target-weight allocator that places nothing.
Isolated in its own virtual environment; an import-isolation test enforces that
nothing here can reach an order-placement client.

---

## Browsing safely while the collectors run

Every dashboard and ad-hoc query opens its connection `mode=ro` (enforced at the
file level, not by convention), so a reader can never contend with the live
writer. Two always-fresh SQL views (`view_trade_ledger`, `view_upcoming_games`)
are dropped and recreated on every connect so their definition always matches the
code — cheap for a view, and it removes a class of "stale view" bugs.
