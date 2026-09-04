"""
Forensic analysis of the trade bot's live paper-trading logs.

Read-only. Extends app/trade_log_parser.py (which only understands the CURRENT
"EXIT filled <SYM>." format) with:

  * the OLDER single-position format  "EXIT filled. exit_pnl=..."  (no symbol)
  * exit-REASON attribution, by correlating the trigger line the bot emits
    immediately before it sends the market sell:
        "Synthetic stop breached <SYM>: ..."          -> stop_loss_synthetic
        "Take-profit triggered ... <SYM> ..."         -> take_profit
        "Trend-break exit ..."                        -> trend_break
        "Candlestick bearish-reversal exit ..."       -> candlestick
        "Time stop reached ..."                       -> time_stop
        "End-of-day cutoff ..."                       -> eod
        (Alpaca "STOP filled" never appears in these logs -> see report)
  * entry-diagnostic ("why no buy") rejection-reason tallies
  * online/offline trainer threshold traces
  * operational error / restart counters

Usage:
    python app/research/log_forensics.py [--log app/bot.log] [--no-charts]

Everything printed is derived from the log; nothing is simulated.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
REPO_DIR = os.path.dirname(APP_DIR)

DEFAULT_LOG = os.path.join(APP_DIR, "bot.log")
LEGACY_LOG = os.path.join(REPO_DIR, "bot.log")

# Universe classification. Leveraged-ETF universe is the CURRENT config default.
CURRENT_UNIVERSE = {"TQQQ", "SOXL", "SQQQ", "UVXY", "SOXS"}
LEGACY_UNIVERSE = {"SPY", "QQQ", "IWM", "DIA", "VTI"}

# --------------------------------------------------------------------------
# Line patterns
# --------------------------------------------------------------------------

# Only real log records start with a timestamp; tracebacks echo source lines
# containing the same literals ("EXIT filled. exit_pnl=%.2f ...") and must be
# excluded or they inflate every count.
RE_REC = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+(?P<lvl>[A-Z]+)\s+(?P<msg>.*)$"
)

RE_ENTRY_FILLED = re.compile(
    r"^ENTRY filled:\s*symbol=(?P<sym>[A-Z]+)\s+qty=(?P<qty>\d+(?:\.\d+)?)"
    r"\s+avg_price=(?P<avg>\d+(?:\.\d+)?)"
)
RE_EXIT_SYM = re.compile(r"^EXIT filled (?P<sym>[A-Z]+)\.\s*exit_pnl=(?P<pnl>-?\d+(?:\.\d+)?)")
RE_EXIT_NOSYM = re.compile(r"^EXIT filled\.\s*exit_pnl=(?P<pnl>-?\d+(?:\.\d+)?)")
RE_STOP_FILLED = re.compile(r"^STOP filled\s+(?P<sym>[A-Z]+)?\.?\s*exit_pnl=(?P<pnl>-?\d+(?:\.\d+)?)")

RE_PLACED_ENTRY = re.compile(
    r"^Placed ENTRY(?: limit buy)?:\s*(?P<sym>[A-Z]+)\s+qty=(?P<qty>[\d.]+)\s+"
    r"limit=(?P<limit>[\d.]+)\s+score=(?P<score>-?[\d.eE+]+)\s+spread_pct=(?P<spread>-?[\d.eE+]+)"
)

# Exit triggers, checked in order. First match wins.
EXIT_TRIGGERS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^Synthetic stop breached\s+(?P<sym>[A-Z]+):"), "stop_loss_synthetic"),
    (re.compile(r"^Take-profit triggered.*?exiting (?P<sym>[A-Z]+) with market sell"), "take_profit"),
    (re.compile(r"^Take-profit triggered.*?exiting with market sell"), "take_profit"),
    (re.compile(r"^Trend-break exit;\s*market sell (?P<sym>[A-Z]+)"), "trend_break"),
    (re.compile(r"^Trend-break exit.*?exiting with market sell"), "trend_break"),
    (re.compile(r"^Candlestick bearish-reversal exit;\s*market sell (?P<sym>[A-Z]+)"), "candlestick"),
    (re.compile(r"^Candlestick bearish-reversal exit.*?exiting with market sell"), "candlestick"),
    (re.compile(r"^Time stop reached.*?market sell (?P<sym>[A-Z]+)"), "time_stop"),
    (re.compile(r"^Time stop reached.*?exiting with market sell"), "time_stop"),
    (re.compile(r"^End-of-day cutoff.*?flattening (?P<sym>[A-Z]+)"), "eod"),
    (re.compile(r"^End-of-day cutoff.*?(flattening with market sell|reached)"), "eod"),
    (re.compile(r"^Flatten: market sell (?P<sym>[A-Z]+)"), "flatten_shutdown"),
]

RE_TRAIL = re.compile(
    r"^Trailing stop(?:\s+(?P<sym>[A-Z]+))?:\s*raising stop from (?P<old>\d+(?:\.\d+)?) to "
    r"(?P<new>\d+(?:\.\d+)?) \(peak=(?P<peak>\d+(?:\.\d+)?)\)"
)

RE_DIAG = re.compile(r"^Entry diagnostic: (?P<body>.*)$")
RE_DIAG_TH = re.compile(r"thresholds score>=(?P<s>-?\d+(?:\.\d+)?) mom>=(?P<m>-?\d+(?:\.\d+)?)")

RE_OFFLINE = re.compile(
    r"^Offline training applied: avg_exit_pnl=(?P<avg>-?\d+(?:\.\d+)?) n_trades=(?P<n>\d+) "
    r"dynamic_entry_score_threshold=(?P<s>-?\d+(?:\.\d+)?) dynamic_min_momentum_return=(?P<m>-?\d+(?:\.\d+)?)"
)
RE_ONLINE_SET = re.compile(
    r"^Online training: window=(?P<w>\d+) mean=(?P<mean>-?\d+(?:\.\d+)?) -> "
    r"score_th=(?P<s>-?\d+(?:\.\d+)?) mom_th=(?P<m>-?\d+(?:\.\d+)?)"
)
RE_ONLINE_NEUTRAL = re.compile(
    r"^Online training: window=(?P<w>\d+) mean=(?P<mean>-?\d+(?:\.\d+)?) within neutral band"
)
RE_ONLINE_SMALL = re.compile(r"^Online training: window has (?P<w>\d+)/(?P<need>\d+) trades")

# Operational counters: label -> regex on msg
OPS_PATTERNS: Dict[str, re.Pattern] = {
    "bot_restart": re.compile(r"^Bot starting\."),
    "new_day": re.compile(r"^New day detected"),
    "stale_market_data": re.compile(r"Market data stale"),
    "unhandled_loop_error": re.compile(r"^Unhandled error in main loop"),
    "clock_fetch_failed": re.compile(r"^Clock fetch failed"),
    "loop_quotes_fetch_failed": re.compile(r"^Loop quotes fetch failed"),
    "stop_place_failed_safe_stop": re.compile(r"^Failed placing stop-loss \(SAFE STOP\)"),
    "stop_place_failed_halt": re.compile(r"^Failed to place stop-loss"),
    "stop_placed_during_reconcile": re.compile(r"^Stop-loss placed during reconcile"),
    "stop_already_active_broker": re.compile(r"^Stop-loss already active on broker"),
    "entry_timeout_cancel": re.compile(r"^Entry timeout reached"),
    "entry_unfilled_on_cancel": re.compile(r"^Entry had no filled qty"),
    "entry_partial_on_cancel": re.compile(r"^Entry partially filled on cancel"),
    "stale_leg_removed": re.compile(r"^Removing stale leg"),
    "leg_bootstrapped_from_broker": re.compile(r"^Bootstrapped position leg from broker"),
    "state_normalized_no_holdings": re.compile(r"^IN_POSITION but no broker holdings"),
    "exit_order_cancelled_rejected": re.compile(r"^Exit order canceled/rejected"),
    "flat_cleanup_leftover_sell": re.compile(r"^FLAT cleanup: cancel leftover sell order"),
    "notional_too_small": re.compile(r"^Computed notional too small"),
    "qty_too_small": re.compile(r"^Computed qty too small"),
    "max_runtime_flatten": re.compile(r"^Max runtime reached"),
    "keyboard_interrupt": re.compile(r"^KeyboardInterrupt received"),
    "trailing_stop_skipped": re.compile(r"^Trailing stop update skipped"),
    "offline_training_failed": re.compile(r"^Offline training failed"),
    "online_seed_failed": re.compile(r"^Online training log seed failed"),
    "migrated_single_position": re.compile(r"^Migrated single-position state"),
    "startup_reconcile_stop": re.compile(r"^Startup reconcile: found existing stop_order_id"),
    "position_closed_reset_flat": re.compile(r"^Position appears closed"),
}

RE_HTTP_ERR = re.compile(r"(GET|POST|DELETE|PATCH) (?P<path>/v2/[\w/]+) failed: (?P<code>\d{3})")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Trade:
    entry_ts: Optional[datetime]
    exit_ts: datetime
    symbol: str
    qty: float
    entry_avg: float
    pnl: float
    exit_reason: str
    reason_source: str          # "symbol_match" | "global_fallback" | "explicit" | "none"
    log_format: str             # "with_symbol" | "no_symbol"
    matched_entry: bool

    @property
    def hold_sec(self) -> Optional[float]:
        if self.entry_ts is None:
            return None
        return (self.exit_ts - self.entry_ts).total_seconds()

    @property
    def notional(self) -> float:
        return self.qty * self.entry_avg

    @property
    def pnl_pct(self) -> Optional[float]:
        n = self.notional
        return (self.pnl / n) if n > 0 else None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["entry_ts"] = self.entry_ts.isoformat() if self.entry_ts else None
        d["exit_ts"] = self.exit_ts.isoformat()
        d["hold_sec"] = self.hold_sec
        d["notional"] = round(self.notional, 2)
        d["pnl_pct"] = self.pnl_pct
        return d


@dataclass
class ParseResult:
    trades: List[Trade] = field(default_factory=list)
    ops: Counter = field(default_factory=Counter)
    http_errors: Counter = field(default_factory=Counter)
    diag_rejections: Counter = field(default_factory=Counter)
    diag_blockers: Counter = field(default_factory=Counter)
    diag_lines: int = 0
    diag_threshold_trace: List[Tuple[datetime, float, float]] = field(default_factory=list)
    offline_events: List[Dict[str, Any]] = field(default_factory=list)
    online_events: List[Dict[str, Any]] = field(default_factory=list)
    placed_entries: List[Dict[str, Any]] = field(default_factory=list)
    entries_filled: int = 0
    trailing_updates: Counter = field(default_factory=Counter)
    unmatched_exits: int = 0
    orphan_entries: List[Dict[str, Any]] = field(default_factory=list)
    sample_lines: Dict[str, str] = field(default_factory=dict)
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    total_records: int = 0
    total_lines: int = 0


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

TRIGGER_MAX_AGE_SEC = 900.0   # a trigger older than this is not credited to an exit


def parse_log(path: str) -> ParseResult:
    res = ParseResult()
    pending: Dict[str, Dict[str, Any]] = {}        # symbol -> entry info
    last_trigger_sym: Dict[str, Tuple[datetime, str]] = {}
    last_trigger_global: Optional[Tuple[datetime, str]] = None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            res.total_lines += 1
            line = raw.rstrip("\r\n")
            m = RE_REC.match(line)
            if not m:
                continue
            res.total_records += 1
            ts = _parse_ts(m.group("ts"))
            msg = m.group("msg")
            if res.first_ts is None:
                res.first_ts = ts
            res.last_ts = ts

            for label, rx in OPS_PATTERNS.items():
                if rx.search(msg):
                    res.ops[label] += 1
                    res.sample_lines.setdefault(label, line[:300])
                    if label == "unhandled_loop_error":
                        hm = RE_HTTP_ERR.search(msg)
                        if hm:
                            res.http_errors[f"{hm.group('path')} {hm.group('code')}"] += 1
                        elif "<html>" in msg or "401 Authorization" in msg:
                            res.http_errors["html_401_body"] += 1
                        else:
                            res.http_errors["other"] += 1
                    break

            em = RE_ENTRY_FILLED.match(msg)
            if em:
                sym = em.group("sym")
                res.entries_filled += 1
                if sym in pending:
                    res.orphan_entries.append({"ts": ts.isoformat(), "symbol": sym,
                                               "note": "entry while previous leg still open"})
                pending[sym] = {
                    "ts": ts,
                    "qty": float(em.group("qty")),
                    "avg": float(em.group("avg")),
                }
                continue

            pm = RE_PLACED_ENTRY.match(msg)
            if pm:
                res.placed_entries.append({
                    "ts": ts, "symbol": pm.group("sym"),
                    "qty": float(pm.group("qty")),
                    "score": float(pm.group("score")),
                    "spread": float(pm.group("spread")),
                })
                continue

            hit = False
            for rx, reason in EXIT_TRIGGERS:
                tm = rx.match(msg)
                if tm:
                    sym = tm.groupdict().get("sym")
                    if sym:
                        last_trigger_sym[sym] = (ts, reason)
                    last_trigger_global = (ts, reason)
                    hit = True
                    break
            if hit:
                continue

            trm = RE_TRAIL.match(msg)
            if trm:
                res.trailing_updates[trm.groupdict().get("sym") or "(no symbol)"] += 1
                continue

            xm = RE_EXIT_SYM.match(msg)
            sm = RE_STOP_FILLED.match(msg)
            nm = None if (xm or sm) else RE_EXIT_NOSYM.match(msg)

            if xm or sm or nm:
                if sm:
                    sym = sm.group("sym") or ""
                    pnl = float(sm.group("pnl"))
                    fmt = "with_symbol"
                    forced_reason = "stop_order_filled"
                elif xm:
                    sym = xm.group("sym")
                    pnl = float(xm.group("pnl"))
                    fmt = "with_symbol"
                    forced_reason = None
                else:
                    sym = ""
                    pnl = float(nm.group("pnl"))
                    fmt = "no_symbol"
                    forced_reason = None

                entry = None
                if sym and sym in pending:
                    entry = pending.pop(sym)
                elif not sym and len(pending) >= 1:
                    # Old single-position format: close the oldest open leg.
                    k = min(pending, key=lambda s: pending[s]["ts"])
                    sym = k
                    entry = pending.pop(k)

                if entry is None:
                    res.unmatched_exits += 1

                if forced_reason:
                    reason, src = forced_reason, "explicit"
                else:
                    reason, src = "unattributed", "none"
                    cand = last_trigger_sym.get(sym)
                    if cand and (ts - cand[0]).total_seconds() <= TRIGGER_MAX_AGE_SEC:
                        reason, src = cand[1], "symbol_match"
                    elif last_trigger_global and \
                            (ts - last_trigger_global[0]).total_seconds() <= TRIGGER_MAX_AGE_SEC:
                        reason, src = last_trigger_global[1], "global_fallback"
                last_trigger_sym.pop(sym, None)

                res.trades.append(Trade(
                    entry_ts=entry["ts"] if entry else None,
                    exit_ts=ts,
                    symbol=sym or "UNKNOWN",
                    qty=entry["qty"] if entry else 0.0,
                    entry_avg=entry["avg"] if entry else 0.0,
                    pnl=pnl,
                    exit_reason=reason,
                    reason_source=src,
                    log_format=fmt,
                    matched_entry=entry is not None,
                ))
                continue

            om = RE_OFFLINE.match(msg)
            if om:
                res.offline_events.append({
                    "ts": ts, "avg_pnl": float(om.group("avg")), "n": int(om.group("n")),
                    "score_th": float(om.group("s")), "mom_th": float(om.group("m")),
                })
                continue
            onm = RE_ONLINE_SET.match(msg)
            if onm:
                res.online_events.append({
                    "ts": ts, "window": int(onm.group("w")), "mean": float(onm.group("mean")),
                    "score_th": float(onm.group("s")), "mom_th": float(onm.group("m")),
                    "kind": "set",
                })
                continue
            onn = RE_ONLINE_NEUTRAL.match(msg)
            if onn:
                res.online_events.append({
                    "ts": ts, "window": int(onn.group("w")), "mean": float(onn.group("mean")),
                    "score_th": None, "mom_th": None, "kind": "neutral_reset",
                })
                continue
            ons = RE_ONLINE_SMALL.match(msg)
            if ons:
                res.online_events.append({
                    "ts": ts, "window": int(ons.group("w")), "mean": None,
                    "score_th": None, "mom_th": None, "kind": "too_few_trades",
                })
                continue

            dm = RE_DIAG.match(msg)
            if dm:
                res.diag_lines += 1
                body = dm.group("body")
                thm = RE_DIAG_TH.search(body)
                if thm:
                    res.diag_threshold_trace.append(
                        (ts, float(thm.group("s")), float(thm.group("m"))))
                if " | Blocker: " in body:
                    blk = body.split(" | Blocker: ", 1)[1].split(" | Detail: ")[0]
                    res.diag_blockers[_norm_blocker(blk)] += 1
                elif " | Detail: " not in body and " | " in body:
                    res.diag_blockers[_norm_blocker(body.split(" | ", 1)[1])] += 1
                if " | Detail: " in body:
                    detail = body.split(" | Detail: ", 1)[1]
                    for tok in detail.split():
                        res.diag_rejections[_norm_reject(tok)] += 1
                continue

    for sym, e in pending.items():
        res.orphan_entries.append({"ts": e["ts"].isoformat(), "symbol": sym,
                                   "note": "open at end of log / never observed closing"})
    return res


def _norm_blocker(s: str) -> str:
    return re.sub(r"\d+", "N", s.strip())[:110]


def _norm_reject(tok: str) -> str:
    """Collapse 'SQQQ:spread=0.00203>0.00100' -> 'spread_too_wide'."""
    rest = tok.split(":", 1)[1] if ":" in tok else tok
    table = [
        ("spread=", "spread_too_wide"),
        ("mom=", "momentum_below_threshold"),
        ("score=", "score_below_threshold"),
        ("cooldown_until", "symbol_entry_cooldown"),
        ("thin_bars", "no_symbols_scored:thin_bars"),
        ("avg_vol", "no_symbols_scored:low_avg_volume"),
        ("bad_past_or_vol", "no_symbols_scored:bad_past_or_vol"),
        ("no_bars_frame", "no_symbols_scored:no_bars_frame"),
        ("passed_filters", "passed_filters(unexpected)"),
        ("blocked_by_SPY", "spy_market_filter"),
        ("SPY_market_filter", "spy_market_filter_blocks_all"),
        ("scoring_bars_unavailable", "scoring_bars_unavailable"),
    ]
    for pref, name in table:
        if rest.startswith(pref):
            return name
    exact = {
        "held": "already_held",
        "no_quote": "no_quote",
        "dual_ma_not_uptrend": "dual_ma_not_uptrend",
        "candlestick_gate_fail": "candlestick_gate_fail",
        "no_past_slice": "no_past_slice",
        "no_symbols_scored": "no_symbols_scored(header)",
    }
    return exact.get(rest, rest[:50])


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def pnl_stats(trades: List[Trade]) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    p = [t.pnl for t in trades]
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x < 0]
    flats = [x for x in p if x == 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    total = sum(p)
    srt = sorted(p)
    out: Dict[str, Any] = {
        "n": n,
        "total_pnl": round(total, 2),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(flats),
        "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "avg_win": round(statistics.mean(wins), 4) if wins else 0.0,
        "avg_loss": round(statistics.mean(losses), 4) if losses else 0.0,
        "median_win": round(statistics.median(wins), 4) if wins else 0.0,
        "median_loss": round(statistics.median(losses), 4) if losses else 0.0,
        "largest_win": round(max(p), 2),
        "largest_loss": round(min(p), 2),
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy_per_trade": round(total / n, 4),
        "stdev_pnl": round(statistics.pstdev(p), 4) if n > 1 else 0.0,
        "payoff_ratio": (round(statistics.mean(wins) / abs(statistics.mean(losses)), 3)
                         if wins and losses else None),
        "pnl_p10": round(srt[max(0, int(0.10 * n) - 1)], 2),
        "pnl_p50": round(statistics.median(p), 2),
        "pnl_p90": round(srt[min(n - 1, int(0.90 * n))], 2),
    }
    if n > 1 and out["stdev_pnl"] > 0:
        se = statistics.stdev(p) / math.sqrt(n)
        out["t_stat_mean_pnl"] = round((total / n) / se, 2)
    else:
        out["t_stat_mean_pnl"] = None
    for k in (1, 3, 5):
        out[f"total_pnl_ex_top{k}"] = round(sum(sorted(p, reverse=True)[k:]), 2)
    for k in (1, 3, 5):
        out[f"total_pnl_ex_bottom{k}"] = round(sum(sorted(p)[k:]), 2)
    return out


def group_stats(trades: List[Trade], keyfn) -> Dict[Any, Dict[str, Any]]:
    buckets: Dict[Any, List[Trade]] = defaultdict(list)
    for t in trades:
        buckets[keyfn(t)].append(t)
    return {k: pnl_stats(v) for k, v in buckets.items()}


def streak_analysis(trades: List[Trade]) -> Dict[str, Any]:
    """Trades must already be in exit-time order."""
    seq = list(trades)
    after_win, after_loss, after_flat = [], [], []
    for i in range(1, len(seq)):
        prev, cur = seq[i - 1].pnl, seq[i].pnl
        (after_win if prev > 0 else after_loss if prev < 0 else after_flat).append(cur)

    streaks, cur_sign, cur_len = [], None, 0
    for t in seq:
        s = 1 if t.pnl > 0 else (-1 if t.pnl < 0 else 0)
        if s == cur_sign:
            cur_len += 1
        else:
            if cur_sign is not None:
                streaks.append((cur_sign, cur_len))
            cur_sign, cur_len = s, 1
    if cur_sign is not None:
        streaks.append((cur_sign, cur_len))
    win_streaks = [l for s, l in streaks if s == 1]
    loss_streaks = [l for s, l in streaks if s == -1]

    def wr(xs):
        return round(100 * sum(1 for x in xs if x > 0) / len(xs), 1) if xs else None

    return {
        "n_after_win": len(after_win),
        "mean_after_win": round(statistics.mean(after_win), 4) if after_win else None,
        "winrate_after_win_pct": wr(after_win),
        "n_after_loss": len(after_loss),
        "mean_after_loss": round(statistics.mean(after_loss), 4) if after_loss else None,
        "winrate_after_loss_pct": wr(after_loss),
        "n_after_flat": len(after_flat),
        "mean_after_flat": round(statistics.mean(after_flat), 4) if after_flat else None,
        "max_win_streak": max(win_streaks) if win_streaks else 0,
        "max_loss_streak": max(loss_streaks) if loss_streaks else 0,
        "n_streaks": len(streaks),
    }


HOLD_BUCKETS = [(0, 60), (60, 180), (180, 300), (300, 600), (600, 1200),
                (1200, 2400), (2400, 4800), (4800, 10 ** 9)]


def _bucket_label(lo: int, hi: int) -> str:
    return f"{lo // 60}m-{'inf' if hi > 10 ** 8 else str(hi // 60) + 'm'}"


def hold_bucket(sec: Optional[float]) -> str:
    if sec is None:
        return "unknown"
    for lo, hi in HOLD_BUCKETS:
        if lo <= sec < hi:
            return _bucket_label(lo, hi)
    return "unknown"


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

def make_charts(trades: List[Trade], outdir: str) -> List[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # pragma: no cover
        print(f"[charts] matplotlib unavailable: {e}")
        return []

    made = []
    p = [t.pnl for t in trades]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(p, bins=40, color="#3f6fb5", edgecolor="white")
    ax.axvline(0, color="#333", lw=1)
    ax.set_title(f"Closed-trade P&L distribution (n={len(p)})")
    ax.set_xlabel("realized P&L ($)")
    ax.set_ylabel("trades")
    fig.tight_layout()
    f = os.path.join(outdir, "pnl_histogram.png")
    fig.savefig(f, dpi=110)
    plt.close(fig)
    made.append(f)

    eq, run = [], 0.0
    for t in trades:
        run += t.pnl
        eq.append(run)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(range(1, len(eq) + 1), eq, color="#2a7f62")
    ax.axhline(0, color="#999", lw=0.8)
    ax.set_title("Cumulative realized P&L by trade sequence")
    ax.set_xlabel("trade #")
    ax.set_ylabel("cumulative $")
    fig.tight_layout()
    f = os.path.join(outdir, "equity_curve.png")
    fig.savefig(f, dpi=110)
    plt.close(fig)
    made.append(f)

    by = defaultdict(list)
    for t in trades:
        by[t.exit_reason].append(t.pnl)
    keys = sorted(by, key=lambda k: -sum(by[k]))
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar(keys, [sum(by[k]) for k in keys],
           color=["#2a7f62" if sum(by[k]) >= 0 else "#b5453f" for k in keys])
    for i, k in enumerate(keys):
        tot = sum(by[k])
        ax.text(i, tot, f"n={len(by[k])}", ha="center",
                va="bottom" if tot >= 0 else "top", fontsize=8)
    ax.axhline(0, color="#333", lw=0.8)
    ax.set_title("Total realized P&L by exit reason")
    ax.set_ylabel("$")
    plt.xticks(rotation=25, ha="right")
    fig.tight_layout()
    f = os.path.join(outdir, "pnl_by_exit_reason.png")
    fig.savefig(f, dpi=110)
    plt.close(fig)
    made.append(f)

    pts = [(t.hold_sec / 60.0, t.pnl) for t in trades if t.hold_sec is not None]
    if pts:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.scatter([x for x, _ in pts], [y for _, y in pts], s=18, alpha=0.7,
                   c=["#2a7f62" if y > 0 else "#b5453f" for _, y in pts])
        ax.axhline(0, color="#333", lw=0.8)
        ax.set_xscale("symlog")
        ax.set_title(f"P&L vs holding time (n={len(pts)})")
        ax.set_xlabel("holding minutes (symlog)")
        ax.set_ylabel("$")
        fig.tight_layout()
        f = os.path.join(outdir, "pnl_vs_holdtime.png")
        fig.savefig(f, dpi=110)
        plt.close(fig)
        made.append(f)

    by_h = defaultdict(list)
    for t in trades:
        if t.entry_ts:
            by_h[t.entry_ts.hour].append(t.pnl)
    if by_h:
        hs = sorted(by_h)
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.bar([str(h) for h in hs], [sum(by_h[h]) for h in hs],
               color=["#2a7f62" if sum(by_h[h]) >= 0 else "#b5453f" for h in hs])
        for i, h in enumerate(hs):
            tot = sum(by_h[h])
            ax.text(i, tot, f"n={len(by_h[h])}", ha="center",
                    va="bottom" if tot >= 0 else "top", fontsize=8)
        ax.axhline(0, color="#333", lw=0.8)
        ax.set_title("Total realized P&L by entry hour (log local time, ET)")
        ax.set_xlabel("hour")
        ax.set_ylabel("$")
        fig.tight_layout()
        f = os.path.join(outdir, "pnl_by_entry_hour.png")
        fig.savefig(f, dpi=110)
        plt.close(fig)
        made.append(f)
    return made


def make_threshold_chart(res: ParseResult, outdir: str) -> Optional[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    tr = res.diag_threshold_trace
    if not tr:
        return None
    xs = [t for t, _, _ in tr]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(xs, [s for _, s, _ in tr], label="entry_score_threshold", lw=1)
    ax.plot(xs, [m for _, _, m in tr], label="min_momentum_return", lw=1)
    ax.axhline(0.0, color="#999", lw=0.8, ls="--", label="config default (0.0)")
    ax.set_title(f"Auto-tuned entry thresholds over time (n={len(tr)} diagnostic samples)")
    ax.legend(fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    f = os.path.join(outdir, "threshold_drift.png")
    fig.savefig(f, dpi=110)
    plt.close(fig)
    return f


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def print_report(res: ParseResult, legacy: Optional[ParseResult] = None) -> Dict[str, Any]:
    T = res.trades
    out: Dict[str, Any] = {}

    def head(s):
        print("\n" + "=" * 78)
        print(s)
        print("=" * 78)

    head("0. LOG COVERAGE")
    print(f"lines={res.total_lines:,} timestamped_records={res.total_records:,}")
    print(f"range: {res.first_ts} .. {res.last_ts}")
    print(f"ENTRY filled events: {res.entries_filled}   closed trades parsed: {len(T)}")
    print(f"  with-symbol exit format: {sum(1 for t in T if t.log_format == 'with_symbol')}")
    print(f"  legacy no-symbol format: {sum(1 for t in T if t.log_format == 'no_symbol')}")
    print(f"  exits with no matching ENTRY line: {res.unmatched_exits}")
    print(f"  Placed ENTRY orders: {len(res.placed_entries)}  -> fill ratio "
          f"{res.entries_filled}/{len(res.placed_entries)} = "
          f"{100 * res.entries_filled / max(1, len(res.placed_entries)):.1f}%")
    out["coverage"] = {
        "lines": res.total_lines, "records": res.total_records,
        "first_ts": str(res.first_ts), "last_ts": str(res.last_ts),
        "entries_filled": res.entries_filled, "trades": len(T),
        "placed_entries": len(res.placed_entries),
        "unmatched_exits": res.unmatched_exits,
    }

    head("1. WIN/LOSS DISTRIBUTION - ALL PARSED TRADES")
    out["all"] = pnl_stats(T)
    for k, v in out["all"].items():
        print(f"  {k:28s} {v}")

    cur = [t for t in T if t.log_format == "with_symbol"]
    leg = [t for t in T if t.log_format == "no_symbol"]

    head("1b. CURRENT-FORMAT SUBSET (what the existing dashboard parser sees)")
    out["current_format"] = pnl_stats(cur)
    for k, v in out["current_format"].items():
        print(f"  {k:28s} {v}")

    head("1c. LEGACY NO-SYMBOL SUBSET (single-position era)")
    out["legacy_format"] = pnl_stats(leg)
    for k, v in out["legacy_format"].items():
        print(f"  {k:28s} {v}")

    head("2. EXIT REASON BREAKDOWN (all trades)")
    g = group_stats(T, lambda t: t.exit_reason)
    rows = sorted(g.items(), key=lambda kv: -kv[1]["total_pnl"])
    print(f"  {'reason':24s} {'n':>4s} {'total$':>9s} {'avg$':>8s} {'win%':>6s} "
          f"{'avgWin':>8s} {'avgLoss':>8s} {'PF':>6s}")
    for k, s in rows:
        pf = s["profit_factor"]
        print(f"  {k:24s} {s['n']:>4d} {s['total_pnl']:>9.2f} {s['expectancy_per_trade']:>8.3f} "
              f"{s['win_rate_pct']:>6.1f} {s['avg_win']:>8.3f} {s['avg_loss']:>8.3f} "
              f"{(pf if pf is not None else float('nan')):>6.2f}")
    out["by_exit_reason"] = dict(rows)
    print("\n  attribution source counts:", dict(Counter(t.reason_source for t in T)))
    out["reason_source"] = dict(Counter(t.reason_source for t in T))

    print("\n  --- same, current-format trades only ---")
    g2 = group_stats(cur, lambda t: t.exit_reason)
    for k, s in sorted(g2.items(), key=lambda kv: -kv[1]["total_pnl"]):
        print(f"  {k:24s} n={s['n']:<4d} total={s['total_pnl']:>8.2f} "
              f"avg={s['expectancy_per_trade']:>7.3f} win%={s['win_rate_pct']:>5.1f}")
    out["by_exit_reason_current"] = g2

    head("3. HOLDING TIME")
    hs = [t.hold_sec for t in T if t.hold_sec is not None]
    if hs:
        srt = sorted(hs)
        print(f"  n_with_entry={len(hs)}  median={statistics.median(hs) / 60:.1f}m "
              f"mean={statistics.mean(hs) / 60:.1f}m p10={srt[int(.1 * len(hs))] / 60:.1f}m "
              f"p90={srt[int(.9 * len(hs))] / 60:.1f}m max={max(hs) / 60:.1f}m")
        w = [t.hold_sec for t in T if t.hold_sec is not None and t.pnl > 0]
        l = [t.hold_sec for t in T if t.hold_sec is not None and t.pnl < 0]
        print(f"  winners: n={len(w)} median_hold={statistics.median(w) / 60:.1f}m "
              f"mean={statistics.mean(w) / 60:.1f}m")
        print(f"  losers : n={len(l)} median_hold={statistics.median(l) / 60:.1f}m "
              f"mean={statistics.mean(l) / 60:.1f}m")
        out["hold"] = {
            "n": len(hs), "median_min": round(statistics.median(hs) / 60, 2),
            "mean_min": round(statistics.mean(hs) / 60, 2),
            "winner_median_min": round(statistics.median(w) / 60, 2) if w else None,
            "loser_median_min": round(statistics.median(l) / 60, 2) if l else None,
            "winner_n": len(w), "loser_n": len(l),
        }
    gb = group_stats([t for t in T if t.hold_sec is not None], lambda t: hold_bucket(t.hold_sec))
    print(f"\n  {'bucket':12s} {'n':>4s} {'total$':>9s} {'avg$':>8s} {'win%':>6s}")
    for lo, hi in HOLD_BUCKETS:
        b = _bucket_label(lo, hi)
        if b in gb:
            s = gb[b]
            print(f"  {b:12s} {s['n']:>4d} {s['total_pnl']:>9.2f} "
                  f"{s['expectancy_per_trade']:>8.3f} {s['win_rate_pct']:>6.1f}")
    out["by_hold_bucket"] = gb

    head("4. TIME OF DAY (log local time = ET; EOD flatten lines land at 15:55)")
    gh = group_stats([t for t in T if t.entry_ts], lambda t: t.entry_ts.hour)
    print(f"  {'hour':5s} {'n':>4s} {'total$':>9s} {'avg$':>8s} {'win%':>6s}")
    for h in sorted(gh):
        s = gh[h]
        print(f"  {h:>4d}: {s['n']:>4d} {s['total_pnl']:>9.2f} "
              f"{s['expectancy_per_trade']:>8.3f} {s['win_rate_pct']:>6.1f}")
    out["by_entry_hour"] = gh

    g30 = group_stats([t for t in T if t.entry_ts],
                      lambda t: f"{t.entry_ts.hour:02d}:{'00' if t.entry_ts.minute < 30 else '30'}")
    print(f"\n  {'bucket':8s} {'n':>4s} {'total$':>9s} {'avg$':>8s} {'win%':>6s}")
    for k in sorted(g30):
        s = g30[k]
        print(f"  {k:8s} {s['n']:>4d} {s['total_pnl']:>9.2f} "
              f"{s['expectancy_per_trade']:>8.3f} {s['win_rate_pct']:>6.1f}")
    out["by_entry_30min"] = g30

    head("5. PER SYMBOL")
    gs = group_stats(T, lambda t: t.symbol)
    print(f"  {'sym':8s} {'grp':8s} {'n':>4s} {'total$':>9s} {'avg$':>8s} {'win%':>6s} {'PF':>6s}")
    for k in sorted(gs, key=lambda k: -gs[k]["total_pnl"]):
        s = gs[k]
        grp = "current" if k in CURRENT_UNIVERSE else ("legacy" if k in LEGACY_UNIVERSE else "?")
        pf = s["profit_factor"]
        print(f"  {k:8s} {grp:8s} {s['n']:>4d} {s['total_pnl']:>9.2f} "
              f"{s['expectancy_per_trade']:>8.3f} {s['win_rate_pct']:>6.1f} "
              f"{(pf if pf is not None else float('nan')):>6.2f}")
    out["by_symbol"] = gs

    cur_u = [t for t in T if t.symbol in CURRENT_UNIVERSE]
    leg_u = [t for t in T if t.symbol in LEGACY_UNIVERSE]
    out["universe_current"] = pnl_stats(cur_u)
    out["universe_legacy"] = pnl_stats(leg_u)
    print("\n  CURRENT leveraged-ETF universe:", out["universe_current"])
    print("\n  LEGACY index-ETF universe    :", out["universe_legacy"])

    print("\n  --- exit-reason breakdown, CURRENT universe only ---")
    gcu = group_stats(cur_u, lambda t: t.exit_reason)
    for k, s in sorted(gcu.items(), key=lambda kv: -kv[1]["total_pnl"]):
        print(f"  {k:24s} n={s['n']:<4d} total={s['total_pnl']:>8.2f} "
              f"avg={s['expectancy_per_trade']:>7.3f} win%={s['win_rate_pct']:>5.1f}")
    out["by_exit_reason_current_universe"] = gcu

    head("6. WHY NO BUY - entry diagnostic tallies")
    print(f"  entry-diagnostic log lines: {res.diag_lines}")
    tot = sum(res.diag_rejections.values())
    print(f"  per-symbol rejection tokens: {tot}")
    for k, v in res.diag_rejections.most_common(30):
        print(f"    {k:34s} {v:>7d}  {100 * v / max(1, tot):5.1f}%")
    print("\n  top-level blockers:")
    tb = sum(res.diag_blockers.values())
    for k, v in res.diag_blockers.most_common(20):
        print(f"    {k:72s} {v:>7d}  {100 * v / max(1, tb):5.1f}%")
    out["diag_rejections"] = dict(res.diag_rejections)
    out["diag_blockers"] = dict(res.diag_blockers)

    days = defaultdict(int)
    for t in T:
        days[t.exit_ts.date()] += 1
    if days:
        vals = sorted(days.values())
        print(f"\n  trading days with >=1 closed trade: {len(days)}")
        print(f"  trades/day: mean={statistics.mean(vals):.2f} "
              f"median={statistics.median(vals)} max={max(vals)}")
        out["trades_per_day"] = {"days": len(days), "mean": round(statistics.mean(vals), 2),
                                 "median": statistics.median(vals), "max": max(vals)}
    entry_days = defaultdict(int)
    for e in res.placed_entries:
        entry_days[e["ts"].date()] += 1
    if entry_days:
        print(f"  days with >=1 ENTRY order placed: {len(entry_days)} "
              f"(mean {statistics.mean(list(entry_days.values())):.2f} orders/day)")
        out["entry_order_days"] = len(entry_days)

    head("7. SEQUENCING / STREAKS")
    out["streaks"] = streak_analysis(T)
    for k, v in out["streaks"].items():
        print(f"  {k:28s} {v}")
    print("\n  same, current-format only:")
    out["streaks_current"] = streak_analysis(cur)
    for k, v in out["streaks_current"].items():
        print(f"  {k:28s} {v}")

    head("8. AUTO-TUNING (offline + online trainer)")
    print(f"  'Offline training applied' events: {len(res.offline_events)}")
    if res.offline_events:
        ss = [e["score_th"] for e in res.offline_events]
        mm = [e["mom_th"] for e in res.offline_events]
        print(f"    score_th: min={min(ss):.6f} max={max(ss):.6f} "
              f"distinct={len(set(ss))} last={ss[-1]:.6f}")
        print(f"    mom_th  : min={min(mm):.6f} max={max(mm):.6f} "
              f"distinct={len(set(mm))} last={mm[-1]:.6f}")
        changes = sum(1 for a, b in zip(ss, ss[1:]) if b != a)
        reversals = sum(1 for a, b, c in zip(ss, ss[1:], ss[2:]) if (b - a) * (c - b) < 0)
        print(f"    changes={changes} direction_reversals={reversals}")
        out["offline"] = {"n": len(res.offline_events), "score_min": min(ss),
                          "score_max": max(ss), "distinct_score": len(set(ss)),
                          "changes": changes, "reversals": reversals,
                          "last_score": ss[-1], "last_mom": mm[-1]}
        print("    first 3:", [(str(e['ts']), e['n'], e['avg_pnl'], e['score_th'])
                               for e in res.offline_events[:3]])
        print("    last  3:", [(str(e['ts']), e['n'], e['avg_pnl'], e['score_th'])
                               for e in res.offline_events[-3:]])
    kinds = Counter(e["kind"] for e in res.online_events)
    print(f"\n  'Online training:' events: {len(res.online_events)} -> {dict(kinds)}")
    for e in res.online_events[:5]:
        print("    ", str(e["ts"]), e["kind"], "mean=", e["mean"], "score_th=", e["score_th"])
    if len(res.online_events) > 10:
        print("     ...")
    for e in res.online_events[-5:]:
        print("    ", str(e["ts"]), e["kind"], "mean=", e["mean"], "score_th=", e["score_th"])
    out["online"] = {"n": len(res.online_events), "kinds": dict(kinds)}

    if res.diag_threshold_trace:
        ss = [s for _, s, _ in res.diag_threshold_trace]
        mm = [m for _, _, m in res.diag_threshold_trace]
        print(f"\n  thresholds observed in entry diagnostics (n={len(ss)} samples):")
        print(f"    score_th distinct: {sorted(set(round(x, 6) for x in ss))}")
        print(f"    mom_th   distinct: {sorted(set(round(x, 6) for x in mm))}")
        print(f"    fraction of samples with score_th != 0.0: "
              f"{100 * sum(1 for x in ss if abs(x) > 1e-9) / len(ss):.1f}%")
        out["threshold_values"] = {"score": sorted(set(round(x, 6) for x in ss)),
                                   "mom": sorted(set(round(x, 6) for x in mm))}

    head("9. OPERATIONAL EVENTS / BUGS")
    for k, v in res.ops.most_common():
        print(f"  {k:34s} {v:>7d}")
    print("\n  HTTP error breakdown (from 'Unhandled error in main loop'):")
    for k, v in res.http_errors.most_common(15):
        print(f"    {k:40s} {v:>7d}")
    print(f"\n  orphan / never-closed entry legs: {len(res.orphan_entries)}")
    for o in res.orphan_entries[:12]:
        print("   ", o)
    out["ops"] = dict(res.ops)
    out["http_errors"] = dict(res.http_errors)
    out["orphan_entries"] = res.orphan_entries
    out["trailing_updates"] = dict(res.trailing_updates)
    print(f"\n  trailing-stop raises: {dict(res.trailing_updates)}")

    print("\n  representative lines:")
    for k in ["stop_place_failed_safe_stop", "stop_place_failed_halt", "stale_market_data",
              "unhandled_loop_error", "entry_unfilled_on_cancel", "stale_leg_removed",
              "clock_fetch_failed"]:
        if k in res.sample_lines:
            print(f"    [{k}] {res.sample_lines[k][:230]}")

    if legacy is not None:
        head("10. LEGACY ROOT bot.log")
        print(f"lines={legacy.total_lines:,} records={legacy.total_records:,} "
              f"range {legacy.first_ts} .. {legacy.last_ts}")
        print(f"closed trades: {len(legacy.trades)}  entries filled: {legacy.entries_filled}")
        print("ops:", dict(legacy.ops.most_common(12)))
        print("http:", dict(legacy.http_errors.most_common(8)))
        out["legacy_log"] = {
            "lines": legacy.total_lines, "records": legacy.total_records,
            "trades": len(legacy.trades), "entries": legacy.entries_filled,
            "ops": dict(legacy.ops), "http": dict(legacy.http_errors),
            "stats": pnl_stats(legacy.trades),
        }
        print("stats:", out["legacy_log"]["stats"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--legacy-log", default=LEGACY_LOG)
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--dump-trades", default=os.path.join(HERE, "trades.json"))
    ap.add_argument("--dump-stats", default=os.path.join(HERE, "stats.json"))
    a = ap.parse_args()

    res = parse_log(a.log)
    legacy = parse_log(a.legacy_log) if (a.legacy_log and os.path.exists(a.legacy_log)) else None
    stats = print_report(res, legacy)

    if a.dump_trades:
        with open(a.dump_trades, "w", encoding="utf-8") as fh:
            json.dump([t.to_dict() for t in res.trades], fh, indent=1)
        print(f"\n[wrote] {a.dump_trades}")
    if a.dump_stats:
        with open(a.dump_stats, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, indent=1, default=str)
        print(f"[wrote] {a.dump_stats}")

    if not a.no_charts:
        made = make_charts(res.trades, HERE)
        t = make_threshold_chart(res, HERE)
        if t:
            made.append(t)
        for f in made:
            print(f"[chart] {f}")


if __name__ == "__main__":
    main()
