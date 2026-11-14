"""Pure tests for actual_spend.py, using the in-memory expense store (no DB)."""

import pytest

from actual_spend import RUPEES_PER_LAKH, load_actual_monthly_spend, monthly_spend_lakhs
from fake_expense_store import InMemoryExpenseStore


def test_rupees_are_converted_to_lakhs_in_this_one_place():
    assert monthly_spend_lakhs([{"month": "2026-01", "total": 150_000.0}]) == {"2026-01": 1.5}
    assert RUPEES_PER_LAKH == 100_000


def test_months_without_rows_are_absent_not_zero():
    assert monthly_spend_lakhs([{"month": "2026-02", "total": 1000.0}]) == {"2026-02": pytest.approx(0.01)}


def test_malformed_entries_are_skipped():
    spend = monthly_spend_lakhs([
        {"month": None, "total": 5.0},
        {"month": "2026-03", "total": None},
        {"month": "2026-04", "total": 100_000.0},
    ])
    assert spend == {"2026-04": 1.0}


def test_loader_counts_every_source_and_ignores_deleted_rows():
    store = InMemoryExpenseStore()
    store.add("api row", date="2026-01-05", cost=50_000.0, source="api")
    store.add("pdf row", date="2026-01-20", cost=25_000.0, source="manual_pdf")
    store.add("gone", date="2026-01-21", cost=900_000.0, deleted_at="2026-02-01T00:00:00")
    assert load_actual_monthly_spend(store, 2026) == {"2026-01": pytest.approx(0.75)}


def test_loader_ignores_history_before_january_of_the_start_year():
    store = InMemoryExpenseStore()
    store.add("old", date="2025-12-31", cost=100_000.0)
    store.add("new", date="2026-01-01", cost=100_000.0)
    assert load_actual_monthly_spend(store, 2026) == {"2026-01": 1.0}


def test_loader_groups_by_month_across_groups():
    store = InMemoryExpenseStore()
    store.add("a", date="2026-03-01", cost=100_000.0, group_id=1)
    store.add("b", date="2026-03-31", cost=100_000.0, group_id=2)
    store.add("c", date="2026-04-01", cost=50_000.0)
    assert load_actual_monthly_spend(store, 2026) == {"2026-03": 2.0, "2026-04": 0.5}
