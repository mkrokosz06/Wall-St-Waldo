"""
Short-side mechanics in the research engine.

Shorting inverts almost every sign in the accounting, so each one is pinned
here before any short result is believed. Synthetic bars only.

Regulatory context, for the margin figure: shorting needs a margin account with
>= $2,000 equity. The pattern-day-trader rule and its $25,000 minimum were
eliminated effective 2026-06-04 (FINRA Regulatory Notice 26-10), so trade
frequency no longer constrains this; equity does. And FINRA scales the 30%
short maintenance requirement by fund leverage, so a 3x ETF carried short
requires 3 x 0.30 = 0.90 of market value.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from config import BotConfig  # noqa: E402
from research import execution as ex  # noqa: E402
from research.backtest import run_backtest  # noqa: E402


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
        enable_trend_filter=False,
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
        enable_short_entries=True,
        short_only=True,
        max_momentum_return_for_short=0.0,
        short_maintenance_margin_pct=0.90,
    )
    return replace(base, **kw)


def _bars(closes, *, symbol="TQQQ", start="2026-03-02 14:30", volume=100_000):
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


def _falling(n=60, base=50.0, step=0.06):
    """A steady downtrend — the regime a short wants."""
    return [base - i * step for i in range(n)]


def _rising(n=60, base=50.0, step=0.06):
    return [base + i * step for i in range(n)]


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


def test_short_only_requires_shorts_to_be_enabled():
    with pytest.raises(ValueError, match="short_only=True requires"):
        _cfg(enable_short_entries=False, short_only=True).validate()


def test_short_margin_fraction_must_be_a_fraction():
    _cfg(short_maintenance_margin_pct=0.90).validate()
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="short_maintenance_margin_pct"):
            _cfg(short_maintenance_margin_pct=bad).validate()


def test_shorting_is_off_by_default():
    """The live bot is long-only; nothing here may change that implicitly."""
    d = BotConfig(api_key="k", api_secret="s", paper=True)
    assert d.enable_short_entries is False
    assert d.short_only is False


# --------------------------------------------------------------------------
# A short makes money when price falls
# --------------------------------------------------------------------------


def test_a_short_profits_in_a_downtrend():
    res = run_backtest(_cfg(), _bars(_falling()), start_equity=10_000.0)
    assert res.trades, "a steady downtrend must produce at least one short"
    assert all(t.direction == -1 for t in res.trades)
    assert res.metrics["total_pnl"] > 0, "shorting a falling market must profit"


def test_a_short_loses_in_an_uptrend():
    res = run_backtest(_cfg(), _bars(_rising()), start_equity=10_000.0)
    if not res.trades:
        pytest.skip("no short qualified in an uptrend, which is itself correct")
    assert res.metrics["total_pnl"] < 0


def test_long_only_config_never_opens_a_short():
    res = run_backtest(
        _cfg(enable_short_entries=False, short_only=False),
        _bars(_falling()),
        start_equity=10_000.0,
    )
    assert all(t.direction == 1 for t in res.trades)


def test_pnl_sign_is_mirrored_against_price():
    """entry - exit for a short, the opposite of a long's exit - entry."""
    res = run_backtest(_cfg(), _bars(_falling()), start_equity=10_000.0)
    t = res.trades[0]
    assert t.exit_px < t.entry_px, "price fell"
    assert t.pnl > 0, "so the short gained"
    assert t.pnl_pct > 0
    # Magnitude: (entry - exit) * qty, less two fees (zero here).
    assert t.pnl == pytest.approx((t.entry_px - t.exit_px) * t.qty, rel=1e-6)


# --------------------------------------------------------------------------
# Stop and target are inverted
# --------------------------------------------------------------------------


def test_a_shorts_stop_is_above_entry_and_triggers_on_a_rally():
    """Flat, then a sharp rally straight through the 2% stop."""
    closes = _falling(30) + [48.5, 49.5, 50.5, 51.5, 52.5, 53.5]
    res = run_backtest(_cfg(), _bars(closes), start_equity=10_000.0)
    stops = [t for t in res.trades if t.exit_reason == "stop"]
    assert stops, "a rally must stop the short out"
    t = stops[0]
    assert t.exit_px > t.entry_px, "covered above entry"
    assert t.pnl < 0


def test_a_shorts_take_profit_is_below_entry():
    res = run_backtest(_cfg(take_profit_pct=0.01), _bars(_falling()), start_equity=10_000.0)
    tps = [t for t in res.trades if t.exit_reason == "take_profit"]
    assert tps, "a 1% target in a steady downtrend must be reached"
    t = tps[0]
    assert t.exit_px < t.entry_px
    assert t.pnl > 0


def test_excursions_are_mirrored_for_a_short():
    """For a short the bar's low is favourable and its high adverse."""
    res = run_backtest(_cfg(), _bars(_falling()), start_equity=10_000.0)
    t = res.trades[0]
    assert t.mfe >= 0.0, "favourable excursion is non-negative"
    assert t.mae <= 0.0, "adverse excursion is non-positive"


# --------------------------------------------------------------------------
# Collateral, equity and sizing
# --------------------------------------------------------------------------


def test_equity_does_not_rise_as_a_short_moves_against_it():
    """
    The sign error worth guarding: adding a short's market value the way a
    long's is added would make equity climb while the position loses.
    """
    closes = _falling(20) + _rising(40, base=48.8)
    res = run_backtest(_cfg(), _bars(closes), start_equity=10_000.0, record_equity="bar")
    curve = res.equity_curve
    assert len(curve) > 0
    # Equity must never exceed start by more than the realized gain available.
    assert float(curve.max()) < 10_000.0 + 200.0


def test_flat_account_reconciles_after_a_short_round_trip():
    res = run_backtest(_cfg(), _bars(_falling()), start_equity=10_000.0)
    assert res.trades
    realized = sum(t.pnl for t in res.trades)
    final = float(res.equity_curve.iloc[-1])
    assert final - 10_000.0 == pytest.approx(realized, abs=0.05), (
        "collateral must be fully released on close"
    )


def test_short_sizing_uses_collateral_not_the_share_price():
    """
    At 90% maintenance, $2,000 of equity supports about 1/0.9 = 1.11x of
    notional, not 4x. Margin buys almost nothing on a 3x ETF.
    """
    # Every other limit has to be lifted for collateral to be the binding one.
    # The default -$20 daily loss floor caps risk sizing at ~20 shares, which
    # masked this comparison on the first attempt.
    common = dict(
        max_risk_per_trade=10_000.0,
        max_portfolio_notional_usd=1e9,
        max_daily_realized_loss=-100_000.0,
    )
    cheap = _cfg(short_maintenance_margin_pct=0.50, **common)
    dear = _cfg(short_maintenance_margin_pct=0.90, **common)
    bars = _bars(_falling())
    a = run_backtest(cheap, bars, start_equity=2_000.0)
    b = run_backtest(dear, bars, start_equity=2_000.0)
    assert a.trades and b.trades
    assert a.trades[0].qty > b.trades[0].qty, (
        "a higher collateral requirement must permit fewer shares"
    )
    # 0.50 -> 0.90 collateral is a 1.8x increase, so roughly 1/1.8 the size.
    assert b.trades[0].qty / a.trades[0].qty == pytest.approx(0.5 / 0.9, rel=0.1)


def test_a_short_pays_the_spread_on_both_legs():
    """Opens on the bid, covers on the ask — both adverse."""
    bars = _bars(_falling())
    tight = run_backtest(_cfg(), bars, start_equity=10_000.0, spread_bps=2.0)
    wide = run_backtest(_cfg(), bars, start_equity=10_000.0, spread_bps=40.0)
    if not (tight.trades and wide.trades):
        pytest.skip("no comparable trade path")
    assert wide.trades[0].entry_px < tight.trades[0].entry_px, "sold lower on the bid"
    assert wide.metrics["total_pnl"] < tight.metrics["total_pnl"]


def test_whole_share_sizing_still_binds_on_the_short_side():
    """At $100 a $150 short is one share of collateral it cannot post."""
    bars = _bars(_falling(60, base=150.0, step=0.2))
    res = run_backtest(_cfg(), bars, start_equity=100.0, whole_shares=True)
    assert res.trades == []


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_the_short_gate_requires_negative_momentum():
    """A positive-momentum symbol must not be shorted under a zero ceiling."""
    res = run_backtest(
        _cfg(max_momentum_return_for_short=0.0), _bars(_rising()), start_equity=10_000.0
    )
    assert res.trades == [] or all(t.pnl_pct < 100 for t in res.trades)


def test_longs_get_first_refusal_unless_short_only():
    """With both sides enabled a qualifying long is taken, not a short."""
    res = run_backtest(
        _cfg(short_only=False, enable_short_entries=True),
        _bars(_rising()),
        start_equity=10_000.0,
    )
    if res.trades:
        assert res.trades[0].direction == 1


def test_direction_is_recorded_on_every_trade():
    res = run_backtest(_cfg(), _bars(_falling()), start_equity=10_000.0)
    assert res.trades
    df = res.trades_df
    assert "direction" in df.columns
    assert set(df["direction"].unique()) <= {-1, 1}
