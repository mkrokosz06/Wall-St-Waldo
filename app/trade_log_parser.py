"""
Parse bot.log for closed trades (ENTRY filled → EXIT or STOP filled).

Used by the local dashboard; keeps logic separate from bot runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

# 2026-03-20 15:20:14.506 INFO ...
_TS_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")
_ENTRY = re.compile(
    r"ENTRY filled:\s*symbol=(?P<sym>[A-Z]+)\s+qty=(?P<qty>\d+(?:\.\d+)?)\s+avg_price=(?P<avg>\d+(?:\.\d+)?)"
)
_EXIT = re.compile(r"EXIT filled\.\s*exit_pnl=(?P<pnl>[-\d.]+)")
_STOP = re.compile(r"STOP filled\.\s*exit_pnl=(?P<pnl>[-\d.]+)")


@dataclass
class ParsedTrade:
    """One round-trip trade as inferred from log lines."""

    entry_ts: str
    symbol: str
    qty: float
    entry_avg: float
    exit_ts: str
    exit_pnl: float
    exit_kind: str  # "market_exit" | "stop_loss"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_ts": self.entry_ts,
            "symbol": self.symbol,
            "qty": self.qty,
            "entry_avg": self.entry_avg,
            "exit_ts": self.exit_ts,
            "exit_pnl": self.exit_pnl,
            "exit_kind": self.exit_kind,
        }


def _parse_ts(line: str) -> Optional[str]:
    m = _TS_PREFIX.match(line)
    return m.group(1) if m else None


def _tail_lines(path: str, n: int = 200_000) -> list:
    """Read only the last `n` lines of a file without loading it all into memory."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Estimate ~120 bytes per log line; seek back from end.
            f.seek(max(0, size - n * 120))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return raw.splitlines()


def parse_trades_from_log(log_path: str, max_lines: int = 200_000) -> List[ParsedTrade]:
    """
    Walk the log in order. ENTRY filled opens a position; EXIT/STOP filled closes it.
    If a close appears without a matching open, we still record it with symbol unknown.
    """
    trades: List[ParsedTrade] = []
    pending: Optional[Dict[str, Any]] = None

    lines = _tail_lines(log_path, n=max_lines)

    for line in lines:
        ts = _parse_ts(line)
        if not ts:
            continue

        em = _ENTRY.search(line)
        if em:
            try:
                qty = float(em.group("qty"))
                avg = float(em.group("avg"))
            except ValueError:
                # Skip malformed numeric fragments in noisy logs.
                continue
            pending = {
                "ts": ts,
                "symbol": em.group("sym"),
                "qty": qty,
                "avg": avg,
            }
            continue

        xm = _EXIT.search(line)
        sm = _STOP.search(line)
        if xm or sm:
            pnl_s = (xm or sm).group("pnl")
            try:
                pnl = float(pnl_s)
            except ValueError:
                continue
            kind = "market_exit" if xm else "stop_loss"
            if pending:
                trades.append(
                    ParsedTrade(
                        entry_ts=pending["ts"],
                        symbol=pending["symbol"],
                        qty=pending["qty"],
                        entry_avg=pending["avg"],
                        exit_ts=ts,
                        exit_pnl=pnl,
                        exit_kind=kind,
                    )
                )
                pending = None
            else:
                trades.append(
                    ParsedTrade(
                        entry_ts=ts,
                        symbol="?",
                        qty=0.0,
                        entry_avg=0.0,
                        exit_ts=ts,
                        exit_pnl=pnl,
                        exit_kind=kind,
                    )
                )
            continue

    return trades


def aggregate_stats(
    trades: List[ParsedTrade],
    et_date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Total PnL, today's PnL (ET date string YYYY-MM-DD), win rate, etc."""
    total = sum(t.exit_pnl for t in trades)
    if et_date_str:
        day_total = 0.0
        day_count = 0
        for t in trades:
            if t.exit_ts.startswith(et_date_str):
                day_total += t.exit_pnl
                day_count += 1
    else:
        day_total = None
        day_count = None

    wins = sum(1 for t in trades if t.exit_pnl > 0)
    losses = sum(1 for t in trades if t.exit_pnl < 0)
    flat = sum(1 for t in trades if t.exit_pnl == 0)
    n = len(trades)
    win_rate = (wins / n * 100.0) if n else 0.0

    by_symbol: Dict[str, float] = {}
    for t in trades:
        if t.symbol not in by_symbol:
            by_symbol[t.symbol] = 0.0
        by_symbol[t.symbol] += t.exit_pnl

    return {
        "trade_count": n,
        "total_pnl": round(total, 4),
        "daily_pnl": None if day_total is None else round(day_total, 4),
        "daily_trade_count": day_count,
        "wins": wins,
        "losses": losses,
        "breakeven": flat,
        "win_rate_pct": round(win_rate, 2),
        "avg_pnl_per_trade": round(total / n, 6) if n else 0.0,
        "pnl_by_symbol": {k: round(v, 4) for k, v in sorted(by_symbol.items(), key=lambda x: -abs(x[1]))},
    }
