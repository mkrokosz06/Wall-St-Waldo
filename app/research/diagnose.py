"""
Baseline + ablation diagnostics for the live configuration.

Answers the first question any tuning pass has to answer: *where does the money
actually go?* Ablations turn one mechanism off at a time and re-run the whole
history, which attributes P&L to a mechanism far more honestly than reading the
exit-reason column of a single run (an exit reason tells you how a trade ended,
not whether that exit helped).

Usage::

    python -m research.diagnose                # baseline + all ablations
    python -m research.diagnose --no-ablations # baseline only
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

import pandas as pd

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(APP / ".env")

import config as cfg_mod  # noqa: E402
from research import backtest, data  # noqa: E402

UNIVERSE = ["TQQQ", "SOXL", "SQQQ", "UVXY", "SOXS"]
START = "2026-01-01"
END = "2026-09-01"


def load_bars() -> pd.DataFrame:
    return data.fetch_minute_bars(UNIVERSE + ["SPY"], START, END)


def exit_reason_table(res: backtest.BacktestResult) -> pd.DataFrame:
    """P&L attribution by how the trade ended, with hold time and hit rate."""
    df = res.trades_df
    if df.empty:
        return pd.DataFrame()
    g = df.groupby("exit_reason")
    out = pd.DataFrame(
        {
            "trades": g.size(),
            "total_pnl": g["pnl"].sum().round(2),
            "avg_pnl": g["pnl"].mean().round(4),
            "win_rate_%": (g["pnl"].apply(lambda s: (s > 0).mean() * 100)).round(1),
            "avg_bars_held": g["bars_held"].mean().round(1),
            "avg_mfe_%": (g["mfe"].mean() * 100).round(3),
            "avg_mae_%": (g["mae"].mean() * 100).round(3),
        }
    )
    return out.sort_values("total_pnl")


def hold_time_table(res: backtest.BacktestResult) -> pd.DataFrame:
    df = res.trades_df
    if df.empty:
        return pd.DataFrame()
    bins = [0, 2, 5, 10, 20, 40, 80, 10_000]
    labels = ["0-2", "3-5", "6-10", "11-20", "21-40", "41-80", "80+"]
    b = pd.cut(df["bars_held"], bins=bins, labels=labels, right=True, include_lowest=True)
    g = df.groupby(b, observed=True)
    return pd.DataFrame(
        {
            "trades": g.size(),
            "total_pnl": g["pnl"].sum().round(2),
            "avg_pnl": g["pnl"].mean().round(4),
            "win_rate_%": (g["pnl"].apply(lambda s: (s > 0).mean() * 100)).round(1),
        }
    )


def hour_table(res: backtest.BacktestResult) -> pd.DataFrame:
    df = res.trades_df.copy()
    if df.empty:
        return pd.DataFrame()
    et = pd.DatetimeIndex(df["entry_ts"]).tz_convert(cfg_mod.ET_TZ)
    df["hour"] = et.hour
    g = df.groupby("hour")
    return pd.DataFrame(
        {
            "trades": g.size(),
            "total_pnl": g["pnl"].sum().round(2),
            "avg_pnl": g["pnl"].mean().round(4),
            "win_rate_%": (g["pnl"].apply(lambda s: (s > 0).mean() * 100)).round(1),
        }
    )


def ablations(base) -> Dict[str, object]:
    """One mechanism changed per variant, so the delta is attributable."""
    return {
        "baseline": {},
        "no_trend_break_exit": dict(enable_trend_break_exit=False),
        "no_candlestick_exit": dict(enable_candlestick_exit=False),
        "no_trailing_stop": dict(enable_trailing_stop=False),
        "no_take_profit": dict(enable_take_profit=False),
        "no_trend_filter": dict(enable_trend_filter=False),
        "no_online_training": dict(enable_online_training=False),
        "no_structure_exits": dict(
            enable_trend_break_exit=False, enable_candlestick_exit=False
        ),
        "long_only_bull_etfs": dict(symbols_universe=["TQQQ", "SOXL"]),
        "inverse_etfs_only": dict(symbols_universe=["SQQQ", "SOXS", "UVXY"]),
        "one_position": dict(max_open_positions=1),
        "wider_stop_1pct": dict(stop_loss_pct=0.01, take_profit_pct=0.02),
        "no_open_delay": dict(market_open_delay_minutes=0),
        "longer_open_delay_60m": dict(market_open_delay_minutes=60),
    }


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-ablations", action="store_true")
    ap.add_argument("--equity", type=float, default=100.0)
    args = ap.parse_args(argv)

    bars = load_bars()
    base = cfg_mod.load_config()
    print(f"bars: {len(bars):,}  {START} -> {END}  universe={base.symbols_universe}")
    print(
        f"stop={base.stop_loss_pct} tp={base.take_profit_pct} trail={base.trailing_stop_pct} "
        f"max_pos={base.max_open_positions} equity=${args.equity:g}\n"
    )

    res = backtest.run_backtest(base, bars, args.equity)
    print("=== BASELINE ===")
    print(res.summary())
    print("\n--- P&L by exit reason ---")
    print(exit_reason_table(res).to_string())
    print("\n--- P&L by bars held ---")
    print(hold_time_table(res).to_string())
    print("\n--- P&L by ET entry hour ---")
    print(hour_table(res).to_string())

    if args.no_ablations:
        return 0

    print("\n=== ABLATIONS (whole history re-run, one change each) ===")
    rows = []
    for name, over in ablations(base).items():
        cfg = replace(base, **over) if over else base
        r = backtest.run_backtest(cfg, bars, args.equity)
        m = r.metrics
        rows.append(
            {
                "variant": name,
                "trades": m["trade_count"],
                "total_pnl": round(m["total_pnl"], 2),
                "win_%": round(m["win_rate"], 1),
                "profit_factor": round(m["profit_factor"], 3),
                "expectancy": round(m["expectancy"], 4),
                "max_dd_%": round(m["max_drawdown_pct"], 1),
            }
        )
    tbl = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    print(tbl.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
