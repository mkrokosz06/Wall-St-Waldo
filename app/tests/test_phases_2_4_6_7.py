"""
Acceptance tests for Phases 2, 4, 6 and 7 of docs/CLAUDE_IMPLEMENTATION_PLAN.md.

Phase 2 - confirmed cancellation and verified protection.
Phase 4 - one session policy for entries and closing.
Phase 6 - per-symbol freshness and quote validation.
Phase 7 - reserve portfolio risk before entering.

Offline and deterministic: fake broker, fake clock, injected sleep.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import data_validation as dv  # noqa: E402
import protection as prot  # noqa: E402
import risk_budget as rb  # noqa: E402
import session_policy as sp  # noqa: E402
from tests.fakes import FakeBroker, FakeClock  # noqa: E402

UTC = timezone.utc
ET = sp.ET


def _noop_sleep(_):
    """Injected so the retry/backoff logic never actually waits."""


# ==========================================================================
# Phase 2 - cancellation
# ==========================================================================


def _fill_on_poll(broker, n, qty, price):
    """Make the n-th get_order poll fill the order, to race the cancel."""
    calls = {"n": 0}
    real_get = broker.get_order

    def hooked(order_id):
        calls["n"] += 1
        if calls["n"] == n:
            broker.fill(order_id, qty, price)
        return real_get(order_id)

    broker.get_order = hooked


def test_cancel_ok_but_pending_cancel_then_fully_filling_is_reported_as_filled():
    """
    The plan's named case. A successful cancel request followed by a fill must
    be recorded as a fill, and the replacement must not oversell.
    """
    clock = FakeClock(datetime(2026, 9, 15, 15, 0, tzinfo=UTC))
    broker = FakeBroker(clock)
    broker.cancel_leaves_pending = True
    o = broker.submit_order(symbol="TQQQ", qty=5, side="sell", order_type="stop", stop_price=99.0)
    _fill_on_poll(broker, 2, 5.0, 98.9)

    out = prot.confirm_cancel(broker, o["id"], sleep=_noop_sleep)

    assert out.outcome == prot.CANCEL_FILLED
    assert out.filled_qty == pytest.approx(5.0)
    assert out.raced_a_fill
    assert prot.replacement_qty_after_cancel(5.0, out) == 0.0, "nothing left to replace"


def test_a_partial_fill_racing_a_cancel_is_recorded_but_earns_no_replacement():
    """
    The dangerous middle case: 2 of 5 filled and the order is still live, so
    more may yet fill. The fill must be recorded, and no replacement may be
    stacked on top - sizing one from the remaining 3 would oversell if the rest
    fills too.
    """
    clock = FakeClock(datetime(2026, 9, 15, 15, 0, tzinfo=UTC))
    broker = FakeBroker(clock)
    broker.cancel_leaves_pending = True
    o = broker.submit_order(symbol="TQQQ", qty=5, side="sell", order_type="stop", stop_price=99.0)
    _fill_on_poll(broker, 2, 2.0, 98.9)

    out = prot.confirm_cancel(broker, o["id"], max_polls=4, sleep=_noop_sleep)

    assert out.outcome == prot.CANCEL_UNRESOLVED, "partially filled and still live"
    assert out.filled_qty == pytest.approx(2.0), "the racing fill is still recorded"
    assert out.raced_a_fill
    assert prot.replacement_qty_after_cancel(5.0, out) == 0.0


def test_unresolved_cancel_yields_no_replacement_at_all():
    """An order that may still fill must not have a replacement stacked on it."""
    clock = FakeClock()
    broker = FakeBroker(clock)
    broker.cancel_leaves_pending = True
    o = broker.submit_order(symbol="TQQQ", qty=5, side="sell", order_type="stop", stop_price=99.0)

    out = prot.confirm_cancel(broker, o["id"], max_polls=3, sleep=_noop_sleep)
    assert out.outcome == prot.CANCEL_UNRESOLVED
    assert out.polls == 3
    assert prot.replacement_qty_after_cancel(5.0, out) == 0.0


def test_clean_cancel_is_confirmed():
    broker = FakeBroker()
    o = broker.submit_order(symbol="TQQQ", qty=5, side="sell", order_type="stop", stop_price=99.0)
    out = prot.confirm_cancel(broker, o["id"], sleep=_noop_sleep)
    assert out.outcome == prot.CANCEL_CONFIRMED
    assert prot.replacement_qty_after_cancel(5.0, out) == pytest.approx(5.0)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("rejected", prot.CANCEL_REJECTED),
        ("expired", prot.CANCEL_EXPIRED),
        ("replaced", prot.CANCEL_REPLACED),
    ],
)
def test_distinct_terminal_outcomes_are_distinguished(status, expected):
    broker = FakeBroker()
    o = broker.submit_order(symbol="TQQQ", qty=5, side="sell", order_type="stop", stop_price=99.0)
    broker.set_status(o["id"], status)
    out = prot.confirm_cancel(broker, o["id"], sleep=_noop_sleep)
    assert out.outcome == expected


def test_replacement_is_also_capped_by_actual_holding():
    out = prot.CancelOutcome(prot.CANCEL_CONFIRMED, "o", "canceled", 0.0, 0.0, 1)
    assert prot.replacement_qty_after_cancel(5.0, out, held_qty=3.0) == pytest.approx(3.0)
    assert prot.replacement_qty_after_cancel(5.0, out, held_qty=0.0) == 0.0


def test_a_stop_that_fills_during_cancel_leaves_nothing_to_replace():
    broker = FakeBroker()
    o = broker.submit_order(symbol="TQQQ", qty=5, side="sell", order_type="stop", stop_price=99.0)
    broker.fill(o["id"], 5.0, 98.0)
    out = prot.confirm_cancel(broker, o["id"], sleep=_noop_sleep)
    assert out.outcome == prot.CANCEL_FILLED
    assert prot.replacement_qty_after_cancel(5.0, out) == 0.0


# ==========================================================================
# Phase 2 - protection
# ==========================================================================


def _stop_order(qty, filled=0.0, status="new", symbol="TQQQ", otype="stop", oid="s1"):
    return {
        "id": oid,
        "symbol": symbol,
        "side": "sell",
        "type": otype,
        "status": status,
        "qty": str(qty),
        "filled_qty": str(filled),
    }


def test_two_share_stop_is_not_sufficient_for_five_share_holding():
    st = prot.verify_protection("TQQQ", 5.0, [_stop_order(2)])
    assert st.covered_qty == pytest.approx(2.0)
    assert st.gap_qty == pytest.approx(3.0)
    assert not st.fully_protected
    assert st.partially_protected
    assert "UNPROTECTED 3" in st.describe()


@pytest.mark.parametrize("status", ["canceled", "rejected", "expired", "replaced", "filled"])
def test_terminal_stop_ids_are_not_accepted_as_active_protection(status):
    st = prot.verify_protection("TQQQ", 5.0, [_stop_order(5, status=status)], known_ids=["s1"])
    assert st.covered_qty == 0.0
    assert st.gap_qty == pytest.approx(5.0)
    assert st.stale_ids == ["s1"], "a stale id must be reported, not silently trusted"


def test_a_sell_limit_is_not_protection():
    """
    A sell limit below market is marketable, not conditional. Counting it as
    protection would report a position as covered while it is being sold.
    """
    st = prot.verify_protection("TQQQ", 5.0, [_stop_order(5, otype="limit")])
    assert st.covered_qty == 0.0


def test_partially_filled_stop_only_covers_its_remainder():
    st = prot.verify_protection("TQQQ", 5.0, [_stop_order(5, filled=2, status="partially_filled")])
    assert st.covered_qty == pytest.approx(3.0)


def test_protection_across_two_orders_sums():
    st = prot.verify_protection("TQQQ", 5.0, [_stop_order(2, oid="a"), _stop_order(3, oid="b")])
    assert st.fully_protected
    assert sorted(st.orders) == ["a", "b"]


def test_other_symbols_do_not_protect_this_one():
    st = prot.verify_protection("TQQQ", 5.0, [_stop_order(5, symbol="SOXL")])
    assert st.covered_qty == 0.0


def test_valid_broker_stop_found_on_startup_is_reused_not_duplicated():
    found = prot.reusable_stop("TQQQ", 5.0, [_stop_order(5, oid="existing")])
    assert found is not None and found["id"] == "existing"


def test_split_protection_is_not_reusable_as_one_stop():
    assert prot.reusable_stop("TQQQ", 5.0, [_stop_order(2), _stop_order(3, oid="b")]) is None


def test_stale_known_id_does_not_suppress_recovery():
    """A stored id that is no longer live must leave the gap visible."""
    st = prot.verify_protection("TQQQ", 5.0, [], known_ids=["old-id"])
    assert st.gap_qty == pytest.approx(5.0)
    assert st.stale_ids == ["old-id"]


def test_flat_position_is_trivially_protected():
    st = prot.verify_protection("TQQQ", 0.0, [])
    assert st.fully_protected
    assert "flat" in st.describe()


# ==========================================================================
# Phase 4 - session policy
# ==========================================================================

NORMAL = [{"date": "2026-09-15", "open": "09:30", "close": "16:00"}]
EARLY = [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]


def _policy(rows, flat=5, delay=5):
    return sp.SessionPolicy.from_calendar(
        rows, end_of_day_flat_minutes=flat, market_open_delay_minutes=delay
    )


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


@pytest.mark.parametrize(
    "hh,mm,allowed,reason",
    [
        (9, 29, False, sp.BEFORE_OPEN),
        (9, 31, False, sp.IN_OPENING_DELAY),
        (9, 35, True, sp.OK),
        (15, 50, True, sp.OK),
        (15, 54, True, sp.OK),
        (15, 55, False, sp.AFTER_CUTOFF),
        (15, 59, False, sp.AFTER_CUTOFF),
        (16, 0, False, sp.AFTER_CLOSE),
        (16, 30, False, sp.AFTER_CLOSE),
    ],
)
def test_entry_eligibility_around_the_cutoff(hh, mm, allowed, reason):
    pol = _policy(NORMAL)
    ok, why = pol.entry_allowed(_et(2026, 9, 15, hh, mm))
    assert ok is allowed
    assert why == reason


def test_an_early_close_uses_its_own_cutoff_not_1555():
    pol = _policy(EARLY)
    now = _et(2026, 11, 27, 12, 56)
    assert pol.cutoff_at(now) == _et(2026, 11, 27, 12, 55)
    assert pol.entry_allowed(now) == (False, sp.AFTER_CUTOFF)
    assert pol.should_close_positions(now) is True
    # A fixed 15:55 would still permit entries here, two hours after the close.
    assert pol.entry_allowed(_et(2026, 11, 27, 13, 30)) == (False, sp.AFTER_CLOSE)
    assert pol.session_for(now).is_early_close


def test_closing_begins_on_the_first_loop_after_a_pause_past_cutoff():
    """Resuming past the cutoff must close, not open a new trade."""
    pol = _policy(NORMAL)
    now = _et(2026, 9, 15, 15, 57)
    assert pol.should_close_positions(now) is True
    assert pol.entry_allowed(now)[0] is False


def test_weekend_and_holiday_block_entries():
    pol = _policy(NORMAL)
    assert pol.entry_allowed(_et(2026, 9, 19, 12, 0)) == (False, sp.MARKET_CLOSED)
    assert pol.is_trading_day(_et(2026, 9, 19, 12, 0)) is False


def test_session_lookup_failure_blocks_entries_but_does_not_force_liquidation():
    """
    Untrusted session data must not permit entries, and must not claim the
    session is ending either - liquidating on a guess is its own error.
    """
    pol = sp.SessionPolicy.unknown(end_of_day_flat_minutes=5)
    now = _et(2026, 9, 15, 12, 0)
    assert pol.entry_allowed(now) == (False, sp.SESSION_UNKNOWN)
    assert pol.should_close_positions(now) is False
    assert "UNKNOWN" in pol.describe(now)


def test_malformed_calendar_rows_are_ignored_and_leave_policy_untrusted():
    pol = sp.SessionPolicy.from_calendar(
        [{"date": "nope", "open": "09:30", "close": "16:00"}, {"date": "2026-09-15"}]
    )
    assert pol.trusted is False
    assert pol.entry_allowed(_et(2026, 9, 15, 12, 0)) == (False, sp.SESSION_UNKNOWN)


def test_calendar_times_are_et_not_utc():
    """Treating them as UTC would shift every session by four or five hours."""
    pol = _policy(NORMAL)
    s = pol.session_for(_et(2026, 9, 15, 12, 0))
    assert s.open_at.utcoffset() == timedelta(hours=-4)  # EDT in September
    assert s.open_at.astimezone(UTC).hour == 13


def test_dst_transition_keeps_a_local_0930_open():
    """November is EST (-5); the session still opens at 09:30 local."""
    pol = _policy([{"date": "2026-11-27", "open": "09:30", "close": "13:00"}])
    s = pol.session_for(_et(2026, 11, 27, 10, 0))
    assert s.open_at.hour == 9 and s.open_at.minute == 30
    assert s.open_at.utcoffset() == timedelta(hours=-5)


def test_is_open_tracks_the_real_session_bounds():
    pol = _policy(NORMAL)
    assert pol.is_open(_et(2026, 9, 15, 9, 30)) is True
    assert pol.is_open(_et(2026, 9, 15, 15, 59)) is True
    assert pol.is_open(_et(2026, 9, 15, 16, 0)) is False
    assert pol.is_open(_et(2026, 9, 15, 9, 29)) is False


# ==========================================================================
# Phase 6 - per-symbol freshness
# ==========================================================================


def test_fresh_tqqq_and_twenty_minute_old_soxl_rejects_only_soxl():
    """The plan's named case: one bad symbol must not block a good one."""
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    fresh = now - timedelta(seconds=90)
    stale = now - timedelta(minutes=20)

    good = dv.validate_bar("TQQQ", fresh, now, max_age_sec=150)
    bad = dv.validate_bar("SOXL", stale, now, max_age_sec=150)

    assert good.ok and good.reason == dv.OK
    assert not bad.ok and bad.reason == dv.BAR_STALE
    assert bad.age_sec == pytest.approx(1200.0)
    assert "SOXL:bar_stale" in bad.describe()


def test_an_unfinished_minute_bar_is_rejected():
    """A bar starting 30s ago is still forming; using it reads a partial minute."""
    now = datetime(2026, 9, 15, 15, 0, 30, tzinfo=UTC)
    start = datetime(2026, 9, 15, 15, 0, 0, tzinfo=UTC)
    assert dv.validate_bar("TQQQ", start, now, max_age_sec=150).reason == dv.BAR_INCOMPLETE
    # Exactly one interval later it is complete.
    assert dv.validate_bar(
        "TQQQ", start, start + timedelta(seconds=60), max_age_sec=150
    ).ok


def test_bar_age_boundary_is_inclusive_of_the_limit():
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    assert dv.validate_bar("TQQQ", now - timedelta(seconds=150), now, max_age_sec=150).ok
    assert not dv.validate_bar("TQQQ", now - timedelta(seconds=151), now, max_age_sec=150).ok


def test_future_dated_bar_is_rejected():
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    chk = dv.validate_bar("TQQQ", now + timedelta(minutes=5), now, max_age_sec=150)
    assert chk.reason == dv.BAR_FUTURE


def test_missing_bars_and_untrusted_clock_are_distinct_reasons():
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    assert dv.validate_bar("TQQQ", None, now, max_age_sec=150).reason == dv.NO_BARS
    assert (
        dv.validate_bar("TQQQ", now - timedelta(seconds=90), None, max_age_sec=150).reason
        == dv.CLOCK_UNTRUSTED
    )


@pytest.mark.parametrize(
    "quote,reason",
    [
        (None, dv.NO_QUOTE),
        ({"bid": 70.0, "ask": 70.1}, dv.QUOTE_NO_TIMESTAMP),
        ({"bid": 70.2, "ask": 70.1, "t": "now"}, dv.QUOTE_CROSSED),
        ({"bid": float("nan"), "ask": 70.1, "t": "now"}, dv.QUOTE_NOT_FINITE),
        ({"bid": 0.0, "ask": 70.1, "t": "now"}, dv.QUOTE_NONPOSITIVE),
        ({"bid": -1.0, "ask": 70.1, "t": "now"}, dv.QUOTE_NONPOSITIVE),
    ],
)
def test_bad_quotes_are_rejected_with_specific_reasons(quote, reason):
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    if quote and quote.get("t") == "now":
        quote = dict(quote, t=now)
    assert dv.validate_quote("TQQQ", quote, now, max_age_sec=30).reason == reason


def test_quote_age_boundary_and_staleness():
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    ok = {"bid": 70.0, "ask": 70.1, "t": now - timedelta(seconds=30)}
    bad = {"bid": 70.0, "ask": 70.1, "t": now - timedelta(seconds=31)}
    assert dv.validate_quote("TQQQ", ok, now, max_age_sec=30).ok
    assert dv.validate_quote("TQQQ", bad, now, max_age_sec=30).reason == dv.QUOTE_STALE


def test_materially_future_quote_is_rejected_but_small_skew_is_tolerated():
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    skew = {"bid": 70.0, "ask": 70.1, "t": now + timedelta(seconds=2)}
    ahead = {"bid": 70.0, "ask": 70.1, "t": now + timedelta(minutes=5)}
    assert dv.validate_quote("TQQQ", skew, now, max_age_sec=30).ok
    assert dv.validate_quote("TQQQ", ahead, now, max_age_sec=30).reason == dv.QUOTE_FUTURE


def test_fresh_bars_with_a_stale_quote_reject_the_candidate():
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    ok, chk = dv.validate_symbol(
        "TQQQ",
        now - timedelta(seconds=90),
        {"bid": 70.0, "ask": 70.1, "t": now - timedelta(minutes=5)},
        now,
        bar_max_age_sec=150,
        quote_max_age_sec=30,
    )
    assert not ok and chk.reason == dv.QUOTE_STALE


def test_a_quote_that_expires_between_selection_and_submission_fails_revalidation():
    """The plan's case: elapsed work makes the snapshot too old to submit from."""
    t0 = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    quote = {"bid": 70.0, "ask": 70.1, "t": t0}
    assert dv.validate_quote("TQQQ", quote, t0 + timedelta(seconds=5), max_age_sec=30).ok
    assert (
        dv.validate_quote("TQQQ", quote, t0 + timedelta(seconds=45), max_age_sec=30).reason
        == dv.QUOTE_STALE
    )


# ---- the advancing clock -------------------------------------------------


def test_cached_clock_age_advances_so_old_data_cannot_stay_fresh():
    """
    The frozen-reference bug: a broker timestamp cached for 60s was used as
    "now", so within that window nothing aged.
    """
    mono = {"t": 1000.0}
    clock = dv.AdvancingClock(monotonic=lambda: mono["t"], refresh_after_sec=60)
    anchor = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    clock.set_anchor(anchor)

    assert clock.now() == anchor
    mono["t"] += 30.0
    assert clock.now() == anchor + timedelta(seconds=30), "now must advance"

    # 100s old at the anchor: fresh at +30s (130s), stale at +60s (160s).
    bar = anchor - timedelta(seconds=100)
    assert dv.validate_bar("TQQQ", bar, clock.now(), max_age_sec=150).ok
    mono["t"] += 30.0
    chk = dv.validate_bar("TQQQ", bar, clock.now(), max_age_sec=150)
    assert chk.reason == dv.BAR_STALE
    assert chk.age_sec == pytest.approx(160.0), "age must track elapsed time, not the anchor"


def test_clock_confidence_expires_and_then_authorizes_nothing():
    mono = {"t": 0.0}
    clock = dv.AdvancingClock(monotonic=lambda: mono["t"], max_confidence_sec=300)
    clock.set_anchor(datetime(2026, 9, 15, 15, 0, tzinfo=UTC))
    assert clock.trusted

    mono["t"] += 301.0
    assert not clock.trusted
    assert clock.now() is None
    assert (
        dv.validate_bar("TQQQ", datetime(2026, 9, 15, 15, 0, tzinfo=UTC), clock.now(),
                        max_age_sec=150).reason
        == dv.CLOCK_UNTRUSTED
    )


def test_clock_with_no_anchor_is_untrusted():
    clock = dv.AdvancingClock(monotonic=lambda: 0.0)
    assert clock.now() is None
    assert not clock.trusted
    assert clock.needs_refresh()


# ==========================================================================
# Phase 7 - risk reservation
# ==========================================================================


def test_floor_minus20_realized_minus12_reserved6_allows_at_most_2():
    """The plan's worked example."""
    budget = rb.RiskBudget(
        daily_loss_floor=-20.0,
        realized_today=-12.0,
        max_risk_per_trade=5.0,
        max_open_positions=3,
        max_portfolio_open_risk=100.0,
        exposures=[rb.Exposure("TQQQ", qty=6.0, basis=100.0, stop_price=99.0)],
    )
    assert budget.remaining_daily_allowance == pytest.approx(8.0)
    assert budget.reserved == pytest.approx(6.0)
    assert budget.headroom() == pytest.approx(2.0)
    assert budget.can_reserve(2.0, symbol="SOXL").allowed
    d = budget.can_reserve(2.01, symbol="SOXL")
    assert not d.allowed and d.binding == "daily_floor"


def test_two_entry_intents_cannot_each_consume_the_same_budget():
    budget = rb.RiskBudget(
        daily_loss_floor=-20.0, realized_today=-16.0, max_risk_per_trade=5.0,
        max_open_positions=3, max_portfolio_open_risk=100.0,
    )
    assert budget.headroom() == pytest.approx(4.0)
    assert budget.can_reserve(4.0, symbol="TQQQ").allowed

    # Reserving the first consumes it; the second must then be refused.
    budget.exposures.append(rb.Exposure("TQQQ", qty=4.0, basis=100.0, stop_price=99.0, pending=True))
    assert budget.reserved == pytest.approx(4.0)
    assert budget.headroom() == pytest.approx(0.0)
    assert not budget.can_reserve(4.0, symbol="SOXL").allowed


def test_a_pending_order_uses_a_slot_even_with_no_broker_holdings():
    budget = rb.RiskBudget(
        daily_loss_floor=-20.0, max_risk_per_trade=5.0, max_open_positions=1,
        exposures=[rb.Exposure("TQQQ", qty=1.0, basis=70.0, stop_price=69.0, pending=True)],
    )
    assert budget.open_slots_used == 1
    d = budget.can_reserve(0.5, symbol="SOXL")
    assert not d.allowed and d.binding == "slots"
    # Adding to the same symbol is not a new slot.
    assert budget.can_reserve(0.5, symbol="TQQQ").allowed


def test_a_partial_fill_transfers_rather_than_duplicates_its_reservation():
    budget = rb.RiskBudget(
        daily_loss_floor=-50.0, max_risk_per_trade=10.0, max_open_positions=2,
        exposures=[rb.Exposure("TQQQ", qty=5.0, basis=100.0, stop_price=99.0, pending=True)],
    )
    assert budget.reserved == pytest.approx(5.0)
    budget.fill_pending("TQQQ", 2.0, 100.0)
    assert budget.reserved == pytest.approx(5.0), "transfer, not addition"
    assert sum(1 for e in budget.exposures if e.pending) == 1
    assert sum(1 for e in budget.exposures if not e.pending) == 1


def test_a_pending_cancellation_retains_the_remainder():
    budget = rb.RiskBudget(
        daily_loss_floor=-50.0, max_risk_per_trade=10.0,
        exposures=[rb.Exposure("TQQQ", qty=5.0, basis=100.0, stop_price=99.0, pending=True)],
    )
    budget.release_pending("SOXL")  # different symbol: no effect
    assert budget.reserved == pytest.approx(5.0)
    budget.release_pending("TQQQ")
    assert budget.reserved == pytest.approx(0.0)


def test_unrealized_gain_in_one_position_does_not_offset_another():
    """A winner is not collateral: a stop above basis reserves 0, never negative."""
    winner = rb.Exposure("TQQQ", qty=5.0, basis=100.0, stop_price=105.0)
    assert winner.reserved_loss() == pytest.approx(0.0)
    budget = rb.RiskBudget(
        daily_loss_floor=-20.0, max_risk_per_trade=5.0, max_open_positions=3,
        max_portfolio_open_risk=100.0,
        exposures=[winner, rb.Exposure("SOXL", qty=2.0, basis=110.0, stop_price=108.0)],
    )
    assert budget.reserved == pytest.approx(4.0)


def test_unknown_or_absent_protection_reserves_the_whole_notional_and_blocks():
    unprotected = rb.Exposure("TQQQ", qty=5.0, basis=100.0, stop_price=None)
    assert unprotected.reserved_loss() == pytest.approx(500.0)

    budget = rb.RiskBudget(
        daily_loss_floor=-20.0, max_risk_per_trade=5.0, exposures=[unprotected]
    )
    assert budget.has_unprotected
    d = budget.can_reserve(1.0, symbol="SOXL")
    assert not d.allowed and d.binding == "unprotected"


def test_unresolved_accounting_blocks_new_risk():
    budget = rb.RiskBudget(
        daily_loss_floor=-20.0, max_risk_per_trade=5.0, unresolved_value=-3.0
    )
    d = budget.can_reserve(1.0, symbol="TQQQ")
    assert not d.allowed and d.binding == "unresolved"
    assert "unresolved" in d.reason


def test_breached_daily_floor_blocks_everything():
    budget = rb.RiskBudget(daily_loss_floor=-20.0, realized_today=-20.0, max_risk_per_trade=5.0)
    assert budget.remaining_daily_allowance == 0.0
    d = budget.can_reserve(0.01, symbol="TQQQ")
    assert not d.allowed and d.binding == "daily_floor"


def test_portfolio_cap_defaults_to_per_trade_times_slots():
    budget = rb.RiskBudget(daily_loss_floor=-500.0, max_risk_per_trade=5.0, max_open_positions=2)
    assert budget.portfolio_risk_cap == pytest.approx(10.0)
    budget.exposures = [rb.Exposure("TQQQ", qty=10.0, basis=100.0, stop_price=99.0)]
    assert budget.reserved == pytest.approx(10.0)
    d = budget.can_reserve(1.0, symbol="SOXL")
    assert not d.allowed and d.binding == "portfolio_risk"


def test_sizing_respects_risk_cash_notional_and_whole_shares():
    budget = rb.RiskBudget(
        daily_loss_floor=-100.0, max_risk_per_trade=5.0, max_open_positions=2,
        max_portfolio_open_risk=100.0,
    )
    # Risk-bound: $5 headroom / $1.05 per share = 4.76 -> 4 shares.
    assert budget.max_qty_for(70.0, 68.95, cash=10_000.0, notional_room=10_000.0) == 4.0
    # Cash-bound: $100 * 0.98 / 70 = 1.4 -> 1 share.
    assert budget.max_qty_for(70.0, 68.95, cash=100.0, notional_room=10_000.0) == 1.0
    # Notional-bound.
    assert budget.max_qty_for(70.0, 68.95, cash=10_000.0, notional_room=80.0) == 1.0
    # Cannot afford one whole share at all.
    assert budget.max_qty_for(112.0, 110.0, cash=100.0, notional_room=10_000.0) == 0.0


def test_sizing_rejects_nonsense_stop_geometry():
    budget = rb.RiskBudget(daily_loss_floor=-100.0, max_risk_per_trade=5.0)
    assert budget.max_qty_for(70.0, 70.0, cash=1000.0, notional_room=1000.0) == 0.0
    assert budget.max_qty_for(70.0, 71.0, cash=1000.0, notional_room=1000.0) == 0.0
    assert budget.max_qty_for(0.0, 0.0, cash=1000.0, notional_room=1000.0) == 0.0


def test_sizing_returns_zero_when_there_is_no_headroom():
    budget = rb.RiskBudget(daily_loss_floor=-20.0, realized_today=-20.0, max_risk_per_trade=5.0)
    assert budget.max_qty_for(70.0, 69.0, cash=10_000.0, notional_room=10_000.0) == 0.0


def test_budget_is_built_from_the_ledger_not_the_broker_position():
    from order_ledger import ROLE_ENTRY, Ledger, OrderRecord

    led = Ledger()
    led.managed["TQQQ"] = {"qty": 5.0, "basis": 5 * 100.0}
    led.day_book.add("2026-09-15", -4.0)
    led.register(
        OrderRecord(
            client_order_id="p1", symbol="SOXL", side="buy", role=ROLE_ENTRY,
            intent_qty=2.0, trade_id="t2", limit_price=110.0, status="new",
        )
    )

    budget = rb.from_ledger(
        led,
        et_date="2026-09-15",
        daily_loss_floor=-20.0,
        max_risk_per_trade=5.0,
        max_open_positions=3,
        max_portfolio_open_risk=100.0,
        stop_prices={"TQQQ": 99.0, "SOXL": 108.0},
    )
    assert budget.realized_today == pytest.approx(-4.0)
    assert budget.open_slots_used == 2, "the pending SOXL entry occupies a slot"
    assert budget.reserved == pytest.approx(5.0 + 4.0)
    assert budget.remaining_daily_allowance == pytest.approx(16.0)


def test_from_ledger_marks_positions_without_a_stop_as_unprotected():
    from order_ledger import Ledger

    led = Ledger()
    led.managed["TQQQ"] = {"qty": 5.0, "basis": 500.0}
    budget = rb.from_ledger(
        led, et_date="2026-09-15", daily_loss_floor=-20.0,
        max_risk_per_trade=5.0, max_open_positions=2, stop_prices={},
    )
    assert budget.has_unprotected
    assert not budget.can_reserve(1.0, symbol="SOXL").allowed


def test_describe_surfaces_the_blocking_conditions():
    budget = rb.RiskBudget(
        daily_loss_floor=-20.0, realized_today=-5.0, max_risk_per_trade=5.0,
        unresolved_value=-2.0,
        exposures=[rb.Exposure("TQQQ", qty=1.0, basis=70.0, stop_price=None)],
    )
    text = budget.describe()
    assert "UNRESOLVED" in text and "UNPROTECTED" in text
