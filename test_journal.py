"""Pure tests for journal.py, using the in-memory journal store (no DB)."""

from datetime import date

import pytest

import journal
from fake_journal_store import InMemoryJournalStore
from loans import build_timeline, emi_from_tenure

NOW = "2030-01-01T10:00:00"      # late enough that every example date below is in the past
NOW_2026 = "2026-11-14T10:00:00"
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


def test_an_unparseable_date_is_rejected():
    with pytest.raises(journal.JournalError, match="as_of"):
        journal.setup_loan(InMemoryJournalStore(), NOW, name="x", outstanding_lakhs=1, as_of="31/10/2026",
                           annual_rate_pct=9, emi_lakhs=1)


# -- terms, statements, prepayments ---------------------------------------------


def test_a_terms_change_does_not_create_a_balance_observation():
    store = store_with_loan()
    journal.add_loan_terms(store, NOW, loan_id=1, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7)
    assert len(store.terms(1)) == 2
    assert len(store.observations("loan")) == 1


def test_a_terms_change_can_optionally_record_the_statement_balance():
    store = store_with_loan()
    journal.add_loan_terms(store, NOW, loan_id=1, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7,
                           statement_balance_lakhs=44.0)
    observations = store.observations("loan")
    assert [(o["as_of"], o["amount_lakhs"], o["source"]) for o in observations][-1] == (
        "2027-04-01", 44.0, "terms_change_statement")


def test_only_terms_record_cannot_be_voided_but_later_ones_can():
    store = store_with_loan()
    with pytest.raises(journal.JournalError, match="only terms"):
        journal.void_loan_terms(store, NOW, 1)
    journal.add_loan_terms(store, NOW, loan_id=1, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7)
    journal.void_loan_terms(store, NOW, 2)
    assert [t["id"] for t in store.terms(1)] == [1]


def test_prepayment_is_a_ledger_entry_linked_to_the_loan():
    store = store_with_loan()
    entry_id = journal.add_prepayment(store, NOW, loan_id=1, entry_date="2026-12-05", amount_lakhs=2.0, note="bonus")
    entry = store.entry(entry_id)
    assert (entry["kind"], entry["ref_id"], entry["amount_lakhs"], entry["status"]) == ("prepayment", 1, 2.0, "done")


def test_loan_actions_against_an_unknown_loan_are_refused():
    store = InMemoryJournalStore()
    with pytest.raises(journal.JournalError, match="Unknown loan"):
        journal.add_prepayment(store, NOW, loan_id=1, entry_date="2026-12-05", amount_lakhs=2.0)
    with pytest.raises(journal.JournalError, match="Unknown loan"):
        journal.add_loan_statement(store, NOW, loan_id=1, as_of="2026-12-05", outstanding_lakhs=2.0)
    store = store_with_loan()
    with pytest.raises(journal.JournalError, match="Unknown loan"):
        journal.add_prepayment(store, NOW, loan_id=99, entry_date="2026-12-05", amount_lakhs=2.0)


def test_loan_views_have_summary_terms_schedule_and_prepayments_per_loan():
    store = store_with_loan()
    journal.add_prepayment(store, NOW, loan_id=1, entry_date="2026-12-05", amount_lakhs=2.0)
    data = journal.loan_views(store, TODAY, 2026, 2040)
    [view] = data["loans"]
    assert data["combined"] is None                                   # totals only make sense for 2+ loans
    assert view["summary"]["payoff_month"] is not None
    assert view["summary"]["total_prepaid_remaining"] == 2.0
    assert view["summary"]["monthly_emi"] == pytest.approx(emi_from_tenure(50.0, 9.0, 120))
    assert len(view["terms"]) == 1 and len(view["prepayments"]) == 1
    assert view["schedule"][0]["month"] == "2026-11"
    assert {r["month"][:4] for r in view["timeline"]} <= {str(y) for y in range(2026, 2041)}


def test_loan_views_without_a_loan_are_empty():
    assert journal.loan_views(InMemoryJournalStore(), TODAY, 2026, 2040) == {"loans": [], "combined": None}


def test_prepayment_preview_reports_savings_without_saving_anything():
    store = store_with_loan()
    preview = journal.prepayment_preview(store, loan_id=1, entry_date="2027-01-10", amount_lakhs=5.0)
    assert preview["months_saved"] > 0 and preview["interest_saved"] > 0
    assert store.entries("prepayment") == []


# -- check-ins ------------------------------------------------------------------


def test_check_in_records_one_observation_per_supplied_balance():
    store = store_with_loan()
    ids = journal.record_check_in(store, NOW, as_of="2026-11-14", cash_lakhs=7.0, equity_lakhs=60.0,
                                  loan_balances={1: 49.0}, note="monthly")
    assert set(ids) == {"cash", "equity", "loan_1"}
    assert [o["bucket"] for o in store.observations() if o["as_of"] == "2026-11-14"] == ["cash", "equity", "loan"]
    assert store.observations("loan")[-1]["ref_id"] == 1
    assert store.observations("cash")[0]["ref_id"] is None


def test_check_in_with_nothing_is_refused_and_loan_balance_needs_a_loan():
    store = InMemoryJournalStore()
    with pytest.raises(journal.JournalError, match="at least one"):
        journal.record_check_in(store, NOW, as_of="2026-11-14")
    with pytest.raises(journal.JournalError, match="Unknown loan"):
        journal.record_check_in(store, NOW, as_of="2026-11-14", loan_balances={1: 1.0})


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


def test_the_old_year_only_table_is_dropped_when_it_is_empty():
    store = InMemoryJournalStore()
    assert journal.retire_legacy_extra_income_table(store) == "dropped"
    assert store.legacy_table_dropped and store.commits == 1


def test_the_old_table_is_never_dropped_while_it_still_holds_rows():
    store = InMemoryJournalStore(legacy_extra_income=[{"year": 2028, "amount_lakhs": 4.0, "note": "sale"}])
    assert journal.retire_legacy_extra_income_table(store) == "kept"
    assert not store.legacy_table_dropped and store.legacy_extra_income_rows() != []
    assert store.entries("income") == []                              # and nothing is copied into the ledger behind your back


def test_retiring_the_old_table_does_not_touch_the_ledger():
    store = InMemoryJournalStore()
    journal.add_income(store, NOW, entry_date="2027-03-01", amount_lakhs=3.0)
    journal.retire_legacy_extra_income_table(store)
    assert journal.extra_income_by_year(store) == {2027: 3.0}


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
    journal.add_prepayment(store, NOW, loan_id=1, entry_date="2026-12-05", amount_lakhs=2.0)
    journal.add_loan_statement(store, NOW, loan_id=1, as_of="2027-03-31", outstanding_lakhs=30.0)
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


# -- future-dated entries ------------------------------------------------------------------
# Things that have happened can't be dated in the future; planned things can.


def test_recorded_facts_cannot_be_dated_in_the_future():
    store = store_with_loan(as_of="2026-10-31")
    for call in (
        lambda: journal.record_check_in(store, NOW_2026, as_of="2026-11-15", cash_lakhs=1.0),
        lambda: journal.add_prepayment(store, NOW_2026, loan_id=1, entry_date="2026-12-01", amount_lakhs=1.0),
        lambda: journal.add_loan_statement(store, NOW_2026, loan_id=1, as_of="2027-01-31", outstanding_lakhs=40.0),
        lambda: journal.add_transfer(store, NOW_2026, kind="invest", entry_date="2026-11-30", amount_lakhs=1.0, bucket="equity"),
        lambda: journal.setup_loan(InMemoryJournalStore(), NOW_2026, name="x", outstanding_lakhs=1, as_of="2026-12-01",
                                   annual_rate_pct=9, emi_lakhs=1),
    ):
        with pytest.raises(journal.JournalError, match="in the future"):
            call()


def test_nothing_is_stored_when_a_future_date_is_refused():
    store = store_with_loan(as_of="2026-10-31")
    before = (len(store.observations()), len(store.entries()))
    with pytest.raises(journal.JournalError):
        journal.record_check_in(store, NOW_2026, as_of="2027-01-01", cash_lakhs=1.0, equity_lakhs=2.0)
    assert (len(store.observations()), len(store.entries())) == before


def test_today_itself_is_allowed():
    store = store_with_loan(as_of="2026-10-31")
    journal.record_check_in(store, NOW_2026, as_of="2026-11-14", cash_lakhs=1.0)
    journal.add_prepayment(store, NOW_2026, loan_id=1, entry_date="2026-11-14", amount_lakhs=1.0)


def test_planned_things_may_still_be_dated_in_the_future():
    store = store_with_loan(as_of="2026-10-31")
    journal.add_income(store, NOW_2026, entry_date="2028-05-01", amount_lakhs=4.0)
    journal.add_loan_terms(store, NOW_2026, loan_id=1, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7)
    assert journal.extra_income_by_year(store) == {2028: 4.0}


def test_a_statement_balance_with_a_terms_change_counts_as_a_recorded_fact():
    store = store_with_loan(as_of="2026-10-31")
    with pytest.raises(journal.JournalError, match="in the future"):
        journal.add_loan_terms(store, NOW_2026, loan_id=1, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7,
                               statement_balance_lakhs=44.0)


# -- editing and deleting -----------------------------------------------------------------


def group_ids(store, as_of):
    return {journal._observation_key(o): o["id"] for o in store.observations() if o["as_of"] == as_of}


def test_editing_a_check_in_replaces_changed_balances_and_keeps_the_old_as_voided():
    store = InMemoryJournalStore()
    journal.record_check_in(store, NOW, as_of="2026-09-01", cash_lakhs=5.0, equity_lakhs=50.0, note="first")
    ids = group_ids(store, "2026-09-01")
    journal.edit_check_in(store, NOW, ids=ids, as_of="2026-09-02", cash_lakhs=6.0, equity_lakhs=50.0, note="fixed")
    live = {o["bucket"]: (o["as_of"], o["amount_lakhs"], o["note"]) for o in store.observations()}
    assert live == {"cash": ("2026-09-02", 6.0, "fixed"), "equity": ("2026-09-02", 50.0, "fixed")}
    assert {o["id"] for o in store._observations} > set(ids.values())     # the old rows are still stored (voided)
    assert all(o["voided_at"] for o in store._observations if o["id"] in ids.values())


def test_editing_a_check_in_with_nothing_changed_stores_nothing_new():
    store = InMemoryJournalStore()
    journal.record_check_in(store, NOW, as_of="2026-09-01", cash_lakhs=5.0, note="n")
    before = len(store._observations)
    journal.edit_check_in(store, NOW, ids=group_ids(store, "2026-09-01"), as_of="2026-09-01", cash_lakhs=5.0, note="n")
    assert len(store._observations) == before


def test_editing_a_check_in_can_add_and_remove_buckets():
    store = InMemoryJournalStore()
    journal.record_check_in(store, NOW, as_of="2026-09-01", cash_lakhs=5.0, equity_lakhs=50.0)
    journal.edit_check_in(store, NOW, ids=group_ids(store, "2026-09-01"), as_of="2026-09-01",
                          cash_lakhs=5.0, debt_lakhs=20.0)
    assert {o["bucket"]: o["amount_lakhs"] for o in store.observations()} == {"cash": 5.0, "debt": 20.0}


def test_editing_a_check_in_to_nothing_asks_for_a_delete_instead():
    store = InMemoryJournalStore()
    journal.record_check_in(store, NOW, as_of="2026-09-01", cash_lakhs=5.0)
    with pytest.raises(journal.JournalError, match="delete it instead"):
        journal.edit_check_in(store, NOW, ids=group_ids(store, "2026-09-01"), as_of="2026-09-01")
    assert len(store.observations()) == 1


def test_editing_a_check_in_keeps_the_source_of_replaced_loan_balances():
    store = store_with_loan(as_of="2026-10-31")
    ids = group_ids(store, "2026-10-31")
    journal.edit_check_in(store, NOW, ids=ids, as_of="2026-10-30", loan_balances={1: 49.0})
    [loan_obs] = store.observations("loan")
    assert (loan_obs["source"], loan_obs["amount_lakhs"], loan_obs["as_of"]) == ("loan_setup", 49.0, "2026-10-30")


def test_a_check_in_edit_cannot_remove_the_loans_only_balance_or_use_future_dates_or_foreign_ids():
    store = store_with_loan(as_of="2026-10-31")
    journal.record_check_in(store, NOW, as_of="2026-10-31", cash_lakhs=5.0)
    ids = group_ids(store, "2026-10-31")
    with pytest.raises(journal.JournalError, match="only balance"):
        journal.edit_check_in(store, NOW, ids=ids, as_of="2026-10-31", cash_lakhs=5.0)         # drops the loan balance
    with pytest.raises(journal.JournalError, match="in the future"):
        journal.edit_check_in(store, NOW_2026, ids=ids, as_of="2027-01-01", cash_lakhs=5.0, loan_balances={1: 50.0})
    with pytest.raises(journal.JournalError, match="Unknown balance"):
        journal.edit_check_in(store, NOW, ids={"cash": 999}, as_of="2026-10-31", cash_lakhs=1.0)
    assert len(store.observations()) == 2


def test_deleting_a_check_in_voids_every_balance_in_it():
    store = InMemoryJournalStore()
    journal.record_check_in(store, NOW, as_of="2026-09-01", cash_lakhs=5.0, equity_lakhs=50.0)
    journal.record_check_in(store, NOW, as_of="2026-10-01", cash_lakhs=6.0)
    journal.void_check_in(store, NOW, group_ids(store, "2026-09-01"))
    assert [(o["as_of"], o["bucket"]) for o in store.observations()] == [("2026-10-01", "cash")]
    assert len(store._observations) == 3                                  # kept, voided


def test_deleting_the_check_in_that_holds_the_loans_only_balance_is_refused_and_changes_nothing():
    store = store_with_loan(as_of="2026-10-31")
    journal.record_check_in(store, NOW, as_of="2026-10-31", cash_lakhs=5.0)
    with pytest.raises(journal.JournalError, match="only balance"):
        journal.void_check_in(store, NOW, group_ids(store, "2026-10-31"))
    assert len(store.observations()) == 2
    journal.add_loan_statement(store, NOW, loan_id=1, as_of="2027-01-31", outstanding_lakhs=45.0)
    journal.void_check_in(store, NOW, group_ids(store, "2026-10-31"))     # another loan balance exists now
    assert [o["bucket"] for o in store.observations()] == ["loan"]


def test_editing_loan_terms_replaces_the_record_and_keeps_the_history():
    store = store_with_loan()
    journal.add_loan_terms(store, NOW, loan_id=1, effective_from="2027-04-01", annual_rate_pct=10.0, emi_lakhs=0.7)
    journal.edit_loan_terms(store, NOW, 2, effective_from="2027-05-01", annual_rate_pct=9.5, emi_lakhs=0.65, note="fixed")
    live = [(t["effective_from"], t["annual_rate_pct"], t["emi_lakhs"], t["note"]) for t in store.terms(1)]
    assert live[-1] == ("2027-05-01", 9.5, 0.65, "fixed") and len(live) == 2
    assert len(store._terms) == 3 and store._terms[1]["voided_at"]
    with pytest.raises(journal.JournalError, match="Unknown loan terms"):
        journal.edit_loan_terms(store, NOW, 2, effective_from="2027-05-01", annual_rate_pct=9.5, emi_lakhs=0.65)


def test_the_opening_terms_can_be_edited_even_though_they_cannot_be_deleted():
    store = store_with_loan()
    journal.edit_loan_terms(store, NOW, 1, effective_from="2026-10-31", annual_rate_pct=8.5, emi_lakhs=0.62)
    assert [t["annual_rate_pct"] for t in store.terms(1)] == [8.5]


def test_editing_a_prepayment_replaces_it_and_keeps_the_link_to_the_loan():
    store = store_with_loan()
    entry_id = journal.add_prepayment(store, NOW, loan_id=1, entry_date="2026-12-05", amount_lakhs=2.0)
    journal.edit_entry(store, NOW, entry_id, entry_date="2026-12-06", amount_lakhs=2.5, note="bonus")
    [live] = store.entries("prepayment")
    assert (live["entry_date"], live["amount_lakhs"], live["note"], live["ref_id"]) == ("2026-12-06", 2.5, "bonus", 1)
    assert store.entry(entry_id)["voided_at"] == NOW


def test_editing_income_changes_amount_status_and_clears_the_approximate_flag_only_if_the_date_moves():
    store = InMemoryJournalStore()
    income_id = journal.add_income(store, NOW, entry_date="2028-07-01", amount_lakhs=4.0, note="sale", approximate=True)
    journal.edit_entry(store, NOW, income_id, entry_date="2028-07-01", amount_lakhs=5.0, note="sale", status="planned")
    assert store.entries("income")[0]["approximate"] is True and store.entries("income")[0]["amount_lakhs"] == 5.0
    new_id = store.entries("income")[0]["id"]
    journal.edit_entry(store, NOW, new_id, entry_date="2028-09-15", amount_lakhs=5.0, status="received")
    [live] = store.entries("income")
    assert (live["entry_date"], live["approximate"], live["status"]) == ("2028-09-15", False, "received")
    assert journal.extra_income_by_year(store) == {2028: 5.0}


def test_editing_a_transfer_can_change_its_bucket():
    store = InMemoryJournalStore()
    entry_id = journal.add_transfer(store, NOW, kind="invest", entry_date="2026-11-01", amount_lakhs=3.0, bucket="equity")
    journal.edit_entry(store, NOW, entry_id, entry_date="2026-11-02", amount_lakhs=3.5, bucket="debt")
    assert [(t["from_bucket"], t["to_bucket"], t["amount_lakhs"]) for t in journal.transfers_for_projection(store)] == [
        ("cash", "debt", 3.5)]
    entry_id = store.entries("invest")[0]["id"]
    journal.edit_entry(store, NOW, entry_id, entry_date="2026-11-02", amount_lakhs=3.5)         # bucket kept
    assert store.entries("invest")[0]["to_bucket"] == "debt"


def test_entry_edits_follow_the_same_rules_as_new_entries():
    store = store_with_loan(as_of="2026-10-31")
    prepayment = journal.add_prepayment(store, NOW_2026, loan_id=1, entry_date="2026-11-01", amount_lakhs=1.0)
    with pytest.raises(journal.JournalError, match="in the future"):
        journal.edit_entry(store, NOW_2026, prepayment, entry_date="2027-01-01", amount_lakhs=1.0)
    income = journal.add_income(store, NOW_2026, entry_date="2027-03-01", amount_lakhs=1.0)
    journal.edit_entry(store, NOW_2026, income, entry_date="2028-01-01", amount_lakhs=1.0)    # planned: future is fine
    with pytest.raises(journal.JournalError, match="status"):
        journal.edit_entry(store, NOW_2026, store.entries("income")[0]["id"], entry_date="2028-01-01", amount_lakhs=1.0, status="maybe")
    with pytest.raises(journal.JournalError, match="Unknown entry"):
        journal.edit_entry(store, NOW_2026, 999, entry_date="2026-01-01", amount_lakhs=1.0)
    journal.void_entry(store, NOW_2026, prepayment)
    with pytest.raises(journal.JournalError, match="Unknown entry"):
        journal.edit_entry(store, NOW_2026, prepayment, entry_date="2026-11-02", amount_lakhs=1.0)


# -- several loans ------------------------------------------------------------------------


def two_loans(**second):
    """Loan 1: 50L @ 9% / 120 months. Loan 2: 20L @ 11% / 60 months (overridable)."""
    store = store_with_loan(as_of="2026-10-31")
    args = dict(name="Car loan", outstanding_lakhs=20.0, as_of="2026-10-31", annual_rate_pct=11.0, remaining_months=60)
    args.update(second)
    journal.setup_loan(store, NOW, **args)
    return store


def test_any_number_of_loans_can_be_set_up_each_with_its_own_terms_and_opening_balance():
    store = two_loans()
    assert [l["name"] for l in store.loans()] == ["Home loan", "Car loan"]
    assert store.terms(1)[0]["emi_lakhs"] == pytest.approx(emi_from_tenure(50.0, 9.0, 120))
    assert store.terms(2)[0]["emi_lakhs"] == pytest.approx(emi_from_tenure(20.0, 11.0, 60))
    assert [(o["ref_id"], o["amount_lakhs"], o["source"]) for o in store.observations("loan")] == [
        (1, 50.0, "loan_setup"), (2, 20.0, "loan_setup")]


def test_each_loan_view_is_independent_and_the_combined_strip_adds_them_up():
    store = two_loans()
    data = journal.loan_views(store, TODAY, 2026, 2040)
    home, car = data["loans"]
    assert (home["loan"]["name"], car["loan"]["name"]) == ("Home loan", "Car loan")
    assert home["summary"]["payoff_month"] == "2036-10" and car["summary"]["payoff_month"] == "2031-10"
    combined = data["combined"]
    assert combined["current_balance"] == pytest.approx(home["summary"]["current_balance"] + car["summary"]["current_balance"], abs=1e-3)
    assert combined["monthly_emi"] == pytest.approx(home["summary"]["monthly_emi"] + car["summary"]["monthly_emi"], abs=1e-3)
    assert combined["total_interest_remaining"] == pytest.approx(
        home["summary"]["total_interest_remaining"] + car["summary"]["total_interest_remaining"], abs=1e-3)
    assert combined["payoff_month"] == "2036-10"                       # the last one to clear


def test_a_prepayment_applies_only_to_the_loan_it_names():
    store = two_loans()
    journal.add_prepayment(store, NOW, loan_id=2, entry_date="2026-11-10", amount_lakhs=3.0)
    home, car = journal.loan_views(store, TODAY, 2026, 2040)["loans"]
    assert home["prepayments"] == [] and len(car["prepayments"]) == 1
    assert home["summary"]["total_prepaid_remaining"] == 0.0 and car["summary"]["total_prepaid_remaining"] == 3.0
    assert car["summary"]["payoff_month"] < "2031-10" and home["summary"]["payoff_month"] == "2036-10"


def test_terms_changes_statements_and_previews_target_one_loan():
    store = two_loans()
    journal.add_loan_terms(store, NOW, loan_id=2, effective_from="2027-04-01", annual_rate_pct=12.0, emi_lakhs=0.5)
    assert len(store.terms(1)) == 1 and len(store.terms(2)) == 2
    journal.add_loan_statement(store, NOW, loan_id=1, as_of="2027-01-31", outstanding_lakhs=44.0)
    assert [o["amount_lakhs"] for o in store.observations("loan", 1)] == [50.0, 44.0]
    assert [o["amount_lakhs"] for o in store.observations("loan", 2)] == [20.0]
    a = journal.prepayment_preview(store, loan_id=1, entry_date="2027-02-10", amount_lakhs=2.0)
    b = journal.prepayment_preview(store, loan_id=2, entry_date="2027-02-10", amount_lakhs=2.0)
    assert a["payoff_month_before"] != b["payoff_month_before"]
    assert store.entries("prepayment") == []


def test_terms_records_are_looked_up_across_loans_and_each_loans_only_record_is_protected():
    store = two_loans()
    journal.add_loan_terms(store, NOW, loan_id=2, effective_from="2027-04-01", annual_rate_pct=12.0, emi_lakhs=0.5)
    with pytest.raises(journal.JournalError, match="only terms"):
        journal.void_loan_terms(store, NOW, 1)                          # loan 1 has just its opening terms
    journal.void_loan_terms(store, NOW, 3)                              # loan 2 has two, so one can go
    journal.edit_loan_terms(store, NOW, 2, effective_from="2026-10-31", annual_rate_pct=10.5, emi_lakhs=0.43)
    assert [t["annual_rate_pct"] for t in store.terms(2)] == [10.5] and store.terms(1)[0]["annual_rate_pct"] == 9.0


def test_a_check_in_records_one_balance_per_loan_and_the_history_groups_them_under_their_keys():
    store = two_loans()
    ids = journal.record_check_in(store, NOW, as_of="2026-11-14", cash_lakhs=5.0, loan_balances={1: 49.0, "2": 19.0})
    assert set(ids) == {"cash", "loan_1", "loan_2"}
    assert [(o["ref_id"]) for o in store.observations("loan") if o["as_of"] == "2026-11-14"] == [1, 2]
    [latest, *_] = journal.check_in_history(store)
    assert latest["balances"] == {"cash": 5.0, "loan_1": 49.0, "loan_2": 19.0}


def test_a_check_in_for_an_unknown_loan_is_refused_and_stores_nothing():
    store = two_loans()
    before = len(store.observations())
    with pytest.raises(journal.JournalError, match="Unknown loan"):
        journal.record_check_in(store, NOW, as_of="2026-11-14", cash_lakhs=5.0, loan_balances={7: 1.0})
    assert len(store.observations()) == before


def test_editing_a_check_in_changes_each_loans_balance_and_can_drop_one():
    store = two_loans()
    journal.record_check_in(store, NOW, as_of="2026-11-14", loan_balances={1: 49.0, 2: 19.0})
    ids = group_ids(store, "2026-11-14")
    journal.edit_check_in(store, NOW, ids=ids, as_of="2026-11-14", loan_balances={1: 48.5})
    live = {journal._observation_key(o): o["amount_lakhs"] for o in store.observations() if o["as_of"] == "2026-11-14"}
    assert live == {"loan_1": 48.5}                                        # loan 2's balance dropped (it still has its opening one)


def test_the_loan_balance_guard_is_per_loan():
    store = two_loans()
    journal.record_check_in(store, NOW, as_of="2026-11-14", loan_balances={1: 49.0})
    # loan 1 has its opening balance as well, so this check-in can go ...
    journal.void_check_in(store, NOW, group_ids(store, "2026-11-14"))
    # ... but loan 2's only balance (its opening one) cannot, even though loan 1 has others
    with pytest.raises(journal.JournalError, match="only balance"):
        journal.void_check_in(store, NOW, {"loan_2": store.observations("loan", 2)[0]["id"]})
    with pytest.raises(journal.JournalError, match="only balance"):
        journal.void_check_in(store, NOW, group_ids(store, "2026-10-31"))     # opening balances of both loans


def test_a_check_in_key_must_match_its_observation():
    store = two_loans()
    journal.record_check_in(store, NOW, as_of="2026-11-14", loan_balances={1: 49.0})
    with pytest.raises(journal.JournalError, match="Unknown balance"):
        journal.void_check_in(store, NOW, {"loan_2": group_ids(store, "2026-11-14")["loan_1"]})


def test_the_projection_gets_one_merged_timeline_so_it_has_the_same_rows_for_any_number_of_loans():
    store = two_loans()
    inputs = journal.projection_inputs(store, 2026, 2040, include_actuals=True)
    timeline = {r["month"]: r for r in inputs["loan_timeline"]}
    singles = [
        {r["month"]: r for r in build_timeline(store.observations("loan", i), store.terms(i), [], 2026, 2040)}
        for i in (1, 2)
    ]
    for month in ("2026-12", "2028-06", "2031-09"):
        for field in ("emi_paid", "interest", "principal", "closing"):
            expected = sum(single[month][field] for single in singles if month in single)
            assert timeline[month][field] == pytest.approx(expected, abs=2e-4), (month, field)
    # loan 2 clears in 2031, after which only loan 1 contributes
    assert timeline["2033-01"]["emi_paid"] == singles[0]["2033-01"]["emi_paid"]
    assert timeline["2033-01"]["closing"] == singles[0]["2033-01"]["closing"]


def test_the_plan_view_uses_each_loans_setup_and_terms_but_no_prepayments_or_later_statements():
    store = two_loans()
    journal.add_prepayment(store, NOW, loan_id=2, entry_date="2026-11-10", amount_lakhs=3.0)
    journal.add_loan_statement(store, NOW, loan_id=1, as_of="2027-01-31", outstanding_lakhs=30.0)
    plan = journal.projection_inputs(store, 2026, 2040, include_actuals=False)["loan_timeline"]
    actual = journal.projection_inputs(store, 2026, 2040, include_actuals=True)["loan_timeline"]
    assert sum(r["prepayment"] for r in plan) == 0.0 and sum(r["prepayment"] for r in actual) == 3.0
    assert all(r["drift"] is None for r in plan) and any(r["drift"] is not None for r in actual)


def test_a_loan_with_no_balance_left_is_skipped_by_the_projection():
    store = store_with_loan()
    store._loans.append({"id": 2, "name": "Empty", "created_at": NOW, "closed_at": None})
    assert [v["loan"]["id"] for v in journal.loan_views(store, TODAY, 2026, 2040)["loans"]] == [1]
    assert journal.projection_inputs(store, 2026, 2040, True)["loan_timeline"] is not None


def test_future_dates_are_refused_for_every_loan_action():
    store = two_loans()
    with pytest.raises(journal.JournalError, match="in the future"):
        journal.add_prepayment(store, NOW_2026, loan_id=2, entry_date="2027-01-01", amount_lakhs=1.0)
    with pytest.raises(journal.JournalError, match="in the future"):
        journal.add_loan_statement(store, NOW_2026, loan_id=2, as_of="2027-01-01", outstanding_lakhs=1.0)
    with pytest.raises(journal.JournalError, match="in the future"):
        journal.record_check_in(store, NOW_2026, as_of="2027-01-01", loan_balances={2: 1.0})


# -- before a loan is entered, the old assumptions still apply -----------------------------
#
# Entering a loan changes nothing before it: months earlier than the loan's first
# entry keep the flat-EMI assumption, and from that month on the loan's own schedule
# takes over and carries on (the same way a check-in does).

from projections import DEFAULT_ASSUMPTIONS, run_projection  # noqa: E402

LEGACY = {"annual_emi_lakhs": 12.0, "loan_end_year": 2028}      # 1.0 L a month until the end of 2028


def timeline_for(store, include_actuals=False, legacy=LEGACY, end=2040):
    return {r["month"]: r for r in journal.projection_inputs(store, 2026, end, include_actuals, legacy)["loan_timeline"]}


def test_months_before_the_loan_was_entered_use_the_flat_emi_assumption():
    store = store_with_loan(as_of="2026-11-14", outstanding_lakhs=40.0, emi_lakhs=0.6, remaining_months=None)
    months = timeline_for(store)
    assert [months[m]["emi_paid"] for m in ("2026-01", "2026-06", "2026-10")] == [1.0, 1.0, 1.0]
    assert months["2026-01"]["closing"] is None and months["2026-10"]["closing"] is None     # no loan balance yet
    assert months["2026-01"]["interest"] == 0.0 and months["2026-01"]["principal"] == 0.0
    assert months["2026-11"]["emi_paid"] == 0.6                                              # the loan takes over
    assert months["2026-11"]["closing"] == 40.0
    assert months["2026-12"]["emi_paid"] == 0.6 and months["2026-12"]["interest"] > 0
    assert months["2027-06"]["emi_paid"] == 0.6                                              # and carries on


def test_the_flat_emi_assumption_stops_after_its_end_year_even_before_the_loan():
    store = store_with_loan(as_of="2029-12-01", outstanding_lakhs=40.0, emi_lakhs=0.6, remaining_months=None)
    months = timeline_for(store)
    assert months["2028-12"]["emi_paid"] == 1.0 and months["2029-01"]["emi_paid"] == 0.0
    assert months["2029-11"]["emi_paid"] == 0.0 and months["2029-12"]["emi_paid"] == 0.6


def test_without_a_legacy_assumption_months_before_the_loan_have_no_emi():
    store = store_with_loan(as_of="2026-11-14", outstanding_lakhs=40.0, emi_lakhs=0.6, remaining_months=None)
    assert timeline_for(store, legacy=None)["2026-03"]["emi_paid"] == 0.0


def test_the_plan_view_also_keeps_the_older_numbers_before_the_loan():
    store = store_with_loan(as_of="2026-11-14", outstanding_lakhs=40.0, emi_lakhs=0.6, remaining_months=None)
    journal.add_loan_statement(store, NOW, loan_id=1, as_of="2026-12-20", outstanding_lakhs=30.0)
    for include_actuals in (False, True):
        months = timeline_for(store, include_actuals)
        assert months["2026-05"]["emi_paid"] == 1.0, include_actuals


def test_with_two_loans_the_flat_assumption_applies_until_the_earliest_entry():
    store = store_with_loan(as_of="2026-08-10", outstanding_lakhs=40.0, emi_lakhs=0.6, remaining_months=None)
    journal.setup_loan(store, NOW, name="Car", outstanding_lakhs=10.0, as_of="2026-11-02", annual_rate_pct=10.0, emi_lakhs=0.3)
    months = timeline_for(store)
    assert months["2026-07"]["emi_paid"] == 1.0                      # before any loan: the assumption
    assert months["2026-08"]["emi_paid"] == 0.6                      # loan 1 entered; loan 2 not yet
    assert months["2026-10"]["emi_paid"] == 0.6
    assert months["2026-11"]["emi_paid"] == pytest.approx(0.9)      # loan 2 joins in its own month


def test_a_prepayment_before_the_loan_was_entered_is_still_a_cash_outflow_on_top_of_the_assumption():
    store = store_with_loan(as_of="2026-11-14", outstanding_lakhs=40.0, emi_lakhs=0.6, remaining_months=None)
    journal.add_prepayment(store, NOW, loan_id=1, entry_date="2026-05-20", amount_lakhs=2.0)
    months = timeline_for(store, include_actuals=True)
    assert months["2026-05"]["emi_paid"] == 1.0 and months["2026-05"]["prepayment"] == 2.0


def test_entering_a_loan_leaves_every_projected_month_before_it_unchanged():
    """End to end: the year's EMI is the assumption for the months before the entry
    and the loan's schedule from the entry month on."""
    assumptions = {**DEFAULT_ASSUMPTIONS, "annual_emi_lakhs": 12.0, "loan_end_year": 2028}
    store = store_with_loan(as_of="2026-11-14", outstanding_lakhs=40.0, emi_lakhs=0.6, remaining_months=None)
    inputs = journal.projection_inputs(store, 2026, 2070, False, LEGACY)
    before = run_projection(assumptions)                              # no loan at all
    after = run_projection(assumptions, loan_timeline=inputs["loan_timeline"])
    assert before[0]["emi_amount"] == 12.0
    assert after[0]["emi_amount"] == pytest.approx(10 * 1.0 + 2 * 0.6, abs=1e-3)       # 10 old months + Nov and Dec of the loan
    assert after[1]["emi_amount"] == pytest.approx(12 * 0.6, abs=1e-3)                  # then the loan alone
    # nothing before November moved: with the blend on, Jan-Oct months are identical
    spend = {f"2026-{m:02d}": 1.0 for m in range(1, 11)}
    today = date(2026, 11, 14)
    a = run_projection(assumptions, actual_monthly_spend=spend, today=today)[0]["months"]
    b = run_projection(assumptions, actual_monthly_spend=spend, today=today, loan_timeline=inputs["loan_timeline"])[0]["months"]
    assert [m["expense"] for m in a[:10]] == [m["expense"] for m in b[:10]]
    assert [m["cash_end"] for m in a[:10]] == [m["cash_end"] for m in b[:10]]
    assert b[10]["expense"] < a[10]["expense"]                                          # November onwards differs
