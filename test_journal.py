"""Pure tests for journal.py, using the in-memory journal store (no DB)."""

from datetime import date

import pytest

import journal
from fake_journal_store import InMemoryJournalStore
from loans import emi_from_tenure

NOW = "2026-11-14T10:00:00"
TODAY = date(2026, 11, 14)


def store_with_loan(**overrides):
    store = InMemoryJournalStore()
    args = dict(name="Home loan", outstanding_lakhs=50.0, as_of="2026-10-31", annual_rate_pct=9.0, remaining_months=120)
    args.update(overrides)
    journal.setup_loan(store, NOW, **args)
    return store


# -- loan setup -----------------------------------------------------------------


def test_setup_with_remaining_months_derives_the_emi():
    store = store_with_loan()
    terms = store.terms(1)
    assert terms[0]["emi_lakhs"] == pytest.approx(emi_from_tenure(50.0, 9.0, 120))
    assert terms[0]["annual_rate_pct"] == 9.0 and terms[0]["effective_from"] == "2026-10-31"


def test_setup_with_an_explicit_emi_keeps_it():
    store = store_with_loan(emi_lakhs=0.7, remaining_months=None)
    assert store.terms(1)[0]["emi_lakhs"] == 0.7


def test_setup_creates_the_opening_balance_as_the_first_observation():
    store = store_with_loan()
    [obs] = store.observations("loan")
    assert (obs["amount_lakhs"], obs["as_of"], obs["source"], obs["ref_id"]) == (50.0, "2026-10-31", "loan_setup", 1)


def test_setup_needs_emi_or_months():
    with pytest.raises(journal.JournalError, match="emi_lakhs or remaining_months"):
        journal.setup_loan(InMemoryJournalStore(), NOW, name="x", outstanding_lakhs=1, as_of="2026-01-01", annual_rate_pct=9)


def test_a_second_loan_is_refused_for_now():
    store = store_with_loan()
    with pytest.raises(journal.JournalError, match="already set up"):
        journal.setup_loan(store, NOW, name="b", outstanding_lakhs=1, as_of="2026-01-01", annual_rate_pct=9, emi_lakhs=1)


def test_an_unparseable_date_is_rejected():
    with pytest.raises(journal.JournalError, match="as_of"):
        journal.setup_loan(InMemoryJournalStore(), NOW, name="x", outstanding_lakhs=1, as_of="31/10/2026",
                           annual_rate_pct=9, emi_lakhs=1)


# -- terms, statements, prepayments ---------------------------------------------


def test_a_terms_change_does_not_create_a_balance_observation():
    store = store_with_loan()
    journal.add_loan_terms(store, NOW, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7)
    assert len(store.terms(1)) == 2
    assert len(store.observations("loan")) == 1


def test_a_terms_change_can_optionally_record_the_statement_balance():
    store = store_with_loan()
    journal.add_loan_terms(store, NOW, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7,
                           statement_balance_lakhs=44.0)
    observations = store.observations("loan")
    assert [(o["as_of"], o["amount_lakhs"], o["source"]) for o in observations][-1] == (
        "2027-04-01", 44.0, "terms_change_statement")


def test_only_terms_record_cannot_be_voided_but_later_ones_can():
    store = store_with_loan()
    with pytest.raises(journal.JournalError, match="only terms"):
        journal.void_loan_terms(store, NOW, 1)
    journal.add_loan_terms(store, NOW, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7)
    journal.void_loan_terms(store, NOW, 2)
    assert [t["id"] for t in store.terms(1)] == [1]


def test_prepayment_is_a_ledger_entry_linked_to_the_loan():
    store = store_with_loan()
    entry_id = journal.add_prepayment(store, NOW, entry_date="2026-12-05", amount_lakhs=2.0, note="bonus")
    entry = store.entry(entry_id)
    assert (entry["kind"], entry["ref_id"], entry["amount_lakhs"], entry["status"]) == ("prepayment", 1, 2.0, "done")


def test_loan_actions_without_a_loan_are_refused():
    store = InMemoryJournalStore()
    with pytest.raises(journal.JournalError, match="No loan"):
        journal.add_prepayment(store, NOW, entry_date="2026-12-05", amount_lakhs=2.0)
    with pytest.raises(journal.JournalError, match="No loan"):
        journal.add_loan_statement(store, NOW, as_of="2026-12-05", outstanding_lakhs=2.0)


def test_loan_view_has_summary_terms_schedule_and_prepayments():
    store = store_with_loan()
    journal.add_prepayment(store, NOW, entry_date="2026-12-05", amount_lakhs=2.0)
    view = journal.loan_view(store, TODAY, 2026, 2040)
    assert view["summary"]["payoff_month"] is not None
    assert view["summary"]["total_prepaid_remaining"] == 2.0
    assert len(view["terms"]) == 1 and len(view["prepayments"]) == 1
    assert view["schedule"][0]["month"] == "2026-11"
    assert {r["month"][:4] for r in view["timeline"]} <= {str(y) for y in range(2026, 2041)}


def test_loan_view_without_a_loan_is_just_none():
    assert journal.loan_view(InMemoryJournalStore(), TODAY, 2026, 2040) == {"loan": None}


def test_prepayment_preview_reports_savings_without_saving_anything():
    store = store_with_loan()
    preview = journal.prepayment_preview(store, entry_date="2027-01-10", amount_lakhs=5.0)
    assert preview["months_saved"] > 0 and preview["interest_saved"] > 0
    assert store.entries("prepayment") == []


def test_only_loan_observation_cannot_be_voided_but_statements_can():
    store = store_with_loan()
    with pytest.raises(journal.JournalError, match="only balance observation"):
        journal.void_observation(store, NOW, 1)
    journal.add_loan_statement(store, NOW, as_of="2027-01-31", outstanding_lakhs=47.0)
    journal.void_observation(store, NOW, 2)
    assert len(store.observations("loan")) == 1


# -- check-ins ------------------------------------------------------------------


def test_check_in_records_one_observation_per_supplied_balance():
    store = store_with_loan()
    ids = journal.record_check_in(store, NOW, as_of="2026-11-14", cash_lakhs=7.0, equity_lakhs=60.0,
                                  loan_outstanding_lakhs=49.0, note="monthly")
    assert set(ids) == {"cash", "equity", "loan"}
    assert [o["bucket"] for o in store.observations() if o["as_of"] == "2026-11-14"] == ["cash", "equity", "loan"]
    assert store.observations("loan")[-1]["ref_id"] == 1
    assert store.observations("cash")[0]["ref_id"] is None


def test_check_in_with_nothing_is_refused_and_loan_balance_needs_a_loan():
    store = InMemoryJournalStore()
    with pytest.raises(journal.JournalError, match="at least one"):
        journal.record_check_in(store, NOW, as_of="2026-11-14")
    with pytest.raises(journal.JournalError, match="No loan"):
        journal.record_check_in(store, NOW, as_of="2026-11-14", loan_outstanding_lakhs=1.0)


def test_check_in_history_groups_by_date_newest_first():
    store = InMemoryJournalStore()
    journal.record_check_in(store, NOW, as_of="2026-09-01", cash_lakhs=1.0)
    journal.record_check_in(store, NOW, as_of="2026-11-01", cash_lakhs=2.0, equity_lakhs=9.0)
    history = journal.check_in_history(store)
    assert [h["as_of"] for h in history] == ["2026-11-01", "2026-09-01"]
    assert history[0]["balances"] == {"cash": 2.0, "equity": 9.0}


# -- extra income ----------------------------------------------------------------


def test_planned_and_received_income_both_count_per_year_and_multiple_per_year_add_up():
    store = InMemoryJournalStore()
    journal.add_income(store, NOW, entry_date="2027-03-01", amount_lakhs=3.0, status="planned")
    journal.add_income(store, NOW, entry_date="2027-09-01", amount_lakhs=2.0, status="received")
    journal.add_income(store, NOW, entry_date="2028-01-01", amount_lakhs=1.0)
    assert journal.extra_income_by_year(store) == {2027: 5.0, 2028: 1.0}


def test_a_voided_income_entry_no_longer_counts():
    store = InMemoryJournalStore()
    entry_id = journal.add_income(store, NOW, entry_date="2027-03-01", amount_lakhs=3.0)
    journal.void_entry(store, NOW, entry_id)
    assert journal.extra_income_by_year(store) == {}
    assert store.entry(entry_id)["voided_at"] == NOW  # voided, not deleted


def test_income_status_is_checked_and_can_be_confirmed():
    store = InMemoryJournalStore()
    with pytest.raises(journal.JournalError, match="status"):
        journal.add_income(store, NOW, entry_date="2027-03-01", amount_lakhs=1.0, status="maybe")
    entry_id = journal.add_income(store, NOW, entry_date="2027-03-01", amount_lakhs=1.0)
    journal.confirm_income(store, entry_id, "received")
    assert store.entry(entry_id)["status"] == "received"


def test_past_dated_planned_income_is_flagged_for_confirmation():
    store = InMemoryJournalStore()
    journal.add_income(store, NOW, entry_date="2026-06-01", amount_lakhs=1.0)                      # past, planned
    journal.add_income(store, NOW, entry_date="2026-06-02", amount_lakhs=1.0, status="received")   # past, received
    journal.add_income(store, NOW, entry_date="2027-06-01", amount_lakhs=1.0)                      # future, planned
    flags = {e["entry_date"]: e["needs_confirmation"] for e in journal.journal_entries(store, TODAY)}
    assert flags == {"2026-06-01": True, "2026-06-02": False, "2027-06-01": False}


def test_legacy_wrapper_replaces_the_year_with_one_approximate_mid_year_entry():
    store = InMemoryJournalStore()
    journal.add_income(store, NOW, entry_date="2027-02-01", amount_lakhs=1.0)
    journal.add_income(store, NOW, entry_date="2027-08-01", amount_lakhs=2.0)
    journal.set_extra_income_for_year(store, NOW, 2027, 9.0, "bonus")
    assert journal.extra_income_by_year(store) == {2027: 9.0}
    [entry] = store.entries("income")
    assert (entry["entry_date"], entry["approximate"]) == ("2027-07-01", True)
    assert journal.extra_income_by_year_list(store) == [{"year": 2027, "amount_lakhs": 9.0, "note": "bonus"}]
    journal.remove_extra_income_for_year(store, NOW, 2027)
    assert journal.extra_income_by_year_list(store) == []


def test_legacy_rows_migrate_once_as_approximate_planned_mid_year_entries():
    store = InMemoryJournalStore(legacy_extra_income=[
        {"year": 2028, "amount_lakhs": 4.0, "note": "sale"}, {"year": 2030, "amount_lakhs": 1.5, "note": None},
    ])
    assert journal.migrate_legacy_extra_income(store, NOW) == 2
    assert journal.extra_income_by_year(store) == {2028: 4.0, 2030: 1.5}
    entry = store.entries("income")[0]
    assert (entry["entry_date"], entry["status"], entry["approximate"], entry["source"]) == (
        "2028-07-01", "planned", True, "legacy_extra_income")
    assert journal.migrate_legacy_extra_income(store, NOW) == 0            # idempotent
    assert len(store.entries("income")) == 2


def test_a_migrated_year_that_was_since_cancelled_does_not_come_back():
    store = InMemoryJournalStore(legacy_extra_income=[{"year": 2028, "amount_lakhs": 4.0, "note": None}])
    journal.migrate_legacy_extra_income(store, NOW)
    journal.void_entry(store, NOW, store.entries("income")[0]["id"])
    assert journal.migrate_legacy_extra_income(store, NOW) == 0
    assert journal.extra_income_by_year(store) == {}


# -- transfers ------------------------------------------------------------------


def test_invest_moves_cash_to_the_instrument_and_withdraw_moves_it_back():
    store = InMemoryJournalStore()
    journal.add_transfer(store, NOW, kind="invest", entry_date="2026-11-01", amount_lakhs=3.0, bucket="equity")
    journal.add_transfer(store, NOW, kind="withdraw", entry_date="2026-11-02", amount_lakhs=1.0, bucket="debt")
    moves = [(t["from_bucket"], t["to_bucket"], t["amount_lakhs"]) for t in journal.transfers_for_projection(store)]
    assert moves == [("cash", "equity", 3.0), ("debt", "cash", 1.0)]


def test_transfer_kind_and_bucket_are_checked():
    store = InMemoryJournalStore()
    with pytest.raises(journal.JournalError, match="kind"):
        journal.add_transfer(store, NOW, kind="gift", entry_date="2026-11-01", amount_lakhs=1.0, bucket="equity")
    with pytest.raises(journal.JournalError, match="bucket"):
        journal.add_transfer(store, NOW, kind="invest", entry_date="2026-11-01", amount_lakhs=1.0, bucket="cash")


# -- projection inputs ----------------------------------------------------------


def test_an_empty_journal_gives_the_engine_nothing_extra():
    for include_actuals in (False, True):
        inputs = journal.projection_inputs(InMemoryJournalStore(), 2026, 2070, include_actuals)
        assert inputs == {"extra_income_by_year": {}, "loan_timeline": None, "observations": [], "transfers": []}


def test_plan_view_uses_loan_setup_and_terms_but_not_prepayments_or_check_ins():
    store = store_with_loan()
    journal.add_prepayment(store, NOW, entry_date="2026-12-05", amount_lakhs=2.0)
    journal.add_loan_statement(store, NOW, as_of="2027-03-31", outstanding_lakhs=30.0)
    journal.record_check_in(store, NOW, as_of="2026-11-14", cash_lakhs=7.0)
    journal.add_transfer(store, NOW, kind="invest", entry_date="2026-11-20", amount_lakhs=1.0, bucket="equity")
    plan = journal.projection_inputs(store, 2026, 2040, include_actuals=False)
    actual = journal.projection_inputs(store, 2026, 2040, include_actuals=True)
    assert plan["observations"] == [] and plan["transfers"] == []
    assert sum(r["prepayment"] for r in plan["loan_timeline"]) == 0.0
    assert sum(r["prepayment"] for r in actual["loan_timeline"]) == 2.0
    assert [o["bucket"] for o in actual["observations"]] == ["cash"]
    assert len(actual["transfers"]) == 1
    # the statement re-bases only the actual view
    assert {r["month"]: r for r in plan["loan_timeline"]}["2027-03"]["rebased"] is False
    assert {r["month"]: r for r in actual["loan_timeline"]}["2027-03"]["rebased"] is True


# -- connection threading ---------------------------------------------------------


def test_sql_journal_store_opens_a_connection_that_can_cross_worker_threads(monkeypatch):
    """Regression: FastAPI creates a `yield` dependency's value in one worker
    thread and runs the endpoint in another, so the connection must be opened
    with check_same_thread=False. (No connection is opened: the opener is
    replaced by a recorder.)"""
    import journal_data

    seen = {}

    def fake_open(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(journal_data, "get_db_connection", fake_open)
    journal_data.SqliteJournalStore()
    assert seen == {"check_same_thread": False}
