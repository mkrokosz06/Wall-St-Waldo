"""Shared helpers for bot process + state (dashboard + CLI)."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from alpaca_client import AlpacaTradingREST
from config import load_config, resolve_bot_log_path


_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def pick_env_path() -> str:
    env = os.path.join(_APP_DIR, ".env")
    return env if os.path.exists(env) else os.path.join(_APP_DIR, ".env.example")


def read_lock_pid(lock_path: str) -> str:
    if not os.path.exists(lock_path):
        return ""
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def pid_is_running(pid: str) -> bool:
    if not pid or not pid.isdigit():
        return False
    proc = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "").lower()
    return f" {pid} " in out and "no tasks are running" not in out


def read_state(state_path: str) -> Dict[str, Any]:
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_positions_safe() -> List[Dict[str, Any]]:
    load_dotenv(dotenv_path=pick_env_path(), override=True)
    cfg = load_config()
    try:
        trading = AlpacaTradingREST(cfg.api_key, cfg.api_secret, paper=cfg.paper)
        return trading.get_positions()
    except Exception:
        return []


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def fetch_broker_snapshot() -> Tuple[
    List[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
]:
    """Fetch positions, account, and clock in one shot. Returns (positions, account, clock).

    Any individual field may be None if the broker call failed.
    """
    load_dotenv(dotenv_path=pick_env_path(), override=True)
    cfg = load_config()
    positions: List[Dict[str, Any]] = []
    account: Optional[Dict[str, Any]] = None
    clock: Optional[Dict[str, Any]] = None
    try:
        trading = AlpacaTradingREST(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    except Exception:
        return positions, account, clock
    try:
        positions = trading.get_positions() or []
    except Exception:
        positions = []
    try:
        account = trading.get_account()
    except Exception:
        account = None
    try:
        clock = trading.get_clock()
    except Exception:
        clock = None
    return positions, account, clock


def get_snapshot() -> Dict[str, Any]:
    """Unified status for dashboard / API."""
    load_dotenv(dotenv_path=pick_env_path(), override=True)
    cfg = load_config()
    lock_path = os.getenv("BOT_LOCK_PATH", os.path.join(_APP_DIR, "bot.lock"))
    pid = read_lock_pid(lock_path)
    running = pid_is_running(pid)
    state = read_state(cfg.state_path)

    positions, account, clock = fetch_broker_snapshot()

    pos_rows: List[Dict[str, Any]] = []
    total_unrealized = 0.0
    total_market_value = 0.0
    for p in positions:
        qty = _safe_float(p.get("qty"))
        if qty <= 0:
            continue
        market_value = _safe_float(p.get("market_value"))
        avg_entry = _safe_float(p.get("avg_entry_price"))
        current_price = _safe_float(p.get("current_price"))
        unrealized_pl = _safe_float(p.get("unrealized_pl"))
        unrealized_plpc = _safe_float(p.get("unrealized_plpc"))
        # Intraday P&L (since today's open) — useful on a day trading dashboard.
        unrealized_intraday_pl = _safe_float(p.get("unrealized_intraday_pl"))
        unrealized_intraday_plpc = _safe_float(p.get("unrealized_intraday_plpc"))
        change_today = _safe_float(p.get("change_today"))
        lastday_price = _safe_float(p.get("lastday_price"))
        total_unrealized += unrealized_pl
        total_market_value += market_value
        pos_rows.append(
            {
                "symbol": str(p.get("symbol", "")),
                "qty": qty,
                "side": str(p.get("side", "long")),
                "market_value": market_value,
                "avg_entry_price": avg_entry,
                "current_price": current_price,
                "lastday_price": lastday_price,
                "unrealized_pl": unrealized_pl,
                "unrealized_plpc": unrealized_plpc,
                "unrealized_intraday_pl": unrealized_intraday_pl,
                "unrealized_intraday_plpc": unrealized_intraday_plpc,
                "change_today": change_today,
            }
        )

    if account:
        account_info: Optional[Dict[str, Any]] = {
            "equity": _safe_float(account.get("equity")),
            "last_equity": _safe_float(account.get("last_equity")),
            "cash": _safe_float(account.get("cash")),
            "buying_power": _safe_float(account.get("buying_power")),
            "portfolio_value": _safe_float(account.get("portfolio_value")),
            "long_market_value": _safe_float(account.get("long_market_value")),
            "daytrade_count": int(_safe_float(account.get("daytrade_count"))),
            "pattern_day_trader": bool(account.get("pattern_day_trader", False)),
            "trading_blocked": bool(account.get("trading_blocked", False)),
            "account_blocked": bool(account.get("account_blocked", False)),
            "status": str(account.get("status", "")),
        }
        equity_now = account_info["equity"]
        last_equity = account_info["last_equity"]
        if last_equity > 0:
            account_info["day_change_abs"] = round(equity_now - last_equity, 2)
            account_info["day_change_pct"] = round((equity_now - last_equity) / last_equity * 100.0, 3)
        else:
            account_info["day_change_abs"] = 0.0
            account_info["day_change_pct"] = 0.0
    else:
        account_info = None

    if clock:
        clock_info: Optional[Dict[str, Any]] = {
            "is_open": bool(clock.get("is_open", False)),
            "timestamp": clock.get("timestamp"),
            "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"),
        }
    else:
        clock_info = None

    day_start_equity = _safe_float(state.get("day_start_equity"))

    return {
        "running": running,
        "pid": pid or None,
        "paper": cfg.paper,
        "bot_state": state.get("state", "unknown"),
        "daily_realized_pnl": state.get("daily_realized_pnl"),
        "halt_new_entries": bool(state.get("halt_new_entries", False)),
        "dynamic_entry_score_threshold": state.get("dynamic_entry_score_threshold"),
        "dynamic_min_momentum_return": state.get("dynamic_min_momentum_return"),
        "entry_score_threshold_default": cfg.entry_score_threshold,
        "min_momentum_return_default": cfg.min_momentum_return,
        "max_daily_realized_loss": cfg.max_daily_realized_loss,
        "max_open_positions": cfg.max_open_positions,
        "max_portfolio_notional_usd": cfg.max_portfolio_notional_usd,
        "stop_loss_pct": cfg.stop_loss_pct,
        "take_profit_pct": cfg.take_profit_pct,
        "trailing_stop_pct": cfg.trailing_stop_pct,
        "symbols_universe": list(cfg.symbols_universe or []),
        "day_utc": state.get("day_utc"),
        "day_start_equity": day_start_equity,
        "positions": pos_rows,
        "positions_total_unrealized_pl": round(total_unrealized, 4),
        "positions_total_market_value": round(total_market_value, 4),
        "account": account_info,
        "clock": clock_info,
        "lock_path": lock_path,
        "log_path": resolve_bot_log_path(),
        "state_path": os.path.abspath(cfg.state_path),
    }
