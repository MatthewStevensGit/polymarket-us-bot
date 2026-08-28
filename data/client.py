"""
Thin wrapper around the Polymarket US public (gateway) API, plus a
strictly-read-only wrapper around the authenticated api.polymarket.us
portfolio endpoints.

TradingClient below deliberately exposes GET-only methods (positions,
activities/trade history). It does NOT implement order creation, order
modification, order cancellation, or close-position -- those are order
placement/execution concerns that belong to the execution phase, not here.
"""

import base64
import time

import requests
from nacl.signing import SigningKey

from config import API_BASE_URL, GATEWAY_BASE_URL, REQUEST_TIMEOUT

# Per https://docs.polymarket.us/api-reference/rate-limits: unauthenticated
# requests are capped at 20/sec/IP, and a 429 should be met with "stop
# immediately, wait at least 1 second, then retry with exponential backoff."
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0


class GatewayClient:
    def __init__(self, base_url: str = GATEWAY_BASE_URL, timeout: float = REQUEST_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        # requests serializes Python bool as "True"/"False"; the API expects lowercase.
        if params:
            params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()}

        backoff = RATE_LIMIT_BACKOFF_BASE_SECONDS
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
            if resp.status_code == 429:
                if attempt == RATE_LIMIT_MAX_RETRIES:
                    resp.raise_for_status()
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()

    def list_events(self, **params) -> dict:
        return self._get("/v1/events", params=params)

    def get_league_by_slug(self, slug: str) -> dict:
        return self._get(f"/v2/leagues/{slug}")

    def get_event_by_slug(self, slug: str, groups: bool = True) -> dict:
        return self._get(f"/v1/events/slug/{slug}", params={"groups": groups})

    def list_markets(self, **params) -> dict:
        return self._get("/v1/markets", params=params)

    def get_market_by_slug(self, slug: str) -> dict:
        return self._get(f"/v1/market/slug/{slug}")

    def get_market_bbo(self, slug: str) -> dict:
        return self._get(f"/v1/markets/{slug}/bbo")

    def get_market_book(self, slug: str) -> dict:
        return self._get(f"/v1/markets/{slug}/book")


def _create_auth_headers(key_id: str, secret_key: str, method: str, path: str) -> dict[str, str]:
    """Ed25519-sign {timestamp}{method}{path} per
    https://docs.polymarket.us/api-reference/authentication -- query
    parameters and body are NOT part of the signed message."""
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}"

    secret_key_bytes = base64.b64decode(secret_key)
    if len(secret_key_bytes) == 64:
        secret_key_bytes = secret_key_bytes[:32]

    signature = SigningKey(secret_key_bytes).sign(message.encode()).signature

    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": base64.b64encode(signature).decode(),
    }


class TradingClient:
    """Read-only wrapper around the authenticated api.polymarket.us portfolio
    endpoints. GET-only by design -- no order creation, modification,
    cancellation, or close-position methods exist here or anywhere in this
    class. Adding any of those is an execution-phase concern, not this one."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        base_url: str = API_BASE_URL,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.key_id = key_id
        self.secret_key = secret_key
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        headers = _create_auth_headers(self.key_id, self.secret_key, "GET", path)
        headers["Content-Type"] = "application/json"

        backoff = RATE_LIMIT_BACKOFF_BASE_SECONDS
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            resp = self.session.get(
                f"{self.base_url}{path}", params=params, headers=headers, timeout=self.timeout
            )
            if resp.status_code == 429:
                if attempt == RATE_LIMIT_MAX_RETRIES:
                    resp.raise_for_status()
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()

    def get_positions(self, **params) -> dict:
        return self._get("/v1/portfolio/positions", params=params)

    def get_activities(self, **params) -> dict:
        return self._get("/v1/portfolio/activities", params=params)

    def get_balances(self) -> dict:
        return self._get("/v1/account/balances")
