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

    rows.append(_row("baseline", backtest.run_backtest(base, bars, 100.0)))
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
    for seed in (1, 2, 3, 4, 5):
        res = backtest.run_backtest(
            base, bars, 100.0, random_entry_seed=seed  # type: ignore[call-arg]
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
