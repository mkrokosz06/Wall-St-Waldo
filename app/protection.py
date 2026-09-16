"""
Confirmed cancellation and verified protection (Phase 2).

Two things here, both of which used to be assumed rather than checked.

**Cancellation was a sleep.** The old code cancelled an order, slept a second,
and carried on as though the order were gone. It is not gone. Alpaca can leave
an order in ``pending_cancel``, and a fill can land *after* a successful cancel
request. Computing a replacement quantity from the pre-cancel position then
oversells. :func:`confirm_cancel` polls to a genuinely terminal state with
bounded retries and reports which outcome occurred, including a fill that raced
the cancel.

**Protection was an order id.** The old code treated a stored ``stop_order_id``
as proof the position was protected. An id proves nothing: the order may have
been rejected, expired, cancelled or replaced, or may cover two shares of a
five-share holding. :func:`verify_protection` checks the live order and reports
what is actually covered, so a gap can be repaired instead of assumed away.

Everything here takes an injected broker-ish object and an injected sleep, so
it is fully testable offline with no real waiting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from order_ledger import (
    LIVE_STATUSES,
    QTY_EPS,
    TERMINAL_STATUSES,
    OrderRecord,
    _f,
)

# Outcomes of a cancel attempt.
CANCEL_CONFIRMED = "canceled"  # order is terminal and did not fill further
CANCEL_FILLED = "filled"  # it filled instead (fully or partially) — race
CANCEL_REJECTED = "rejected"
CANCEL_EXPIRED = "expired"
CANCEL_REPLACED = "replaced"
CANCEL_UNRESOLVED = "unresolved"  # still not terminal within the budget
CANCEL_GONE = "gone"  # broker no longer knows the order at all


@dataclass
class CancelOutcome:
    """
    What actually happened to a cancel request.

    ``filled_qty`` is the cumulative quantity the broker reports as filled, which
    is the number a replacement order must be computed from — never the
    pre-cancel position size.
    """

    outcome: str
    order_id: Optional[str]
    status: str
    filled_qty: float
    filled_avg_price: float
    polls: int
    snapshot: Optional[Dict[str, Any]] = None

    @property
    def terminal(self) -> bool:
        return self.outcome not in (CANCEL_UNRESOLVED,)

    @property
    def raced_a_fill(self) -> bool:
        return self.filled_qty > QTY_EPS


def confirm_cancel(
    broker: Any,
    order_id: str,
    *,
    max_polls: int = 6,
    initial_delay: float = 0.2,
    max_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> CancelOutcome:
    """
    Request a cancel and poll until the order is genuinely terminal.

    Bounded on purpose: one unresolved order must never block the whole
    portfolio indefinitely. If the budget runs out the outcome is
    :data:`CANCEL_UNRESOLVED`, which callers must treat as "this order may still
    fill" rather than as a cancellation.

    Backs off geometrically from ``initial_delay`` to ``max_delay``.
    """
    try:
        broker.cancel_order(order_id)
    except Exception:
        # The cancel request itself failing does not tell us the order's state.
        # Fall through and let the polling below establish it.
        pass

    delay = initial_delay
    status = "unknown"
    snap: Optional[Dict[str, Any]] = None
    filled_qty = 0.0
    avg = 0.0

    for attempt in range(1, max_polls + 1):
        try:
            snap = broker.get_order(order_id)
        except Exception:
            # Distinguish "broker says no such order" from a transport failure?
            # We cannot, from an exception alone. Keep polling within budget; a
            # persistent failure ends as UNRESOLVED, never as a confirmed cancel.
            snap = None
        if snap:
            status = str(snap.get("status") or "").lower()
            filled_qty = _f(snap.get("filled_qty"))
            avg = _f(snap.get("filled_avg_price"))
            if status in TERMINAL_STATUSES:
                if status == "filled" or filled_qty > QTY_EPS:
                    outcome = CANCEL_FILLED
                elif status == "rejected":
                    outcome = CANCEL_REJECTED
                elif status == "expired":
                    outcome = CANCEL_EXPIRED
                elif status == "replaced":
                    outcome = CANCEL_REPLACED
                else:
                    outcome = CANCEL_CONFIRMED
                return CancelOutcome(
                    outcome=outcome,
                    order_id=order_id,
                    status=status,
                    filled_qty=filled_qty,
                    filled_avg_price=avg,
                    polls=attempt,
                    snapshot=snap,
                )
        if attempt < max_polls:
            sleep(delay)
            delay = min(max_delay, delay * 2.0)

    return CancelOutcome(
        outcome=CANCEL_UNRESOLVED,
        order_id=order_id,
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=avg,
        polls=max_polls,
        snapshot=snap,
    )


@dataclass
class ProtectionStatus:
    """
    Whether a holding is actually protected, and by how much.

    ``covered_qty`` counts only live sell orders of a protective type with
    remaining quantity. ``gap_qty`` is the unprotected remainder — the number
    that should drive a repair.
    """

    symbol: str
    held_qty: float
    covered_qty: float
    orders: List[str]
    stale_ids: List[str]

    @property
    def gap_qty(self) -> float:
        return max(0.0, self.held_qty - self.covered_qty)

    @property
    def fully_protected(self) -> bool:
        return self.held_qty <= QTY_EPS or self.gap_qty <= QTY_EPS

    @property
    def partially_protected(self) -> bool:
        return self.covered_qty > QTY_EPS and not self.fully_protected

    def describe(self) -> str:
        if self.held_qty <= QTY_EPS:
            return f"{self.symbol}: flat"
        if self.fully_protected:
            return f"{self.symbol}: {self.covered_qty:g}/{self.held_qty:g} protected"
        return (
            f"{self.symbol}: UNPROTECTED {self.gap_qty:g} of {self.held_qty:g} "
            f"(covered {self.covered_qty:g}"
            + (f", stale ids {self.stale_ids}" if self.stale_ids else "")
            + ")"
        )


def _is_protective_order(o: Dict[str, Any]) -> bool:
    """
    A protective order is a conditional sell: stop, stop_limit or trailing_stop.

    A plain sell *limit* below the market is explicitly not protection — it is
    marketable, so it executes immediately rather than waiting for the trigger.
    Counting one as protection would report a position as covered while it is
    actually being sold at once.
    """
    if str(o.get("side") or "").lower() != "sell":
        return False
    otype = str(o.get("type") or o.get("order_type") or "").lower()
    return otype in {"stop", "stop_limit", "trailing_stop"}


def verify_protection(
    symbol: str,
    held_qty: float,
    open_orders: List[Dict[str, Any]],
    *,
    known_ids: Optional[List[str]] = None,
) -> ProtectionStatus:
    """
    Establish real protection for ``held_qty`` of ``symbol`` from live orders.

    ``open_orders`` should be the broker's live orders. ``known_ids`` are ids the
    bot believes are protective; any that do not appear as live protective orders
    come back in ``stale_ids`` so a stale id cannot suppress recovery.
    """
    sym = symbol.upper()
    covered = 0.0
    matched: List[str] = []

    for o in open_orders or []:
        if str(o.get("symbol") or "").upper() != sym:
            continue
        if not _is_protective_order(o):
            continue
        status = str(o.get("status") or "").lower()
        if status and status not in LIVE_STATUSES:
            continue
        qty = _f(o.get("qty"))
        filled = _f(o.get("filled_qty"))
        remaining = max(0.0, qty - filled)
        if remaining <= QTY_EPS:
            continue
        covered += remaining
        oid = o.get("id")
        if oid:
            matched.append(str(oid))

    stale = [str(i) for i in (known_ids or []) if str(i) not in matched]
    return ProtectionStatus(
        symbol=sym,
        held_qty=held_qty,
        covered_qty=covered,
        orders=matched,
        stale_ids=stale,
    )


def reusable_stop(
    symbol: str, held_qty: float, open_orders: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """
    A live protective order already covering the whole holding, if one exists.

    Used at startup so a valid broker stop found on the account is reused rather
    than duplicated. Returns the single covering order; when protection is split
    across several orders there is nothing to reuse as one stop, so this returns
    ``None`` and the caller should repair instead.
    """
    sym = symbol.upper()
    for o in open_orders or []:
        if str(o.get("symbol") or "").upper() != sym or not _is_protective_order(o):
            continue
        status = str(o.get("status") or "").lower()
        if status and status not in LIVE_STATUSES:
            continue
        remaining = max(0.0, _f(o.get("qty")) - _f(o.get("filled_qty")))
        if remaining + QTY_EPS >= held_qty and held_qty > QTY_EPS:
            return o
    return None


def replacement_qty_after_cancel(
    intended_qty: float, outcome: CancelOutcome, *, held_qty: Optional[float] = None
) -> float:
    """
    How many shares a replacement sell may cover after a cancel attempt.

    The fill that raced the cancel is subtracted, which is what prevents the
    oversell. When ``held_qty`` is supplied it is an additional ceiling: never
    submit a sell for more than is actually held, even if the arithmetic
    suggests it.

    Returns 0.0 when the outcome is unresolved — an order that may still fill
    must not have a replacement stacked on top of it.
    """
    if outcome.outcome == CANCEL_UNRESOLVED:
        return 0.0
    remaining = max(0.0, intended_qty - outcome.filled_qty)
    if held_qty is not None:
        remaining = min(remaining, max(0.0, held_qty))
    return remaining if remaining > QTY_EPS else 0.0
