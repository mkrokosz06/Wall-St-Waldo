from dotenv import load_dotenv

load_dotenv(".env", override=True)

import pandas as pd

from bot import TradeBot


def main() -> None:
    b = TradeBot()

    bars = b._fetch_scoring_snapshot()
    print("bars_none", bars is None, "bars_empty", (bars is not None and bars.empty))
    if bars is None or bars.empty:
        return

    ranked = b._score_symbols(bars)
    print("ranked_top", ranked[:5])

    quotes = b.market.get_latest_quotes(b.config.symbols_universe)

    dyn_score_th = (
        b.state.dynamic_entry_score_threshold
        if b.state.dynamic_entry_score_threshold is not None
        else b.config.entry_score_threshold
    )
    dyn_mom_th = (
        b.state.dynamic_min_momentum_return
        if b.state.dynamic_min_momentum_return is not None
        else b.config.min_momentum_return
    )
    print("dyn_score_th", dyn_score_th, "dyn_mom_th", dyn_mom_th, "max_spread_pct", b.config.max_spread_pct)

    if not ranked:
        return

    for sym, score in ranked:
        q = quotes.get(sym)
        if not q:
            continue
        spread_pct = q.spread_pct
        spread_ok = spread_pct is not None and spread_pct <= b.config.max_spread_pct
        score_ok = score >= dyn_score_th

        df_sym = bars.xs(sym, level=0).sort_index()
        latest_close = float(df_sym["close"].iloc[-1])
        latest_ts = df_sym.index.max()
        target_time = latest_ts - pd.Timedelta(minutes=b.config.momentum_lookback_minutes)
        past_slice = df_sym[df_sym.index <= target_time]
        close_past = float(past_slice["close"].iloc[-1]) if not past_slice.empty else None
        momentum_return = (
            (latest_close / close_past) - 1.0
            if (close_past is not None and close_past > 0)
            else None
        )
        mom_ok = momentum_return is not None and momentum_return >= dyn_mom_th

        trend_ok = b._symbol_dual_ma_uptrend(bars, sym)
        print(
            f"{sym}: score={score:.6f} spread_ok={spread_ok} mom_ok={mom_ok} trend_ok={trend_ok} "
            f"spread={spread_pct:.8f} momentum={momentum_return}"
        )

    if b.config.enable_spy_market_trend_filter and "SPY" in b.config.symbols_universe:
        print("SPY trend gate:", b._symbol_dual_ma_uptrend(bars, "SPY"))


if __name__ == "__main__":
    main()

