"""
Quick Alpaca auth checker.

Run:
  python test_auth.py

It validates /v2/clock and /v2/account using the same env loading behavior
as run_bot.py.
"""

import os
from dotenv import load_dotenv

from alpaca_client import AlpacaTradingREST


def main() -> None:
    env_path = ".env" if os.path.exists(".env") else ".env.example"
    # Force-load values from the env file so regenerated keys are used.
    load_dotenv(dotenv_path=env_path, override=True)

    api_key = os.environ.get("ALPACA_API_KEY", "").strip().strip('"').strip("'")
    api_secret = os.environ.get("ALPACA_API_SECRET", "").strip().strip('"').strip("'")

    if not api_key or not api_secret:
        raise SystemExit("Missing ALPACA_API_KEY / ALPACA_API_SECRET in env file.")

    paper = os.environ.get("PAPER", "true").strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    trading = AlpacaTradingREST(api_key=api_key, api_secret=api_secret, paper=paper)

    try:
        _ = trading.get_clock()
        _ = trading.get_account()
    except Exception as e:
        msg = str(e).lower()
        if "401" in msg:
            raise RuntimeError(
                "Alpaca returned 401 (Unauthorized). Regenerate Alpaca *Trading* API keys "
                "in the Alpaca dashboard and replace ALPACA_API_KEY / ALPACA_API_SECRET."
            ) from e
        raise

    print("Alpaca auth OK (clock + account responded).")


if __name__ == "__main__":
    main()

