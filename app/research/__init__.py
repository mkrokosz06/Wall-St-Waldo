"""Offline research tooling (data cache, backtest engine, parameter sweeps).

Nothing in this package is imported by the live bot; it is read-only with
respect to `bot.py` / `config.py` so research can never break live trading.
"""
