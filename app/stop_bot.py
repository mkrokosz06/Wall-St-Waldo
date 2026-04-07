import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from alpaca_client import AlpacaTradingREST
from config import load_config, resolve_bot_log_path


def _pick_env_path() -> str:
    return ".env" if os.path.exists(".env") else ".env.example"

def _read_state(state_path: str) -> Dict[str, Any]:
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state_path: str, state: Dict[str, Any]) -> None:
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path)


def _order_filled_qty_avg(order: Dict[str, Any]) -> tuple[float, Optional[float]]:
    try:
        fq = float(order.get("filled_qty") or 0.0)
    except (TypeError, ValueError):
        fq = 0.0
    try:
        ap = order.get("filled_avg_price")
        avg = None if ap in (None, "") else float(ap)
    except (TypeError, ValueError):
        avg = None
    return fq, avg


def _poll_until_filled(trading: AlpacaTradingREST, order_id: str, timeout_sec: int = 90) -> Optional[Dict[str, Any]]:
    deadline = time.time() + timeout_sec
    last: Optional[Dict[str, Any]] = None
    while time.time() < deadline:
        try:
            o = trading.get_order(order_id)
        except Exception:
            time.sleep(1)
            continue
        last = o
        st = str(o.get("status", "")).lower()
        if st == "filled":
            return o
        if st in {"canceled", "rejected", "expired"}:
            return o
        time.sleep(1)
    return last


def main() -> None:
    # Cancel open orders + market-sell any remaining positions.
    load_dotenv(dotenv_path=_pick_env_path(), override=True)
    cfg = load_config()
    trading = AlpacaTradingREST(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    state = _read_state(cfg.state_path)

    # Cancel any open orders.
    open_orders = trading.get_open_orders()
    for o in open_orders:
        oid = o.get("id")
        if not oid:
            continue
        try:
            trading.cancel_order(str(oid))
        except Exception:
            # Best-effort cancel; continue.
            pass

    time.sleep(1)

    log_path = resolve_bot_log_path()

    # Build per-symbol entry data from multi-leg state (position_legs), fall back to top-level.
    legs: Dict[str, Any] = {}
    raw_legs = state.get("position_legs")
    if isinstance(raw_legs, dict):
        for s, leg in raw_legs.items():
            if isinstance(leg, dict):
                legs[str(s).upper()] = leg
    # Legacy fallback: single top-level position
    if not legs:
        ts = state.get("entry_symbol")
        ea = state.get("entry_avg_price")
        eq = state.get("entry_filled_qty")
        if ts and ea:
            legs[str(ts).upper()] = {"entry_avg_price": ea, "entry_filled_qty": float(eq or 0)}

    # Market sell any remaining position.
    positions = trading.get_positions()
    for p in positions:
        sym = p.get("symbol")
        qty = float(p.get("qty", 0) or 0)
        if not sym or qty <= 0:
            continue
        resp = trading.submit_order(
            {
                "symbol": str(sym),
                "qty": str(qty),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
            }
        )
        sym_u = str(sym).upper()
        leg = legs.get(sym_u, {})
        entry_avg = leg.get("entry_avg_price") or state.get("entry_avg_price")

        order_id = resp.get("id")
        if order_id and entry_avg:
            filled = _poll_until_filled(trading, str(order_id), timeout_sec=90)
            if filled:
                fq, sell_avg = _order_filled_qty_avg(filled)
                if fq > 0 and sell_avg is not None:
                    realized = (float(sell_avg) - float(entry_avg)) * float(fq)
                    try:
                        state["daily_realized_pnl"] = float(state.get("daily_realized_pnl") or 0.0) + realized
                    except Exception:
                        state["daily_realized_pnl"] = realized
                    # Write a log line so the trade parser records this flatten.
                    try:
                        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.000")
                        daily_pnl = state.get("daily_realized_pnl", realized)
                        log_line = (
                            f"{ts_str} INFO EXIT filled {sym_u}. "
                            f"exit_pnl={realized:.2f} daily_realized_pnl={daily_pnl:.2f} "
                            f"halt_new_entries=False\n"
                        )
                        with open(log_path, "a", encoding="utf-8") as lf:
                            lf.write(log_line)
                    except Exception:
                        pass
                    # Remove leg from position_legs.
                    if isinstance(state.get("position_legs"), dict):
                        state["position_legs"].pop(sym_u, None)
        # Reset top-level fields if this was the legacy tracked symbol.
        if sym_u == str(state.get("entry_symbol", "")).upper():
            state["state"] = "FLAT"
            state["entry_order_id"] = None
            state["entry_symbol"] = None
            state["entry_submitted_at"] = None
            state["entry_filled_qty"] = 0.0
            state["entry_avg_price"] = None
            state["entry_filled_at"] = None
            state["stop_order_id"] = None
            state["stop_submitted_at"] = None
            state["exit_order_id"] = None
            state["exit_submitted_at"] = None
    # If all legs cleared, mark FLAT.
    if isinstance(state.get("position_legs"), dict) and not state["position_legs"]:
        state["state"] = "FLAT"
    _write_state(cfg.state_path, state)

    # Stop the running bot process if bot.lock exists.
    lock_path = os.getenv("BOT_LOCK_PATH", "bot.lock")
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                pid = f.read().strip()
            if pid:
                # Use os.system so we don't depend on PowerShell availability here.
                os.system(f"taskkill /PID {pid} /F >NUL 2>&1")
        except Exception:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()

