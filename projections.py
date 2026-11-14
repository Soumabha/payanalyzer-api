"""
Pure calculation engine for the FIRE / wealth projection.

No database or FastAPI dependencies here on purpose — this module only knows
how to turn a dict of assumptions (+ optional per-year manual extra income)
into a year-by-year projection table whose columns mirror the original
planning spreadsheet's columns A-X (year through total portfolio).
"""

import calendar
from datetime import date
from typing import Any

from loans import monthly_cash_outflow, summarize_year as summarize_loan_year

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


def _blend_year(
    year: int,
    today: date,
    actual_monthly_spend: dict[str, float],
    assumed_total_expense: float,
    loan_outflow_months: list[float],
    total_income: float,
) -> dict[str, Any]:
    """
    Month-by-month view of one year that is not wholly in the future.

    Month status, relative to `today`:
      actual       - month already closed and has recorded spend: expense is
                     that spend plus the loan payment (EMI is debited straight
                     from the bank, so it never appears in the expense records).
                     Each month reports `spend` (the non-loan part) and
                     `loan_payment` separately, so it reconciles to the
                     expense records: expense == spend + loan_payment.
      no_data      - month closed but has no recorded spend (most likely a
                     sync/import gap): falls back to the forecast and is
                     flagged so the user can fix the gap.
      in_progress  - the current month: forecast is used until it closes; the
                     spend recorded so far is shown for information only.
      forecast     - a month still to come in the current year.

    `loan_outflow_months` is the 12 monthly loan payments (EMI + prepayment)
    already inside the assumed total. A forecast month spends the non-loan part
    of the assumed total evenly (/12) plus that month's own loan payment; an
    actual month is recorded spend plus that month's loan payment. With a flat
    EMI this is simply assumed total / 12 for forecast months. Income is
    always the assumed annual figure / 12 (there is no income data source).
    """
    non_loan_month = (assumed_total_expense - sum(loan_outflow_months)) / 12
    income_month = total_income / 12

    months = []
    year_expense = 0.0
    actual_spend_total = 0.0
    no_data_months = 0
    all_actual = True

    for month_number in range(1, 13):
        key = f"{year}-{month_number:02d}"
        recorded = actual_monthly_spend.get(key)
        if (year, month_number) < (today.year, today.month):
            if recorded is None:
                status = "no_data"
                no_data_months += 1
            else:
                status = "actual"
        elif (year, month_number) == (today.year, today.month):
            status = "in_progress"
        else:
            status = "forecast"
        if status != "actual":
            all_actual = False

        loan_month = loan_outflow_months[month_number - 1]
        forecast_month_expense = non_loan_month + loan_month
        if status == "actual":
            expense = recorded + loan_month
            actual_spend_total += recorded
        else:
            expense = forecast_month_expense

        months.append({
            "month": key,
            "status": status,
            "expense": round(expense, 4),
            "spend": round(expense - loan_month, 4),
            "loan_payment": round(loan_month, 4),
            "actual_spend": round(recorded, 4) if recorded is not None and status in ("actual", "in_progress") else None,
            "forecast_expense": round(forecast_month_expense, 4),
            "income": round(income_month, 4),
            "net": round(income_month - expense, 4),
        })
        year_expense += expense

    return {
        "total_expense": year_expense,
        "months": months,
        "actual_spend": actual_spend_total,
        "no_data_months": no_data_months,
        "source": "actual" if all_actual else "blended",
    }


BUCKETS = ("cash", "equity", "debt")


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _anchors_by_year(
    observations: list[dict[str, Any]], start_year: int, end_year: int
) -> dict[str, dict[int, tuple[date, float]]]:
    """bucket -> year -> (as_of, balance): the latest observation in that year
    (ties: the later in the list). Observations outside the window are ignored."""
    anchors: dict[str, dict[int, tuple[date, float]]] = {}
    for observation in observations:
        bucket = observation["bucket"]
        if bucket not in BUCKETS:
            continue
        as_of = _as_date(observation["as_of"])
        if not start_year <= as_of.year <= end_year:
            continue
        current = anchors.setdefault(bucket, {}).get(as_of.year)
        if current is None or as_of >= current[0]:
            anchors[bucket][as_of.year] = (as_of, observation["amount_lakhs"])
    return anchors


def _transfer_deltas_by_year(
    transfers: list[dict[str, Any]], observations: list[dict[str, Any]], start_year: int, end_year: int
) -> dict[int, dict[str, float]]:
    latest_observed: dict[str, date] = {}
    for observation in observations:
        if observation["bucket"] in BUCKETS:
            as_of = _as_date(observation["as_of"])
            if observation["bucket"] not in latest_observed or as_of > latest_observed[observation["bucket"]]:
                latest_observed[observation["bucket"]] = as_of

    deltas: dict[int, dict[str, float]] = {}
    for transfer in transfers:
        moved_on = _as_date(transfer["date"])
        touched = (transfer["from_bucket"], transfer["to_bucket"])
        if not start_year <= moved_on.year <= end_year:
            continue
        if any(bucket in latest_observed and latest_observed[bucket] >= moved_on for bucket in touched):
            continue
        year_deltas = deltas.setdefault(moved_on.year, {bucket: 0.0 for bucket in BUCKETS})
        if transfer["from_bucket"] in year_deltas:
            year_deltas[transfer["from_bucket"]] -= transfer["amount_lakhs"]
        if transfer["to_bucket"] in year_deltas:
            year_deltas[transfer["to_bucket"]] += transfer["amount_lakhs"]
    return deltas


def _elapsed_year_fraction(as_of: date) -> float:
    days_in_year = 366 if calendar.isleap(as_of.year) else 365
    return as_of.timetuple().tm_yday / days_in_year


def _elapsed_net_cash_flow(as_of: date, annual_net: float, blend: dict[str, Any] | None) -> float:
    """Net cash flow of the year up to the end of `as_of`: month by month when
    the year is blended with actuals, otherwise pro rata by day."""
    if blend is None:
        return annual_net * _elapsed_year_fraction(as_of)
    elapsed = sum(month["net"] for month in blend["months"][: as_of.month - 1])
    days_in_month = calendar.monthrange(as_of.year, as_of.month)[1]
    return elapsed + blend["months"][as_of.month - 1]["net"] * as_of.day / days_in_month


def run_projection(
    assumptions: dict[str, Any],
    extra_income_by_year: dict[int, float] | None = None,
    start_year: int = PROJECTION_START_YEAR,
    end_year: int = PROJECTION_END_YEAR,
    actual_monthly_spend: dict[str, float] | None = None,
    today: date | None = None,
    loan_timeline: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    transfers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Returns one dict per year from start_year to end_year (inclusive),
    matching spreadsheet columns A-X. The formulas are documented inline,
    step by step, in the loop below; test_projections.py holds verified
    worked examples for each of them.

    Actuals mode (opt-in): pass `actual_monthly_spend` - a {"YYYY-MM": lakhs}
    dict of recorded spend for the months that have any - and years up to
    `today`'s year are blended month by month (see _blend_year). Those rows
    gain `expense_source`, `assumed_total_expense`, `actual_spend`,
    `no_data_months` and a 12-entry `months` list; future rows only gain
    `expense_source: "forecast"`. Only that year's total expense (and so its
    cash flow) changes: the assumed base-expense chain that kids/parents and
    all later years derive from is never re-based on actuals. Without
    `actual_monthly_spend` the output is exactly the plain forecast, with no
    extra keys. `today` is injectable for tests and defaults to date.today().

    Journal inputs (each optional; omitted -> exactly the plain forecast):
      loan_timeline - monthly rows from loans.build_timeline(). When non-empty
                      it replaces the flat annual EMI: each year's EMI and
                      prepayments come from the schedule, rows gain
                      loan_interest / loan_principal / loan_prepayment /
                      loan_balance_end, and prepayments are part of total
                      expense (so they are deducted from cash).
      observations  - known cash/equity/debt balances ({bucket, as_of,
                      amount_lakhs}). For the year of the latest observation
                      of a bucket the year's start balance is replaced by the
                      one IMPLIED by the observation (cash: observed balance
                      minus the net cash flow elapsed to that date; equity/
                      debt: observed balance de-grown by the elapsed share of
                      the year's return), so the year closes where the
                      observation plus the rest of the year's flows say it
                      should. Later years carry on from there. Anchored rows
                      carry an `anchored` {bucket: as_of} dict.
      transfers     - invest/withdraw entries ({date, amount_lakhs,
                      from_bucket, to_bucket}) applied at the end of their
                      year, except those dated on/before an observation of a
                      bucket they touch (that balance already includes them).
                      Growth on a transferred amount is not modelled in the
                      year of transfer. Rows gain a `transfers` dict.
    """
    validate_assumptions(assumptions)
    extra_income_by_year = extra_income_by_year or {}
    loan_enabled = bool(loan_timeline)
    anchors = _anchors_by_year(observations or [], start_year, end_year)
    transfer_deltas = _transfer_deltas_by_year(transfers or [], observations or [], start_year, end_year)
    actuals_enabled = actual_monthly_spend is not None
    if actuals_enabled and today is None:
        today = date.today()

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

        # F/G - EMI. With a loan schedule (journal) the amounts come from the
        # month-by-month amortization; otherwise the flat annual assumption.
        if loan_enabled:
            loan_year = summarize_loan_year(loan_timeline, year)
            emi_amount = loan_year["emi"]
            emi_active = emi_amount > 0
            loan_prepayment = loan_year["prepayment"]
            loan_outflow_months = monthly_cash_outflow(loan_timeline, year)
        else:
            emi_active = year <= loan_end_year
            emi_amount = assumptions["annual_emi_lakhs"] if emi_active else 0.0
            loan_prepayment = 0.0
            loan_outflow_months = [emi_amount / 12] * 12

        # H - Total Expense
        total_expense = base_expense + kids_expense + parents_expense + emi_amount + loan_prepayment

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

        # Actuals mode only: replace this year's assumed total expense with
        # the month-by-month blend. Everything above (the assumed chains) is
        # untouched, so later years are unaffected.
        assumed_total_expense = total_expense
        blend = None
        if actuals_enabled and year <= today.year:
            blend = _blend_year(
                year, today, actual_monthly_spend, assumed_total_expense, loan_outflow_months, total_income
            )
            total_expense = blend["total_expense"]

        # M/N - Net Expense / Reserve Contribution
        net_expense = max(0.0, total_expense - total_income)
        reserve_contribution = max(0.0, total_income - total_expense)

        # O/P/Q - Cash flow
        net_cash_flow = reserve_contribution - net_expense
        year_anchors = {bucket: anchors[bucket][year] for bucket in anchors if year in anchors[bucket]}
        if "cash" in year_anchors:
            observed_on, observed_cash = year_anchors["cash"]
            cash_start = observed_cash - _elapsed_net_cash_flow(observed_on, net_cash_flow, blend)
        else:
            cash_start = cash_end_prev
        cash_end = max(0.0, cash_start + net_cash_flow)

        # Withdrawal waterfall: cash -> debt -> equity
        cash_shortfall = max(0.0, -(cash_start + net_cash_flow))

        # R/S - Equity start/growth (before withdrawal)
        if "equity" in year_anchors:
            observed_on, observed_equity = year_anchors["equity"]
            equity_start = observed_equity / (1 + equity_return) ** _elapsed_year_fraction(observed_on)
        else:
            equity_start = equity_end_prev
        equity_growth = equity_start * equity_return

        # U/V - Debt start/growth (before withdrawal)
        if "debt" in year_anchors:
            observed_on, observed_debt = year_anchors["debt"]
            debt_start = observed_debt / (1 + debt_return) ** _elapsed_year_fraction(observed_on)
        else:
            debt_start = debt_end_prev
        debt_growth = debt_start * debt_return

        debt_withdrawal = min(cash_shortfall, debt_start + debt_growth)
        equity_withdrawal = max(0.0, cash_shortfall - (debt_start + debt_growth))

        # T/W - Equity/Debt end
        equity_end = equity_start + equity_growth - equity_withdrawal
        debt_end = debt_start + debt_growth - debt_withdrawal

        # Journal invest/withdraw transfers between buckets (after the
        # waterfall, so they never trigger it).
        year_transfers = transfer_deltas.get(year)
        if year_transfers:
            cash_end = max(0.0, cash_end + year_transfers["cash"])
            equity_end += year_transfers["equity"]
            debt_end += year_transfers["debt"]

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

        if loan_enabled:
            rows[-1].update({
                "loan_interest": round(loan_year["interest"], 4),
                "loan_principal": round(loan_year["principal"], 4),
                "loan_prepayment": round(loan_prepayment, 4),
                "loan_balance_end": None if loan_year["closing"] is None else round(loan_year["closing"], 4),
            })
        if year_anchors:
            rows[-1]["anchored"] = {bucket: pair[0].isoformat() for bucket, pair in year_anchors.items()}
        if year_transfers:
            rows[-1]["transfers"] = {bucket: round(value, 4) for bucket, value in year_transfers.items()}

        if actuals_enabled:
            if blend is None:
                rows[-1]["expense_source"] = "forecast"
            else:
                rows[-1].update({
                    "expense_source": blend["source"],
                    "assumed_total_expense": round(assumed_total_expense, 4),
                    "actual_spend": round(blend["actual_spend"], 4),
                    "no_data_months": blend["no_data_months"],
                    "months": blend["months"],
                })

        cash_end_prev = cash_end
        equity_end_prev = equity_end
        debt_end_prev = debt_end

    return rows
