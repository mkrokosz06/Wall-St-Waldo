# Claude implementation handoff: trading-bot correctness and validation

Prepared from a read-only review on 2026-09-15.

Project root: `C:/Users/mkrok/OneDrive/Desktop/Trade Bot`.

## Task

Implement the required phases below, in order, and verify them with deterministic offline tests. This is an implementation assignment, not a request for another proposal. Complete the code, state migration, tests, and documentation. Make routine implementation decisions yourself within these requirements; record material assumptions in the final implementation summary.

The objective is reliable order tracking, accurate realized P&L, enforceable risk controls, and research results that describe the actual execution assumptions. Improving profitability is a separate research question. Do not represent engineering fixes or backtest improvements as proof of a profitable strategy.

## Working boundaries

- Read `CLAUDE.md`, any applicable `AGENTS.md`, and the current code before editing. Function names below are navigation anchors; line numbers may have changed.
- Inspect `git status` and `git diff`. There was an existing user change in `app/config.py`: it permits take-profit distances smaller than stop distances, warns about that relationship, and validates positive take-profit distance. Preserve that work. A target smaller than a stop is not inherently an invalid configuration.
- Do not launch the bot, restart its process, invoke dashboard trading actions, submit/cancel broker orders, flatten real/paper holdings, or run credential/auth scripts as part of this implementation. Use a fake broker. A monitored paper run is a subsequent operational step.
- Do not edit the user's `.env`, credentials, active `state.json`, existing logs, or historical result files. Exercise migrations on copied fixtures in temporary test directories. New code should migrate state safely when the user subsequently starts it.
- Keep paper/live credentials and state separated in any new persistence identity. Never print secrets or include them in reports/config snapshots.
- Preserve the current strategy settings. Do not enable trailing/structure exits, fractional entries, extended hours, or training; change the universe; widen stops; change targets; or reduce/increase the user's configured dollar loss limit as a side effect of fixing execution.
- Avoid unrelated UI redesigns, dependency upgrades, and a wholesale strategy rewrite. Small modules for order accounting, session policy, data validation, and risk calculations are appropriate where they reduce duplicated logic.

## Baseline observed during review

These are saved local settings, not confirmation of a running process or broker account state. Recheck safe, non-secret configuration fields before implementing.

| Setting | Observed value |
|---|---|
| Mode | Paper |
| Universe | TQQQ, SOXL |
| Entry | Whole-share limit buy near bid |
| Signal | 10-minute momentum multiplied by `log1p(volume_ratio)`, spread/volume/trend filters, optional candle bonus |
| Maximum positions | 2 |
| Stop / target | 0.006 / 0.012, meaning 0.6% / 1.2% |
| Maximum daily realized loss | -200 dollars |
| Maximum risk per trade | 5 dollars from config default |
| Portfolio notional cap | 10,000 dollars |
| Opening delay | 5 minutes |
| Trailing, trend-break, candlestick exits | Disabled |
| Time stop | Disabled |
| Offline and online training | Disabled |
| Data feed | IEX |
| Maximum bar age | 150 seconds |
| Saved dynamic thresholds | Score and momentum both -0.00025, despite disabled training |

The README contains older settings, including a different universe, loop interval, and a -20-dollar daily loss limit. The current code plus effective configuration take precedence. Update documentation to distinguish defaults from configurable examples; do not silently reset settings to README values.

## Read these areas

- `app/bot.py`: initialization; `run_forever`; `_sync_position_legs_with_broker`; `_manage_one_symbol`; all pending-order handlers; order submission/cancellation helpers; `_flatten_symbols`; `_flatten_and_stop`; entry selection and sizing.
- `app/state_store.py`: current JSON schema, legacy fields, serialization, atomic replacement.
- `app/alpaca_client.py`: broker endpoints, quote timestamps, data feed selection.
- `app/config.py`, `app/offline_trainer.py`, `app/strategy_signals.py`.
- `app/run_bot.py`, `app/run_bot_for.py`, `app/stop_bot.py`, `app/dashboard_runtime.py`, `app/dashboard_app.py`, `app/status_bot.py`, `app/trade_log_parser.py`: compatibility with state, shutdown, and logs.
- `app/research/backtest.py`, `fastsig.py`, `controls.py`, `sweep.py`, existing research tests, `FINDINGS.md`, and `docs/log_analysis.md`.

## Required invariants

1. Every broker execution affecting a managed trade is accounted for exactly once, including partial fills and fills discovered after restart.
2. A missing position does not prove that its closing fills have been accounted for. A missing API response does not prove that an order failed.
3. Pending buy, pending sell, and protective-order state cannot overwrite each other, whether they concern one symbol or different symbols.
4. Cancellation is complete only when broker state confirms a terminal outcome. `pending_cancel` is still live.
5. An order ID alone does not establish protection. Protection requires an appropriate active order and quantity.
6. Bad entry data blocks affected entries while reconciliation, risk management, and closing-time actions continue.
7. At and after the closing cutoff, no new buys are submitted. Pending buys are reconciled, and any late fills are included in liquidation.
8. Unknown order state, unknown basis, and unresolved liquidation are represented explicitly. They are never reported as successful cancellation, zero P&L, or a flat portfolio.
9. Existing and pending exposure reserve risk before another entry can consume it.
10. Backtest costs, cash, realized P&L, and marked equity must reconcile under a documented execution model.

## Phase 1 — Durable order tracking and fill accounting

### Problems to fix

`_sync_position_legs_with_broker` removes absent holdings before `_manage_open_positions` can inspect their stop orders. A stop that fills between polls therefore disappears without updating daily P&L or cooldowns.

The single top-level `ENTRY_PENDING` / `EXIT_PENDING` state can forget an outstanding order on restart, or when one symbol exits while another is entering. Several exit paths also use cumulative order fills without durable per-order accounting.

### Implementation requirements

- Introduce durable order records, keyed by broker order ID and/or persisted client-order ID, associated with a managed trade and symbol. Maintain independent pending entry, protective, and exit records. A portfolio status may remain for dashboard compatibility, but it must be derived and must not gate whether an individual order is reconciled.
- Persist order intent and a unique stable client-order ID before submission. On an uncertain submission result, query/reconcile that identity before retrying. Do not issue a new client ID and assume the first submission failed.
- Extend `AlpacaTradingREST` only as needed for lookup by client ID, terminal order history/executions, and session information. Handle pagination where required. Verify broker semantics against official documentation.
- Reconcile tracked orders and their executions before pruning position records or deciding that startup is flat. Also recover bot-created outstanding orders that are absent from local records after a crash.
- Distinguish a successful, validated empty broker snapshot from an exception, malformed response, incomplete pagination, or unresolved lookup. Only the former establishes absence. Failed retrieval must preserve exposure, order records, and risk reservations and block new entries requiring that information.
- Prefer broker execution IDs when available. If using cumulative order snapshots, persist processed cumulative filled quantity and cumulative filled value. Compute deltas from both values. For example, cumulative 2 shares at $100 average followed by cumulative 5 shares at $101 average means 3 additional shares and $305 additional value, not $303.
- Account for partial exits immediately in daily realized P&L. Keep the remaining position and its cost basis. Finalize one completed-trade result only after the trade's exposure and relevant in-flight orders are resolved. Apply cooldown/training completion events once, not once per partial fill.
- Assign financial results to the execution's America/New_York date, not the discovery date. Late historical executions must not consume today's loss allowance or create a new cooldown starting at discovery time. Calculate any still-applicable cooldown from the actual final exit time.
- A cumulative quantity/value delta cannot establish day attribution if its newly discovered fills span ET dates. Recover timestamped executions for that case; if unavailable, retain explicit unresolved attribution and block new entries that depend on an uncertain daily allowance. Do not assign the whole delta to the latest fill timestamp.
- Use a consistent fee policy. Per-order estimates must not be deducted on every repeated poll or every partial fill. Clearly distinguish estimated fees from broker-reported fees.
- Make processed executions, accounting totals, and completion markers recoverable as one atomic persistence update or through an explicitly replayable journal. Preserve the existing atomic-write property; avoid separate files that can disagree after a crash without a recovery procedure.
- Ensure only one process owns order execution and state writes for the same account/state identity. Use an exclusive lock or equivalent verified ownership mechanism; atomic file replacement alone does not prevent competing writers.
- Add a schema version and a tested migration from current single-order and `position_legs` state. Preserve basis, entry timestamps, day-start equity, cooldowns, and previously recorded P&L. Legacy state lacks an execution ledger: do not backfill and add already-booked historical fills blindly. Ambiguous historical accounting must be flagged for reconciliation rather than guessed.
- Do not clear intentional/risk-related halts merely because startup holdings are empty. Persist halt reasons where needed and clear each only when its underlying condition has been resolved. A daily reset must not erase outstanding orders or unresolved errors.
- Keep bot imports/test construction free of network calls. Inject a broker, market-data provider, clock, and temporary state store, or provide an equivalent clean test seam. Normal production startup can retain its authentication check.

### Acceptance tests

- Full stop fills between polls; broker positions are empty; loss, final exit time, cooldown, and one completed trade are recorded before the leg disappears.
- Poll the same fill repeatedly and restart after saving it: totals do not change again.
- Verify the 2-at-$100 then 5-at-$101 cumulative example exactly.
- Partial stop fill, confirmed cancellation, and a second sell for the remainder produce correct total P&L and one completed trade.
- A previous-day execution discovered today updates the correct day and does not reset today's risk budget or start a new cooldown.
- Recover partial fills spanning two ET dates correctly, or retain unresolved attribution when only cumulative data is available.
- A valid empty positions response permits reconciliation; a failed, malformed, or incomplete response cannot prune exposure or release reservations.
- Restart with an unfilled buy and no holdings: recover the buy and reserve its slot. Restart with a pending sell: track it without submitting a competing full-position sell.
- Symbol A's pending buy remains tracked while symbol B exits.
- Simulate a timeout after the broker accepts submission: recovery finds the original client ID and does not duplicate the order.
- Migrate representative legacy flat, pending-entry, pending-exit, and multi-leg states without resetting financial history.

## Phase 2 — Confirm cancellations and actual protection

### Implementation requirements

- Replace fixed sleeps used as evidence of cancellation with explicit order-state transitions. Poll with bounded retries/backoff or consume order updates, then reconcile. Never block the entire portfolio indefinitely while one order is unresolved.
- Treat `pending_cancel`, partial fill, rejection, expiration, and replacement as distinct outcomes. Record fills that race a cancellation before calculating a replacement quantity.
- Verify protective orders by symbol, side, order type, live status, price, and remaining covered quantity. Repair protection when a stored stop has expired, been canceled, been rejected, or covers only part of the actual holding.
- Reuse a valid broker stop discovered on startup instead of duplicating it. Do not let a stale canceled ID suppress recovery.
- A discretionary exit and its protective stop must share a controlled transition: resolve cancel/fill state and reserved sell quantity before submitting a sell for the remaining exposure. No overlapping full-position sell orders based on stale quantity.
- Preserve the current failure-policy intent: if broker protection cannot be established, record the holding and synthetic fallback explicitly, halt new entries, and continue reconciliation and recovery. A synthetic stop requires valid usable quotes and a running process; do not mark it as broker-protected. If data or broker access is unavailable, retain and report unresolved risk and retry; do not pretend liquidation occurred.
- Keep disabled paths safe: if trailing replacement is exercised in tests, a failed replacement must not leave a canceled stop ID treated as active. Do not enable trailing as part of this work.
- Existing comments claim all fractional limit/stop orders are unsupported. Current Alpaca Supported Order Types documentation lists fractional DAY limit and stop support. Verify current constraints before retaining blanket claims; do not add fractional trading or change the whole-share policy in this task.

### Acceptance tests

- Cancellation returns successfully but status remains `pending_cancel`; a subsequent fill is recorded, and the replacement does not oversell.
- Rejected, expired, canceled, and replaced stop IDs are not accepted as active protection.
- A two-share stop cannot be considered sufficient for a five-share holding.
- Additional entry fills and partial exits update coverage without duplicating sell reservations.
- A stop fills during cancel/replace; the system accounts for that fill and does not submit a replacement against nonexistent shares.
- Failed stop placement retains the actual position, activates the documented fallback/halt, and is visible to status reporting.

## Phase 3 — Safe position lookup, scope, and verified flattening

### Problem

`_flatten_symbols` is called for holdings outside the universe but reads quantity through `_position_for_symbol`, which searches only inside the universe. It can cancel protection and then sell nothing.

### Implementation requirements

- Separate broker lookup from entry-universe filtering. A requested symbol's quantity must come from all relevant broker holdings, even after that symbol is removed from `SYMBOLS_UNIVERSE`.
- Centralize target selection and ownership. Normal bot cleanup should include current managed trades and previously bot-owned holdings identified by persisted records/client-order IDs. Do not infer ownership solely because a symbol is outside the current universe. For ambiguous unrelated holdings, report them and preserve their orders; do not silently liquidate them.
- Ownership is quantity-aware. Broker positions can combine bot and manual shares in the same symbol. Track managed quantity and basis from the bot's executions; symbol membership alone never authorizes selling the entire net broker position. If legacy ownership cannot be separated reliably, retain an unresolved condition instead of inventing ownership or cost basis.
- Document this ownership rule and migration behavior for `FLATTEN_UNTRACKED_POSITIONS_ON_START`. Preserve an explicitly account-wide emergency-stop mode only as a clearly documented distinct operation. Do not broaden routine cleanup to all account orders/holdings.
- `_flatten_symbols(targets)` must operate only on those verified targets. Sequence cancel/reconcile/sell using the order machinery above, including partial fills and late entry fills.
- Make normal shutdown, maximum-runtime shutdown, startup cleanup, EOD, and the stop script use compatible logic. A submitted exit is not proof of flatness. Track and verify terminal orders and remaining quantities within a bounded shutdown window.
- The stop script must request cooperative shutdown from the current execution owner, or verify an exclusive takeover before it submits orders or changes state. It must not trade and rewrite state while the runner can still do the same. Concurrent stop requests must be idempotent; failure to acknowledge shutdown is not permission to become a second execution owner.
- If the market is closed, the broker is unavailable, or the shutdown deadline expires, persist pending work and report unresolved exposure. Do not falsely clear state. No code can guarantee liquidation after an abrupt process kill or during a broker outage; document that practical limit.

### Acceptance tests

- A bot-owned SQQQ holding of 10 shares, removed from a TQQQ/SOXL universe, is found and closed through the fake broker; its quantity never becomes zero merely because of filtering.
- Unrelated holdings/orders and non-target symbols are untouched by routine cleanup.
- With 10 bot-owned shares and 5 unrelated shares of the same symbol, routine flattening sells only the 10 managed shares and preserves unrelated orders.
- With startup-flatten flags disabled, no corresponding liquidation is initiated.
- Partial liquidation, a late entry fill, and a failed exit submission remain tracked until resolved.
- Unconfirmed shutdown reports unresolved exposure, never successful flatness.
- Concurrent stop requests and an unacknowledged running process cannot create competing order submitters or state writers.

## Phase 4 — One session policy for entries and closing

- Build a session policy from broker session/calendar information, with timezone-aware UTC/ET conversion. Support normal days, holidays, early closes, and daylight-saving changes. Cache session data appropriately; do not fetch a calendar for every symbol.
- Derive the closing cutoff from the actual session close minus `end_of_day_flat_minutes`. Preserve the configured opening delay.
- At and after cutoff, block new entries in the common entry gate and recheck immediately before submission. Cancel/reconcile pending buys; flatten any resulting fills and remaining managed positions.
- Reconciliation and closing actions must not sit behind the current `in_session` early return or a market-data freshness gate. When execution is unavailable after close, record pending/unresolved work rather than issuing repeated blind orders or declaring completion.
- Missing/untrusted session information blocks new entries while risk/order handling continues.
- Extended hours stays disabled. If retaining that option, do not treat a sell limit below market as a protective stop: it is marketable, not conditional. Either implement a tested explicit extended-hours policy or reject the unsupported combination in config validation.

### Acceptance tests

- Entry eligibility immediately before cutoff, at cutoff, and at 15:59 on a normal day; at/after cutoff no buy is submitted.
- An early-close session uses its early cutoff, not 15:55.
- The first loop after a pause resumes past cutoff and begins closing without opening a new trade.
- A buy fills while cutoff cancellation is pending; that fill is closed and accounted for.
- Stale/missing bars and quotes do not prevent broker-order reconciliation or an EOD liquidation attempt.
- Weekend/holiday/session lookup failure blocks entries.

## Phase 5 — Effective configuration and training flags

- Resolve effective entry score and momentum thresholds in one helper shared by actual selection, diagnostic messages, and config snapshots.
- If both trainers are disabled, ignore persisted dynamic thresholds and invalidate/clear their active overrides during state migration/startup. Preserve historical trade data.
- Record override provenance when training is enabled. Offline training seeds startup values when enabled; online training may replace them after its configured sample requirement is met. Overrides from a disabled source, an incompatible configuration, or unknown legacy provenance must not silently reactivate. Do not rewrite the training algorithm in this phase.
- Log a secret-free effective configuration snapshot including thresholds and their source, relevant flags, risk settings, feed, and strategy/config version.
- Validate positive stop distance, enabled target distance, nonnegative costs, sensible positive intervals/limits, and consistent lookbacks. Preserve valid reward-to-risk ratios below 1.
- `max_entry_attempts_per_day` currently exists but is never enforced. Wire it into `load_config` and the common entry gate. Its current default is 3; document that enforcing it changes previously unlimited behavior. Count distinct entry intents once, not retries of the same client ID. Reset on the ET session date and retain counts across restart.

### Acceptance tests

- Load -0.00025 overrides with both flags false: effective thresholds equal configured values; a negative-momentum candidate fails a configured zero floor.
- Diagnostics report exactly the values used for candidate selection.
- Restart/day reset do not restore disabled overrides; enabling only one source cannot restore the other's stale values.
- A target smaller than a positive stop remains valid; zero/negative stop distance does not.
- Entry-attempt limit survives restart and counts a recovered submission only once.

## Phase 6 — Per-symbol freshness and quote validation

- Validate each candidate's bars and quote independently. A fresh TQQQ bar must not authorize stale SOXL data, and bad SOXL data must not unnecessarily block valid TQQQ candidates.
- Use current advancing time. The present cached broker timestamp does not advance between refreshes; fix that with a clock abstraction/monotonic elapsed time or another explicitly tested method. Expired clock confidence must not authorize entries.
- Require completed bars for signals. Alpaca minute-bar timestamps denote interval start; account for the interval when checking completion. Preserve the existing `STALE_DATA_MAX_AGE_SEC` start-timestamp age interpretation for compatibility, and document it. Do not silently increase the user's configured tolerance to manufacture more trades.
- Add a separate configurable `QUOTE_STALE_MAX_AGE_SEC`, default 30 seconds. Validate quote timestamp and finite, positive bid/ask with `ask >= bid`; reject stale, missing-timestamp, or materially future-dated quotes for entries. Define/test a small explicit clock-skew tolerance rather than accepting arbitrary future timestamps.
- Validate again before order submission if elapsed work has made the originally selected snapshot too old. Do not mix unrelated quote/bar snapshots silently.
- Make stale/unavailable signal data explicit. Passing `None` must not cause exit helpers to refetch the same stale bars and bypass the decision to suppress structure signals.
- Continue managing broker stops, fills, outstanding exits, and EOD regardless of signal-data health. A quote-dependent synthetic stop or target must not act on a stale quote; retain the unresolved protection state and recovery policy.
- Diagnostics should identify symbol, data type, timestamp/age, and rejection reason without flooding logs.

### Acceptance tests

- Fresh TQQQ plus 20-minute-old SOXL rejects only SOXL.
- Fresh bars plus a stale quote, missing quote timestamp, crossed quote, NaN price, or nonpositive price reject the candidate.
- Quote-age boundary, bar-age boundary, unfinished minute bar, and future timestamp cases are explicit.
- Cached clock ages advance correctly; repeated checks cannot keep old data fresh indefinitely.
- A quote expires between selection and submission: no order is submitted from it.
- Signal-data failure does not disable independent position/order management or EOD.

## Phase 7 — Reserve portfolio risk before entering

- Keep the daily control as a realized-loss allowance plus reservations for potential stop losses; do not silently replace it with an equity-drawdown rule.
- Let `P` be the correctly recorded current-day realized P&L and `L` the negative configured loss floor. Remaining daily allowance is `max(0, P - L)`.
- Reserve nonnegative additional loss to the active/planned stop for managed open quantities, plus pending entry remainders and uncertain submissions. Use remaining cost basis, quantity, executable/rounded order prices, and the documented cost allowance. Do not use unrealized gains in one position to offset another position's reservation.
- Add `MAX_PORTFOLIO_OPEN_RISK_USD`; if unspecified, derive it from `max_risk_per_trade * max_open_positions`. Keep it separate from the notional cap. A new order's reserved risk must fit the per-trade limit, remaining daily allowance after existing reservations, and remaining portfolio-risk cap.
- Count pending entries against position slots and buying/notional capacity. Transfer reservation from pending to filled exposure without double counting. Release an unfilled reservation only after confirmed cancellation/rejection.
- Unknown/unprotected exposure prevents additional risk until resolved; do not count it as zero risk.
- Keep cash/notional limits and whole-share rounding. Use one pure risk calculation shared with research where practical. Stop slippage/gaps can exceed a modelled reservation; the budget is not a guaranteed maximum realized loss.

### Acceptance tests

- For loss floor -20, realized P&L -12, and existing reserved loss 6, a new order can reserve at most 2 dollars.
- Two entry intents cannot each consume the same remaining budget.
- A partial fill transfers, rather than duplicates, its reservation; a pending cancellation retains the remainder.
- A pending order uses a position slot even when broker holdings are empty.
- Cash cap, notional cap, risk cap, whole-share rounding, and a breached daily loss floor all constrain the final order.

## Phase 8 — Repair research execution and accounting

The current backtest is useful scaffolding, but it uses SIP while the bot uses IEX, approximates a 20-second limit order with minute bars, and does not consistently charge modeled spread to fills. Do not optimize parameters until these differences are explicit and the accounting tests pass.

### Implementation requirements

- Introduce named execution assumptions/modes in run configuration and saved results. Retain any old model only as explicitly labeled legacy behavior.
- Define a reference price, modeled bid/ask from spread, and adverse slippage separately. Market buys execute on the ask side plus slippage; market sells execute on the bid side minus slippage. Never charge spread twice if input prices are already bid/ask.
- A limit buy fills only under its documented eligibility model and never above its submitted limit, including slippage. Do not assume every candidate fills at the next open. Minute bars cannot establish whether a limit filled during the live 20-second window; label the approximation and leave exact timeout validation to quote/trade replay or later paper-order evidence.
- Model sell-stop gap-through execution and costs consistently. Evaluate take-profit triggers on the intended bid-side price and credit an executable modeled exit price. Preserve documented conservative intrabar ordering when a minute touches both stop and target.
- Charge fees consistently to cash, realized trade results, daily risk accounting, and equity. Reconcile any existing overlap between `fee_estimate_per_order` and simulator commission settings so a fee is charged once.
- Fix daily equity recording when postmarket bars follow the last regular-session bar. Preserve a correct final equity even when recorded output frequency is daily.
- Share sizing/effective-strategy helpers where practical. Maintain the fast/slow signal equivalence and no-lookahead guarantees. Do not claim an offline replay reproduces broker cancellations, latency, or the actual fill queue.
- Save a secret-free manifest with each new result: code/config version, starting equity, symbols, dates, feed, adjustments, execution mode, spread/slippage/fees, random seed, and any missing-data policy.
- Make research commands reproducible. Either actually implement the documented count-matched random control and seed count or correct those descriptions. Compare matched trading opportunities/exposure rather than interpreting unmatched raw P&L as evidence of signal quality.
- Update documentation to distinguish historical results from newly generated results. Do not overwrite old statistics with incompatible new runs without versioning their assumptions.

### Acceptance tests

- Fixed scheduled round trip with fixed quantity/reference prices: increasing spread changes execution cost by the expected amount. Hold the trade path fixed; full-strategy P&L need not be monotonic because spread can alter which trades occur.
- A buy limit never fills above its limit, including adverse slippage.
- A gap below a sell stop executes below the stop under the chosen model.
- With no remaining positions, final equity minus starting equity equals realized net P&L, including nonzero fees. With holdings, the difference also includes marked unrealized P&L.
- Adding irrelevant postmarket bars cannot erase the daily equity curve or reset final equity to starting capital.
- Existing no-lookahead and fast/slow equivalence tests remain passing.
- The same manifest, input data, and seed reproduce the same result.

## Phase 9 — Documentation, regression checks, and handoff

- Update both READMEs and `.env.example` for actual configurable behavior, newly enforced limits, new options, order recovery, and known execution limitations. Do not edit `.env`.
- Preserve dashboard/status/parser compatibility, or migrate those consumers in the same change. Keep existing human-readable entry/exit records working and add structured order/execution metadata for exact accounting. Avoid duplicate completed-trade rows after restart.
- Add deterministic tests under a dedicated offline path such as `app/tests/`. Use fake clocks, fake broker state transitions, synthetic prices, and temporary state paths. Do not write tests whose imports trigger authentication or network access.
- Run the existing research tests and new offline tests explicitly, e.g. from `app`: `python -m pytest tests research/test_backtest.py research/test_fastsig.py -q`. Inspect collection first. Do not broadly collect `test_auth.py` or `test_data_auth.py`; they concern real credentials/API calls.
- If pytest is absent, install development dependencies using the project's supported environment/permission flow, or report that environment blocker precisely. A successful ad-hoc smoke script is not a substitute for claiming the full suite passed.
- Existing tests requiring unavailable market-data cache may be reported as unavailable; retain synthetic coverage for every mandatory acceptance case and do not fetch broker data silently.
- Run the applicable syntax/import checks and inspect the final diff for accidental secrets, runtime-state edits, historical artifact changes, or lost pre-existing work.
- Leave a concise implementation summary listing phases completed, files changed, tests actually run, any skipped checks, migration behavior, and remaining practical limits. Do not claim the bot is deployed, profitable, or verified with real execution.

## Separate research backlog — no automatic strategy promotion

After the required engineering phases, the following are hypotheses to evaluate against the repaired fixed baseline:

1. Volatility/ATR-based stop distances, with position size reduced as stop distance increases to maintain the same dollar risk.
2. Alternative entry filters, profit targets, trailing behavior, and per-symbol treatment.
3. Training only if it demonstrably adds value on genuinely unseen periods after costs.

Do not enable these in the user's runtime configuration during this assignment. If adding experiment support, make it opt-in and record the exact settings. Use chronological training/validation/test separation, fit/tune on training data only, report all tested alternatives and out-of-sample costs/drawdowns, and compare against the fixed baseline and reproducible random-entry controls. A best in-sample result is not a deployment decision.

Historical caution: negative P&L grouped by stop/trend exit does not establish that removing those exits improves the strategy. The earlier research's claimed tenfold stop overshoot also used the wrong position size: historical notional was around $833, making a 0.6% planned loss about $5, not $0.60. Do not carry that claim into new documentation.

## Definition of done

- All required invariants and acceptance scenarios have corresponding passing offline coverage, or a specific unresolved blocker is disclosed.
- No fill is discarded merely because broker holdings are flat; duplicate polls/restarts do not duplicate P&L.
- Pending orders survive restart and independent symbol activity; unknown state remains explicit.
- Protection, cancellations, cleanup, EOD, data validity, disabled training, and portfolio reservations behave as specified.
- Existing user configuration changes are preserved, active runtime artifacts are untouched, and state migrations are tested.
- Research cost/equity tests reconcile and saved assumptions are reproducible.
- No bot process or broker trading action was initiated during implementation.

## Official references to consult

- [Alpaca order lifecycle and order types](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- [Alpaca fractional Supported Order Types](https://docs.alpaca.markets/us/docs/fractional-trading)
- [Alpaca market-data FAQ and bar timestamps](https://docs.alpaca.markets/us/docs/market-data-faq)

Check current official documentation when implementing an API contract. Existing code comments and historical API errors are not an authoritative specification of today's broker capabilities.
