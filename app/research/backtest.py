"""
Offline replay engine for the live bot's strategy.

The engine walks 1-minute bars in chronological order and, at every minute,
reproduces the decision sequence that ``TradeBot.run_forever`` performs on a live
loop iteration: session gates -> manage open positions -> maybe place an entry.
All signal maths is delegated to ``strategy_signals`` (the *same* functions the
live bot calls) so a signal can never silently drift between live and backtest.

No-lookahead
------------
This is the property everything else depends on, so it is enforced structurally
rather than by convention:

* The only way strategy code can see bars is through :class:`_BarWindow`, which
  is built from a **positional slice ending at the current bar**. There is no
  path from a window back to the full frame.
* Slice end positions come from ``np.searchsorted(..., side="right")`` against
  the current timestamp, computed once up front.
* :func:`run_backtest` accepts ``strict=True`` (default) which asserts, on every
  window handed to strategy code, that ``window.index.max() <= now``.

Approximations vs live bot
--------------------------
The live bot trades against a real order book and a broker; a bar replay cannot
see either. Every place we substitute a model is listed here.

1. **Quotes / spread.** Live reads Alpaca bid/ask (``get_latest_quotes``) for the
   spread filter, the take-profit trigger, the trailing-stop peak, and the
   "skip structure exit while profitable" check. Backtest synthesizes them from
   the bar: ``bid = price * (1 - spread_bps/2e4)``, ``ask = price * (1 +
   spread_bps/2e4)``, and ``spread_pct = spread_bps/1e4`` (constant). With the
   default 2 bps, ``spread_pct = 0.0002`` always passes the 0.001 filter, so the
   spread filter is effectively **disabled** in backtest. Live it does reject
   entries. Raise ``spread_bps`` above ``cfg.max_spread_pct * 1e4`` to see the
   opposite extreme.
2. **Entry execution.** Live places a *limit* buy at ``bid * (1 +
   entry_limit_offset_pct)`` with a 20s timeout, and cancels if unfilled.
   Backtest decides at bar close and fills at the **next bar's open** times
   ``(1 + slippage_bps/1e4)``, and assumes the fill always happens
   (``require_limit_fill=True`` instead skips the entry when the next open is
   above the limit). Live's cancel-on-timeout path — which quietly removes
   entries in fast markets — is therefore not modelled by default.
3. **Intrabar ordering.** When a bar's low breaches the stop *and* its high hits
   the take-profit, the **stop is assumed to fill first**. This is the
   conservative assumption; 1-minute bars carry no path information.
4. **Stop fills.** A stop is filled at the stop price, or at the bar's open when
   the bar gapped through it, minus ``slippage_bps``. Real stop orders on
   leveraged ETFs can slip further on a gap.
5. **Trailing stop ratchet.** Live raises the peak on every 10s quote. Backtest
   updates the peak from the bar's high, but only **after** that bar's stop check
   — the stop in force during bar *k* is the one derived from bars up to *k-1*.
   This avoids using a bar's own high to lock in profit inside that same bar.
   Live's ``trailing_stop_min_move_pct`` throttle and the cancel/replace round
   trip (which briefly leaves the position unprotected) are modelled as an
   instant, free stop move.
6. **Structure / time / EOD exits.** Evaluated on bar close, filled at the next
   bar's open minus slippage. If no next bar exists in the session (EOD), the
   fill is the current bar's close. Live would have sold seconds after the
   signal, at a price between the two.
7. **Broker latency, partial fills, rejections, wash-trade blocks, order-state
   reconciliation, and the ``halt_new_entries`` paths driven by API errors** are
   not modelled at all. Neither is Alpaca's rejection of stop orders on
   fractional positions (and the synthetic-stop fallback that follows).
8. **Position sizing.** ``_compute_qty_for_entry`` is reproduced exactly, using
   simulated cash and simulated position market values. ``whole_shares=True``
   (default) then floors to integer shares, because Alpaca rejects limit orders
   on fractional quantities — on a ~$100 account this is the binding constraint
   and it removes most expensive symbols from consideration entirely.
9. **Offline/online training.** ``enable_offline_training`` reads ``bot.log`` and
   is always off here. Online training (rolling EWA of realized P&L nudging the
   entry thresholds) IS reproduced, gated by ``cfg.enable_online_training``.
10. **Data feed.** The live bot requests ``DataFeed.IEX`` bars and quotes; this
    research stack defaults to ``DataFeed.SIP``. SIP volume is materially larger
    than IEX volume, so ``min_avg_volume`` and the ``volume_ratio`` term in the
    score behave differently. This is the single largest source of divergence.
11. **Fees.** ``commission_per_order`` defaults to 0 (Alpaca is commission-free).
    Regulatory SEC/TAF sell fees, borrow costs, and the ETFs' expense drag are
    not modelled.
12. **max_entry_attempts_per_day** exists in ``BotConfig`` but is never read by
    ``bot.py``. It is off here too unless ``enforce_max_entry_attempts=True``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from research import execution as execution_mod

import sys as _sys
from pathlib import Path as _Path

# Allow both `python -m app.research.x` and `python app/research/x.py` by making
# the `app/` directory importable (that is where config/strategy_signals live).
_APP_DIR = str(_Path(__file__).resolve().parent.parent)
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

try:  # pragma: no cover
    from ..config import ET_TZ, BotConfig
    from ..strategy_signals import (
        bearish_candlestick_exit,
        candlestick_bullish_strength,
        dual_ma_uptrend,
        trend_break_confirmed_below_sma,
    )
except ImportError:  # pragma: no cover
    from config import ET_TZ, BotConfig  # type: ignore[no-redef]
    from strategy_signals import (  # type: ignore[no-redef]
        bearish_candlestick_exit,
        candlestick_bullish_strength,
        dual_ma_uptrend,
        trend_break_confirmed_below_sma,
    )

try:  # pragma: no cover
    from . import fastsig
except ImportError:  # pragma: no cover
    import fastsig  # type: ignore[no-redef]

UTC = pd.Timestamp("2020-01-01", tz="UTC").tzinfo

BAR_COLUMNS = ("open", "high", "low", "close", "volume")


# --------------------------------------------------------------------------- #
# result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Trade:
    """One completed round trip."""

    symbol: str
    entry_ts: pd.Timestamp
    entry_px: float
    exit_ts: pd.Timestamp
    exit_px: float
    qty: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    bars_held: int
    mfe: float  # max favourable excursion, fraction of entry price
    mae: float  # max adverse excursion, fraction of entry price (negative)
    direction: int = 1  # +1 long, -1 short


@dataclass
class BacktestResult:
    trades: List[Trade]
    equity_curve: pd.Series
    metrics: Dict[str, object]
    config: BotConfig
    params: Dict[str, object] = field(default_factory=dict)

    @property
    def trades_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame(
                columns=[
                    "symbol", "entry_ts", "entry_px", "exit_ts", "exit_px", "qty",
                    "pnl", "pnl_pct", "exit_reason", "bars_held", "mfe", "mae",
                ]
            )
        return pd.DataFrame([t.__dict__ for t in self.trades])

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"trades          {m['trade_count']}",
            f"total_pnl       {m['total_pnl']:.2f}",
            f"return_pct      {m['return_pct']:.2f}%",
            f"win_rate        {m['win_rate']:.1f}%",
            f"avg_win         {m['avg_win']:.3f}",
            f"avg_loss        {m['avg_loss']:.3f}",
            f"profit_factor   {m['profit_factor']:.3f}",
            f"expectancy      {m['expectancy']:.4f}",
            f"max_drawdown    {m['max_drawdown_pct']:.2f}%",
            f"sharpe          {m['sharpe']:.3f}",
            f"sortino         {m['sortino']:.3f}",
            f"avg_bars_held   {m['avg_bars_held']:.1f}",
            f"exit_reasons    {m['exit_reasons']}",
            f"per_symbol_pnl  {m['per_symbol_pnl']}",
        ]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# bar access with a structural no-lookahead guarantee
# --------------------------------------------------------------------------- #
class _BarWindow:
    """
    Duck-typed stand-in for the live ``bars_df`` MultiIndex frame.

    ``strategy_signals`` and the scoring code only ever reach bars through
    ``bars_df.xs(sym, level=0)``, so implementing that one method is enough to
    run the real helpers unmodified — and far cheaper than rebuilding a
    MultiIndex frame on every one of ~65k minutes.

    The window holds only already-sliced per-symbol frames, so future bars are
    not merely un-consulted, they are unreachable.
    """

    __slots__ = ("_frames", "_now")

    def __init__(self, frames: Dict[str, pd.DataFrame], now: pd.Timestamp) -> None:
        self._frames = frames
        self._now = now

    def xs(self, key: str, level: int = 0) -> pd.DataFrame:
        if level != 0:
            raise KeyError(level)
        try:
            return self._frames[key]
        except KeyError as exc:  # mirrors pandas' KeyError contract
            raise KeyError(key) from exc

    @property
    def empty(self) -> bool:
        return not any(len(f) for f in self._frames.values())

    def symbols(self) -> Sequence[str]:
        return tuple(self._frames)


class _Series:
    """Per-symbol column store + a positional map from the global timeline."""

    __slots__ = ("symbol", "df", "index", "open", "high", "low", "close", "volume", "end_pos")

    def __init__(self, symbol: str, df: pd.DataFrame, timeline: pd.DatetimeIndex) -> None:
        self.symbol = symbol
        self.df = df
        self.index = df.index
        self.open = df["open"].to_numpy(dtype=float)
        self.high = df["high"].to_numpy(dtype=float)
        self.low = df["low"].to_numpy(dtype=float)
        self.close = df["close"].to_numpy(dtype=float)
        self.volume = df["volume"].to_numpy(dtype=float)
        # end_pos[k] = number of this symbol's bars with ts <= timeline[k].
        # "side=right" is what makes the window inclusive of the current minute
        # and exclusive of everything after it.
        self.end_pos = np.searchsorted(
            self.index.asi8, timeline.asi8, side="right"
        ).astype(np.int64)

    def has_bar_at(self, k: int) -> bool:
        """True when this symbol printed a bar exactly at timeline position k."""
        e = self.end_pos[k]
        return e > 0 and (k == 0 or e > self.end_pos[k - 1])


def _prepare(bars: pd.DataFrame, symbols: Sequence[str]) -> Tuple[Dict[str, _Series], pd.DatetimeIndex]:
    if not isinstance(bars.index, pd.MultiIndex) or bars.index.nlevels != 2:
        raise ValueError("bars must have a (symbol, timestamp) MultiIndex")
    missing = [c for c in BAR_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars missing columns {missing}")

    bars = bars.sort_index()
    available = set(bars.index.get_level_values(0).unique())
    wanted = [s for s in symbols if s in available]
    if not wanted:
        raise ValueError(f"none of {list(symbols)} present in bars ({sorted(available)})")

    per_symbol_raw = {s: bars.xs(s, level=0).sort_index() for s in wanted}
    timeline = pd.DatetimeIndex(
        sorted(set().union(*[set(d.index) for d in per_symbol_raw.values()]))
    )
    if timeline.tz is None:
        timeline = timeline.tz_localize("UTC")
    series = {s: _Series(s, d, timeline) for s, d in per_symbol_raw.items()}
    return series, timeline


# --------------------------------------------------------------------------- #
# engine internals
# --------------------------------------------------------------------------- #
@dataclass
class _Leg:
    symbol: str
    qty: float
    entry_px: float
    entry_ts: pd.Timestamp
    entry_bar: int
    stop_px: float
    peak: float
    mfe: float = 0.0
    mae: float = 0.0
    # +1 long, -1 short. A short's stop sits ABOVE entry and its target BELOW,
    # and its excursions are mirrored: the high of a bar is adverse, the low
    # favourable. Every sign in the accounting derives from this field.
    direction: int = 1

    @property
    def is_short(self) -> bool:
        return self.direction < 0


@dataclass
class _PendingEntry:
    symbol: str
    qty: float
    limit_px: float
    score: float
    direction: int = 1


def _parse_hhmm(value: str) -> Tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


def _ewa(pnls: Sequence[float], decay: float = 0.9) -> float:
    """Copy of ``offline_trainer._ewa`` (importing it would drag in log parsing)."""
    if not pnls:
        return 0.0
    n = len(pnls)
    weights = [decay ** (n - 1 - i) for i in range(n)]
    return sum(w * p for w, p in zip(weights, pnls)) / sum(weights)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
def run_backtest(
    cfg: BotConfig,
    bars: pd.DataFrame,
    start_equity: float = 100.0,
    *,
    symbols: Optional[Sequence[str]] = None,
    slippage_bps: float = 1.0,
    spread_bps: float = 2.0,
    commission_per_order: float = 0.0,
    execution_model: str = execution_mod.SPREAD_AWARE,
    whole_shares: bool = True,
    require_limit_fill: bool = False,
    enforce_max_entry_attempts: bool = False,
    fast: bool = True,
    random_entry_seed: Optional[int] = None,
    random_entry_rate: float = 0.01,
    record_equity: str = "bar",
    strict: bool = True,
) -> BacktestResult:
    """
    Replay ``bars`` minute by minute under ``cfg`` and return the result.

    Parameters
    ----------
    cfg
        A :class:`BotConfig`. Sweeps vary this via ``dataclasses.replace``.
    bars
        MultiIndex ``(symbol, timestamp)`` frame in UTC (see ``research.data``).
    start_equity
        Starting cash. The live account is ~$100; sizing is very sensitive to it.
    slippage_bps / spread_bps / commission_per_order
        Execution model. See "Approximations vs live bot" in the module docstring.
    whole_shares
        Floor order quantity to integer shares (Alpaca rejects fractional limit
        orders — this is what actually constrains a $100 account).
    require_limit_fill
        Model the entry as a real limit order: skip the entry if the next bar's
        open is above the limit price.
    record_equity
        ``"bar"`` for a per-minute equity curve, ``"day"`` for ET-daily closes.
    random_entry_seed / random_entry_rate
        Control experiment. When a seed is given the entry *decision* is replaced
        by a coin flip at ``random_entry_rate`` per eligible minute over a
        uniformly chosen eligible symbol; every gate that is not the signal
        (session window, cooldowns, position cap, sizing) and every exit rule
        stay exactly as they are. Comparing this against the real signal is the
        only honest way to tell an edge from a well-tuned coin. See
        ``research.controls``.
    fast
        Use ``research.fastsig``'s precomputed signal arrays instead of calling
        the pandas helpers on a fresh window every minute. ~40x faster and
        proven equal bar-for-bar by ``test_fastsig.py``. Set False to replay
        through the live bot's own helper code as a cross-check.
    strict
        Assert on every window that no future bar is visible. Costs ~10%.
        Only meaningful on the slow path — the fast arrays are causal by
        construction (``test_fastsig.test_no_lookahead_*``).
    """
    universe = list(symbols or cfg.symbols_universe or [])
    if not universe:
        raise ValueError("cfg.symbols_universe is empty and no symbols= override given")

    # SPY is only fetched to feed the market trend filter; it is never traded
    # unless the user actually put it in the universe.
    trend_symbols = list(universe)
    if cfg.enable_spy_market_trend_filter and "SPY" in bars.index.get_level_values(0).unique():
        if "SPY" not in trend_symbols:
            trend_symbols.append("SPY")

    rng = np.random.default_rng(random_entry_seed) if random_entry_seed is not None else None
    series, timeline = _prepare(bars, trend_symbols)
    sigs: Dict[str, "fastsig.SymbolSignals"] = {}
    if fast:
        sigs = {
            sym: fastsig.precompute_symbol(
                s_.index, s_.open, s_.high, s_.low, s_.close, s_.volume, cfg,
                int(cfg.bars_lookback_minutes_for_scoring),
            )
            for sym, s_ in series.items()
        }
    tradable = [s for s in universe if s in series]
    n = len(timeline)
    if n == 0:
        raise ValueError("no bars to replay")

    lookback = int(cfg.bars_lookback_minutes_for_scoring)
    # One named execution model owns every price adjustment. Before this, the
    # modelled bid was applied to exits but not to entries, so a round trip paid
    # about half the spread it should have - which is why published results
    # barely moved between 2 and 10 bp. See research/execution.py.
    xm = execution_mod.ExecutionModel(
        name=execution_model,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        # One fee policy. The engine previously deducted commission_per_order
        # from cash and 2x from P&L, then also deducted cfg.fee_estimate_per_order
        # from P&L only - a second fee that never touched cash.
        fee_per_order=(
            commission_per_order
            if commission_per_order
            else float(getattr(cfg, "fee_estimate_per_order", 0.0) or 0.0)
        ),
    )
    slip = xm.slip
    bid_f = xm.bid_factor
    spread_pct_model = xm.spread_pct

    open_hh, open_mm = _parse_hhmm(cfg.market_open_time_et)
    close_hh, close_mm = _parse_hhmm(cfg.market_close_time_et)
    ah_hh, ah_mm = _parse_hhmm(cfg.after_hours_end_et)
    open_min = open_hh * 60 + open_mm
    close_min = close_hh * 60 + close_mm
    ah_min = ah_hh * 60 + ah_mm
    session_end_min = ah_min if cfg.enable_extended_hours else close_min
    eod_cut_min = session_end_min - int(cfg.end_of_day_flat_minutes)
    entry_open_min = open_min + int(cfg.market_open_delay_minutes)

    ts_et = timeline.tz_convert(ET_TZ)
    et_minute = (ts_et.hour * 60 + ts_et.minute).to_numpy()
    et_weekday = ts_et.weekday.to_numpy()
    et_date = np.asarray(ts_et.date)

    # --- simulated account / state ------------------------------------------
    cash = float(start_equity)
    legs: Dict[str, _Leg] = {}
    trades: List[Trade] = []
    pending_entry: Optional[_PendingEntry] = None
    pending_exits: Dict[str, str] = {}  # symbol -> reason, filled next bar open
    cooldown_until: Dict[str, pd.Timestamp] = {}
    recent_pnls: List[float] = []
    dyn_score_th: Optional[float] = None
    dyn_mom_th: Optional[float] = None
    daily_realized = 0.0
    halt_new_entries = False
    entry_attempts_today = 0
    cur_date = None
    equity_ts: List[pd.Timestamp] = []
    equity_vals: List[float] = []
    last_close: Dict[str, float] = {}

    def _apply_online_training() -> None:
        """Mirror of ``TradeBot._apply_online_training_from_window``."""
        nonlocal dyn_score_th, dyn_mom_th
        if not cfg.enable_online_training:
            return
        if len(recent_pnls) < max(1, int(cfg.online_training_min_trades)):
            return
        avg = _ewa(recent_pnls)
        if abs(avg) <= float(cfg.online_training_neutral_band_abs_usd):
            dyn_score_th = None
            dyn_mom_th = None
            return
        step_s = abs(cfg.online_training_step_score)
        step_m = abs(cfg.online_training_step_momentum)
        sign = -1.0 if avg > 0.0 else 1.0
        dyn_score_th = _clamp(
            cfg.entry_score_threshold + sign * step_s,
            cfg.offline_training_min_entry_score_threshold,
            cfg.offline_training_max_entry_score_threshold,
        )
        dyn_mom_th = _clamp(
            cfg.min_momentum_return + sign * step_m,
            cfg.offline_training_min_min_momentum_return,
            cfg.offline_training_max_min_momentum_return,
        )

    def _window(k: int) -> _BarWindow:
        """Bars visible at timeline position k. Never includes bar k+1 or later."""
        frames: Dict[str, pd.DataFrame] = {}
        for sym, s in series.items():
            end = int(s.end_pos[k])
            if end <= 0:
                continue
            frames[sym] = s.df.iloc[max(0, end - lookback) : end]
        w = _BarWindow(frames, timeline[k])
        if strict:
            now = timeline[k]
            for sym, f in frames.items():
                if len(f) and f.index[-1] > now:
                    raise AssertionError(
                        f"lookahead: window for {sym} at {now} ends {f.index[-1]}"
                    )
        return w

    def _score_symbols(window: _BarWindow) -> List[Tuple[str, float]]:
        """Byte-for-byte port of ``TradeBot._score_symbols``."""
        scores: List[Tuple[str, float]] = []
        need = max(cfg.momentum_lookback_minutes, cfg.volume_lookback_minutes) + 5
        for sym in universe:
            try:
                df_sym = window.xs(sym, level=0)
            except KeyError:
                continue
            if len(df_sym) < need:
                continue
            latest_ts = df_sym.index.max()
            latest_close = float(df_sym["close"].iloc[-1])
            latest_volume = float(df_sym["volume"].iloc[-1])
            target_time = latest_ts - pd.Timedelta(minutes=cfg.momentum_lookback_minutes)
            past_slice = df_sym[df_sym.index <= target_time]
            if past_slice.empty:
                continue
            close_past = float(past_slice["close"].iloc[-1])
            if close_past <= 0:
                continue
            momentum_return = (latest_close / close_past) - 1.0
            vol_window = df_sym["volume"].iloc[-cfg.volume_lookback_minutes :]
            avg_vol = float(vol_window.mean()) if not vol_window.empty else 0.0
            if avg_vol <= 0 or avg_vol < cfg.min_avg_volume:
                continue
            capped_vol_ratio = min(latest_volume / avg_vol, 5.0)
            scores.append((sym, float(momentum_return * math.log1p(capped_vol_ratio))))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    def _choose_entry_candidate(
        window: _BarWindow, now: pd.Timestamp, held: set[str]
    ) -> Tuple[Optional[str], float]:
        """Port of ``TradeBot._choose_entry_candidate`` (quotes replaced by the model)."""
        ranked = _score_symbols(window)
        if not ranked:
            return None, -np.inf
        adj = cfg.symbol_score_adjustments or {}
        ranked.sort(key=lambda x: x[1] + float(adj.get(x[0], 0.0)), reverse=True)

        score_th = dyn_score_th if dyn_score_th is not None else cfg.entry_score_threshold
        mom_th = dyn_mom_th if dyn_mom_th is not None else cfg.min_momentum_return

        for best_symbol, raw_score in ranked:
            if best_symbol in held:
                continue
            ok_after = cooldown_until.get(best_symbol)
            if ok_after is not None and now < ok_after:
                continue
            # Modelled spread (constant): live checks the live book here.
            if spread_pct_model > cfg.max_spread_pct:
                continue

            df_sym = window.xs(best_symbol, level=0)
            latest_close = float(df_sym["close"].iloc[-1])
            latest_ts = df_sym.index.max()
            target_time = latest_ts - pd.Timedelta(minutes=cfg.momentum_lookback_minutes)
            past_slice = df_sym[df_sym.index <= target_time]
            if past_slice.empty:
                continue
            close_past = float(past_slice["close"].iloc[-1])
            momentum_return = (latest_close / close_past) - 1.0 if close_past > 0 else -np.inf
            if momentum_return < mom_th:
                continue

            if cfg.enable_spy_market_trend_filter and "SPY" in universe:
                if not dual_ma_uptrend(window, "SPY", cfg):
                    continue
            if not dual_ma_uptrend(window, best_symbol, cfg):
                continue

            candle_strength = candlestick_bullish_strength(window, best_symbol, cfg)
            if cfg.enable_candlestick_entry_filter and candle_strength <= 0.0:
                continue

            final_score = (
                raw_score
                + float(cfg.candlestick_score_bonus) * candle_strength
                + float(adj.get(best_symbol, 0.0))
            )
            if final_score < score_th:
                continue
            return best_symbol, final_score
        return None, -np.inf

    def _score_symbols_fast(k: int) -> List[Tuple[str, float]]:
        """Array-lookup twin of ``_score_symbols``. Same iteration and sort order."""
        out: List[Tuple[str, float]] = []
        for sym in universe:
            s_ = series.get(sym)
            if s_ is None:
                continue
            e = int(s_.end_pos[k])
            if e <= 0:
                continue
            sg = sigs[sym]
            j = e - 1
            if not sg.score_valid[j]:
                continue
            out.append((sym, float(sg.score[j])))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    def _choose_short_candidate_fast(
        k: int, now: pd.Timestamp, held: set[str]
    ) -> Tuple[Optional[str], float]:
        """
        Pick a short candidate: the mirror of the long gate.

        Ranks by *ascending* score and requires momentum at or below
        ``max_momentum_return_for_short``. The trend filter is inverted too - a
        long wants price above its moving averages, a short wants it below.

        This is deliberately the symmetric counterpart of the existing signal
        rather than a new idea. It answers "does this signal work in the other
        direction", which is worth knowing before anything more elaborate is
        built on top of a signal already measured as a coin flip.
        """
        ranked = _score_symbols_fast(k)
        if not ranked:
            return None, np.inf
        adj = cfg.symbol_score_adjustments or {}
        # Ascending: the most negative score is the best short.
        ranked.sort(key=lambda x: x[1] + float(adj.get(x[0], 0.0)))

        mom_ceiling = float(cfg.max_momentum_return_for_short)
        for best_symbol, raw_score in ranked:
            if best_symbol in held:
                continue
            ok_after = cooldown_until.get(best_symbol)
            if ok_after is not None and now < ok_after:
                continue
            if spread_pct_model > cfg.max_spread_pct:
                continue
            s_ = series[best_symbol]
            j = int(s_.end_pos[k]) - 1
            sg = sigs[best_symbol]
            momentum_return = float(sg.momentum[j])
            if not (momentum_return <= mom_ceiling):  # NaN-safe
                continue
            if cfg.enable_trend_filter and bool(sg.uptrend[j]):
                # Inverted: an uptrend disqualifies a short.
                continue
            # score_valid already folds in the minimum-volume and
            # sufficient-history gates (see fastsig.precompute_symbol).
            if not bool(sg.score_valid[j]):
                continue
            return best_symbol, raw_score
        return None, np.inf

    def _choose_entry_candidate_fast(
        k: int, now: pd.Timestamp, held: set[str]
    ) -> Tuple[Optional[str], float]:
        """Array-lookup twin of ``_choose_entry_candidate``."""
        ranked = _score_symbols_fast(k)
        if not ranked:
            return None, -np.inf
        adj = cfg.symbol_score_adjustments or {}
        ranked.sort(key=lambda x: x[1] + float(adj.get(x[0], 0.0)), reverse=True)

        score_th = dyn_score_th if dyn_score_th is not None else cfg.entry_score_threshold
        mom_th = dyn_mom_th if dyn_mom_th is not None else cfg.min_momentum_return

        spy_ok: Optional[bool] = None
        for best_symbol, raw_score in ranked:
            if best_symbol in held:
                continue
            ok_after = cooldown_until.get(best_symbol)
            if ok_after is not None and now < ok_after:
                continue
            if spread_pct_model > cfg.max_spread_pct:
                continue

            s_ = series[best_symbol]
            j = int(s_.end_pos[k]) - 1
            sg = sigs[best_symbol]
            momentum_return = float(sg.momentum[j])
            if not (momentum_return >= mom_th):  # NaN-safe: NaN fails the gate
                continue

            if cfg.enable_spy_market_trend_filter and "SPY" in universe:
                if spy_ok is None:
                    spy = series.get("SPY")
                    e_spy = int(spy.end_pos[k]) if spy is not None else 0
                    spy_ok = bool(sigs["SPY"].uptrend[e_spy - 1]) if e_spy > 0 else False
                if not spy_ok:
                    continue
            if not sg.uptrend[j]:
                continue

            candle_strength = float(sg.candle[j])
            if cfg.enable_candlestick_entry_filter and candle_strength <= 0.0:
                continue

            final_score = (
                raw_score
                + float(cfg.candlestick_score_bonus) * candle_strength
                + float(adj.get(best_symbol, 0.0))
            )
            if final_score < score_th:
                continue
            return best_symbol, final_score
        return None, -np.inf

    def _choose_entry_candidate_random(
        k: int, now: pd.Timestamp, held: set[str]
    ) -> Tuple[Optional[str], float]:
        """Coin-flip control: same eligibility, no signal."""
        assert rng is not None
        if rng.random() >= random_entry_rate:
            return None, -np.inf
        pool = []
        for sym in tradable:
            if sym in held:
                continue
            ok_after = cooldown_until.get(sym)
            if ok_after is not None and now < ok_after:
                continue
            s_ = series[sym]
            e = int(s_.end_pos[k])
            if e <= 0:
                continue
            if fast and not sigs[sym].score_valid[e - 1]:
                continue
            pool.append(sym)
        if not pool:
            return None, -np.inf
        return str(rng.choice(pool)), 0.0

    def _invested_value() -> float:
        """
        Mark-to-market value of open positions, as it contributes to equity.

        A long contributes its market value. A short contributes the collateral
        held against it plus its unrealized gain, because the sale proceeds were
        never credited as spendable cash - they sit against the position. Adding
        a short's market value the way a long's is added would make equity rise
        as the position moved against us.
        """
        total = 0.0
        for l in legs.values():
            px = last_close.get(l.symbol, l.entry_px)
            if l.is_short:
                total += l.qty * l.entry_px * cfg.short_maintenance_margin_pct
                total += (l.entry_px - px) * l.qty
            else:
                total += l.qty * px
        return total

    def _gross_exposure() -> float:
        """Absolute notional at risk, both sides, for the portfolio cap."""
        return sum(l.qty * last_close.get(l.symbol, l.entry_px) for l in legs.values())

    def _compute_qty(entry_price: float, direction: int = 1) -> float:
        """Port of ``TradeBot._compute_qty_for_entry`` against simulated balances."""
        if daily_realized <= cfg.max_daily_realized_loss:
            return 0.0
        remaining = abs(cfg.max_daily_realized_loss - daily_realized)
        risk_per_trade = min(cfg.max_risk_per_trade, remaining)
        stop_distance = entry_price * cfg.stop_loss_pct
        if stop_distance <= 0 or entry_price <= 0 or cash <= 0:
            return 0.0
        qty_by_risk = risk_per_trade / stop_distance
        if direction < 0:
            # A short does not pay the share price; it posts collateral. FINRA
            # scales the 30% short requirement by fund leverage, so a 3x ETF
            # needs 90% of market value - which means margin buys almost
            # nothing on this universe. 1/0.90 = 1.11x, not 4x.
            per_share_collateral = entry_price * cfg.short_maintenance_margin_pct
            qty_by_cash = cash / per_share_collateral if per_share_collateral > 0 else 0.0
        else:
            qty_by_cash = cash / entry_price
        room = max(0.0, float(cfg.max_portfolio_notional_usd) - _gross_exposure())
        qty_by_cap = (room * 0.98) / entry_price
        qty = min(qty_by_risk, qty_by_cash * 0.98, qty_by_cap)
        if whole_shares:
            qty = float(math.floor(qty))
        else:
            qty = float(round(qty, 6))
        return qty if qty > 0 else 0.0

    def _close_fill(leg: _Leg, reference: float) -> float:
        """Price to close ``leg`` at ``reference`` — a short covers by buying."""
        return xm.buy_fill(reference) if leg.is_short else xm.sell_fill(reference)

    def _close_leg(leg: _Leg, exit_px: float, exit_ts: pd.Timestamp, reason: str, k: int) -> None:
        nonlocal cash, daily_realized, halt_new_entries
        # Charge the fee once, to cash and to P&L, from the same figure. The
        # entry already paid one fee when it filled, so a round trip pays two.
        #
        # Signed for direction. A long sells to close, receiving proceeds. A
        # short BUYS to close, paying them out - and its gain is entry minus
        # exit, the mirror of a long's.
        if leg.is_short:
            # Release the collateral that was reserved at entry, then pay for
            # the buy-to-cover. The entry credited no cash: short sale proceeds
            # are held against the position, not spendable.
            cash += leg.qty * leg.entry_px * cfg.short_maintenance_margin_pct
            cash -= leg.qty * exit_px - leg.qty * leg.entry_px
            cash -= xm.fee
        else:
            cash += leg.qty * exit_px - xm.fee
        pnl = (exit_px - leg.entry_px) * leg.qty * leg.direction - 2.0 * xm.fee
        daily_realized += pnl
        if daily_realized <= cfg.max_daily_realized_loss:
            halt_new_entries = True
        trades.append(
            Trade(
                symbol=leg.symbol,
                entry_ts=leg.entry_ts,
                entry_px=leg.entry_px,
                exit_ts=exit_ts,
                exit_px=exit_px,
                qty=leg.qty,
                pnl=pnl,
                pnl_pct=(exit_px / leg.entry_px - 1.0) * 100.0 * leg.direction,
                exit_reason=reason,
                bars_held=k - leg.entry_bar,
                mfe=leg.mfe,
                mae=leg.mae,
                direction=leg.direction,
            )
        )
        legs.pop(leg.symbol, None)
        # Cooldown, mirroring _record_exit_for_cooldown.
        cd = float(cfg.entry_cooldown_sec)
        if pnl < 0:
            cd += float(cfg.post_loss_extra_cooldown_sec)
        cooldown_until[leg.symbol] = exit_ts + pd.Timedelta(seconds=cd)
        if cfg.enable_online_training:
            recent_pnls.append(pnl)
            del recent_pnls[: max(0, len(recent_pnls) - max(1, int(cfg.online_training_window_trades)))]
            _apply_online_training()

    # ----------------------------------------------------------------------- #
    # main replay loop
    # ----------------------------------------------------------------------- #
    for k in range(n):
        now = timeline[k]
        minute = int(et_minute[k])
        day = et_date[k]

        # --- daily reset (ET date rollover), mirroring _reset_daily_if_needed
        if day != cur_date:
            cur_date = day
            daily_realized = 0.0
            halt_new_entries = False
            entry_attempts_today = 0
            recent_pnls.clear()
            dyn_score_th = None
            dyn_mom_th = None

        for sym, s in series.items():
            e = int(s.end_pos[k])
            if e > 0:
                last_close[sym] = float(s.close[e - 1])

        in_session = (
            et_weekday[k] < 5 and open_min <= minute <= session_end_min
        )
        if not in_session:
            # Live sleeps outside the session. Anything queued at the last bar of
            # a session is dropped rather than filled at a stale next-day open.
            pending_entry = None
            pending_exits.clear()
            # Mark to market before skipping. The daily equity record used to be
            # attached to the last bar of each ET *date*, but this continue runs
            # first - so when postmarket bars trailed the session (SIP includes
            # them) the last bar of the date was out of session, the record was
            # never written, and record_equity="day" returned an EMPTY curve.
            if record_equity == "day" and (k + 1 == n or et_date[k + 1] != day):
                equity_ts.append(now)
                equity_vals.append(cash + _invested_value())
            continue

        # --- 1. fill orders queued on the previous bar, at this bar's open ----
        if pending_exits:
            for sym, reason in list(pending_exits.items()):
                leg = legs.get(sym)
                s = series.get(sym)
                if leg is None or s is None or not s.has_bar_at(k):
                    pending_exits.pop(sym, None)
                    continue
                px = _close_fill(leg, float(s.open[int(s.end_pos[k]) - 1]))
                _close_leg(leg, px, now, reason, k)
                pending_exits.pop(sym, None)

        if pending_entry is not None:
            pe = pending_entry
            pending_entry = None
            s = series.get(pe.symbol)
            if s is not None and s.has_bar_at(k) and pe.symbol not in legs:
                raw_open = float(s.open[int(s.end_pos[k]) - 1])
                is_short = pe.direction < 0
                # A short opens by SELLING, so it fills on the bid.
                fill = xm.sell_fill(raw_open) if is_short else xm.buy_fill(raw_open)
                if is_short:
                    # Skip when the open is below the short's limit: selling
                    # short below the intended price is the adverse case.
                    skip = require_limit_fill and raw_open < pe.limit_px
                else:
                    skip = require_limit_fill and raw_open > pe.limit_px
                    # The limit is only a real constraint in require_limit_fill mode.
                    # By default the engine models the entry as filling at the next
                    # bar's open regardless of the submitted limit - a documented
                    # approximation, because a minute bar cannot establish whether a
                    # limit filled inside the live 20-second window. Under
                    # require_limit_fill the guarantee is enforced strictly: a buy
                    # never fills above its limit, slippage included.
                    if require_limit_fill and not skip:
                        if xm.cap_buy_at_limit(fill, pe.limit_px) is None:
                            skip = True
                if is_short:
                    # Post collateral rather than pay the share price. Short sale
                    # proceeds are held against the position, not credited as
                    # spendable cash, which is why _invested_value carries the
                    # collateral back into equity.
                    cost = pe.qty * fill * cfg.short_maintenance_margin_pct + xm.fee
                else:
                    cost = pe.qty * fill + xm.fee
                if not skip and pe.qty > 0 and cost <= cash:
                    cash -= cost
                    legs[pe.symbol] = _Leg(
                        symbol=pe.symbol,
                        qty=pe.qty,
                        entry_px=fill,
                        entry_ts=now,
                        entry_bar=k,
                        # A short's stop is ABOVE its entry.
                        stop_px=fill * (1.0 + cfg.stop_loss_pct)
                        if is_short
                        else fill * (1.0 - cfg.stop_loss_pct),
                        peak=fill,
                        direction=pe.direction,
                    )

        # --- 2. manage open positions on this bar ----------------------------
        if legs:
            window = None if fast else _window(k)
            for sym in list(legs):
                leg = legs[sym]
                s = series[sym]
                if not s.has_bar_at(k):
                    continue
                i = int(s.end_pos[k]) - 1
                b_open, b_high, b_low, b_close = (
                    float(s.open[i]), float(s.high[i]), float(s.low[i]), float(s.close[i])
                )

                # excursions, mirrored for a short: the bar's high is the
                # adverse move and its low the favourable one.
                if leg.is_short:
                    leg.mfe = max(leg.mfe, 1.0 - b_low / leg.entry_px)
                    leg.mae = min(leg.mae, 1.0 - b_high / leg.entry_px)
                else:
                    leg.mfe = max(leg.mfe, b_high / leg.entry_px - 1.0)
                    leg.mae = min(leg.mae, b_low / leg.entry_px - 1.0)

                # 2a. stop first (conservative intrabar ordering)
                if leg.is_short:
                    # A short's stop is above entry and triggers on the high.
                    # Gap-through is upward: a bar opening above the stop covers
                    # at the open, which is worse than the stop price.
                    if b_high >= leg.stop_px:
                        reference = max(leg.stop_px, b_open) if b_open > leg.stop_px else leg.stop_px
                        reference = min(reference, b_high)
                        px = xm.buy_fill(reference)
                        _close_leg(leg, px, now, "stop", k)
                        continue
                elif b_low <= leg.stop_px:
                    # A stop is a trigger, not a guaranteed price: if the bar
                    # gapped below it, the fill is the open.
                    px = xm.stop_fill(leg.stop_px, b_open, b_low)
                    _close_leg(leg, px, now, "stop", k)
                    continue

                # 2b. take-profit (live compares the bid)
                if cfg.enable_take_profit and leg.entry_px > 0:
                    if leg.is_short:
                        # A short's target is below entry, and it covers by
                        # buying, so the ask is what has to reach the target.
                        tp_ask_target = leg.entry_px * (1.0 - cfg.take_profit_pct)
                        ask_f = xm.ask_factor
                        if b_low * ask_f <= tp_ask_target:
                            raw_tp = tp_ask_target / ask_f if ask_f else tp_ask_target
                            reference = min(raw_tp, b_open) if b_open < raw_tp else raw_tp
                            px = xm.buy_fill(reference)
                            _close_leg(leg, px, now, "take_profit", k)
                            continue
                    else:
                        tp_bid_target = leg.entry_px * (1.0 + cfg.take_profit_pct)
                        if b_high * bid_f >= tp_bid_target:
                            # The reference price at which the bid reaches the
                            # target. Previously this divided the bid factor back
                            # out and then credited that mid directly, so the exit
                            # was filled above the bid it was meant to sell at.
                            raw_tp = tp_bid_target / bid_f if bid_f else tp_bid_target
                            reference = max(raw_tp, b_open) if b_open > raw_tp else raw_tp
                            px = xm.sell_fill(reference)
                            _close_leg(leg, px, now, "take_profit", k)
                            continue

                # 2c. ratchet the trailing stop AFTER this bar's checks
                if cfg.enable_trailing_stop:
                    leg.peak = max(leg.peak, b_high)
                    target = max(
                        leg.entry_px * (1.0 - cfg.stop_loss_pct),
                        leg.peak * (1.0 - cfg.trailing_stop_pct),
                    )
                    if target > leg.stop_px * (1.0 + cfg.trailing_stop_min_move_pct):
                        leg.stop_px = target

                # 2d. structure exits (bar close decision -> next open fill)
                held_minutes = (now - leg.entry_ts).total_seconds() / 60.0
                unlocked = held_minutes >= float(cfg.min_hold_minutes_before_structure_exit)
                profitable_skip = (
                    cfg.trend_break_skip_if_profitable
                    and b_close * bid_f
                    >= leg.entry_px * (1.0 + float(cfg.trend_break_min_profit_pct_to_skip))
                )
                reason: Optional[str] = None
                if unlocked and not profitable_skip:
                    if fast:
                        sg = sigs[sym]
                        if cfg.enable_trend_break_exit and sg.trend_break[i]:
                            reason = "trend_break"
                        elif cfg.enable_candlestick_exit and sg.bear_exit[i]:
                            reason = "candlestick"
                    else:
                        df_sym = window.xs(sym, level=0)
                        if cfg.enable_trend_break_exit and trend_break_confirmed_below_sma(
                            df_sym,
                            int(cfg.trend_break_ma_minutes),
                            max(1, int(cfg.trend_break_confirm_bars)),
                        ):
                            reason = "trend_break"
                        elif cfg.enable_candlestick_exit and len(df_sym) >= 2:
                            if bearish_candlestick_exit(
                                df_sym.iloc[-2],
                                df_sym.iloc[-1],
                                wick_ratio=float(cfg.candlestick_wick_ratio),
                            ):
                                reason = "candlestick"

                if reason is None and cfg.enable_time_stop:
                    if now >= leg.entry_ts + timedelta(minutes=int(cfg.time_stop_minutes)):
                        reason = "time_stop"
                if reason is None and minute >= eod_cut_min:
                    reason = "eod"

                if reason is not None:
                    nxt = k + 1
                    same_session = (
                        nxt < n
                        and et_date[nxt] == day
                        and int(et_minute[nxt]) <= session_end_min
                        and s.has_bar_at(nxt)
                    )
                    if same_session:
                        pending_exits[sym] = reason
                    else:
                        # No next bar in the session: settle at this bar's close.
                        _close_leg(leg, _close_fill(leg, b_close), now, reason, k)

        # --- 3. entry scan ----------------------------------------------------
        can_enter = (
            not halt_new_entries
            and pending_entry is None
            and minute >= entry_open_min
            and minute < eod_cut_min
            and len(legs) < int(cfg.max_open_positions)
            and daily_realized > cfg.max_daily_realized_loss
        )
        if can_enter and enforce_max_entry_attempts:
            can_enter = entry_attempts_today < int(cfg.max_entry_attempts_per_day)
        if can_enter:
            held = set(legs) | set(pending_exits)
            cand: Optional[str] = None
            score = -np.inf
            direction = 1
            if rng is not None:
                cand, score = _choose_entry_candidate_random(k, now, held)
            else:
                if not cfg.short_only:
                    if fast:
                        cand, score = _choose_entry_candidate_fast(k, now, held)
                    else:
                        cand, score = _choose_entry_candidate(_window(k), now, held)
                if cand is None and cfg.enable_short_entries and fast:
                    # Longs get first refusal unless short_only is set. Only one
                    # side is attempted per bar, mirroring the live loop's single
                    # pending entry.
                    cand, score = _choose_short_candidate_fast(k, now, held)
                    if cand is not None:
                        direction = -1
            if cand is not None:
                s = series[cand]
                px_close = float(s.close[int(s.end_pos[k]) - 1])
                if direction < 0:
                    # Sell short at the bid, offset the other way.
                    limit_px = px_close * bid_f * (1.0 - cfg.entry_limit_offset_pct)
                else:
                    bid = px_close * bid_f
                    limit_px = bid * (1.0 + cfg.entry_limit_offset_pct)
                qty = _compute_qty(limit_px, direction)
                if qty > 0 and k + 1 < n:
                    pending_entry = _PendingEntry(cand, qty, limit_px, score, direction)
                    entry_attempts_today += 1

        # --- 4. mark to market ------------------------------------------------
        equity = cash + _invested_value()
        if record_equity == "bar":
            equity_ts.append(now)
            equity_vals.append(equity)
        elif record_equity == "day" and (
            k + 1 == n
            or et_date[k + 1] != day
            # Also record on the last *in-session* bar of the day, so a session
            # followed by postmarket bars still produces exactly one point.
            or not (et_weekday[k + 1] < 5 and open_min <= int(et_minute[k + 1]) <= session_end_min)
        ):
            equity_ts.append(now)
            equity_vals.append(equity)

    # Force-flatten anything still open at the end of the data.
    if legs:
        k = n - 1
        now = timeline[k]
        for sym in list(legs):
            leg = legs[sym]
            s = series[sym]
            e = int(s.end_pos[k])
            px = float(s.close[e - 1]) if e > 0 else leg.entry_px
            _close_leg(leg, _close_fill(leg, px), now, "end_of_data", k)
        if equity_vals:
            equity_vals[-1] = cash

    curve = pd.Series(equity_vals, index=pd.DatetimeIndex(equity_ts), name="equity")
    metrics = compute_metrics(trades, curve, start_equity)
    return BacktestResult(
        trades=trades,
        equity_curve=curve,
        metrics=metrics,
        config=cfg,
        params={
            "start_equity": start_equity,
            "slippage_bps": slippage_bps,
            **xm.manifest(),
            "round_trip_cost_pct": xm.round_trip_cost_pct(),
            "spread_bps": spread_bps,
            "commission_per_order": commission_per_order,
            "whole_shares": whole_shares,
            "require_limit_fill": require_limit_fill,
            "symbols": tradable,
            "bars": int(len(bars)),
            "first_ts": timeline[0],
            "last_ts": timeline[-1],
        },
    )


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def compute_metrics(
    trades: Sequence[Trade], equity_curve: pd.Series, start_equity: float
) -> Dict[str, object]:
    """Performance stats. Sharpe/Sortino are daily-sampled and annualized (252)."""
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())

    if len(equity_curve):
        daily = equity_curve.groupby(equity_curve.index.tz_convert(ET_TZ).date).last()
        rets = daily.pct_change().dropna().to_numpy(dtype=float)
        run_max = np.maximum.accumulate(equity_curve.to_numpy(dtype=float))
        dd = (equity_curve.to_numpy(dtype=float) - run_max) / np.where(run_max == 0, 1, run_max)
        max_dd = float(dd.min() * 100.0)
        final_equity = float(equity_curve.iloc[-1])
    else:
        rets = np.array([], dtype=float)
        max_dd = 0.0
        final_equity = float(start_equity)

    def _ratio(downside_only: bool) -> float:
        if rets.size < 2:
            return 0.0
        denom_src = rets[rets < 0] if downside_only else rets
        sd = float(denom_src.std(ddof=1)) if denom_src.size >= 2 else 0.0
        if sd <= 0:
            return 0.0
        return float(rets.mean() / sd * math.sqrt(252.0))

    reasons: Dict[str, int] = {}
    per_symbol: Dict[str, float] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        per_symbol[t.symbol] = round(per_symbol.get(t.symbol, 0.0) + t.pnl, 4)

    return {
        "total_pnl": float(pnls.sum()),
        "final_equity": final_equity,
        "return_pct": (final_equity / start_equity - 1.0) * 100.0 if start_equity else 0.0,
        "trade_count": int(len(trades)),
        "win_rate": float(len(wins) / len(pnls) * 100.0) if len(pnls) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "expectancy": float(pnls.mean()) if len(pnls) else 0.0,
        "max_drawdown_pct": max_dd,
        "sharpe": _ratio(False),
        "sortino": _ratio(True),
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])) if trades else 0.0,
        "avg_mfe": float(np.mean([t.mfe for t in trades])) if trades else 0.0,
        "avg_mae": float(np.mean([t.mae for t in trades])) if trades else 0.0,
        "exit_reasons": reasons,
        "per_symbol_pnl": per_symbol,
        "trading_days": int(rets.size + 1) if rets.size else 0,
    }
