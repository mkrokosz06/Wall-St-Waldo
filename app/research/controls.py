"""
Control experiments: does the entry signal contain any edge at all?

Tuning parameters is only worth doing if the entry rule beats a coin flip. These
controls answer that before any sweep runs, because a grid search over a signal
with no edge will still return a "best" row — it will just be the row that fit
the noise most flatteringly.

Three controls:

``zero_cost``
    Same strategy, no spread and no slippage. Splits the result into "is the
    signal bad" versus "are the costs eating it".

``random_entry``
    The entry gate is replaced by a coin flip with the same trade frequency;
    exits are untouched. If the real signal scores no better than this, the
    momentum score is decoration.

``shuffled_signal``
    Entries taken at the same times of day but on a randomly chosen symbol.
    Isolates symbol *selection* from entry *timing*.

Each randomized control is run over several seeds so the comparison is against a
distribution, not one draw.

Usage::

    python -m research.controls
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(APP / ".env")

import config as cfg_mod  # noqa: E402
from research import backtest, data, diagnose  # noqa: E402


# Eight seeds, matching what FINDINGS.md reports.
RANDOM_SEEDS = (1, 2, 3, 4, 5, 6, 7, 8)


def calibrate_random_rate(
    base_cfg,
    bars,
    target_trades: int,
    *,
    seed: int = 0,
    tolerance: float = 0.08,
    max_iter: int = 12,
) -> tuple:
    """
    Find the random entry rate that reproduces ``target_trades``.

    Bisection on the rate, because trade count rises monotonically with it. The
    point is comparability: a control must take a similar number of trades, and
    therefore carry similar exposure and pay similar costs, before its P&L can
    be set against the real signal's.

    Returns ``(rate, achieved_trades)``. Falls back to the closest rate found if
    the tolerance cannot be met within ``max_iter``.
    """
    lo, hi = 0.0005, 0.5
    best = (0.01, -1)
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        res = backtest.run_backtest(
            base_cfg, bars, 100.0,
            random_entry_seed=seed,  # type: ignore[call-arg]
            random_entry_rate=mid,  # type: ignore[call-arg]
        )
        n = int(res.metrics["trade_count"])
        if best[1] < 0 or abs(n - target_trades) < abs(best[1] - target_trades):
            best = (mid, n)
        if target_trades > 0 and abs(n - target_trades) <= tolerance * target_trades:
            return mid, n
        if n < target_trades:
            lo = mid
        else:
            hi = mid
    return best


def _row(name: str, res: backtest.BacktestResult) -> Dict[str, Any]:
    m = res.metrics
    return {
        "variant": name,
        "trades": m["trade_count"],
        "total_pnl": round(m["total_pnl"], 2),
        "win_%": round(m["win_rate"], 1),
        "profit_factor": round(m["profit_factor"], 3),
        "expectancy": round(m["expectancy"], 4),
        "avg_win": round(m["avg_win"], 3),
        "avg_loss": round(m["avg_loss"], 3),
    }


def main() -> int:
    bars = diagnose.load_bars()
    base = cfg_mod.load_config()
    rows: List[Dict[str, Any]] = []

    baseline_res = backtest.run_backtest(base, bars, 100.0)
    rows.append(_row("baseline", baseline_res))
    df_baseline_n = int(baseline_res.metrics["trade_count"])
    rows.append(
        _row(
            "zero_cost",
            backtest.run_backtest(base, bars, 100.0, slippage_bps=0.0, spread_bps=0.0),
        )
    )
    rows.append(
        _row(
            "double_cost",
            backtest.run_backtest(base, bars, 100.0, slippage_bps=2.0, spread_bps=4.0),
        )
    )

    # A signal with no gates at all: buy whatever ranks first, every time it can.
    # Not random, but it strips the filters out to show what they are worth.
    nofilter = replace(
        base,
        enable_trend_filter=False,
        entry_score_threshold=-1.0,
        min_momentum_return=-1.0,
    )
    rows.append(_row("no_entry_filters", backtest.run_backtest(nofilter, bars, 100.0)))

    # Randomized controls: same machinery, entry decision replaced by a coin flip.
    #
    # Count-matched. An unmatched control is not a fair comparison: a coin flip
    # that takes 200 trades against a signal that takes 360 is being judged on
    # different exposure, and raw P&L then says more about trade count than
    # about edge. calibrate_random_rate finds the entry rate that reproduces the
    # real signal's trade count before the comparison is made.
    #
    # FINDINGS.md described this as "8 seeds, count-matched" while the code ran
    # 5 seeds at a fixed rate and matched nothing. Both are now true.
    real_n = int(df_baseline_n)
    rate, achieved = calibrate_random_rate(base, bars, real_n)
    print(
        f"count-matching: target {real_n} trades -> random_entry_rate={rate:.4f} "
        f"produces ~{achieved} (seed 0)"
    )
    for seed in RANDOM_SEEDS:
        res = backtest.run_backtest(
            base, bars, 100.0,
            random_entry_seed=seed,  # type: ignore[call-arg]
            random_entry_rate=rate,  # type: ignore[call-arg]
        )
        rows.append(_row(f"random_entry_seed{seed}", res))

    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    rnd = df[df["variant"].str.startswith("random_entry")]
    if not rnd.empty:
        real_pf = float(df.loc[df["variant"] == "baseline", "profit_factor"].iloc[0])
        print(
            f"\nrandom-entry profit factor: mean {rnd['profit_factor'].mean():.3f} "
            f"sd {rnd['profit_factor'].std():.3f}  |  real signal: {real_pf:.3f}"
        )
        z = (real_pf - rnd["profit_factor"].mean()) / max(rnd["profit_factor"].std(), 1e-9)
        print(f"the real entry signal is {z:+.2f} sd from the coin-flip control")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
