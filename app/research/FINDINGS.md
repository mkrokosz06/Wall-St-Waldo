# Findings

Written 2026-09-03. Every number below came from a script in this directory and
can be reproduced with the commands in `README.md`.

Data: Alpaca SIP 1-minute bars, split- and dividend-adjusted, 2026-01-01 to
2026-09-01, 884,734 bars over 167 sessions, for TQQQ / SOXL / SQQQ / UVXY /
SOXS / SPY. Live evidence: `app/bot.log`, 86,197 lines, 189 round-trip trades
between 2026-03-18 and 2026-07-07.

---

## The headline

**The entry signal has no measurable edge.** Replacing it with a coin flip,
holding every other part of the system constant — same session gates, same
cooldowns, same position sizing, same exit ladder — produces statistically
indistinguishable results.

| | trades | total P&L | win % | profit factor | expectancy |
|---|---|---|---|---|---|
| Real signal | 3,605 | **-52.52** | 23.9 | 0.893 | -0.0146 |
| Coin flip (8 seeds, count-matched) | ~2,400 | -22.9 … -49.0 | 19.1 | 0.867 ± 0.042 | -0.0148 ± 0.0037 |

The real signal sits **+0.06 standard deviations** from random on expectancy.
That is not a weak edge; it is no edge.

A second control points the same way: switching off *every* entry filter — trend
filter, score threshold, momentum threshold — changes the eight-month result
from -52.52 to -54.19. The filters that the config spends most of its surface
area on are worth about $1.70 over 3,600 trades.

Reproduce: `python -m research.controls`

---

## Why it loses: the stop is inside the noise

Measured 1-minute volatility during regular hours:

| symbol | median 1-min range | 1-min σ | 10-min σ |
|---|---|---|---|
| TQQQ | 0.160% | 0.202% | 0.640% |
| SQQQ | 0.157% | 0.202% | 0.637% |
| UVXY | 0.181% | 0.293% | 0.926% |
| SOXL | 0.330% | 0.483% | 1.526% |
| SOXS | 0.467% | 0.497% | 1.571% |
| SPY | 0.037% | 0.045% | 0.142% |

The live config runs a 0.6% stop and a 1.2% take-profit. On TQQQ that is roughly
a 1σ stop against a 2σ target over a ten-minute horizon. For a driftless random
walk the probability of touching -1σ before +2σ is about ⅔ — which predicts a
win rate near 33%. The backtest produces 23.9% and the live log produces 37.0%.
**The observed hit rate is what the stop geometry alone predicts.** No alpha is
required to explain the results, and none is present.

On SOXL, whose 10-minute σ is 1.53%, a 0.6% stop is a *0.4σ* barrier. Those
trades are close to a coin flip that pays the spread every time it is flipped.

---

## Where the money actually goes

Backtest, 8 months, live config:

| exit reason | trades | total P&L | avg | win % | avg bars held | avg MFE | avg MAE |
|---|---|---|---|---|---|---|---|
| stop | 1,068 | **-322.10** | -0.302 | 0.0 | 4.3 | +0.28% | -0.80% |
| trend_break | 1,695 | **-165.19** | -0.098 | 3.1 | 16.4 | +0.34% | -0.30% |
| eod | 128 | +18.20 | +0.142 | 74.2 | 33.6 | +0.58% | -0.19% |
| take_profit | 714 | +416.57 | +0.583 | 100.0 | 17.9 | +1.40% | -0.21% |

And the same pattern in the live log, independently:

| exit reason (live, current universe) | trades | total P&L | win % | t-stat |
|---|---|---|---|---|
| `trend_break` | 41 | **-22.82** | 12.2 | **-4.89** |
| `stop_loss_synthetic` | 22 | **-78.17** | 18.2 | **-4.03** |
| `take_profit` | 15 | +223.79 | 100.0 | — |

Two results survive both the backtest and the live log at meaningful
significance:

1. **The trend-break exit is a statistically significant money loser.** 1,695
   backtested trades at -0.098 average; 41 live trades at -0.56 average with a
   t-statistic of -4.89. It fires after an average of 16 bars on positions whose
   average maximum favourable excursion was +0.34% — it is systematically
   cutting positions that had already moved in their favour, at a loss.
   Disabling it improves the backtest by $7 and, at wider stops, by much more.

2. **The synthetic stop is far worse than the real one.** Live, exits tagged
   `stop_loss_synthetic` average **-$6.03** per loss on a ~$100 account. A 0.6%
   stop on a $100 position should lose about $0.60. This is a ten-fold overshoot.
   The synthetic stop is the in-loop fallback used when Alpaca rejects a stop
   order on a fractional position (`use_notional_market_entry=True`), and it only
   checks on the 10-second poll — so it exits at whatever the price is by the
   time the loop notices, not at the stop. This is an execution defect, not a
   strategy parameter.

Hold time tells the same story from another angle:

| bars held | trades | total P&L | win % |
|---|---|---|---|
| 0–2 | 723 | -101.66 | 15.2 |
| 3–5 | 662 | -40.75 | 21.1 |
| 6–10 | 788 | -28.22 | 20.3 |
| 11–20 | 783 | +18.58 | 23.0 |
| 21–40 | 422 | +51.98 | 36.5 |
| 41–80 | 172 | +34.42 | 49.4 |
| 80+ | 55 | +13.13 | 58.2 |

Every short-hold bucket loses and every long-hold bucket wins. That is the
signature of exits firing on noise rather than on information.

---

> **Companion document.** `docs/log_analysis.md` is an independent forensic
> analysis of the live logs (it recovers 87 round-trips that
> `trade_log_parser.py` silently drops, and reports bootstrap CIs rather than
> t-statistics). It was produced separately from this backtest study and reaches
> the same two primary conclusions — the trend-break exit and the synthetic stop
> are the significant money losers. Where the two documents differ in detail,
> the log analysis has the better ledger (184 matched trades vs 100) and this
> one has the counterfactual (it can re-run history with the rule removed).

## The live +$157 is five trades — and all five were accidents

The live log shows 189 round trips for +$159.57, 36.0% win rate, profit factor
1.97. That looks good. It is not significant:

- t-statistic on mean P&L: **1.76**
- P&L excluding the top 1 trade: +$106.30
- excluding the top 3: +$35.00
- excluding the top 5: **-$0.63**

On the current leveraged-ETF universe alone (63 trades, +$110.87), excluding the
top 3 gives **-$13.70** and excluding the top 5 gives **-$34.92**.

The entire live profit is a handful of take-profit hits — 19 trades produced
+$275.26 while everything else lost $115. With a 2:1 payoff and a hit rate this
low, that distribution is exactly what a zero-edge system with a wide target
produces. It is not evidence of a working strategy.

The log analysis pins down what those trades actually were, and it is worse than
"lucky". Of 184 matched round-trips, **173 that opened and closed the same day
made +$22.79 in total** (PF 1.18, bootstrap CI [-$0.29, +$0.57] — straddling
zero). The **11 trades held across a session boundary made +$135.64**: 86% of
all profit from 6% of the trades. All five of the trades whose removal flips the
bot to a loss are in that group.

Those overnight holds were not a strategy. They happened because the process was
killed before the end-of-day flatten could run — the log has 89 `Bot starting`
lines against only 21 end-of-day cutoff firings. `_should_end_of_day_exit` only
works while the process is alive, so the shutdown path (`stop_bot.py`, max
runtime) and the startup reconcile both need to force a flatten. As it stands
the bot's entire track record is **unmanaged overnight gap risk on 3x leveraged
ETFs**, and the worst such hold lost -$19.92 on a single gap — 2.4% of notional,
four times the intended 0.6% stop.

Note also that most live trades came from the *old* universe (SPY/QQQ/IWM/VTI/
DIA, 124 trades) rather than the current leveraged-ETF one (63 trades). The live
record for the configuration that is actually running is 63 trades long.

---

## Bugs found

### 1. Three `.env` settings were silently ignored — **fixed**

`STOP_LOSS_PCT`, `TAKE_PROFIT_PCT` and `TRAILING_STOP_PCT` are set in `.env` and
documented in `.env.example`, but `load_config()` never read them. `.env` said
`TAKE_PROFIT_PCT=0.009`; the bot was running the 0.012 default.

Fixed in `config.py`. `.env` was left at `TAKE_PROFIT_PCT=0.012` so that fixing
the plumbing does not silently change live behaviour — 0.009 was never in force,
so adopting it now would be a change, not a restoration.

### 2. Unadjusted split data — **fixed**

`research/data.py` originally fetched Alpaca's default RAW bars. SOXS
reverse-split twice in the sample, appearing as single bars of **+1,963%** and
**+873%**. Any momentum strategy reads those as the trade of the century. The
first full backtest run reported -74.63 on corrupt data; on split-adjusted data
the same config gives -52.52, and SOXS goes from -37.58 to +9.68.

Fixed: `adjustment="all"` by default, with the adjustment in the cache key so
raw and adjusted bars can never be mixed.

### 3. `max_entry_attempts_per_day` is dead config

`config.py:153` defines it and `bot.py:1470` increments the counter, but nothing
ever compares the two. The documented "3 entries per day" cap does not exist.
The backtest reproduces the real behaviour (unlimited) by default; pass
`enforce_max_entry_attempts=True` to see what the intent would do.

### 4. Operational instability

From `app/bot.log` (86,197 lines, ~3.5 months):

| event | count |
|---|---|
| `stale_market_data` | 14,259 |
| `unhandled_loop_error` | 1,509 |
| `stop_place_failed_safe_stop` | 808 |
| HTTP 401 with an HTML body | 814 |
| `/v2/orders` 422 | 647 |
| `clock_fetch_failed` | 579 |
| `bot_restart` | 89 |
| `stop_place_failed_halt` | 44 |
| `stale_leg_removed` | 38 |

808 failures to place a protective stop is the one to worry about: each is a
position that was open without broker-side protection, falling back to the
in-loop synthetic stop — the same mechanism that is losing $6 a trade above.

The entry diagnostics also show `spread_too_wide` as the single largest
rejection reason (539), ahead of `momentum_below_threshold` (360). Real spreads
on this universe frequently exceed the 10 bp `max_spread_pct` gate, which means
**the backtest's 2 bp constant spread is optimistic** — the real cost of trading
these names at 1–3 share size is higher than modelled.

---

## The universe is structurally wrong for a long-only strategy

Buy-and-hold over the sample:

| symbol | start | end | change |
|---|---|---|---|
| SOXL | 42.02 | 112.89 | **+168.7%** |
| TQQQ | 52.49 | 71.88 | +36.9% |
| SQQQ | 66.80 | 38.52 | -42.3% |
| UVXY | 35.80 | 18.02 | **-49.7%** |
| SOXS | 615.34 | 48.99 | **-92.0%** |

Three of the five names are long-only-hostile by construction. UVXY holds VIX
futures in perpetual contango and bleeds regardless of timing; SQQQ and SOXS are
daily-rebalanced inverse funds whose volatility decay compounds against a
long holder. A long-only momentum bot on this universe spends most of its time
buying instruments with a large, permanent negative drift.

The universe is also two inverse pairs: TQQQ/SQQQ track the same index in
opposite directions, as do SOXL/SOXS. A long-only ranker choosing among them is
largely picking which side of the same bet to take — the diversification is
illusory, and when it holds both it is paying two spreads to be flat.

---

---

## Walk-forward: nothing survives out of sample

The definitive test. 15 folds, 42-day training window, 14-day test window. All
192 parameter combinations are swept inside each *training* window; the winner
is then run, untouched, on the following two weeks it has never seen.

| | in-sample | out-of-sample |
|---|---|---|
| total P&L | **+442.40** | **-59.95** |
| trades | 3,166 | 981 |
| mean expectancy | +0.185 | -0.088 |
| mean profit factor | 1.462 | 0.797 |
| mean win rate | 37.6% | 31.4% |
| mean Sharpe | +4.99 | -3.49 |
| folds positive | 15 / 15 | **3 / 15** |

Every single training window produced a profitable, high-Sharpe configuration.
Twelve of fifteen then lost money in the fortnight that followed. This is the
textbook signature of a search fitting noise: the optimizer is not finding a
setting that works, it is finding whichever setting happened to match the last
six weeks of randomness.

The chosen parameters bear that out — they are unstable fold to fold:

| test window | stop | target | trend-break | open delay | momentum lookback |
|---|---|---|---|---|---|
| 2026-02-12 | 0.025 | 0.012 | off | 5 | 10 |
| 2026-02-26 | 0.010 | 0.035 | off | 5 | 20 |
| 2026-03-12 | 0.006 | 0.060 | off | 5 | 5 |
| 2026-03-26 | 0.025 | 0.060 | off | 30 | 10 |
| 2026-04-09 | 0.025 | 0.060 | off | 30 | 10 |
| 2026-04-23 | 0.015 | 0.060 | off | 5 | 10 |
| 2026-05-07 | 0.015 | 0.035 | on | 30 | 5 |
| 2026-05-21 | 0.025 | 0.035 | off | 30 | 5 |
| 2026-06-04 | 0.025 | 0.060 | off | 5 | 5 |
| 2026-06-18 | 0.025 | 0.020 | off | 5 | 5 |
| 2026-07-02 | 0.025 | 0.060 | off | 30 | 20 |
| 2026-07-16 | 0.006 | 0.060 | on | 30 | 20 |
| 2026-07-30 | 0.025 | 0.060 | on | 5 | 10 |
| 2026-08-13 | 0.015 | 0.035 | on | 5 | 20 |
| 2026-08-27 | 0.010 | 0.060 | on | 5 | 10 |

`momentum_lookback_minutes` is picked 5/10/20 almost uniformly (5, 6, 4 times) —
the parameter at the heart of the entry signal has no stable optimum, which is
what you would expect if it carries no information. `stop_loss_pct` ranges over
all four grid values.

Two weak tendencies do persist: a wide take-profit (0.06 chosen in 9 of 15) and
the trend-break exit switched off (10 of 15). Those match the ablations and the
live log, so they are the parts of this study worth acting on. Everything else
in the grid is noise.

**Conclusion: there is no parameter setting of the current strategy that is
profitable out of sample.** This is not "we have not searched hard enough" — a
harder search makes the in-sample number better and the out-of-sample number
worse.

---

## What a parameter search found, and why it should not be trusted

An in-sample grid over 156 valid combinations of stop, target, trend-break
on/off, open delay and momentum lookback produced 21 profitable combinations.
The best:

```
stop_loss_pct=0.025  take_profit_pct=0.060  enable_trend_break_exit=False
market_open_delay_minutes=5  momentum_lookback_minutes=5
-> 496 trades, +62.42, PF 1.176, Sharpe 1.98, max DD -29%
```

Three reasons not to believe it:

1. **Selection.** Best-of-156 at +2.78σ against the coin-flip control is
   approximately the expected maximum of 156 draws from a standard normal. The
   significance is entirely consumed by the search.
2. **One symbol.** Per-symbol P&L at that config: SOXS +42.92, SOXL +6.70,
   TQQQ -3.53, SQQQ -13.45, UVXY -38.70. Drop SOXS and the config makes
   **-16.51**. Four of five names lose.
3. **It is not really a strategy.** Average hold is 200 bars and 270 of 496
   exits are end-of-day. At a 2.5% stop and 6% target on these instruments, the
   exits almost never trigger — it is "buy something in the morning, sell at the
   close", with the entry signal shown above to be worthless.

Walk-forward confirms all three: the same grid that produces +$442 in-sample
produces -$60 out-of-sample.

The direction of the result is still informative even if the magnitude is not:
wide stops and no trend-break exit beat tight stops and structure exits, in
every neighbourhood of the grid, not just at the winning cell. That is
consistent with the volatility measurements and with the exit attribution. It is
evidence about the *exits*, not about the entries.

---

## What I would actually do

Ordered by expected value, and honest about which are supported.

**Supported by the evidence here:**

1. **Turn off the trend-break exit** (`ENABLE_TREND_BREAK_EXIT=false`).
   Significant loser in the backtest (1,695 trades) and in the live log
   (t = -4.89). This is the clearest single result in the whole study.
2. **Stop using notional/fractional entries, or fix the synthetic stop.** Live
   synthetic-stop exits lose $6.03 each against an intended $0.60 risk. Either
   size to whole shares so Alpaca accepts a real stop order, or make the
   synthetic stop check on every quote rather than on the 10-second poll.
3. **Drop UVXY, and probably SOXS and SQQQ, from a long-only universe.** They
   have large permanent negative drift; UVXY alone is -38.70 at the best config
   and -30.43 at the live config.
4. **Widen the stop to at least 1σ of the instrument's 10-minute volatility, and
   size down to compensate.** A fixed 0.6% across TQQQ (10-min σ 0.64%) and SOXL
   (1.53%) is not one setting, it is two very different bets. ATR-scaled stops
   are the standard fix.
5. **Turn off the online and offline trainers**
   (`ENABLE_OFFLINE_TRAINING=false`, `ENABLE_ONLINE_TRAINING=false`). The log
   analysis takes these apart in detail: both write to the *same* two state
   fields with different step sizes and lookbacks, so whichever ran last wins;
   the output is a three-valued toggle that cannot converge; `_PNL_RE` matches
   `exit_pnl=` on any line so the training window includes phantom and orphan
   exits; and the whole log is re-read on every process start, which is why 89
   restarts produced 97 "training applied" events on unchanged data. Empirically
   the knob moved 56 up and 46 down across 212 events — a coin flip — and every
   value ever written was negative, i.e. four months of "learning" only ever
   loosened the gates until they sat against the clamp that exists specifically
   to stop the bot buying dips.

6. **Force a flatten on shutdown and on startup reconcile.** See above: 86% of
   the live P&L came from positions the bot failed to close, and that exposure
   is uncompensated overnight gap risk, not edge. It cuts both ways — the same
   mechanism produced the -$19.92 worst trade.

**Not supported — do not do these yet:**

7. Do not deploy the +62 grid winner. See above.
8. Do not add features (VWAP, RSI, ATR bands, ML) to the current entry rule
   while it is indistinguishable from random. Adding inputs to a zero-edge model
   produces a better-fitting zero-edge model.

**The structural problem:**

A $100 account trading whole shares of $40–$110 instruments holds 1–3 shares.
Every round trip pays a spread that the log shows frequently exceeds 10 bp,
against a target of 1.2%. That is roughly 8% of the target consumed by costs per
trade before any edge — and at 3,600 trades a year, costs alone dominate. Account
size is a binding constraint on what is achievable here, independent of the
strategy.

---

---

## Applied changes (2026-09-03)

All five recommendations below were implemented.

| # | Change | Where |
|---|---|---|
| 1 | Trend-break exit off | `.env` `ENABLE_TREND_BREAK_EXIT="false"` |
| 2a | Floor entry quantity to whole shares | `bot.py:_compute_qty_for_entry` |
| 2b | Failed broker stop now falls back to a synthetic stop instead of leaving the position unrecorded | `bot.py:_open_leg_after_buy_fill` |
| 3a | Flatten on `KeyboardInterrupt` instead of just saving state | `bot.py:run_forever` |
| 3b | Flatten positions carried across an ET date boundary at startup | `bot.py:run_forever` startup reconcile |
| 4 | Universe cut to TQQQ + SOXL; `MAX_OPEN_POSITIONS` 3 → 2 | `.env` |
| 5 | Both trainers off | `.env` `ENABLE_OFFLINE_TRAINING`/`ENABLE_ONLINE_TRAINING="false"` |

New config flags `FLATTEN_ON_SHUTDOWN` and `FLATTEN_STALE_POSITIONS_ON_START`,
both defaulting true.

**2a is the root cause of a cluster of live failures.** `_compute_qty_for_entry`
returned `round(qty, 6)` — a fractional quantity — and fed it straight into a
*limit* buy. Alpaca rejects fractional limit orders, which is the 647 `/v2/orders
422` errors in the log. The fractional fills that did get through could not carry
a broker stop (also whole-share only), which forced the synthetic in-loop stop,
which is the mechanism losing ~$6 per stop-out. One missing `floor()` produced
the order rejections, the 808 failed stop placements, and the -$78 synthetic-stop
line item.

### Measured effect, same 8 months

| config | spread | trades | total P&L | win % | PF | max DD |
|---|---|---|---|---|---|---|
| before | 2 bp | 3,605 | -52.52 | 23.9 | 0.893 | -60.3% |
| before | 4 bp | 3,711 | -44.48 | 25.1 | 0.914 | -53.8% |
| before | 10 bp | 3,604 | -51.81 | 26.8 | 0.895 | -56.4% |
| **after** | 2 bp | 1,370 | **-28.02** | 34.2 | 0.915 | **-32.1%** |
| **after** | 4 bp | 1,394 | -27.71 | 34.0 | 0.920 | -32.1% |
| **after** | 10 bp | 1,323 | -27.83 | 33.6 | 0.913 | -31.6% |

Loss roughly halved, drawdown roughly halved, win rate up ten points, trade count
down 62%, and the result is now stable across cost assumptions rather than
drifting with them.

**It is still negative, and it was always going to be.** Per-trade expectancy
actually got slightly worse (-0.0146 → -0.0205); the total improved because the
bot trades far less. These five changes stop the bot bleeding through defective
exits, unprotected positions and rejected orders. None of them give it an edge,
because the entry signal is still the coin flip measured at the top of this
document.

Fixes 2b, 3a and 3b cannot show up in a backtest at all — the backtest already
assumes whole shares, never has an order rejected, and never crashes. Their value
is in closing the gap between backtest and live: no more orphaned positions, no
more accidental overnight holds.

### One consequence to be aware of

Whole-share sizing plus the trimmed universe means that at current prices
(TQQQ $71.88, SOXL $112.89) a $100 account **cannot buy a single share of SOXL**.
Replaying August alone: 97 trades, all TQQQ, all quantity 1. The bot is now
effectively a one-share TQQQ bot.

That is honest sizing rather than a regression — the previous behaviour "worked"
only by submitting fractional orders that Alpaca rejected or that could not be
protected by a stop. But it does mean the universe is one instrument in practice.
Raising account capital, or adding a lower-priced instrument with positive drift,
is what would restore breadth.

---

## What this study cannot tell you

- Whether the strategy works in a different regime. Eight months is one draw.
- **The live bot and the backtest read different tapes.** Every backtest here is
  built on SIP (full consolidated tape). The live bot cannot use it: this
  subscription permits SIP for *historical* bars only and answers a request for
  recent SIP data with `403 "subscription does not permit querying recent SIP
  data"`. Live therefore runs on IEX, a ~2% volume venue. The entry score is
  `momentum_return * log1p(volume_ratio)`, and the volume term in particular is
  measuring something materially different live than in any backtest above.
  Closing this gap needs a market-data subscription upgrade, not a config change.
- Whether real fills match the model. Spread and slippage are modelled constants
  (2 bp / 1 bp); the live log's 539 `spread_too_wide` rejections say real
  spreads are often wider. Every backtest number here is therefore optimistic.
- Whether a *different* entry signal has edge. Nothing here tests one; it only
  establishes that this one does not.
- Anything about short entries. The bot is long-only and so is the backtest.
