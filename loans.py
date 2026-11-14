"""
Pure loan maths: monthly amortization, prepayments, rate/EMI changes,
re-basing on observed balances.

No database, no FastAPI, no clock. Amounts are in lakhs, months are 'YYYY-MM'
strings, rates are annual percentages.

Conventions (kept deliberately simple, and relied on by the tests):
  * An observed balance is the balance on its date, after that month's EMI:
    the schedule continues from the NEXT month. A prepayment dated BEFORE the
    observation date is already inside the observed balance; one dated on or
    after it (same day included) was made after the balance was taken, so it
    is deducted from the observed balance before the schedule continues.
  * Each month: interest = opening balance * annual_rate / 12;
    principal = EMI - interest. Anything paid beyond the interest is
    principal, so paying more than the EMI automatically shortens the tenure
    rather than lowering the EMI.
  * A prepayment is an extra principal payment made after that month's EMI.
    It always reduces the tenure; the EMI stays as set.
  * The final month pays exactly interest + remaining principal.
  * Terms (rate, EMI) apply from the month of their effective date; among
    terms effective in the same month the later-recorded one wins.
  * With several observations, each one re-bases the schedule from its month;
    the difference between what the schedule computed and what was observed
    is reported as `drift` (positive = the schedule thought more was owed).
  * Every prepayment is a real cash outflow in its month, even one dated
    before the balance it would have reduced was observed (that balance
    already reflects it); `prepayment` on a timeline row is that cash outflow.
"""

EPS = 1e-9
MAX_SCHEDULE_MONTHS = 600


def month_index(year_month: str) -> int:
    year, month = year_month.split("-")
    return int(year) * 12 + int(month) - 1


def month_str(index: int) -> str:
    return f"{index // 12}-{index % 12 + 1:02d}"


def emi_from_tenure(principal: float, annual_rate_pct: float, months: int) -> float:
    """The standard annuity EMI that clears `principal` in `months` payments."""
    if months <= 0:
        raise ValueError("months must be positive")
    monthly_rate = annual_rate_pct / 1200
    if monthly_rate == 0:
        return principal / months
    growth = (1 + monthly_rate) ** months
    return principal * monthly_rate * growth / (growth - 1)


def tenure_from_emi(principal: float, annual_rate_pct: float, emi: float) -> int | None:
    """Months an EMI needs to clear `principal` (None if it never does)."""
    monthly_rate = annual_rate_pct / 1200
    if emi <= principal * monthly_rate + EPS:
        return None
    months = 0
    balance = principal
    while balance > EPS and months < MAX_SCHEDULE_MONTHS:
        balance = balance * (1 + monthly_rate) - emi
        months += 1
    return months if balance <= EPS else None


def terms_for_month(terms: list[dict], year_month: str) -> dict:
    """The terms in force in `year_month`: the latest-effective one that has
    started, or the earliest one if none has started yet."""
    ordered = sorted(
        enumerate(terms), key=lambda pair: (pair[1]["effective_from"][:7], pair[1].get("id", 0), pair[0])
    )
    started = [t for _, t in ordered if t["effective_from"][:7] <= year_month]
    return started[-1] if started else ordered[0][1]


def _prepaid_by_month(prepayments: list[dict]) -> dict[str, float]:
    prepaid: dict[str, float] = {}
    for prepayment in prepayments:
        key = prepayment["date"][:7]
        prepaid[key] = prepaid.get(key, 0.0) + prepayment["amount_lakhs"]
    return prepaid


def amortize(
    opening_balance: float,
    opening_month: str,
    terms: list[dict],
    prepayments: list[dict],
    end_month: str | None = None,
    max_months: int = MAX_SCHEDULE_MONTHS,
) -> list[dict]:
    """
    Monthly schedule from the month after `opening_month` until the loan is
    cleared, `end_month` is passed, or `max_months` is reached. Prepayments
    dated in or before `opening_month` are ignored (the opening balance
    already reflects them).
    """
    prepaid_by_month = _prepaid_by_month(prepayments)
    rows = []
    balance = opening_balance
    start = month_index(opening_month)
    for step in range(1, max_months + 1):
        if balance <= EPS:
            break
        year_month = month_str(start + step)
        if end_month is not None and year_month > end_month:
            break

        current = terms_for_month(terms, year_month)
        rate, emi = current["annual_rate_pct"], current["emi_lakhs"]
        interest = balance * rate / 1200
        if emi >= balance + interest - EPS:
            principal, emi_paid = balance, balance + interest
        else:
            principal, emi_paid = emi - interest, emi
        after_emi = balance - principal
        prepayment = min(prepaid_by_month.get(year_month, 0.0), after_emi)
        closing = after_emi - prepayment
        if closing <= EPS:
            closing = 0.0

        rows.append({
            "month": year_month,
            "opening": round(balance, 4),
            "interest": round(interest, 4),
            "principal": round(principal, 4),
            "emi_paid": round(emi_paid, 4),
            "prepayment": round(prepayment, 4),
            "closing": round(closing, 4),
            "annual_rate_pct": rate,
            "emi_scheduled": emi,
            "estimated": False,
            "rebased": False,
            "drift": None,
        })
        balance = closing
    return rows


def _observations_by_month(observations: list[dict]) -> list[tuple[str, float, str]]:
    """(month, balance, as_of date) in month order; if a month has several, the
    latest-dated one wins (ties: the later in the given list)."""
    ordered = sorted(enumerate(observations), key=lambda pair: (pair[1]["as_of"][:10], pair[0]))
    latest: dict[str, tuple[float, str]] = {}
    for _, observation in ordered:
        latest[observation["as_of"][:7]] = (observation["amount_lakhs"], observation["as_of"][:10])
    return [(month, balance, as_of) for month, (balance, as_of) in sorted(latest.items())]


def _prepaid_after_observation(prepayments: list[dict], month: str, as_of: str) -> float:
    """Prepayments in the observation's month dated on/after its date: made
    after the balance was taken, so not yet in it."""
    return sum(
        p["amount_lakhs"] for p in prepayments if p["date"][:7] == month and p["date"][:10] >= as_of
    )


def build_timeline(
    observations: list[dict],
    terms: list[dict],
    prepayments: list[dict],
    start_year: int,
    end_year: int,
) -> list[dict]:
    """
    The loan's monthly rows for [start_year, end_year].

    Months before the first observation have no known balance: their EMI is
    assumed paid at the terms' EMI with no interest/principal split, and they
    are marked `estimated` (closing None, except in the observation's own
    month, where the balance is the observed one less any prepayment made
    after it). From each observation on, the schedule is computed forward
    until the next observation re-bases it; `drift` on that row is what the
    schedule computed minus what was observed.
    """
    if not observations:
        return []
    anchors = _observations_by_month(observations)
    prepaid_by_month = _prepaid_by_month(prepayments)
    window_start, window_end = f"{start_year}-01", f"{end_year}-12"

    rows: dict[str, dict] = {}
    first_month, first_balance, first_date = anchors[0]
    first_late = _prepaid_after_observation(prepayments, first_month, first_date)
    first_index = min(month_index(window_start), month_index(first_month))
    for index in range(first_index, month_index(first_month) + 1):
        year_month = month_str(index)
        rows[year_month] = {
            "month": year_month,
            "interest": 0.0,
            "principal": 0.0,
            "emi_paid": terms_for_month(terms, year_month)["emi_lakhs"],
            "prepayment": round(prepaid_by_month.get(year_month, 0.0), 4),
            "closing": None,
            "estimated": True,
            "rebased": False,
            "drift": None,
        }
    rows[first_month]["closing"] = round(max(0.0, first_balance - first_late), 4)
    rows[first_month]["rebased"] = True

    for position, (month, balance, as_of) in enumerate(anchors):
        late = _prepaid_after_observation(prepayments, month, as_of)
        next_anchor = anchors[position + 1] if position + 1 < len(anchors) else None
        end_month = next_anchor[0] if next_anchor else window_end
        segment = amortize(max(0.0, balance - late), month, terms, prepayments, end_month=end_month)
        for row in segment:
            rows[row["month"]] = row
        if next_anchor is not None:
            next_month, next_balance, next_date = next_anchor
            next_late = _prepaid_after_observation(prepayments, next_month, next_date)
            landed = rows.get(next_month)
            if landed is None:
                # The schedule had already cleared before the next observation.
                landed = {
                    "month": next_month, "interest": 0.0, "principal": 0.0, "emi_paid": 0.0,
                    "prepayment": round(prepaid_by_month.get(next_month, 0.0), 4),
                    "closing": 0.0, "estimated": True, "rebased": False, "drift": None,
                }
                rows[next_month] = landed
            # The computed month already includes this month's late prepayments;
            # the observation does not, so compare like with like.
            landed["drift"] = round(landed["closing"] + next_late - next_balance, 4)
            landed["closing"] = round(max(0.0, next_balance - next_late), 4)
            landed["rebased"] = True

    return [row for month, row in sorted(rows.items()) if window_start <= month <= window_end]


def summarize_year(timeline: list[dict], year: int) -> dict:
    """Annual totals for the engine's loan columns. Years after payoff (or
    with no rows) are all zero with a zero balance; a year entirely before the
    first observation has no known balance (None)."""
    prefix = f"{year}-"
    in_year = [row for row in timeline if row["month"].startswith(prefix)]
    return {
        "emi": sum(row["emi_paid"] for row in in_year),
        "interest": sum(row["interest"] for row in in_year),
        "principal": sum(row["principal"] for row in in_year),
        "prepayment": sum(row["prepayment"] for row in in_year),
        "closing": in_year[-1]["closing"] if in_year else 0.0,
        "estimated_months": sum(1 for row in in_year if row["estimated"]),
    }


def monthly_cash_outflow(timeline: list[dict], year: int) -> list[float]:
    """EMI + prepayment paid in each month of `year` (12 values)."""
    by_month = {row["month"]: row["emi_paid"] + row["prepayment"] for row in timeline}
    return [by_month.get(f"{year}-{m:02d}", 0.0) for m in range(1, 13)]


def forward_schedule(observations: list[dict], terms: list[dict], prepayments: list[dict]) -> list[dict]:
    """The remaining schedule from the latest observation until payoff."""
    month, balance, as_of = _observations_by_month(observations)[-1]
    late = _prepaid_after_observation(prepayments, month, as_of)
    return amortize(max(0.0, balance - late), month, terms, prepayments)


def summarize_schedule(observations: list[dict], terms: list[dict], prepayments: list[dict], today_month: str) -> dict:
    """Headline numbers for the loan page, from the latest observation on."""
    month, balance, as_of = _observations_by_month(observations)[-1]
    late = _prepaid_after_observation(prepayments, month, as_of)
    schedule = forward_schedule(observations, terms, prepayments)
    paid_off = bool(schedule) and schedule[-1]["closing"] == 0.0
    elapsed = [row for row in schedule if row["month"] <= today_month]
    return {
        "balance_as_of": month,
        "balance_observed": round(balance, 4),
        "current_balance": round(elapsed[-1]["closing"] if elapsed else max(0.0, balance - late), 4),
        "months_remaining": len([row for row in schedule if row["month"] > today_month]) if paid_off else None,
        "payoff_month": schedule[-1]["month"] if paid_off else None,
        "total_interest_remaining": round(sum(row["interest"] for row in schedule), 4),
        "total_prepaid_remaining": round(sum(row["prepayment"] for row in schedule) + late, 4),
    }


def preview_prepayment(
    observations: list[dict], terms: list[dict], prepayments: list[dict], new_prepayment: dict
) -> dict:
    """What one more prepayment would change: payoff month, months and
    interest saved."""
    before = forward_schedule(observations, terms, prepayments)
    after = forward_schedule(observations, terms, [*prepayments, new_prepayment])

    def payoff(rows):
        return rows[-1]["month"] if rows and rows[-1]["closing"] == 0.0 else None

    both_cleared = payoff(before) is not None and payoff(after) is not None
    return {
        "payoff_month_before": payoff(before),
        "payoff_month_after": payoff(after),
        "months_saved": len(before) - len(after) if both_cleared else None,
        "interest_saved": round(sum(r["interest"] for r in before) - sum(r["interest"] for r in after), 4),
    }
