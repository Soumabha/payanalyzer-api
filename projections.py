"""
Pure calculation engine for the FIRE / wealth projection.

No database or FastAPI dependencies here on purpose — this module only knows
how to turn a dict of assumptions (+ optional per-year manual extra income)
into a year-by-year projection table whose columns mirror the original
planning spreadsheet's columns A-X (year through total portfolio).
"""

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
    actual month is recorded spend plus that month's loan payment. Income is
    always the assumed annual figure / 12 (there is no income data source).
    """
    non_loan_month = (assumed_total_expense - sum(loan_outflow_months)) / 12
    income_month = total_income / 12

    months = []
    nets = []
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
        nets.append(income_month - expense)
        year_expense += expense

    return {
        "total_expense": year_expense,
        "months": months,
        "nets": nets,
        "actual_spend": actual_spend_total,
        "no_data_months": no_data_months,
        "source": "actual" if all_actual else "blended",
    }


BUCKETS = ("cash", "equity", "debt")


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _month_key(value: date) -> str:
    return f"{value.year}-{value.month:02d}"


def _checkins_by_month(
    observations: list[dict[str, Any]], start_year: int, today: date
) -> dict[str, dict[str, tuple[date, float]]]:
    """month -> bucket -> (as_of, balance). A check-in is snapped to the START of
    its month; if a bucket has several in a month the latest-dated wins (ties:
    the later in the list). Months before start_year or after today's month
    can't be applied and are ignored."""
    by_month: dict[str, dict[str, tuple[date, float]]] = {}
    last_month = _month_key(today)
    for observation in observations:
        bucket = observation["bucket"]
        if bucket not in BUCKETS:
            continue
        as_of = _as_date(observation["as_of"])
        key = _month_key(as_of)
        if as_of.year < start_year or key > last_month:
            continue
        current = by_month.setdefault(key, {}).get(bucket)
        if current is None or as_of >= current[0]:
            by_month[key][bucket] = (as_of, observation["amount_lakhs"])
    return by_month


def _transfer_deltas_by_month(
    transfers: list[dict[str, Any]], observations: list[dict[str, Any]], start_year: int
) -> dict[str, dict[str, float]]:
    """month -> bucket -> net change from invest/withdraw entries, applied at the
    END of their month.

    Each side of a transfer is judged on its own: a side is skipped only if THAT
    bucket has a check-in in the same month dated on/after the transfer - the
    balance observed then already includes it. The other side is still applied,
    so a check-in of just one bucket never drops (or duplicates) the half of a
    transfer it did not observe. A transfer in an earlier month than a check-in
    is applied in full; the check-in then shows only what it did not explain."""
    deltas: dict[str, dict[str, float]] = {}
    for transfer in transfers:
        moved_on = _as_date(transfer["date"])
        if moved_on.year < start_year:
            continue
        month = _month_key(moved_on)

        def observed_after(bucket: str) -> bool:
            return any(
                observation["bucket"] == bucket
                and _month_key(_as_date(observation["as_of"])) == month
                and _as_date(observation["as_of"]) >= moved_on
                for observation in observations
            )

        for bucket, sign in ((transfer["from_bucket"], -1), (transfer["to_bucket"], 1)):
            if bucket in BUCKETS and not observed_after(bucket):
                month_deltas = deltas.setdefault(month, {name: 0.0 for name in BUCKETS})
                month_deltas[bucket] += sign * transfer["amount_lakhs"]
    return deltas


def _monthly_balance_path(
    blend: dict[str, Any],
    cash: float,
    equity: float,
    debt: float,
    equity_return: float,
    debt_return: float,
    checkins: dict[str, dict[str, tuple[date, float]]],
    transfer_deltas: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """
    Month-by-month cash/equity/debt balances for one blended year, carried from
    the year's (forecast) start balances. Each month:
      1. a check-in snapped to this month resets the observed buckets to the
         observed balance; the variance is observed minus what the path held
         at the start of the month;
      2. the month's net cash flow hits cash; a shortfall is withdrawn from
         debt first, then equity (the same cash -> debt -> equity waterfall as
         the annual engine, which may leave equity negative);
      3. equity and debt grow by (1 + annual return)^(1/12) - 1;
      4. invest/withdraw transfers dated in the month move money at month end.
    Expenses and income are not touched: a check-in only moves balances.
    """
    equity_monthly = (1 + equity_return) ** (1 / 12) - 1
    debt_monthly = (1 + debt_return) ** (1 / 12) - 1
    balances = {"cash": cash, "equity": equity, "debt": debt}
    adjustments = {bucket: 0.0 for bucket in BUCKETS}
    transfer_totals = {bucket: 0.0 for bucket in BUCKETS}
    equity_growth_total = debt_growth_total = 0.0
    checkin_months = []
    months_out = []

    for month, net in zip(blend["months"], blend["nets"]):
        key = month["month"]
        marks = {}
        for bucket, (as_of, observed) in checkins.get(key, {}).items():
            expected = balances[bucket]
            marks[bucket] = {
                "as_of": as_of.isoformat(),
                "observed": round(observed, 4),
                "expected": round(expected, 4),
                "variance": round(observed - expected, 4),
            }
            adjustments[bucket] += observed - expected
            balances[bucket] = observed
        if marks:
            checkin_months.append(key)

        cash_after = balances["cash"] + net
        shortfall = max(0.0, -cash_after)
        cash_now = max(0.0, cash_after)
        equity_growth = balances["equity"] * equity_monthly
        debt_growth = balances["debt"] * debt_monthly
        debt_available = balances["debt"] + debt_growth
        debt_withdrawal = min(shortfall, debt_available)
        equity_withdrawal = max(0.0, shortfall - debt_available)
        equity_now = balances["equity"] + equity_growth - equity_withdrawal
        debt_now = balances["debt"] + debt_growth - debt_withdrawal
        equity_growth_total += equity_growth
        debt_growth_total += debt_growth

        moved = transfer_deltas.get(key)
        if moved:
            cash_now = max(0.0, cash_now + moved["cash"])
            equity_now += moved["equity"]
            debt_now += moved["debt"]
            for bucket in BUCKETS:
                transfer_totals[bucket] += moved[bucket]
        balances = {"cash": cash_now, "equity": equity_now, "debt": debt_now}

        months_out.append({
            "cash_end": round(cash_now, 4),
            "equity_end": round(equity_now, 4),
            "debt_end": round(debt_now, 4),
            "total_portfolio": round(cash_now + equity_now + debt_now, 4),
            "checkin": marks or None,
            "checkin_total_variance": round(sum(m["variance"] for m in marks.values()), 4) if marks else None,
        })

    return {
        "months": months_out,
        "cash": balances["cash"],
        "equity": balances["equity"],
        "debt": balances["debt"],
        "equity_growth": equity_growth_total,
        "debt_growth": debt_growth_total,
        "adjustments": adjustments,
        "checkin_months": checkin_months,
        "transfer_totals": transfer_totals if any(transfer_totals.values()) else None,
    }


def _merge_loan_into_months(months: list[dict[str, Any]], loan_by_month: dict[str, dict[str, Any]]) -> None:
    """Per-month loan columns; a statement balance that disagreed with the
    computed schedule becomes a `loan` check-in variance (observed - computed,
    so a lower balance than expected is negative)."""
    for month in months:
        row = loan_by_month.get(month["month"])
        month["loan_interest"] = row["interest"] if row else 0.0
        month["loan_principal"] = row["principal"] if row else 0.0
        month["loan_prepayment"] = row["prepayment"] if row else 0.0
        month["loan_balance_end"] = row["closing"] if row else 0.0
        if row and row["drift"] is not None and abs(row["drift"]) >= 0.00005:
            marks = month["checkin"] or {}
            marks["loan"] = {
                "observed": row["closing"],
                "expected": round(row["closing"] + row["drift"], 4),
                "variance": round(-row["drift"], 4),
            }
            month["checkin"] = marks


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
      loan_timeline - monthly rows from loans.build_timeline(). Without it the
                      projection has no loan payments. Each year's EMI and
                      prepayments come from the schedule, and rows gain
                      loan_interest / loan_principal / loan_prepayment /
                      loan_balance_end (detail only: the table shows none of
                      them). emi_amount is the year's EMI plus any prepayment,
                      so prepayments are part of total expense and deducted
                      from cash.
      observations  - check-ins: known cash/equity/debt balances ({bucket,
                      as_of, amount_lakhs}); actuals mode only. A check-in is
                      snapped to the START of its month: that month's start
                      balance becomes the observed one and the rest of the
                      path (months, later years) follows from it. The balance
                      the path held before is kept, so each check-in carries
                      a variance (observed - expected) shown on its month
                      row; an earlier check-in sets the path until the next
                      one. Start columns are never rewritten: a year's start
                      balances are always the forecast carried in, and the
                      year's adjustments appear as cash/equity/debt_adjustment.
                      Check-ins after today's month are ignored.
      transfers     - invest/withdraw entries ({date, amount_lakhs,
                      from_bucket, to_bucket}), actuals mode only, applied at
                      the end of their month; a side whose bucket has a
                      same-month check-in on/after the transfer is skipped
                      (that balance already includes it), the other side is
                      not. Rows gain a `transfers` dict.

    In actuals mode every year up to today's runs month by month for balances
    (see _monthly_balance_path). Expenses and income are never changed by a
    check-in: past months use recorded spend, everything else the settings.
    """
    validate_assumptions(assumptions)
    extra_income_by_year = extra_income_by_year or {}
    loan_enabled = bool(loan_timeline)
    actuals_enabled = actual_monthly_spend is not None
    if (observations or transfers) and not actuals_enabled:
        raise ValueError("observations and transfers are only applied in actuals mode (pass actual_monthly_spend)")
    if actuals_enabled and today is None:
        today = date.today()
    checkins = _checkins_by_month(observations or [], start_year, today) if actuals_enabled else {}
    transfer_months = _transfer_deltas_by_month(transfers or [], observations or [], start_year) if actuals_enabled else {}
    loan_by_month = {row["month"]: row for row in loan_timeline} if loan_enabled else {}

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

        # F/G - EMI. Loans live in the journal: with a loan schedule the amounts
        # come from its month-by-month amortization; without one there is no
        # loan payment at all (months before a loan's first recorded balance
        # carry none either).
        if loan_enabled:
            loan_year = summarize_loan_year(loan_timeline, year)
            # EMI Amt is every loan payment of the year, a prepayment included,
            # so the visible expense columns always add up to Total Exp.
            loan_prepayment = loan_year["prepayment"]
            emi_amount = loan_year["emi"] + loan_prepayment
            emi_active = emi_amount > 0
            loan_outflow_months = monthly_cash_outflow(loan_timeline, year)
        else:
            emi_active = False
            emi_amount = 0.0
            loan_prepayment = 0.0
            loan_outflow_months = [0.0] * 12

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
        cash_start = cash_end_prev
        equity_start = equity_end_prev
        debt_start = debt_end_prev

        if blend is not None:
            # Blended (actuals) years run month by month so check-ins and
            # transfers land in their own month; see _monthly_balance_path.
            path = _monthly_balance_path(
                blend, cash_start, equity_start, debt_start, equity_return, debt_return, checkins, transfer_months
            )
            for month, extra in zip(blend["months"], path["months"]):
                month.update(extra)
            if loan_enabled:
                _merge_loan_into_months(blend["months"], loan_by_month)
            cash_end, equity_end, debt_end = path["cash"], path["equity"], path["debt"]
            equity_growth, debt_growth = path["equity_growth"], path["debt_growth"]
        else:
            path = None
            cash_end = max(0.0, cash_start + net_cash_flow)

            # Withdrawal waterfall: cash -> debt -> equity
            cash_shortfall = max(0.0, -(cash_start + net_cash_flow))

            # R/S - Equity growth (before withdrawal)
            equity_growth = equity_start * equity_return

            # U/V - Debt growth (before withdrawal)
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

        if loan_enabled:
            rows[-1].update({
                "loan_interest": round(loan_year["interest"], 4),
                "loan_principal": round(loan_year["principal"], 4),
                "loan_prepayment": round(loan_prepayment, 4),
                "loan_balance_end": None if loan_year["closing"] is None else round(loan_year["closing"], 4),
            })
        if path is not None:
            if path["checkin_months"]:
                rows[-1]["checkin_months"] = path["checkin_months"]
                for bucket in BUCKETS:
                    rows[-1][f"{bucket}_adjustment"] = round(path["adjustments"][bucket], 4)
            if path["transfer_totals"]:
                rows[-1]["transfers"] = {b: round(v, 4) for b, v in path["transfer_totals"].items()}

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
