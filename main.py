"""
Polymarket US — World Cup player prop data puller (read-only).

Usage:
    python main.py            # fetch current player prop markets + prices, cache to SQLite
    python main.py --csv      # also export the full cache history to cache/prop_snapshots.csv
    python main.py --raw      # print a raw sample event/market JSON (schema discovery), no caching
"""

import json
import sys

from data import cache
from data.client import GatewayClient
from data.markets import fetch_player_prop_snapshot, find_player_prop_markets, find_world_cup_events


def main():
    show_raw = "--raw" in sys.argv
    do_csv   = "--csv" in sys.argv

    client = GatewayClient()

    if show_raw:
        print("Fetching raw list_events response (no filters)...")
        try:
            raw = client.list_events()
        except Exception as exc:
            print(f"  [error] Could not reach Polymarket US API: {exc}")
            return
        print("\n--- raw list_events response ---")
        print(json.dumps(raw, indent=2))

        events = raw.get("events", [])
        if not events:
            print("\n  No events in the raw response — nothing further to inspect.")
            return

        sample = events[0]
        print("\n--- sample event (for schema discovery) ---")
        print(json.dumps(sample, indent=2)[:4000])
        markets = find_player_prop_markets(client, sample)
        if markets:
            print("\n--- sample player prop market ---")
            print(json.dumps(markets[0], indent=2)[:4000])
        else:
            print("\n  No player prop markets matched in this event yet — "
                  "check config.PROP_SPORTS_MARKET_TYPES against the event JSON above.")
        return

    print("Fetching World Cup soccer events...")
    try:
        events = find_world_cup_events(client)
    except Exception as exc:
        print(f"  [error] Could not reach Polymarket US API: {exc}")
        return
    print(f"  Found {len(events)} event(s)")

    print("Scanning events for goal/assist player prop markets...")
    rows = fetch_player_prop_snapshot(client)
    print(f"  Found {len(rows)} player prop market(s)")

    saved = cache.save_snapshot(rows)
    print(f"  Cached {saved} row(s) to cache/polymarket.db")

    if do_csv:
        cache.export_csv()


if __name__ == "__main__":
    main()
