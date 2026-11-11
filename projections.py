"""
Pure calculation engine for the FIRE / wealth projection.

No database or FastAPI dependencies here on purpose — this module only knows
how to turn a dict of assumptions (+ optional per-year manual extra income)
into a year-by-year projection table, exactly matching the columns (A-X)
described in plans/01-phase-1-projection-engine.md.
"""

from typing import Any

# Fixed horizon start/end used as the API defaults and as the anchor for
# assumption validation (projection_data.save_assumptions) - named constants
# so 2026/2070 aren't duplicated as bare literals in multiple places (main.py,
# DEFAULT_ASSUMPTIONS' open-ended end-year fields, and this module's own
# run_projection default) - change the horizon here and it takes effect
# everywhere that imports these instead of hardcoding its own copy. They also
# mirror PROJECTION_START_YEAR/PROJECTION_END_YEAR in static/projection.js,
# which can't share this constant directly (no shared runtime between Python
# and the browser) and would need to be kept in sync by hand if changed.
PROJECTION_START_YEAR = 2026
PROJECTION_END_YEAR = 2070

# Ordered list of every field a valid `assumptions` dict must contain.
ASSUMPTION_FIELDS = [
    "current_annual_expense_lakhs",
    "inflation_pct",
    "education_inflation_pct",
    "healthcare_inflation_pct",
    "kids_expense_start_year",
    "kids_school_start_age",
    "kids_expense_end_year",
    "parents_expense_start_year",
    "parents_expense_end_year",
    "loan_outstanding_lakhs",
    "annual_emi_lakhs",
    "loan_end_year",
    "rental_income_lakhs",
    "rent_increase_pct",
    "current_annual_salary_lakhs",
    "salary_growth_pct",
    "retirement_year",
    "starting_cash_lakhs",
    "equity_portfolio_lakhs",
    "equity_return_pct",
    "debt_portfolio_lakhs",
    "debt_return_pct",
]

# Sensible starting values so the form/table isn't empty before the user has
# entered their own numbers.
DEFAULT_ASSUMPTIONS = {
    "current_annual_expense_lakhs": 12.0,
    "inflation_pct": 6.0,
    "education_inflation_pct": 10.0,
    "healthcare_inflation_pct": 8.0,
    "kids_expense_start_year": 2029,
    "kids_school_start_age": 5,
    "kids_expense_end_year": PROJECTION_END_YEAR,
    "parents_expense_start_year": 2037,
    "parents_expense_end_year": PROJECTION_END_YEAR,
    "loan_outstanding_lakhs": 40.0,
    "annual_emi_lakhs": 8.0,
    "loan_end_year": 2031,
    "rental_income_lakhs": 3.0,
    "rent_increase_pct": 5.0,
    "current_annual_salary_lakhs": 25.0,
    "salary_growth_pct": 8.0,
    "retirement_year": 2045,
    "starting_cash_lakhs": 5.0,
    "equity_portfolio_lakhs": 50.0,
    "equity_return_pct": 12.0,
    "debt_portfolio_lakhs": 30.0,
    "debt_return_pct": 7.0,
}


def validate_assumptions(assumptions: dict[str, Any]) -> None:
    missing = [f for f in ASSUMPTION_FIELDS if f not in assumptions]
    if missing:
        raise ValueError(f"Missing assumption fields: {missing}")


def run_projection(
    assumptions: dict[str, Any],
    extra_income_by_year: dict[int, float] | None = None,
    start_year: int = PROJECTION_START_YEAR,
    end_year: int = PROJECTION_END_YEAR,
) -> list[dict[str, Any]]:
    """
    Returns one dict per year from start_year to end_year (inclusive),
    matching spreadsheet columns A-X. See plans/01-phase-1-projection-engine.md
    section 2/3 for the formulas and worked examples this implements.
    """
    validate_assumptions(assumptions)
    extra_income_by_year = extra_income_by_year or {}

    inflation = assumptions["inflation_pct"] / 100
    education_inflation = assumptions["education_inflation_pct"] / 100
    healthcare_inflation = assumptions["healthcare_inflation_pct"] / 100
    rent_increase = assumptions["rent_increase_pct"] / 100
    salary_growth = assumptions["salary_growth_pct"] / 100
    equity_return = assumptions["equity_return_pct"] / 100
    debt_return = assumptions["debt_return_pct"] / 100

    kids_start_year = assumptions["kids_expense_start_year"]
    kids_end_year = assumptions["kids_expense_end_year"]
    # Education costs start later than general kids expenses - once the
    # child actually reaches school age - not from kids_start_year itself.
    kids_edu_start_year = kids_start_year + assumptions["kids_school_start_age"]
    parents_start_year = assumptions["parents_expense_start_year"]
    parents_end_year = assumptions["parents_expense_end_year"]
    loan_end_year = assumptions["loan_end_year"]
    retirement_year = assumptions["retirement_year"]

    rows = []

    # Running state carried from one year to the next. Every *_prev
    # variable below is always assigned the raw (unrounded) value computed
    # that year - only the dicts appended to `rows` are rounded (to 4dp, for
    # display/API output) - so rounding never compounds across the horizon.
    prev_base_expense = None
    prev_rental_income = None
    prev_salary = None
    kids_basic_prev = None
    kids_edu_prev = None
    parents_prev = None
    cash_end_prev = assumptions["starting_cash_lakhs"]
    equity_end_prev = assumptions["equity_portfolio_lakhs"]
    debt_end_prev = assumptions["debt_portfolio_lakhs"]

    for year in range(start_year, end_year + 1):
        offset = year - start_year

        # C - Base Expense
        if year == start_year:
            base_expense = assumptions["current_annual_expense_lakhs"]
        else:
            base_expense = prev_base_expense * (1 + inflation)
        prev_base_expense = base_expense

        # D - Kids Expense: two sub-components on independent timelines.
        # Basic (30%) runs kids_start_year..kids_end_year, snapshotted at
        # kids_start_year and grown by general inflation. Education (10%)
        # only starts once the child reaches kids_edu_start_year (school
        # age), snapshotted then, and grown by education inflation - it
        # never starts at all if that falls after kids_end_year.
        if year < kids_start_year or year > kids_end_year:
            kids_expense = 0.0
            kids_basic_prev = None
            kids_edu_prev = None
        else:
            if year == kids_start_year:
                kids_basic = 0.30 * base_expense
            else:
                kids_basic = kids_basic_prev * (1 + inflation)
            kids_basic_prev = kids_basic

            if year < kids_edu_start_year:
                kids_edu = 0.0
                kids_edu_prev = None
            elif year == kids_edu_start_year:
                kids_edu = 0.10 * base_expense
                kids_edu_prev = kids_edu
            else:
                kids_edu = kids_edu_prev * (1 + education_inflation)
                kids_edu_prev = kids_edu

            kids_expense = kids_basic + kids_edu

        # E - Parents Expense: single component, snapshotted then grown by
        # healthcare inflation (not general inflation). Stops (back to 0)
        # after parents_end_year.
        if year < parents_start_year or year > parents_end_year:
            parents_expense = 0.0
            parents_prev = None
        else:
            if year == parents_start_year:
                parents_expense = 0.40 * base_expense
            else:
                parents_expense = parents_prev * (1 + healthcare_inflation)
            parents_prev = parents_expense

        # F/G - EMI
        emi_active = year <= loan_end_year
        emi_amount = assumptions["annual_emi_lakhs"] if emi_active else 0.0

        # H - Total Expense
        total_expense = base_expense + kids_expense + parents_expense + emi_amount

        # I - Rental Income
        if year == start_year:
            rental_income = assumptions["rental_income_lakhs"]
        else:
            rental_income = prev_rental_income * (1 + rent_increase)
        prev_rental_income = rental_income

        # J - Salary: flat growth, drops to 0 from retirement_year onward.
        if year >= retirement_year:
            salary = 0.0
        elif year == start_year:
            salary = assumptions["current_annual_salary_lakhs"]
        else:
            salary = prev_salary * (1 + salary_growth)
        prev_salary = salary

        # K - Extra Income (manual, ad hoc)
        extra_income = extra_income_by_year.get(year, 0.0)

        # L - Total Income
        total_income = rental_income + salary + extra_income

        # M/N - Net Expense / Reserve Contribution
        net_expense = max(0.0, total_expense - total_income)
        reserve_contribution = max(0.0, total_income - total_expense)

        # O/P/Q - Cash flow
        cash_start = cash_end_prev
        net_cash_flow = reserve_contribution - net_expense
        cash_end = max(0.0, cash_start + net_cash_flow)

        # Withdrawal waterfall: cash -> debt -> equity
        cash_shortfall = max(0.0, -(cash_start + net_cash_flow))

        # R/S - Equity start/growth (before withdrawal)
        equity_start = equity_end_prev
        equity_growth = equity_start * equity_return

        # U/V - Debt start/growth (before withdrawal)
        debt_start = debt_end_prev
        debt_growth = debt_start * debt_return

        debt_withdrawal = min(cash_shortfall, debt_start + debt_growth)
        equity_withdrawal = max(0.0, cash_shortfall - (debt_start + debt_growth))

        # T/W - Equity/Debt end
        equity_end = equity_start + equity_growth - equity_withdrawal
        debt_end = debt_start + debt_growth - debt_withdrawal

        # X - Total Portfolio (year-end snapshot only; does not feed back
        # into equity_end/debt_end).
        total_portfolio = equity_end + debt_end + cash_end

        rows.append({
            "year": year,
            "offset": offset,
            "base_expense": round(base_expense, 4),
            "kids_expense": round(kids_expense, 4),
            "parents_expense": round(parents_expense, 4),
            "emi_active": emi_active,
            "emi_amount": round(emi_amount, 4),
            "total_expense": round(total_expense, 4),
            "rental_income": round(rental_income, 4),
            "salary": round(salary, 4),
            "extra_income": round(extra_income, 4),
            "total_income": round(total_income, 4),
            "net_expense": round(net_expense, 4),
            "reserve_contribution": round(reserve_contribution, 4),
            "cash_start": round(cash_start, 4),
            "net_cash_flow": round(net_cash_flow, 4),
            "cash_end": round(cash_end, 4),
            "equity_start": round(equity_start, 4),
            "equity_growth": round(equity_growth, 4),
            "equity_end": round(equity_end, 4),
            "debt_start": round(debt_start, 4),
            "debt_growth": round(debt_growth, 4),
            "debt_end": round(debt_end, 4),
            "total_portfolio": round(total_portfolio, 4),
        })

        cash_end_prev = cash_end
        equity_end_prev = equity_end
        debt_end_prev = debt_end

    return rows
