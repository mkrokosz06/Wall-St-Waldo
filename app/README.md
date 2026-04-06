# ETF Day Trading Bot (Alpaca, Python)

Long-only bot (buy and sell-to-close), cash account assumptions, no shorting.

## What it does (v1)
- Trades **ETFs only** from a small universe (`SPY`, `QQQ`, `IWM`, `DIA`, `VTI`)
- Runs a **30-second loop**
- **Entry pipeline** (when flat): rank symbols by **momentum × log(volume ratio)** → filter by spread, momentum floor, optional dual-MA trend gate, optional SPY market filter, optional candlestick score boost → **limit buy** near bid.
- **Exit priority** (in position): broker **stop-loss** fill first → optional **take-profit** → optional **trend-break** (consecutive closes below SMA) → optional **candlestick bearish** exit → optional **time stop** → **end-of-day flatten**. Implementation detail: non-stop exits use `_execute_market_exit` (cancel stop, then market sell).
- Core signal math lives in **`strategy_signals.py`** (easier to test and tune than embedding everything in `bot.py`).
- **Time stop is off by default** (exits are driven by risk + structure, not a clock). Optional safety cap: set `ENABLE_TIME_STOP=true` and `TIME_STOP_MINUTES` in `.env`.
- **Adaptive thresholds**: (1) *offline* nudge from recent trades in `bot.log` on startup; (2) *online* nudge after each closed trade (small bounded steps — not ML, but learns from outcomes).
- Daily kill-switch: once **realized P&L after exits** reaches **-$20**, it stops placing new entries for the rest of the session.
- **Paper trading first** before live.

## Prerequisites
- Python 3.10+
- Alpaca account with API keys
- Market data access/subscription in Alpaca as required for real-time bars/quotes

## Setup
1. Create your environment file:
   - Copy `.env.example` to `.env` and fill in `ALPACA_API_KEY` and `ALPACA_API_SECRET`
2. Install dependencies:
   - `pip install -r requirements.txt`

## Run (paper)
Run from the `app` folder (where `run_bot.py` and `.env` live):

- `python run_bot.py`

The bot uses `PAPER=true` from `.env` by default.

## Local dashboard (optional)
Install deps (`pip install -r requirements.txt`).

The dashboard is a **separate process** from the trading bot — you must start it yourself and **keep that terminal open** (or use the batch file below).

**Option A — Windows:** double-click `run_dashboard.bat` inside `app/`.

**Option B — command line:** open a terminal, `cd` into the **`app`** folder (same folder as `.env`), then:

- `python dashboard_app.py`

In your browser open exactly:

- **http://127.0.0.1:5050** — use **`http://`**, not `https://`

If the page says it can’t be reached:

1. Confirm you see `Running on http://127.0.0.1:5050` in the terminal (if not, fix any error shown there).
2. Run from **`app/`**, not the parent `Trade Bot` folder.
3. Try **http://localhost:5050** on the **same PC** only (this does not work from your phone unless you change host — default is localhost-only for safety).
4. If port 5050 is busy, set `DASHBOARD_PORT=5051` in `.env` or the environment and restart the dashboard.

Shows bot status, broker daily P&amp;L, per-trade P&amp;L parsed from `bot.log`, totals, and win rate. **Start** / **Stop & flatten** call the same scripts as the CLI (`run_bot.py` / `stop_bot.py`).

Optional env: `DASHBOARD_HOST` (default `127.0.0.1`), `DASHBOARD_PORT` (default `5050`).

### Optional `.env` toggles
- `ENABLE_TIME_STOP` — `true` / `false` (default `false`)
- `TIME_STOP_MINUTES` — e.g. `240` (only if time stop enabled)
- `ENABLE_TREND_FILTER` — dual-MA trend gate on the candidate symbol
- `ENABLE_SPY_MARKET_TREND_FILTER` — require SPY uptrend (if `SPY` is in your universe)
- `ENABLE_CANDLESTICK_ENTRY_FILTER` — require bullish engulfing or hammer confirmation for entries
- `ENABLE_CANDLESTICK_EXIT` — exit early on bearish engulfing or shooting-star reversal
- `REQUIRE_CANDLESTICK_CONFIRMATION` — require the next candle to close above the bullish pattern close
- `CANDLESTICK_VOLUME_CONFIRM_MULT` — pattern-candle volume must be this multiple of recent average
- `CANDLESTICK_SCORE_BONUS` — score boost when bullish candlestick context is strong
- `ENABLE_TRAILING_STOP` — ratchet stop up with new highs
- `ENABLE_TREND_BREAK_EXIT` — exit when close falls below short SMA
- `TREND_BREAK_CONFIRM_BARS` — require this many consecutive closes below SMA before trend-break exit
- `MIN_HOLD_MINUTES_BEFORE_STRUCTURE_EXIT` — minimum hold time before trend-break/candlestick exits can trigger
- `ENABLE_ONLINE_TRAINING` — per-trade threshold nudges after each exit

## Run for a fixed duration (recommended for Scheduled Task)
- `python run_bot_for.py 1800`
  - runs in paper mode for 1800 seconds (~30 minutes) and then flattens/stops

## Emergency stop / flatten
- `python stop_bot.py`

## Quick status
- `python status_bot.py`
  - prints: running/not running, PID, paper/live mode, bot state, today's realized P&L, and open positions

## Logs
- `bot.log` (file)
- Console output

## Safety notes (important)
- With $100 capital, real trading behavior (spread, partial fills, queueing) can differ from backtests.
- Start with paper trading for several sessions, verify:
  - order lifecycle tracking (entry/stop/exit)
  - daily kill-switch triggers at the right time
  - no shorting behavior
- This bot is v1 and not “guaranteed profitable.” It is engineered for cautious execution and safety rails.

