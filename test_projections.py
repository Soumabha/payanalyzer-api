"""
Pure, DB-free tests for projections.py - the FIRE/wealth projection engine.

No fixtures, no database, no FastAPI: every test builds its own assumptions
dict inline and calls run_projection() directly, exactly like the engine's
own module docstring promises ("no database or FastAPI dependencies here on
purpose"). Golden values are deliberately "nice" illustrative numbers taken
from plans/01-phase-1-projection-engine.md's worked examples - not meant to
byte-match any real production data, just precise enough to catch logic
regressions (and to keep the plan doc's worked examples honest, since a
prose description of these formulas drifted out of sync with the code once
already - see the Kids Education snapshot-timing fix this suite locks in).

Run with: python3 -m pytest test_projections.py -v
"""

import pytest

from projections import (
    DEFAULT_ASSUMPTIONS,
    PROJECTION_END_YEAR,
    PROJECTION_START_YEAR,
    run_projection,
)


def disabled_assumptions(**overrides):
    """
    A complete, valid assumptions dict with every optional feature pushed
    out of range or zeroed out (kids/parents/EMI never trigger, no income,
    no starting balances/growth) - each test overrides only the handful of
    fields it's actually exercising, so tests can't accidentally interfere
    with each other via a shared "realistic" baseline.
    """
    assumptions = {
        "current_annual_expense_lakhs": 0.0,
        "inflation_pct": 0.0,
        "education_inflation_pct": 0.0,
        "healthcare_inflation_pct": 0.0,
        "kids_expense_start_year": PROJECTION_END_YEAR + 1,
        "kids_school_start_age": 1,
        "kids_expense_end_year": PROJECTION_END_YEAR + 1,
        "parents_expense_start_year": PROJECTION_END_YEAR + 1,
        "parents_expense_end_year": PROJECTION_END_YEAR + 1,
        "loan_outstanding_lakhs": 0.0,
        "annual_emi_lakhs": 0.0,
        "loan_end_year": PROJECTION_START_YEAR - 1,
        "rental_income_lakhs": 0.0,
        "rent_increase_pct": 0.0,
        "current_annual_salary_lakhs": 0.0,
        "salary_growth_pct": 0.0,
        "retirement_year": PROJECTION_END_YEAR + 1,
        "starting_cash_lakhs": 0.0,
        "equity_portfolio_lakhs": 0.0,
        "equity_return_pct": 0.0,
        "debt_portfolio_lakhs": 0.0,
        "debt_return_pct": 0.0,
    }
    assumptions.update(overrides)
    return assumptions


def test_default_assumptions_run_end_to_end_without_error():
    """Smoke test: the shipped DEFAULT_ASSUMPTIONS run across the full
    default horizon (reusing the constants, not hardcoded 2026/2070) and
    every row's total_portfolio ties out to equity + debt + cash."""
    rows = run_projection(dict(DEFAULT_ASSUMPTIONS))
    assert rows[0]["year"] == PROJECTION_START_YEAR
    assert rows[-1]["year"] == PROJECTION_END_YEAR
    for row in rows:
        assert row["total_portfolio"] == pytest.approx(
            row["equity_end"] + row["debt_end"] + row["cash_end"], rel=1e-6
        )


def test_base_expense_compounds_at_inflation_pct():
    """Golden values from the plan doc's normal-year walkthrough:
    C(2026)=12, C(2027)=12.72, C(2028)=13.4832, C(2029)=14.2922."""
    assumptions = disabled_assumptions(current_annual_expense_lakhs=12.0, inflation_pct=6.0)
    rows = run_projection(assumptions, start_year=PROJECTION_START_YEAR, end_year=PROJECTION_START_YEAR + 3)

    assert rows[0]["base_expense"] == pytest.approx(12.0)
    assert rows[1]["base_expense"] == pytest.approx(12.72, rel=1e-3)
    assert rows[2]["base_expense"] == pytest.approx(13.4832, rel=1e-3)
    assert rows[3]["base_expense"] == pytest.approx(14.2922, rel=1e-3)

    # Compounding assertion: every year is exactly the prior year * (1 + rate).
    for prev, curr in zip(rows, rows[1:]):
        assert curr["base_expense"] == pytest.approx(prev["base_expense"] * 1.06, rel=1e-3)


def test_kids_basic_snapshots_at_start_year_and_compounds_at_inflation_pct():
    """Kids Basic (30% of C) snapshots at kids_expense_start_year and then
    compounds at general inflation_pct - independent of C's own growth from
    then on. kids_school_start_age is pushed far out so Education never
    contaminates this test."""
    start = PROJECTION_START_YEAR
    kids_start = start + 3
    assumptions = disabled_assumptions(
        current_annual_expense_lakhs=12.0,
        inflation_pct=6.0,
        kids_expense_start_year=kids_start,
        kids_school_start_age=50,
        kids_expense_end_year=PROJECTION_END_YEAR,
    )
    rows = run_projection(assumptions, start_year=start, end_year=kids_start + 2)
    by_year = {r["year"]: r for r in rows}

    assert by_year[kids_start - 1]["kids_expense"] == 0.0
    assert by_year[kids_start]["kids_expense"] == pytest.approx(
        0.30 * by_year[kids_start]["base_expense"], rel=1e-3
    )
    # Compounding assertion: Basic grows at inflation_pct after its snapshot,
    # not by recomputing 0.30 x C(y) fresh each year.
    assert by_year[kids_start + 1]["kids_expense"] == pytest.approx(
        by_year[kids_start]["kids_expense"] * 1.06, rel=1e-3
    )
    assert by_year[kids_start + 2]["kids_expense"] == pytest.approx(
        by_year[kids_start + 1]["kids_expense"] * 1.06, rel=1e-3
    )


def test_kids_education_snapshots_later_than_basic_at_its_own_rate():
    """The exact bug the plan doc had wrong: Education does NOT snapshot
    alongside Basic at kids_expense_start_year - it snapshots later, at
    kids_expense_start_year + kids_school_start_age, and compounds at
    education_inflation_pct (not inflation_pct) from then on. Golden values
    verified directly against run_projection(): D(2029)=4.29 (Basic only),
    D(2034)=7.65 (Education snapshots here), D(2035)=8.19."""
    start = PROJECTION_START_YEAR
    kids_start = start + 3  # 2029
    school_age = 5
    edu_start = kids_start + school_age  # 2034
    assumptions = disabled_assumptions(
        current_annual_expense_lakhs=12.0,
        inflation_pct=6.0,
        education_inflation_pct=10.0,
        kids_expense_start_year=kids_start,
        kids_school_start_age=school_age,
        kids_expense_end_year=PROJECTION_END_YEAR,
    )
    rows = run_projection(assumptions, start_year=start, end_year=edu_start + 1)
    by_year = {r["year"]: r for r in rows}

    # Before edu_start, D(y) is Basic-only - Education contributes nothing.
    assert by_year[edu_start - 1]["kids_expense"] == pytest.approx(5.4131, rel=1e-3)
    assert by_year[kids_start]["kids_expense"] == pytest.approx(4.2877, rel=1e-3)

    # At edu_start, Education snapshots at 10% of THAT year's base expense,
    # on top of Basic's already-independently-compounded value.
    edu_row = by_year[edu_start]
    expected_edu_portion = 0.10 * edu_row["base_expense"]
    expected_basic_portion = by_year[edu_start - 1]["kids_expense"] * 1.06
    assert edu_row["kids_expense"] == pytest.approx(
        expected_basic_portion + expected_edu_portion, rel=1e-3
    )
    assert edu_row["kids_expense"] == pytest.approx(7.65, rel=1e-3)

    # Compounding assertion: post-snapshot, Education compounds at
    # education_inflation_pct - NOT the general inflation_pct Basic uses.
    next_row = by_year[edu_start + 1]
    next_edu_portion = expected_edu_portion * 1.10
    next_basic_portion = expected_basic_portion * 1.06
    assert next_row["kids_expense"] == pytest.approx(next_basic_portion + next_edu_portion, rel=1e-3)
    assert next_row["kids_expense"] == pytest.approx(8.19, rel=1e-3)


def test_kids_expense_stops_after_end_year():
    start = PROJECTION_START_YEAR
    kids_start = start + 1
    kids_end = start + 3
    assumptions = disabled_assumptions(
        current_annual_expense_lakhs=12.0,
        inflation_pct=6.0,
        kids_expense_start_year=kids_start,
        kids_school_start_age=50,  # keep Education out of this window entirely
        kids_expense_end_year=kids_end,
    )
    rows = run_projection(assumptions, start_year=start, end_year=kids_end + 2)
    by_year = {r["year"]: r for r in rows}

    assert by_year[kids_end]["kids_expense"] > 0
    assert by_year[kids_end + 1]["kids_expense"] == 0.0
    assert by_year[kids_end + 2]["kids_expense"] == 0.0


def test_parents_expense_snapshots_and_compounds_at_healthcare_inflation():
    """Parents (40% of C, single component) snapshots at
    parents_expense_start_year and compounds at healthcare_inflation_pct -
    not general inflation_pct - from then on, same shape as Kids but with
    only one sub-component. Also stops after parents_expense_end_year."""
    start = PROJECTION_START_YEAR
    parents_start = start + 2
    parents_end = start + 4
    assumptions = disabled_assumptions(
        current_annual_expense_lakhs=12.0,
        inflation_pct=6.0,
        healthcare_inflation_pct=8.0,
        parents_expense_start_year=parents_start,
        parents_expense_end_year=parents_end,
    )
    rows = run_projection(assumptions, start_year=start, end_year=parents_end + 1)
    by_year = {r["year"]: r for r in rows}

    assert by_year[parents_start - 1]["parents_expense"] == 0.0
    assert by_year[parents_start]["parents_expense"] == pytest.approx(
        0.40 * by_year[parents_start]["base_expense"], rel=1e-3
    )
    # Compounding assertion: healthcare_inflation_pct, not inflation_pct.
    assert by_year[parents_start + 1]["parents_expense"] == pytest.approx(
        by_year[parents_start]["parents_expense"] * 1.08, rel=1e-3
    )
    assert by_year[parents_end]["parents_expense"] > 0
    assert by_year[parents_end + 1]["parents_expense"] == 0.0


def test_salary_stops_exactly_at_retirement_year():
    start = PROJECTION_START_YEAR
    retirement = start + 5
    assumptions = disabled_assumptions(
        current_annual_salary_lakhs=25.0,
        salary_growth_pct=8.0,
        retirement_year=retirement,
    )
    rows = run_projection(assumptions, start_year=start, end_year=retirement + 1)
    by_year = {r["year"]: r for r in rows}

    assert by_year[start]["salary"] == pytest.approx(25.0)
    # Compounding assertion, right up to the boundary.
    assert by_year[retirement - 1]["salary"] == pytest.approx(
        25.0 * (1.08 ** (retirement - 1 - start)), rel=1e-3
    )
    assert by_year[retirement - 1]["salary"] > 0
    # The boundary itself, and every year after, is exactly 0 - not a
    # fraction of a final paycheck, not a gradual taper.
    assert by_year[retirement]["salary"] == 0.0
    assert by_year[retirement + 1]["salary"] == 0.0


@pytest.mark.parametrize(
    "loan_end_offset,emi_amount",
    [
        (5, 8.0),  # active for several years, a "normal" EMI
        (0, 3.0),  # active for exactly one year - the start year itself
        (-1, 8.0),  # loan_end_year before start_year - inactive for the whole horizon
    ],
)
def test_emi_active_flag_and_amount_track_loan_end_year(loan_end_offset, emi_amount):
    start = PROJECTION_START_YEAR
    loan_end = start + loan_end_offset
    assumptions = disabled_assumptions(annual_emi_lakhs=emi_amount, loan_end_year=loan_end)
    rows = run_projection(assumptions, start_year=start, end_year=start + 6)

    for row in rows:
        if row["year"] <= loan_end:
            assert row["emi_active"] is True
            assert row["emi_amount"] == pytest.approx(emi_amount)
        else:
            assert row["emi_active"] is False
            assert row["emi_amount"] == 0.0


def test_withdrawal_waterfall_matches_plan_doc_spec_case():
    """plans/01-phase-1-projection-engine.md §2 "Isolated example": a 10L
    shortfall with 6L of debt and 40L of equity available before withdrawal.
    Debt is drained first (6L), equity covers the remaining 4L, cash is
    floored at 0 rather than ever going negative."""
    start = PROJECTION_START_YEAR
    assumptions = disabled_assumptions(
        current_annual_expense_lakhs=10.0,  # the whole shortfall - no income to offset it
        equity_portfolio_lakhs=40.0,
        debt_portfolio_lakhs=6.0,
    )
    rows = run_projection(assumptions, start_year=start, end_year=start)
    row = rows[0]

    assert row["net_cash_flow"] == pytest.approx(-10.0)
    assert row["cash_end"] == pytest.approx(0.0)
    assert row["debt_end"] == pytest.approx(0.0)
    assert row["equity_end"] == pytest.approx(36.0)
    assert row["total_portfolio"] == pytest.approx(36.0)


def test_equity_can_go_negative_and_compounds_further_negative_while_debt_and_cash_floor_at_zero():
    """
    Locks in an intentional (if surprising) asymmetry in the withdrawal
    waterfall: when a shortfall exceeds BOTH debt and equity combined, debt
    is structurally prevented from going negative (capped at its own
    balance via min()) and cash is explicitly floored at 0 - but equity is
    the last resort in the cash -> debt -> equity order and has no floor.
    Once negative, it keeps compounding further negative at
    equity_return_pct, exactly as a positive balance would compound further
    positive - a future "fix" that adds a matching floor to equity would be
    a real behavior change, not a bug fix, and this test is here so that
    change can't happen silently.
    """
    start = PROJECTION_START_YEAR
    assumptions = disabled_assumptions(
        current_annual_expense_lakhs=30.0,  # shortfall far exceeds debt+equity combined
        debt_portfolio_lakhs=5.0,
        equity_portfolio_lakhs=5.0,
        equity_return_pct=10.0,
    )
    rows = run_projection(assumptions, start_year=start, end_year=start + 1)
    year1, year2 = rows[0], rows[1]

    assert year1["equity_end"] == pytest.approx(-19.5, rel=1e-3)
    assert year1["debt_end"] == pytest.approx(0.0)
    assert year1["cash_end"] == pytest.approx(0.0)

    # Compounding assertion: year 2's equity_start carries the negative
    # balance forward, and equity_growth = equity_start * equity_return_pct
    # - for a negative start, that's itself negative.
    assert year2["equity_start"] == pytest.approx(year1["equity_end"])
    assert year2["equity_growth"] == pytest.approx(year2["equity_start"] * 0.10, rel=1e-3)
    assert year2["equity_growth"] < 0
    assert year2["equity_end"] == pytest.approx(-51.45, rel=1e-3)
    assert year2["equity_end"] < year1["equity_end"]  # strictly more negative
    assert year2["debt_end"] == pytest.approx(0.0)
    assert year2["cash_end"] == pytest.approx(0.0)
