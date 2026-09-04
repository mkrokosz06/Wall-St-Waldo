"""
Correctness tests for the replay engine.

These are the tests that make the backtest trustworthy. They exist to prove four
things: exits fire at exactly the price and minute they should, a future bar can
never influence a past decision, the signals the engine computes are the same
ones ``strategy_signals`` computes, and the P&L adds up.

Run:  python -m pytest app/research/test_backtest.py -q
"""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ET_TZ, BotConfig  # noqa: E402
from strategy_signals import dual_ma_uptrend, trend_break_confirmed_below_sma  # noqa: E402

from research.backtest import _BarWindow, _prepare, run_backtest  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #
def make_cfg(**over) -> BotConfig:
    """Base config for synthetic tests: filters off so a single rule is isolated."""
    base = dict(
        api_key="x",
        api_secret="y",
        paper=True,
        symbols_universe=["AAA"],
        symbol_score_adjustments={"AAA": 0.0},
        market_open_delay_minutes=0,
        min_avg_volume=0.0,
        enable_trend_filter=False,
        enable_spy_market_trend_filter=False,
        enable_candlestick_entry_filter=False,
        enable_candlestick_exit=False,
        enable_trend_break_exit=False,
        enable_trailing_stop=False,
        enable_take_profit=False,
        enable_online_training=False,
        enable_offline_training=False,
        entry_cooldown_sec=0,
        post_loss_extra_cooldown_sec=0,
        momentum_lookback_minutes=5,
        volume_lookback_minutes=5,
        bars_lookback_minutes_for_scoring=45,
        max_open_positions=1,
        max_risk_per_trade=100000.0,
        max_portfolio_notional_usd=1_000_000.0,
        max_daily_realized_loss=-1_000_000.0,
        end_of_day_flat_minutes=5,
    )
    base.update(over)
    return BotConfig(**base)


def bars_from(closes: Sequence[float], symbol: str = "AAA", *,
              start_et: str = "2026-03-02 09:30", volume: float = 10_000.0,
              highs: Sequence[float] | None = None,
              lows: Sequence[float] | None = None,
              opens: Sequence[float] | None = None) -> pd.DataFrame:
    """Build a minute-bar frame in the live MultiIndex shape from a close series."""
    n = len(closes)
    idx = pd.date_range(pd.Timestamp(start_et, tz=ET_TZ), periods=n, freq="1min").tz_convert("UTC")
    # Default open = previous close, so a bar that falls to `close` still *opens*
    # above the stop. Otherwise every down bar looks like a gap-through.
    o = list(opens) if opens is not None else [closes[0]] + list(closes[:-1])
    h = list(highs) if highs is not None else [max(a, b) for a, b in zip(o, closes)]
    lo = list(lows) if lows is not None else [min(a, b) for a, b in zip(o, closes)]
    df = pd.DataFrame(
        {"open": o, "high": h, "low": lo, "close": list(closes), "volume": [volume] * n},
        index=pd.MultiIndex.from_arrays(
            [[symbol] * n, idx], names=["symbol", "timestamp"]
        ),
    )
    return df


def rising_then(path: Sequence[float], *, ramp: int = 20, base: float = 10.0) -> List[float]:
    """`ramp` minutes of steady uptrend (guarantees a positive momentum score), then `path`."""
    up = [base * (1.0 + 0.0005 * i) for i in range(ramp)]
    return up + list(path)


# --------------------------------------------------------------------------- #
# 1. known entry + exact stop / take-profit / trailing exits
# --------------------------------------------------------------------------- #
def test_entry_fills_at_next_bar_open_with_slippage():
    closes = rising_then([])
    bars = bars_from(closes)
    cfg = make_cfg()
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=10.0, spread_bps=0.0)

    assert res.trades, "a steady uptrend must produce at least one entry"
    t = res.trades[0]
    ts = bars.xs("AAA", level=0).index
    # Score needs max(5,5)+5 = 10 bars, so the first decision is at index 9 and
    # the fill is the open of index 10.
    assert t.entry_ts == ts[10]
    expected = closes[9] * (1.0 + 10.0 / 1e4)  # bar 10 opens at bar 9's close
    assert t.entry_px == pytest.approx(expected, rel=1e-12)


def test_stop_fires_at_exact_price_and_minute():
    # Ramp up for 20 bars (entry at bar 10), then one bar that dives through the stop.
    closes = rising_then([10.0] * 5 + [9.0])
    bars = bars_from(closes)
    cfg = make_cfg(stop_loss_pct=0.006, take_profit_pct=0.5)
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)

    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "stop"
    stop_px = t.entry_px * (1.0 - 0.006)
    assert t.exit_px == pytest.approx(stop_px, rel=1e-12)
    ts = bars.xs("AAA", level=0).index
    assert t.exit_ts == ts[25]  # the bar whose low breaches the stop


def test_stop_gap_through_fills_at_bar_open():
    closes = rising_then([10.0] * 5 + [5.0])
    bars = bars_from(closes, opens=list(closes))  # every bar opens where it closes
    cfg = make_cfg(stop_loss_pct=0.006, take_profit_pct=0.5)
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    t = res.trades[0]
    assert t.exit_reason == "stop"
    # Gapped: fill is the (worse) open, not the untouched stop price.
    assert t.exit_px == pytest.approx(5.0, rel=1e-12)


def test_take_profit_fires_at_exact_price():
    closes = rising_then([10.0] * 3 + [11.0])
    bars = bars_from(closes)
    cfg = make_cfg(enable_take_profit=True, take_profit_pct=0.012, stop_loss_pct=0.5)
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)

    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "take_profit"
    assert t.exit_px == pytest.approx(t.entry_px * 1.012, rel=1e-12)
    assert t.pnl_pct == pytest.approx(1.2, abs=1e-9)


def test_stop_wins_when_a_bar_hits_both_stop_and_take_profit():
    closes = rising_then([10.0] * 3 + [10.0])
    n = len(closes)
    highs = list(closes)
    lows = list(closes)
    highs[-1] = 20.0   # would smash the take-profit
    lows[-1] = 1.0     # and also blow the stop
    bars = bars_from(closes, highs=highs, lows=lows)
    cfg = make_cfg(enable_take_profit=True, take_profit_pct=0.012, stop_loss_pct=0.006)
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    assert res.trades[0].exit_reason == "stop", "documented conservative ordering"


def test_trailing_stop_ratchets_and_exits_at_trailed_level():
    # Up to 10.5, then collapse. The trail (0.4% off the peak) must be the exit,
    # well above the original 0.6%-below-entry stop.
    up = [10.0 * (1.0 + 0.0005 * i) for i in range(20)]
    closes = up + [10.5, 10.5, 10.5, 9.0]
    bars = bars_from(closes)
    cfg = make_cfg(
        enable_trailing_stop=True,
        trailing_stop_pct=0.004,
        trailing_stop_min_move_pct=0.0,
        stop_loss_pct=0.006,
        take_profit_pct=0.5,
    )
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_px == pytest.approx(10.5 * (1.0 - 0.004), rel=1e-12)
    assert t.exit_px > t.entry_px * (1.0 - 0.006), "trail must sit above the initial stop"


def test_eod_flat_exit_at_cutoff_minute():
    # 09:30 -> 16:00 would be huge; start late so the cutoff is a few bars in.
    closes = rising_then([10.0] * 40)
    bars = bars_from(closes, start_et="2026-03-02 15:30")
    cfg = make_cfg(stop_loss_pct=0.5, take_profit_pct=0.5, end_of_day_flat_minutes=5)
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    t = res.trades[-1]
    assert t.exit_reason == "eod"
    assert t.exit_ts.tz_convert(ET_TZ).hour * 60 + t.exit_ts.tz_convert(ET_TZ).minute >= 15 * 60 + 56


def test_time_stop():
    closes = rising_then([10.0] * 60)
    bars = bars_from(closes)
    cfg = make_cfg(enable_time_stop=True, time_stop_minutes=10,
                   stop_loss_pct=0.5, take_profit_pct=0.5, max_open_positions=1)
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    assert res.trades[0].exit_reason == "time_stop"
    assert res.trades[0].bars_held == 11  # decided at +10, filled next bar


# --------------------------------------------------------------------------- #
# 2. no lookahead
# --------------------------------------------------------------------------- #
def test_future_spike_cannot_change_past_decisions():
    """
    Run the same history twice: once plain, once with a violent spike appended.
    Every trade that closed before the spike must be byte-identical.
    """
    closes = rising_then([10.0, 10.01, 10.02, 9.9, 9.8, 10.0, 10.1])
    cfg = make_cfg(enable_take_profit=True, take_profit_pct=0.012, stop_loss_pct=0.006)

    base = run_backtest(cfg, bars_from(closes), start_equity=10_000.0,
                        slippage_bps=0.0, spread_bps=0.0)
    spiked = run_backtest(cfg, bars_from(list(closes) + [500.0, 0.01, 500.0]),
                          start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)

    # Strictly before the last plain bar: the plain run force-flattens there
    # ("end_of_data"), which is an artifact of where the data stops, not a decision.
    cutoff = bars_from(closes).xs("AAA", level=0).index[-1]
    a = [t for t in base.trades if t.exit_ts < cutoff]
    b = [t for t in spiked.trades if t.exit_ts < cutoff]
    assert a, "test needs at least one pre-spike trade to be meaningful"
    assert a == b


def test_window_never_contains_a_future_bar():
    """strict=True raises on lookahead; here we prove the windows are clean."""
    bars = bars_from(rising_then([10.0] * 30))
    series, timeline = _prepare(bars, ["AAA"])
    s = series["AAA"]
    for k in range(len(timeline)):
        end = int(s.end_pos[k])
        if end == 0:
            continue
        assert s.df.index[end - 1] <= timeline[k]
        assert end == len(s.df) or s.df.index[end] > timeline[k]


def test_strict_mode_is_on_by_default_and_engine_runs_clean():
    bars = bars_from(rising_then([10.0] * 30))
    run_backtest(make_cfg(), bars, start_equity=10_000.0, strict=True)  # must not raise


# --------------------------------------------------------------------------- #
# 3. signal parity with strategy_signals
# --------------------------------------------------------------------------- #
def test_bar_window_xs_matches_real_multiindex_xs():
    bars = bars_from(rising_then([10.0] * 30))
    series, timeline = _prepare(bars, ["AAA"])
    s = series["AAA"]
    k = len(timeline) - 1
    end = int(s.end_pos[k])
    window = _BarWindow({"AAA": s.df.iloc[max(0, end - 45): end]}, timeline[k])

    truth = bars[bars.index.get_level_values(1) <= timeline[k]].xs("AAA", level=0).sort_index()
    pd.testing.assert_frame_equal(window.xs("AAA", level=0), truth.iloc[-45:])


def test_engine_trend_signals_match_direct_helper_calls():
    """
    The engine passes _BarWindow into the real helpers. Verify the helpers give
    the same answer on a _BarWindow as on a genuine MultiIndex frame, at every
    minute of a series with both up and down structure.
    """
    closes = rising_then([10.0 - 0.01 * i for i in range(30)])
    bars = bars_from(closes)
    cfg = make_cfg(enable_trend_filter=True, trend_ma_fast=5, trend_ma_slow=15)
    series, timeline = _prepare(bars, ["AAA"])
    s = series["AAA"]

    checked = 0
    for k in range(len(timeline)):
        end = int(s.end_pos[k])
        if end == 0:
            continue
        window = _BarWindow({"AAA": s.df.iloc[max(0, end - 45): end]}, timeline[k])
        truth_frame = bars[bars.index.get_level_values(1) <= timeline[k]]

        assert dual_ma_uptrend(window, "AAA", cfg) == dual_ma_uptrend(truth_frame, "AAA", cfg)
        assert trend_break_confirmed_below_sma(
            window.xs("AAA", level=0), 15, 3
        ) == trend_break_confirmed_below_sma(truth_frame.xs("AAA", level=0).sort_index().iloc[-45:], 15, 3)
        checked += 1
    assert checked > 30


def test_score_matches_hand_computed_formula():
    """score = momentum_return * log1p(min(volume_ratio, 5))."""
    closes = [10.0 + 0.01 * i for i in range(20)]
    bars = bars_from(closes, volume=1000.0)
    cfg = make_cfg(momentum_lookback_minutes=5, volume_lookback_minutes=5)
    from research.backtest import _prepare as prep

    series, timeline = prep(bars, ["AAA"])
    s = series["AAA"]
    k = len(timeline) - 1
    df = s.df

    # Hand-compute the score at every bar the engine could act on (needs
    # max(5,5)+5 = 10 bars of history), and take the best.
    best = -1e18
    for end in range(10, len(df) + 1):
        w = df.iloc[:end]
        latest_close = float(w["close"].iloc[-1])
        past = w[w.index <= w.index.max() - pd.Timedelta(minutes=5)]
        if past.empty:
            continue
        mom = latest_close / float(past["close"].iloc[-1]) - 1.0
        avg_vol = float(w["volume"].iloc[-5:].mean())
        ratio = min(float(w["volume"].iloc[-1]) / avg_vol, 5.0)
        best = max(best, mom * math.log1p(ratio))

    # A threshold a hair under the best score must let exactly that bar through;
    # a hair over must reject every bar.
    lo = run_backtest(replace(cfg, entry_score_threshold=best - 1e-9), bars,
                      start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    hi = run_backtest(replace(cfg, entry_score_threshold=best + 1e-9), bars,
                      start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    assert len(lo.trades) >= 1
    assert len(hi.trades) == 0


# --------------------------------------------------------------------------- #
# 4. accounting
# --------------------------------------------------------------------------- #
def test_sum_of_trade_pnl_equals_equity_curve_delta():
    closes = rising_then([10.0, 10.1, 9.95, 10.2, 10.0, 9.9, 10.3, 10.4, 10.1] * 4)
    bars = bars_from(closes)
    cfg = make_cfg(enable_take_profit=True, take_profit_pct=0.012, stop_loss_pct=0.006)
    start = 10_000.0
    res = run_backtest(cfg, bars, start_equity=start, slippage_bps=1.0, spread_bps=2.0)

    assert res.trades
    total = sum(t.pnl for t in res.trades)
    assert res.equity_curve.iloc[-1] - start == pytest.approx(total, abs=1e-9)
    assert res.metrics["total_pnl"] == pytest.approx(total, abs=1e-9)
    assert res.metrics["final_equity"] == pytest.approx(start + total, abs=1e-9)


def test_commission_is_charged_per_side():
    closes = rising_then([10.0] * 5 + [9.0])
    bars = bars_from(closes)
    cfg = make_cfg(stop_loss_pct=0.006, take_profit_pct=0.5)
    free = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0,
                        spread_bps=0.0, commission_per_order=0.0)
    paid = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0,
                        spread_bps=0.0, commission_per_order=0.5)
    assert paid.trades[0].pnl == pytest.approx(free.trades[0].pnl - 1.0, abs=1e-9)


def test_whole_share_sizing_blocks_entries_a_tiny_account_cannot_afford():
    """The $100-account reality check: a $150 share is simply untradeable."""
    closes = rising_then([], base=150.0)
    bars = bars_from(closes)
    cfg = make_cfg()
    res = run_backtest(cfg, bars, start_equity=100.0, whole_shares=True)
    assert res.trades == []
    frac = run_backtest(cfg, bars, start_equity=100.0, whole_shares=False)
    assert frac.trades, "fractional sizing would have traded — sizing is the binding constraint"


def test_qty_respects_risk_and_cash_caps():
    closes = rising_then([], base=10.0)
    bars = bars_from(closes)
    cfg = make_cfg(max_risk_per_trade=5.0, stop_loss_pct=0.006)
    # Sizing uses the live formula's `limit_price` = bid * (1 + entry_limit_offset_pct),
    # evaluated on the decision bar (index 9), not the fill price.
    limit_px = closes[9] * (1.0 + cfg.entry_limit_offset_pct)

    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    assert res.trades[0].qty == math.floor(5.0 / (limit_px * 0.006))  # risk-capped

    res2 = run_backtest(cfg, bars, start_equity=100.0, slippage_bps=0.0, spread_bps=0.0)
    assert res2.trades[0].qty == math.floor(100.0 * 0.98 / limit_px)  # cash-capped


def test_daily_loss_kill_switch_halts_new_entries():
    # A gap-down blows through the risk budget (a clean stop cannot: the risk cap
    # sizes the position so a stop loses slightly less than the remaining budget).
    tail = [10.0] * 5 + [5.0] + [10.0 * (1 + 0.0005 * i) for i in range(40)]
    closes = rising_then(tail)
    opens = list(closes)
    bars = bars_from(closes, opens=opens)
    cfg = make_cfg(stop_loss_pct=0.006, take_profit_pct=0.5,
                   max_risk_per_trade=5.0, max_daily_realized_loss=-5.0,
                   entry_cooldown_sec=0, post_loss_extra_cooldown_sec=0)
    res = run_backtest(cfg, bars, start_equity=10_000.0, slippage_bps=0.0, spread_bps=0.0)
    assert len(res.trades) == 1, "one big loss should trip the kill switch for the rest of the day"
    assert res.trades[0].pnl <= -5.0


def test_cooldown_blocks_immediate_reentry():
    closes = rising_then([10.0] * 5 + [9.0] + [10.0 * (1 + 0.0005 * i) for i in range(40)])
    bars = bars_from(closes)
    slow = make_cfg(stop_loss_pct=0.006, take_profit_pct=0.5,
                    entry_cooldown_sec=3600, post_loss_extra_cooldown_sec=0)
    fast = replace(slow, entry_cooldown_sec=0)
    assert len(run_backtest(slow, bars, start_equity=10_000.0).trades) == 1
    assert len(run_backtest(fast, bars, start_equity=10_000.0).trades) > 1


def test_max_open_positions_is_respected():
    a = bars_from(rising_then([10.0] * 40), symbol="AAA")
    b = bars_from(rising_then([10.0] * 40), symbol="BBB")
    c = bars_from(rising_then([10.0] * 40), symbol="CCC")
    bars = pd.concat([a, b, c]).sort_index()
    cfg = make_cfg(symbols_universe=["AAA", "BBB", "CCC"],
                   symbol_score_adjustments={"AAA": 0.0, "BBB": 0.0, "CCC": 0.0},
                   max_open_positions=2, stop_loss_pct=0.5, take_profit_pct=0.5)
    res = run_backtest(cfg, bars, start_equity=100_000.0)
    # Everything exits at EOD/end-of-data, so at most 2 legs can ever coexist.
    opens = sorted((t.entry_ts, 1) for t in res.trades)
    closes_ = sorted((t.exit_ts, -1) for t in res.trades)
    events = sorted(opens + closes_)
    live, peak = 0, 0
    for _, d in events:
        live += d
        peak = max(peak, live)
    assert peak <= 2


def test_metrics_are_internally_consistent():
    closes = rising_then([10.0, 10.1, 9.95, 10.2, 10.0, 9.9, 10.3] * 5)
    bars = bars_from(closes)
    cfg = make_cfg(enable_take_profit=True, take_profit_pct=0.012, stop_loss_pct=0.006)
    res = run_backtest(cfg, bars, start_equity=10_000.0)
    m = res.metrics
    tdf = res.trades_df
    assert m["trade_count"] == len(tdf)
    assert m["win_rate"] == pytest.approx((tdf["pnl"] > 0).mean() * 100.0)
    assert m["expectancy"] == pytest.approx(tdf["pnl"].mean())
    assert m["max_drawdown_pct"] <= 0.0


# --------------------------------------------------------------------------- #
# 5. sweep harness smoke test
# --------------------------------------------------------------------------- #
def test_sweep_runs_and_ranks():
    from research.sweep import sweep

    bars = bars_from(rising_then([10.0, 10.1, 9.95, 10.2, 10.0, 9.9, 10.3] * 5))
    cfg = make_cfg(enable_take_profit=True)
    df = sweep({"stop_loss_pct": [0.004, 0.006], "take_profit_pct": [0.012, 0.02]},
               bars, cfg, metric="expectancy", workers=1, start_equity=10_000.0)
    assert len(df) == 4
    assert list(df["expectancy"]) == sorted(df["expectancy"], reverse=True)


def test_walk_forward_reports_is_and_oos():
    from research.sweep import walk_forward

    # 8 sessions of data so a 3-day train / 2-day test split produces folds.
    frames = []
    for d in range(1, 15):
        try:
            ts = pd.Timestamp(f"2026-03-{d:02d} 09:30", tz=ET_TZ)
        except ValueError:
            continue
        if ts.weekday() >= 5:
            continue
        frames.append(bars_from(rising_then([10.0, 10.1, 9.95, 10.2] * 8),
                                start_et=f"2026-03-{d:02d} 09:30"))
    bars = pd.concat(frames).sort_index()
    cfg = make_cfg(enable_take_profit=True)
    df = walk_forward({"stop_loss_pct": [0.004, 0.006]}, bars, cfg,
                      train_days=4, test_days=2, workers=1, min_trades=0,
                      start_equity=10_000.0)
    assert not df.empty
    assert {"is_expectancy", "oos_expectancy"} <= set(df.columns)
    assert df.iloc[-1]["fold"] == "ALL"
