"""
Durable order tracking and exactly-once fill accounting.

Why this module exists
----------------------
The bot used to track one entry order, one stop order and one exit order in
flat top-level state fields, and it computed realized P&L from whatever the
broker happened to report the last time it polled. That loses money twice over:

- ``_sync_position_legs_with_broker`` pruned an absent holding before anything
  inspected its stop order, so a stop that filled between two polls vanished
  without ever updating daily P&L or the re-entry cooldown.
- A single top-level ``ENTRY_PENDING``/``EXIT_PENDING`` slot cannot represent
  symbol A entering while symbol B exits, and it forgets an outstanding order
  across a restart.

This module owns the part that has to be exactly right: which orders exist,
what has actually executed on them, and which ET day each execution belongs to.
It is deliberately pure — no network, no clock reads, no file I/O — so every
rule below is testable offline.

The accounting rule that matters
--------------------------------
Alpaca reports ``filled_qty`` and ``filled_avg_price`` as *cumulative*
snapshots of an order, not as a stream of individual fills. Differencing only
the quantity is wrong, because the average price moves as new fills land. The
correct delta needs both cumulative quantity and cumulative *value*:

    cumulative 2 @ $100.00  ->  cum_qty 2, cum_value 200.00
    cumulative 5 @ $101.00  ->  cum_qty 5, cum_value 505.00
    delta                   ->  3 shares for 305.00, i.e. $101.67 each

Differencing the average price instead would report 3 shares at $101 = $303 and
silently lose $2. :func:`OrderRecord.apply_snapshot` implements the correct
form, and ``test_order_ledger.py`` pins this exact example.

Day attribution
---------------
A realized result belongs to the America/New_York date of the *execution*, not
of the poll that discovered it. Discovering yesterday's stop fill this morning
must not consume today's loss allowance or start a fresh cooldown from now.

When the broker gives timestamped executions we attribute each one directly.
When only a cumulative snapshot is available and the unobserved window spans
more than one ET date, the delta's attribution is genuinely unknowable — this
module marks it ``UNRESOLVED`` rather than guessing. Unresolved value blocks
entries that depend on an accurate daily allowance; it never silently books to
today.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCHEMA_VERSION = 2

# Order roles. A managed trade has at most one entry, but may have several
# protective and exit orders over its life (stop replaced, partial exits).
ROLE_ENTRY = "entry"
ROLE_PROTECTIVE = "protective"
ROLE_EXIT = "exit"

# Terminal broker statuses. "canceled" spelling follows Alpaca.
TERMINAL_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "replaced", "done_for_day"}
)
# Live-but-leaving statuses. pending_cancel is NOT terminal: a fill can still
# land after a successful cancel request, which is exactly the race that used
# to produce oversells.
LIVE_STATUSES = frozenset(
    {"new", "accepted", "partially_filled", "pending_new", "pending_cancel", "pending_replace",
     "accepted_for_bidding", "held", "calculated"}
)

UNRESOLVED = "UNRESOLVED"

# Tolerances. Share quantities are floats only because Alpaca returns them as
# strings that may carry fractional values; compare with an epsilon rather than
# exactly.
QTY_EPS = 1e-9
VALUE_EPS = 1e-6


def et_date_of(dt: Optional[datetime]) -> Optional[str]:
    """ET calendar date of an aware datetime, as ``YYYY-MM-DD``."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("et_date_of requires an aware datetime")
    return dt.astimezone(ET).date().isoformat()


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce a broker-supplied string/number to float, tolerating None and ''."""
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).replace("Z", "+00:00")
        out = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo is not None else None


@dataclass
class Execution:
    """
    One accounted-for increment of an order's fills.

    ``qty`` and ``value`` are *incremental*, not cumulative. ``et_date`` is the
    day this increment is booked to, or :data:`UNRESOLVED` when a cumulative
    snapshot could not establish it.
    """

    order_id: str
    symbol: str
    side: str
    role: str
    seq: int
    qty: float
    value: float
    at: Optional[datetime] = None
    et_date: str = UNRESOLVED
    exec_id: Optional[str] = None

    @property
    def avg_price(self) -> float:
        return self.value / self.qty if abs(self.qty) > QTY_EPS else 0.0

    @property
    def resolved(self) -> bool:
        return self.et_date != UNRESOLVED

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat() if self.at else None
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Execution":
        return Execution(
            order_id=str(d.get("order_id") or ""),
            symbol=str(d.get("symbol") or "").upper(),
            side=str(d.get("side") or ""),
            role=str(d.get("role") or ""),
            seq=int(d.get("seq") or 0),
            qty=_f(d.get("qty")),
            value=_f(d.get("value")),
            at=_dt(d.get("at")),
            et_date=str(d.get("et_date") or UNRESOLVED),
            exec_id=(str(d["exec_id"]) if d.get("exec_id") else None),
        )


@dataclass
class OrderRecord:
    """
    Durable record of one order the bot submitted, plus what has executed on it.

    Identity is the *client* order id, which the bot chooses and persists before
    submitting. That is what makes an uncertain submission recoverable: if the
    HTTP call times out, the order may still exist at the broker, and it can be
    found again by this id. Generating a fresh id and retrying would duplicate
    the order — the failure mode this field exists to prevent.
    """

    client_order_id: str
    symbol: str
    side: str
    role: str
    intent_qty: float
    trade_id: str
    order_id: Optional[str] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = "unsubmitted"
    submitted_at: Optional[datetime] = None
    # Processed cumulative totals. The whole point: deltas come from both.
    cum_qty: float = 0.0
    cum_value: float = 0.0
    last_seq: int = 0
    # Broker execution ids already booked, so a replayed report cannot double count.
    seen_exec_ids: List[str] = field(default_factory=list)
    # Last time a snapshot for this order was successfully observed.
    last_observed_at: Optional[datetime] = None
    # True once the broker reported a terminal status we trust.
    terminal: bool = False
    # Set when submission returned an uncertain result and identity is unconfirmed.
    submission_uncertain: bool = False

    # ---- derived -----------------------------------------------------------

    @property
    def is_live(self) -> bool:
        """Still capable of producing a fill."""
        return not self.terminal

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.intent_qty - self.cum_qty)

    @property
    def avg_price(self) -> float:
        return self.cum_value / self.cum_qty if abs(self.cum_qty) > QTY_EPS else 0.0

    def key(self) -> str:
        return self.client_order_id

    # ---- accounting --------------------------------------------------------

    def apply_snapshot(
        self,
        snapshot: Dict[str, Any],
        *,
        observed_at: Optional[datetime] = None,
        executions: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> List[Execution]:
        """
        Fold a broker order snapshot into this record and return new executions.

        ``snapshot`` is an Alpaca order object (or the subset of it this module
        uses). ``executions``, when supplied, is the order's individual timestamped
        executions — preferred, because they make day attribution exact.

        Returns the executions newly accounted for. Calling this repeatedly with
        the same snapshot returns an empty list: accounting is idempotent, which
        is what makes re-polling and restart safe.
        """
        out: List[Execution] = []
        if snapshot.get("id"):
            self.order_id = str(snapshot["id"])
            self.submission_uncertain = False

        status = str(snapshot.get("status") or "").lower()
        if status:
            self.status = status

        # Capture the start of the unobserved window BEFORE advancing it. Day
        # attribution for a cumulative delta depends on how long the bot was not
        # looking; advancing the marker first would collapse that window to zero
        # and make every delta look same-day.
        window_start = self.last_observed_at or self.submitted_at
        if observed_at is not None:
            self.last_observed_at = observed_at

        if executions is not None:
            out.extend(self._apply_executions(executions))
        else:
            out.extend(self._apply_cumulative(snapshot, observed_at, window_start))

        # Terminal is decided only after fills are booked, so a fill that lands
        # in the same snapshot as a terminal status is never dropped.
        if status in TERMINAL_STATUSES:
            self.terminal = True
        return out

    def _apply_executions(self, executions: Iterable[Dict[str, Any]]) -> List[Execution]:
        """Book individual timestamped executions, skipping ones already seen."""
        out: List[Execution] = []
        for ex in executions:
            exec_id = str(ex.get("id") or ex.get("execution_id") or "") or None
            if exec_id and exec_id in self.seen_exec_ids:
                continue
            qty = _f(ex.get("qty"))
            price = _f(ex.get("price"))
            if abs(qty) <= QTY_EPS:
                continue
            at = _dt(ex.get("transaction_time") or ex.get("timestamp") or ex.get("at"))
            self.last_seq += 1
            self.cum_qty += qty
            self.cum_value += qty * price
            if exec_id:
                self.seen_exec_ids.append(exec_id)
            out.append(
                Execution(
                    order_id=self.order_id or self.client_order_id,
                    symbol=self.symbol,
                    side=self.side,
                    role=self.role,
                    seq=self.last_seq,
                    qty=qty,
                    value=qty * price,
                    at=at,
                    et_date=et_date_of(at) or UNRESOLVED,
                    exec_id=exec_id,
                )
            )
        return out

    def _apply_cumulative(
        self,
        snapshot: Dict[str, Any],
        observed_at: Optional[datetime],
        window_start: Optional[datetime] = None,
    ) -> List[Execution]:
        """
        Book the delta implied by a cumulative snapshot.

        Both cumulative quantity and cumulative value are differenced — see the
        module docstring for why differencing the average price is wrong.
        """
        new_qty = _f(snapshot.get("filled_qty"))
        avg = _f(snapshot.get("filled_avg_price"))
        new_value = new_qty * avg

        d_qty = new_qty - self.cum_qty
        d_value = new_value - self.cum_value

        if d_qty <= QTY_EPS and abs(d_value) <= VALUE_EPS:
            return []  # nothing new, or a repeated poll
        if d_qty <= QTY_EPS:
            # Value moved without quantity moving. Almost always a corrected
            # average price on an already-booked fill. Absorb the correction into
            # totals but do not invent a zero-quantity execution.
            self.cum_value = new_value
            return []
        if d_qty < -QTY_EPS:
            # Cumulative quantity went backwards. Broker data we cannot reconcile;
            # refuse to un-book anything and leave the record for reconciliation.
            return []

        filled_at = _dt(snapshot.get("filled_at"))
        et_date = self._attribute_cumulative_delta(filled_at, observed_at, window_start)

        self.last_seq += 1
        self.cum_qty = new_qty
        self.cum_value = new_value
        return [
            Execution(
                order_id=self.order_id or self.client_order_id,
                symbol=self.symbol,
                side=self.side,
                role=self.role,
                seq=self.last_seq,
                qty=d_qty,
                value=d_value,
                at=filled_at,
                et_date=et_date,
            )
        ]

    def _attribute_cumulative_delta(
        self,
        filled_at: Optional[datetime],
        observed_at: Optional[datetime],
        window_start: Optional[datetime] = None,
    ) -> str:
        """
        Decide which ET day a cumulative delta belongs to.

        A cumulative delta is a *range* of unobserved fills, not one fill. It can
        only be attributed when the whole unobserved window sits inside one ET
        date. Otherwise the honest answer is :data:`UNRESOLVED`; the plan this
        implements is explicit that the delta must not simply be stamped with the
        latest fill timestamp.
        """
        if window_start is None:
            window_start = self.submitted_at
        candidates = [d for d in (et_date_of(filled_at), et_date_of(observed_at)) if d]
        if not candidates:
            return UNRESOLVED
        latest = candidates[0]
        start_date = et_date_of(window_start)
        if start_date is None:
            # No window information at all — trust the fill timestamp only.
            return et_date_of(filled_at) or UNRESOLVED
        if start_date == latest:
            return latest
        return UNRESOLVED

    # ---- protection --------------------------------------------------------

    def protects(self, symbol: str, qty: float, *, tolerance: float = QTY_EPS) -> bool:
        """
        True when this record is *actually* protection for ``qty`` of ``symbol``.

        An order id alone never establishes protection. A canceled, rejected,
        expired or replaced stop is not protection, and a 2-share stop does not
        protect a 5-share holding.
        """
        if self.role != ROLE_PROTECTIVE or self.side != "sell":
            return False
        if self.symbol.upper() != symbol.upper():
            return False
        if self.terminal or self.status not in LIVE_STATUSES:
            return False
        return self.remaining_qty + tolerance >= qty

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["submitted_at"] = self.submitted_at.isoformat() if self.submitted_at else None
        d["last_observed_at"] = (
            self.last_observed_at.isoformat() if self.last_observed_at else None
        )
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OrderRecord":
        return OrderRecord(
            client_order_id=str(d.get("client_order_id") or ""),
            symbol=str(d.get("symbol") or "").upper(),
            side=str(d.get("side") or ""),
            role=str(d.get("role") or ""),
            intent_qty=_f(d.get("intent_qty")),
            trade_id=str(d.get("trade_id") or ""),
            order_id=(str(d["order_id"]) if d.get("order_id") else None),
            limit_price=(_f(d["limit_price"]) if d.get("limit_price") is not None else None),
            stop_price=(_f(d["stop_price"]) if d.get("stop_price") is not None else None),
            status=str(d.get("status") or "unsubmitted"),
            submitted_at=_dt(d.get("submitted_at")),
            cum_qty=_f(d.get("cum_qty")),
            cum_value=_f(d.get("cum_value")),
            last_seq=int(d.get("last_seq") or 0),
            seen_exec_ids=[str(x) for x in (d.get("seen_exec_ids") or [])],
            last_observed_at=_dt(d.get("last_observed_at")),
            terminal=bool(d.get("terminal")),
            submission_uncertain=bool(d.get("submission_uncertain")),
        )


@dataclass
class DayBook:
    """Realized P&L for one ET date, plus the part that could not be attributed."""

    by_date: Dict[str, float] = field(default_factory=dict)
    unresolved: float = 0.0
    # Completed-trade markers, so cooldown/training fire once per trade.
    completed_trade_ids: List[str] = field(default_factory=list)

    def add(self, et_date: str, amount: float) -> None:
        if et_date == UNRESOLVED:
            self.unresolved += amount
        else:
            self.by_date[et_date] = self.by_date.get(et_date, 0.0) + amount

    def realized_for(self, et_date: str) -> float:
        return self.by_date.get(et_date, 0.0)

    @property
    def has_unresolved(self) -> bool:
        return abs(self.unresolved) > VALUE_EPS

    def mark_complete(self, trade_id: str) -> bool:
        """Record a trade as complete. False if it was already recorded."""
        if trade_id in self.completed_trade_ids:
            return False
        self.completed_trade_ids.append(trade_id)
        return True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "DayBook":
        return DayBook(
            by_date={str(k): _f(v) for k, v in (d.get("by_date") or {}).items()},
            unresolved=_f(d.get("unresolved")),
            completed_trade_ids=[str(x) for x in (d.get("completed_trade_ids") or [])],
        )


@dataclass
class Ledger:
    """
    The whole order/execution book. Serialized as one object inside bot state so
    it cannot disagree with the rest of state after a crash.
    """

    schema_version: int = SCHEMA_VERSION
    orders: Dict[str, OrderRecord] = field(default_factory=dict)
    executions: List[Execution] = field(default_factory=list)
    day_book: DayBook = field(default_factory=DayBook)
    # Managed quantity and basis per symbol, from the bot's own executions only.
    # Never from the broker's net position: that can combine bot and manual shares.
    managed: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # ---- lookup ------------------------------------------------------------

    def by_client_id(self, client_order_id: str) -> Optional[OrderRecord]:
        return self.orders.get(client_order_id)

    def by_order_id(self, order_id: str) -> Optional[OrderRecord]:
        for rec in self.orders.values():
            if rec.order_id and rec.order_id == order_id:
                return rec
        return None

    def live_orders(
        self, *, symbol: Optional[str] = None, role: Optional[str] = None
    ) -> List[OrderRecord]:
        out = []
        for rec in self.orders.values():
            if not rec.is_live:
                continue
            if symbol and rec.symbol.upper() != symbol.upper():
                continue
            if role and rec.role != role:
                continue
            out.append(rec)
        return out

    def protection_for(self, symbol: str, qty: float) -> Optional[OrderRecord]:
        """An order record that actually protects ``qty`` of ``symbol``, if any."""
        for rec in self.orders.values():
            if rec.protects(symbol, qty):
                return rec
        return None

    # ---- mutation ----------------------------------------------------------

    def register(self, rec: OrderRecord) -> OrderRecord:
        """Persist an order's intent. Call this *before* submitting it."""
        self.orders[rec.client_order_id] = rec
        return rec

    def record_executions(self, execs: Iterable[Execution]) -> List[Execution]:
        """Append executions and fold them into managed quantity and basis."""
        added = []
        for ex in execs:
            self.executions.append(ex)
            self._apply_to_managed(ex)
            added.append(ex)
        return added

    def _apply_to_managed(self, ex: Execution) -> None:
        sym = ex.symbol.upper()
        book = self.managed.setdefault(sym, {"qty": 0.0, "basis": 0.0})
        if ex.side == "buy":
            book["qty"] += ex.qty
            book["basis"] += ex.value
            return

        # Sell: realize against average basis and reduce the position.
        held = book["qty"]
        if held <= QTY_EPS:
            # Selling what the ledger does not know it owns. Do not invent a
            # basis; book the proceeds as unresolved for reconciliation.
            self.day_book.add(UNRESOLVED, ex.value)
            return
        sold = min(ex.qty, held)
        avg_basis = book["basis"] / held
        realized = ex.value - sold * avg_basis
        if ex.qty - sold > QTY_EPS:
            # Sold more than the ledger's managed quantity: the excess has no
            # known basis, so its result cannot be attributed.
            self.day_book.add(UNRESOLVED, (ex.qty - sold) * ex.avg_price)
        book["qty"] = held - sold
        book["basis"] = book["basis"] - sold * avg_basis
        if book["qty"] <= QTY_EPS:
            book["qty"] = 0.0
            book["basis"] = 0.0
        self.day_book.add(ex.et_date, realized)

    def managed_qty(self, symbol: str) -> float:
        return self.managed.get(symbol.upper(), {}).get("qty", 0.0)

    def managed_basis(self, symbol: str) -> float:
        """Average cost of the managed shares, 0.0 when none are held."""
        book = self.managed.get(symbol.upper())
        if not book or book.get("qty", 0.0) <= QTY_EPS:
            return 0.0
        return book["basis"] / book["qty"]

    def prune_terminal(self, keep_executions: int = 2000) -> None:
        """Drop terminal orders with nothing outstanding, and cap the exec log."""
        for key in [
            k
            for k, r in self.orders.items()
            if r.terminal and r.remaining_qty <= QTY_EPS and not r.submission_uncertain
        ]:
            del self.orders[key]
        if len(self.executions) > keep_executions:
            self.executions = self.executions[-keep_executions:]

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "orders": {k: v.to_dict() for k, v in self.orders.items()},
            "executions": [e.to_dict() for e in self.executions],
            "day_book": self.day_book.to_dict(),
            "managed": {k: dict(v) for k, v in self.managed.items()},
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Ledger":
        if not d:
            return Ledger()
        return Ledger(
            schema_version=int(d.get("schema_version") or SCHEMA_VERSION),
            orders={
                str(k): OrderRecord.from_dict(v) for k, v in (d.get("orders") or {}).items()
            },
            executions=[Execution.from_dict(e) for e in (d.get("executions") or [])],
            day_book=DayBook.from_dict(d.get("day_book") or {}),
            managed={
                str(k).upper(): {"qty": _f(v.get("qty")), "basis": _f(v.get("basis"))}
                for k, v in (d.get("managed") or {}).items()
            },
        )


def make_client_order_id(prefix: str, symbol: str, role: str, nonce: str) -> str:
    """
    Build a stable, unique client order id.

    Stable because recovery after an uncertain submission has to find the *same*
    id; unique because reusing one across trades would make two orders
    indistinguishable. ``nonce`` should be monotonic (a timestamp or counter).
    """
    safe = "".join(ch for ch in f"{prefix}-{symbol}-{role}-{nonce}" if ch.isalnum() or ch in "-_")
    return safe[:128]


def migrate_legacy_state(state_dict: Dict[str, Any]) -> Tuple[Ledger, List[str]]:
    """
    Build a ledger from pre-ledger state, and report what could not be recovered.

    Legacy state has no execution history — only a current position and whatever
    P&L had already been booked. So this recovers *position* (quantity, basis,
    entry time) and leaves financial history alone. It deliberately does not
    invent executions for historical fills: replaying them would double-count
    against ``daily_realized_pnl``, which legacy state already carries.

    Returns the ledger and a list of human-readable notes about anything
    ambiguous, which the caller should surface rather than swallow.
    """
    notes: List[str] = []
    led = Ledger()

    legs = state_dict.get("position_legs") or {}
    for sym, leg in legs.items():
        if not isinstance(leg, dict):
            continue
        symbol = str(sym).upper()
        qty = _f(leg.get("qty"))
        avg = _f(leg.get("entry_avg_price"))
        if qty <= QTY_EPS:
            # Legacy legs often omitted quantity: it lived only at the broker.
            # Record the basis so reconciliation can pair it with the real
            # holding instead of guessing a size here.
            if avg > 0:
                notes.append(
                    f"{symbol}: legacy leg has entry_avg_price {avg} but no quantity; "
                    "quantity must come from broker reconciliation"
                )
                led.managed[symbol] = {"qty": 0.0, "basis": 0.0}
            continue
        if avg <= 0:
            notes.append(f"{symbol}: legacy leg has quantity {qty} but no usable basis")
        led.managed[symbol] = {"qty": qty, "basis": qty * avg}

    # A legacy top-level single leg, from before position_legs existed.
    top_sym = state_dict.get("entry_symbol")
    top_qty = _f(state_dict.get("entry_filled_qty"))
    top_avg = _f(state_dict.get("entry_avg_price"))
    if top_sym and top_qty > QTY_EPS and str(top_sym).upper() not in led.managed:
        led.managed[str(top_sym).upper()] = {"qty": top_qty, "basis": top_qty * top_avg}

    # Preserve already-booked P&L under its own day so the allowance is not reset.
    booked = _f(state_dict.get("daily_realized_pnl"))
    day = state_dict.get("day_utc")
    if abs(booked) > VALUE_EPS:
        if day:
            led.day_book.add(str(day), booked)
            notes.append(
                f"carried {booked:+.2f} of already-booked realized P&L into day {day} "
                "without replaying its executions"
            )
        else:
            led.day_book.add(UNRESOLVED, booked)
            notes.append(
                f"carried {booked:+.2f} of already-booked realized P&L with no day "
                "attribution; flagged unresolved"
            )

    # Outstanding legacy orders are recorded as identity-only, uncertain records.
    # They have no client order id, so they can only be recovered by broker id.
    for field_name, role, side in (
        ("entry_order_id", ROLE_ENTRY, "buy"),
        ("exit_order_id", ROLE_EXIT, "sell"),
    ):
        oid = state_dict.get(field_name)
        if not oid:
            continue
        sym = str(state_dict.get("entry_symbol") or "").upper()
        cid = str(state_dict.get("entry_client_order_id") or f"legacy-{oid}")
        led.register(
            OrderRecord(
                client_order_id=cid,
                symbol=sym,
                side=side,
                role=role,
                intent_qty=_f(state_dict.get("entry_filled_qty")),
                trade_id=f"legacy-{sym}",
                order_id=str(oid),
                status="unknown",
                submission_uncertain=True,
            )
        )
        notes.append(
            f"legacy {role} order {oid} recovered by broker id only; status unknown "
            "until reconciled"
        )

    for sym, leg in legs.items():
        if isinstance(leg, dict) and leg.get("stop_order_id"):
            symbol = str(sym).upper()
            led.register(
                OrderRecord(
                    client_order_id=f"legacy-stop-{symbol}",
                    symbol=symbol,
                    side="sell",
                    role=ROLE_PROTECTIVE,
                    intent_qty=led.managed_qty(symbol),
                    trade_id=f"legacy-{symbol}",
                    order_id=str(leg["stop_order_id"]),
                    status="unknown",
                    submission_uncertain=True,
                )
            )
            notes.append(
                f"{symbol}: legacy stop {leg['stop_order_id']} recovered by broker id only; "
                "protection unverified until reconciled"
            )

    return led, notes
