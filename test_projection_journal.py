"""
Pure, DB-free tests for how run_projection() uses journal inputs: a loan
schedule (annual mode), and check-ins and invest/withdraw transfers (actuals
mode, month by month). With none of them supplied the output is the plain
forecast (the golden snapshot and test_projections.py pin that).
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


def test_the_loan_schedule_drives_the_emi_for_as_long_as_it_runs():
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=loan_timeline()))
    assert rows[Y]["emi_amount"] == pytest.approx(12 * EMI, abs=1e-3)
    assert rows[Y]["emi_active"] is True
    assert plain()[Y]["emi_amount"] == 0.0 and plain()[2033]["emi_active"] is False       # no loan, no EMI
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


def test_the_visible_expense_columns_add_up_to_total_expense_even_with_prepayments():
    """The table shows Base, Kids, Parents and EMI Amt but no separate prepayment
    column, so EMI Amt must carry the prepayment or the row would not add up."""
    prepay = [{"date": f"{Y}-06-15", "amount_lakhs": 5.0}, {"date": f"{Y + 2}-02-01", "amount_lakhs": 3.0}]
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=loan_timeline(prepay)))
    for year in (Y, Y + 1, Y + 2, 2040):
        r = rows[year]
        assert r["base_expense"] + r["kids_expense"] + r["parents_expense"] + r["emi_amount"] == pytest.approx(
            r["total_expense"], abs=3e-4), year
    assert rows[Y]["emi_amount"] == pytest.approx(rows[Y]["loan_interest"] + rows[Y]["loan_principal"] + 5.0, abs=1e-3)
    assert rows[Y]["loan_prepayment"] == 5.0                    # still reported as detail


def test_a_months_base_plus_emi_columns_add_up_to_its_expense_with_a_prepayment():
    timeline = loan_timeline([{"date": f"{Y}-02-10", "amount_lakhs": 6.0}])
    row = by_year(run_projection(DEFAULT_ASSUMPTIONS, actual_monthly_spend={f"{Y}-01": 1.0, f"{Y}-02": 1.0},
                                 today=date(Y, 4, 10), loan_timeline=timeline))[Y]
    february = row["months"][1]
    assert february["spend"] + february["loan_payment"] == pytest.approx(february["expense"], abs=2e-4)
    assert february["loan_payment"] == pytest.approx(EMI + 6.0, abs=1e-3)


def test_without_a_loan_timeline_no_loan_keys_appear():
    assert all("loan_interest" not in r for r in run_projection(DEFAULT_ASSUMPTIONS))


def test_loan_balance_is_unknown_for_years_before_the_first_observation():
    timeline = build_timeline(
        [{"as_of": f"{Y + 1}-03-31", "amount_lakhs": 40.0}],
        [{"effective_from": f"{Y + 1}-03-31", "annual_rate_pct": 9.0, "emi_lakhs": EMI}], [], Y, 2040,
    )
    rows = by_year(run_projection(DEFAULT_ASSUMPTIONS, loan_timeline=timeline))
    assert rows[Y]["loan_balance_end"] is None
    # Nothing is attributed to a loan before its first recorded balance.
    assert rows[Y]["emi_amount"] == 0.0
    assert rows[Y]["loan_interest"] == 0.0
    # the loan is entered in March of Y+1: its first month and the nine after it
    assert rows[Y + 1]["emi_amount"] == pytest.approx(10 * EMI, abs=1e-3)
    assert rows[Y + 2]["emi_amount"] == pytest.approx(12 * EMI, abs=1e-3)


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


# -- check-ins (month by month, actuals mode) ----------------------------------------
#
# Scenario: today is 14 Nov of the start year, no recorded spend, so Jan-Oct are
# forecast ("no data") months and the default assumptions drive every flow. A
# check-in is snapped to the START of its month.

TODAY = date(Y, 11, 14)


def run(**kwargs):
    kwargs.setdefault("actual_monthly_spend", {})
    kwargs.setdefault("today", TODAY)
    return by_year(run_projection(DEFAULT_ASSUMPTIONS, **kwargs))


def month(rows, year, number):
    return rows[year]["months"][number - 1]


def obs(bucket, amount, as_of):
    return {"bucket": bucket, "amount_lakhs": amount, "as_of": as_of}


def test_a_check_in_never_rewrites_the_start_columns_or_the_months_before_it():
    base = run()
    rows = run(observations=[obs("cash", 20.0, f"{Y}-11-14"), obs("equity", 240.0, f"{Y}-11-14")])
    assert rows[Y]["cash_start"] == base[Y]["cash_start"] == DEFAULT_ASSUMPTIONS["starting_cash_lakhs"]
    assert rows[Y]["equity_start"] == base[Y]["equity_start"]
    for number in range(1, 11):
        assert month(rows, Y, number) == month(base, Y, number), number     # Jan..Oct untouched


def test_the_check_in_month_starts_from_the_observed_balance_and_reports_the_variance():
    base = run()
    rows = run(observations=[obs("cash", 20.0, f"{Y}-11-14")])
    november, october = month(rows, Y, 11), month(base, Y, 10)
    mark = november["checkin"]["cash"]
    assert mark["observed"] == 20.0 and mark["as_of"] == f"{Y}-11-14"
    assert mark["expected"] == october["cash_end"]                           # what the forecast said
    assert mark["variance"] == pytest.approx(20.0 - october["cash_end"], abs=1e-3)
    # month end = observed start + the month's (unchanged) net flow
    assert november["cash_end"] == pytest.approx(20.0 + month(base, Y, 11)["net"], abs=1e-3)


def test_later_months_and_years_follow_the_latest_check_in_by_the_same_amount():
    base = run()
    rows = run(observations=[obs("cash", 20.0, f"{Y}-11-14")])
    variance = month(rows, Y, 11)["checkin"]["cash"]["variance"]
    assert month(rows, Y, 12)["cash_end"] == pytest.approx(month(base, Y, 12)["cash_end"] + variance, abs=1e-3)
    assert rows[Y]["cash_end"] == month(rows, Y, 12)["cash_end"]
    assert rows[Y + 1]["cash_start"] == rows[Y]["cash_end"]
    assert rows[Y + 3]["cash_end"] == pytest.approx(base[Y + 3]["cash_end"] + variance, abs=1e-2)


def test_an_equity_variance_keeps_growing_at_the_equity_return():
    base = run()
    rows = run(observations=[obs("equity", 240.0, f"{Y}-11-14")])
    variance = month(rows, Y, 11)["checkin"]["equity"]["variance"]
    # The check-in resets the START of November, so by the end of December the
    # variance has grown for two months, and by the end of Y+2 for two years and two months.
    assert month(rows, Y, 12)["equity_end"] == pytest.approx(
        month(base, Y, 12)["equity_end"] + variance * 1.12 ** (2 / 12), abs=1e-3
    )
    assert rows[Y + 2]["equity_end"] == pytest.approx(
        base[Y + 2]["equity_end"] + variance * 1.12 ** (2 + 2 / 12), abs=1e-2
    )


def test_a_check_in_is_snapped_to_the_start_of_its_month():
    early = run(observations=[obs("cash", 20.0, f"{Y}-11-01")])
    late = run(observations=[obs("cash", 20.0, f"{Y}-11-30")])
    balances = ("cash_end", "equity_end", "debt_end", "total_portfolio")
    for number in range(1, 13):
        assert [month(early, Y, number)[k] for k in balances] == [month(late, Y, number)[k] for k in balances]
    assert month(early, Y, 11)["checkin"]["cash"]["variance"] == month(late, Y, 11)["checkin"]["cash"]["variance"]
    assert month(early, Y, 11)["checkin"]["cash"]["as_of"] == f"{Y}-11-01"
    assert month(late, Y, 11)["checkin"]["cash"]["as_of"] == f"{Y}-11-30"      # the real date is still reported


def test_a_second_check_in_is_compared_with_the_path_set_by_the_first():
    first_only = run(observations=[obs("cash", 30.0, f"{Y}-03-10")])
    both = run(observations=[obs("cash", 30.0, f"{Y}-03-10"), obs("cash", 41.0, f"{Y}-09-02")])
    # Mar..Aug follow the March check-in...
    for number in range(3, 9):
        assert month(both, Y, number) == month(first_only, Y, number), number
    # ...and September's variance is against THAT path, not against the settings.
    september = month(both, Y, 9)["checkin"]["cash"]
    assert september["expected"] == month(first_only, Y, 8)["cash_end"]
    assert september["variance"] == pytest.approx(41.0 - month(first_only, Y, 8)["cash_end"], abs=1e-3)
    assert month(both, Y, 3)["checkin"]["cash"]["expected"] == month(run(), Y, 2)["cash_end"]
    assert both[Y]["cash_adjustment"] == pytest.approx(
        month(both, Y, 3)["checkin"]["cash"]["variance"] + september["variance"], abs=1e-3
    )


def test_a_backdated_check_in_is_chained_in_date_order():
    forward = run(observations=[obs("cash", 30.0, f"{Y}-03-10"), obs("cash", 41.0, f"{Y}-09-02")])
    reversed_order = run(observations=[obs("cash", 41.0, f"{Y}-09-02"), obs("cash", 30.0, f"{Y}-03-10")])
    assert forward[Y] == reversed_order[Y]


def test_the_latest_dated_check_in_in_a_month_wins_for_that_bucket():
    rows = run(observations=[obs("cash", 99.0, f"{Y}-11-02"), obs("cash", 20.0, f"{Y}-11-12")])
    assert month(rows, Y, 11)["checkin"]["cash"]["observed"] == 20.0


def test_a_partial_check_in_moves_only_the_buckets_it_supplies():
    base = run()
    rows = run(observations=[obs("cash", 20.0, f"{Y}-11-14")])
    assert set(month(rows, Y, 11)["checkin"]) == {"cash"}
    for year in range(Y, Y + 5):
        assert rows[year]["equity_end"] == base[year]["equity_end"]
        assert rows[year]["debt_end"] == base[year]["debt_end"]


def test_a_check_in_changes_balances_only_never_expenses_or_income():
    base = run()
    rows = run(observations=[obs("cash", 5.0, f"{Y}-06-10"), obs("equity", 10.0, f"{Y}-06-10")],
               actual_monthly_spend={f"{Y}-01": 1.0, f"{Y}-02": 1.2})
    base_spend = run(actual_monthly_spend={f"{Y}-01": 1.0, f"{Y}-02": 1.2})
    for year in range(Y, 2040):
        for key in ("base_expense", "kids_expense", "parents_expense", "total_expense", "total_income",
                    "net_cash_flow", "net_expense", "reserve_contribution"):
            assert rows[year][key] == base_spend[year][key], (year, key)
    for number in range(1, 13):
        for key in ("expense", "spend", "income", "net", "status"):
            assert month(rows, Y, number)[key] == month(base_spend, Y, number)[key]
    assert base  # (the plain scenario still runs)


def test_check_ins_after_todays_month_and_before_the_start_year_are_ignored():
    base = run()
    assert run(observations=[obs("cash", 99.0, f"{Y}-12-05")])[Y] == base[Y]       # next month: not applied
    assert run(observations=[obs("cash", 99.0, f"{Y - 1}-12-31")])[Y] == base[Y]
    assert run(observations=[obs("loan", 99.0, f"{Y}-06-01")])[Y] == base[Y]       # not a portfolio bucket


def test_the_year_row_reconciles_start_flow_and_adjustment_and_flags_the_check_in_months():
    rows = run(observations=[obs("cash", 20.0, f"{Y}-11-14")])
    year = rows[Y]
    assert year["checkin_months"] == [f"{Y}-11"]
    assert year["cash_end"] == pytest.approx(year["cash_start"] + year["net_cash_flow"] + year["cash_adjustment"], abs=1e-3)
    assert year["equity_adjustment"] == 0.0 and year["debt_adjustment"] == 0.0
    assert "checkin_months" not in rows[Y + 1] and "cash_adjustment" not in rows[Y + 1]


def test_without_any_check_in_the_year_rows_carry_no_check_in_keys():
    row = run()[Y]
    assert "checkin_months" not in row and "cash_adjustment" not in row
    assert all(m["checkin"] is None and m["checkin_total_variance"] is None for m in row["months"])


def test_months_carry_balances_and_the_last_month_equals_the_year_end():
    row = run(observations=[obs("cash", 20.0, f"{Y}-11-14")])[Y]
    last = row["months"][-1]
    assert (last["cash_end"], last["equity_end"], last["debt_end"], last["total_portfolio"]) == (
        row["cash_end"], row["equity_end"], row["debt_end"], row["total_portfolio"])
    assert last["total_portfolio"] == pytest.approx(last["cash_end"] + last["equity_end"] + last["debt_end"], abs=2e-4)


def test_total_variance_is_the_sum_of_the_portfolio_buckets():
    rows = run(observations=[obs("cash", 20.0, f"{Y}-11-14"), obs("equity", 240.0, f"{Y}-11-14"),
                             obs("debt", 40.0, f"{Y}-11-14")])
    mark = month(rows, Y, 11)
    assert mark["checkin_total_variance"] == pytest.approx(sum(v["variance"] for v in mark["checkin"].values()), abs=1e-3)


def test_observations_and_transfers_need_actuals_mode():
    with pytest.raises(ValueError, match="actuals mode"):
        run_projection(DEFAULT_ASSUMPTIONS, observations=[obs("cash", 1.0, f"{Y}-03-01")])
    with pytest.raises(ValueError, match="actuals mode"):
        run_projection(DEFAULT_ASSUMPTIONS, transfers=[{"date": f"{Y}-03-01", "amount_lakhs": 1.0,
                                                        "from_bucket": "cash", "to_bucket": "equity"}])


# -- the monthly withdrawal waterfall ------------------------------------------------------


def shortfall_assumptions():
    return {**DEFAULT_ASSUMPTIONS, "current_annual_expense_lakhs": 60.0,
            "current_annual_salary_lakhs": 0.0, "rental_income_lakhs": 0.0, "starting_cash_lakhs": 0.0,
            "debt_portfolio_lakhs": 10.0, "equity_portfolio_lakhs": 50.0, "equity_return_pct": 12.0,
            "debt_return_pct": 6.0, "retirement_year": Y}


def test_a_monthly_shortfall_drains_debt_before_equity_and_keeps_cash_at_zero():
    rows = by_year(run_projection(shortfall_assumptions(), actual_monthly_spend={}, today=TODAY))
    months = rows[Y]["months"]
    assert all(m["cash_end"] == 0.0 for m in months)
    first_equity_draw = next(i for i, m in enumerate(months) if m["debt_end"] <= 1e-9)
    assert months[first_equity_draw - 1]["debt_end"] > 0
    # equity is only touched once debt is gone: until then it just grows
    assert months[0]["equity_end"] == pytest.approx(50.0 * 1.12 ** (1 / 12), abs=1e-3)
    assert months[-1]["equity_end"] < 50.0 * 1.12


def test_negative_equity_still_compounds_in_a_blended_year():
    assumptions = {**shortfall_assumptions(), "equity_portfolio_lakhs": 0.0, "debt_portfolio_lakhs": 0.0}
    rows = by_year(run_projection(assumptions, actual_monthly_spend={}, today=TODAY))
    assert rows[Y]["equity_end"] < -60.0                      # drawn 60, and the debt compounds at 12%
    assert rows[Y]["debt_end"] == 0.0 and rows[Y]["cash_end"] == 0.0


def test_a_monthly_year_matches_the_annual_engine_when_there_is_no_shortfall():
    plain_rows = by_year(run_projection(DEFAULT_ASSUMPTIONS))
    blended = run()
    for key in ("cash_end", "equity_end", "debt_end", "total_portfolio", "equity_growth", "debt_growth"):
        assert blended[Y][key] == pytest.approx(plain_rows[Y][key], abs=2e-3), key


# -- transfers --------------------------------------------------------------------------------


def transfer(amount, when, from_bucket="cash", to_bucket="equity"):
    return {"date": when, "amount_lakhs": amount, "from_bucket": from_bucket, "to_bucket": to_bucket}


def test_invest_moves_money_from_cash_to_equity_at_the_end_of_its_month():
    base = run()
    rows = run(transfers=[transfer(4.0, f"{Y}-06-20")])
    assert month(rows, Y, 5) == month(base, Y, 5)
    june, base_june = month(rows, Y, 6), month(base, Y, 6)
    assert june["cash_end"] == pytest.approx(base_june["cash_end"] - 4.0, abs=1e-3)
    assert june["equity_end"] == pytest.approx(base_june["equity_end"] + 4.0, abs=1e-3)
    assert june["total_portfolio"] == pytest.approx(base_june["total_portfolio"], abs=1e-3)
    assert rows[Y]["transfers"]["cash"] == -4.0
    assert rows[Y]["equity_end"] > base[Y]["equity_end"] + 4.0 - 1e-6        # the moved money then grows


def test_withdraw_moves_money_from_debt_to_cash():
    base = run()
    rows = run(transfers=[transfer(3.0, f"{Y}-05-01", from_bucket="debt", to_bucket="cash")])
    may, base_may = month(rows, Y, 5), month(base, Y, 5)
    assert may["debt_end"] == pytest.approx(base_may["debt_end"] - 3.0, abs=1e-3)
    assert may["cash_end"] == pytest.approx(base_may["cash_end"] + 3.0, abs=1e-3)


def test_a_transfer_in_an_earlier_month_is_applied_and_the_later_check_in_shows_only_the_rest():
    with_transfer = run(transfers=[transfer(4.0, f"{Y}-10-05")], observations=[obs("cash", 20.0, f"{Y}-11-14")])
    without = run(observations=[obs("cash", 20.0, f"{Y}-11-14")])
    assert month(with_transfer, Y, 10)["cash_end"] == pytest.approx(month(without, Y, 10)["cash_end"] - 4.0, abs=1e-3)
    # the 4L explains part of the gap, so the unexplained variance is 4L larger (cash was forecast 4L lower)
    assert month(with_transfer, Y, 11)["checkin"]["cash"]["variance"] == pytest.approx(
        month(without, Y, 11)["checkin"]["cash"]["variance"] + 4.0, abs=1e-3)


def test_a_transfer_on_or_before_a_same_month_check_in_of_a_touched_bucket_is_already_in_that_balance():
    observations = [obs("cash", 20.0, f"{Y}-11-14")]
    on_before = run(observations=observations, transfers=[transfer(4.0, f"{Y}-11-05")])
    assert "transfers" not in on_before[Y]
    after = run(observations=observations, transfers=[transfer(2.0, f"{Y}-11-20")])
    assert after[Y]["transfers"]["cash"] == -2.0
    assert month(after, Y, 11)["cash_end"] == pytest.approx(month(run(observations=observations), Y, 11)["cash_end"] - 2.0, abs=1e-3)


def test_a_transfer_that_would_overdraw_cash_floors_cash_at_zero():
    rows = run(transfers=[transfer(10_000.0, f"{Y}-02-10")])
    assert month(rows, Y, 2)["cash_end"] == 0.0
    assert month(rows, Y, 2)["equity_end"] > month(run(), Y, 2)["equity_end"]


# -- loan columns in the month rows -------------------------------------------------------------


def test_month_rows_carry_the_loan_schedule_and_a_statement_shows_as_a_reversed_variance():
    computed = {r["month"]: r for r in loan_timeline()}[f"{Y}-06"]["closing"]
    timeline = loan_timeline(extra_observations=[{"as_of": f"{Y}-06-30", "amount_lakhs": computed - 2.0}])
    rows = run(loan_timeline=timeline)
    june = month(rows, Y, 6)
    assert june["loan_balance_end"] == pytest.approx(computed - 2.0, abs=1e-3)
    mark = june["checkin"]["loan"]
    assert mark["expected"] == pytest.approx(computed, abs=1e-3)
    assert mark["variance"] == pytest.approx(-2.0, abs=1e-3)               # owing less than expected -> negative
    assert month(rows, Y, 5)["checkin"] is None
    assert month(rows, Y, 1)["loan_interest"] > 0
    assert "checkin_total_variance" in june and june["checkin_total_variance"] is None   # loan is not part of the portfolio


def test_month_rows_have_no_loan_keys_without_a_loan():
    assert "loan_balance_end" not in month(run(), Y, 1)
