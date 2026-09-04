"""
Parameter search over the backtest engine.

Two entry points:

* :func:`sweep` — brute-force grid, one backtest per combination, fanned out
  across CPU cores. Useful for seeing the *shape* of the parameter surface.
* :func:`walk_forward` — rolling train/test folds. This is the one that tells you
  whether a parameter set is real. A grid search on the whole history will
  always find something that looks great; the only honest question is whether
  the parameters chosen on weeks 1-3 still work on week 4, repeatedly.

``BotConfig`` is a frozen dataclass, so every variant is built with
``dataclasses.replace``.

WHY multiprocessing and not threads: the engine is pure-Python bar iteration and
holds the GIL the entire time. Bars are loaded once in the parent and inherited
by workers (fork) or re-read from the parquet cache (spawn, i.e. Windows) via a
module-level global set in the pool initializer — either way each combination
does not re-download or re-parse data.
"""

from __future__ import annotations

import itertools
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

import sys as _sys
from pathlib import Path as _Path

# Allow both `python -m app.research.x` and `python app/research/x.py` by making
# the `app/` directory importable (that is where config/strategy_signals live).
_APP_DIR = str(_Path(__file__).resolve().parent.parent)
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

try:  # pragma: no cover
    from ..config import ET_TZ, BotConfig
    from .backtest import BacktestResult, run_backtest
except ImportError:  # pragma: no cover
    from config import ET_TZ, BotConfig  # type: ignore[no-redef]
    from research.backtest import BacktestResult, run_backtest  # type: ignore[no-redef]


# Set once per worker process by _init_worker; avoids pickling the bar frame for
# every single grid point.
_BARS: Optional[pd.DataFrame] = None
_BASE: Optional[BotConfig] = None
_KWARGS: Dict[str, Any] = {}


def _init_worker(bars: pd.DataFrame, base_cfg: BotConfig, kwargs: Dict[str, Any]) -> None:
    global _BARS, _BASE, _KWARGS
    _BARS, _BASE, _KWARGS = bars, base_cfg, kwargs


def expand_grid(param_grid: Dict[str, Sequence[Any]]) -> List[Dict[str, Any]]:
    """Cartesian product of ``{field: [values]}`` into a list of override dicts."""
    if not param_grid:
        return [{}]
    keys = list(param_grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(param_grid[k] for k in keys))]


def _row_from_result(overrides: Dict[str, Any], res: BacktestResult) -> Dict[str, Any]:
    row: Dict[str, Any] = dict(overrides)
    m = res.metrics
    for key in (
        "trade_count", "total_pnl", "return_pct", "win_rate", "avg_win", "avg_loss",
        "profit_factor", "expectancy", "max_drawdown_pct", "sharpe", "sortino",
        "avg_bars_held", "avg_mfe", "avg_mae",
    ):
        row[key] = m[key]
    row["exit_reasons"] = m["exit_reasons"]
    return row


def _run_one(overrides: Dict[str, Any]) -> Dict[str, Any]:
    assert _BARS is not None and _BASE is not None
    cfg = replace(_BASE, **overrides)
    res = run_backtest(cfg, _BARS, **_KWARGS)
    return _row_from_result(overrides, res)


def run_single(
    overrides: Dict[str, Any],
    bars: pd.DataFrame,
    base_cfg: BotConfig,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Single-process equivalent of one grid point (used by walk-forward)."""
    cfg = replace(base_cfg, **overrides)
    return _row_from_result(overrides, run_backtest(cfg, bars, **kwargs))


def sweep(
    param_grid: Dict[str, Sequence[Any]],
    bars: pd.DataFrame,
    base_cfg: BotConfig,
    metric: str = "expectancy",
    *,
    workers: Optional[int] = None,
    min_trades: int = 0,
    **backtest_kwargs: Any,
) -> pd.DataFrame:
    """
    Run every combination in ``param_grid`` and return one row per combination.

    The frame is sorted by ``metric`` descending. ``min_trades`` drops rows with
    too few trades to mean anything (a 2-trade profit factor of 9 is noise).
    """
    combos = expand_grid(param_grid)
    workers = workers or max(1, (os.cpu_count() or 2) - 1)

    if workers == 1 or len(combos) == 1:
        rows = [run_single(c, bars, base_cfg, **backtest_kwargs) for c in combos]
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(bars, base_cfg, backtest_kwargs),
        ) as pool:
            rows = list(pool.map(_run_one, combos, chunksize=1))

    df = pd.DataFrame(rows)
    if min_trades > 0 and "trade_count" in df.columns:
        df = df[df["trade_count"] >= min_trades]
    if metric in df.columns:
        df = df.sort_values(metric, ascending=False)
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# walk-forward
# --------------------------------------------------------------------------- #
def _slice_bars(bars: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    ts = bars.index.get_level_values(1)
    return bars[(ts >= start) & (ts < end)]


def make_folds(
    bars: pd.DataFrame,
    train_days: int = 21,
    test_days: int = 7,
    step_days: Optional[int] = None,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """
    Rolling (train_start, test_start, test_end) boundaries in calendar days.

    Calendar days, not sessions: it keeps folds aligned to weeks, which is how
    intraday regimes actually shift.
    """
    step_days = step_days or test_days
    ts = bars.index.get_level_values(1)
    first = pd.Timestamp(ts.min()).normalize()
    last = pd.Timestamp(ts.max()).normalize() + pd.Timedelta(days=1)

    folds = []
    train_start = first
    while True:
        test_start = train_start + pd.Timedelta(days=train_days)
        test_end = test_start + pd.Timedelta(days=test_days)
        if test_start >= last:
            break
        folds.append((train_start, test_start, min(test_end, last)))
        train_start = train_start + pd.Timedelta(days=step_days)
    return folds


def walk_forward(
    param_grid: Dict[str, Sequence[Any]],
    bars: pd.DataFrame,
    base_cfg: BotConfig,
    metric: str = "expectancy",
    *,
    train_days: int = 21,
    test_days: int = 7,
    step_days: Optional[int] = None,
    workers: Optional[int] = None,
    min_trades: int = 3,
    **backtest_kwargs: Any,
) -> pd.DataFrame:
    """
    Optimize on each training window, evaluate the winner on the following
    (unseen) test window, and report both side by side.

    Returned columns are prefixed ``is_`` (in-sample) and ``oos_`` (out-of-
    sample). The number that matters is the aggregate ``oos_`` row at the bottom
    — if in-sample expectancy is strongly positive and out-of-sample is around
    zero or negative, the grid found noise, not edge.
    """
    folds = make_folds(bars, train_days, test_days, step_days)
    if not folds:
        raise ValueError("bar range too short for the requested train/test window")

    rows: List[Dict[str, Any]] = []
    for i, (tr_start, te_start, te_end) in enumerate(folds):
        train_bars = _slice_bars(bars, tr_start, te_start)
        test_bars = _slice_bars(bars, te_start, te_end)
        if train_bars.empty or test_bars.empty:
            continue

        ranked = sweep(
            param_grid, train_bars, base_cfg, metric,
            workers=workers, min_trades=min_trades, **backtest_kwargs,
        )
        if ranked.empty:
            # No parameter set traded enough in-sample; fall back to base config
            # rather than silently skipping the fold.
            ranked = sweep(param_grid, train_bars, base_cfg, metric,
                           workers=workers, min_trades=0, **backtest_kwargs)
        if ranked.empty:
            continue

        best = ranked.iloc[0]
        overrides = {k: best[k] for k in param_grid if k in ranked.columns}
        oos = run_single(overrides, test_bars, base_cfg, **backtest_kwargs)

        row: Dict[str, Any] = {
            "fold": i,
            "train_start": tr_start.date(),
            "test_start": te_start.date(),
            "test_end": te_end.date(),
            "params": overrides,
        }
        for k in ("trade_count", "expectancy", "total_pnl", "win_rate", "profit_factor", "sharpe"):
            row[f"is_{k}"] = best[k]
            row[f"oos_{k}"] = oos[k]
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    agg = {"fold": "ALL", "train_start": "", "test_start": "", "test_end": "", "params": ""}
    for k in ("trade_count", "total_pnl"):
        agg[f"is_{k}"] = df[f"is_{k}"].sum()
        agg[f"oos_{k}"] = df[f"oos_{k}"].sum()
    for k in ("expectancy", "win_rate", "profit_factor", "sharpe"):
        agg[f"is_{k}"] = df[f"is_{k}"].mean()
        agg[f"oos_{k}"] = df[f"oos_{k}"].mean()
    return pd.concat([df, pd.DataFrame([agg])], ignore_index=True)
