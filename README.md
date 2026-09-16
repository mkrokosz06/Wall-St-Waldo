# Wall St Waldo

A long-only intraday ETF trading bot on Alpaca, with an offline backtesting and
strategy-validation suite.

> **Read this first.** The strategy in this repo does not have a measurable
> edge. That is not a disclaimer, it is a measurement: replacing the entry
> signal with a coin flip produces statistically indistinguishable results.
> The full evidence is in [`app/research/FINDINGS.md`](app/research/FINDINGS.md).
> The code is a working, well-instrumented execution harness. Treat it as
> that, and run it on paper.

## What it does

Ranks a small ETF universe by `momentum_return x log1p(volume_ratio)`, buys the
best candidate with a limit order near the bid, and manages the position with a
broker-side stop, a take-profit, and an end-of-day flatten.

- **Long only.** Buy and sell-to-close. No shorting, cash-account assumptions.
- **Universe:** `TQQQ`, `SOXL` (set by `SYMBOLS_UNIVERSE`).
- **Loop:** 10 seconds (`LOOP_INTERVAL_SEC`).
- **Whole shares only.** Alpaca rejects fractional limit orders, and a
  fractional fill cannot carry a broker-side stop.
- **Exit priority:** broker stop fill -> take-profit -> optional trend-break ->
  optional candlestick reversal -> optional time stop -> end-of-day flatten.
- **Risk rails:** per-trade risk cap, max open positions, portfolio notional
  cap, per-symbol entry cooldowns, and a daily realized-loss kill switch that
  stops new entries for the session.
- **Flattens on shutdown** (`FLATTEN_ON_SHUTDOWN`) and flattens positions
  carried across a date boundary at startup
  (`FLATTEN_STALE_POSITIONS_ON_START`). Both matter — see "What went wrong
  before" below.

Signal math lives in `app/strategy_signals.py`, kept out of `bot.py` so it can
be tested and replayed offline.

## Layout

| Path | What it is |
|---|---|
| `app/bot.py` | The trading loop, order lifecycle, and state machine |
| `app/config.py` | `BotConfig` + `.env` loading and validation |
| `app/strategy_signals.py` | Entry score, momentum, MA/trend, candlestick helpers |
| `app/alpaca_client.py` | Broker and market-data wrapper |
| `app/state_store.py` | Crash-safe JSON state with atomic writes |
| `app/dashboard_app.py` | Local Flask dashboard (separate process) |
| `app/research/` | Backtest engine, sweeps, walk-forward, log forensics |
| `tools/` | `analyze_trades.py`, `mine_trades.py` — log/trade analysis |
| `docs/log_analysis.md` | Independent forensic analysis of the live logs |

## Setup

```bash
pip install -r app/requirements.txt
cp app/.env.example app/.env     # then fill in ALPACA_API_KEY / ALPACA_API_SECRET
```

Run everything from the `app/` folder — that is where `.env` and `state.json`
live.

## Run (paper)

```bash
cd app
python run_bot.py              # runs until stopped, flattens on exit
python run_bot_for.py 1800     # run 30 minutes, then flatten and stop
python status_bot.py           # running? state? today's P&L? open positions?
python stop_bot.py             # emergency stop and flatten
```

`PAPER=true` is the default in `.env`. Keep it there.

## Dashboard (optional)

A **separate process** from the bot — start it yourself and keep the terminal
open.

```bash
cd app
python dashboard_app.py        # or double-click run_dashboard.bat on Windows
```

Then open **http://127.0.0.1:5050** (`http://`, not `https://`). Shows bot
status, broker daily P&L, per-trade P&L parsed from `bot.log`, totals, and win
rate, with Start and Stop-and-flatten buttons that call the same scripts as the
CLI. Localhost-only by default; override with `DASHBOARD_HOST` /
`DASHBOARD_PORT`.

## Backtesting

The research suite replays the live bot's own `BotConfig` bar by bar over
cached Alpaca SIP minute bars, so a parameter sweep is just
`dataclasses.replace` over the object the bot loads in production.

```bash
cd app
python -c "from research import data; data.fetch_minute_bars(['TQQQ','SOXL'],'2026-01-01','2026-09-01')"
python -m research.diagnose    # baseline, P&L attribution, ablations
python -m research.controls    # does the signal beat a coin flip?
python -m pytest research/ -q  # 39 tests
```

See [`app/research/README.md`](app/research/README.md) for the engine's
approximations and the ways a backtest here can lie to you. On Windows, wrap
any script that calls `sweep()` or `walk_forward()` in an
`if __name__ == "__main__":` guard — the process pool needs it.

**Always read `walk_forward`, never the raw sweep.** On a signal with no edge
the sweep's top row is simply the luckiest one.

## Key configuration

Full list in `app/.env.example`. The ones that change behaviour most:

| Variable | Current | Notes |
|---|---|---|
| `SYMBOLS_UNIVERSE` | `TQQQ,SOXL` | Inverse and vol ETFs were removed — see findings |
| `STOP_LOSS_PCT` | `0.006` | Flat across instruments; this is a known weakness |
| `TAKE_PROFIT_PCT` | `0.012` | |
| `MAX_OPEN_POSITIONS` | `2` | |
| `MAX_RISK_PER_TRADE` | `5.0` | Sized for a $100 account |
| `MAX_DAILY_REALIZED_LOSS` | `-200` | Must be negative; kill switch |
| `MARKET_OPEN_DELAY_MINUTES` | `5` | Skip the opening auction |
| `MAX_SPREAD_PCT` | `0.001` | Largest single entry-rejection reason live |
| `ALPACA_DATA_FEED` | `iex` | See the SIP/IEX asymmetry below |
| `ENABLE_TREND_BREAK_EXIT` | `false` | Measured money loser, t = -4.89 |
| `ENABLE_ONLINE_TRAINING` | `false` | Off deliberately — see findings |
| `ENABLE_OFFLINE_TRAINING` | `false` | Off deliberately — see findings |

### The SIP/IEX asymmetry

Backtests run on SIP (the full consolidated tape). The live bot **cannot** —
this subscription permits SIP for historical bars only and answers a request
for recent SIP data with `403 subscription does not permit querying recent SIP
data`. Live therefore runs on IEX, a ~2% volume venue. Since the entry score
includes a `volume_ratio` term, that term measures something materially
different live than in any backtest. Closing the gap needs a data subscription
upgrade, not a config change.

## What went wrong before

Kept here because the failure modes are the useful part of this repo. All of
these are fixed; each was found by analysing `bot.log`, not by reading code.

- **Fractional order quantities.** `_compute_qty_for_entry` returned a rounded
  float and fed it to a limit buy — 647 HTTP 422 rejections. The fractional
  fills that did get through could not carry a broker stop, which forced an
  in-loop synthetic stop that exited ~10x wider than intended. One missing
  `floor()` caused all of it.
- **808 positions held with no protective stop**, because a failed stop
  placement returned early without recording the leg.
- **Accidental overnight holds produced 86% of all realised P&L.** The
  end-of-day flatten only ran while the loop was alive, so killing the process
  left 3x leveraged ETFs open overnight. The worst single gap lost 2.4% of
  notional against an intended 0.6% stop. That track record was unmanaged gap
  risk, not edge.
- **Three `.env` settings silently ignored.** `STOP_LOSS_PCT`,
  `TAKE_PROFIT_PCT` and `TRAILING_STOP_PCT` were documented and set but never
  read by `load_config()`.
- **Unadjusted split data in research.** SOXS reverse-split twice in the
  sample, appearing as single +1,963% and +873% bars that a momentum backtest
  reads as the trade of the century. Bars are now fetched with
  `adjustment="all"`, with the adjustment in the cache key.
- **Two trainers fighting over the same state.** The online and offline
  threshold nudgers wrote the same two fields with different step sizes, so
  whichever ran last won. 212 adjustments came out 56 up / 46 down, and every
  value ever written loosened the entry gates until they sat against the clamp
  that exists to stop the bot buying dips.
- **A stale-data storm.** Alpaca publishes a 1-minute bar 60-90s after the
  minute closes, against a 90s staleness guard — so the bot spent most of each
  session refusing to evaluate signals. 14,259 warnings. The guard is now 150s.

## Safety notes

- Paper trade first, for several sessions. Verify order lifecycle tracking, the
  kill switch, and that no short ever appears.
- At a $100 account these instruments are 1-3 shares, and one round trip pays a
  spread that the live log shows frequently exceeds 10bp against a 1.2% target.
  Costs dominate. Account size is a binding constraint here, independent of
  strategy.
- `bot.log` is gitignored and grows fast — it reached 47MB.
- Nothing here is investment advice, and none of it is guaranteed profitable.
  The measured result is a loss.
