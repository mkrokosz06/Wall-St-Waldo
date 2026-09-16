"""
Phase 1 acceptance tests: durable order tracking and exactly-once accounting.

Each test here corresponds to a named acceptance scenario in
``docs/CLAUDE_IMPLEMENTATION_PLAN.md``. They are offline and deterministic — no
network, no credentials, no real clock.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_ledger import (  # noqa: E402
    ROLE_ENTRY,
    ROLE_EXIT,
    ROLE_PROTECTIVE,
    UNRESOLVED,
    Ledger,
    OrderRecord,
    make_client_order_id,
    migrate_legacy_state,
)
from tests.fakes import BrokerError, FakeBroker, FakeClock  # noqa: E402

UTC = timezone.utc


def _rec(**kw) -> OrderRecord:
    base = dict(
        client_order_id="cid-1",
        symbol="TQQQ",
        side="buy",
        role=ROLE_ENTRY,
        intent_qty=5.0,
        trade_id="t1",
        order_id="ord-1",
        status="new",
        submitted_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        last_observed_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
    )
    base.update(kw)
    return OrderRecord(**base)


# --------------------------------------------------------------------------
# The cumulative-delta rule
# --------------------------------------------------------------------------


def test_cumulative_two_at_100_then_five_at_101_is_three_shares_for_305():
    """The exact example from the plan. Differencing avg price would give 303."""
    rec = _rec(intent_qty=5.0)
    obs = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)

    first = rec.apply_snapshot(
        {"id": "ord-1", "status": "partially_filled", "filled_qty": "2", "filled_avg_price": "100"},
        observed_at=obs,
    )
    assert len(first) == 1
    assert first[0].qty == pytest.approx(2.0)
    assert first[0].value == pytest.approx(200.0)

    second = rec.apply_snapshot(
        {"id": "ord-1", "status": "filled", "filled_qty": "5", "filled_avg_price": "101"},
        observed_at=obs,
    )
    assert len(second) == 1
    assert second[0].qty == pytest.approx(3.0)
    assert second[0].value == pytest.approx(305.0), "must be 305, not 303"
    assert second[0].avg_price == pytest.approx(305.0 / 3.0)

    assert rec.cum_qty == pytest.approx(5.0)
    assert rec.cum_value == pytest.approx(505.0)
    assert rec.avg_price == pytest.approx(101.0)


def test_repeated_identical_poll_books_nothing_further():
    rec = _rec()
    snap = {"id": "ord-1", "status": "filled", "filled_qty": "5", "filled_avg_price": "100"}
    obs = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)

    assert len(rec.apply_snapshot(dict(snap), observed_at=obs)) == 1
    for _ in range(5):
        assert rec.apply_snapshot(dict(snap), observed_at=obs) == []
    assert rec.cum_qty == pytest.approx(5.0)
    assert rec.cum_value == pytest.approx(500.0)


def test_totals_survive_a_serialization_round_trip_without_rebooking():
    """Restart after saving a fill: totals do not change again."""
    rec = _rec()
    obs = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    snap = {"id": "ord-1", "status": "filled", "filled_qty": "5", "filled_avg_price": "100"}
    rec.apply_snapshot(dict(snap), observed_at=obs)

    revived = OrderRecord.from_dict(rec.to_dict())
    assert revived.cum_qty == pytest.approx(5.0)
    assert revived.cum_value == pytest.approx(500.0)
    assert revived.apply_snapshot(dict(snap), observed_at=obs) == []


def test_cumulative_quantity_going_backwards_is_refused():
    rec = _rec()
    obs = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    rec.apply_snapshot(
        {"id": "ord-1", "status": "partially_filled", "filled_qty": "5", "filled_avg_price": "100"},
        observed_at=obs,
    )
    assert (
        rec.apply_snapshot(
            {"id": "ord-1", "status": "partially_filled", "filled_qty": "2", "filled_avg_price": "100"},
            observed_at=obs,
        )
        == []
    )
    assert rec.cum_qty == pytest.approx(5.0), "must not un-book a fill"


def test_terminal_status_arriving_with_a_fill_does_not_drop_the_fill():
    """A stop that fills and reports 'filled' in one snapshot must still book."""
    rec = _rec(role=ROLE_PROTECTIVE, side="sell", intent_qty=5.0)
    execs = rec.apply_snapshot(
        {"id": "ord-1", "status": "filled", "filled_qty": "5", "filled_avg_price": "99"},
        observed_at=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
    )
    assert len(execs) == 1
    assert rec.terminal is True


def test_individual_executions_are_preferred_and_deduplicated():
    rec = _rec(intent_qty=4.0)
    t = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    execs = [
        {"id": "e1", "qty": "1", "price": "100", "transaction_time": t.isoformat()},
        {"id": "e2", "qty": "3", "price": "102", "transaction_time": t.isoformat()},
    ]
    got = rec.apply_snapshot({"id": "ord-1", "status": "filled"}, observed_at=t, executions=execs)
    assert [e.qty for e in got] == [1.0, 3.0]
    assert rec.cum_value == pytest.approx(1 * 100 + 3 * 102)

    # Replaying the same report books nothing new.
    assert rec.apply_snapshot({"id": "ord-1", "status": "filled"}, observed_at=t, executions=execs) == []


# --------------------------------------------------------------------------
# Day attribution
# --------------------------------------------------------------------------


def test_execution_is_attributed_to_its_own_et_date_not_the_discovery_date():
    """A previous-day fill discovered today books to the previous day."""
    yesterday = datetime(2026, 9, 14, 19, 45, tzinfo=UTC)  # 15:45 ET on the 14th
    today = datetime(2026, 9, 15, 13, 35, tzinfo=UTC)

    rec = _rec(
        role=ROLE_PROTECTIVE,
        side="sell",
        submitted_at=yesterday - timedelta(hours=2),
        last_observed_at=yesterday - timedelta(hours=2),
    )
    got = rec.apply_snapshot(
        {"id": "ord-1", "status": "filled"},
        observed_at=today,
        executions=[
            {"id": "e1", "qty": "5", "price": "99", "transaction_time": yesterday.isoformat()}
        ],
    )
    assert got[0].et_date == "2026-09-14"
    assert got[0].et_date != "2026-09-15"


def test_cumulative_delta_spanning_et_dates_is_unresolved_not_stamped_to_latest():
    """
    Only cumulative data, and the unobserved window crosses a date boundary.
    The plan is explicit: do not assign the whole delta to the latest timestamp.
    """
    rec = _rec(
        role=ROLE_PROTECTIVE,
        side="sell",
        submitted_at=datetime(2026, 9, 14, 18, 0, tzinfo=UTC),
        last_observed_at=datetime(2026, 9, 14, 18, 0, tzinfo=UTC),
    )
    got = rec.apply_snapshot(
        {
            "id": "ord-1",
            "status": "filled",
            "filled_qty": "5",
            "filled_avg_price": "99",
            "filled_at": datetime(2026, 9, 15, 14, 0, tzinfo=UTC).isoformat(),
        },
        observed_at=datetime(2026, 9, 15, 14, 5, tzinfo=UTC),
    )
    assert len(got) == 1
    assert got[0].et_date == UNRESOLVED


def test_cumulative_delta_inside_one_et_date_is_attributed():
    rec = _rec(
        role=ROLE_PROTECTIVE,
        side="sell",
        submitted_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        last_observed_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
    )
    got = rec.apply_snapshot(
        {
            "id": "ord-1",
            "status": "filled",
            "filled_qty": "5",
            "filled_avg_price": "99",
            "filled_at": datetime(2026, 9, 15, 15, 0, tzinfo=UTC).isoformat(),
        },
        observed_at=datetime(2026, 9, 15, 15, 1, tzinfo=UTC),
    )
    assert got[0].et_date == "2026-09-15"


def test_previous_day_loss_does_not_consume_todays_allowance():
    led = Ledger()
    led.managed["TQQQ"] = {"qty": 5.0, "basis": 5 * 100.0}

    sell = _rec(role=ROLE_PROTECTIVE, side="sell", client_order_id="cid-stop", intent_qty=5.0,
                submitted_at=datetime(2026, 9, 14, 18, 0, tzinfo=UTC),
                last_observed_at=datetime(2026, 9, 14, 18, 0, tzinfo=UTC))
    led.register(sell)
    got = sell.apply_snapshot(
        {"id": "ord-1", "status": "filled"},
        observed_at=datetime(2026, 9, 15, 13, 35, tzinfo=UTC),
        executions=[
            {
                "id": "e1",
                "qty": "5",
                "price": "97",
                "transaction_time": datetime(2026, 9, 14, 19, 45, tzinfo=UTC).isoformat(),
            }
        ],
    )
    led.record_executions(got)

    assert led.day_book.realized_for("2026-09-14") == pytest.approx(-15.0)
    assert led.day_book.realized_for("2026-09-15") == pytest.approx(0.0)
    assert not led.day_book.has_unresolved


# --------------------------------------------------------------------------
# Partial exits, completion markers, managed basis
# --------------------------------------------------------------------------


def test_partial_stop_then_second_sell_gives_correct_total_and_one_completion():
    led = Ledger()
    buy = led.register(_rec(client_order_id="cid-buy", intent_qty=5.0))
    led.record_executions(
        buy.apply_snapshot(
            {"id": "ord-b", "status": "filled", "filled_qty": "5", "filled_avg_price": "100"},
            observed_at=datetime(2026, 9, 15, 14, 30, tzinfo=UTC),
        )
    )
    assert led.managed_qty("TQQQ") == pytest.approx(5.0)
    assert led.managed_basis("TQQQ") == pytest.approx(100.0)

    stop = led.register(
        _rec(client_order_id="cid-stop", order_id="ord-s", role=ROLE_PROTECTIVE,
             side="sell", intent_qty=5.0)
    )
    led.record_executions(
        stop.apply_snapshot(
            {"id": "ord-s", "status": "partially_filled", "filled_qty": "2", "filled_avg_price": "99"},
            observed_at=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
        )
    )
    assert led.managed_qty("TQQQ") == pytest.approx(3.0)
    assert led.day_book.realized_for("2026-09-15") == pytest.approx(-2.0)

    exit_ord = led.register(
        _rec(client_order_id="cid-exit", order_id="ord-x", role=ROLE_EXIT,
             side="sell", intent_qty=3.0)
    )
    led.record_executions(
        exit_ord.apply_snapshot(
            {"id": "ord-x", "status": "filled", "filled_qty": "3", "filled_avg_price": "98"},
            observed_at=datetime(2026, 9, 15, 15, 5, tzinfo=UTC),
        )
    )

    assert led.managed_qty("TQQQ") == pytest.approx(0.0)
    assert led.day_book.realized_for("2026-09-15") == pytest.approx(-8.0)
    assert led.day_book.mark_complete("t1") is True
    assert led.day_book.mark_complete("t1") is False, "cooldown/training must fire once"


def test_selling_without_known_basis_is_unresolved_not_zero_pnl():
    led = Ledger()
    orphan = led.register(
        _rec(client_order_id="cid-x", order_id="ord-x", role=ROLE_EXIT, side="sell", intent_qty=5.0)
    )
    led.record_executions(
        orphan.apply_snapshot(
            {"id": "ord-x", "status": "filled", "filled_qty": "5", "filled_avg_price": "50"},
            observed_at=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
        )
    )
    assert led.day_book.has_unresolved
    assert led.day_book.realized_for("2026-09-15") == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Protection is not an order id
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["canceled", "rejected", "expired", "replaced", "filled"])
def test_terminal_stop_is_not_accepted_as_protection(status):
    rec = _rec(role=ROLE_PROTECTIVE, side="sell", intent_qty=5.0, status=status)
    rec.terminal = True
    assert rec.protects("TQQQ", 5.0) is False


def test_two_share_stop_does_not_protect_five_shares():
    rec = _rec(role=ROLE_PROTECTIVE, side="sell", intent_qty=2.0, status="new")
    assert rec.protects("TQQQ", 2.0) is True
    assert rec.protects("TQQQ", 5.0) is False


def test_pending_cancel_stop_still_counts_as_live_protection():
    """pending_cancel is not terminal: it can still fill."""
    rec = _rec(role=ROLE_PROTECTIVE, side="sell", intent_qty=5.0, status="pending_cancel")
    assert rec.protects("TQQQ", 5.0) is True


def test_protection_lookup_ignores_other_symbols_and_roles():
    led = Ledger()
    led.register(_rec(client_order_id="a", symbol="SOXL", role=ROLE_PROTECTIVE, side="sell",
                      intent_qty=9.0, status="new"))
    led.register(_rec(client_order_id="b", symbol="TQQQ", role=ROLE_EXIT, side="sell",
                      intent_qty=9.0, status="new"))
    assert led.protection_for("TQQQ", 5.0) is None
    led.register(_rec(client_order_id="c", symbol="TQQQ", role=ROLE_PROTECTIVE, side="sell",
                      intent_qty=5.0, status="new"))
    assert led.protection_for("TQQQ", 5.0).client_order_id == "c"


# --------------------------------------------------------------------------
# Independent symbols and restart recovery
# --------------------------------------------------------------------------


def test_symbol_a_pending_buy_survives_symbol_b_exiting():
    led = Ledger()
    led.register(_rec(client_order_id="a-buy", symbol="TQQQ", role=ROLE_ENTRY, side="buy",
                      intent_qty=3.0, status="new"))
    b_exit = led.register(_rec(client_order_id="b-exit", symbol="SOXL", role=ROLE_EXIT,
                               side="sell", intent_qty=1.0, order_id="ord-b", status="new"))
    led.managed["SOXL"] = {"qty": 1.0, "basis": 110.0}
    led.record_executions(
        b_exit.apply_snapshot(
            {"id": "ord-b", "status": "filled", "filled_qty": "1", "filled_avg_price": "112"},
            observed_at=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
        )
    )
    still_live = [r.client_order_id for r in led.live_orders()]
    assert "a-buy" in still_live
    assert led.managed_qty("SOXL") == 0.0


def test_ledger_round_trips_through_dict():
    led = Ledger()
    rec = led.register(_rec(client_order_id="a", order_id="ord-a"))
    led.record_executions(
        rec.apply_snapshot(
            {"id": "ord-a", "status": "filled", "filled_qty": "2", "filled_avg_price": "100"},
            observed_at=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
        )
    )
    revived = Ledger.from_dict(led.to_dict())
    assert revived.managed_qty("TQQQ") == pytest.approx(2.0)
    assert revived.by_client_id("a").cum_value == pytest.approx(200.0)
    assert len(revived.executions) == 1


def test_prune_keeps_orders_with_outstanding_quantity():
    led = Ledger()
    done = led.register(_rec(client_order_id="done", intent_qty=1.0, status="filled"))
    done.cum_qty = 1.0
    done.terminal = True
    partial = led.register(_rec(client_order_id="partial", intent_qty=5.0, status="canceled"))
    partial.cum_qty = 2.0
    partial.terminal = True
    led.prune_terminal()
    assert "done" not in led.orders
    assert "partial" in led.orders, "2 of 5 shares still need reconciling"


# --------------------------------------------------------------------------
# Uncertain submission must be recovered, never duplicated
# --------------------------------------------------------------------------


def test_timeout_after_broker_accepts_is_recovered_by_client_id_not_duplicated():
    clock = FakeClock(datetime(2026, 9, 15, 14, 30, tzinfo=UTC))
    broker = FakeBroker(clock)
    broker.submit_then_fail = True

    cid = make_client_order_id("bot", "TQQQ", ROLE_ENTRY, "1")
    led = Ledger()
    rec = led.register(
        OrderRecord(
            client_order_id=cid, symbol="TQQQ", side="buy", role=ROLE_ENTRY,
            intent_qty=3.0, trade_id="t1", submitted_at=clock.utc_now(),
        )
    )
    rec.submission_uncertain = True

    with pytest.raises(BrokerError):
        broker.submit_order(symbol="TQQQ", qty=3, side="buy", client_order_id=cid)

    # The order exists at the broker. Recovery finds it by the persisted id.
    found = broker.get_order_by_client_id(cid)
    assert found is not None
    rec.apply_snapshot(found, observed_at=clock.utc_now())
    assert rec.submission_uncertain is False
    assert rec.order_id == found["id"]

    # Re-submitting the same id is refused, which is what prevents a duplicate.
    broker.submit_then_fail = False
    with pytest.raises(BrokerError, match="duplicate"):
        broker.submit_order(symbol="TQQQ", qty=3, side="buy", client_order_id=cid)
    assert len(broker.orders) == 1


def test_stop_fill_is_booked_even_though_the_position_already_vanished():
    """
    The regression that motivated this module: a stop fills between polls and
    the holding is gone by the time the bot looks. The fill must still be
    accounted for, with its own exit time.
    """
    clock = FakeClock(datetime(2026, 9, 15, 15, 0, tzinfo=UTC))
    broker = FakeBroker(clock)
    broker.set_position("TQQQ", 5.0, 100.0)

    led = Ledger()
    led.managed["TQQQ"] = {"qty": 5.0, "basis": 500.0}
    stop_raw = broker.submit_order(
        symbol="TQQQ", qty=5, side="sell", order_type="stop",
        stop_price=99.0, client_order_id="cid-stop",
    )
    stop = led.register(
        OrderRecord(
            client_order_id="cid-stop", symbol="TQQQ", side="sell", role=ROLE_PROTECTIVE,
            intent_qty=5.0, trade_id="t1", order_id=stop_raw["id"],
            status="new", submitted_at=clock.utc_now(), last_observed_at=clock.utc_now(),
        )
    )

    fill_time = clock.advance(seconds=30)
    broker.fill(stop_raw["id"], 5.0, 98.5, at=fill_time, with_execution=True)
    assert broker.list_positions() == [], "holding is gone"

    execs = led.record_executions(
        stop.apply_snapshot(
            broker.get_order(stop_raw["id"]),
            observed_at=clock.utc_now(),
            executions=broker.get_order_executions(stop_raw["id"]),
        )
    )
    assert len(execs) == 1
    assert led.day_book.realized_for("2026-09-15") == pytest.approx(-7.5)
    assert led.managed_qty("TQQQ") == 0.0
    assert execs[0].at == fill_time, "cooldown must run from the real exit time"


# --------------------------------------------------------------------------
# Failed reads must not be mistaken for absence
# --------------------------------------------------------------------------


def test_failed_or_malformed_positions_read_is_not_evidence_of_flat():
    broker = FakeBroker()
    broker.set_position("TQQQ", 5.0, 100.0)

    broker.fail_list_positions = True
    with pytest.raises(BrokerError):
        broker.list_positions()

    broker.fail_list_positions = False
    broker.malformed_positions = True
    snap = broker.list_positions()
    assert snap and "qty" not in snap[0], "malformed row must be detectable, not read as zero"

    broker.malformed_positions = False
    assert broker.list_positions()[0]["qty"] == "5.0"


# --------------------------------------------------------------------------
# Legacy migration
# --------------------------------------------------------------------------


def test_migration_recovers_legs_and_preserves_booked_pnl():
    legacy = {
        "state": "IN_POSITION",
        "day_utc": "2026-09-04",
        "daily_realized_pnl": -12.5,
        "position_legs": {
            "SOXL": {"stop_order_id": "s-1", "entry_avg_price": 116.5, "qty": 2.0},
            "TQQQ": {"stop_order_id": "s-2", "entry_avg_price": 72.41},
        },
    }
    led, notes = migrate_legacy_state(legacy)

    assert led.managed_qty("SOXL") == pytest.approx(2.0)
    assert led.managed_basis("SOXL") == pytest.approx(116.5)
    assert led.day_book.realized_for("2026-09-04") == pytest.approx(-12.5)

    # TQQQ had no quantity in legacy state: it must be flagged, not guessed.
    assert led.managed_qty("TQQQ") == 0.0
    assert any("TQQQ" in n and "quantity" in n for n in notes)

    # Legacy stops are recovered by broker id but never trusted as protection.
    stop = led.by_order_id("s-1")
    assert stop is not None and stop.submission_uncertain
    assert stop.protects("SOXL", 2.0) is False
    assert any("protection unverified" in n for n in notes)


def test_migration_does_not_replay_historical_fills():
    """Booked P&L is carried, not re-derived, so it cannot be double counted."""
    legacy = {"day_utc": "2026-09-04", "daily_realized_pnl": -12.5, "position_legs": {}}
    led, _ = migrate_legacy_state(legacy)
    assert led.executions == []
    assert led.day_book.realized_for("2026-09-04") == pytest.approx(-12.5)


def test_migration_flags_booked_pnl_with_no_day():
    led, notes = migrate_legacy_state({"daily_realized_pnl": -5.0, "day_utc": None})
    assert led.day_book.has_unresolved
    assert any("unresolved" in n for n in notes)


def test_migration_of_flat_legacy_state_is_empty_and_quiet():
    led, notes = migrate_legacy_state({"state": "FLAT", "daily_realized_pnl": 0.0})
    assert led.orders == {} and led.executions == [] and not led.day_book.has_unresolved
    assert notes == []
