"""
Vectorized precomputation of every per-bar signal the backtest needs.

Why this exists
---------------
``backtest.run_backtest`` originally called the real ``strategy_signals``
helpers on a freshly sliced pandas window at every minute. That is the most
faithful thing to do — it literally runs the live bot's code — but it costs
~55 of every 68 seconds of replay, which makes a parameter sweep impossible
(a single 8-month pass took 13 minutes; a 500-point grid would take four days).

So: compute the same quantities once per symbol as numpy arrays, then let the
replay loop do O(1) lookups. The slow path is kept and is the reference
implementation — ``test_fastsig.py`` asserts the two agree bar for bar on real
data. If they ever disagree, the slow path is right and this file is wrong.

Every array here is indexed by a symbol's own bar position ``j`` and encodes
"what the live bot would have computed with bars up to and including j".
Nothing reads past j, which is what makes the fast path safe.

Window semantics
----------------
The live bot fetches ``bars_lookback_minutes_for_scoring`` (45) minutes of bars
and the helpers roll over that window, so validity gates are expressed against
``win_len(j) = min(j + 1, lookback)`` rather than the full series length. Every
rolling span in use (5, 15, 10, 8 ...) is shorter than the window, so a plain
positional rolling mean over the full series gives the identical value; only
the *validity* gates need the window length.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover
    from config import BotConfig


def _rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    """
    Trailing mean of the last ``n`` values, NaN until ``n`` values exist.

    Deliberately delegates to ``pd.Series.rolling`` rather than a cumsum. A
    cumsum is faster but drifts: summing 158k prices around $55 leaves ~3e-12 of
    float error in each mean, and the signals compare ``close < sma`` directly.
    On a price plateau — a stretch of identical closes, common in a quiet book —
    the true comparison is an exact tie, and 3e-12 of drift flips it. That
    single flipped bar changes a trend-break exit, which changes a trade. Parity
    with the live bot is worth more here than the microseconds.
    """
    n = int(n)
    if n <= 0 or n > x.size:
        return np.full(x.shape, np.nan, dtype=float)
    return pd.Series(x).rolling(n).mean().to_numpy()


def _trailing_mean_min1(x: np.ndarray, n: int) -> np.ndarray:
    """Trailing mean over the last ``n`` values, shrinking at the head (min_periods=1)."""
    n = max(1, int(n))
    return pd.Series(x).rolling(n, min_periods=1).mean().to_numpy()


def _slice_mean(x: np.ndarray, lo_off: int, hi_off: int) -> np.ndarray:
    """
    Mean of ``x[j - lo_off : j - hi_off]`` for every j, NaN where out of range.

    Used for the candlestick volume baseline, which is ``vols.iloc[-8:-2]`` —
    the six bars ending two before the current one.
    """
    width = lo_off - hi_off
    if width <= 0:
        return np.full(x.shape, np.nan, dtype=float)
    # rolling(width).mean()[k] covers x[k-width+1 : k+1]; the slice we want ends
    # at j-hi_off-1, hence the shift of hi_off+1 rather than hi_off.
    return pd.Series(x).rolling(width).mean().shift(hi_off + 1).to_numpy()


@dataclass
class SymbolSignals:
    """Per-bar signal arrays for one symbol. All arrays share the series length."""

    score: np.ndarray  # entry score; NaN where _score_symbols would skip
    momentum: np.ndarray  # momentum_return; NaN where undefined
    score_valid: np.ndarray  # bool: symbol appears in the ranked list at all
    uptrend: np.ndarray  # bool: dual_ma_uptrend
    candle: np.ndarray  # float: candlestick_bullish_strength in [0, 1]
    bear_exit: np.ndarray  # bool: bearish_candlestick_exit(prev, cur)
    trend_break: np.ndarray  # bool: trend_break_confirmed_below_sma


def precompute_symbol(
    index: "pd.DatetimeIndex",
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    cfg: "BotConfig",
    lookback: int,
) -> SymbolSignals:
    """
    Vectorize every signal for one symbol.

    ``index`` is the symbol's bar index. Momentum uses a *timestamp* lookback,
    not a positional one, because the live bot does — a gap in the bar stream
    shortens the momentum window rather than reaching further back in time.
    The lookup goes through pandas rather than raw ``asi8`` integers because a
    DatetimeIndex may carry microsecond resolution (parquet round-trips often
    do), and hard-coding nanoseconds silently shifts every reference bar.
    """
    m = close.size
    j = np.arange(m)
    win_len = np.minimum(j + 1, int(lookback))
    win_start = j + 1 - win_len

    # --- momentum: last close at or before (now - momentum_lookback_minutes) --
    mom_lb = int(cfg.momentum_lookback_minutes)
    vol_lb = int(cfg.volume_lookback_minutes)
    target = index - pd.Timedelta(minutes=mom_lb)
    past_pos = np.asarray(index.searchsorted(target, side="right")) - 1

    momentum = np.full(m, np.nan, dtype=float)
    # The past bar must still be inside the visible window; otherwise the live
    # code's ``past_slice`` is empty and the symbol is skipped entirely.
    have_past = (past_pos >= win_start) & (past_pos >= 0)
    cp = np.where(have_past, close[np.clip(past_pos, 0, m - 1)], np.nan)
    good = have_past & (cp > 0)
    momentum[good] = close[good] / cp[good] - 1.0

    # --- volume ratio ------------------------------------------------------- #
    avg_vol = _trailing_mean_min1(volume, vol_lb)
    vol_ok = (avg_vol > 0) & (avg_vol >= float(cfg.min_avg_volume))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.minimum(np.where(vol_ok, volume / avg_vol, np.nan), 5.0)

    need = max(mom_lb, vol_lb) + 5
    score_valid = (win_len >= need) & good & vol_ok
    score = np.full(m, np.nan, dtype=float)
    score[score_valid] = momentum[score_valid] * np.log1p(ratio[score_valid])

    # --- dual MA uptrend ----------------------------------------------------- #
    if not cfg.enable_trend_filter:
        uptrend = np.ones(m, dtype=bool)
    else:
        fast = int(cfg.trend_ma_fast)
        slow = int(cfg.trend_ma_slow)
        tol = float(cfg.trend_ma_tolerance_pct)
        sma_fast = _rolling_mean(close, fast)
        sma_slow = _rolling_mean(close, slow)
        with np.errstate(invalid="ignore"):
            uptrend = (
                (win_len >= slow + 3)
                & (sma_fast >= sma_slow * (1.0 - tol))
                & (close >= sma_fast * (1.0 - tol))
            )
        uptrend = np.nan_to_num(uptrend, nan=0.0).astype(bool)

    # --- candlestick bullish strength ---------------------------------------- #
    candle = _candle_strength(open_, high, low, close, volume, cfg, win_len)

    # --- bearish exit pattern (prev = j-1, cur = j) --------------------------- #
    wick = float(cfg.candlestick_wick_ratio)
    bear = np.zeros(m, dtype=bool)
    if m >= 2:
        prev_o, prev_c = open_[:-1], close[:-1]
        cur_o, cur_c = open_[1:], close[1:]
        bear_eng = (prev_c > prev_o) & (cur_c < cur_o) & (cur_o >= prev_c) & (cur_c <= prev_o)
        star = _is_shooting_star(open_[1:], high[1:], low[1:], close[1:], wick)
        bear[1:] = (bear_eng | star) & (win_len[1:] >= 2)

    # --- trend break ---------------------------------------------------------- #
    trend_break = _trend_break(
        close,
        int(cfg.trend_break_ma_minutes),
        max(1, int(cfg.trend_break_confirm_bars)),
        win_len,
    )

    return SymbolSignals(
        score=score,
        momentum=momentum,
        score_valid=score_valid,
        uptrend=uptrend,
        candle=candle,
        bear_exit=bear,
        trend_break=trend_break,
    )


def _is_shooting_star(
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, wick_ratio: float
) -> np.ndarray:
    body = np.abs(c - o)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    return (body > 0) & (upper >= wick_ratio * body) & (lower <= body)


def _candle_strength(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    cfg: "BotConfig",
    win_len: np.ndarray,
) -> np.ndarray:
    """
    Port of ``strategy_signals.candlestick_bullish_strength``.

    Window positions map to global positions as: prev = j-2, pattern = j-1,
    confirmation = j. The pattern bar is the one before last because the live
    helper wants a completed bar plus a confirming bar after it.
    """
    m = c.size
    out = np.zeros(m, dtype=float)
    if m < 3:
        return out

    wick = float(cfg.candlestick_wick_ratio)
    j = np.arange(m)
    ok = (win_len >= 8) & (j >= 2)
    if not ok.any():
        return out

    prev = j - 2
    pat = j - 1
    p = np.clip(prev, 0, m - 1)
    q = np.clip(pat, 0, m - 1)

    bull_eng = (c[p] < o[p]) & (c[q] > o[q]) & (o[q] <= c[p]) & (c[q] >= o[p])
    body = np.abs(c[q] - o[q])
    lower = np.minimum(o[q], c[q]) - l[q]
    upper = h[q] - np.maximum(o[q], c[q])
    hammer = (body > 0) & (lower >= wick * body) & (upper <= body)
    pattern = bull_eng | hammer

    # Context: pattern close must sit near/above SMA5 measured at the pattern bar.
    sma5 = _rolling_mean(c, 5)
    sma5_at_pat = np.full(m, np.nan)
    sma5_at_pat[1:] = sma5[:-1]
    prox = float(cfg.candlestick_sma_proximity_pct)
    with np.errstate(invalid="ignore"):
        near_sma = c[q] >= sma5_at_pat * (1.0 - prox)
    near_sma = np.nan_to_num(near_sma, nan=0.0).astype(bool)

    if cfg.require_candlestick_confirmation:
        confirmed = c > c[q]
    else:
        confirmed = np.ones(m, dtype=bool)

    # Volume baseline is vols.iloc[-8:-2]: the six bars ending at j-2.
    vol_base = _slice_mean(v, 7, 1)
    mult = float(cfg.candlestick_volume_confirm_mult)
    with np.errstate(invalid="ignore"):
        vol_ok = ~((vol_base > 0) & (v[q] < vol_base * mult))
    vol_ok = np.nan_to_num(vol_ok, nan=1.0).astype(bool)

    hit = ok & pattern & near_sma & confirmed & vol_ok
    strength = np.full(m, 0.6)
    with np.errstate(invalid="ignore"):
        strength = strength + np.where(
            (vol_base > 0) & (v[q] >= vol_base * (mult + 0.3)), 0.2, 0.0
        )
    strength = strength + np.where(bull_eng, 0.2, 0.0)
    out[hit] = np.minimum(1.0, strength[hit])
    return out


def _trend_break(
    close: np.ndarray, ma_n: int, confirm_n: int, win_len: np.ndarray
) -> np.ndarray:
    """
    Port of ``strategy_signals.trend_break_confirmed_below_sma``.

    True when the last ``confirm_n`` closes are each below their SMA(ma_n) and
    the run is not already bouncing (last close not above the first of the run).
    """
    m = close.size
    out = np.zeros(m, dtype=bool)
    ma_n = int(ma_n)
    confirm_n = max(1, int(confirm_n))
    if m < ma_n + confirm_n + 1:
        return out

    sma = _rolling_mean(close, ma_n)
    with np.errstate(invalid="ignore"):
        below = close < sma
    below = np.nan_to_num(below, nan=0.0).astype(bool)

    # All of the last confirm_n bars below their SMA.
    run = np.ones(m, dtype=bool)
    for d in range(confirm_n):
        shifted = np.zeros(m, dtype=bool)
        if d == 0:
            shifted = below
        else:
            shifted[d:] = below[:-d]
        run &= shifted

    j = np.arange(m)
    first = j - (confirm_n - 1)
    ok = (win_len >= ma_n + confirm_n + 1) & (first >= 0)

    # The live helper bails out when the run is already bouncing: the last close
    # of the run above its first close. With a single confirm bar there is no
    # run to compare, so the check does not apply.
    if confirm_n >= 2:
        f = np.clip(first, 0, m - 1)
        not_bouncing = close <= close[f]
    else:
        not_bouncing = np.ones(m, dtype=bool)

    return ok & run & not_bouncing
