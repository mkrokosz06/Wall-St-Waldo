"""
One session policy for entries and closing (Phase 4).

The bot previously derived its end-of-day cutoff from a hard-coded 15:55 ET and
decided whether the market was open from a locally parsed clock. That breaks on
early closes (the day after Thanksgiving, Christmas Eve and similar close at
13:00 ET, so a 15:55 cutoff fires two hours after the close), on holidays, and
around daylight-saving transitions.

This module turns broker calendar/clock data into one set of answers used by
every caller: may a new entry be submitted, is it time to close, and is the
session information trustworthy at all. When the answer is unknown it says so —
missing session data blocks *new entries* while still permitting reconciliation
and closing, because refusing to manage risk is worse than refusing to trade.

Pure and injectable: it takes calendar rows and a current time, never fetches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Why entries may be refused. Distinct values because they need distinct
# responses: a closed market is normal, unknown session data is a fault.
OK = "ok"
BEFORE_OPEN = "before_open"
IN_OPENING_DELAY = "opening_delay"
AFTER_CUTOFF = "after_cutoff"
AFTER_CLOSE = "after_close"
MARKET_CLOSED = "market_closed"
SESSION_UNKNOWN = "session_unknown"


@dataclass
class Session:
    """One trading session, in ET."""

    session_date: date
    open_at: datetime
    close_at: datetime

    @property
    def is_early_close(self) -> bool:
        return self.close_at.time() < time(16, 0)

    def cutoff_at(self, flat_minutes: int) -> datetime:
        """
        When new entries stop and closing begins.

        Derived from the *actual* session close, which is what makes an early
        close work. A fixed 15:55 would sit long after a 13:00 close.
        """
        return self.close_at - timedelta(minutes=max(0, int(flat_minutes)))

    def entry_open_at(self, delay_minutes: int) -> datetime:
        return self.open_at + timedelta(minutes=max(0, int(delay_minutes)))


def parse_calendar(rows: Any) -> List[Session]:
    """
    Build sessions from Alpaca calendar rows (``date``, ``open``, ``close``).

    Times in a calendar row are ET wall-clock strings like ``"09:30"``. They are
    localized to ET rather than treated as UTC; getting that wrong shifts every
    session by four or five hours and silently changes with daylight saving.
    """
    out: List[Session] = []
    if not rows:
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = row.get("date")
        raw_open = row.get("open")
        raw_close = row.get("close")
        if not (raw_date and raw_open and raw_close):
            continue
        try:
            d = date.fromisoformat(str(raw_date)[:10])
            oh, om = (int(x) for x in str(raw_open).split(":")[:2])
            ch, cm = (int(x) for x in str(raw_close).split(":")[:2])
        except (TypeError, ValueError):
            continue
        out.append(
            Session(
                session_date=d,
                open_at=datetime(d.year, d.month, d.day, oh, om, tzinfo=ET),
                close_at=datetime(d.year, d.month, d.day, ch, cm, tzinfo=ET),
            )
        )
    out.sort(key=lambda s: s.session_date)
    return out


class SessionPolicy:
    """
    Session questions answered from cached calendar data.

    Cache the calendar once per day, not once per symbol — the old code's
    per-call clock fetches were a meaningful share of its request volume and one
    of the recurring failure modes in the log (``clock_fetch_failed``, 579
    occurrences).
    """

    def __init__(
        self,
        sessions: Optional[List[Session]] = None,
        *,
        end_of_day_flat_minutes: int = 5,
        market_open_delay_minutes: int = 0,
        trusted: bool = True,
    ):
        self.sessions = sessions or []
        self.end_of_day_flat_minutes = int(end_of_day_flat_minutes)
        self.market_open_delay_minutes = int(market_open_delay_minutes)
        # False when the calendar could not be fetched or parsed. Entries are
        # blocked in that case; risk handling continues.
        self.trusted = bool(trusted) and bool(self.sessions)

    # ---- construction ------------------------------------------------------

    @classmethod
    def from_calendar(cls, rows: Any, **kw) -> "SessionPolicy":
        sessions = parse_calendar(rows)
        return cls(sessions, trusted=bool(sessions), **kw)

    @classmethod
    def unknown(cls, **kw) -> "SessionPolicy":
        """A policy that knows nothing and therefore permits no new entries."""
        return cls([], trusted=False, **kw)

    # ---- lookup ------------------------------------------------------------

    def session_for(self, now_et: datetime) -> Optional[Session]:
        d = now_et.date()
        for s in self.sessions:
            if s.session_date == d:
                return s
        return None

    def is_trading_day(self, now_et: datetime) -> bool:
        return self.session_for(now_et) is not None

    def is_open(self, now_et: datetime) -> bool:
        s = self.session_for(now_et)
        return bool(s and s.open_at <= now_et < s.close_at)

    def cutoff_at(self, now_et: datetime) -> Optional[datetime]:
        s = self.session_for(now_et)
        return s.cutoff_at(self.end_of_day_flat_minutes) if s else None

    # ---- the two decisions -------------------------------------------------

    def entry_allowed(self, now_et: datetime) -> Tuple[bool, str]:
        """
        May a new buy be submitted right now?

        Also call this immediately before submission, not only when selecting a
        candidate: scanning and sizing take time, and the cutoff can pass in
        between. That recheck is the difference between "no entries after the
        cutoff" and "no entries *selected* after the cutoff".
        """
        if not self.trusted:
            return False, SESSION_UNKNOWN
        s = self.session_for(now_et)
        if s is None:
            return False, MARKET_CLOSED
        if now_et < s.open_at:
            return False, BEFORE_OPEN
        if now_et < s.entry_open_at(self.market_open_delay_minutes):
            return False, IN_OPENING_DELAY
        if now_et >= s.close_at:
            return False, AFTER_CLOSE
        if now_et >= s.cutoff_at(self.end_of_day_flat_minutes):
            return False, AFTER_CUTOFF
        return True, OK

    def should_close_positions(self, now_et: datetime) -> bool:
        """
        Is it at or past the closing cutoff for today's session?

        False when session data is untrusted: without a calendar there is no
        basis for asserting the session is ending, and liquidating on a guess is
        its own kind of error. Untrusted data blocks entries instead.
        """
        if not self.trusted:
            return False
        s = self.session_for(now_et)
        if s is None:
            return False
        return now_et >= s.cutoff_at(self.end_of_day_flat_minutes)

    # ---- reporting ---------------------------------------------------------

    def describe(self, now_et: datetime) -> str:
        if not self.trusted:
            return "session=UNKNOWN (entries blocked; risk handling continues)"
        s = self.session_for(now_et)
        if s is None:
            return f"session=none ({now_et.date()} is not a trading day)"
        cutoff = s.cutoff_at(self.end_of_day_flat_minutes)
        return (
            f"session={s.session_date} open={s.open_at:%H:%M} close={s.close_at:%H:%M}"
            f"{' EARLY' if s.is_early_close else ''} cutoff={cutoff:%H:%M}"
        )
