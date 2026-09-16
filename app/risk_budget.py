"""
Reserve portfolio risk before entering (Phase 7).

The old daily control compared realized P&L against a floor and stopped new
entries once the floor was breached. That is a rear-view mirror: it counts
losses that have already happened and ignores losses already committed to. Two
positions open at full per-trade risk, with a third being submitted, could each
be checked against the same remaining allowance and all three pass.

This module makes committed risk explicit. Open positions reserve the distance
from basis to their stop; pending entries reserve what they will risk once
filled; and exposure whose protection is unknown reserves as *unprotected*
rather than as zero. A new order must fit inside what is left.

Deliberate choices worth stating:

- Unrealized gains in one position never offset another position's reservation.
  A winner is not collateral for a loser; it can give the gain back.
- Unknown exposure is not free. If the bot cannot establish a position's
  protection, that position reserves its full notional-to-zero risk, which will
  normally block new entries until it is resolved. Treating unknowns as zero is
  how the 808 unprotected positions in the log went unnoticed.
- This is a *budget*, not a guarantee. A stop can gap through, and slippage can
  exceed the modelled distance, so realized loss can exceed a reservation.

Pure: no broker, no clock, no config object.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

QTY_EPS = 1e-9


@dataclass
class Exposure:
    """
    One position or pending order, and the loss it has already committed.

    ``stop_price`` of ``None`` means protection is unknown or absent — which is
    treated as risking the whole position, not as risking nothing.
    """

    symbol: str
    qty: float
    basis: float
    stop_price: Optional[float] = None
    pending: bool = False
    protected: bool = True

    @property
    def notional(self) -> float:
        return self.qty * self.basis

    def reserved_loss(self, *, cost_allowance_pct: float = 0.0) -> float:
        """
        Worst modelled loss for this exposure, never negative.

        A stop *above* basis (a trailing stop in profit) reserves 0.0 rather
        than a negative number: a profitable stop does not create budget to
        spend elsewhere.
        """
        if self.qty <= QTY_EPS or self.basis <= 0:
            return 0.0
        if not self.protected or self.stop_price is None or self.stop_price <= 0:
            # No known protection: the modelled floor is zero, so the whole
            # notional is at risk.
            return self.notional * (1.0 + cost_allowance_pct)
        distance = max(0.0, self.basis - self.stop_price)
        return distance * self.qty + self.notional * cost_allowance_pct


@dataclass
class RiskDecision:
    """Whether an order may be placed, and which limit bound it."""

    allowed: bool
    max_reservable: float
    reason: str = "ok"
    binding: str = ""

    def describe(self) -> str:
        if self.allowed:
            return f"ok (headroom ${self.max_reservable:.2f}, binding={self.binding or 'none'})"
        return f"blocked: {self.reason}"


@dataclass
class RiskBudget:
    """
    The three independent limits an entry must satisfy.

    ``daily_loss_floor`` is negative (e.g. -20.0). ``realized_today`` is the
    correctly attributed current-day realized P&L — which is why day attribution
    in :mod:`order_ledger` matters here: booking yesterday's loss to today would
    shrink today's allowance for no reason.
    """

    daily_loss_floor: float
    realized_today: float = 0.0
    max_risk_per_trade: float = 5.0
    max_portfolio_open_risk: Optional[float] = None
    max_open_positions: int = 2
    cost_allowance_pct: float = 0.0
    # Value whose day or basis could not be established. Any of it blocks new
    # entries, because the daily allowance cannot be computed reliably.
    unresolved_value: float = 0.0

    exposures: List[Exposure] = field(default_factory=list)

    # ---- derived ----------------------------------------------------------

    @property
    def remaining_daily_allowance(self) -> float:
        """``max(0, P - L)`` — how much more may be lost today."""
        return max(0.0, self.realized_today - self.daily_loss_floor)

    @property
    def reserved(self) -> float:
        return sum(
            e.reserved_loss(cost_allowance_pct=self.cost_allowance_pct) for e in self.exposures
        )

    @property
    def portfolio_risk_cap(self) -> float:
        """
        Cap on total open risk.

        Derived from ``max_risk_per_trade * max_open_positions`` when not set
        explicitly, so the two settings cannot silently disagree.
        """
        if self.max_portfolio_open_risk is not None:
            return max(0.0, float(self.max_portfolio_open_risk))
        return max(0.0, self.max_risk_per_trade * max(1, self.max_open_positions))

    @property
    def open_slots_used(self) -> int:
        """
        Positions *and* pending entries count against the slot limit.

        A pending buy with no fill yet still occupies a slot. Counting only
        broker holdings is how two entries could be in flight for a
        one-position limit.
        """
        return len({e.symbol.upper() for e in self.exposures if e.qty > QTY_EPS})

    @property
    def has_unprotected(self) -> bool:
        return any(
            (not e.protected or e.stop_price is None) and e.qty > QTY_EPS
            for e in self.exposures
        )

    # ---- the decision -----------------------------------------------------

    def headroom(self) -> float:
        """Additional loss that may still be reserved, across all limits."""
        by_daily = self.remaining_daily_allowance - self.reserved
        by_portfolio = self.portfolio_risk_cap - self.reserved
        return max(0.0, min(self.max_risk_per_trade, by_daily, by_portfolio))

    def can_reserve(self, amount: float, *, symbol: Optional[str] = None) -> RiskDecision:
        """
        May ``amount`` of additional modelled loss be committed?

        Order of checks is chosen so the reported reason is the most actionable
        one: a breached daily floor or an unresolved accounting state is a
        different problem from simply having no headroom left.
        """
        if self.remaining_daily_allowance <= 0:
            return RiskDecision(False, 0.0, "daily realized loss floor reached", "daily_floor")
        if abs(self.unresolved_value) > 1e-6:
            return RiskDecision(
                False,
                0.0,
                f"unresolved accounting of ${self.unresolved_value:.2f}; "
                "daily allowance cannot be established",
                "unresolved",
            )
        if self.has_unprotected:
            return RiskDecision(
                False,
                0.0,
                "an open position has unknown or absent protection",
                "unprotected",
            )

        already = {e.symbol.upper() for e in self.exposures if e.qty > QTY_EPS}
        if symbol and symbol.upper() not in already:
            if self.open_slots_used >= self.max_open_positions:
                return RiskDecision(
                    False,
                    0.0,
                    f"max_open_positions reached ({self.open_slots_used}/"
                    f"{self.max_open_positions}, pending included)",
                    "slots",
                )

        room = self.headroom()
        if amount > room + 1e-9:
            binding = "per_trade"
            if self.remaining_daily_allowance - self.reserved <= room + 1e-9:
                binding = "daily_floor"
            if self.portfolio_risk_cap - self.reserved <= room + 1e-9:
                binding = "portfolio_risk"
            return RiskDecision(
                False,
                room,
                f"needs ${amount:.2f} but only ${room:.2f} may be reserved",
                binding,
            )

        binding = "per_trade"
        if abs(room - (self.remaining_daily_allowance - self.reserved)) < 1e-9:
            binding = "daily_floor"
        elif abs(room - (self.portfolio_risk_cap - self.reserved)) < 1e-9:
            binding = "portfolio_risk"
        return RiskDecision(True, room, "ok", binding)

    # ---- sizing -----------------------------------------------------------

    def max_qty_for(
        self,
        entry_price: float,
        stop_price: float,
        *,
        cash: float,
        notional_room: float,
        whole_shares: bool = True,
    ) -> float:
        """
        Largest order that satisfies every limit at once.

        Applies risk headroom, available cash, the portfolio notional cap, and
        whole-share rounding. Rounding is last: a quantity that only fits before
        flooring does not fit.
        """
        if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
            return 0.0
        room = self.headroom()
        if room <= 0:
            return 0.0
        per_share_risk = entry_price - stop_price
        qty_by_risk = room / per_share_risk
        qty_by_cash = max(0.0, cash) / entry_price
        qty_by_notional = max(0.0, notional_room) / entry_price
        qty = min(qty_by_risk, qty_by_cash * 0.98, qty_by_notional * 0.98)
        if whole_shares:
            qty = float(math.floor(qty))
        if qty <= 0:
            return 0.0
        return qty

    # ---- reservation transfer ---------------------------------------------

    def fill_pending(self, symbol: str, filled_qty: float, avg_price: float) -> None:
        """
        Move a pending entry's reservation onto the filled position.

        Transfer, not addition. Adding would double-count the same risk while
        the order is partly filled and partly outstanding, which would block
        legitimate entries.
        """
        sym = symbol.upper()
        for e in self.exposures:
            if e.symbol.upper() == sym and e.pending:
                remaining = max(0.0, e.qty - filled_qty)
                if remaining <= QTY_EPS:
                    e.pending = False
                    e.qty = filled_qty
                    e.basis = avg_price or e.basis
                else:
                    e.qty = remaining
                    self.exposures.append(
                        Exposure(
                            symbol=sym,
                            qty=filled_qty,
                            basis=avg_price or e.basis,
                            stop_price=e.stop_price,
                            pending=False,
                            protected=e.protected,
                        )
                    )
                return

    def release_pending(self, symbol: str) -> None:
        """
        Drop a pending reservation after *confirmed* cancellation or rejection.

        Never call this on an unresolved cancel: an order that may still fill
        still commits its risk.
        """
        sym = symbol.upper()
        self.exposures = [
            e for e in self.exposures if not (e.symbol.upper() == sym and e.pending)
        ]

    def describe(self) -> str:
        return (
            f"risk: realized_today={self.realized_today:+.2f} "
            f"floor={self.daily_loss_floor:.2f} allowance={self.remaining_daily_allowance:.2f} "
            f"reserved={self.reserved:.2f} headroom={self.headroom():.2f} "
            f"slots={self.open_slots_used}/{self.max_open_positions} "
            f"portfolio_cap={self.portfolio_risk_cap:.2f}"
            + (f" UNRESOLVED={self.unresolved_value:.2f}" if self.unresolved_value else "")
            + (" UNPROTECTED" if self.has_unprotected else "")
        )


def from_ledger(
    ledger,
    *,
    et_date: str,
    daily_loss_floor: float,
    max_risk_per_trade: float,
    max_open_positions: int,
    max_portfolio_open_risk: Optional[float] = None,
    stop_prices: Optional[Dict[str, float]] = None,
    protected: Optional[Dict[str, bool]] = None,
    cost_allowance_pct: float = 0.0,
) -> RiskBudget:
    """
    Build a budget from an :class:`order_ledger.Ledger`.

    Managed quantity and basis come from the ledger's own executions, never from
    the broker's net position, which can mix bot-owned and manually-held shares.
    """
    stop_prices = stop_prices or {}
    protected = protected or {}
    exposures: List[Exposure] = []

    for sym, book in (ledger.managed or {}).items():
        qty = float(book.get("qty") or 0.0)
        if qty <= QTY_EPS:
            continue
        basis = ledger.managed_basis(sym)
        exposures.append(
            Exposure(
                symbol=sym,
                qty=qty,
                basis=basis,
                stop_price=stop_prices.get(sym.upper()),
                pending=False,
                protected=bool(protected.get(sym.upper(), sym.upper() in stop_prices)),
            )
        )

    for rec in ledger.live_orders(role="entry"):
        remaining = rec.remaining_qty
        if remaining <= QTY_EPS:
            continue
        price = rec.limit_price or rec.avg_price or 0.0
        if price <= 0:
            continue
        exposures.append(
            Exposure(
                symbol=rec.symbol,
                qty=remaining,
                basis=price,
                stop_price=stop_prices.get(rec.symbol.upper()),
                pending=True,
                protected=bool(protected.get(rec.symbol.upper(), True)),
            )
        )

    return RiskBudget(
        daily_loss_floor=daily_loss_floor,
        realized_today=ledger.day_book.realized_for(et_date),
        max_risk_per_trade=max_risk_per_trade,
        max_portfolio_open_risk=max_portfolio_open_risk,
        max_open_positions=max_open_positions,
        cost_allowance_pct=cost_allowance_pct,
        unresolved_value=ledger.day_book.unresolved,
        exposures=exposures,
    )
