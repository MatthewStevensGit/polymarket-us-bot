import os
from dotenv import load_dotenv

load_dotenv()

# Loaded for future authenticated (trading) use — not used by the read-only
# data-fetching module, since Polymarket US market/event endpoints are public.
POLYMARKET_US_KEY_ID     = os.getenv("POLYMARKET_US_KEY_ID")
POLYMARKET_US_SECRET_KEY = os.getenv("POLYMARKET_US_SECRET_KEY")

GATEWAY_BASE_URL = "https://gateway.polymarket.us"  # public market/event data, no auth
API_BASE_URL     = "https://api.polymarket.us"       # authenticated trading (not used yet)

REQUEST_TIMEOUT = 20

CACHE_DIR     = "cache"
CACHE_DB_PATH = f"{CACHE_DIR}/polymarket.db"

# World Cup / player-prop discovery filters.
# The gateway's /v1/events `categories` param filters on the coarse content
# vertical (always "sports" for every sport) — it's not a per-sport/league
# filter, so soccer events can't be found that way. The reliable path is the
# league's slug -> its activeSeriesId (via GET /v2/leagues/{slug}) -> every
# event tagged with that seriesId (GET /v1/events?seriesId=...), which
# includes past/live/upcoming games plus tournament-wide futures.
WORLD_CUP_LEAGUE_SLUG = "fwc"

# Confirmed sportsMarketType values for player goal/assist markets (verified
# against live event data on 2026-06-30). Team-level markets like
# "soccer_team_full_time_winner" or player markets for other stats like
# "soccer_player_shots" are deliberately excluded.
PROP_SPORTS_MARKET_TYPES = (
    "soccer_player_goals",
    "soccer_player_assists",
    "soccer_player_goals_plus_assists",
)
