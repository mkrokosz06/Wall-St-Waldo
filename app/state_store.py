import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, time, timezone
from typing import Any, Dict, List, Optional


ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"


def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # datetime.fromisoformat handles offsets like "+00:00"
    return datetime.fromisoformat(s)


def _today_utc_iso() -> str:
    return date.today().isoformat()


@dataclass
class BotState:
    # High-level state machine
    state: str = "FLAT"  # FLAT, ENTRY_PENDING, IN_POSITION, EXIT_PENDING

    # Entry order tracking
    entry_order_id: Optional[str] = None
    entry_client_order_id: Optional[str] = None
    entry_symbol: Optional[str] = None
    entry_submitted_at: Optional[datetime] = None

    # Filled entry details (used for stop-loss + time stop + realized P&L)
    entry_filled_qty: float = 0.0
    entry_avg_price: Optional[float] = None
    entry_filled_at: Optional[datetime] = None

    # Stop-loss exit tracking
    stop_order_id: Optional[str] = None
    stop_submitted_at: Optional[datetime] = None

    # Time-stop / market-exit tracking
    exit_order_id: Optional[str] = None
    exit_submitted_at: Optional[datetime] = None

    # Daily realized P&L kill-switch
    day_utc: str = _today_utc_iso()
    daily_realized_pnl: float = 0.0
    halt_new_entries: bool = False
    # Account equity at start of current trading day (for drawdown / ROI tracking).
    day_start_equity: float = 0.0

    # Retry + cooldown
    entry_attempts_today: int = 0
    last_entry_attempt_at: Optional[datetime] = None
    # Set when a position closes (stop or market exit) so cooldown can run from exit time.
    last_exit_at: Optional[datetime] = None
    last_exit_pnl: Optional[float] = None

    # For safety/reconciliation
    last_reconciled_at: Optional[datetime] = None

    # Offline "training" knobs (learned from recent paper results in bot.log).
    # If None, bot will fall back to config defaults.
    dynamic_entry_score_threshold: Optional[float] = None
    dynamic_min_momentum_return: Optional[float] = None
    # Rolling window of realized P&amp;L per closed trade (online training uses mean of this list).
    recent_trade_pnls: List[float] = field(default_factory=list)

    # Multi-position: keyed by symbol — stop_order_id, peak_price_since_entry, entry_filled_at (ISO),
    # entry_avg_price, pending_exit_realized_accumulator (float).
    position_legs: Dict[str, Any] = field(default_factory=dict)
    # Per-symbol re-entry cooldown: do not open a new position in that symbol until this UTC time (ISO str).
    symbol_next_entry_ok_after: Dict[str, str] = field(default_factory=dict)
    # Market exit currently in flight for this symbol (single-flight exit orders).
    exit_pending_symbol: Optional[str] = None
    # Symbols that need a market exit once the current exit order completes.
    pending_exit_symbols: List[str] = field(default_factory=list)

    # Intraday position management (trailing stop / partial exit P&L)
    peak_price_since_entry: Optional[float] = None
    # When time-stop cancels a partially-filled stop, accumulate realized here until exit order fills.
    pending_exit_realized_accumulator: float = 0.0

    def to_json(self) -> str:
        d = asdict(self)
        # Convert datetimes to ISO strings
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = _dt_to_iso(v)
        return json.dumps(d, indent=2)

    @staticmethod
    def from_json(raw: str) -> "BotState":
        data: Dict[str, Any] = json.loads(raw)
        # Merge with defaults so older state.json files stay valid after new fields are added.
        merged: Dict[str, Any] = asdict(BotState())
        for k in merged:
            if k in data:
                merged[k] = data[k]
        # Convert ISO strings back to datetimes where relevant
        for key in [
            "entry_submitted_at",
            "entry_filled_at",
            "stop_submitted_at",
            "exit_submitted_at",
            "last_entry_attempt_at",
            "last_exit_at",
            "last_reconciled_at",
        ]:
            merged[key] = _iso_to_dt(merged.get(key))
        raw_pnls = merged.get("recent_trade_pnls")
        if isinstance(raw_pnls, list):
            merged["recent_trade_pnls"] = [float(x) for x in raw_pnls]
        else:
            merged["recent_trade_pnls"] = []

        legs = merged.get("position_legs")
        if isinstance(legs, dict):
            merged["position_legs"] = {
                str(k).upper(): dict(v) if isinstance(v, dict) else {} for k, v in legs.items()
            }
        else:
            merged["position_legs"] = {}

        cd = merged.get("symbol_next_entry_ok_after")
        if isinstance(cd, dict):
            merged["symbol_next_entry_ok_after"] = {str(k).upper(): str(v) for k, v in cd.items()}
        else:
            merged["symbol_next_entry_ok_after"] = {}

        pe = merged.get("pending_exit_symbols")
        if isinstance(pe, list):
            merged["pending_exit_symbols"] = [str(x).upper() for x in pe if x]
        else:
            merged["pending_exit_symbols"] = []

        eps = merged.get("exit_pending_symbol")
        merged["exit_pending_symbol"] = str(eps).upper() if eps else None

        return BotState(**merged)


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> BotState:
        if not os.path.exists(self.path):
            return BotState()
        with open(self.path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return BotState()
        return BotState.from_json(raw)

    def save(self, state: BotState) -> None:
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(state.to_json())
        os.replace(tmp_path, self.path)

