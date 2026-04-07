import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List
from zoneinfo import ZoneInfo


ET_TZ = ZoneInfo("America/New_York")


def resolve_bot_log_path() -> str:
    """
    Absolute path to the bot trade log.

    The file handler, offline/online log seeding, and dashboard parser must all
    use this path. Default is ``app/bot.log`` (next to this module), not the
    process working directory, so running ``run_bot.py`` from a parent folder
    does not write a separate log the dashboard never reads.
    """
    raw = os.getenv("BOT_LOG_PATH", "").strip().strip('"').strip("'")
    if raw:
        return os.path.abspath(raw)
    app_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dir, "bot.log")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    # .env files often use quoted values like PAPER="true".
    # Strip quotes so we interpret booleans correctly.
    v = v.strip().strip('"').strip("'").lower()
    return v in {"1", "true", "t", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().strip('"').strip("'")
    return float(v)


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().strip('"').strip("'")
    return int(v)


def _default_symbol_score_adjustments() -> Dict[str, float]:
    """
    Added to (momentum×volume) score before the entry threshold.
    From bot.log aggregates: SPY / IWM net negative; QQQ / VTI net positive — nudge selection accordingly.
    """
    return {
        "SPY": -0.0004,
        "IWM": -0.00025,
        "QQQ": 0.00006,
        "VTI": 0.00008,
        "DIA": 0.0,
    }


def _parse_symbol_score_adjustments() -> Dict[str, float]:
    """Env SYMBOL_SCORE_ADJUSTMENTS=SPY=-0.0005,IWM=0 overrides defaults (merge by symbol)."""
    base = _default_symbol_score_adjustments()
    raw = os.getenv("SYMBOL_SCORE_ADJUSTMENTS")
    if not raw:
        return base
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        base[k.strip().upper()] = float(v.strip())
    return base


@dataclass(frozen=True)
class BotConfig:
    # --- Alpaca ---
    api_key: str
    api_secret: str
    paper: bool

    # --- Runtime ---
    loop_interval_sec: int = 10

    # --- Session ---
    symbols_universe: List[str] = None  # type: ignore[assignment]
    market_open_time_et: str = "09:30"
    market_close_time_et: str = "16:00"
    enable_extended_hours: bool = False
    after_hours_end_et: str = "20:00"

    # --- Entry: momentum × volume score + filters (see strategy_signals.py) ---
    # Loosen defaults so we have enough bars for all tickers.
    momentum_lookback_minutes: int = 10
    volume_lookback_minutes: int = 10
    # Score = momentum_return * log1p(volume_ratio). Tighter defaults = fewer, pickier entries.
    entry_score_threshold: float = -0.0005
    min_momentum_return: float = -0.0005
    max_spread_pct: float = 0.001  # 0.10% — skip wide books
    min_avg_volume: float = 2000  # slightly more liquidity than bare minimum
    # Per-symbol score nudge (see _parse_symbol_score_adjustments).
    symbol_score_adjustments: Dict[str, float] = field(default_factory=_default_symbol_score_adjustments)

    # Trend filter (dual MA on 1m closes): long only when structure is supportive.
    enable_trend_filter: bool = True
    trend_ma_fast: int = 5
    trend_ma_slow: int = 15
    # How far below the fast MA the last close can be and still count as "uptrend".
    trend_ma_tolerance_pct: float = 0.001  # 0.10% below fast MA allowed
    # Require SPY (if in universe) to also be in uptrend before any buy.
    enable_spy_market_trend_filter: bool = True

    # Optional candlestick boost/gate (often too strict if enabled as hard gate).
    enable_candlestick_entry_filter: bool = False
    enable_candlestick_exit: bool = True
    require_candlestick_confirmation: bool = True
    candlestick_volume_confirm_mult: float = 1.2
    candlestick_score_bonus: float = 0.00035
    # Wick-to-body ratio required for hammer / shooting-star patterns.
    candlestick_wick_ratio: float = 2.0
    # Pattern close must be within this % of SMA5 to qualify.
    candlestick_sma_proximity_pct: float = 0.001

    # Opening range: skip the first N minutes after market open (volatile, noisy).
    market_open_delay_minutes: int = 15

    # Execution (entry)
    entry_limit_offset_pct: float = 0.0001  # 0.01% above best bid (approx)
    entry_timeout_sec: int = 20
    entry_cooldown_sec: int = 180
    # Extra seconds after a losing exit before the next entry (reduces revenge churn).
    post_loss_extra_cooldown_sec: int = 150
    # Min gap between *placing* new entry orders (any symbol); per-symbol cooldown is separate.
    min_seconds_between_entry_orders: int = 2
    max_entry_attempts_per_day: int = 3
    # INFO log every N seconds summarizing why no buy (0 = disable). Helps explain "silent" sessions.
    entry_diagnostic_interval_sec: int = 180

    # --- Risk & position ---
    max_open_positions: int = 2
    # Cap total long market value (sum of Alpaca position market_value for universe symbols).
    max_portfolio_notional_usd: float = 10_000.0
    stop_loss_pct: float = 0.006  # -0.6%
    # Optional safety cap (disabled by default; exits are stop / trail / trend-break / EOD).
    enable_time_stop: bool = False
    time_stop_minutes: int = 240
    end_of_day_flat_minutes: int = 5

    # Trailing stop: ratchet protective stop up as price makes new highs (never loosened).
    enable_trailing_stop: bool = True
    trailing_stop_pct: float = 0.004  # trail distance from peak (0.4%)
    # Min relative move before replacing the stop order (avoids spamming tiny updates).
    trailing_stop_min_move_pct: float = 0.0002  # 0.02% of current stop price

    # Exit when short-term trend breaks (close below SMA on 1m bars).
    enable_trend_break_exit: bool = True
    # Slightly slower SMA reduces whipsaws in chop (was 10).
    trend_break_ma_minutes: int = 15
    trend_break_confirm_bars: int = 3
    # Brief delay before trend/candle exits so new fills are not chopped immediately (log: high churn).
    min_hold_minutes_before_structure_exit: int = 2
    # If bid is above entry by at least trend_break_min_profit_pct_to_skip, skip structure exits
    # so take-profit / stop can run (trend-break was cutting many small winners in logs).
    trend_break_skip_if_profitable: bool = True
    trend_break_min_profit_pct_to_skip: float = 0.0

    # Take-profit (sell for gain) rule:
    # If price rises by this percent above entry during the holding window, the bot will sell early.
    enable_take_profit: bool = True
    take_profit_pct: float = 0.009  # +0.9% — 1.5:1 reward-to-risk vs 0.6% stop

    # Risk: your requested rules
    max_daily_realized_loss: float = -20.0  # realized P&L after exits only
    max_risk_per_trade: float = 5.0  # recommended for $100 account survivability

    # Offline "training" from recent paper results (bot.log).
    # This is a simple parameter nudging based on realized P&L (no ML).
    enable_offline_training: bool = True
    offline_training_lookback_trades: int = 15
    offline_training_step_score: float = 0.00025
    offline_training_step_momentum: float = 0.00025
    offline_training_max_entry_score_threshold: float = 0.005
    offline_training_min_entry_score_threshold: float = -0.005
    offline_training_max_min_momentum_return: float = 0.005
    offline_training_min_min_momentum_return: float = -0.01

    # Online training: rolling window of closed-trade P&amp;L; thresholds follow mean(window), not last trade.
    enable_online_training: bool = True
    online_training_window_trades: int = 20
    online_training_min_trades: int = 3
    # If abs(mean window) <= this ($), clear dynamic knobs (use config defaults).
    online_training_neutral_band_abs_usd: float = 0.05
    online_training_step_score: float = 0.00015
    online_training_step_momentum: float = 0.00015

    # Fees
    fee_estimate_per_order: float = 0.0  # set if you want kill-switch to include fees

    # State / logging
    state_path: str = "state.json"
    log_level: str = "INFO"

    # Data
    # Must cover trend_ma_slow + buffer for dual-MA filter.
    bars_lookback_minutes_for_scoring: int = 45
    stale_data_max_age_sec: int = 20

    def validate(self) -> None:
        """Raise ValueError on contradictory settings; warn on suspicious ones."""
        _log = logging.getLogger("trade_bot")

        if self.max_daily_realized_loss >= 0:
            raise ValueError(
                f"max_daily_realized_loss must be negative (got {self.max_daily_realized_loss}). "
                "Set e.g. MAX_DAILY_REALIZED_LOSS=-20"
            )

        if self.stop_loss_pct >= self.take_profit_pct:
            raise ValueError(
                f"stop_loss_pct ({self.stop_loss_pct}) >= take_profit_pct ({self.take_profit_pct}). "
                "The stop will always trigger before the take-profit — fix your risk/reward settings."
            )

        if self.momentum_lookback_minutes > self.bars_lookback_minutes_for_scoring:
            raise ValueError(
                f"momentum_lookback_minutes ({self.momentum_lookback_minutes}) > "
                f"bars_lookback_minutes_for_scoring ({self.bars_lookback_minutes_for_scoring}). "
                "Not enough bar history to compute momentum. Increase BARS_LOOKBACK_MINUTES_FOR_SCORING."
            )

        if self.trailing_stop_pct > self.stop_loss_pct:
            _log.warning(
                "Config warning: trailing_stop_pct (%.4f) > stop_loss_pct (%.4f). "
                "The trail is wider than the initial stop — the initial stop will never be the floor.",
                self.trailing_stop_pct,
                self.stop_loss_pct,
            )

        if self.entry_score_threshold > 0.005:
            _log.warning(
                "Config warning: entry_score_threshold=%.6f is very high. "
                "The bot may rarely or never find a qualifying entry.",
                self.entry_score_threshold,
            )


def load_config() -> BotConfig:
    symbols = os.getenv("SYMBOLS_UNIVERSE")
    if symbols:
        universe = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        universe = ["SPY", "QQQ", "IWM", "DIA", "VTI"]

    # Keys in .env files are sometimes pasted with surrounding quotes.
    # Strip quotes so auth doesn't fail.
    api_key = os.environ["ALPACA_API_KEY"].strip().strip('"').strip("'")
    api_secret = os.environ["ALPACA_API_SECRET"].strip().strip('"').strip("'")

    paper = _env_bool("PAPER", True)

    state_path = os.getenv("STATE_PATH", "state.json")
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    return BotConfig(
        api_key=api_key,
        api_secret=api_secret,
        paper=paper,
        symbols_universe=universe,
        state_path=state_path,
        log_level=log_level,
        loop_interval_sec=_env_int("LOOP_INTERVAL_SEC", 10),
        max_daily_realized_loss=_env_float("MAX_DAILY_REALIZED_LOSS", -20.0),
        enable_time_stop=_env_bool("ENABLE_TIME_STOP", False),
        time_stop_minutes=_env_int("TIME_STOP_MINUTES", 240),
        enable_trend_filter=_env_bool("ENABLE_TREND_FILTER", True),
        enable_spy_market_trend_filter=_env_bool("ENABLE_SPY_MARKET_TREND_FILTER", True),
        enable_candlestick_entry_filter=_env_bool("ENABLE_CANDLESTICK_ENTRY_FILTER", False),
        enable_candlestick_exit=_env_bool("ENABLE_CANDLESTICK_EXIT", True),
        require_candlestick_confirmation=_env_bool("REQUIRE_CANDLESTICK_CONFIRMATION", True),
        candlestick_volume_confirm_mult=_env_float("CANDLESTICK_VOLUME_CONFIRM_MULT", 1.2),
        candlestick_score_bonus=_env_float("CANDLESTICK_SCORE_BONUS", 0.00035),
        enable_trailing_stop=_env_bool("ENABLE_TRAILING_STOP", True),
        enable_trend_break_exit=_env_bool("ENABLE_TREND_BREAK_EXIT", True),
        trend_break_ma_minutes=_env_int("TREND_BREAK_MA_MINUTES", 15),
        trend_break_confirm_bars=_env_int("TREND_BREAK_CONFIRM_BARS", 3),
        min_hold_minutes_before_structure_exit=_env_int("MIN_HOLD_MINUTES_BEFORE_STRUCTURE_EXIT", 2),
        symbol_score_adjustments=_parse_symbol_score_adjustments(),
        trend_break_skip_if_profitable=_env_bool("TREND_BREAK_SKIP_IF_PROFITABLE", True),
        trend_break_min_profit_pct_to_skip=_env_float("TREND_BREAK_MIN_PROFIT_PCT_TO_SKIP", 0.0),
        entry_score_threshold=_env_float("ENTRY_SCORE_THRESHOLD", -0.0005),
        min_momentum_return=_env_float("MIN_MOMENTUM_RETURN", -0.0005),
        max_spread_pct=_env_float("MAX_SPREAD_PCT", 0.001),
        min_avg_volume=_env_float("MIN_AVG_VOLUME", 2000.0),
        entry_cooldown_sec=_env_int("ENTRY_COOLDOWN_SEC", 180),
        post_loss_extra_cooldown_sec=_env_int("POST_LOSS_EXTRA_COOLDOWN_SEC", 150),
        min_seconds_between_entry_orders=_env_int("MIN_SECONDS_BETWEEN_ENTRY_ORDERS", 2),
        entry_diagnostic_interval_sec=_env_int("ENTRY_DIAGNOSTIC_INTERVAL_SEC", 180),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 3),
        market_open_delay_minutes=_env_int("MARKET_OPEN_DELAY_MINUTES", 15),
        max_portfolio_notional_usd=_env_float("MAX_PORTFOLIO_NOTIONAL_USD", 10_000.0),
        enable_offline_training=_env_bool("ENABLE_OFFLINE_TRAINING", True),
        offline_training_lookback_trades=_env_int("OFFLINE_TRAINING_LOOKBACK_TRADES", 15),
        enable_online_training=_env_bool("ENABLE_ONLINE_TRAINING", True),
        online_training_window_trades=_env_int("ONLINE_TRAINING_WINDOW_TRADES", 20),
        online_training_min_trades=_env_int("ONLINE_TRAINING_MIN_TRADES", 3),
        online_training_neutral_band_abs_usd=_env_float("ONLINE_TRAINING_NEUTRAL_BAND_ABS_USD", 0.05),
        trailing_stop_min_move_pct=_env_float("TRAILING_STOP_MIN_MOVE_PCT", 0.0002),
        trend_ma_tolerance_pct=_env_float("TREND_MA_TOLERANCE_PCT", 0.001),
        candlestick_wick_ratio=_env_float("CANDLESTICK_WICK_RATIO", 2.0),
        candlestick_sma_proximity_pct=_env_float("CANDLESTICK_SMA_PROXIMITY_PCT", 0.001),
        enable_extended_hours=_env_bool("ENABLE_EXTENDED_HOURS", False),
        after_hours_end_et=os.getenv("AFTER_HOURS_END_ET", "20:00"),
    )

