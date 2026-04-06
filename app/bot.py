import logging
import math
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from alpaca_client import AlpacaMarketData, AlpacaTradingREST
from config import ET_TZ, BotConfig, load_config, resolve_bot_log_path
from offline_trainer import extract_recent_exit_pnls_from_log, train_from_bot_log
from state_store import BotState, StateStore
from strategy_signals import (
    bearish_candlestick_exit,
    candlestick_bullish_strength,
    dual_ma_uptrend,
    trend_break_confirmed_below_sma,
)


# Consecutive 401 failures from the data API before halting new entries.
_DATA_AUTH_FAIL_LIMIT = 3


def _parse_time_hhmm_to_et(time_hhmm: str) -> Tuple[int, int]:
    hh, mm = time_hhmm.split(":")
    return int(hh), int(mm)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class TradeBot:
    def __init__(self) -> None:
        self.config: BotConfig = load_config()
        self.state_store = StateStore(self.config.state_path)
        self.state: BotState = self.state_store.load()

        self.trading = AlpacaTradingREST(
            api_key=self.config.api_key,
            api_secret=self.config.api_secret,
            paper=self.config.paper,
        )
        self.market = AlpacaMarketData(
            api_key=self.config.api_key,
            api_secret=self.config.api_secret,
            paper=self.config.paper,
        )

        # Logging
        self.logger = logging.getLogger("trade_bot")
        self._setup_logging()

        # halt_new_entries persists in state.json; broker/API halts (e.g. failed stop) survive restarts.
        # If we're flat and not at the daily loss kill, clear a stale halt so restarts behave as expected.
        self._maybe_clear_stale_halt_on_startup()

        # Offline training: tune entry filters based on recent paper results in bot.log.
        if self.config.enable_offline_training:
            try:
                tr = train_from_bot_log(
                    self.config, self.state, bot_log_path=resolve_bot_log_path()
                )
                self.state.dynamic_entry_score_threshold = tr.dynamic_entry_score_threshold
                self.state.dynamic_min_momentum_return = tr.dynamic_min_momentum_return
                self.state_store.save(self.state)
                self.logger.info(
                    "Offline training applied: avg_exit_pnl=%.2f n_trades=%d dynamic_entry_score_threshold=%.6f dynamic_min_momentum_return=%.6f",
                    tr.avg_exit_pnl,
                    tr.n_trades,
                    tr.dynamic_entry_score_threshold,
                    tr.dynamic_min_momentum_return,
                )
            except Exception as e:
                self.logger.warning("Offline training failed (non-fatal): %s", e)

        # Seed rolling P&L window from log once so online training reflects recent history after restart.
        if self.config.enable_online_training and not self.state.recent_trade_pnls:
            try:
                n = max(1, int(self.config.online_training_window_trades))
                pnls = extract_recent_exit_pnls_from_log(resolve_bot_log_path(), n)
                if pnls:
                    self.state.recent_trade_pnls = pnls
                    self._apply_online_training_from_window()
                    self.state_store.save(self.state)
                    self.logger.info("Online training window seeded from bot.log (%d trades).", len(pnls))
            except Exception as e:
                self.logger.warning("Online training log seed failed (non-fatal): %s", e)

        # Validate credentials early (prevents running into 401s mid-loop).
        self._validate_auth()

        # Throttle "why no buy" INFO diagnostics (see _maybe_log_entry_diagnosis).
        self._last_entry_diagnostic_ts: float = 0.0

        # Consecutive market-data 401 failures — halt only after _DATA_AUTH_FAIL_LIMIT consecutive hits.
        self._data_auth_fail_count: int = 0

        # Cached broker clock timestamp for stale-data guard (refreshed every 60s).
        self._cached_broker_ts: Optional[datetime] = None
        self._broker_ts_fetched_at: float = 0.0

        # Validate config before starting (catches contradictory settings immediately).
        self.config.validate()

    def _validate_auth(self) -> None:
        try:
            _ = self.trading.get_clock()
            _ = self.trading.get_account()
        except Exception as e:
            msg = str(e)
            ml = msg.lower()
            if "401" in msg and ("unauthorized" in ml or "authorization required" in ml or "not authorized" in ml):
                raise RuntimeError(
                    "Alpaca authorization failed (401 unauthorized). "
                    "Double-check that ALPACA_API_KEY and ALPACA_API_SECRET are your Alpaca *Trading* keys "
                    "and that you pasted the correct values (regenerate keys in Alpaca dashboard if needed). "
                    "This bot uses both paper and live endpoints depending on PAPER, but 401 indicates credentials are invalid."
                ) from e
            raise

    def _setup_logging(self) -> None:
        level = getattr(logging, self.config.log_level.upper(), logging.INFO)
        self.logger.setLevel(level)
        if not self.logger.handlers:
            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            sh.setLevel(level)
            self.logger.addHandler(sh)

            fh = logging.FileHandler(resolve_bot_log_path(), encoding="utf-8")
            fh.setFormatter(fmt)
            fh.setLevel(level)
            self.logger.addHandler(fh)

    def _maybe_clear_stale_halt_on_startup(self) -> None:
        if not self.state.halt_new_entries:
            return
        if self.state.state != "FLAT":
            return
        if self.state.daily_realized_pnl <= self.config.max_daily_realized_loss:
            return
        self.state.halt_new_entries = False
        self.state_store.save(self.state)
        self.logger.info(
            "Cleared stale halt_new_entries on startup (FLAT; daily PnL %.2f above kill threshold %.2f).",
            self.state.daily_realized_pnl,
            self.config.max_daily_realized_loss,
        )

    def _et_now(self) -> datetime:
        return datetime.now(tz=ET_TZ)

    def _utc_now(self) -> datetime:
        return datetime.now(tz=ZoneInfo("UTC"))

    def _snapshot_day_start_equity(self) -> None:
        """Fetch account equity and log / store it as the day-start baseline."""
        try:
            acct = self.trading.get_account()
            equity = float(acct.get("equity") or acct.get("portfolio_value") or 0.0)
            cash = float(acct.get("cash") or 0.0)
            self.state.day_start_equity = equity
            self.logger.info("DAY_START equity=%.2f cash=%.2f", equity, cash)
        except Exception as e:
            self.logger.warning("Could not fetch account equity for DAY_START snapshot: %s", e)

    def _reset_daily_if_needed(self) -> None:
        # We use ET date for the "day" concept since you're day-trading.
        day_et = self._et_now().date().isoformat()
        if self.state.day_utc != day_et:
            self.logger.info("New day detected; resetting daily kill-switch and attempts.")
            self.state.day_utc = day_et
            self.state.daily_realized_pnl = 0.0
            self.state.halt_new_entries = False
            self.state.entry_attempts_today = 0
            self.state.last_entry_attempt_at = None
            self.state.last_exit_at = None
            self.state.last_exit_pnl = None
            self.state.recent_trade_pnls = []
            self._snapshot_day_start_equity()

    def _is_market_open(self) -> bool:
        try:
            clock = self.trading.get_clock()
            return bool(clock.get("is_open"))
        except Exception as e:
            self.logger.warning("Clock fetch failed; assuming market closed: %s", e)
            return False

    def _is_regular_hours(self) -> bool:
        # Additional guard around broker clock.
        now_et = self._et_now()
        if now_et.weekday() >= 5:
            return False

        open_hh, open_mm = _parse_time_hhmm_to_et(self.config.market_open_time_et)
        close_hh, close_mm = _parse_time_hhmm_to_et(self.config.market_close_time_et)
        open_time = now_et.replace(hour=open_hh, minute=open_mm, second=0, microsecond=0)
        close_time = now_et.replace(hour=close_hh, minute=close_mm, second=0, microsecond=0)
        return open_time <= now_et <= close_time

    def _get_broker_ts(self) -> Optional[datetime]:
        """Return cached broker clock timestamp, refreshing at most once every 60 seconds."""
        now_wall = time.time()
        if self._cached_broker_ts is None or (now_wall - self._broker_ts_fetched_at) > 60.0:
            try:
                clock = self.trading.get_clock()
                raw = clock.get("timestamp")
                if raw:
                    self._cached_broker_ts = datetime.fromisoformat(str(raw))
                    self._broker_ts_fetched_at = now_wall
            except Exception:
                pass
        return self._cached_broker_ts

    def _market_stale_guard(self, bars_df: Optional[pd.DataFrame] = None) -> bool:
        """Return True if the newest bar in bars_df is older than stale_data_max_age_sec."""
        if bars_df is None or bars_df.empty:
            return False
        broker_ts = self._get_broker_ts()
        if broker_ts is None:
            return False
        try:
            latest_bar_ts = bars_df.index.get_level_values(1).max()
            # Ensure both timestamps are timezone-aware for comparison.
            if latest_bar_ts.tzinfo is None:
                latest_bar_ts = latest_bar_ts.tz_localize("UTC")
            if broker_ts.tzinfo is None:
                broker_ts = broker_ts.replace(tzinfo=ZoneInfo("UTC"))
            age_sec = (broker_ts - latest_bar_ts).total_seconds()
            if age_sec > self.config.stale_data_max_age_sec:
                self.logger.warning(
                    "Market data stale: newest bar is %.0fs old (limit=%ds). Skipping signal evaluation.",
                    age_sec,
                    self.config.stale_data_max_age_sec,
                )
                return True
        except Exception:
            return False
        return False

    def _list_universe_positions(self) -> List[Tuple[str, float, float, float]]:
        """
        All long ETF positions in the configured universe.
        Returns sorted list of (symbol, qty, avg_entry_price, market_value).
        """
        positions = self.trading.get_positions()
        universe = set(self.config.symbols_universe)
        out: List[Tuple[str, float, float, float]] = []
        for p in positions:
            sym = str(p.get("symbol", "")).upper()
            if sym not in universe:
                continue
            try:
                qty = float(p.get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            avg = float(p.get("avg_entry_price", 0) or 0)
            try:
                mv = float(p.get("market_value", 0) or 0)
            except (TypeError, ValueError):
                mv = 0.0
            if mv <= 0 and avg > 0:
                mv = qty * avg
            out.append((sym, qty, avg, mv))
        out.sort(key=lambda x: x[0])
        return out

    def _total_universe_market_value(self) -> float:
        return sum(p[3] for p in self._list_universe_positions())

    def _position_for_symbol(self, symbol: str) -> Tuple[float, float]:
        """Broker qty and avg for symbol (0,0 if flat)."""
        sym_u = symbol.upper()
        for sym, qty, avg, _ in self._list_universe_positions():
            if sym == sym_u:
                return qty, avg
        return 0.0, 0.0

    def _reconcile_position(self) -> Tuple[float, Optional[str], Optional[float]]:
        """First universe position only (legacy helpers); prefer _list_universe_positions for multi."""
        lst = self._list_universe_positions()
        if not lst:
            return 0.0, None, None
        sym, qty, avg, _ = lst[0]
        return qty, sym, avg

    def _symbol_entry_cooldown_elapsed(self, sym: str) -> bool:
        raw = self.state.symbol_next_entry_ok_after.get(sym.upper())
        if not raw:
            return True
        try:
            ok_after = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        return self._utc_now() >= ok_after

    def _normalize_aggregate_state(self) -> None:
        """Set FLAT vs IN_POSITION from broker + legs when not in a pending order state."""
        if self.state.state in ("ENTRY_PENDING", "EXIT_PENDING"):
            return
        held = self._list_universe_positions()
        if not held and not self.state.position_legs:
            self.state.state = "FLAT"
        elif held:
            self.state.state = "IN_POSITION"

    def _sync_position_legs_with_broker(self) -> None:
        held = self._list_universe_positions()
        held_set = {h[0] for h in held}
        for sym in list(self.state.position_legs.keys()):
            if sym in held_set:
                continue
            if self.state.state == "EXIT_PENDING" and self.state.exit_pending_symbol == sym:
                continue
            self.logger.info("Removing stale leg (no broker position): %s", sym)
            self.state.position_legs.pop(sym, None)
        for sym, qty, avg, _ in held:
            if sym in self.state.position_legs:
                continue
            self.state.position_legs[sym] = {
                "stop_order_id": None,
                "peak_price_since_entry": float(avg),
                "entry_filled_at": None,
                "entry_avg_price": float(avg),
                "pending_exit_realized_accumulator": 0.0,
            }
            self.logger.info("Bootstrapped position leg from broker: %s qty=%s avg=%.4f", sym, qty, avg)

    def _migrate_legacy_single_leg_if_needed(self) -> None:
        if self.state.position_legs:
            return
        if self.state.state != "IN_POSITION" or not self.state.entry_symbol:
            return
        sym = str(self.state.entry_symbol).upper()
        ef = self.state.entry_filled_at.isoformat() if self.state.entry_filled_at else None
        self.state.position_legs[sym] = {
            "stop_order_id": self.state.stop_order_id,
            "peak_price_since_entry": self.state.peak_price_since_entry,
            "entry_filled_at": ef,
            "entry_avg_price": float(self.state.entry_avg_price or 0),
            "pending_exit_realized_accumulator": float(self.state.pending_exit_realized_accumulator or 0),
        }
        self.logger.info("Migrated single-position state into position_legs[%s]", sym)

    def _find_active_stop_order(self, sym: str) -> Optional[str]:
        """
        Query Alpaca for any live stop-sell order for `sym`.
        Returns the order ID string if found, else None.
        Used to prevent duplicate stop placements after a crash/restart.
        """
        try:
            open_orders = self.trading.get_open_orders(symbols=[sym.upper()])
            for o in open_orders:
                if str(o.get("symbol", "")).upper() != sym.upper():
                    continue
                if str(o.get("side", "")).lower() != "sell":
                    continue
                o_type = str(o.get("order_type", "") or o.get("type", "")).lower()
                if "stop" not in o_type:
                    continue
                status = str(o.get("status", "")).lower()
                if status not in {"new", "submitted", "accepted", "pending_new"}:
                    continue
                return str(o.get("id"))
        except Exception as e:
            self.logger.warning("_find_active_stop_order(%s) failed: %s", sym, e)
        return None

    def _place_stop_sell_at_price(self, symbol: str, qty: float, stop_price: float) -> str:
        stop_price_q = self._quantize_stop_price(stop_price)
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "time_in_force": "day",
            # Send as string to avoid float representation issues.
            "stop_price": str(stop_price_q),
        }
        order = self.trading.submit_order(payload)
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError(f"Stop-loss order did not return id: {order}")
        return str(order_id)

    def _initial_stop_price(self, entry_avg_price: float) -> float:
        return float(entry_avg_price) * (1.0 - self.config.stop_loss_pct)

    def _flat_state_preserve_meta(self) -> BotState:
        """FLAT state while keeping daily + learned knobs + per-symbol entry cooldowns."""
        return BotState(
            state="FLAT",
            day_utc=self.state.day_utc,
            daily_realized_pnl=self.state.daily_realized_pnl,
            halt_new_entries=self.state.halt_new_entries,
            entry_attempts_today=self.state.entry_attempts_today,
            last_entry_attempt_at=self.state.last_entry_attempt_at,
            last_exit_at=self.state.last_exit_at,
            last_exit_pnl=self.state.last_exit_pnl,
            dynamic_entry_score_threshold=self.state.dynamic_entry_score_threshold,
            dynamic_min_momentum_return=self.state.dynamic_min_momentum_return,
            recent_trade_pnls=list(self.state.recent_trade_pnls),
            symbol_next_entry_ok_after=dict(self.state.symbol_next_entry_ok_after),
            position_legs={},
            pending_exit_symbols=[],
            exit_pending_symbol=None,
        )

    def _record_exit_for_cooldown(self, symbol: str, exit_pnl: float) -> None:
        sym = symbol.upper()
        now = self._utc_now()
        cd = timedelta(seconds=float(self.config.entry_cooldown_sec))
        if exit_pnl < 0:
            cd += timedelta(seconds=float(self.config.post_loss_extra_cooldown_sec))
        self.state.symbol_next_entry_ok_after[sym] = (now + cd).isoformat()
        self.state.last_exit_at = now
        self.state.last_exit_pnl = float(exit_pnl)

    def _structure_exit_skipped_because_profitable(
        self,
        symbol: str,
        entry_avg: float,
        loop_quotes: Optional[dict] = None,
    ) -> bool:
        """If True, skip trend-break / candlestick exits (take-profit and stop still apply)."""
        if not self.config.trend_break_skip_if_profitable:
            return False
        if entry_avg <= 0:
            return False
        try:
            if loop_quotes is not None:
                q = loop_quotes.get(symbol)
            else:
                quotes = self.market.get_latest_quotes([symbol])
                q = quotes.get(symbol)
            if not q:
                return False
            bid = float(q.bid)
            min_p = float(self.config.trend_break_min_profit_pct_to_skip)
            if bid >= float(entry_avg) * (1.0 + min_p):
                return True
        except Exception:
            return False
        return False

    def _online_nudge_from_trade(self, trade_realized_pnl: float) -> None:
        """
        Append this trade to a rolling window and set entry filters from mean(window).

        Same idea as offline training (avg over many trades), not a single-step random walk on last P&L.
        """
        if not self.config.enable_online_training:
            return
        window = max(1, int(self.config.online_training_window_trades))
        pnls = list(self.state.recent_trade_pnls)
        pnls.append(float(trade_realized_pnl))
        self.state.recent_trade_pnls = pnls[-window:]
        self._apply_online_training_from_window()

    def _apply_online_training_from_window(self) -> None:
        """Recompute dynamic entry thresholds from state.recent_trade_pnls (no new trade appended)."""
        if not self.config.enable_online_training:
            return
        pnls = self.state.recent_trade_pnls
        min_n = max(1, int(self.config.online_training_min_trades))
        if len(pnls) < min_n:
            self.logger.debug(
                "Online training: window has %d/%d trades; keeping dynamic thresholds unchanged.",
                len(pnls),
                min_n,
            )
            return

        avg = sum(pnls) / len(pnls)
        band = float(self.config.online_training_neutral_band_abs_usd)
        if abs(avg) <= band:
            self.state.dynamic_entry_score_threshold = None
            self.state.dynamic_min_momentum_return = None
            self.logger.info(
                "Online training: window=%d mean=%.4f within neutral band (±%.2f); using config defaults.",
                len(pnls),
                avg,
                band,
            )
            return

        step_s = abs(self.config.online_training_step_score)
        step_m = abs(self.config.online_training_step_momentum)
        if avg > 0.0:
            new_score = self.config.entry_score_threshold - step_s
            new_mom = self.config.min_momentum_return - step_m
        else:
            new_score = self.config.entry_score_threshold + step_s
            new_mom = self.config.min_momentum_return + step_m
        new_score = _clamp(
            new_score,
            self.config.offline_training_min_entry_score_threshold,
            self.config.offline_training_max_entry_score_threshold,
        )
        new_mom = _clamp(
            new_mom,
            self.config.offline_training_min_min_momentum_return,
            self.config.offline_training_max_min_momentum_return,
        )
        self.state.dynamic_entry_score_threshold = new_score
        self.state.dynamic_min_momentum_return = new_mom
        self.logger.info(
            "Online training: window=%d mean=%.4f -> score_th=%.6f mom_th=%.6f",
            len(pnls),
            avg,
            new_score,
            new_mom,
        )

    def _symbol_dual_ma_uptrend(self, bars_df: pd.DataFrame, sym: str) -> bool:
        return dual_ma_uptrend(bars_df, sym, self.config)

    def _candlestick_entry_ok(self, bars_df: pd.DataFrame, sym: str) -> bool:
        if not self.config.enable_candlestick_entry_filter:
            return True
        return candlestick_bullish_strength(bars_df, sym, self.config) > 0.0

    def _candlestick_bullish_strength(self, bars_df: pd.DataFrame, sym: str) -> float:
        return candlestick_bullish_strength(bars_df, sym, self.config)

    def _candlestick_exit_triggered(self, symbol: str, bars_df: Optional[pd.DataFrame] = None) -> bool:
        if not self.config.enable_candlestick_exit:
            return False
        if bars_df is not None:
            try:
                df_sym = bars_df.xs(symbol, level=0).sort_index()
            except Exception:
                return False
        else:
            now_et = self._et_now()
            end = now_et.astimezone(ZoneInfo("UTC"))
            start = end - timedelta(minutes=20)
            try:
                df = self.market.get_recent_bars([symbol], start, end)
            except Exception:
                return False
            if df is None or df.empty:
                return False
            try:
                df_sym = df.xs(symbol, level=0).sort_index()
            except Exception:
                return False
        if len(df_sym) < 2:
            return False
        prev_bar = df_sym.iloc[-2]
        cur_bar = df_sym.iloc[-1]
        return bearish_candlestick_exit(prev_bar, cur_bar, wick_ratio=float(self.config.candlestick_wick_ratio))

    def _structure_exits_unlocked(self, entry_filled_at: Optional[datetime]) -> bool:
        """Optional delay before evaluating trend/candle exits (0 = immediate)."""
        mins = int(self.config.min_hold_minutes_before_structure_exit)
        if mins <= 0:
            return True
        if entry_filled_at is None:
            # Unknown fill time (e.g. old state): do not block forever; allow after min_hold from "now"
            # is wrong — caller should bootstrap entry_filled_at. Treat as not yet unlocked.
            return False
        unlock_at = entry_filled_at + timedelta(minutes=mins)
        return self._utc_now() >= unlock_at

    def _trend_break_triggered(self, symbol: str, bars_df: Optional[pd.DataFrame] = None) -> bool:
        if not self.config.enable_trend_break_exit:
            return False
        ma_n = int(self.config.trend_break_ma_minutes)
        confirm_n = max(1, int(self.config.trend_break_confirm_bars))
        if bars_df is not None:
            try:
                df_sym = bars_df.xs(symbol, level=0).sort_index()
            except Exception:
                return False
        else:
            lookback = max(ma_n + confirm_n + 6, 25)
            now_et = self._et_now()
            end = now_et.astimezone(ZoneInfo("UTC"))
            start = end - timedelta(minutes=lookback)
            try:
                df = self.market.get_recent_bars([symbol], start, end)
            except Exception:
                return False
            if df is None or df.empty:
                return False
            try:
                df_sym = df.xs(symbol, level=0).sort_index()
            except Exception:
                return False
        return trend_break_confirmed_below_sma(df_sym, ma_n, confirm_n)

    def _leg_entry_avg(self, leg: dict, broker_avg: float) -> float:
        try:
            v = leg.get("entry_avg_price")
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
        return float(broker_avg)

    def _leg_filled_at_dt(self, leg: dict) -> Optional[datetime]:
        raw = leg.get("entry_filled_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    def _maybe_update_trailing_stop(
        self,
        symbol: str,
        leg: dict,
        pos_qty: float,
        broker_avg: float,
        loop_quotes: Optional[dict] = None,
    ) -> None:
        if not self.config.enable_trailing_stop:
            return
        stop_id = leg.get("stop_order_id")
        if not stop_id or pos_qty <= 0:
            return
        entry_avg = self._leg_entry_avg(leg, broker_avg)
        if entry_avg <= 0:
            return
        try:
            if loop_quotes is not None:
                quotes = loop_quotes
            else:
                quotes = self.market.get_latest_quotes([symbol])
            q = quotes.get(symbol)
            if not q:
                return
            mid = float(q.mid)
            peak = leg.get("peak_price_since_entry")
            try:
                peak_f = float(peak) if peak is not None else entry_avg
            except (TypeError, ValueError):
                peak_f = entry_avg
            peak_f = max(peak_f, mid)
            leg["peak_price_since_entry"] = peak_f

            initial_stop = self._initial_stop_price(entry_avg)
            trail_stop = peak_f * (1.0 - self.config.trailing_stop_pct)
            target_stop = max(initial_stop, trail_stop)

            order = self._get_order(str(stop_id))
            if not order:
                return
            cur_raw = order.get("stop_price")
            if cur_raw in (None, ""):
                cur_raw = order.get("stop_limit_price")
            if cur_raw in (None, ""):
                return
            cur_stop = float(cur_raw)
            min_move = cur_stop * self.config.trailing_stop_min_move_pct
            if target_stop <= cur_stop + min_move:
                return

            target_q = float(self._quantize_stop_price(target_stop))
            if target_q <= cur_stop + min_move:
                return

            self.logger.info(
                "Trailing stop %s: raising stop from %.4f to %.4f (peak=%.4f)",
                symbol,
                cur_stop,
                target_q,
                peak_f,
            )
            self._cancel_order_safely(str(stop_id))
            time.sleep(2)
            pos_qty_now, avg_now = self._position_for_symbol(symbol)
            if pos_qty_now <= 0:
                return
            new_id = self._place_stop_sell_at_price(symbol, pos_qty_now, target_q)
            leg["stop_order_id"] = new_id
            self.state_store.save(self.state)
        except Exception as e:
            self.logger.warning("Trailing stop update skipped: %s", e)

    def _place_limit_buy_and_mark_pending(
        self, symbol: str, qty: float, limit_price: float
    ) -> str:
        client_order_id = f"entry_{symbol}_{int(time.time())}"
        limit_price_q = self._quantize_limit_price(limit_price)
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            # Send as string to avoid float representation issues.
            "limit_price": str(limit_price_q),
            "client_order_id": client_order_id,
        }
        order = self.trading.submit_order(payload)
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError(f"Entry order did not return id: {order}")
        return str(order_id)

    def _quantize_limit_price(self, price: float) -> Decimal:
        # For limit BUY, quantize UP to the nearest tick to avoid being rejected
        # and to avoid placing a limit that is slightly below the intended level.
        return self._quantize_price(price=price, tick=Decimal("0.01"), mode=ROUND_UP)

    def _quantize_stop_price(self, price: float) -> Decimal:
        # For stop SELL, quantize DOWN so the stop stays at or below the computed level.
        return self._quantize_price(price=price, tick=Decimal("0.01"), mode=ROUND_DOWN)

    def _quantize_price(self, price: float, tick: Decimal, mode) -> Decimal:
        if price <= 0:
            return Decimal("0")
        q = (Decimal(str(price)) / tick).to_integral_value(rounding=mode) * tick
        # Ensure fixed decimals for $0.01 tick.
        return q.quantize(tick)

    def _place_market_sell(self, symbol: str, qty: float) -> str:
        client_order_id = f"exit_{symbol}_{int(time.time())}"
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        order = self.trading.submit_order(payload)
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError(f"Exit order did not return id: {order}")
        return str(order_id)

    def _cancel_order_safely(self, order_id: Optional[str]) -> None:
        if not order_id:
            return
        try:
            self.trading.cancel_order(order_id)
        except Exception as e:
            self.logger.warning("Cancel failed (non-fatal): %s", e)

    def _get_order(self, order_id: Optional[str]) -> Optional[dict]:
        if not order_id:
            return None
        try:
            return self.trading.get_order(order_id)
        except Exception as e:
            self.logger.warning("Order fetch failed for %s: %s", order_id, e)
            return None

    def _order_is_filled(self, order: dict) -> bool:
        return str(order.get("status", "")).lower() == "filled"

    def _order_is_cancelled_or_rejected(self, order: dict) -> bool:
        st = str(order.get("status", "")).lower()
        return st in {"canceled", "rejected", "expired"}

    def _order_filled_qty_avg(self, order: dict) -> Tuple[float, Optional[float]]:
        filled_qty = order.get("filled_qty")
        filled_avg_price = order.get("filled_avg_price")
        try:
            fq = float(filled_qty or 0.0)
        except (TypeError, ValueError):
            fq = 0.0
        try:
            ap = None if filled_avg_price in (None, "") else float(filled_avg_price)
        except (TypeError, ValueError):
            ap = None
        return fq, ap

    def _compute_realized_pnl_from_exit(self, exit_order: dict, entry_avg_price: float) -> float:
        if entry_avg_price <= 0:
            return 0.0
        fq, filled_avg_price = self._order_filled_qty_avg(exit_order)
        if fq <= 0 or filled_avg_price is None:
            return 0.0
        pnl = (filled_avg_price - entry_avg_price) * fq
        pnl -= self.config.fee_estimate_per_order
        return float(pnl)

    def _fetch_scoring_snapshot(self) -> Optional[pd.DataFrame]:
        now_et = self._et_now()
        end = now_et.astimezone(ZoneInfo("UTC"))
        start = end - timedelta(minutes=self.config.bars_lookback_minutes_for_scoring)
        try:
            df = self.market.get_recent_bars(self.config.symbols_universe, start, end)
        except Exception as e:
            msg = str(e).lower()
            if "401" in msg:
                self._data_auth_fail_count += 1
                if self._data_auth_fail_count >= _DATA_AUTH_FAIL_LIMIT:
                    self.logger.error(
                        "Market data auth failed 401 three times in a row. "
                        "Fix Alpaca Data access/subscription. Halting new entries. Error=%s",
                        str(e),
                    )
                    self.state.halt_new_entries = True
                    self.state_store.save(self.state)
                else:
                    self.logger.warning(
                        "Market data auth 401 (%d/%d). Will retry next loop. Error=%s",
                        self._data_auth_fail_count,
                        _DATA_AUTH_FAIL_LIMIT,
                        str(e),
                    )
                return None
            self.logger.warning("Failed to fetch scoring bars: %s", e)
            return None
        if df is None or df.empty:
            return None
        # Successful fetch — reset the consecutive-failure counter.
        self._data_auth_fail_count = 0
        return df

    def _score_symbols(self, bars_df: pd.DataFrame) -> List[Tuple[str, float]]:
        """
        Returns ranked candidate symbols: [(symbol, score), ...] (highest score first).
        """
        scores: List[Tuple[str, float]] = []

        for sym in self.config.symbols_universe:
            try:
                df_sym = bars_df.xs(sym, level=0).sort_index()
            except Exception:
                continue
            if len(df_sym) < max(self.config.momentum_lookback_minutes, self.config.volume_lookback_minutes) + 5:
                continue

            latest_ts = df_sym.index.max()
            latest_close = float(df_sym["close"].iloc[-1])
            latest_volume = float(df_sym["volume"].iloc[-1])

            target_time = latest_ts - pd.Timedelta(minutes=self.config.momentum_lookback_minutes)
            past_slice = df_sym[df_sym.index <= target_time]
            if past_slice.empty:
                continue
            close_past = float(past_slice["close"].iloc[-1])
            if close_past <= 0:
                continue

            momentum_return = (latest_close / close_past) - 1.0

            vol_window = df_sym["volume"].iloc[-self.config.volume_lookback_minutes:]
            avg_vol = float(vol_window.mean()) if not vol_window.empty else 0.0
            if avg_vol <= 0:
                continue

            if avg_vol < self.config.min_avg_volume:
                continue

            volume_ratio = latest_volume / avg_vol
            # Deterministic, bounded scoring
            capped_vol_ratio = min(volume_ratio, 5.0)
            score = float(momentum_return * math.log1p(capped_vol_ratio))
            scores.append((sym, score))

        # Highest score first
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    def _diagnose_empty_score_rank(self, bars_df: pd.DataFrame) -> str:
        """Why _score_symbols produced no (symbol, score) pairs."""
        need = max(self.config.momentum_lookback_minutes, self.config.volume_lookback_minutes) + 5
        thin_bars = 0
        low_avg_vol = 0
        bad_past = 0
        no_frame = 0
        for sym in self.config.symbols_universe:
            try:
                df_sym = bars_df.xs(sym, level=0).sort_index()
            except Exception:
                no_frame += 1
                continue
            if len(df_sym) < need:
                thin_bars += 1
                continue
            latest_ts = df_sym.index.max()
            target_time = latest_ts - pd.Timedelta(minutes=self.config.momentum_lookback_minutes)
            past_slice = df_sym[df_sym.index <= target_time]
            if past_slice.empty:
                bad_past += 1
                continue
            close_past = float(past_slice["close"].iloc[-1])
            if close_past <= 0:
                bad_past += 1
                continue
            vol_window = df_sym["volume"].iloc[-self.config.volume_lookback_minutes :]
            avg_vol = float(vol_window.mean()) if not vol_window.empty else 0.0
            if avg_vol <= 0:
                bad_past += 1
                continue
            if avg_vol < self.config.min_avg_volume:
                low_avg_vol += 1
                continue
        return (
            f"no_symbols_scored thin_bars<{need}={thin_bars} "
            f"avg_vol<{self.config.min_avg_volume}={low_avg_vol} "
            f"bad_past_or_vol={bad_past} no_bars_frame={no_frame}"
        )

    def _diagnose_why_no_buy(
        self,
        held_syms: Set[str],
        loop_quotes: Optional[dict] = None,
        bars_df: Optional[pd.DataFrame] = None,
    ) -> str:
        """
        Human-readable explanation mirroring _choose_entry_candidate gates (for INFO logs).
        """
        if bars_df is None:
            bars_df = self._fetch_scoring_snapshot()
        if bars_df is None:
            return "scoring_bars_unavailable(check_market_data_or_halt)"
        ranked = self._score_symbols(bars_df)
        adj = self.config.symbol_score_adjustments
        ranked.sort(key=lambda x: x[1] + float(adj.get(x[0], 0.0)), reverse=True)
        if not ranked:
            return self._diagnose_empty_score_rank(bars_df)

        dynamic_score_threshold = (
            self.state.dynamic_entry_score_threshold
            if self.state.dynamic_entry_score_threshold is not None
            else self.config.entry_score_threshold
        )
        dynamic_min_momentum_return = (
            self.state.dynamic_min_momentum_return
            if self.state.dynamic_min_momentum_return is not None
            else self.config.min_momentum_return
        )

        if loop_quotes is not None:
            quotes = loop_quotes
        else:
            quotes = self.market.get_latest_quotes(self.config.symbols_universe)
        held = {s.upper() for s in held_syms}
        pieces: List[str] = []

        spy_blocks = False
        if self.config.enable_spy_market_trend_filter and "SPY" in self.config.symbols_universe:
            if not self._symbol_dual_ma_uptrend(bars_df, "SPY"):
                spy_blocks = True
                pieces.append("SPY_market_filter_not_uptrend(blocks_all)")

        shown = 0
        for best_symbol, raw_score in ranked:
            if shown >= 4:
                break
            bs = best_symbol.upper()
            if bs in held:
                pieces.append(f"{bs}:held")
                shown += 1
                continue
            if not self._symbol_entry_cooldown_elapsed(bs):
                until = self.state.symbol_next_entry_ok_after.get(bs, "?")
                pieces.append(f"{bs}:cooldown_until={until}")
                shown += 1
                continue
            q = quotes.get(best_symbol)
            if q is None:
                pieces.append(f"{bs}:no_quote")
                shown += 1
                continue
            if q.spread_pct > self.config.max_spread_pct:
                pieces.append(
                    f"{bs}:spread={q.spread_pct:.5f}>{self.config.max_spread_pct:.5f}"
                )
                shown += 1
                continue

            df_sym = bars_df.xs(best_symbol, level=0).sort_index()
            latest_close = float(df_sym["close"].iloc[-1])
            latest_ts = df_sym.index.max()
            target_time = latest_ts - pd.Timedelta(minutes=self.config.momentum_lookback_minutes)
            past_slice = df_sym[df_sym.index <= target_time]
            if past_slice.empty:
                pieces.append(f"{bs}:no_past_slice")
                shown += 1
                continue
            close_past = float(past_slice["close"].iloc[-1])
            momentum_return = (latest_close / close_past) - 1.0 if close_past > 0 else float("-inf")
            if momentum_return < dynamic_min_momentum_return:
                pieces.append(
                    f"{bs}:mom={momentum_return:.6f}<{dynamic_min_momentum_return:.6f}"
                )
                shown += 1
                continue

            if spy_blocks:
                pieces.append(f"{bs}:blocked_by_SPY_market_filter")
                shown += 1
                continue

            if not self._symbol_dual_ma_uptrend(bars_df, best_symbol):
                pieces.append(f"{bs}:dual_ma_not_uptrend")
                shown += 1
                continue

            candle_strength = self._candlestick_bullish_strength(bars_df, best_symbol)
            if self.config.enable_candlestick_entry_filter and candle_strength <= 0.0:
                pieces.append(f"{bs}:candlestick_gate_fail")
                shown += 1
                continue

            score_with_candle = raw_score + (
                float(self.config.candlestick_score_bonus) * candle_strength
            )
            sym_adj = float(adj.get(best_symbol, 0.0))
            final_score = score_with_candle + sym_adj
            if final_score < dynamic_score_threshold:
                pieces.append(
                    f"{bs}:score={final_score:.6f}<{dynamic_score_threshold:.6f}"
                    f"(raw={raw_score:.6f})"
                )
                shown += 1
                continue

            pieces.append(f"{bs}:passed_filters(unexpected)")
            shown += 1

        return " ".join(pieces)

    def _maybe_log_entry_diagnosis(
        self,
        *,
        pre_scan_reason: Optional[str] = None,
        held_syms: Optional[Set[str]] = None,
        loop_quotes: Optional[dict] = None,
        bars_df: Optional[pd.DataFrame] = None,
    ) -> None:
        iv = int(self.config.entry_diagnostic_interval_sec)
        if iv <= 0:
            return
        now = time.time()
        if now - self._last_entry_diagnostic_ts < float(iv):
            return
        self._last_entry_diagnostic_ts = now

        if held_syms is None:
            held_syms = {r[0] for r in self._list_universe_positions()}

        try:
            acct = self.trading.get_account()
            cash = float(acct.get("cash") or 0.0)
        except Exception:
            cash = float("nan")
        inv = self._total_universe_market_value()
        cap = float(self.config.max_portfolio_notional_usd)
        room = max(0.0, cap - inv)

        ds = self.state.dynamic_entry_score_threshold
        dm = self.state.dynamic_min_momentum_return
        score_th = ds if ds is not None else self.config.entry_score_threshold
        mom_th = dm if dm is not None else self.config.min_momentum_return

        head = (
            f"Entry diagnostic: state={self.state.state} halt_new_entries={self.state.halt_new_entries} "
            f"held={sorted(held_syms)} portfolio≈${inv:.0f}/${cap:.0f} room≈${room:.0f} cash≈${cash:.2f} "
            f"thresholds score>={score_th:.6f} mom>={mom_th:.6f} max_spread={self.config.max_spread_pct}"
        )

        skip_scan = pre_scan_reason and (
            "ENTRY_PENDING" in pre_scan_reason or "EXIT_PENDING" in pre_scan_reason
        )
        if skip_scan:
            self.logger.info("%s | %s", head, pre_scan_reason)
            return

        scan = self._diagnose_why_no_buy(held_syms, loop_quotes=loop_quotes, bars_df=bars_df)
        if pre_scan_reason:
            self.logger.info("%s | Blocker: %s | Detail: %s", head, pre_scan_reason, scan)
        else:
            self.logger.info("%s | Detail: %s", head, scan)

    def _choose_entry_candidate(
        self,
        held_symbols: Optional[Set[str]] = None,
        loop_quotes: Optional[dict] = None,
        bars_df: Optional[pd.DataFrame] = None,
    ) -> Tuple[Optional[str], float, Optional[float]]:
        """
        Returns (symbol, score, spread_pct).
        Spread is checked using latest quotes for chosen symbol(s).
        Skips symbols already held and symbols still in per-symbol entry cooldown.
        """
        if bars_df is None:
            bars_df = self._fetch_scoring_snapshot()
        if bars_df is None:
            return None, -np.inf, None

        ranked = self._score_symbols(bars_df)
        if not ranked:
            return None, -np.inf, None

        # Re-rank by raw momentum score + per-symbol adjustment (from paper-trade log performance).
        adj = self.config.symbol_score_adjustments
        ranked.sort(key=lambda x: x[1] + float(adj.get(x[0], 0.0)), reverse=True)

        if loop_quotes is not None:
            quotes = loop_quotes
        else:
            quotes = self.market.get_latest_quotes(self.config.symbols_universe)

        dynamic_score_threshold = (
            self.state.dynamic_entry_score_threshold
            if self.state.dynamic_entry_score_threshold is not None
            else self.config.entry_score_threshold
        )
        dynamic_min_momentum_return = (
            self.state.dynamic_min_momentum_return
            if self.state.dynamic_min_momentum_return is not None
            else self.config.min_momentum_return
        )

        held: Set[str] = {s.upper() for s in (held_symbols or set())}

        # Final strict entry filters (spread + momentum + trend),
        # applied in score order. This prevents "top score fails trend -> no trade"
        # behavior.
        for best_symbol, raw_score in ranked:
            bs = best_symbol.upper()
            if bs in held:
                continue
            if not self._symbol_entry_cooldown_elapsed(bs):
                continue
            q = quotes.get(best_symbol)
            if q is None:
                continue
            spread_pct = q.spread_pct

            if spread_pct > self.config.max_spread_pct:
                continue

            # Recompute momentum_return for this candidate.
            df_sym = bars_df.xs(best_symbol, level=0).sort_index()
            latest_close = float(df_sym["close"].iloc[-1])
            latest_ts = df_sym.index.max()
            target_time = latest_ts - pd.Timedelta(minutes=self.config.momentum_lookback_minutes)
            past_slice = df_sym[df_sym.index <= target_time]
            if past_slice.empty:
                continue
            close_past = float(past_slice["close"].iloc[-1])
            momentum_return = (latest_close / close_past) - 1.0 if close_past > 0 else -np.inf
            if momentum_return < dynamic_min_momentum_return:
                continue

            # Trend: optional SPY market filter + candidate must align with trend gate.
            if self.config.enable_spy_market_trend_filter and "SPY" in self.config.symbols_universe:
                if not self._symbol_dual_ma_uptrend(bars_df, "SPY"):
                    continue
            if not self._symbol_dual_ma_uptrend(bars_df, best_symbol):
                continue

            candle_strength = self._candlestick_bullish_strength(bars_df, best_symbol)
            if self.config.enable_candlestick_entry_filter and candle_strength <= 0.0:
                continue

            # Soft integration: candlestick quality can improve ranking confidence
            # without forcing every trade to depend on a rare pattern.
            score_with_candle = raw_score + (float(self.config.candlestick_score_bonus) * candle_strength)
            sym_adj = float(adj.get(best_symbol, 0.0))
            final_score = score_with_candle + sym_adj
            if final_score < dynamic_score_threshold:
                continue

            return best_symbol, final_score, spread_pct

        return None, -np.inf, None

    def _compute_qty_for_entry(self, entry_price: float) -> float:
        kill_threshold = self.config.max_daily_realized_loss
        daily_pnl = self.state.daily_realized_pnl

        if daily_pnl <= kill_threshold:
            return 0.0

        remaining_allowed_loss_mag = abs(kill_threshold - daily_pnl)
        risk_per_trade = min(self.config.max_risk_per_trade, remaining_allowed_loss_mag)
        stop_distance = entry_price * self.config.stop_loss_pct
        if stop_distance <= 0:
            return 0.0
        qty_by_risk = risk_per_trade / stop_distance

        account = self.trading.get_account()
        cash = float(account.get("cash") or 0.0)
        if cash <= 0:
            return 0.0

        qty_by_cash = cash / entry_price

        cap = float(self.config.max_portfolio_notional_usd)
        invested = self._total_universe_market_value()
        room_usd = max(0.0, cap - invested)
        qty_by_portfolio_cap = (room_usd * 0.98) / entry_price if entry_price > 0 else 0.0

        qty = min(qty_by_risk, qty_by_cash * 0.98, qty_by_portfolio_cap)
        if qty <= 0:
            return 0.0
        return float(round(qty, 6))

    def _maybe_place_entry(
        self,
        loop_quotes: Optional[dict] = None,
        bars_df: Optional[pd.DataFrame] = None,
    ) -> None:
        if self.state.state == "ENTRY_PENDING":
            self._maybe_log_entry_diagnosis(
                pre_scan_reason="ENTRY_PENDING — waiting for limit buy to fill or timeout.",
            )
            return
        if self.state.state == "EXIT_PENDING":
            self._maybe_log_entry_diagnosis(
                pre_scan_reason="EXIT_PENDING — waiting for market sell to complete.",
            )
            return
        if self.state.halt_new_entries:
            self._maybe_log_entry_diagnosis(
                pre_scan_reason="halt_new_entries=True — new buys disabled (e.g. risk/data). Resume from dashboard if appropriate.",
            )
            return

        self._cancel_leftover_sell_orders_when_flat()

        held_rows = self._list_universe_positions()
        held_syms: Set[str] = {r[0] for r in held_rows}

        if self.state.last_entry_attempt_at is not None:
            elapsed = (self._utc_now() - self.state.last_entry_attempt_at).total_seconds()
            if elapsed < float(self.config.min_seconds_between_entry_orders):
                self._maybe_log_entry_diagnosis(
                    pre_scan_reason=(
                        f"min_seconds_between_entry_orders "
                        f"({elapsed:.0f}s < {self.config.min_seconds_between_entry_orders}s since last attempt)."
                    ),
                    held_syms=held_syms,
                )
                return

        if len(held_rows) >= int(self.config.max_open_positions):
            self._maybe_log_entry_diagnosis(
                pre_scan_reason=(
                    f"max_open_positions reached ({len(held_rows)}/{self.config.max_open_positions})."
                ),
                held_syms=held_syms,
            )
            return

        candidate, score, spread_pct = self._choose_entry_candidate(
            held_symbols=held_syms,
            loop_quotes=loop_quotes,
            bars_df=bars_df,
        )
        if not candidate:
            self._maybe_log_entry_diagnosis(
                held_syms=held_syms,
                loop_quotes=loop_quotes,
                bars_df=bars_df,
            )
            return

        # Use the pre-fetched quote if available; fall back to a fresh fetch if not.
        if loop_quotes is not None:
            q = loop_quotes.get(candidate)
        else:
            quotes = self.market.get_latest_quotes([candidate])
            q = quotes.get(candidate)
        if q is None:
            self._maybe_log_entry_diagnosis(
                pre_scan_reason=f"No quote for selected candidate {candidate}.",
                held_syms=held_syms,
            )
            return

        best_bid = q.bid
        limit_price = best_bid * (1.0 + self.config.entry_limit_offset_pct)
        if limit_price <= 0:
            self._maybe_log_entry_diagnosis(
                pre_scan_reason=f"Invalid limit_price for {candidate} (bid={best_bid}).",
                held_syms=held_syms,
            )
            return

        qty = self._compute_qty_for_entry(limit_price)
        if qty <= 0:
            self.logger.info("Computed qty too small; skipping entry.")
            self._maybe_log_entry_diagnosis(
                pre_scan_reason="Candidate passed filters but qty=0 (daily risk kill, cash, or portfolio notional cap).",
                held_syms=held_syms,
            )
            return

        order_id = self._place_limit_buy_and_mark_pending(candidate, qty=qty, limit_price=limit_price)
        self.state.state = "ENTRY_PENDING"
        self.state.entry_order_id = order_id
        self.state.entry_client_order_id = None
        self.state.entry_symbol = candidate
        self.state.entry_submitted_at = self._utc_now()
        self.state.entry_filled_qty = 0.0
        self.state.entry_avg_price = None
        self.state.entry_filled_at = None
        self.state.stop_order_id = None
        self.state.exit_order_id = None
        self.state.exit_submitted_at = None
        self.state.peak_price_since_entry = None
        self.state.pending_exit_realized_accumulator = 0.0

        self.state.last_entry_attempt_at = self._utc_now()

        self.state_store.save(self.state)

        self.logger.info(
            "Placed ENTRY limit buy: %s qty=%s limit=%.6f score=%.6f spread_pct=%.6f order_id=%s",
            candidate,
            qty,
            limit_price,
            score,
            spread_pct if spread_pct is not None else float("nan"),
            order_id,
        )

    def _cancel_leftover_sell_orders_when_flat(self) -> None:
        """
        Best-effort cleanup to prevent wash-trade rejections:
        if state says FLAT, cancel any open sell orders for our ETF universe.
        """
        try:
            if self._list_universe_positions():
                return

            universe = set(self.config.symbols_universe or [])
            open_orders = self.trading.get_open_orders()
            canceled_any = False
            for o in open_orders:
                sym = str(o.get("symbol", "")).upper()
                if sym not in universe:
                    continue
                side = str(o.get("side", "")).lower()
                if side != "sell":
                    continue
                oid = o.get("id")
                if not oid:
                    continue
                self.logger.info("FLAT cleanup: cancel leftover sell order id=%s symbol=%s", oid, sym)
                self._cancel_order_safely(str(oid))
                canceled_any = True

            # Allow broker to process cancels before next order placement.
            if canceled_any:
                time.sleep(2)
        except Exception as e:
            self.logger.warning("FLAT cleanup failed (non-fatal): %s", e)

    def _open_leg_after_buy_fill(self, sym: str, qty: float, avg: float) -> bool:
        """Place protective stop and record `position_legs` for a new fill. Returns False if stop failed."""
        sym_u = sym.upper()
        try:
            sp = self._initial_stop_price(float(avg))
            stop_id = self._place_stop_sell_at_price(
                symbol=sym_u,
                qty=float(qty),
                stop_price=sp,
            )
        except Exception as e:
            self.logger.error("Failed placing stop-loss (SAFE STOP). error=%s", e)
            self.state.halt_new_entries = True
            self.state_store.save(self.state)
            return False

        now = self._utc_now()
        self.state.position_legs[sym_u] = {
            "stop_order_id": stop_id,
            "peak_price_since_entry": float(avg),
            "entry_filled_at": now.isoformat(),
            "entry_avg_price": float(avg),
            "pending_exit_realized_accumulator": 0.0,
        }
        self.state.state = "IN_POSITION"
        self.state.entry_order_id = None
        self.state.entry_symbol = None
        self.state.stop_order_id = None
        self.state.exit_order_id = None
        self.state.entry_filled_qty = float(qty)
        self.state.entry_avg_price = float(avg)
        self.state.entry_filled_at = now
        self.state.peak_price_since_entry = float(avg)
        self.state_store.save(self.state)
        self.logger.info(
            "ENTRY filled: symbol=%s qty=%s avg_price=%.6f. Stop-loss placed id=%s",
            sym_u,
            qty,
            avg,
            stop_id,
        )
        return True

    def _handle_entry_pending(self) -> None:
        if self.state.state != "ENTRY_PENDING":
            return
        if not self.state.entry_order_id or not self.state.entry_symbol:
            self.state.state = "FLAT"
            self.state_store.save(self.state)
            return

        order = self._get_order(self.state.entry_order_id)
        if order is None:
            return

        status = str(order.get("status", "")).lower()
        fq, avg_price = self._order_filled_qty_avg(order)

        # Filled?
        if self._order_is_filled(order):
            if fq <= 0 or avg_price is None:
                self.logger.warning("Entry marked filled but qty/avg missing. order=%s", order)
                self.state = BotState()
                self.state_store.save(self.state)
                return

            sym = str(self.state.entry_symbol).upper()
            _ = self._open_leg_after_buy_fill(sym, float(fq), float(avg_price))
            return

        # Timeout / cancel if not filled quickly
        submitted_at = self.state.entry_submitted_at or self._utc_now()
        elapsed = (self._utc_now() - submitted_at).total_seconds()
        if elapsed >= self.config.entry_timeout_sec:
            self.logger.info("Entry timeout reached; canceling entry order=%s status=%s fq=%s", self.state.entry_order_id, status, fq)
            self._cancel_order_safely(self.state.entry_order_id)

            # Re-fetch after cancel to learn filled qty (could be partially filled).
            order2 = self._get_order(self.state.entry_order_id)
            if order2:
                fq2, avg2 = self._order_filled_qty_avg(order2)
                if fq2 > 0 and avg2 is not None:
                    sym = str(self.state.entry_symbol).upper()
                    ok = self._open_leg_after_buy_fill(sym, float(fq2), float(avg2))
                    if ok:
                        self.logger.info("Entry partially filled on cancel; qty=%s avg=%.6f", fq2, avg2)
                else:
                    self.logger.info("Entry had no filled qty on cancel.")
                    self.state.entry_order_id = None
                    self.state.entry_symbol = None
                    self._normalize_aggregate_state()
                    self.state_store.save(self.state)
            else:
                self.state.entry_order_id = None
                self.state.entry_symbol = None
                self._normalize_aggregate_state()
                self.state_store.save(self.state)
            return

        # If canceled/rejected earlier, clear back to FLAT (or partial fill fallback if we have qty)
        if self._order_is_cancelled_or_rejected(order):
            if fq > 0 and avg_price is not None:
                sym = str(self.state.entry_symbol).upper()
                ok = self._open_leg_after_buy_fill(sym, float(fq), float(avg_price))
                if ok:
                    self.logger.info(
                        "Entry cancelled/rejected but partially filled: qty=%s avg=%.6f",
                        fq,
                        avg_price,
                    )
            else:
                self.state.entry_order_id = None
                self.state.entry_symbol = None
                self._normalize_aggregate_state()
                self.state_store.save(self.state)
            return

    def _queue_or_execute_market_exit(self, sym: str, pos_qty: float) -> None:
        sym_u = sym.upper()
        if self.state.state == "EXIT_PENDING":
            if sym_u not in self.state.pending_exit_symbols:
                self.state.pending_exit_symbols.append(sym_u)
            return
        self._execute_market_exit(sym_u, pos_qty)

    def _drain_pending_market_exit(self) -> None:
        if self.state.state == "EXIT_PENDING":
            return
        while self.state.pending_exit_symbols:
            sym_u = self.state.pending_exit_symbols.pop(0)
            pq, _ = self._position_for_symbol(sym_u)
            if pq > 0:
                self._execute_market_exit(sym_u, pq)
                return

    def _scrub_top_level_if_flat(self) -> None:
        if self.state.state != "FLAT":
            return
        self.state.entry_order_id = None
        self.state.entry_symbol = None
        self.state.stop_order_id = None
        self.state.exit_order_id = None
        self.state.exit_pending_symbol = None
        self.state.pending_exit_symbols = []
        self.state.entry_filled_qty = 0.0
        self.state.entry_avg_price = None
        self.state.entry_filled_at = None
        self.state.peak_price_since_entry = None
        self.state.pending_exit_realized_accumulator = 0.0

    def _after_stop_or_exit_closed_leg(self, sym: str, trade_total: float) -> None:
        self._online_nudge_from_trade(trade_total)
        self._record_exit_for_cooldown(sym, trade_total)
        self.state.position_legs.pop(sym.upper(), None)
        self._normalize_aggregate_state()
        self._scrub_top_level_if_flat()

    def _manage_one_symbol(
        self,
        sym: str,
        broker_qty: float,
        broker_avg: float,
        loop_quotes: Optional[dict] = None,
        bars_df: Optional[pd.DataFrame] = None,
    ) -> None:
        if self.state.state == "EXIT_PENDING" and self.state.exit_pending_symbol == sym:
            return

        leg = self.state.position_legs.setdefault(
            sym,
            {
                "stop_order_id": None,
                "peak_price_since_entry": float(broker_avg),
                "entry_filled_at": None,
                "entry_avg_price": float(broker_avg),
                "pending_exit_realized_accumulator": 0.0,
            },
        )
        entry_avg = self._leg_entry_avg(leg, broker_avg)
        leg["entry_avg_price"] = entry_avg
        # Needed for structure/time exits; broker-only legs had None and never unlocked exits.
        if leg.get("entry_filled_at") is None and broker_qty > 0:
            leg["entry_filled_at"] = self._utc_now().isoformat()
            self.state_store.save(self.state)
        entry_filled_at = self._leg_filled_at_dt(leg)

        stop_ok = bool(leg.get("stop_order_id"))
        if not stop_ok:
            # Check broker first — an active stop may already exist from a prior loop iteration
            # that completed the API call but crashed before writing state.json.
            existing_stop_id = self._find_active_stop_order(sym)
            if existing_stop_id:
                self.logger.info(
                    "Stop-loss already active on broker (idempotency check). %s stop_id=%s",
                    sym,
                    existing_stop_id,
                )
                leg["stop_order_id"] = existing_stop_id
                stop_ok = True
                if leg.get("peak_price_since_entry") is None:
                    leg["peak_price_since_entry"] = entry_avg
                self.state_store.save(self.state)
            else:
                try:
                    sp = self._initial_stop_price(entry_avg)
                    stop_id = self._place_stop_sell_at_price(symbol=sym, qty=broker_qty, stop_price=sp)
                    leg["stop_order_id"] = stop_id
                    stop_ok = True
                    if leg.get("peak_price_since_entry") is None:
                        leg["peak_price_since_entry"] = entry_avg
                    self.state_store.save(self.state)
                    self.logger.info("Stop-loss placed during reconcile. %s stop_id=%s", sym, stop_id)
                except Exception as e:
                    self.logger.error(
                        "Failed to place stop-loss (halt new entries; will still try TP/EOD exit): %s",
                        e,
                    )
                    self.state.halt_new_entries = True
                    self.state_store.save(self.state)

        if stop_ok:
            self._maybe_update_trailing_stop(sym, leg, broker_qty, broker_avg, loop_quotes=loop_quotes)

        stop_id = leg.get("stop_order_id")
        if stop_ok and stop_id:
            stop_order = self._get_order(str(stop_id))
            if stop_order and self._order_is_filled(stop_order):
                realized = self._compute_realized_pnl_from_exit(stop_order, entry_avg)
                self.state.daily_realized_pnl += realized
                if self.state.daily_realized_pnl <= self.config.max_daily_realized_loss:
                    self.state.halt_new_entries = True
                self.logger.info(
                    "STOP filled %s. exit_pnl=%.2f daily_realized_pnl=%.2f halt_new_entries=%s",
                    sym,
                    realized,
                    self.state.daily_realized_pnl,
                    self.state.halt_new_entries,
                )
                self._after_stop_or_exit_closed_leg(sym, realized)
                self.state_store.save(self.state)
                return

        if self.config.enable_take_profit and entry_avg > 0:
            try:
                if loop_quotes is not None:
                    q = loop_quotes.get(sym)
                else:
                    quotes = self.market.get_latest_quotes([sym])
                    q = quotes.get(sym)
                if q and float(q.bid) >= float(entry_avg) * (1.0 + self.config.take_profit_pct):
                    self.logger.info("Take-profit triggered; exiting %s with market sell.", sym)
                    self._queue_or_execute_market_exit(sym, broker_qty)
                    return
            except Exception:
                pass

        if self._structure_exits_unlocked(entry_filled_at):
            if self._trend_break_triggered(sym, bars_df=bars_df):
                if self._structure_exit_skipped_because_profitable(sym, entry_avg, loop_quotes=loop_quotes):
                    self.logger.debug("Trend-break ignored (still profitable vs entry): %s", sym)
                else:
                    self.logger.info("Trend-break exit; market sell %s", sym)
                    self._queue_or_execute_market_exit(sym, broker_qty)
                    return
            if self._candlestick_exit_triggered(sym, bars_df=bars_df):
                if self._structure_exit_skipped_because_profitable(sym, entry_avg, loop_quotes=loop_quotes):
                    self.logger.debug("Candlestick exit ignored (still profitable vs entry): %s", sym)
                else:
                    self.logger.info("Candlestick bearish-reversal exit; market sell %s", sym)
                    self._queue_or_execute_market_exit(sym, broker_qty)
                    return

        now_et = self._et_now()
        if self._should_time_stop(now_et, entry_filled_at):
            self.logger.info("Time stop reached; market sell %s", sym)
            self._queue_or_execute_market_exit(sym, broker_qty)
            return

        if self._should_end_of_day_exit(now_et):
            self.logger.info("End-of-day cutoff; flattening %s", sym)
            self._log_day_end_equity()
            self._queue_or_execute_market_exit(sym, broker_qty)

    def _manage_open_positions(
        self,
        loop_quotes: Optional[dict] = None,
        bars_df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Run stop / trail / exits for every open universe position.
        Must run even when `state` is ENTRY_PENDING (adding another leg) or EXIT_PENDING."""
        rows = self._list_universe_positions()
        if not rows:
            if self.state.state == "IN_POSITION":
                self.logger.info("IN_POSITION but no broker holdings; normalizing state.")
                self.state.position_legs.clear()
                self._normalize_aggregate_state()
                self.state_store.save(self.state)
            return
        for sym, qty, avg, _ in rows:
            self._manage_one_symbol(sym, qty, avg, loop_quotes=loop_quotes, bars_df=bars_df)

    def _should_time_stop(self, now_et: datetime, entry_filled_at: Optional[datetime]) -> bool:
        if not self.config.enable_time_stop:
            return False
        if entry_filled_at is None:
            return False
        target = entry_filled_at + timedelta(minutes=self.config.time_stop_minutes)
        return self._utc_now() >= target

    def _should_end_of_day_exit(self, now_et: datetime) -> bool:
        hh, mm = _parse_time_hhmm_to_et(self.config.market_close_time_et)
        close_dt = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
        cutoff = close_dt - timedelta(minutes=self.config.end_of_day_flat_minutes)
        return now_et >= cutoff

    def _execute_market_exit(self, symbol: str, pos_qty: float) -> None:
        """Cancel this symbol's protective stop, then market-sell remaining qty."""
        sym_u = symbol.upper()
        if pos_qty <= 0:
            return
        leg = self.state.position_legs.get(sym_u, {})
        stop_id = leg.get("stop_order_id")
        entry_avg = float(leg.get("entry_avg_price") or 0)

        self._cancel_order_safely(str(stop_id) if stop_id else None)
        time.sleep(2)

        if stop_id:
            stop_order_after = self._get_order(str(stop_id))
            if stop_order_after:
                filled_qty, filled_avg = self._order_filled_qty_avg(stop_order_after)
                if filled_qty > 0 and filled_avg is not None and entry_avg > 0:
                    realized_part = self._compute_realized_pnl_from_exit(stop_order_after, entry_avg)
                    self.state.daily_realized_pnl += realized_part
                    acc = float(leg.get("pending_exit_realized_accumulator") or 0)
                    leg["pending_exit_realized_accumulator"] = acc + realized_part
                    if self.state.daily_realized_pnl <= self.config.max_daily_realized_loss:
                        self.state.halt_new_entries = True
                    self.logger.info(
                        "Market exit %s: partial stop fill realized_part=%.2f daily_realized_pnl=%.2f",
                        sym_u,
                        realized_part,
                        self.state.daily_realized_pnl,
                    )

        leg["stop_order_id"] = None
        pos_qty_now, _ = self._position_for_symbol(sym_u)
        if pos_qty_now <= 0:
            self.logger.info("Market exit %s: position already flat after cancel.", sym_u)
            trade_total = float(leg.get("pending_exit_realized_accumulator") or 0)
            leg["pending_exit_realized_accumulator"] = 0.0
            self._after_stop_or_exit_closed_leg(sym_u, trade_total)
            self.state_store.save(self.state)
            self._drain_pending_market_exit()
            return

        exit_id = self._place_market_sell(sym_u, qty=pos_qty_now)
        self.state.exit_order_id = exit_id
        self.state.exit_submitted_at = self._utc_now()
        self.state.exit_pending_symbol = sym_u
        self.state.state = "EXIT_PENDING"
        self.state_store.save(self.state)

    def _handle_exit_pending(self) -> None:
        if self.state.state != "EXIT_PENDING":
            return
        if not self.state.exit_order_id or not self.state.exit_pending_symbol:
            self.state.exit_pending_symbol = None
            self.state.exit_order_id = None
            self._normalize_aggregate_state()
            self.state_store.save(self.state)
            self._drain_pending_market_exit()
            return

        sym_u = str(self.state.exit_pending_symbol).upper()
        leg = self.state.position_legs.get(sym_u, {})
        entry_avg = float(leg.get("entry_avg_price") or 0)

        exit_order = self._get_order(self.state.exit_order_id)
        if not exit_order:
            return

        if self._order_is_filled(exit_order):
            realized = self._compute_realized_pnl_from_exit(exit_order, entry_avg)
            self.state.daily_realized_pnl += realized
            if self.state.daily_realized_pnl <= self.config.max_daily_realized_loss:
                self.state.halt_new_entries = True

            self.logger.info(
                "EXIT filled %s. exit_pnl=%.2f daily_realized_pnl=%.2f halt_new_entries=%s",
                sym_u,
                realized,
                self.state.daily_realized_pnl,
                self.state.halt_new_entries,
            )
            acc = float(leg.get("pending_exit_realized_accumulator") or 0)
            trade_total = acc + realized
            leg["pending_exit_realized_accumulator"] = 0.0
            self.state.exit_order_id = None
            self.state.exit_submitted_at = None
            self.state.exit_pending_symbol = None
            self._after_stop_or_exit_closed_leg(sym_u, trade_total)
            self.state_store.save(self.state)
            self._drain_pending_market_exit()
            return

        if self._order_is_cancelled_or_rejected(exit_order):
            pos_qty, _ = self._position_for_symbol(sym_u)
            if pos_qty <= 0:
                trade_total = float(leg.get("pending_exit_realized_accumulator") or 0)
                leg["pending_exit_realized_accumulator"] = 0.0
                self.state.exit_order_id = None
                self.state.exit_pending_symbol = None
                self._after_stop_or_exit_closed_leg(sym_u, trade_total)
                self.state_store.save(self.state)
                self._drain_pending_market_exit()
                return
            self.logger.warning("Exit order canceled/rejected; retrying market exit once for %s.", sym_u)
            self._cancel_order_safely(self.state.exit_order_id)
            exit_id = self._place_market_sell(sym_u, qty=pos_qty)
            self.state.exit_order_id = exit_id
            self.state.exit_submitted_at = self._utc_now()
            self.state_store.save(self.state)

    def run_forever(self, max_runtime_sec: Optional[int] = None) -> None:
        deadline_ts = time.time() + float(max_runtime_sec) if max_runtime_sec else None
        self.logger.info("Bot starting. paper=%s universe=%s", self.config.paper, self.config.symbols_universe)
        self.logger.info("State loaded: state=%s daily_realized_pnl=%.2f halt_new_entries=%s", self.state.state, self.state.daily_realized_pnl, self.state.halt_new_entries)

        self._migrate_legacy_single_leg_if_needed()
        held = self._list_universe_positions()
        if held:
            self.state.state = "IN_POSITION"
            for pos_sym, pos_qty, pos_avg, _ in held:
                sym_u = str(pos_sym).upper()
                if sym_u not in self.state.position_legs:
                    self.state.position_legs[sym_u] = {
                        "stop_order_id": None,
                        "peak_price_since_entry": float(pos_avg),
                        "entry_filled_at": None,
                        "entry_avg_price": float(pos_avg),
                        "pending_exit_realized_accumulator": 0.0,
                    }
                leg = self.state.position_legs[sym_u]
                if not leg.get("stop_order_id"):
                    existing_stop_id = self._find_active_stop_order(sym_u)
                    if existing_stop_id:
                        leg["stop_order_id"] = existing_stop_id
                        self.logger.info(
                            "Startup reconcile: %s stop_order_id=%s",
                            sym_u,
                            existing_stop_id,
                        )
            self.state_store.save(self.state)

        while True:
            try:
                if deadline_ts is not None and time.time() >= deadline_ts:
                    self.logger.info("Max runtime reached; flattening and stopping.")
                    self._flatten_and_stop()
                    return

                self._reset_daily_if_needed()

                if not self._is_market_open() or not self._is_regular_hours():
                    time.sleep(10)
                    continue

                # Hard stop if daily kill-switch is hit
                if self.state.daily_realized_pnl <= self.config.max_daily_realized_loss:
                    self.state.halt_new_entries = True

                if self.state.state == "ENTRY_PENDING":
                    self._handle_entry_pending()

                if self.state.state == "EXIT_PENDING":
                    self._handle_exit_pending()

                if self.state.state not in (
                    "ENTRY_PENDING",
                    "EXIT_PENDING",
                    "FLAT",
                    "IN_POSITION",
                ):
                    self.logger.error(
                        "Unknown bot state: %s. Resetting to FLAT.",
                        self.state.state,
                    )
                    self.state = self._flat_state_preserve_meta()
                    self.state_store.save(self.state)

                # Fetch quotes and bars once per loop — passed into all sub-methods
                # to avoid redundant API calls (was 4-6 separate fetches per cycle).
                loop_quotes: Optional[dict] = None
                loop_bars: Optional[pd.DataFrame] = None
                try:
                    loop_quotes = self.market.get_latest_quotes(self.config.symbols_universe)
                except Exception as e:
                    self.logger.warning("Loop quotes fetch failed: %s", e)
                loop_bars = self._fetch_scoring_snapshot()

                data_stale = self._market_stale_guard(loop_bars)

                if self.state.state in (
                    "FLAT",
                    "IN_POSITION",
                    "ENTRY_PENDING",
                    "EXIT_PENDING",
                ):
                    self._migrate_legacy_single_leg_if_needed()
                    self._sync_position_legs_with_broker()
                    self._normalize_aggregate_state()
                    # Always manage positions (stop/trail checks still use broker orders).
                    # Only skip signal-based exits when data is stale.
                    self._manage_open_positions(
                        loop_quotes=loop_quotes,
                        bars_df=None if data_stale else loop_bars,
                    )

                if self.state.state not in ("ENTRY_PENDING", "EXIT_PENDING") and not data_stale:
                    self._maybe_place_entry(loop_quotes=loop_quotes, bars_df=loop_bars)

                # Always persist occasionally (v1 simplicity).
                # StateStore save happens in transitions, but not on every loop.

                time.sleep(self.config.loop_interval_sec)
            except KeyboardInterrupt:
                self.logger.warning("KeyboardInterrupt received; saving state and exiting.")
                self.state_store.save(self.state)
                return
            except Exception as e:
                self.logger.exception("Unhandled error in main loop: %s", e)
                # Fail-safe: wait a bit before retrying.
                time.sleep(10)

    def _log_day_end_equity(self) -> None:
        """Fetch account equity and log the day-end summary."""
        try:
            acct = self.trading.get_account()
            equity = float(acct.get("equity") or acct.get("portfolio_value") or 0.0)
            cash = float(acct.get("cash") or 0.0)
            pnl = self.state.daily_realized_pnl
            start_eq = self.state.day_start_equity
            day_return_pct = ((equity - start_eq) / start_eq * 100.0) if start_eq > 0 else 0.0
            self.logger.info(
                "DAY_END equity=%.2f cash=%.2f daily_realized_pnl=%.2f day_return_pct=%.3f%%",
                equity,
                cash,
                pnl,
                day_return_pct,
            )
        except Exception as e:
            self.logger.warning("Could not fetch account equity for DAY_END snapshot: %s", e)

    def _flatten_and_stop(self) -> None:
        """
        Best-effort flatten to ensure the bot doesn't leave open risk when stopping due to runtime/automation.
        """
        self._log_day_end_equity()
        try:
            # Cancel open orders first (stop-losses, etc.)
            open_orders = self.trading.get_open_orders()
            for o in open_orders:
                oid = o.get("id")
                if oid:
                    self._cancel_order_safely(str(oid))

            time.sleep(1)

            # Re-check positions and market sell any remaining quantities.
            positions = self.trading.get_positions()
            for p in positions:
                sym = p.get("symbol")
                qty = float(p.get("qty", 0) or 0)
                if sym and qty > 0:
                    self.logger.info("Flatten: market sell %s qty=%s", sym, qty)
                    self._place_market_sell(str(sym), qty)
        except Exception as e:
            self.logger.warning("Flatten failed (non-fatal): %s", e)


def main() -> None:
    lock_path = os.getenv("BOT_LOCK_PATH", "bot.lock")
    # Simple single-instance lock to avoid multiple bots racing on `state.json`.
    # This prevents duplicate orders and inconsistent state.
    if os.path.exists(lock_path):
        raise SystemExit(f"Bot appears to be already running (lock file exists: {lock_path}).")

    with open(lock_path, "x", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    try:
        bot = TradeBot()
        max_runtime = os.getenv("BOT_MAX_RUNTIME_SEC")
        if max_runtime:
            bot.run_forever(max_runtime_sec=int(max_runtime))
        else:
            bot.run_forever()
    finally:
        # Best-effort cleanup of lock file.
        try:
            os.remove(lock_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()

