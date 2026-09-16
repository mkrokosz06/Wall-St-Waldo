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
| `app/state_store.py` | Crash-safe JSON state, atomic writes, schema migration |
| `app/order_ledger.py` | Durable order records and exactly-once fill accounting |
| `app/protection.py` | Confirmed cancellation and verified stop coverage |
| `app/session_policy.py` | Trading session, opening delay, closing cutoff |
| `app/data_validation.py` | Per-symbol bar/quote freshness and sanity |
| `app/risk_budget.py` | Daily allowance and reserved open risk |
| `app/tests/` | Offline deterministic suite (fake broker, fake clock) |
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
python -m pytest tests research/test_backtest.py research/test_fastsig.py -q
```

That is the full offline suite — 184 tests, no network and no credentials. Do
not collect `test_auth.py` or `test_data_auth.py`: those talk to the real API.

**Execution models matter.** Results carry one, in `result.params`. The default
`spread_aware` charges the ask on buys and the bid on sells; `legacy` reproduces
pre-2026-09-15 behaviour, which charged **no spread on entries** and so barely
responded to `spread_bps` at all. Re-run anything important at `spread_bps=10`.
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
| `STOP_LOSS_PCT` | `0.015` | Flat across instruments; ATR-scaling is open work |
| `TAKE_PROFIT_PCT` | `0.035` | Loses least under honest costs; still loses |
| `MAX_OPEN_POSITIONS` | `2` | |
| `MAX_RISK_PER_TRADE` | `5.0` | Sized for a $100 account |
| `MAX_DAILY_REALIZED_LOSS` | `-200` | Must be negative; kill switch |
| `MAX_ENTRY_ATTEMPTS_PER_DAY` | `3` | **Newly enforced** — was dead config. 0 disables |
| `QUOTE_STALE_MAX_AGE_SEC` | `30` | Quote age limit for entries |
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
  in-loop synthetic stop that checked only on the 10-second poll and so exited
  wide of its trigger — averaging -$6.03 against an intended $4.89 on ~$815 of
  notional, about a 23% overshoot. One missing `floor()` caused all of it.
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
- **The backtest never charged the spread on entries.** The modelled bid/ask was
  applied to exits only, so a round trip paid about half the spread it should
  have and total cost did not move with `spread_bps`. Results looked robust to
  transaction costs; they were simply not paying them. Correcting it turns the
  current config's full-sample +$8.32 into **-$20.48** at a realistic 10bp.
- **A universe-filtered flatten.** `_flatten_symbols` is called for holdings
  *outside* the universe but read quantity through a universe-filtered lookup,
  got zero, cancelled the position's protective orders and sold nothing.
- **Dead risk config.** `max_entry_attempts_per_day` was defined and its counter
  incremented, but the two were never compared, so the documented cap did not
  exist. Same for the daily loss control, which counted only realized losses and
  ignored risk already committed to open and pending orders.
- **An empty equity curve.** `record_equity="day"` attached its point to the
  last bar of each ET date, but out-of-session bars skipped the mark-to-market
  step — so whenever postmarket bars trailed the session the daily curve came
  back empty, taking Sharpe and max drawdown with it.

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
