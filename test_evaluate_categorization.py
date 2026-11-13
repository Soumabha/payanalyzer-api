"""
Unit tests for evaluate_categorization.py, the read-only backtest harness for
category_inference: it replays already-categorized rows from a date range as if
they didn't exist yet (excluded from the evidence) and compares what inference
would have said against the real category.

Driven through the in-memory store (the `store` fixture); no database is opened.

Run with: python -m pytest test_evaluate_categorization.py -v
"""

import re

import pytest

from evaluate_categorization import evaluate, report


@pytest.fixture
def history(store):
    """Evidence from June (outside the evaluated range)."""
    store.add("Cab", date="2026-06-01", category="Transport")
    store.add("Groceries - Big Bazaar", date="2026-06-02", category="Groceries")
    return store


def _by_description(results):
    return {r["description"]: r for r in results}


def test_evaluate_predicts_each_row_from_history_outside_the_range(history):
    history.add("Cab", date="2026-09-03", category="Transport")
    history.add("Groceries - Big Bazar", date="2026-09-04", category="Groceries")
    history.add("Mystery Thing", date="2026-09-05", category="Misc")

    results = _by_description(evaluate("2026-09-01", "2026-09-30", store=history))

    assert (results["Cab"]["predicted"], results["Cab"]["tier"]) == ("Transport", "exact")
    assert (results["Groceries - Big Bazar"]["predicted"], results["Groceries - Big Bazar"]["tier"]) == ("Groceries", "fuzzy")
    assert (results["Mystery Thing"]["predicted"], results["Mystery Thing"]["tier"]) == (None, "none")
    assert results["Cab"]["actual"] == "Transport"


def test_evaluate_never_lets_a_row_vouch_for_itself(store):
    # The only "Unique Thing" is the row being evaluated; if rows in the range
    # stayed in the evidence it would "predict" its own answer.
    store.add("Unique Thing", date="2026-09-03", category="Misc")
    (result,) = evaluate("2026-09-01", "2026-09-30", store=store)
    assert result["predicted"] is None


def test_evaluate_covers_exactly_the_categorized_live_rows_in_the_range(history):
    history.add("Out Of Range", date="2026-10-01", category="Misc")
    history.add("No Category", date="2026-09-03", category=None)
    history.add("Deleted", date="2026-09-03", category="Misc", deleted_at="2026-09-04T00:00:00")
    history.add("In Range", date="2026-09-03", category="Misc")
    assert [r["description"] for r in evaluate("2026-09-01", "2026-09-30", store=history)] == ["In Range"]


def test_evaluate_can_be_narrowed_to_specific_ids(history):
    keep = history.add("Cab", date="2026-09-03", category="Transport")
    history.add("Groceries - Big Bazar", date="2026-09-04", category="Groceries")
    results = evaluate("2026-09-01", "2026-09-30", ids=[keep], store=history)
    assert [r["description"] for r in results] == ["Cab"]


def test_evaluate_never_writes(history):
    history.add("Cab", date="2026-09-03", category="Transport")
    before, commits = history.rows(), history.commits
    evaluate("2026-09-01", "2026-09-30", store=history)
    assert history.rows() == before and history.commits == commits


def test_evaluate_opens_and_closes_its_own_store_when_none_is_given(monkeypatch, history):
    import evaluate_categorization

    monkeypatch.setattr(evaluate_categorization, "SqliteExpenseStore", lambda: history)
    evaluate("2026-09-01", "2026-09-30")
    assert history.closed is True


def test_evaluate_leaves_a_borrowed_store_open(history):
    evaluate("2026-09-01", "2026-09-30", store=history)
    assert history.closed is False


def test_report_summarizes_coverage_precision_and_lists_the_misses(history, capsys):
    history.add("Cab", date="2026-09-03", category="Transport")  # right (exact)
    history.add("Cab", date="2026-09-06", category="Travel")  # wrong (exact says Transport)
    history.add("Groceries - Big Bazar", date="2026-09-04", category="Groceries")  # right (fuzzy)
    history.add("Mystery Thing", date="2026-09-05", category="Misc")  # no prediction

    report(evaluate("2026-09-01", "2026-09-30", store=history))
    out = capsys.readouterr().out

    assert re.search(r"Total items evaluated:\s+4", out)
    assert re.search(r"exact-tier predictions:\s+2", out)
    assert re.search(r"fuzzy-tier predictions:\s+1", out)
    assert re.search(r"no prediction \(none\):\s+1", out)
    assert "Coverage (made a prediction):            75.0%  (3/4)" in out
    assert "Precision (correct, among predictions):  66.7%  (2/3)" in out
    assert "Incorrect predictions (1)" in out and "actual='Travel'" in out
    assert "No prediction made (1)" in out and "Mystery Thing" in out
    assert "Fuzzy-tier detail (1)" in out and "[OK]" in out


def test_report_handles_an_empty_result_set(capsys):
    report([])
    out = capsys.readouterr().out
    assert re.search(r"Total items evaluated:\s+0", out)
    assert "0.0%" in out
