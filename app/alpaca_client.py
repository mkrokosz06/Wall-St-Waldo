import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame


def _parse_utc_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    if isinstance(s, dt.datetime):
        return s
    # Alpaca often returns timestamps like "2024-01-01T12:34:56.000000Z"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    timestamp: Optional[dt.datetime] = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if m <= 0:
            return 1.0
        return (self.ask - self.bid) / m


class AlpacaMarketData:
    """
    Uses alpaca-py market data clients for bars and quotes.
    Note: You may need a market data subscription depending on your account.
    """

    def __init__(self, api_key: str, api_secret: str, paper: bool):
        # Alpaca-py `sandbox` controls the *market data host*.
        # We default to sandbox=False because your paper trading keys worked with the
        # production data host in `test_data_auth.py`.
        # Only force sandbox if explicitly requested.
        md_sandbox_raw = os.getenv("FORCE_MARKETDATA_SANDBOX", "").strip().lower()
        md_sandbox = md_sandbox_raw in {"1", "true", "t", "yes", "y", "on"}
        self._historical = StockHistoricalDataClient(api_key, api_secret, sandbox=md_sandbox)
        # Quotes are also available via the historical data client.
        # paper flag affects trading endpoints; market data endpoints are generally the same for alpaca-py.
        self.paper = paper

        # Feed selection. IEX is the default and must stay the default for live
        # trading: this subscription allows SIP only for *historical* bars, and
        # answers a request for recent SIP data with
        #   403 "subscription does not permit querying recent SIP data".
        #
        # That asymmetry is worth knowing about, because research/ builds its
        # backtests on SIP while the live bot necessarily runs on IEX — a ~2%
        # volume venue whose minute bars are sparse enough that the newest bar is
        # often older than stale_data_max_age_sec (14,281 "Market data stale"
        # warnings in bot.log). The live signal is therefore computed on a much
        # thinner tape than any backtest assumes. Raising the feed to SIP would
        # need a market-data subscription upgrade, not a config change.
        feed_raw = os.getenv("ALPACA_DATA_FEED", "iex").strip().strip('"').strip("'").lower()
        self.feed = {"sip": DataFeed.SIP, "iex": DataFeed.IEX}.get(feed_raw, DataFeed.IEX)

    def get_latest_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=self.feed)
        resp = self._historical.get_stock_latest_quote(req)

        out: Dict[str, Quote] = {}
        # resp is typically a dict keyed by symbol
        for sym, q in resp.items():
            out[sym] = Quote(
                symbol=sym,
                bid=float(q.bid_price),
                ask=float(q.ask_price),
                timestamp=_parse_utc_ts(getattr(q, "timestamp", None)),
            )
        return out

    def get_recent_bars(
        self,
        symbols: List[str],
        start: dt.datetime,
        end: dt.datetime,
        timeframe: TimeFrame = TimeFrame.Minute,
    ) -> pd.DataFrame:
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=self.feed,
        )
        resp = self._historical.get_stock_bars(request)
        # Expected: resp.df is a pandas DataFrame with a MultiIndex [symbol, timestamp]
        return resp.df


class AlpacaTradingREST:
    def __init__(self, api_key: str, api_secret: str, paper: bool):
        self.api_key = api_key
        self.api_secret = api_secret
        # Allow explicit base URL override to match Alpaca dashboard/environment.
        env_base = os.getenv("ALPACA_TRADING_BASE_URL")
        if env_base:
            self.base_url = env_base.rstrip("/")
        else:
            self.base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.api_secret,
                "Content-Type": "application/json",
            }
        )

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base_url + path
        r = self.session.get(url, params=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {path} failed: {r.status_code} {r.text}")
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        url = self.base_url + path
        r = self.session.post(url, data=json.dumps(payload), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} failed: {r.status_code} {r.text}")
        return r.json()

    def _delete(self, path: str) -> dict:
        url = self.base_url + path
        r = self.session.delete(url, timeout=30)
        if r.status_code >= 400:
            # Some cancel calls return 404 if already filled/canceled; treat it as non-fatal.
            if r.status_code == 404:
                return {"status": "not_found"}
            raise RuntimeError(f"DELETE {path} failed: {r.status_code} {r.text}")
        return r.json() if r.text else {"status": "ok"}

    def get_clock(self) -> dict:
        return self._get("/v2/clock")

    def get_positions(self) -> List[dict]:
        # Returns a list of positions
        return self._get("/v2/positions")

    def get_account(self) -> dict:
        return self._get("/v2/account")

    def get_order(self, order_id: str) -> dict:
        return self._get(f"/v2/orders/{order_id}")

    def get_open_orders(self, symbols: Optional[List[str]] = None) -> List[dict]:
        params: Dict[str, str] = {"status": "open"}
        if symbols:
            params["symbols"] = ",".join(symbols)
        data = self._get("/v2/orders", params=params)
        return data if isinstance(data, list) else data.get("orders", [])

    def submit_order(self, order: dict) -> dict:
        # order must match Alpaca POST /v2/orders schema
        return self._post("/v2/orders", payload=order)

    def cancel_order(self, order_id: str) -> None:
        self._delete(f"/v2/orders/{order_id}")

