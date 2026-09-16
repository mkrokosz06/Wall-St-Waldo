"""
Offline test doubles: a fake broker, a fake clock, and a temp state store.

None of these touch the network or read credentials. Importing this module must
never trigger authentication — that is why the real ``AlpacaTradingREST`` is not
imported here, and why tests construct ``TradeBot`` through its injection seam
rather than letting ``__init__`` build live clients.

The fake broker models the order-lifecycle behaviour that actually caused
production losses, not an idealised broker:

- ``filled_qty`` / ``filled_avg_price`` are *cumulative*, as Alpaca reports them.
- a cancel request can leave an order in ``pending_cancel``, and a fill can
  still land afterwards (the race that produced oversells).
- a position can disappear while its stop order is still reportable, which is
  the sequence that used to lose a stop fill entirely.
- list calls can be made to fail or return malformed data, so tests can prove
  that a failed read never prunes exposure.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class BrokerError(RuntimeError):
    """Stand-in for a transport/API failure."""


class FakeClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, now: Optional[datetime] = None):
        self._now = now or datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)

    def utc_now(self) -> datetime:
        return self._now

    def et_now(self) -> datetime:
        return self._now.astimezone(ET)

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt

    def monotonic(self) -> float:
        return self._now.timestamp()


@dataclass
class FakeOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    order_type: str = "market"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = "new"
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    filled_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    executions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "type": self.order_type,
            "limit_price": (str(self.limit_price) if self.limit_price is not None else None),
            "stop_price": (str(self.stop_price) if self.stop_price is not None else None),
            "status": self.status,
            "filled_qty": str(self.filled_qty),
            "filled_avg_price": (str(self.filled_avg_price) if self.filled_qty else None),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
        }


class FakeBroker:
    """
    In-memory broker with explicit, test-driven state transitions.

    Nothing here happens on its own: a test fills an order by calling
    :meth:`fill`, and the position book only changes when the test says so. That
    makes every scenario deterministic and lets a test reproduce orderings that
    are rare but expensive in production.
    """

    def __init__(self, clock: Optional[FakeClock] = None):
        self.clock = clock or FakeClock()
        self.orders: Dict[str, FakeOrder] = {}
        self.positions: Dict[str, Dict[str, float]] = {}
        self._ids = itertools.count(1)
        # Failure injection.
        self.fail_list_positions = False
        self.fail_list_orders = False
        self.fail_submit = False
        self.submit_then_fail = False  # accept the order, then raise (timeout shape)
        self.malformed_positions = False
        self.cancel_leaves_pending = False
        self.submitted_client_ids: List[str] = []

    # ---- submission --------------------------------------------------------

    def submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        if self.fail_submit:
            raise BrokerError("submit failed before the broker saw it")
        cid = client_order_id or f"auto-{next(self._ids)}"
        if any(o.client_order_id == cid for o in self.orders.values()):
            raise BrokerError(f"duplicate client_order_id {cid}")
        oid = f"ord-{next(self._ids)}"
        order = FakeOrder(
            id=oid,
            client_order_id=cid,
            symbol=symbol.upper(),
            side=side,
            qty=float(qty),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            submitted_at=self.clock.utc_now(),
        )
        self.orders[oid] = order
        self.submitted_client_ids.append(cid)
        if self.submit_then_fail:
            # The broker accepted it but the caller never learns the id. This is
            # the case that must be recovered by client order id, not retried.
            raise BrokerError("timeout after broker accepted the order")
        return order.to_dict()

    # ---- lookup ------------------------------------------------------------

    def get_order(self, order_id: str) -> Dict[str, Any]:
        if order_id not in self.orders:
            raise BrokerError(f"no such order {order_id}")
        return self.orders[order_id].to_dict()

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        for o in self.orders.values():
            if o.client_order_id == client_order_id:
                return o.to_dict()
        return None

    def get_order_executions(self, order_id: str) -> List[Dict[str, Any]]:
        if order_id not in self.orders:
            raise BrokerError(f"no such order {order_id}")
        return list(self.orders[order_id].executions)

    def list_orders(self, status: str = "open", **_: Any) -> List[Dict[str, Any]]:
        if self.fail_list_orders:
            raise BrokerError("orders endpoint unavailable")
        out = []
        for o in self.orders.values():
            live = o.status in {"new", "accepted", "partially_filled", "pending_cancel"}
            if status == "open" and not live:
                continue
            if status == "closed" and live:
                continue
            out.append(o.to_dict())
        return out

    def list_positions(self) -> List[Dict[str, Any]]:
        if self.fail_list_positions:
            raise BrokerError("positions endpoint unavailable")
        if self.malformed_positions:
            return [{"symbol": "TQQQ"}]  # no qty field at all
        return [
            {
                "symbol": sym,
                "qty": str(p["qty"]),
                "avg_entry_price": str(p["avg"]),
                "market_value": str(p["qty"] * p.get("last", p["avg"])),
                "current_price": str(p.get("last", p["avg"])),
            }
            for sym, p in self.positions.items()
            if p["qty"] != 0
        ]

    def cancel_order(self, order_id: str) -> None:
        if order_id not in self.orders:
            raise BrokerError(f"no such order {order_id}")
        o = self.orders[order_id]
        if o.status in {"filled", "canceled", "rejected", "expired"}:
            return
        o.status = "pending_cancel" if self.cancel_leaves_pending else "canceled"

    # ---- test-driven transitions -------------------------------------------

    def fill(
        self,
        order_id: str,
        qty: float,
        price: float,
        *,
        at: Optional[datetime] = None,
        with_execution: bool = False,
        update_position: bool = True,
    ) -> None:
        """
        Add ``qty`` shares of fill to an order, updating cumulative totals the
        way Alpaca does: ``filled_qty`` accumulates and ``filled_avg_price``
        becomes the weighted average.
        """
        o = self.orders[order_id]
        prev_value = o.filled_qty * o.filled_avg_price
        o.filled_qty += qty
        o.filled_avg_price = (prev_value + qty * price) / o.filled_qty
        o.filled_at = at or self.clock.utc_now()
        o.status = "filled" if o.filled_qty >= o.qty - 1e-9 else "partially_filled"
        if with_execution:
            o.executions.append(
                {
                    "id": f"exec-{next(self._ids)}",
                    "qty": str(qty),
                    "price": str(price),
                    "transaction_time": (at or self.clock.utc_now()).isoformat(),
                }
            )
        if update_position:
            self._apply_position(o.symbol, qty if o.side == "buy" else -qty, price)

    def _apply_position(self, symbol: str, dq: float, price: float) -> None:
        p = self.positions.setdefault(symbol.upper(), {"qty": 0.0, "avg": price, "last": price})
        new_qty = p["qty"] + dq
        if dq > 0:
            p["avg"] = (p["qty"] * p["avg"] + dq * price) / new_qty if new_qty else price
        p["qty"] = new_qty
        p["last"] = price
        if abs(p["qty"]) < 1e-9:
            del self.positions[symbol.upper()]

    def set_position(self, symbol: str, qty: float, avg: float, last: Optional[float] = None):
        if qty == 0:
            self.positions.pop(symbol.upper(), None)
            return
        self.positions[symbol.upper()] = {"qty": qty, "avg": avg, "last": last or avg}

    def drop_position(self, symbol: str) -> None:
        """Make a holding vanish without touching its orders."""
        self.positions.pop(symbol.upper(), None)

    def set_status(self, order_id: str, status: str) -> None:
        self.orders[order_id].status = status


class FakeMarketData:
    """Minimal quote/bar source with per-symbol staleness control."""

    def __init__(self, clock: Optional[FakeClock] = None):
        self.clock = clock or FakeClock()
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.fail = False

    def set_quote(
        self,
        symbol: str,
        bid: float,
        ask: float,
        *,
        age_sec: float = 0.0,
        ts: Optional[datetime] = None,
    ) -> None:
        stamp = ts or (self.clock.utc_now() - timedelta(seconds=age_sec))
        self.quotes[symbol.upper()] = {"bid": bid, "ask": ask, "t": stamp}

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        if self.fail:
            raise BrokerError("market data unavailable")
        return self.quotes.get(symbol.upper())
