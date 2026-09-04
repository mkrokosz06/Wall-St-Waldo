#!/usr/bin/env python3
"""
Forensic log miner for the Trade Bot.

Reads every bot log it is given (default: app/bot.log and ./bot.log), merges and
de-duplicates the lines, and reconstructs a round-trip trade ledger with the TRUE
exit reason recovered from the trigger line that precedes each fill.

Why this exists
---------------
app/trade_log_parser.py (used by the dashboard) only understands the CURRENT log
format ``EXIT filled <SYM>. exit_pnl=...`` and only reads the tail of the file, so
it silently drops every trade written in the older ``EXIT filled. exit_pnl=...``
format and labels every exit ``market_exit``. This tool understands both formats
and 9 distinct exit triggers.

Usage
-----
    python tools/mine_trades.py                       # writes docs/trades.csv
    python tools/mine_trades.py --out docs/trades.csv --logs app/bot.log bot.log

Timestamps in the logs are machine-local, and the machine runs in America/New_York
(verified: "End-of-day cutoff" lines fire at 15:55, i.e. 16:00 minus
end_of_day_flat_minutes=5). They are therefore treated as ET.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (INFO|WARNING|ERROR|DEBUG) (.*)$")

# --- fills -------------------------------------------------------------------
# new: ENTRY filled: symbol=SQQQ qty=1.2 avg_price=12.340000 stop_id=None synthetic_stop=...
# old: ENTRY filled: symbol=QQQ qty=1.41 avg_price=589.510000. Stop-loss placed id=...
ENTRY_RE = re.compile(
    r"ENTRY filled: symbol=(?P<sym>[A-Z]+) qty=(?P<qty>\d+(?:\.\d+)?) avg_price=(?P<avg>\d+(?:\.\d+)?)"
)
# new: EXIT filled SQQQ. exit_pnl=-0.12 ...      old: EXIT filled. exit_pnl=-0.12 ...
EXIT_RE = re.compile(
    r"(?P<kind>EXIT|STOP) filled(?: (?P<sym>[A-Z]+))?\. exit_pnl=(?P<pnl>[-+]?[\d.]+)"
    r" daily_realized_pnl=(?P<day>[-+]?[\d.]+) halt_new_entries=(?P<halt>\w+)"
)
PARTIAL_RE = re.compile(
    r"Market exit (?P<sym>[A-Z]+): partial stop fill realized_part=(?P<pnl>[-+]?[\d.]+)"
)

# --- exit triggers, in priority order ---------------------------------------
TRIGGERS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"Synthetic stop breached (?P<sym>[A-Z]+):"), "hard_stop_synthetic"),
    (re.compile(r"Take-profit triggered \(synthetic\); exiting (?P<sym>[A-Z]+)"), "take_profit"),
    (re.compile(r"Take-profit triggered; exiting (?P<sym>[A-Z]+) with market sell"), "take_profit"),
    (re.compile(r"Take-profit triggered; exiting with market sell"), "take_profit"),
    (re.compile(r"Trend-break exit; market sell (?P<sym>[A-Z]+)"), "trend_break"),
    (re.compile(r"Trend-break exit \((?:confirmed below SMA|close below SMA)\); exiting with market sell"), "trend_break"),
    (re.compile(r"Candlestick bearish-reversal exit; market sell (?P<sym>[A-Z]+)"), "candlestick_exit"),
    (re.compile(r"Candlestick bearish-reversal exit; exiting with market sell"), "candlestick_exit"),
    (re.compile(r"Time stop reached \(synthetic\); market sell (?P<sym>[A-Z]+)"), "time_stop"),
    (re.compile(r"Time stop reached; market sell (?P<sym>[A-Z]+)"), "time_stop"),
    (re.compile(r"Time stop reached; exiting with market sell"), "time_stop"),
    (re.compile(r"End-of-day cutoff \(synthetic\); flattening (?P<sym>[A-Z]+)"), "eod_flat"),
    (re.compile(r"End-of-day cutoff; flattening (?P<sym>[A-Z]+)"), "eod_flat"),
    (re.compile(r"End-of-day cutoff reached; flattening with market sell"), "eod_flat"),
    (re.compile(r"Max runtime reached; flattening and stopping"), "shutdown_flat"),
    (re.compile(r"Flatten: market sell (?P<sym>[A-Z]+)"), "shutdown_flat"),
]

TRAIL_RE = re.compile(r"Trailing stop(?: (?P<sym>[A-Z]+))?: raising stop from")
NUDGE_RE = re.compile(
    r"Online training nudge: trade_pnl=(?P<pnl>[-+]?[\d.]+) -> score_th=(?P<s>[-+]?[\d.]+) mom_th=(?P<m>[-+]?[\d.]+)"
)
WINDOW_RE = re.compile(
    r"Online training: window=(?P<n>\d+) mean=(?P<mean>[-+]?[\d.]+) -> score_th=(?P<s>[-+]?[\d.]+) mom_th=(?P<m>[-+]?[\d.]+)"
)
NEUTRAL_RE = re.compile(r"Online training: window=(?P<n>\d+) mean=(?P<mean>[-+]?[\d.]+) within neutral band")
OFFLINE_RE = re.compile(
    r"Offline training applied: avg_exit_pnl=(?P<avg>[-+]?[\d.]+) n_trades=(?P<n>\d+) "
    r"dynamic_entry_score_threshold=(?P<s>[-+]?[\d.]+) dynamic_min_momentum_return=(?P<m>[-+]?[\d.]+)"
)
DIAG_RE = re.compile(r"Entry diagnostic: (?P<body>.*)$")
PLACED_RE = re.compile(
    r"Placed ENTRY(?: limit buy)?:? (?P<sym>[A-Z]+) (?:qty=)?(?P<qty>\d+(?:\.\d+)?) limit=(?P<lim>\d+(?:\.\d+)?) score=(?P<score>[-+]?[\d.]+)"
)

# Trigger must be within this many seconds before the fill to be credited.
TRIGGER_WINDOW_SEC = 900.0

LEGACY_UNIVERSE = {"SPY", "QQQ", "IWM", "DIA", "VTI"}
LEVERAGED_UNIVERSE = {"TQQQ", "SOXL", "SQQQ", "UVXY", "SOXS"}


@dataclass
class Trade:
    entry_ts: str
    exit_ts: str
    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    hold_sec: float
    hold_min: float
    exit_reason: str
    trailed: int
    entry_score: Optional[float]
    era: str
    symbol_resolved: str  # logged | inferred_single_open | inferred_fifo | unknown


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")


def load_lines(paths: List[str]):
    """Merge logs, dedupe identical (timestamp, level, message) records, sort by time."""
    seen = set()
    out = []
    for p in paths:
        if not os.path.exists(p):
            print("[warn] missing log: %s" % p, file=sys.stderr)
            continue
        n_new = 0
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                m = TS_RE.match(raw.rstrip("\n"))
                if not m:
                    continue  # traceback continuation / source echo
                key = (m.group(1), m.group(2), m.group(3))
                if key in seen:
                    continue
                seen.add(key)
                n_new += 1
                out.append((parse_ts(m.group(1)), m.group(2), m.group(3), os.path.basename(p)))
        print("[info] %s -> %d new records" % (p, n_new), file=sys.stderr)
    out.sort(key=lambda r: r[0])
    return out


def mine(records) -> dict:
    trades: List[Trade] = []
    pending: Dict[str, dict] = {}
    pending_order: List[str] = []
    last_trigger: Dict[str, Tuple[datetime, str]] = {}
    global_trigger: Optional[Tuple[datetime, str]] = None
    trailed_since_entry: Dict[str, int] = {}
    last_score: Dict[str, float] = {}
    partials: Dict[str, float] = {}

    dup_entries = 0
    orphan_exits = 0
    knob_events: List[dict] = []
    diagnostics: List[Tuple[datetime, str]] = []
    counters: Dict[str, int] = {}
    warn_kinds: Dict[str, int] = {}
    err_kinds: Dict[str, int] = {}

    for ts, level, msg, src in records:
        if level == "WARNING":
            warn_kinds[msg[:60]] = warn_kinds.get(msg[:60], 0) + 1
        elif level == "ERROR":
            err_kinds[msg[:60]] = err_kinds.get(msg[:60], 0) + 1

        pm = PLACED_RE.search(msg)
        if pm:
            last_score[pm.group("sym")] = float(pm.group("score"))
            counters["entry_orders_placed"] = counters.get("entry_orders_placed", 0) + 1
            continue

        em = ENTRY_RE.search(msg)
        if em:
            sym = em.group("sym")
            if sym in pending:
                dup_entries += 1
                pending_order.remove(sym)
            pending[sym] = {
                "ts": ts,
                "qty": float(em.group("qty")),
                "avg": float(em.group("avg")),
                "score": last_score.get(sym),
            }
            pending_order.append(sym)
            trailed_since_entry[sym] = 0
            partials.pop(sym, None)
            continue

        tm = TRAIL_RE.search(msg)
        if tm:
            sym = tm.group("sym")
            if sym:
                trailed_since_entry[sym] = trailed_since_entry.get(sym, 0) + 1
            else:
                for s in pending_order:
                    trailed_since_entry[s] = trailed_since_entry.get(s, 0) + 1
            continue

        pmm = PARTIAL_RE.search(msg)
        if pmm:
            s = pmm.group("sym")
            partials[s] = partials.get(s, 0.0) + float(pmm.group("pnl"))
            counters["partial_stop_fills"] = counters.get("partial_stop_fills", 0) + 1
            continue

        matched_trigger = False
        for rx, reason in TRIGGERS:
            m = rx.search(msg)
            if m:
                sym = m.groupdict().get("sym")
                if sym:
                    last_trigger[sym] = (ts, reason)
                else:
                    global_trigger = (ts, reason)
                counters["trigger:" + reason] = counters.get("trigger:" + reason, 0) + 1
                matched_trigger = True
                break
        if matched_trigger:
            continue

        xm = EXIT_RE.search(msg)
        if xm:
            kind = xm.group("kind")
            sym = xm.group("sym")
            pnl = float(xm.group("pnl"))
            resolved = "logged"
            if sym is None:
                if len(pending_order) == 1:
                    sym = pending_order[0]
                    resolved = "inferred_single_open"
                elif pending_order:
                    sym = pending_order[0]
                    resolved = "inferred_fifo"
                else:
                    sym = "?"
                    resolved = "unknown"
            entry = pending.pop(sym, None)
            if entry is not None:
                pending_order.remove(sym)
            else:
                orphan_exits += 1

            if kind == "STOP":
                reason = "trailing_stop" if trailed_since_entry.get(sym, 0) > 0 else "hard_stop"
            else:
                reason = None
                cand = last_trigger.get(sym)
                if cand and (ts - cand[0]).total_seconds() <= TRIGGER_WINDOW_SEC:
                    reason = cand[1]
                elif global_trigger and (ts - global_trigger[0]).total_seconds() <= TRIGGER_WINDOW_SEC:
                    reason = global_trigger[1]
                if reason is None:
                    reason = "unattributed_market_exit"
                if reason == "hard_stop_synthetic" and trailed_since_entry.get(sym, 0) > 0:
                    reason = "trailing_stop"
            last_trigger.pop(sym, None)
            global_trigger = None

            qty = entry["qty"] if entry else 0.0
            ep = entry["avg"] if entry else 0.0
            xp = (ep + pnl / qty) if (entry and qty > 0) else 0.0
            hold = (ts - entry["ts"]).total_seconds() if entry else float("nan")
            era = "leveraged" if sym in LEVERAGED_UNIVERSE else ("legacy" if sym in LEGACY_UNIVERSE else "unknown")
            trades.append(
                Trade(
                    entry_ts=(entry["ts"].strftime("%Y-%m-%d %H:%M:%S") if entry else ""),
                    exit_ts=ts.strftime("%Y-%m-%d %H:%M:%S"),
                    symbol=sym,
                    qty=round(qty, 6),
                    entry_price=round(ep, 6),
                    exit_price=round(xp, 6),
                    pnl=pnl,
                    hold_sec=round(hold, 1) if hold == hold else float("nan"),
                    hold_min=round(hold / 60.0, 2) if hold == hold else float("nan"),
                    exit_reason=reason,
                    trailed=trailed_since_entry.get(sym, 0),
                    entry_score=entry["score"] if entry else None,
                    era=era,
                    symbol_resolved=resolved,
                )
            )
            trailed_since_entry.pop(sym, None)
            continue

        hit = False
        for rx, tag in ((NUDGE_RE, "nudge"), (WINDOW_RE, "window"), (OFFLINE_RE, "offline")):
            m = rx.search(msg)
            if m:
                d = m.groupdict()
                knob_events.append(
                    {
                        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": tag,
                        "score_th": float(d["s"]),
                        "mom_th": float(d["m"]),
                        "driver": float(d.get("pnl") or d.get("mean") or d.get("avg") or 0.0),
                        "n": int(d["n"]) if d.get("n") else None,
                    }
                )
                hit = True
                break
        if not hit:
            m = NEUTRAL_RE.search(msg)
            if m:
                knob_events.append(
                    {
                        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": "neutral_reset",
                        "score_th": 0.0,
                        "mom_th": 0.0,
                        "driver": float(m.group("mean")),
                        "n": int(m.group("n")),
                    }
                )
                hit = True
        if hit:
            continue

        dm = DIAG_RE.search(msg)
        if dm:
            diagnostics.append((ts, dm.group("body")))

    return {
        "trades": trades,
        "dup_entries": dup_entries,
        "orphan_exits": orphan_exits,
        "open_at_end": list(pending_order),
        "knob_events": knob_events,
        "diagnostics": diagnostics,
        "counters": counters,
        "warn_kinds": warn_kinds,
        "err_kinds": err_kinds,
    }


def write_csv(trades: List[Trade], out: str) -> None:
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    cols = list(asdict(trades[0]).keys()) if trades else []
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def default_paths():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return here, [os.path.join(here, "app", "bot.log"), os.path.join(here, "bot.log")]


def main() -> int:
    here, logs = default_paths()
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", nargs="+", default=logs)
    ap.add_argument("--out", default=os.path.join(here, "docs", "trades.csv"))
    a = ap.parse_args()

    recs = load_lines(a.logs)
    res = mine(recs)
    tr = res["trades"]
    write_csv(tr, a.out)
    print("records=%d trades=%d total_pnl=%.2f" % (len(recs), len(tr), sum(t.pnl for t in tr)))
    print("dup_entry_records=%d orphan_exits=%d open_at_end=%s"
          % (res["dup_entries"], res["orphan_exits"], res["open_at_end"]))
    print("wrote " + a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
