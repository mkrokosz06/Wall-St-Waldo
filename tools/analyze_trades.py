#!/usr/bin/env python3
"""
Analysis pass over the ledger produced by tools/mine_trades.py, plus the
non-trade forensics (training-knob timeline, entry blockers, error tallies)
which are re-mined from the raw logs.

    python tools/analyze_trades.py            # prints every table
    python tools/analyze_trades.py --csv docs/trades.csv

Everything printed here is what docs/log_analysis.md quotes.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mine_trades import load_lines, mine, default_paths  # noqa: E402

import csv


def fmt(x, n=2):
    return ("%%.%df" % n) % x


def table(rows, headers):
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0)) for i, h in enumerate(headers)]
    line = "| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    out = [line, sep]
    for r in rows:
        out.append("| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)) + " |")
    return "\n".join(out)


def stats(pnls):
    n = len(pnls)
    if n == 0:
        return dict(n=0, total=0.0, win=0, wr=0.0, avg=0.0, avg_win=0.0, avg_loss=0.0, pf=float("nan"))
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp = sum(wins)
    gl = -sum(losses)
    return dict(
        n=n,
        total=sum(pnls),
        win=len(wins),
        loss=len(losses),
        flat=n - len(wins) - len(losses),
        wr=100.0 * len(wins) / n,
        avg=sum(pnls) / n,
        avg_win=(gp / len(wins)) if wins else 0.0,
        avg_loss=(-gl / len(losses)) if losses else 0.0,
        pf=(gp / gl) if gl > 0 else float("inf"),
        best=max(pnls),
        worst=min(pnls),
    )


def bootstrap_ci(pnls, iters=20000, seed=7):
    rnd = random.Random(seed)
    n = len(pnls)
    if n == 0:
        return (0.0, 0.0)
    means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += pnls[rnd.randrange(n)]
        means.append(s / n)
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def main():
    here, logs = default_paths()
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(here, "docs", "trades.csv"))
    ap.add_argument("--logs", nargs="+", default=logs)
    a = ap.parse_args()

    with open(a.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["pnl"] = float(r["pnl"])
        r["hold_min"] = float(r["hold_min"]) if r["hold_min"] not in ("", "nan") else float("nan")
        r["qty"] = float(r["qty"])
        r["entry_price"] = float(r["entry_price"])

    real = [r for r in rows if r["entry_ts"]]  # matched round trips only
    pnls = [r["pnl"] for r in real]

    print("=" * 78)
    print("HEADLINE")
    print("=" * 78)
    s = stats(pnls)
    lo, hi = bootstrap_ci(pnls)
    print("matched round-trips: %d   unmatched exit records: %d" % (len(real), len(rows) - len(real)))
    print("total P&L $%.2f | win rate %.1f%% (%dW/%dL/%dBE) | expectancy $%.3f/trade" % (
        s["total"], s["wr"], s["win"], s["loss"], s["flat"], s["avg"]))
    print("avg win $%.3f | avg loss $%.3f | payoff %.2fx | profit factor %.2f" % (
        s["avg_win"], s["avg_loss"], (s["avg_win"] / abs(s["avg_loss"]) if s["avg_loss"] else float("nan")), s["pf"]))
    print("best $%.2f | worst $%.2f" % (s["best"], s["worst"]))
    print("bootstrap 95%% CI on expectancy: [$%.3f, $%.3f]  (20k resamples)" % (lo, hi))
    srt = sorted(pnls)
    print("total ex-best-3 : $%.2f   (best 3 = %s)" % (sum(srt[:-3]), [round(x, 2) for x in srt[-3:]]))
    print("total ex-best-5 : $%.2f" % sum(srt[:-5]))
    print("total ex-worst-3: $%.2f" % sum(srt[3:]))
    print("top-3 share of gross profit: %.1f%%" % (100 * sum(srt[-3:]) / sum(p for p in pnls if p > 0)))
    zero = [r for r in real if r["pnl"] == 0.0]
    print("zero-P&L records: %d (phantom 'EXIT filled' from _execute_market_exit when position already gone)" % len(zero))

    print()
    print("=" * 78)
    print("EXIT REASON BREAKDOWN")
    print("=" * 78)
    by = defaultdict(list)
    for r in real:
        by[r["exit_reason"]].append(r["pnl"])
    rows_t = []
    for k, v in sorted(by.items(), key=lambda kv: sum(kv[1])):
        st = stats(v)
        l, h = bootstrap_ci(v, iters=5000)
        rows_t.append([k, st["n"], fmt(st["total"]), "%.0f%%" % st["wr"], fmt(st["avg"], 3),
                       fmt(st["avg_win"], 3), fmt(st["avg_loss"], 3),
                       "[%.2f, %.2f]" % (l, h)])
    print(table(rows_t, ["exit_reason", "n", "P&L $", "win%", "exp $", "avg win", "avg loss", "95% CI exp"]))

    print()
    print("-- exit reason x era --")
    rows_t = []
    for era in ("legacy", "leveraged"):
        for k in sorted(by):
            v = [r["pnl"] for r in real if r["exit_reason"] == k and r["era"] == era]
            if v:
                st = stats(v)
                rows_t.append([era, k, st["n"], fmt(st["total"]), "%.0f%%" % st["wr"], fmt(st["avg"], 3)])
    print(table(rows_t, ["era", "exit_reason", "n", "P&L $", "win%", "exp $"]))

    print()
    print("=" * 78)
    print("HOLD TIME")
    print("=" * 78)
    hold = [r for r in real if r["hold_min"] == r["hold_min"]]
    hs = sorted(r["hold_min"] for r in hold)
    def pct(p):
        return hs[min(len(hs) - 1, int(p * len(hs)))]
    print("n=%d  min=%.1fm p25=%.1fm median=%.1fm p75=%.1fm p90=%.1fm max=%.1fm mean=%.1fm" % (
        len(hs), hs[0], pct(.25), pct(.5), pct(.75), pct(.9), hs[-1], sum(hs) / len(hs)))
    buckets = [(0, 2), (2, 5), (5, 10), (10, 20), (20, 45), (45, 120), (120, 10 ** 9)]
    rows_t = []
    for lo_, hi_ in buckets:
        v = [r["pnl"] for r in hold if lo_ <= r["hold_min"] < hi_]
        if v:
            st = stats(v)
            rows_t.append(["%g-%s min" % (lo_, "inf" if hi_ > 10 ** 8 else "%g" % hi_), st["n"],
                           fmt(st["total"]), "%.0f%%" % st["wr"], fmt(st["avg"], 3)])
    print(table(rows_t, ["hold bucket", "n", "P&L $", "win%", "exp $"]))
    w = [r["hold_min"] for r in hold if r["pnl"] > 0]
    l = [r["hold_min"] for r in hold if r["pnl"] < 0]
    print("median hold  winners %.1f min (n=%d) | losers %.1f min (n=%d)" % (
        sorted(w)[len(w) // 2], len(w), sorted(l)[len(l) // 2], len(l)))

    print()
    print("=" * 78)
    print("TIME OF DAY (ET, bucketed by ENTRY time)")
    print("=" * 78)
    rows_t = []
    tod = defaultdict(list)
    for r in real:
        t = datetime.strptime(r["entry_ts"], "%Y-%m-%d %H:%M:%S")
        key = "%02d:%02d" % (t.hour, 0 if t.minute < 30 else 30)
        tod[key].append(r["pnl"])
    for k in sorted(tod):
        st = stats(tod[k])
        rows_t.append([k, st["n"], fmt(st["total"]), "%.0f%%" % st["wr"], fmt(st["avg"], 3)])
    print(table(rows_t, ["ET half-hour", "n", "P&L $", "win%", "exp $"]))
    first30 = [p for k, v in tod.items() if k in ("09:30", "10:00") for p in v]
    print("entries before 10:00 ET: %d (open delay = 15 min, so 09:45-10:00 is the only exposed slice)" % len(tod.get("09:30", [])))

    print()
    print("=" * 78)
    print("SYMBOL / ERA")
    print("=" * 78)
    rows_t = []
    for sym in sorted({r["symbol"] for r in real}):
        v = [r["pnl"] for r in real if r["symbol"] == sym]
        st = stats(v)
        l2, h2 = bootstrap_ci(v, iters=5000)
        rows_t.append([sym, real[[r["symbol"] for r in real].index(sym)]["era"], st["n"], fmt(st["total"]),
                       "%.0f%%" % st["wr"], fmt(st["avg"], 3), "[%.2f, %.2f]" % (l2, h2)])
    print(table(rows_t, ["symbol", "era", "n", "P&L $", "win%", "exp $", "95% CI exp"]))
    print()
    for era in ("legacy", "leveraged"):
        v = [r["pnl"] for r in real if r["era"] == era]
        st = stats(v)
        l2, h2 = bootstrap_ci(v)
        span = sorted(r["entry_ts"][:10] for r in real if r["era"] == era)
        notional = [r["qty"] * r["entry_price"] for r in real if r["era"] == era]
        print("%-10s n=%3d  P&L $%7.2f  win%% %.0f  exp $%.3f  CI [%.3f, %.3f]  median notional $%.0f  %s..%s" % (
            era, st["n"], st["total"], st["wr"], st["avg"], l2, h2,
            sorted(notional)[len(notional) // 2], span[0], span[-1]))

    print()
    print("=" * 78)
    print("TRAINING KNOBS + BLOCKERS + ERRORS (re-mined from raw logs)")
    print("=" * 78)
    res = mine(load_lines(a.logs))
    ke = res["knob_events"]
    print("knob-change log events: %d" % len(ke))
    print(Counter(e["kind"] for e in ke))
    vals = [e["score_th"] for e in ke if e["kind"] != "neutral_reset"]
    if vals:
        print("distinct score_th values ever set: %s" % sorted(set(round(v, 6) for v in vals)))
        print("distinct mom_th   values ever set: %s" % sorted(set(round(e["mom_th"], 6) for e in ke if e["kind"] != "neutral_reset")))
    flips = sum(1 for i in range(1, len(ke)) if (ke[i]["score_th"] > ke[i - 1]["score_th"]) != (ke[i]["score_th"] >= ke[i - 1]["score_th"]))
    ups = sum(1 for i in range(1, len(ke)) if ke[i]["score_th"] > ke[i - 1]["score_th"])
    downs = sum(1 for i in range(1, len(ke)) if ke[i]["score_th"] < ke[i - 1]["score_th"])
    print("score_th step ups=%d downs=%d unchanged=%d  (a random walk has ups~downs)" % (
        ups, downs, len(ke) - 1 - ups - downs))

    # Does the nudge predict the next trade? Pair each knob event with the next trade after it.
    ev = sorted(ke, key=lambda e: e["ts"])
    tr_sorted = sorted(real, key=lambda r: r["exit_ts"])
    after_loosen, after_tighten = [], []
    for e in ev:
        if e["kind"] == "neutral_reset":
            continue
        nxt = None
        for r in tr_sorted:
            if r["entry_ts"] > e["ts"]:
                nxt = r
                break
        if nxt is None:
            continue
        (after_loosen if e["driver"] > 0 else after_tighten).append(nxt["pnl"])
    for name, v in (("after a LOOSEN nudge (recent P&L +)", after_loosen),
                    ("after a TIGHTEN nudge (recent P&L -)", after_tighten)):
        if v:
            st = stats(v)
            l2, h2 = bootstrap_ci(v, iters=5000)
            print("next-trade %s: n=%d exp $%.3f win%% %.0f CI [%.3f, %.3f]" % (name, st["n"], st["avg"], st["wr"], l2, h2))

    print()
    print("-- entry blocker tally (from 'Entry diagnostic' lines) --")
    blk = Counter()
    pre = Counter()
    for ts, body in res["diagnostics"]:
        m = re.search(r"Blocker: ([^|]+)", body)
        if m:
            pre[m.group(1).strip().split("(")[0]] += 1
        d = re.search(r"Detail: (.*)$", body)
        if d:
            for tok in d.group(1).split():
                if ":" in tok:
                    _, reason = tok.split(":", 1)
                    reason = re.sub(r"=.*", "", reason).split("(")[0]
                    blk[reason] += 1
                elif tok.startswith("scoring_bars_unavailable") or tok.startswith("SPY_market_filter"):
                    blk[tok.split("(")[0]] += 1
    print("diagnostic lines: %d" % len(res["diagnostics"]))
    print("pre-scan blockers:", pre.most_common(12))
    print("per-candidate rejection reasons:")
    tot = sum(blk.values())
    for k, v in blk.most_common(15):
        print("   %-32s %6d  %5.1f%%" % (k, v, 100.0 * v / tot))

    print()
    print("-- operational errors --")
    print("counters:", dict(sorted(res["counters"].items())))
    print("dup/overwritten ENTRY-filled records: %d ; orphan exits: %d ; still open at EOF: %s" % (
        res["dup_entries"], res["orphan_exits"], res["open_at_end"]))
    print("top WARNING kinds:")
    for k, v in Counter(res["warn_kinds"]).most_common(8):
        print("   %6d  %s" % (v, k))
    print("top ERROR kinds:")
    for k, v in Counter(res["err_kinds"]).most_common(8):
        print("   %6d  %s" % (v, k))


if __name__ == "__main__":
    main()
