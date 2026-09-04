# `research/` — backtesting and strategy validation

Offline tooling for evaluating the live bot's strategy. Nothing in here is
imported by the live bot; `bot.py` and its runtime path are untouched.

## Layout

| File | What it is |
|---|---|
| `data.py` | Alpaca SIP 1-minute bar downloader with a parquet cache |
| `fastsig.py` | Vectorized precomputation of every per-bar signal |
| `backtest.py` | Bar-by-bar replay engine (`run_backtest`) |
| `sweep.py` | Grid search + walk-forward validation |
| `diagnose.py` | Baseline metrics, P&L attribution, one-mechanism-at-a-time ablations |
| `controls.py` | Coin-flip and zero-cost controls — does the signal beat random? |
| `log_forensics.py` | Parses `bot.log` for what the live bot actually did |
| `test_backtest.py`, `test_fastsig.py` | The correctness suite (39 tests) |

## Quick start

```bash
cd app

# 1. Download and cache bars (~90 s first time, instant afterwards).
python -c "from research import data; data.fetch_minute_bars(['TQQQ','SOXL','SQQQ','UVXY','SOXS','SPY'],'2026-01-01','2026-09-01')"

# 2. Baseline + P&L attribution + ablations (~4 min).
python -m research.diagnose

# 3. Is there any edge in the entry signal? (~3 min)
python -m research.controls

# 4. The whole test suite.
python -m pytest research/ -q
```

Credentials come from `app/.env` (`ALPACA_API_KEY` / `ALPACA_API_SECRET`).
The SIP feed is enabled on this account, so full-tape minute bars including
pre/post-market are available.

## Writing a backtest

```python
from dataclasses import replace
from dotenv import load_dotenv; load_dotenv("app/.env")
import config
from research import backtest, data

bars = data.fetch_minute_bars(["TQQQ", "SOXL"], "2026-01-01", "2026-09-01")
cfg  = replace(config.load_config(), stop_loss_pct=0.015, take_profit_pct=0.03)
res  = backtest.run_backtest(cfg, bars, start_equity=100.0)

print(res.summary())
res.trades_df.head()      # per-trade blotter incl. MFE / MAE / exit_reason
res.equity_curve          # pd.Series
res.metrics               # dict
```

The engine takes a `BotConfig`, so a sweep is just `dataclasses.replace` over
the same config object the live bot loads.

## Parameter search

```python
from research import sweep

grid = {"stop_loss_pct": [0.006, 0.015, 0.025],
        "take_profit_pct": [0.012, 0.035, 0.06]}

sweep.sweep(grid, bars, cfg, metric="total_pnl", min_trades=100)   # in-sample
sweep.walk_forward(grid, bars, cfg, train_days=42, test_days=14)   # honest
```

Always read `walk_forward`, never the raw sweep. The sweep's top row is the
best-fitting row, which on a signal with no edge is simply the luckiest one.

## How the engine works

Replay is minute by minute over a merged timeline of every symbol's bars. At
each minute the engine, in this order:

1. fills orders queued on the previous bar, at this bar's open;
2. manages open positions — stop, take-profit, trailing ratchet, trend-break,
   candlestick, time stop, end-of-day flat;
3. scans for an entry — score, filters, cooldowns, position and notional caps,
   daily loss kill switch;
4. marks to market.

Decisions are made on a bar's close and filled at the *next* bar's open, which
is what the live bot's 10-second poll approximates.

### The fast path

`fast=True` (the default) reads precomputed arrays from `fastsig` instead of
re-running the pandas helpers on a fresh window every minute. This is a 54x
speedup — 13 minutes to 13 seconds for an 8-month replay — and it is what makes
a sweep possible at all.

`fast=False` runs the live bot's own `strategy_signals` functions. The two are
proven identical: `test_fastsig.py` checks every signal bar-for-bar on real
TQQQ data, and `test_fast_and_slow_engines_produce_identical_trades` demands an
identical trade blotter end to end. **If they ever disagree, the slow path is
right.**

Two bugs found this way, both of which would silently corrupt results:

- a cumsum rolling mean drifted ~3e-12, which flipped `close < sma` on price
  plateaus and changed trend-break exits. `fastsig` uses `pandas.rolling`.
- `DatetimeIndex.asi8` returned **microseconds**, not nanoseconds, for
  parquet-loaded bars, silently shifting the momentum reference bar.

## Approximations vs the live bot

Read `backtest.py`'s module docstring for the full list. The ones that matter:

- **Quotes.** The live bot reads the live bid/ask. A bar backtest cannot, so the
  spread is a constant (`spread_bps`, default 2 bp) and fills take
  `slippage_bps` (default 1 bp). Entries fill at the next bar's open.
- **Intrabar ordering.** If a bar's low breaches the stop *and* its high hits the
  take-profit, the stop is assumed to fill first. Conservative, and it matters:
  at wide targets a meaningful share of bars touch both.
- **Partial fills and rejects** do not exist here. Every order fills whole.
- **`max_entry_attempts_per_day`** is off by default because `bot.py` counts it
  but never enforces it — the backtest reproduces the live behaviour, not the
  documented one. Pass `enforce_max_entry_attempts=True` to see the intent.
- **Whole shares.** `whole_shares=True` floors quantity to integers, which on a
  $100 account is the binding constraint on everything.

## Known limitations — where the backtest can lie to you

- **Survivorship of the config.** The parameters were themselves chosen by
  looking at this data. Any number produced by a sweep on the same eight months
  used to pick it is in-sample, and in-sample numbers on a weak signal are
  meaningless. This is the biggest risk in the whole directory.
- **One regime.** January–September 2026 is a single market regime. A
  configuration that survives it has been tested against one draw.
- **Split adjustment.** Bars are fetched with `adjustment="all"`. Raw bars are a
  trap here — SOXS reverse-split twice in eight months, and unadjusted those
  appear as single +1900% and +873% bars that a momentum strategy reads as the
  trade of the century. If you ever pass `adjustment="raw"`, every result is
  garbage. The cache key includes the adjustment so the two cannot be mixed.
- **Thin pre/post-market bars.** SIP includes them. The session gate excludes
  them by default (`enable_extended_hours=False`), but turn that flag on and the
  engine will happily trade 4 a.m. prints with real spreads far wider than the
  2 bp model.
- **Costs are modelled, not measured.** 2 bp of spread is reasonable for TQQQ at
  size; it is optimistic for UVXY and for 1-share orders. Re-run anything
  important with `spread_bps=4` before believing it.
