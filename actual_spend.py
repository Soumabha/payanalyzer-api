"""
Turns recorded expenses into the monthly-actuals dict the projection engine
consumes. Pure apart from the injected store (any object with
`monthly_totals(since_date)`), so tests use the in-memory fake.

This is the single place where rupees become lakhs: the expenses table stores
rupees, and everything projection-side is in lakhs.
"""

RUPEES_PER_LAKH = 100_000


def monthly_spend_lakhs(monthly_totals):
    """[{"month": "YYYY-MM", "total": rupees}, ...] -> {"YYYY-MM": lakhs}.

    Months that have no rows are simply absent (the engine reads absence as
    "no data", not as zero spend). Rows with no month or no total are skipped.
    """
    spend = {}
    for entry in monthly_totals:
        month, total = entry.get("month"), entry.get("total")
        if not month or total is None:
            continue
        spend[month] = spend.get(month, 0.0) + total / RUPEES_PER_LAKH
    return spend


def load_actual_monthly_spend(store, start_year):
    """Monthly recorded spend in lakhs, from January of start_year onwards
    (earlier history is deliberately ignored: the projection starts there)."""
    return monthly_spend_lakhs(store.monthly_totals(f"{start_year}-01-01"))
