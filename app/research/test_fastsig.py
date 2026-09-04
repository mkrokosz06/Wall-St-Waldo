"""
Parity tests: the vectorized fast path must equal the real ``strategy_signals``
helpers bar for bar.

The slow path is the reference implementation — it runs the live bot's own code.
If these tests fail, ``fastsig`` is wrong, not ``strategy_signals``.

Parity is checked on real cached SIP minute bars when they are available (the
only data that exercises gaps, halts and thin pre-market volume) and otherwise
on a synthetic random walk, so the suite still runs on a machine with no cache.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from research import fastsig  # noqa: E402
from strategy_signals import (  # noqa: E402
    bearish_candlestick_exit,
    candlestick_bullish_strength,
    dual_ma_uptrend,
    trend_break_confirmed_below_sma,
)

LOOKBACK = 45


class _Win:
    """Minimal ``bars_df`` stand-in: the helpers only ever call ``.xs``."""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def xs(self, key, level=0):  # noqa: ANN001, ANN201 - duck-typed
        return self._df


def _cfg():
    """A BotConfig with every signal enabled, so no branch goes untested."""
    os.environ.setdefault("ALPACA_API_KEY", "test")
    os.environ.setdefault("ALPACA_API_SECRET", "test")
    from dataclasses import replace

    from config import BotConfig

    cfg = BotConfig(api_key="k", api_secret="s", paper=True, symbols_universe=["X"])
    return replace(
        cfg,
        enable_trend_filter=True,
        require_candlestick_confirmation=True,
        min_avg_volume=100.0,
    )


def _synthetic(n: int = 1200, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 30.0 * np.exp(np.cumsum(rng.normal(0, 0.0007, n)))
    spread = np.abs(rng.normal(0, 0.0015, n)) * close
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.0003, n))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = rng.lognormal(8.0, 0.7, n)
    idx = pd.date_range("2026-06-01 13:30", periods=n, freq="min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )


def _real_bars() -> pd.DataFrame | None:
    """Cached TQQQ minute bars if the parquet cache has been populated."""
    try:
        from research import data
    except Exception:
        return None
    cache = Path(data.CACHE_DIR)
    files = sorted(cache.glob("TQQQ_*.parquet")) if cache.exists() else []
    if not files:
        return None
    df = pd.read_parquet(files[-1])
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs("TQQQ", level=0)
    df = df.sort_index()
    return df.iloc[:6000]


def _frames():
    out = [("synthetic", _synthetic())]
    real = _real_bars()
    if real is not None and len(real) > 500:
        out.append(("real-TQQQ", real))
    return out


def _reference_score(df: pd.DataFrame, j: int, cfg) -> tuple[bool, float, float]:
    """
    Recompute ``backtest._score_symbols`` for a single bar with pandas only.

    Returns (valid, score, momentum). Mirrors the live code exactly, including
    its early-outs, so any divergence shows up as a parity failure.
    """
    win = df.iloc[max(0, j + 1 - LOOKBACK) : j + 1]
    need = max(cfg.momentum_lookback_minutes, cfg.volume_lookback_minutes) + 5
    if len(win) < need:
        return False, math.nan, math.nan
    latest_ts = win.index.max()
    latest_close = float(win["close"].iloc[-1])
    latest_volume = float(win["volume"].iloc[-1])
    target = latest_ts - pd.Timedelta(minutes=cfg.momentum_lookback_minutes)
    past = win[win.index <= target]
    if past.empty:
        return False, math.nan, math.nan
    close_past = float(past["close"].iloc[-1])
    if close_past <= 0:
        return False, math.nan, math.nan
    mom = (latest_close / close_past) - 1.0
    vw = win["volume"].iloc[-cfg.volume_lookback_minutes :]
    avg_vol = float(vw.mean()) if not vw.empty else 0.0
    if avg_vol <= 0 or avg_vol < cfg.min_avg_volume:
        return False, math.nan, math.nan
    ratio = min(latest_volume / avg_vol, 5.0)
    return True, float(mom * math.log1p(ratio)), mom


def _precompute(df: pd.DataFrame, cfg):
    return fastsig.precompute_symbol(
        df.index,
        df["open"].to_numpy(float),
        df["high"].to_numpy(float),
        df["low"].to_numpy(float),
        df["close"].to_numpy(float),
        df["volume"].to_numpy(float),
        cfg,
        LOOKBACK,
    )


def _positions(df: pd.DataFrame) -> range:
    """Check every bar on synthetic data; stride the larger real series."""
    return range(len(df)) if len(df) <= 1500 else range(0, len(df), 3)


@pytest.mark.parametrize("name,df", _frames(), ids=lambda v: v if isinstance(v, str) else "")
def test_score_and_momentum_parity(name, df):
    cfg = _cfg()
    sig = _precompute(df, cfg)
    for j in _positions(df):
        valid, score, mom = _reference_score(df, j, cfg)
        assert bool(sig.score_valid[j]) == valid, f"{name} score_valid mismatch at {j}"
        if valid:
            assert sig.score[j] == pytest.approx(score, rel=1e-9, abs=1e-15), f"{name} score @{j}"
            assert sig.momentum[j] == pytest.approx(mom, rel=1e-9, abs=1e-15), f"{name} mom @{j}"


@pytest.mark.parametrize("name,df", _frames(), ids=lambda v: v if isinstance(v, str) else "")
def test_uptrend_parity(name, df):
    cfg = _cfg()
    sig = _precompute(df, cfg)
    for j in _positions(df):
        win = df.iloc[max(0, j + 1 - LOOKBACK) : j + 1]
        expected = dual_ma_uptrend(_Win(win), "X", cfg)
        assert bool(sig.uptrend[j]) == bool(expected), f"{name} uptrend @{j}"


@pytest.mark.parametrize("name,df", _frames(), ids=lambda v: v if isinstance(v, str) else "")
def test_candlestick_strength_parity(name, df):
    cfg = _cfg()
    sig = _precompute(df, cfg)
    for j in _positions(df):
        win = df.iloc[max(0, j + 1 - LOOKBACK) : j + 1]
        expected = candlestick_bullish_strength(_Win(win), "X", cfg)
        assert sig.candle[j] == pytest.approx(expected, abs=1e-12), f"{name} candle @{j}"


@pytest.mark.parametrize("name,df", _frames(), ids=lambda v: v if isinstance(v, str) else "")
def test_bearish_exit_parity(name, df):
    cfg = _cfg()
    sig = _precompute(df, cfg)
    for j in _positions(df):
        win = df.iloc[max(0, j + 1 - LOOKBACK) : j + 1]
        if len(win) < 2:
            assert not sig.bear_exit[j]
            continue
        expected = bearish_candlestick_exit(
            win.iloc[-2], win.iloc[-1], wick_ratio=float(cfg.candlestick_wick_ratio)
        )
        assert bool(sig.bear_exit[j]) == bool(expected), f"{name} bear_exit @{j}"


@pytest.mark.parametrize("name,df", _frames(), ids=lambda v: v if isinstance(v, str) else "")
def test_trend_break_parity(name, df):
    cfg = _cfg()
    sig = _precompute(df, cfg)
    ma_n = int(cfg.trend_break_ma_minutes)
    conf = max(1, int(cfg.trend_break_confirm_bars))
    for j in _positions(df):
        win = df.iloc[max(0, j + 1 - LOOKBACK) : j + 1]
        expected = trend_break_confirmed_below_sma(win, ma_n, conf)
        assert bool(sig.trend_break[j]) == bool(expected), f"{name} trend_break @{j}"


def test_trend_break_parity_single_confirm_bar():
    """confirm_bars=1 disables the bounce check — cover that branch explicitly."""
    from dataclasses import replace

    df = _synthetic(600, seed=11)
    cfg = replace(_cfg(), trend_break_confirm_bars=1)
    sig = _precompute(df, cfg)
    for j in range(len(df)):
        win = df.iloc[max(0, j + 1 - LOOKBACK) : j + 1]
        expected = trend_break_confirmed_below_sma(win, int(cfg.trend_break_ma_minutes), 1)
        assert bool(sig.trend_break[j]) == bool(expected), f"trend_break(conf=1) @{j}"


def test_trend_filter_disabled_is_always_true():
    from dataclasses import replace

    df = _synthetic(300, seed=3)
    cfg = replace(_cfg(), enable_trend_filter=False)
    sig = _precompute(df, cfg)
    assert sig.uptrend.all()


def test_no_lookahead_future_bars_do_not_change_the_past():
    """
    Truncating the series must not change any signal at bars that survive.

    This is the property the whole backtest rests on: a value at bar j is a
    function of bars <= j only.
    """
    df = _synthetic(900, seed=21)
    cfg = _cfg()
    full = _precompute(df, cfg)
    cut = 600
    part = _precompute(df.iloc[:cut], cfg)
    for arr_name in ("score", "momentum", "candle"):
        a = getattr(full, arr_name)[:cut]
        b = getattr(part, arr_name)[:cut]
        np.testing.assert_allclose(a, b, rtol=1e-12, equal_nan=True, err_msg=arr_name)
    for arr_name in ("score_valid", "uptrend", "bear_exit", "trend_break"):
        np.testing.assert_array_equal(
            getattr(full, arr_name)[:cut], getattr(part, arr_name)[:cut], err_msg=arr_name
        )


def test_gap_in_bars_shortens_momentum_window():
    """
    Momentum uses a timestamp lookback. Dropping bars must move the reference
    close to the last one at or before now - lookback, not N positions back.
    """
    df = _synthetic(400, seed=5)
    holed = pd.concat([df.iloc[:200], df.iloc[210:]])
    cfg = _cfg()
    sig = _precompute(holed, cfg)
    j = 205  # a bar just after the hole
    _, _, mom = _reference_score(holed, j, cfg)
    assert sig.momentum[j] == pytest.approx(mom, rel=1e-9, abs=1e-15)


def test_fast_and_slow_engines_produce_identical_trades():
    """
    End-to-end equivalence: the whole replay, not just the signals.

    Signal parity does not by itself prove engine parity — an off-by-one in how
    the engine indexes the arrays would pass every test above and still trade
    differently. This runs both paths over two weeks of real bars and demands
    an identical trade blotter.
    """
    from dataclasses import replace

    real = _real_bars()
    if real is None or len(real) < 3000:
        pytest.skip("no cached bars; run research.data first")

    from research import backtest

    bars = pd.concat({"TQQQ": real.iloc[:4000]}, names=["symbol", "timestamp"])
    cfg = replace(
        _cfg(),
        symbols_universe=["TQQQ"],
        enable_spy_market_trend_filter=False,
        min_avg_volume=2000.0,
    )
    slow = backtest.run_backtest(cfg, bars, 100.0, fast=False, strict=True)
    fast = backtest.run_backtest(cfg, bars, 100.0, fast=True)
    assert len(slow.trades_df) == len(fast.trades_df)
    assert slow.trades_df.equals(fast.trades_df)
    assert slow.metrics["total_pnl"] == pytest.approx(fast.metrics["total_pnl"], abs=1e-12)
