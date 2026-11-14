"""
Pure, DB-free tests for the opt-in "actuals" mode of projections.run_projection().

Scenario used throughout: default assumptions plus a loan paying a flat 0.5 lakh
a month (a zero-rate loan, so the EMI never changes), so 2026 assumed total
expense is 12 (base) + 6 (EMI) = 18 lakhs, i.e. 1.5 per forecast month, and
income is 28 lakhs, i.e. 28/12 per month. "Today" is always injected.
"""

from datetime import date

import pytest

from loans import build_timeline
from projections import DEFAULT_ASSUMPTIONS, PROJECTION_START_YEAR, PROJECTION_END_YEAR, run_projection

Y = PROJECTION_START_YEAR
EMI_MONTH = 0.5
ASSUMED_YEAR_EXPENSE = 12.0 + 12 * EMI_MONTH
FORECAST_MONTH = ASSUMED_YEAR_EXPENSE / 12

# A loan that pays exactly EMI_MONTH every month for the whole window: zero rate,
# so principal = EMI, and a balance big enough never to clear.
LOAN = build_timeline(
    [{"as_of": f"{Y - 1}-12-31", "amount_lakhs": 1000.0}],
    [{"effective_from": f"{Y - 1}-12-31", "annual_rate_pct": 0.0, "emi_lakhs": EMI_MONTH}],
    [], Y, PROJECTION_END_YEAR,
)


def run(spend, today, **kwargs):
    return run_projection(DEFAULT_ASSUMPTIONS, actual_monthly_spend=spend, today=today, **{"loan_timeline": LOAN, **kwargs})


def by_year(rows):
    return {r["year"]: r for r in rows}


def statuses(row):
    return [m["status"] for m in row["months"]]


def test_without_actuals_output_is_the_plain_forecast_with_no_extra_keys():
    plain = run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=LOAN)
    for row in plain:
        assert "months" not in row and "expense_source" not in row


def test_empty_actuals_leave_every_number_identical_to_the_plain_forecast():
    """No recorded spend anywhere -> every past/current month falls back to the
    forecast, so all the numbers equal the plain run (only extra keys differ)."""
    plain = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=LOAN))
    blended = by_year(run({}, date(Y, 11, 13)))
    for year, plain_row in plain.items():
        for key, value in plain_row.items():
            assert blended[year][key] == pytest.approx(value, abs=1e-3), (year, key)


def test_month_statuses_for_the_current_year():
    row = by_year(run({f"{Y}-01": 1.0, f"{Y}-02": 1.0}, date(Y, 4, 15)))[Y]
    assert statuses(row) == (
        ["actual", "actual", "no_data"] + ["in_progress"] + ["forecast"] * 8
    )
    assert row["no_data_months"] == 1
    assert row["expense_source"] == "blended"


def test_closed_month_with_spend_is_actual_plus_assumed_emi():
    row = by_year(run({f"{Y}-01": 1.5}, date(Y, 2, 20)))[Y]
    january = row["months"][0]
    assert january["status"] == "actual"
    assert january["actual_spend"] == 1.5
    assert january["expense"] == pytest.approx(1.5 + EMI_MONTH, abs=1e-4)
    assert january["forecast_expense"] == pytest.approx(FORECAST_MONTH, abs=1e-4)


def test_closed_month_without_rows_uses_forecast_and_is_flagged_not_zero():
    row = by_year(run({}, date(Y, 3, 5)))[Y]
    january = row["months"][0]
    assert january["status"] == "no_data"
    assert january["expense"] == pytest.approx(FORECAST_MONTH, abs=1e-4)
    assert january["actual_spend"] is None


def test_month_in_progress_uses_forecast_but_shows_spend_so_far():
    row = by_year(run({f"{Y}-11": 0.4}, date(Y, 11, 13)))[Y]
    november = row["months"][10]
    assert november["status"] == "in_progress"
    assert november["expense"] == pytest.approx(FORECAST_MONTH, abs=1e-4)
    assert november["actual_spend"] == 0.4
    # ...and it does not count towards the year's recorded-actual total.
    assert row["actual_spend"] == 0.0


def test_months_after_today_are_forecast():
    row = by_year(run({}, date(Y, 6, 1)))[Y]
    assert statuses(row)[6:] == ["forecast"] * 6


def test_year_expense_is_the_sum_of_its_month_expenses_and_drives_cash_flow():
    spend = {f"{Y}-{m:02d}": 2.0 for m in range(1, 11)}
    row = by_year(run(spend, date(Y, 11, 13)))[Y]
    month_total = sum(m["expense"] for m in row["months"])
    assert row["total_expense"] == pytest.approx(month_total, abs=1e-3)
    # 10 actual months of (2.0 + EMI/12), then 2 forecast months.
    expected = 10 * (2.0 + EMI_MONTH) + 2 * FORECAST_MONTH
    assert row["total_expense"] == pytest.approx(expected, abs=1e-3)
    assert row["net_expense"] == pytest.approx(max(0.0, row["total_expense"] - row["total_income"]), abs=1e-3)
    assert row["reserve_contribution"] == pytest.approx(
        max(0.0, row["total_income"] - row["total_expense"]), abs=1e-3
    )
    assert row["assumed_total_expense"] == ASSUMED_YEAR_EXPENSE


def test_a_fully_closed_year_is_marked_actual():
    spend = {f"{Y}-{m:02d}": 1.0 for m in range(1, 13)}
    row = by_year(run(spend, date(Y + 1, 3, 10)))[Y]
    assert row["expense_source"] == "actual"
    assert statuses(row) == ["actual"] * 12
    assert row["actual_spend"] == pytest.approx(12.0)


def test_when_the_year_rolls_over_the_previous_year_becomes_actual_with_no_code_change():
    spend = {f"{Y}-{m:02d}": 1.0 for m in range(1, 13)}
    in_year = by_year(run(spend, date(Y, 12, 31)))
    next_year = by_year(run(spend, date(Y + 1, 1, 1)))
    assert in_year[Y]["expense_source"] == "blended"
    assert next_year[Y]["expense_source"] == "actual"
    assert next_year[Y + 1]["months"][0]["status"] == "in_progress"


def test_future_years_keep_the_assumed_expense_chain_untouched():
    """Actual spend far above the assumption must not re-base later years."""
    plain = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=LOAN))
    spend = {f"{Y}-{m:02d}": 5.0 for m in range(1, 11)}
    blended = by_year(run(spend, date(Y, 11, 13)))
    for year in range(Y + 1, 2040):
        for key in ("base_expense", "kids_expense", "parents_expense", "emi_amount", "total_expense"):
            assert blended[year][key] == plain[year][key], (year, key)
        assert blended[year]["expense_source"] == "forecast"
        assert "months" not in blended[year]


def test_current_year_actuals_flow_into_later_years_only_through_cash_balances():
    plain = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=LOAN))
    spend = {f"{Y}-{m:02d}": 5.0 for m in range(1, 11)}
    blended = by_year(run(spend, date(Y, 11, 13)))
    assert blended[Y]["total_expense"] > plain[Y]["total_expense"]
    assert blended[Y]["cash_end"] < plain[Y]["cash_end"]
    assert blended[Y + 1]["cash_start"] == blended[Y]["cash_end"]


def test_history_before_the_projection_start_is_ignored():
    row = by_year(run({f"{Y - 1}-12": 99.0}, date(Y, 3, 5)))[Y]
    assert all(m["actual_spend"] is None for m in row["months"])


def test_today_before_the_projection_start_blends_nothing():
    rows = run({f"{Y}-01": 1.0}, date(Y - 1, 6, 1))
    assert all(r["expense_source"] == "forecast" and "months" not in r for r in rows)


def test_income_in_sub_rows_is_the_assumed_annual_figure_over_twelve():
    row = by_year(run({}, date(Y, 6, 1)))[Y]
    assert all(m["income"] == pytest.approx(row["total_income"] / 12, abs=1e-4) for m in row["months"])
    first = row["months"][0]
    # Each field is rounded to 4dp independently, hence the looser tolerance.
    assert first["net"] == pytest.approx(first["income"] - first["expense"], abs=2e-4)


def test_every_month_splits_into_non_loan_spend_and_loan_payment_that_add_up_to_its_expense():
    spend = {f"{Y}-01": 0.99, f"{Y}-02": 0.68}
    row = by_year(run(spend, date(Y, 4, 10)))[Y]
    for month in row["months"]:
        assert month["spend"] + month["loan_payment"] == pytest.approx(month["expense"], abs=2e-4)
    january = row["months"][0]
    assert january["spend"] == 0.99                      # exactly what the expense records hold
    assert january["loan_payment"] == pytest.approx(EMI_MONTH, abs=1e-4)
    assert row["months"][2]["spend"] == pytest.approx(FORECAST_MONTH - EMI_MONTH, abs=2e-4)  # no_data month


def test_without_any_loan_an_actual_month_is_just_the_recorded_spend():
    row = by_year(run_projection(DEFAULT_ASSUMPTIONS, actual_monthly_spend={f"{Y}-01": 1.5}, today=date(Y, 2, 20)))[Y]
    january = row["months"][0]
    assert (january["spend"], january["loan_payment"], january["expense"]) == (1.5, 0.0, 1.5)
    assert row["months"][5]["expense"] == pytest.approx(12.0 / 12, abs=1e-4)      # a forecast month: base / 12, no loan
    assert row["emi_amount"] == 0.0 and row["emi_active"] is False
