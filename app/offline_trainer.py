import re
from dataclasses import dataclass
from typing import List, Tuple

from config import BotConfig
from state_store import BotState


_PNL_RE = re.compile(r"exit_pnl=([-+]?\d+\.\d+)")


def extract_recent_exit_pnls_from_log(bot_log_path: str, lookback: int) -> List[float]:
    """Most recent exit P&L values from bot.log (newest last), up to `lookback` trades."""
    return _extract_recent_trade_pnls(bot_log_path, lookback)


def _extract_recent_trade_pnls(bot_log_path: str, lookback: int) -> List[float]:
    try:
        with open(bot_log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    pnls: List[float] = []
    # Walk backwards so we only parse what we need.
    for line in reversed(lines):
        m = _PNL_RE.search(line)
        if not m:
            continue
        try:
            pnls.append(float(m.group(1)))
        except ValueError:
            continue
        if len(pnls) >= lookback:
            break
    return list(reversed(pnls))


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _ewa(pnls: List[float], decay: float = 0.9) -> float:
    """Exponential weighted average — recent trades count more. decay=0.9 → ~6.6-trade half-life."""
    if not pnls:
        return 0.0
    weights = [decay ** (len(pnls) - 1 - i) for i in range(len(pnls))]
    return sum(w * p for w, p in zip(weights, pnls)) / sum(weights)


@dataclass(frozen=True)
class TrainingResult:
    dynamic_entry_score_threshold: float
    dynamic_min_momentum_return: float
    avg_exit_pnl: float
    n_trades: int


def train_from_bot_log(cfg: BotConfig, state: BotState, bot_log_path: str = "bot.log") -> TrainingResult:
    pnls = _extract_recent_trade_pnls(bot_log_path, cfg.offline_training_lookback_trades)
    if not pnls:
        # No training data; fall back to config defaults.
        return TrainingResult(
            dynamic_entry_score_threshold=cfg.entry_score_threshold,
            dynamic_min_momentum_return=cfg.min_momentum_return,
            avg_exit_pnl=0.0,
            n_trades=0,
        )

    avg = _ewa(pnls)

    # Simple nudging rule:
    # - If avg is positive: be more aggressive (lower score cutoff; allow more negative momentum)
    # - If avg is negative: be more conservative (raise score cutoff; require momentum to be less negative)
    if avg >= 0:
        new_score = cfg.entry_score_threshold - abs(cfg.offline_training_step_score)
        new_mom = cfg.min_momentum_return - abs(cfg.offline_training_step_momentum)
    else:
        new_score = cfg.entry_score_threshold + abs(cfg.offline_training_step_score)
        new_mom = cfg.min_momentum_return + abs(cfg.offline_training_step_momentum)

    new_score = _clamp(
        new_score,
        cfg.offline_training_min_entry_score_threshold,
        cfg.offline_training_max_entry_score_threshold,
    )
    new_mom = _clamp(
        new_mom,
        cfg.offline_training_min_min_momentum_return,
        cfg.offline_training_max_min_momentum_return,
    )

    return TrainingResult(
        dynamic_entry_score_threshold=new_score,
        dynamic_min_momentum_return=new_mom,
        avg_exit_pnl=avg,
        n_trades=len(pnls),
    )

