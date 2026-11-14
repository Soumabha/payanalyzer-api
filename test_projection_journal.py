"""
Pure, DB-free tests for how run_projection() uses journal inputs: a loan
schedule, balance observations (re-anchoring) and invest/withdraw transfers.
With none of them supplied the output is the plain forecast (the golden
snapshot and test_projections.py pin that); these tests pin the new behaviour.
"""

from datetime import date

import pytest

from loans import build_timeline, emi_from_tenure
from projections import DEFAULT_ASSUMPTIONS, PROJECTION_START_YEAR, run_projection

Y = PROJECTION_START_YEAR
EMI = emi_from_tenure(50.0, 9.0, 120)


def by_year(rows):
    return {r["year"]: r for r in rows}


def loan_timeline(prepayments=(), extra_observations=(), opening="2025-12-31", opening_balance=50.0, end_year=2070):
    observations = [{"as_of": opening, "amount_lakhs": opening_balance}, *extra_observations]
    terms = [{"effective_from": opening, "annual_rate_pct": 9.0, "emi_lakhs": EMI}]
    return build_timeline(observations, terms, list(prepayments), Y, end_year)


def plain():
    return by_year(run_projection(DEFAULT_ASSUMPTIONS))


# -- loan schedule --------------------------------------------------------------


def test_loan_replaces_the_flat_emi_and_ignores_the_legacy_end_year():
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=loan_timeline()))
    assert rows[Y]["emi_amount"] == pytest.approx(12 * EMI, abs=1e-3)
    assert rows[Y]["emi_active"] is True
    # Legacy assumption: loan_end_year=2031, so the plain run has no EMI after it.
    assert plain()[2033]["emi_amount"] == 0.0
    assert rows[2033]["emi_amount"] == pytest.approx(12 * EMI, abs=1e-3)  # 10-year loan runs to 2035
    assert rows[2036]["emi_amount"] == 0.0 and rows[2036]["emi_active"] is False


def test_loan_columns_split_the_emi_into_interest_and_principal_and_track_the_balance():
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=loan_timeline()))
    first = rows[Y]
    assert first["loan_interest"] + first["loan_principal"] == pytest.approx(first["emi_amount"], abs=1e-3)
    assert first["loan_balance_end"] == pytest.approx(50.0 - first["loan_principal"], abs=1e-3)
    assert rows[Y + 1]["loan_interest"] < first["loan_interest"]
    assert rows[2036]["loan_balance_end"] == 0.0
    assert sum(r["loan_principal"] for r in rows.values()) == pytest.approx(50.0, abs=0.01)


def test_prepayment_is_deducted_from_cash_and_shortens_the_loan():
    prepay = [{"date": f"{Y}-06-15", "amount_lakhs": 5.0}]
    with_prepay = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=loan_timeline(prepay)))
    without = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=loan_timeline()))
    assert with_prepay[Y]["loan_prepayment"] == 5.0
    assert with_prepay[Y]["total_expense"] == pytest.approx(without[Y]["total_expense"] + 5.0, abs=1e-3)
    assert with_prepay[Y]["cash_end"] == pytest.approx(without[Y]["cash_end"] - 5.0, abs=1e-3)
    last_emi_year = lambda rows: max(y for y, r in rows.items() if r["emi_amount"] > 0)
    assert last_emi_year(with_prepay) < last_emi_year(without)
    assert with_prepay[Y + 1]["loan_interest"] < without[Y + 1]["loan_interest"]


def test_without_a_loan_timeline_no_loan_keys_appear():
    assert all("loan_interest" not in r for r in run_projection(DEFAULT_ASSUMPTIONS))


def test_loan_balance_is_unknown_for_years_before_the_first_observation():
    timeline = build_timeline(
        [{"as_of": f"{Y + 1}-03-31", "amount_lakhs": 40.0}],
        [{"effective_from": f"{Y + 1}-03-31", "annual_rate_pct": 9.0, "emi_lakhs": EMI}], [], Y, 2040,
    )
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=timeline))
    assert rows[Y]["loan_balance_end"] is None
    assert rows[Y]["emi_amount"] == pytest.approx(12 * EMI, abs=1e-3)  # assumed paid
    assert rows[Y]["loan_interest"] == 0.0


def test_actuals_blend_uses_each_months_own_loan_payment():
    prepay = [{"date": f"{Y}-02-10", "amount_lakhs": 6.0}]
    timeline = loan_timeline(prepay)
    rows = by_year(run_projection(
        DEFAULT_ASSUMPTIONS, actual_monthly_spend={f"{Y}-01": 1.0, f"{Y}-02": 1.0},
        today=date(Y, 4, 10), loan_timeline=timeline,
    ))
    months = rows[Y]["months"]
    assert months[1]["expense"] == pytest.approx(1.0 + EMI + 6.0, abs=1e-3)   # actual + EMI + prepayment
    assert months[0]["expense"] == pytest.approx(1.0 + EMI, abs=1e-3)
    # A forecast month: non-loan assumed spend / 12 + that month's loan payment.
    non_loan = rows[Y]["assumed_total_expense"] - 12 * EMI - 6.0
    assert months[5]["expense"] == pytest.approx(non_loan / 12 + EMI, abs=1e-3)
    assert sum(m["expense"] for m in months) == pytest.approx(rows[Y]["total_expense"], abs=1e-3)


# -- observations (re-anchoring) --------------------------------------------------


def obs(bucket, amount, as_of):
    return {"bucket": bucket, "amount_lakhs": amount, "as_of": as_of}


def test_cash_observation_pins_the_year_end_to_the_balance_plus_the_rest_of_the_years_flow():
    observed_on = date(Y, 7, 1)
    elapsed = observed_on.timetuple().tm_yday / 365
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, observations=[obs("cash", 20.0, observed_on.isoformat())]))
    net = rows[Y]["net_cash_flow"]
    assert rows[Y]["cash_end"] == pytest.approx(20.0 + net * (1 - elapsed), abs=1e-3)
    assert rows[Y]["anchored"] == {"cash": observed_on.isoformat()}
    assert rows[Y + 1]["cash_start"] == rows[Y]["cash_end"]            # later years carry on from there
    assert "anchored" not in rows[Y + 1]


def test_equity_and_debt_observations_grow_only_for_the_rest_of_the_year():
    observed_on = date(Y, 7, 1)
    elapsed = observed_on.timetuple().tm_yday / 365
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, observations=[
        obs("equity", 100.0, observed_on.isoformat()), obs("debt", 40.0, observed_on.isoformat()),
    ]))
    assert rows[Y]["equity_end"] == pytest.approx(100.0 * 1.12 ** (1 - elapsed), abs=1e-3)
    assert rows[Y]["debt_end"] == pytest.approx(40.0 * 1.07 ** (1 - elapsed), abs=1e-3)
    assert rows[Y + 1]["equity_start"] == rows[Y]["equity_end"]


def test_the_latest_observation_in_a_year_wins():
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, observations=[
        obs("cash", 99.0, f"{Y}-03-01"), obs("cash", 20.0, f"{Y}-09-01"),
    ]))
    assert rows[Y]["anchored"] == {"cash": f"{Y}-09-01"}


def test_observations_outside_the_projection_window_and_unknown_buckets_are_ignored():
    base = run_projection(DEFAULT_ASSUMPTIONS)
    ignored = run_projection(DEFAULT_ASSUMPTIONS, observations=[
        obs("cash", 99.0, f"{Y - 1}-12-31"), obs("loan", 5.0, f"{Y}-06-01"),
    ])
    assert ignored == base


def test_anchoring_month_by_month_when_the_year_is_blended_with_actuals():
    spend = {f"{Y}-{m:02d}": 3.0 for m in range(1, 8)}
    rows = by_year(run_projection(
        DEFAULT_ASSUMPTIONS, actual_monthly_spend=spend, today=date(Y, 8, 10),
        observations=[obs("cash", 20.0, f"{Y}-06-30")],
    ))
    months = rows[Y]["months"]
    remaining = sum(m["net"] for m in months[6:])                          # Jul..Dec
    assert rows[Y]["cash_end"] == pytest.approx(20.0 + remaining, abs=1e-2)


def test_observations_move_later_years_only_through_balances():
    base = plain()
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, observations=[obs("cash", 50.0, f"{Y}-07-01")]))
    for year in range(Y + 1, 2040):
        for key in ("base_expense", "total_expense", "total_income"):
            assert rows[year][key] == base[year][key]
    assert rows[Y + 1]["cash_start"] != base[Y + 1]["cash_start"]


# -- transfers --------------------------------------------------------------------


def transfer(amount, when, from_bucket="cash", to_bucket="equity"):
    return {"date": when, "amount_lakhs": amount, "from_bucket": from_bucket, "to_bucket": to_bucket}


def test_invest_moves_money_from_cash_to_equity_at_year_end():
    base = plain()
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, transfers=[transfer(4.0, f"{Y + 1}-02-01")]))
    assert rows[Y + 1]["cash_end"] == pytest.approx(base[Y + 1]["cash_end"] - 4.0, abs=1e-3)
    assert rows[Y + 1]["equity_end"] == pytest.approx(base[Y + 1]["equity_end"] + 4.0, abs=1e-3)
    assert rows[Y + 1]["total_portfolio"] == pytest.approx(base[Y + 1]["total_portfolio"], abs=1e-3)
    assert rows[Y + 1]["transfers"]["cash"] == -4.0
    assert rows[Y]["cash_end"] == base[Y]["cash_end"]


def test_withdraw_moves_money_from_debt_to_cash():
    base = plain()
    rows = by_year(run_projection(
        DEFAULT_ASSUMPTIONS, transfers=[transfer(3.0, f"{Y}-05-01", from_bucket="debt", to_bucket="cash")]
    ))
    assert rows[Y]["debt_end"] == pytest.approx(base[Y]["debt_end"] - 3.0, abs=1e-3)
    assert rows[Y]["cash_end"] == pytest.approx(base[Y]["cash_end"] + 3.0, abs=1e-3)


def test_transfer_on_or_before_an_observation_of_a_touched_bucket_is_already_in_that_balance():
    observations = [obs("cash", 20.0, f"{Y}-09-01")]
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, observations=observations, transfers=[
        transfer(4.0, f"{Y}-06-01"),   # before the cash observation -> absorbed
        transfer(2.0, f"{Y}-10-01"),   # after -> applied
    ]))
    assert rows[Y]["transfers"]["cash"] == -2.0
    solo = by_year(run_projection(DEFAULT_ASSUMPTIONS, observations=observations))
    assert rows[Y]["cash_end"] == pytest.approx(solo[Y]["cash_end"] - 2.0, abs=1e-3)


def test_a_transfer_that_would_overdraw_cash_floors_cash_at_zero():
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, transfers=[transfer(10_000.0, f"{Y}-02-01")]))
    assert rows[Y]["cash_end"] == 0.0
    assert rows[Y]["equity_end"] > plain()[Y]["equity_end"]
