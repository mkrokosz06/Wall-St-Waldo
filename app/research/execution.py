"""
Named execution models for the backtest (Phase 8).

The engine had three accounting defects that all pushed results in the
optimistic direction, and one that made them internally inconsistent.

1. **Entries never paid the spread.** A buy filled at
   ``next_open * (1 + slippage)``. The modelled bid/ask existed — ``bid_f`` was
   computed from ``spread_bps`` — but it was applied only to exits. So a round
   trip paid roughly half the modelled spread instead of all of it.

   This is visible in the published results and explains something that looked
   like robustness: the live config returns -28.02, -27.71 and -27.83 at 2, 4
   and 10 bp of spread. A strategy trading 1,370 times in eight months cannot be
   indifferent to a fivefold change in transaction costs. It was indifferent
   because four fifths of that cost was never charged.

2. **Take-profit exits credited the mid, not the bid.** The trigger was computed
   correctly on the bid (``b_high * bid_f >= target``) and then immediately
   converted back with ``raw_tp = target / bid_f``, which cancels the
   adjustment. The position was credited a mid price above the bid it was
   supposed to have sold at.

3. **Stops and structure exits used slippage only**, with no bid side.

4. **Fees were charged inconsistently.** ``_close_leg`` deducted
   ``commission_per_order`` from cash and ``2 * commission_per_order`` from the
   trade's P&L, then deducted ``cfg.fee_estimate_per_order`` from the P&L as
   well — a second, different fee that never touched cash. With both defaulting
   to 0.0 nothing was wrong in practice, but cash and realized P&L could not
   both be right once either was set.

This module makes the price model explicit and named, so a saved result records
which assumptions produced it.

Models
------
``spread_aware`` (default)
    Buys execute on the ask plus slippage; sells execute on the bid minus
    slippage. One fee policy, charged once per order to both cash and P&L.

``legacy``
    Reproduces the pre-2026-09-15 behaviour exactly, so every number already in
    ``FINDINGS.md`` stays reproducible. Optimistic, and labelled as such.

``frictionless``
    No spread, no slippage, no fees. For isolating strategy logic from costs;
    never for evaluating profitability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SPREAD_AWARE = "spread_aware"
LEGACY = "legacy"
FRICTIONLESS = "frictionless"

VALID_MODELS = (SPREAD_AWARE, LEGACY, FRICTIONLESS)


@dataclass(frozen=True)
class ExecutionModel:
    """
    How a reference price becomes a fill.

    ``spread_bps`` is the full bid/ask spread, so each side is half of it.
    ``slippage_bps`` is adverse in both directions — it always costs the
    trader, never pays them.
    """

    name: str = SPREAD_AWARE
    spread_bps: float = 2.0
    slippage_bps: float = 1.0
    fee_per_order: float = 0.0

    def __post_init__(self) -> None:
        if self.name not in VALID_MODELS:
            raise ValueError(f"unknown execution model {self.name!r}; expected {VALID_MODELS}")
        if self.spread_bps < 0 or self.slippage_bps < 0 or self.fee_per_order < 0:
            raise ValueError("execution costs must be non-negative")

    # ---- derived factors ---------------------------------------------------

    @property
    def half_spread(self) -> float:
        if self.name == FRICTIONLESS:
            return 0.0
        return (self.spread_bps / 2.0) / 1e4

    @property
    def slip(self) -> float:
        if self.name == FRICTIONLESS:
            return 0.0
        return self.slippage_bps / 1e4

    @property
    def bid_factor(self) -> float:
        """Reference -> bid."""
        return 1.0 - self.half_spread

    @property
    def ask_factor(self) -> float:
        """Reference -> ask."""
        return 1.0 + self.half_spread

    @property
    def spread_pct(self) -> float:
        """Full spread as a fraction, for the bot's max_spread_pct gate."""
        return 0.0 if self.name == FRICTIONLESS else self.spread_bps / 1e4

    @property
    def fee(self) -> float:
        return 0.0 if self.name == FRICTIONLESS else float(self.fee_per_order)

    # ---- fills -------------------------------------------------------------

    def buy_fill(self, reference: float) -> float:
        """
        Price paid to buy at ``reference``.

        ``legacy`` charges slippage only — the defect this model documents.
        """
        if self.name == LEGACY:
            return reference * (1.0 + self.slip)
        return reference * self.ask_factor * (1.0 + self.slip)

    def sell_fill(self, reference: float) -> float:
        """Price received to sell at ``reference``."""
        if self.name == LEGACY:
            return reference * (1.0 - self.slip)
        return reference * self.bid_factor * (1.0 - self.slip)

    def cap_buy_at_limit(self, fill: float, limit_px: Optional[float]) -> Optional[float]:
        """
        Enforce that a buy limit never fills above its limit.

        Returns the fill, or ``None`` when the order could not have filled.
        Slippage cannot push a fill through its own limit — that is the whole
        point of a limit order, and the engine previously allowed it.
        """
        if limit_px is None:
            return fill
        return fill if fill <= limit_px else None

    def stop_fill(self, stop_px: float, bar_open: float, bar_low: float) -> float:
        """
        Price received when a sell stop triggers.

        Models gap-through: if the bar opened below the stop, the fill is the
        open, not the stop price. A stop is not a guaranteed price, and assuming
        it is understates loss on exactly the bars that hurt most.
        """
        reference = min(stop_px, bar_open) if bar_open < stop_px else stop_px
        reference = max(reference, bar_low)
        return self.sell_fill(reference)

    # ---- reporting ---------------------------------------------------------

    def manifest(self) -> dict:
        """Secret-free description, for the result manifest."""
        return {
            "execution_model": self.name,
            "spread_bps": self.spread_bps,
            "slippage_bps": self.slippage_bps,
            "fee_per_order": self.fee_per_order,
            "half_spread": self.half_spread,
            "notes": {
                LEGACY: "entries pay no spread; take-profit credits the mid. Optimistic.",
                SPREAD_AWARE: "buys on the ask, sells on the bid, both plus adverse slippage.",
                FRICTIONLESS: "no costs at all; not a profitability estimate.",
            }[self.name],
        }

    def round_trip_cost_pct(self) -> float:
        """
        Total modelled cost of a round trip, as a fraction of notional.

        Useful as a sanity check against a strategy's target: a 1.2% target
        against a 0.12% round-trip cost spends 10% of the target on costs.
        """
        if self.name == LEGACY:
            return 2.0 * self.slip
        return 2.0 * (self.half_spread + self.slip)
