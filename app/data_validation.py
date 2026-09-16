"""
Per-symbol bar and quote validation (Phase 6).

The old staleness guard was portfolio-wide: it took the newest bar across *all*
symbols and, if that was too old, skipped signal evaluation entirely. Two
distinct faults followed.

1. **Cross-contamination.** A fresh TQQQ bar could authorize trading on a SOXL
   bar twenty minutes old, because only the newest of the two was examined. And
   in the other direction, one bad symbol blocked every good candidate.
2. **A frozen reference clock.** Age was measured against a broker timestamp
   cached for 60 seconds, so within that window "now" did not advance and data
   kept looking as fresh as it did on the first check.

This module validates each symbol independently, against a clock that actually
moves, and reports a specific reason per symbol. It also adds quote validation,
which did not exist: a quote was used if it was present, regardless of age or
whether bid/ask made any sense.

Pure: it takes timestamps and numbers, never fetches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

# Rejection reasons. Specific because the log needs to say which one.
OK = "ok"
NO_BARS = "no_bars"
BAR_STALE = "bar_stale"
BAR_INCOMPLETE = "bar_incomplete"
BAR_FUTURE = "bar_future"
NO_QUOTE = "no_quote"
QUOTE_NO_TIMESTAMP = "quote_no_timestamp"
QUOTE_STALE = "quote_stale"
QUOTE_FUTURE = "quote_future"
QUOTE_CROSSED = "quote_crossed"
QUOTE_NONPOSITIVE = "quote_nonpositive"
QUOTE_NOT_FINITE = "quote_not_finite"
CLOCK_UNTRUSTED = "clock_untrusted"

# A little tolerance for clock skew between us and the venue. Small and explicit:
# accepting arbitrarily future-dated data would let a bad timestamp look fresh
# forever.
DEFAULT_FUTURE_TOLERANCE_SEC = 5.0


@dataclass
class Check:
    """Outcome of one validation, with the numbers that produced it."""

    ok: bool
    reason: str
    symbol: str = ""
    age_sec: Optional[float] = None
    detail: str = ""

    def describe(self) -> str:
        bits = [f"{self.symbol}:{self.reason}"]
        if self.age_sec is not None:
            bits.append(f"age={self.age_sec:.0f}s")
        if self.detail:
            bits.append(self.detail)
        return " ".join(bits)


def _finite(x: Any) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(v) or math.isinf(v))


def validate_bar(
    symbol: str,
    newest_bar_start: Optional[datetime],
    now: Optional[datetime],
    *,
    max_age_sec: float,
    bar_interval_sec: float = 60.0,
    require_complete: bool = True,
    future_tolerance_sec: float = DEFAULT_FUTURE_TOLERANCE_SEC,
) -> Check:
    """
    Validate one symbol's newest bar.

    ``newest_bar_start`` is the bar's *start* timestamp, which is what Alpaca
    reports. Two consequences, both deliberate:

    - **Completeness.** A bar starting at 10:05:00 is not complete until
      10:06:00, so using it at 10:05:30 reads a partial minute as if it were
      settled. ``require_complete`` rejects that.
    - **Age.** Age is measured from the start timestamp, preserving the existing
      ``STALE_DATA_MAX_AGE_SEC`` interpretation so a configured tolerance keeps
      meaning what it meant before. Alpaca publishes a minute bar 60-90s after
      the minute closes, which is why that tolerance has to exceed one interval;
      it is not silently widened here.
    """
    if newest_bar_start is None:
        return Check(False, NO_BARS, symbol)
    if now is None:
        return Check(False, CLOCK_UNTRUSTED, symbol)

    age = (now - newest_bar_start).total_seconds()

    if age < -future_tolerance_sec:
        return Check(False, BAR_FUTURE, symbol, age, f"bar starts {-age:.0f}s in the future")
    if require_complete and age < bar_interval_sec:
        return Check(
            False,
            BAR_INCOMPLETE,
            symbol,
            age,
            f"bar still forming (interval {bar_interval_sec:.0f}s)",
        )
    if age > max_age_sec:
        return Check(False, BAR_STALE, symbol, age, f"limit {max_age_sec:.0f}s")
    return Check(True, OK, symbol, age)


def validate_quote(
    symbol: str,
    quote: Optional[Dict[str, Any]],
    now: Optional[datetime],
    *,
    max_age_sec: float,
    future_tolerance_sec: float = DEFAULT_FUTURE_TOLERANCE_SEC,
) -> Check:
    """
    Validate one symbol's quote for the purpose of *entering*.

    ``quote`` needs ``bid``, ``ask`` and a timestamp under any of ``t``,
    ``timestamp`` or ``ts``. Rejects a missing timestamp outright: an undated
    quote cannot be shown to be current, and the old code's willingness to use
    one is how a stale book could size an entry.

    A crossed book (``bid > ask``) is rejected rather than normalized. It means
    the snapshot is inconsistent, and picking one side of it invents a price
    that never existed.
    """
    if not quote:
        return Check(False, NO_QUOTE, symbol)
    if now is None:
        return Check(False, CLOCK_UNTRUSTED, symbol)

    raw_ts = quote.get("t") or quote.get("timestamp") or quote.get("ts")
    ts = raw_ts if isinstance(raw_ts, datetime) else None
    if ts is None and raw_ts:
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            ts = None
    if ts is None or ts.tzinfo is None:
        return Check(False, QUOTE_NO_TIMESTAMP, symbol)

    bid, ask = quote.get("bid"), quote.get("ask")
    if not _finite(bid) or not _finite(ask):
        return Check(False, QUOTE_NOT_FINITE, symbol, detail=f"bid={bid} ask={ask}")
    bid_f, ask_f = float(bid), float(ask)
    if bid_f <= 0 or ask_f <= 0:
        return Check(False, QUOTE_NONPOSITIVE, symbol, detail=f"bid={bid_f} ask={ask_f}")
    if bid_f > ask_f:
        return Check(False, QUOTE_CROSSED, symbol, detail=f"bid={bid_f} > ask={ask_f}")

    age = (now - ts).total_seconds()
    if age < -future_tolerance_sec:
        return Check(False, QUOTE_FUTURE, symbol, age, f"quote is {-age:.0f}s ahead")
    if age > max_age_sec:
        return Check(False, QUOTE_STALE, symbol, age, f"limit {max_age_sec:.0f}s")
    return Check(True, OK, symbol, age)


class AdvancingClock:
    """
    A broker-timestamp cache whose age keeps advancing between refreshes.

    The bug this replaces: the broker timestamp was cached for 60 seconds and
    used directly as "now", so for a minute at a time the reference point did not
    move and stale bars kept testing fresh. Here the cached broker time is an
    *anchor*, and elapsed monotonic time since the anchor is added to it. The
    anchor also expires: past ``max_confidence_sec`` with no successful refresh
    the clock stops claiming to know the time, and callers must refuse entries
    rather than act on a guess.
    """

    def __init__(
        self,
        *,
        monotonic: Any = None,
        refresh_after_sec: float = 60.0,
        max_confidence_sec: float = 300.0,
    ):
        import time as _time

        self._monotonic = monotonic or _time.monotonic
        self.refresh_after_sec = float(refresh_after_sec)
        self.max_confidence_sec = float(max_confidence_sec)
        self._anchor_broker: Optional[datetime] = None
        self._anchor_mono: Optional[float] = None

    def set_anchor(self, broker_ts: datetime) -> None:
        self._anchor_broker = broker_ts
        self._anchor_mono = float(self._monotonic())

    @property
    def elapsed_since_anchor(self) -> Optional[float]:
        if self._anchor_mono is None:
            return None
        return float(self._monotonic()) - self._anchor_mono

    def needs_refresh(self) -> bool:
        el = self.elapsed_since_anchor
        return el is None or el >= self.refresh_after_sec

    @property
    def trusted(self) -> bool:
        el = self.elapsed_since_anchor
        return el is not None and el <= self.max_confidence_sec

    def now(self) -> Optional[datetime]:
        """Best estimate of broker time, or None when confidence has expired."""
        if self._anchor_broker is None or self._anchor_mono is None:
            return None
        el = float(self._monotonic()) - self._anchor_mono
        if el > self.max_confidence_sec:
            return None
        return self._anchor_broker + timedelta(seconds=el)


def validate_symbol(
    symbol: str,
    newest_bar_start: Optional[datetime],
    quote: Optional[Dict[str, Any]],
    now: Optional[datetime],
    *,
    bar_max_age_sec: float,
    quote_max_age_sec: float,
    bar_interval_sec: float = 60.0,
    require_complete_bar: bool = True,
) -> Tuple[bool, Check]:
    """
    Validate one candidate end to end, returning the first failure.

    Bars first: a symbol with no usable history cannot be scored at all, so its
    quote is irrelevant.
    """
    bar = validate_bar(
        symbol,
        newest_bar_start,
        now,
        max_age_sec=bar_max_age_sec,
        bar_interval_sec=bar_interval_sec,
        require_complete=require_complete_bar,
    )
    if not bar.ok:
        return False, bar
    q = validate_quote(symbol, quote, now, max_age_sec=quote_max_age_sec)
    if not q.ok:
        return False, q
    return True, Check(True, OK, symbol, bar.age_sec)
