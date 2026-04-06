"""
Pure strategy signal helpers (no broker / no I/O).

Used by TradeBot for entry scoring, trend gates, and exit structure checks.
Keep logic here testable and separate from order execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from config import BotConfig


def dual_ma_uptrend(bars_df: pd.DataFrame, sym: str, config: "BotConfig") -> bool:
    """Long-only: price not meaningfully below fast MA (with small tolerance)."""
    if not config.enable_trend_filter:
        return True
    try:
        df_sym = bars_df.xs(sym, level=0).sort_index()
    except Exception:
        return False
    slow = int(config.trend_ma_slow)
    fast = int(config.trend_ma_fast)
    if len(df_sym) < slow + 3:
        return False
    c = df_sym["close"].astype(float)
    sma_fast = float(c.rolling(fast).mean().iloc[-1])
    last = float(c.iloc[-1])
    tol = float(config.trend_ma_tolerance_pct)
    return last >= sma_fast * (1.0 - tol)


def is_bullish_engulfing(prev_bar: pd.Series, cur_bar: pd.Series) -> bool:
    prev_open = float(prev_bar["open"])
    prev_close = float(prev_bar["close"])
    cur_open = float(cur_bar["open"])
    cur_close = float(cur_bar["close"])
    prev_bearish = prev_close < prev_open
    cur_bullish = cur_close > cur_open
    body_engulfs = cur_open <= prev_close and cur_close >= prev_open
    return prev_bearish and cur_bullish and body_engulfs


def is_hammer(bar: pd.Series, wick_ratio: float = 2.0) -> bool:
    o = float(bar["open"])
    c = float(bar["close"])
    h = float(bar["high"])
    l = float(bar["low"])
    body = abs(c - o)
    if body <= 0:
        return False
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return lower_wick >= wick_ratio * body and upper_wick <= body


def is_bearish_engulfing(prev_bar: pd.Series, cur_bar: pd.Series) -> bool:
    prev_open = float(prev_bar["open"])
    prev_close = float(prev_bar["close"])
    cur_open = float(cur_bar["open"])
    cur_close = float(cur_bar["close"])
    prev_bullish = prev_close > prev_open
    cur_bearish = cur_close < cur_open
    body_engulfs = cur_open >= prev_close and cur_close <= prev_open
    return prev_bullish and cur_bearish and body_engulfs


def is_shooting_star(bar: pd.Series, wick_ratio: float = 2.0) -> bool:
    o = float(bar["open"])
    c = float(bar["close"])
    h = float(bar["high"])
    l = float(bar["low"])
    body = abs(c - o)
    if body <= 0:
        return False
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return upper_wick >= wick_ratio * body and lower_wick <= body


def bearish_candlestick_exit(
    prev_bar: pd.Series,
    cur_bar: pd.Series,
    wick_ratio: float = 2.0,
) -> bool:
    """Single-bar / two-bar bearish pattern for optional early exit."""
    return is_bearish_engulfing(prev_bar, cur_bar) or is_shooting_star(cur_bar, wick_ratio=wick_ratio)


def candlestick_bullish_strength(bars_df: pd.DataFrame, sym: str, config: "BotConfig") -> float:
    """
    Bounded strength in [0, 1] for optional entry score boost / hard gate.
    Requires pattern + context + volume; optional next-candle confirmation.
    """
    try:
        df_sym = bars_df.xs(sym, level=0).sort_index()
    except Exception:
        return 0.0
    if len(df_sym) < 8:
        return 0.0

    prev_bar = df_sym.iloc[-3]
    pat_bar = df_sym.iloc[-2]
    conf_bar = df_sym.iloc[-1]

    wick_ratio = float(config.candlestick_wick_ratio)
    bullish_pattern = is_bullish_engulfing(prev_bar, pat_bar) or is_hammer(pat_bar, wick_ratio=wick_ratio)
    if not bullish_pattern:
        return 0.0

    closes = df_sym["close"].astype(float)
    sma5 = float(closes.rolling(5).mean().iloc[-2])
    pat_close = float(pat_bar["close"])
    sma_prox = float(config.candlestick_sma_proximity_pct)
    if pat_close < sma5 * (1.0 - sma_prox):
        return 0.0

    if config.require_candlestick_confirmation:
        conf_close = float(conf_bar["close"])
        if conf_close <= pat_close:
            return 0.0

    vols = df_sym["volume"].astype(float)
    vol_base = float(vols.iloc[-8:-2].mean()) if len(vols.iloc[-8:-2]) > 0 else 0.0
    pat_vol = float(pat_bar["volume"])
    mult = float(config.candlestick_volume_confirm_mult)
    if vol_base > 0 and pat_vol < vol_base * mult:
        return 0.0

    strength = 0.6
    if vol_base > 0 and pat_vol >= vol_base * (mult + 0.3):
        strength += 0.2
    if is_bullish_engulfing(prev_bar, pat_bar):
        strength += 0.2
    return min(1.0, strength)


def trend_break_confirmed_below_sma(df_sym: pd.DataFrame, ma_n: int, confirm_n: int) -> bool:
    """
    True if the last `confirm_n` closes are each below their rolling SMA(ma_n),
    and the last close is not above the first (filters pure noise / bounce).
    """
    confirm_n = max(1, int(confirm_n))
    ma_n = int(ma_n)
    if len(df_sym) < ma_n + confirm_n + 1:
        return False
    c = df_sym["close"].astype(float)
    sma_series = c.rolling(ma_n).mean()
    if sma_series.isna().all():
        return False
    recent_close = c.iloc[-confirm_n:]
    recent_sma = sma_series.iloc[-confirm_n:]
    below_count = int((recent_close < recent_sma).sum())
    if below_count < confirm_n:
        return False
    if len(recent_close) >= 2 and float(recent_close.iloc[-1]) > float(recent_close.iloc[0]):
        return False
    return True
