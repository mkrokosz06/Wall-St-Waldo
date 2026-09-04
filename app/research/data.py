"""
Historical minute-bar cache for offline backtesting.

WHY a cache: the backtester replays the same months of 1-minute bars hundreds of
times during a parameter sweep. Re-downloading from Alpaca every run would be
slow, rate-limited, and non-reproducible (Alpaca can revise/backfill bars). So we
download once into parquet and never touch the network for a range we already
hold.

Shape contract
--------------
The live bot consumes bars as whatever `alpaca-py`'s ``resp.df`` returns from
``StockBarsRequest``: a DataFrame with a 2-level MultiIndex ``(symbol,
timestamp)`` and columns including ``open/high/low/close/volume``. Every consumer
in the live path does ``bars_df.xs(sym, level=0).sort_index()``. This module
reproduces that shape exactly so `strategy_signals` helpers run unchanged against
cached data.

Timezones: the cache stores tz-aware **UTC** timestamps (that is what Alpaca
returns). Session logic (market open/close, EOD flat) needs America/New_York, so
use :func:`to_et` / :func:`et_index` at the point of use rather than converting
the cache — mixing tz conventions in stored data is how off-by-one-hour DST bugs
get in.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import pandas as pd

import sys as _sys
from pathlib import Path as _Path

# Allow both `python -m app.research.x` and `python app/research/x.py` by making
# the `app/` directory importable (that is where config/strategy_signals live).
_APP_DIR = str(_Path(__file__).resolve().parent.parent)
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

try:  # pragma: no cover - import shape differs when run as a script vs package
    from ..config import ET_TZ
except ImportError:  # pragma: no cover
    from config import ET_TZ  # type: ignore[no-redef]

UTC = dt.timezone.utc

BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

#: Directory holding the parquet cache. One file per symbol + requested range.
CACHE_DIR = Path(__file__).resolve().parent / "data"

#: Alpaca will happily stream a year of minute bars, but memory and retry cost
#: are both better behaved if we ask in chunks.
_CHUNK_DAYS = 30


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _as_utc(value: dt.datetime | dt.date | str) -> dt.datetime:
    """Coerce anything date-like into a tz-aware UTC datetime."""
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return dt.datetime(value.year, value.month, value.day, tzinfo=UTC)


def _safe(sym: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sym.upper())


def _cache_path(
    symbol: str, start: dt.datetime, end: dt.datetime, feed: str, adjustment: str
) -> Path:
    # The adjustment is part of the key: split-adjusted and raw bars for the same
    # symbol and range are different data, and mixing them silently is exactly
    # the kind of error a backtest cannot detect on its own.
    key = (
        f"{_safe(symbol)}_{start:%Y%m%d}_{end:%Y%m%d}_"
        f"{feed.lower()}_{adjustment.lower()}_1min.parquet"
    )
    return CACHE_DIR / key


def _empty_frame() -> pd.DataFrame:
    idx = pd.MultiIndex.from_arrays(
        [pd.Index([], dtype=object), pd.DatetimeIndex([], tz="UTC")],
        names=["symbol", "timestamp"],
    )
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}, index=idx)


def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Force the alpaca-py frame into the canonical (symbol, timestamp) shape."""
    if df is None or len(df) == 0:
        return _empty_frame()

    out = df.copy()
    if not isinstance(out.index, pd.MultiIndex):
        # Single-symbol responses occasionally come back with a plain DatetimeIndex.
        out.index = pd.MultiIndex.from_arrays(
            [[symbol.upper()] * len(out), pd.DatetimeIndex(out.index)],
            names=["symbol", "timestamp"],
        )
    out.index = out.index.set_names(["symbol", "timestamp"])

    ts = out.index.get_level_values(1)
    ts = pd.DatetimeIndex(ts)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    syms = pd.Index(out.index.get_level_values(0)).astype(str).str.upper()
    out.index = pd.MultiIndex.from_arrays([syms, ts], names=["symbol", "timestamp"])

    missing = [c for c in BAR_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"bar frame for {symbol} missing columns {missing}")
    out = out[list(BAR_COLUMNS)].astype("float64")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _client(api_key: Optional[str] = None, api_secret: Optional[str] = None):
    """Build a StockHistoricalDataClient, loading `app/.env` if keys aren't passed."""
    from alpaca.data.historical import StockHistoricalDataClient

    if not api_key or not api_secret:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        api_key = os.environ["ALPACA_API_KEY"].strip().strip('"').strip("'")
        api_secret = os.environ["ALPACA_API_SECRET"].strip().strip('"').strip("'")
    return StockHistoricalDataClient(api_key, api_secret)


def _download_chunked(
    symbol: str,
    start: dt.datetime,
    end: dt.datetime,
    feed: str,
    client,
    adjustment: str = "all",
) -> pd.DataFrame:
    """
    Download one symbol in <= _CHUNK_DAYS slices. alpaca-py handles pagination.

    ``adjustment`` defaults to ``all`` (splits + dividends). Alpaca's default is
    RAW, and RAW is a trap for this universe: leveraged ETFs reverse-split often
    (SOXS split twice in eight months), and an unadjusted split shows up as a
    single +1900% bar that a momentum strategy reads as the trade of the century.
    """
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    feed_enum = {"sip": DataFeed.SIP, "iex": DataFeed.IEX}[feed.lower()]
    adj_enum = {
        "raw": Adjustment.RAW,
        "split": Adjustment.SPLIT,
        "dividend": Adjustment.DIVIDEND,
        "all": Adjustment.ALL,
    }[adjustment.lower()]

    parts: List[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + dt.timedelta(days=_CHUNK_DAYS), end)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Minute,
            start=cursor,
            end=chunk_end,
            feed=feed_enum,
            adjustment=adj_enum,
        )
        resp = client.get_stock_bars(req)
        part = _normalize(getattr(resp, "df", None), symbol)
        if len(part):
            parts.append(part)
        cursor = chunk_end

    if not parts:
        return _empty_frame()
    out = pd.concat(parts)
    return out[~out.index.duplicated(keep="last")].sort_index()


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def fetch_minute_bars(
    symbols: Sequence[str],
    start: dt.datetime | dt.date | str,
    end: dt.datetime | dt.date | str,
    feed: str = "sip",
    *,
    adjustment: str = "all",
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    use_cache: bool = True,
    refresh: bool = False,
) -> pd.DataFrame:
    """
    Return 1-minute bars for ``symbols`` between ``start`` and ``end`` (UTC).

    The result has the same shape the live bot sees from Alpaca: MultiIndex
    ``(symbol, timestamp)`` with ``open/high/low/close/volume`` columns and
    tz-aware UTC timestamps, sorted.

    SIP (default) includes pre/post-market minutes; IEX does not. Bars are
    split- and dividend-adjusted by default; see ``_download_chunked``. A cached
    symbol+range+feed+adjustment is never re-downloaded unless ``refresh=True``.
    """
    start_dt = _as_utc(start)
    end_dt = _as_utc(end)
    if end_dt <= start_dt:
        raise ValueError(f"end ({end_dt}) must be after start ({start_dt})")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    syms = [s.strip().upper() for s in symbols if s and s.strip()]

    client = None
    frames: List[pd.DataFrame] = []
    for sym in syms:
        path = _cache_path(sym, start_dt, end_dt, feed, adjustment)
        if use_cache and not refresh and path.exists():
            frames.append(_normalize(pd.read_parquet(path), sym))
            continue
        if client is None:
            client = _client(api_key, api_secret)
        df = _download_chunked(sym, start_dt, end_dt, feed, client, adjustment)
        if use_cache:
            df.to_parquet(path)
        frames.append(df)

    if not frames:
        return _empty_frame()
    out = pd.concat(frames).sort_index()
    return out


def load_cached(
    symbols: Sequence[str],
    start: dt.datetime | dt.date | str,
    end: dt.datetime | dt.date | str,
    feed: str = "sip",
    *,
    adjustment: str = "all",
) -> pd.DataFrame:
    """Cache-only load. Raises if any symbol's range was never downloaded."""
    start_dt, end_dt = _as_utc(start), _as_utc(end)
    missing = [
        s.upper()
        for s in symbols
        if not _cache_path(s.upper(), start_dt, end_dt, feed, adjustment).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"no cached bars for {missing} over {start_dt:%Y-%m-%d}..{end_dt:%Y-%m-%d} "
            f"({feed}/{adjustment}); run fetch_minute_bars first"
        )
    return fetch_minute_bars(
        symbols, start_dt, end_dt, feed, adjustment=adjustment, use_cache=True
    )


def to_et(ts: pd.Timestamp | dt.datetime) -> dt.datetime:
    """UTC bar timestamp -> America/New_York, for session-gate comparisons."""
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(ET_TZ)


def et_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Vectorized UTC -> ET conversion for a whole timestamp index."""
    idx = pd.DatetimeIndex(index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    return idx.tz_convert(ET_TZ)


def restrict_to_regular_hours(bars: pd.DataFrame) -> pd.DataFrame:
    """Keep only 09:30-16:00 ET minutes (what the live bot trades by default)."""
    ts_et = et_index(bars.index.get_level_values(1))
    minute_of_day = ts_et.hour * 60 + ts_et.minute
    mask = (minute_of_day >= 9 * 60 + 30) & (minute_of_day <= 16 * 60)
    return bars[mask]


def describe(bars: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol bar counts and coverage — a quick sanity check after a fetch."""
    rows = []
    for sym in bars.index.get_level_values(0).unique():
        d = bars.xs(sym, level=0)
        rows.append(
            {
                "symbol": sym,
                "bars": len(d),
                "first": d.index.min(),
                "last": d.index.max(),
                "sessions": len(set(et_index(d.index).date)),
            }
        )
    return pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)


def _main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Download + cache 1-minute bars.")
    p.add_argument("--symbols", default="TQQQ,SOXL,SQQQ,UVXY,SOXS,SPY")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-09-01")
    p.add_argument("--feed", default="sip")
    p.add_argument("--refresh", action="store_true")
    a = p.parse_args(list(argv) if argv is not None else None)

    bars = fetch_minute_bars(
        [s for s in a.symbols.split(",")], a.start, a.end, a.feed, refresh=a.refresh
    )
    print(describe(bars).to_string(index=False))
    print(f"\ncache dir: {CACHE_DIR}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
