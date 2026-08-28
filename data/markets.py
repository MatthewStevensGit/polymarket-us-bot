"""
Discovers World Cup player prop markets (goals, assists, goals+assists) and
pulls current pricing for each.
"""

from config import PROP_SPORTS_MARKET_TYPES, WORLD_CUP_LEAGUE_SLUG
from data.client import GatewayClient


def _is_prop_market(market: dict) -> bool:
    return market.get("sportsMarketType") in PROP_SPORTS_MARKET_TYPES


def find_world_cup_events(client: GatewayClient, page_size: int = 200) -> list[dict]:
    """Fetch confirmed head-to-head World Cup games (past, live, and
    upcoming) under the league's active series. Driven by the league's
    activeSeriesId rather than a hardcoded game list, so newly confirmed
    matchups show up automatically as the tournament progresses.

    The series also contains tournament-wide futures/placeholder events
    (golden boot, group winners, bracket-advance stubs with no teams
    assigned yet) — those are excluded by requiring a resolved gameId and
    exactly two teams, which only real scheduled games have."""
    league = client.get_league_by_slug(WORLD_CUP_LEAGUE_SLUG).get("league") or {}
    series_id = league.get("activeSeriesId")
    if series_id is None:
        return []

    events = []
    offset = 0
    while True:
        batch = client.list_events(seriesId=series_id, limit=page_size, offset=offset).get("events", [])
        events.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return [e for e in events if e.get("gameId") and len(e.get("teams") or []) == 2]


def find_player_prop_markets(client: GatewayClient, event: dict) -> list[dict]:
    """Pull an event's markets (embedded on the event, falling back to a
    gameId lookup) and keep the goal/assist player props."""
    markets = event.get("markets") or []
    if not markets:
        game_id = event.get("gameId") or (event.get("eventState") or {}).get("gameId")
        if game_id:
            markets = client.list_markets(gameId=game_id, limit=200).get("markets", [])
    return [m for m in markets if _is_prop_market(m)]


def fetch_player_prop_snapshot(client: GatewayClient) -> list[dict]:
    """Full pull: World Cup events -> player prop markets -> current BBO for
    each. Returns a flat list of dicts ready to cache."""
    rows = []
    for event in find_world_cup_events(client):
        for market in find_player_prop_markets(client, event):
            slug = market.get("slug")
            bbo = {}
            if slug:
                try:
                    bbo = client.get_market_bbo(slug).get("marketData") or {}
                except Exception as exc:
                    print(f"  [warn] bbo failed for {slug}: {exc}")
            rows.append({
                "event_id": event.get("id"),
                "event_title": event.get("title"),
                "market_id": market.get("id"),
                "market_slug": slug,
                "question": market.get("question"),
                "subject": (market.get("subject") or {}).get("name"),
                "sports_market_type": market.get("sportsMarketType"),
                "line": market.get("line"),
                "best_bid": (bbo.get("bestBid") or {}).get("value"),
                "best_ask": (bbo.get("bestAsk") or {}).get("value"),
                "last_trade_px": (bbo.get("lastTradePx") or {}).get("value"),
                "shares_traded": bbo.get("sharesTraded"),
            })
    return rows
