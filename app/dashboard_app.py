"""
Local web dashboard for the ETF bot (read-only + start/stop).

Run from the `app` folder:
  python dashboard_app.py

Then open: http://127.0.0.1:5050

Binds to localhost only by default (not exposed on LAN).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from flask import Flask, jsonify, render_template
from zoneinfo import ZoneInfo

from config import ET_TZ, load_config, resolve_bot_log_path
from dashboard_runtime import get_snapshot, pick_env_path
from dotenv import load_dotenv
from state_store import StateStore
from trade_log_parser import aggregate_stats, parse_trades_from_log

load_dotenv(dotenv_path=pick_env_path(), override=True)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = resolve_bot_log_path()

app = Flask(
    __name__,
    template_folder=os.path.join(APP_ROOT, "templates"),
    static_folder=os.path.join(APP_ROOT, "static"),
)


def _et_today_str() -> str:
    return datetime.now(tz=ET_TZ).date().isoformat()


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    try:
        snap = get_snapshot()
        return jsonify(snap)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/trades")
def api_trades():
    try:
        trades = parse_trades_from_log(LOG_PATH)
        et_day = _et_today_str()
        stats = aggregate_stats(trades, et_date_str=et_day)
        snap = get_snapshot()
        # Broker-tracked daily (authoritative for session) vs log-derived today
        broker_daily = snap.get("daily_realized_pnl")
        return jsonify(
            {
                "ok": True,
                "trades": [t.to_dict() for t in reversed(trades[-500:])],  # newest first, cap 500
                "stats": stats,
                "broker_daily_realized_pnl": broker_daily,
                "et_today": et_day,
                "log_path": LOG_PATH,
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/start", methods=["POST"])
def api_start():
    lock_path = os.path.join(APP_ROOT, os.getenv("BOT_LOCK_PATH", "bot.lock"))
    if os.path.exists(lock_path):
        return jsonify({"ok": False, "error": "Bot lock file exists — already running or stale lock."}), 400
    run_py = os.path.join(APP_ROOT, "run_bot.py")
    if not os.path.isfile(run_py):
        return jsonify({"ok": False, "error": f"Missing {run_py}"}), 500
    try:
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags |= subprocess.DETACHED_PROCESS
            subprocess.Popen(
                [sys.executable, run_py],
                cwd=APP_ROOT,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [sys.executable, run_py],
                cwd=APP_ROOT,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "message": "Started run_bot.py"})


@app.route("/api/resume-entries", methods=["POST"])
def api_resume_entries():
    """Clear halt_new_entries in state.json when safe (same rules as bot startup)."""
    try:
        cfg = load_config()
        store = StateStore(cfg.state_path)
        st = store.load()
        if st.state != "FLAT":
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Bot state is not FLAT; resolve position or run stop_bot first.",
                    }
                ),
                400,
            )
        if st.daily_realized_pnl <= cfg.max_daily_realized_loss:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Daily loss kill-switch active; halt cannot be cleared from dashboard.",
                    }
                ),
                400,
            )
        if not st.halt_new_entries:
            return jsonify({"ok": True, "message": "New entries were already allowed."})
        st.halt_new_entries = False
        store.save(st)
        return jsonify({"ok": True, "message": "halt_new_entries cleared; new entries allowed."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_py = os.path.join(APP_ROOT, "stop_bot.py")
    if not os.path.isfile(stop_py):
        return jsonify({"ok": False, "error": f"Missing {stop_py}"}), 500
    try:
        subprocess.run(
            [sys.executable, stop_py],
            cwd=APP_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "message": "Ran stop_bot.py (flatten + kill)"})


def main() -> None:
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    debug = os.getenv("DASHBOARD_DEBUG", "").lower() in {"1", "true", "yes"}
    print(f"Dashboard: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
