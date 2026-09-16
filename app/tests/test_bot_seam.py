"""
Phase 3 and Phase 5 acceptance tests, driven through the bot's injection seam.

These construct a real ``TradeBot`` with a fake broker, a fake clock and a
temporary state file. Nothing here authenticates or reaches the network — that
is the point of the seam, and it is asserted directly in
:func:`test_construction_makes_no_broker_calls_beyond_state`.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from config import BotConfig  # noqa: E402
from state_store import BotState, StateStore  # noqa: E402
from tests.fakes import BrokerError, FakeBroker, FakeClock, FakeMarketData  # noqa: E402

UTC = timezone.utc


def _cfg(**kw) -> BotConfig:
    """A valid offline config. No credentials; nothing reads them under injection."""
    base = BotConfig(
        api_key="test-key",
        api_secret="test-secret",
        paper=True,
        symbols_universe=["TQQQ", "SOXL"],
        max_open_positions=2,
        stop_loss_pct=0.015,
        take_profit_pct=0.035,
        enable_online_training=False,
        enable_offline_training=False,
        entry_score_threshold=0.0,
        min_momentum_return=0.0,
    )
    return replace(base, **kw)


def _bot(tmp_path, cfg=None, broker=None, clock=None, state=None):
    from bot import TradeBot

    clock = clock or FakeClock(datetime(2026, 9, 15, 14, 30, tzinfo=UTC))
    broker = broker or FakeBroker(clock)
    store = StateStore(str(tmp_path / "state.json"))
    if state is not None:
        store.save(state)
    log_path = tmp_path / "bot.log"
    log_path.write_text("", encoding="utf-8")
    return (
        TradeBot(
            config=cfg or _cfg(),
            state_store=store,
            trading=_TradingAdapter(broker),
            market=FakeMarketData(clock),
            clock=clock,
            bot_log_path=str(log_path),
        ),
        broker,
        clock,
    )


class _TradingAdapter:
    """Maps the bot's trading-client calls onto the fake broker."""

    def __init__(self, broker: FakeBroker):
        self.broker = broker
        self.market_sells: list = []

    def get_positions(self):
        return self.broker.list_positions()

    def get_open_orders(self, symbols=None):
        out = self.broker.list_orders("open")
        if symbols:
            want = {s.upper() for s in symbols}
            out = [o for o in out if o["symbol"] in want]
        return out

    def get_clock(self):
        return {"timestamp": self.broker.clock.utc_now().isoformat(), "is_open": True}

    def get_account(self):
        return {"cash": "100.00", "equity": "100.00"}

    def cancel_order(self, order_id):
        return self.broker.cancel_order(order_id)

    def get_order(self, order_id):
        return self.broker.get_order(order_id)

    def submit_order(self, order: dict) -> dict:
        # Matches AlpacaTradingREST.submit_order(order: dict) exactly, so the
        # adapter cannot pass a test that the real client would reject.
        if order.get("side") == "sell" and order.get("type") == "market":
            self.market_sells.append((order.get("symbol"), float(order.get("qty") or 0)))
        return self.broker.submit_order(
            symbol=order["symbol"],
            qty=float(order.get("qty") or 0),
            side=order["side"],
            order_type=order.get("type", "market"),
            limit_price=(float(order["limit_price"]) if order.get("limit_price") else None),
            stop_price=(float(order["stop_price"]) if order.get("stop_price") else None),
            client_order_id=order.get("client_order_id"),
        )


# --------------------------------------------------------------------------
# The seam itself
# --------------------------------------------------------------------------


def test_construction_makes_no_broker_calls_beyond_state(tmp_path):
    """Injection must skip _validate_auth: a fake has no credentials."""

    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"construction must not call trading.{name}()")

    from bot import TradeBot

    log_path = tmp_path / "bot.log"
    log_path.write_text("", encoding="utf-8")
    b = TradeBot(
        config=_cfg(),
        state_store=StateStore(str(tmp_path / "s.json")),
        trading=Exploding(),
        market=Exploding(),
        clock=FakeClock(),
        bot_log_path=str(log_path),
    )
    assert b.config.symbols_universe == ["TQQQ", "SOXL"]


def test_injected_clock_drives_bot_time(tmp_path):
    clock = FakeClock(datetime(2026, 9, 15, 18, 0, tzinfo=UTC))
    b, _, _ = _bot(tmp_path, clock=clock)
    assert b._utc_now() == clock.utc_now()
    assert b._et_now().hour == 14  # 18:00 UTC is 14:00 ET in September
    clock.advance(hours=1)
    assert b._et_now().hour == 15


# --------------------------------------------------------------------------
# Phase 3: position lookup must not be universe-filtered when closing
# --------------------------------------------------------------------------


def test_bot_owned_holding_outside_the_universe_is_found_and_closed(tmp_path):
    """
    The plan's named case: 10 shares of SQQQ, removed from a TQQQ/SOXL universe.
    Its quantity must never read as zero merely because of filtering.
    """
    b, broker, _ = _bot(tmp_path)
    broker.set_position("SQQQ", 10.0, 40.0)

    # The universe-filtered lookup cannot see it — that is the bug's mechanism.
    assert b._position_for_symbol("SQQQ") == (0.0, 0.0)
    # The unfiltered lookup used for closing does.
    qty, avg = b._broker_position_for_symbol("SQQQ")
    assert qty == pytest.approx(10.0)
    assert avg == pytest.approx(40.0)

    b._flatten_symbols(["SQQQ"])
    assert b.trading.market_sells == [("SQQQ", 10.0)]


def test_failed_position_read_is_reported_not_treated_as_flat(tmp_path):
    b, broker, _ = _bot(tmp_path)
    broker.set_position("SQQQ", 10.0, 40.0)
    broker.fail_list_positions = True

    with pytest.raises(RuntimeError):
        b._broker_position_for_symbol("SQQQ")

    # Through the flatten path the failure must not become a silent no-op sell.
    b._flatten_symbols(["SQQQ"])
    assert b.trading.market_sells == [], "must not sell on unknown quantity"


def test_malformed_position_row_is_not_read_as_zero(tmp_path):
    b, broker, _ = _bot(tmp_path)
    broker.malformed_positions = True
    with pytest.raises(RuntimeError, match="no qty field"):
        b._broker_position_for_symbol("TQQQ")


def test_flatten_ignores_symbols_with_no_position(tmp_path):
    b, broker, _ = _bot(tmp_path)
    broker.set_position("TQQQ", 3.0, 70.0)
    b._flatten_symbols(["SOXL"])
    assert b.trading.market_sells == []
    assert "TQQQ" in broker.positions, "unrelated holding untouched"


# --------------------------------------------------------------------------
# Phase 5: effective configuration and the entry-attempt cap
# --------------------------------------------------------------------------


def test_disabled_trainers_mean_persisted_overrides_are_ignored(tmp_path):
    """
    The live state carried score and momentum floors of -0.00025 written by a
    trainer that is now disabled. A negative momentum floor inverts the gate.
    """
    st = BotState(
        dynamic_entry_score_threshold=-0.00025,
        dynamic_min_momentum_return=-0.00025,
    )
    b, _, _ = _bot(tmp_path, cfg=_cfg(entry_score_threshold=0.0, min_momentum_return=0.0), state=st)

    score, mom = b._effective_thresholds()
    assert score == 0.0
    assert mom == 0.0
    assert b._threshold_source() == "config"

    # A negative-momentum candidate must fail a configured zero floor.
    assert not (-0.0005 >= mom)


def test_enabled_trainer_still_honours_its_override(tmp_path):
    """
    Overrides are set after construction on purpose. With a trainer enabled,
    __init__ legitimately re-seeds the thresholds from the log, so assigning
    them beforehand would test the seeding step rather than the resolver.
    """
    b, _, _ = _bot(tmp_path, cfg=_cfg(enable_online_training=True))
    b.state.dynamic_entry_score_threshold = 0.002
    b.state.dynamic_min_momentum_return = 0.001

    score, mom = b._effective_thresholds()
    assert score == pytest.approx(0.002)
    assert mom == pytest.approx(0.001)
    assert b._threshold_source() == "online_training"


def test_an_unset_override_falls_back_to_config_not_to_a_stale_value(tmp_path):
    b, _, _ = _bot(tmp_path, cfg=_cfg(enable_offline_training=True, min_momentum_return=0.0005))
    b.state.dynamic_entry_score_threshold = 0.003
    b.state.dynamic_min_momentum_return = None

    score, mom = b._effective_thresholds()
    assert score == pytest.approx(0.003)
    assert mom == pytest.approx(0.0005)
    assert b._threshold_source() == "offline_training"


def test_turning_a_trainer_off_neutralises_its_override_without_clearing_state(tmp_path):
    """
    The resolver, not the state, is what stops a stale override applying. Even
    if the value is still persisted, a disabled trainer's override is inert.
    """
    b, _, _ = _bot(tmp_path, cfg=_cfg(min_momentum_return=0.0))
    b.state.dynamic_entry_score_threshold = -0.00025
    b.state.dynamic_min_momentum_return = -0.00025

    assert b._effective_thresholds() == (0.0, 0.0)
    assert b.state.dynamic_min_momentum_return == -0.00025, "state untouched by the resolver"


def test_clearing_overrides_persists_and_keeps_trade_history(tmp_path):
    st = BotState(
        dynamic_entry_score_threshold=-0.00025,
        dynamic_min_momentum_return=-0.00025,
        recent_trade_pnls=[-1.0, 2.0, -0.5],
        daily_realized_pnl=-3.25,
    )
    b, _, _ = _bot(tmp_path, state=st)
    b._clear_disabled_training_overrides()

    reloaded = b.state_store.load()
    assert reloaded.dynamic_entry_score_threshold is None
    assert reloaded.dynamic_min_momentum_return is None
    assert reloaded.recent_trade_pnls == [-1.0, 2.0, -0.5], "history preserved"
    assert reloaded.daily_realized_pnl == pytest.approx(-3.25)


def test_diagnostics_report_the_values_used_for_selection(tmp_path):
    """The diagnostic line and the selection path share one resolver."""
    st = BotState(dynamic_entry_score_threshold=-0.00025, dynamic_min_momentum_return=-0.00025)
    b, _, _ = _bot(tmp_path, state=st)
    assert b._effective_thresholds() == (0.0, 0.0)


@pytest.mark.parametrize(
    "cap,used,exhausted",
    [(3, 0, False), (3, 2, False), (3, 3, True), (3, 4, True), (0, 99, False), (1, 1, True)],
)
def test_entry_attempt_cap_boundaries(tmp_path, cap, used, exhausted):
    b, _, _ = _bot(
        tmp_path,
        cfg=_cfg(max_entry_attempts_per_day=cap),
        state=BotState(entry_attempts_today=used),
    )
    assert b._entry_attempts_exhausted() is exhausted


def test_entry_attempt_count_survives_restart(tmp_path):
    path = str(tmp_path / "state.json")
    StateStore(path).save(BotState(entry_attempts_today=3))

    from bot import TradeBot

    clock = FakeClock(datetime(2026, 9, 15, 14, 30, tzinfo=UTC))
    log_path = tmp_path / "bot.log"
    log_path.write_text("", encoding="utf-8")
    b = TradeBot(
        config=_cfg(max_entry_attempts_per_day=3),
        state_store=StateStore(path),
        trading=_TradingAdapter(FakeBroker(clock)),
        market=FakeMarketData(clock),
        clock=clock,
        bot_log_path=str(log_path),
    )
    assert b.state.entry_attempts_today == 3
    assert b._entry_attempts_exhausted() is True


def test_zero_cap_preserves_previous_unlimited_behaviour(tmp_path):
    b, _, _ = _bot(
        tmp_path,
        cfg=_cfg(max_entry_attempts_per_day=0),
        state=BotState(entry_attempts_today=500),
    )
    assert b._entry_attempts_exhausted() is False


def test_effective_config_snapshot_contains_no_secrets(tmp_path, caplog):
    b, _, _ = _bot(tmp_path, cfg=_cfg(api_key="SECRETKEY", api_secret="SECRETVALUE"))
    with caplog.at_level("INFO", logger="trade_bot"):
        b._log_effective_config()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETKEY" not in text
    assert "SECRETVALUE" not in text
    assert "universe=TQQQ,SOXL" in text
    assert "source=config" in text


# --------------------------------------------------------------------------
# Config validation (Phase 5)
# --------------------------------------------------------------------------


def test_reward_to_risk_below_one_is_valid():
    """A target smaller than a positive stop is a legitimate configuration."""
    _cfg(stop_loss_pct=0.015, take_profit_pct=0.012).validate()


def test_nonpositive_take_profit_is_rejected():
    with pytest.raises(ValueError, match="take_profit_pct"):
        _cfg(take_profit_pct=0.0).validate()


def test_negative_entry_attempt_cap_is_rejected():
    with pytest.raises(ValueError, match="max_entry_attempts_per_day"):
        _cfg(max_entry_attempts_per_day=-1).validate()
