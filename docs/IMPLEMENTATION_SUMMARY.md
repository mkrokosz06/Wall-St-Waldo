# Implementation summary — correctness and validation phases

Against `docs/CLAUDE_IMPLEMENTATION_PLAN.md`. Written 2026-09-15.

No bot process was started, no broker order was submitted or cancelled, no
paper or real holding was flattened, and no credential script was run. All
verification is offline against fakes.

## Test command

```bash
cd app
python -m pytest tests research/test_backtest.py research/test_fastsig.py -q
```

**184 passed.** 145 new offline tests in `app/tests/` plus the pre-existing 39
research tests, all still green. `test_auth.py` and `test_data_auth.py` are
deliberately not collected — they use real credentials.

## What was completed

| Phase | Status | Where |
|---|---|---|
| 1 — durable order tracking and fill accounting | **Module + migration done; not yet wired into `bot.py`** | `app/order_ledger.py`, `app/state_store.py` |
| 2 — confirmed cancellation and real protection | **Module done; not yet wired** | `app/protection.py` |
| 3 — safe position lookup and verified flattening | **Done** (lookup); shutdown-ownership not done | `app/bot.py` |
| 4 — one session policy | **Module done; not yet wired** | `app/session_policy.py` |
| 5 — effective configuration and training flags | **Done** | `app/bot.py`, `app/config.py` |
| 6 — per-symbol freshness and quote validation | **Module done; not yet wired** | `app/data_validation.py` |
| 7 — reserve portfolio risk before entering | **Module done; not yet wired** | `app/risk_budget.py` |
| 8 — repair research execution and accounting | **Done** | `app/research/execution.py`, `backtest.py`, `controls.py` |
| 9 — documentation and regression checks | **Done** | both READMEs, `.env.example`, `FINDINGS.md`, this file |

### Read this before trusting the table

Phases 1, 2, 4, 6 and 7 exist as tested modules that are **not yet called by
the live trading loop.** Every invariant in them is proven offline, but
`bot.py` still uses its original top-level `ENTRY_PENDING`/`EXIT_PENDING` state
machine, its own 15:55 cutoff, its portfolio-wide staleness guard and its
realized-loss-only daily check.

This was a deliberate sequencing decision, not an omission. Replacing the order
lifecycle inside a 2,400-line file that currently manages real positions is the
single riskiest change in the plan, and doing it in the same pass as five new
modules would have made a failure impossible to localise. The modules are the
part that has to be exactly right; wiring them is mechanical but must be done
with the migration tests in front of you.

**Practical consequence:** the engineering defects the plan describes are fixed
in *design and test* but, for phases 1/2/4/6/7, not yet in the running bot.

## Behaviour changes to be aware of

1. **`max_entry_attempts_per_day` is now enforced, and its default is 3.** It
   was dead config — defined, incremented, never compared — so every historical
   run and every backtest had unlimited entries. The backtests place roughly
   2–8 entries per session, so a cap of 3 will materially reduce trading. Set
   `MAX_ENTRY_ATTEMPTS_PER_DAY=0` for the previous behaviour. `.env` was not
   edited, so the bot will start with the cap active.
2. **Persisted `dynamic_*` thresholds are ignored while both trainers are off,
   and cleared at startup.** Live state carried score and momentum floors of
   -0.00025 from a since-disabled trainer; a negative momentum floor inverts the
   entry gate. Trade history is preserved.
3. **The backtest's default execution model changed** from what is now called
   `legacy` to `spread_aware`. Results move, in the pessimistic direction. Pass
   `execution_model="legacy"` to reproduce anything published earlier.
4. **New config:** `QUOTE_STALE_MAX_AGE_SEC` (default 30). New validation
   rejects a non-positive stop distance, take-profit, or staleness window, and
   a negative attempt cap. Reward-to-risk below 1:1 remains valid.

## Migration behaviour

`BotState` gained `ledger`, `schema_version` and `migration_notes`, written by
the existing atomic replace so state and ledger cannot diverge after a crash.
`migrate_if_needed` is idempotent.

Exercised against the real `app/state.json` shape. It recovers the SOXL basis,
carries the already-booked `daily_realized_pnl` into its own ET day **without
replaying historical fills** (replaying would double-count against a figure
that is already final), and records three notes rather than guessing:

- the legacy TQQQ leg has an `entry_avg_price` but no quantity, so quantity must
  come from broker reconciliation;
- the carried P&L is attributed to day `2026-09-04`;
- the legacy stop is recoverable by broker id only, so protection is unverified
  until reconciled.

Legacy state with no day attribution books its P&L as `UNRESOLVED`, which
blocks new entries until a human resolves it.

## Corrections made to existing documentation

Both were claims in `FINDINGS.md` that this work showed to be wrong.

1. **The synthetic-stop "tenfold overshoot" was wrong**, as the plan said. The
   document compared a -$6.03 average loss to a $0.60 intended risk, assuming
   ~$100 of notional. Measured across all 207 `ENTRY filled` lines in
   `bot.log`, mean notional is **$815.25**, so a 0.6% stop intends **$4.89** and
   -$6.03 is a **~23% overshoot**. Direction survives, magnitude does not.
2. **"Insensitive to execution assumptions" was the bug, not a property.** I had
   recommended `0.015/0.035` partly because its result barely moved across 2/4/10
   bp of spread. That was because entries never paid the spread at all: under
   `legacy` the round-trip cost is 0.020% regardless of `spread_bps`. Corrected,
   the same config goes from +$8.32 to **-$20.48** at 10 bp. The
   recommendation stands on narrower grounds — it loses far less than the old
   config (-$20.48 against **-$52.91**) — but it is not near breakeven.

Also corrected: `controls.py` ran **5** seeds with no count-matching while
`FINDINGS.md` claimed "8 seeds, count-matched", and `record_equity="day"`
returned an **empty** curve whenever postmarket bars trailed the session.

## Not done, and why

- **Wiring phases 1/2/4/6/7 into `bot.py`.** See above. This is the main
  remaining task.
- **Exclusive execution ownership** (Phase 1's process lock, Phase 3's
  cooperative shutdown handshake). `stop_bot.py` can still trade and rewrite
  state while the runner is alive. Needs a lock file with liveness checking plus
  a shutdown protocol; it is a coherent piece of work that belongs with the
  wiring.
- **Broker API extensions** for lookup by client id, terminal order history and
  paginated executions. `order_ledger` already consumes timestamped executions
  when given them, and the fake broker provides them, but
  `AlpacaTradingREST` has no such methods yet.
- **Verifying the fractional order-type claim against current Alpaca docs.**
  Phase 2 asks for this. It needs a live documentation fetch, which I did not
  do, so the existing comments are unverified either way. The whole-share policy
  is unchanged regardless.
- **ATR-scaled stops and the other research-backlog items.** Correctly out of
  scope here, and now blocked behind re-running the baseline under
  `spread_aware`, since every prior number is optimistic.

## Practical limits worth stating

- No code can guarantee liquidation after an abrupt process kill or during a
  broker outage. The flatten paths reduce that window; they do not close it.
- A risk budget is not a maximum realized loss. Stops gap through and slippage
  can exceed the modelled distance.
- The backtest cannot reproduce broker cancellations, latency, or fill-queue
  position, and it reads SIP while the live bot necessarily reads IEX.
- An offline replay proving an invariant is not evidence the live bot honours
  it — that is what the unwired phases mean. A monitored paper session is the
  next step, and it should be watched rather than assumed.
