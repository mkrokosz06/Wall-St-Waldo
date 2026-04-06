"""
Quick Alpaca market-data auth checker.

Run:
  python test_data_auth.py

It attempts to fetch latest quotes for a sample symbol to validate access
to the Market Data API (bars/quotes).
"""

import os

from dotenv import load_dotenv

from alpaca_client import AlpacaMarketData


def main() -> None:
    env_path = ".env" if os.path.exists(".env") else ".env.example"
    load_dotenv(dotenv_path=env_path, override=True)

    api_key = os.environ.get("ALPACA_API_KEY", "").strip().strip('"').strip("'")
    api_secret = os.environ.get("ALPACA_API_SECRET", "").strip().strip('"').strip("'")
    if not api_key or not api_secret:
        raise SystemExit("Missing ALPACA_API_KEY / ALPACA_API_SECRET in env file.")

    paper = os.environ.get("PAPER", "true").strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    market = AlpacaMarketData(api_key=api_key, api_secret=api_secret, paper=paper)

    # Use one of the bot's ETF universe symbols.
    quotes = market.get_latest_quotes(["SPY"])
    q = quotes.get("SPY")
    if not q:
        raise RuntimeError("No quote returned for SPY (unexpected).")

    print(f"Market data auth OK: SPY bid={q.bid:.4f} ask={q.ask:.4f}")


if __name__ == "__main__":
    main()

