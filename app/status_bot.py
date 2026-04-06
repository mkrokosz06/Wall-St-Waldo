"""
Quick runtime status for the ETF bot.

Run:
  python status_bot.py
"""

from dotenv import load_dotenv

from config import load_config
from dashboard_runtime import get_snapshot, pick_env_path


def main() -> None:
    load_dotenv(dotenv_path=pick_env_path(), override=True)
    cfg = load_config()
    s = get_snapshot()

    positions_text = "none"
    pos = s.get("positions") or []
    if pos:
        parts = []
        for p in pos:
            parts.append(
                f"{p.get('symbol')} qty={p.get('qty')} side={p.get('side')} market_value={p.get('market_value')}"
            )
        positions_text = "; ".join(parts)

    print("=== BOT STATUS ===")
    print(f"running: {s.get('running')}")
    print(f"pid: {s.get('pid') or 'none'}")
    print(f"mode: {'paper' if cfg.paper else 'live'}")
    print(f"state: {s.get('bot_state', 'unknown')}")
    dr = s.get("daily_realized_pnl")
    if dr is None:
        print("daily_realized_pnl: unknown")
    else:
        print(f"daily_realized_pnl: {float(dr):.2f}")
    print(f"positions: {positions_text}")


if __name__ == "__main__":
    main()
