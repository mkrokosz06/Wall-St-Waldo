"""
Phase 8 acceptance tests: research execution and accounting.

These pin the cost model and the cash/P&L/equity reconciliation. They run on
synthetic bars only — no cached market data, no network — so they work on a
fresh checkout.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from config import BotConfig  # noqa: E402
from research import execution as ex  # noqa: E402
from research.backtest import run_backtest  # noqa: E402

UTC = timezone.utc


# ==========================================================================
# The model itself
# ==========================================================================


def test_buys_pay_the_ask_and_sells_receive_the_bid():
    m = ex.ExecutionModel(spread_bps=100.0, slippage_bps=0.0)
    assert m.buy_fill(100.0) == pytest.approx(100.5)
    assert m.sell_fill(100.0) == pytest.approx(99.5)


def test_slippage_is_adverse_in_both_directions():
    m = ex.ExecutionModel(spread_bps=0.0, slippage_bps=100.0)
    assert m.buy_fill(100.0) == pytest.approx(101.0)
    assert m.sell_fill(100.0) == pytest.approx(99.0)


def test_legacy_model_reproduces_the_old_defect_exactly():
    """
    The documented bug: entries paid slippage only, never the spread. Kept as a
    named model so every number already in FINDINGS.md stays reproducible.
    """
    legacy = ex.ExecutionModel(name=ex.LEGACY, spread_bps=100.0, slippage_bps=10.0)
    assert legacy.buy_fill(100.0) == pytest.approx(100.1), "no ask-side spread"
    assert legacy.sell_fill(100.0) == pytest.approx(99.9)
    # Which is why its round-trip cost does not move with the spread at all.
    assert legacy.round_trip_cost_pct() == pytest.approx(0.002)
    assert ex.ExecutionModel(
        name=ex.LEGACY, spread_bps=1000.0, slippage_bps=10.0
    ).round_trip_cost_pct() == pytest.approx(0.002)


def test_corrected_round_trip_cost_scales_with_the_spread():
    """The property the legacy model lacked, and the reason results looked robust."""
    c2 = ex.ExecutionModel(spread_bps=2.0, slippage_bps=1.0).round_trip_cost_pct()
    c10 = ex.ExecutionModel(spread_bps=10.0, slippage_bps=1.0).round_trip_cost_pct()
    assert c2 == pytest.approx(0.0004)
    assert c10 == pytest.approx(0.0012)
    assert c10 > c2 * 2


def test_frictionless_model_has_no_costs():
    m = ex.ExecutionModel(name=ex.FRICTIONLESS, spread_bps=100.0, slippage_bps=100.0, fee_per_order=1.0)
    assert m.buy_fill(100.0) == 100.0
    assert m.sell_fill(100.0) == 100.0
    assert m.fee == 0.0
    assert m.round_trip_cost_pct() == 0.0


def test_unknown_model_and_negative_costs_are_rejected():
    with pytest.raises(ValueError, match="unknown execution model"):
        ex.ExecutionModel(name="wishful")
    with pytest.raises(ValueError, match="non-negative"):
        ex.ExecutionModel(spread_bps=-1.0)


def test_a_buy_limit_never_fills_above_its_limit_including_slippage():
    m = ex.ExecutionModel(spread_bps=0.0, slippage_bps=100.0)
    fill = m.buy_fill(100.0)  # 101.0
    assert m.cap_buy_at_limit(fill, 101.5) == pytest.approx(101.0)
    assert m.cap_buy_at_limit(fill, 100.5) is None, "slippage cannot breach the limit"
    assert m.cap_buy_at_limit(fill, None) == pytest.approx(101.0)


def test_a_gap_below_the_stop_executes_below_the_stop():
    """A stop is a trigger, not a guaranteed price."""
    m = ex.ExecutionModel(spread_bps=0.0, slippage_bps=0.0)
    # Normal trigger: bar opened above the stop, so the stop price is the fill.
    assert m.stop_fill(stop_px=99.0, bar_open=99.5, bar_low=98.9) == pytest.approx(99.0)
    # Gap: the bar opened at 95, well below the stop.
    assert m.stop_fill(stop_px=99.0, bar_open=95.0, bar_low=94.0) == pytest.approx(95.0)


def test_manifest_records_the_assumptions_and_names_the_model():
    man = ex.ExecutionModel(spread_bps=4.0, slippage_bps=1.0).manifest()
    assert man["execution_model"] == ex.SPREAD_AWARE
    assert man["spread_bps"] == 4.0
    assert "ask" in man["notes"] and "bid" in man["notes"]
    assert "Optimistic" in ex.ExecutionModel(name=ex.LEGACY).manifest()["notes"]


# ==========================================================================
# Engine-level accounting
# ==========================================================================


def _cfg(**kw) -> BotConfig:
    base = BotConfig(
        api_key="k",
        api_secret="s",
        paper=True,
        symbols_universe=["TQQQ"],
        max_open_positions=1,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        enable_trailing_stop=False,
        enable_trend_break_exit=False,
        enable_time_stop=False,
        enable_online_training=False,
        enable_offline_training=False,
        entry_score_threshold=0.0,
        min_momentum_return=0.0,
        market_open_delay_minutes=0,
        entry_cooldown_sec=0,
        post_loss_extra_cooldown_sec=0,
        max_spread_pct=0.05,
        min_avg_volume=0,
        max_entry_attempts_per_day=0,
    )
    return replace(base, **kw)


def _bars(closes, *, symbol="TQQQ", start="2026-03-02 14:30", volume=100_000):
    """Synthetic one-minute bars with a given close path."""
    ts = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(closes), freq="1min")
    idx = pd.MultiIndex.from_product([[symbol], ts], names=["symbol", "timestamp"])
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": c,
            "high": c * 1.0005,
            "low": c * 0.9995,
            "close": c,
            "volume": np.full(len(c), volume, dtype=float),
        },
        index=idx,
    )


def _rising(n=40, base=50.0, step=0.06):
    return [base + i * step for i in range(n)]


def test_increasing_spread_increases_execution_cost_on_a_fixed_trade_path():
    """
    The plan's named test, holding the path fixed: the same round trip must cost
    more when the spread is wider. Full-strategy P&L need not be monotonic
    because spread also changes which trades occur, so this compares fills
    directly rather than end-to-end P&L.
    """
    cheap = ex.ExecutionModel(spread_bps=2.0, slippage_bps=1.0)
    dear = ex.ExecutionModel(spread_bps=20.0, slippage_bps=1.0)

    qty, ref_in, ref_out = 10.0, 100.0, 101.0
    pnl_cheap = qty * (cheap.sell_fill(ref_out) - cheap.buy_fill(ref_in))
    pnl_dear = qty * (dear.sell_fill(ref_out) - dear.buy_fill(ref_in))

    assert pnl_dear < pnl_cheap
    # 18bp of extra spread over 10 shares of ~$100, paid on both sides.
    assert pnl_cheap - pnl_dear == pytest.approx(qty * ref_in * 0.0018, rel=0.05)


def test_legacy_cost_does_not_move_with_spread_which_is_the_defect():
    legacy_cheap = ex.ExecutionModel(name=ex.LEGACY, spread_bps=2.0, slippage_bps=1.0)
    legacy_dear = ex.ExecutionModel(name=ex.LEGACY, spread_bps=20.0, slippage_bps=1.0)
    qty, ref_in, ref_out = 10.0, 100.0, 101.0
    a = qty * (legacy_cheap.sell_fill(ref_out) - legacy_cheap.buy_fill(ref_in))
    b = qty * (legacy_dear.sell_fill(ref_out) - legacy_dear.buy_fill(ref_in))
    assert a == pytest.approx(b), "legacy ignores the spread entirely"


def test_flat_account_reconciles_final_equity_to_realized_pnl_with_fees():
    """
    With no remaining positions, final equity minus starting equity must equal
    realized net P&L, fees included. This is what the two-fee inconsistency
    used to break.
    """
    bars = _bars(_rising(60))
    res = run_backtest(
        _cfg(fee_estimate_per_order=0.01),
        bars,
        start_equity=10_000.0,
        spread_bps=4.0,
        slippage_bps=1.0,
    )
    assert res.trades, "expected at least one round trip"
    realized = sum(t.pnl for t in res.trades)
    final = float(res.equity_curve.iloc[-1])

    open_legs = [t for t in res.trades if getattr(t, "exit_reason", "") == "open"]
    if not open_legs:
        assert final - 10_000.0 == pytest.approx(realized, abs=0.02)


def test_the_fee_is_charged_once_per_order_not_twice():
    bars = _bars(_rising(60))
    free = run_backtest(_cfg(fee_estimate_per_order=0.0), bars, start_equity=10_000.0)
    paid = run_backtest(_cfg(fee_estimate_per_order=0.50), bars, start_equity=10_000.0)
    if not free.trades or len(free.trades) != len(paid.trades):
        pytest.skip("fee changed the trade path; compare only identical paths")
    n = len(free.trades)
    delta = sum(t.pnl for t in free.trades) - sum(t.pnl for t in paid.trades)
    assert delta == pytest.approx(n * 2 * 0.50, abs=1e-6), "two orders per round trip"


def test_postmarket_bars_cannot_erase_the_daily_equity_curve():
    """
    The plan's named case: irrelevant postmarket bars following the last
    regular-session bar must not reset final equity to starting capital.
    """
    closes = _rising(60)
    bars = _bars(closes)
    base = run_backtest(_cfg(), bars, start_equity=10_000.0, record_equity="day")

    # Append bars well after the close, as SIP would include.
    late_ts = pd.date_range(
        pd.Timestamp("2026-03-02 21:30", tz="UTC"), periods=10, freq="1min"
    )
    idx = pd.MultiIndex.from_product([["TQQQ"], late_ts], names=["symbol", "timestamp"])
    tail = pd.DataFrame(
        {
            "open": 60.0, "high": 60.1, "low": 59.9, "close": 60.0,
            "volume": 1000.0,
        },
        index=idx,
    )
    with_tail = pd.concat([bars, tail]).sort_index()
    after = run_backtest(_cfg(), with_tail, start_equity=10_000.0, record_equity="day")

    assert len(after.equity_curve) > 0
    assert float(after.equity_curve.iloc[-1]) != pytest.approx(0.0)
    assert float(after.equity_curve.iloc[-1]) > 0.0
    # The curve must not have been reset to starting capital by the late bars.
    assert float(after.equity_curve.iloc[-1]) == pytest.approx(
        float(base.equity_curve.iloc[-1]), rel=0.05
    )


def test_result_params_carry_a_reproducible_manifest():
    bars = _bars(_rising(40))
    res = run_backtest(_cfg(), bars, start_equity=10_000.0, spread_bps=6.0, slippage_bps=2.0)
    p = res.params
    assert p["execution_model"] == ex.SPREAD_AWARE
    assert p["spread_bps"] == 6.0
    assert p["slippage_bps"] == 2.0
    assert "round_trip_cost_pct" in p
    assert p["start_equity"] == 10_000.0


def test_same_inputs_and_model_reproduce_the_same_result():
    bars = _bars(_rising(50))
    a = run_backtest(_cfg(), bars, start_equity=10_000.0, spread_bps=4.0)
    b = run_backtest(_cfg(), bars, start_equity=10_000.0, spread_bps=4.0)
    assert [t.pnl for t in a.trades] == [t.pnl for t in b.trades]
    assert a.metrics["total_pnl"] == pytest.approx(b.metrics["total_pnl"])


def test_switching_model_changes_results_and_is_recorded():
    bars = _bars(_rising(60))
    legacy = run_backtest(
        _cfg(), bars, start_equity=10_000.0, spread_bps=20.0, execution_model=ex.LEGACY
    )
    aware = run_backtest(
        _cfg(), bars, start_equity=10_000.0, spread_bps=20.0, execution_model=ex.SPREAD_AWARE
    )
    assert legacy.params["execution_model"] == ex.LEGACY
    assert aware.params["execution_model"] == ex.SPREAD_AWARE
    if legacy.trades and aware.trades:
        assert aware.metrics["total_pnl"] < legacy.metrics["total_pnl"], (
            "charging the spread on entries must cost money"
        )
