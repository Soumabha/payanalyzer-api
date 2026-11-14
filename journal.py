"""
Journal business logic: loan setup/terms/prepayments, check-ins, extra income,
invest/withdraw transfers, and assembling the journal inputs the projection
engine consumes.

Pure apart from the injected `store` (see journal_data.SqliteJournalStore and
fake_journal_store.InMemoryJournalStore). Clock values (`now`, `today`) are
always passed in. Amounts are lakhs.

Only structural checks live here (a date must parse, a bucket must be one of
the known ones, the loan being referred to must exist). There are deliberately
no business-rule validations (sign or size of amounts, rate ranges, ...).
"""

from datetime import date

from loans import (
    build_timeline,
    emi_from_tenure,
    forward_schedule,
    preview_prepayment,
    summarize_schedule,
)

LEGACY_INCOME_SOURCE = "legacy_extra_income"
LEGACY_INCOME_MONTH_DAY = "07-01"  # year-only legacy entries land mid-year
INVESTABLE = ("equity", "debt")
OBSERVABLE_BUCKETS = ("cash", "equity", "debt", "loan")
INCOME_STATUSES = ("planned", "received")


class JournalError(ValueError):
    """A request the journal can't honour (reported to the client as 400)."""


def _iso(value, field="date") -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise JournalError(f"{field} must be a YYYY-MM-DD date") from None


# --------------------------------------------------------------------------- #
# Loan
# --------------------------------------------------------------------------- #

def get_loan(store):
    """The single loan (the schema allows several; the app uses one for now)."""
    loans = store.loans()
    return loans[0] if loans else None


def setup_loan(store, now, *, name, outstanding_lakhs, as_of, annual_rate_pct, emi_lakhs=None, remaining_months=None):
    """Create the loan with its opening balance (the first observation) and
    opening terms. Give either the EMI or the remaining months; the other is
    derived."""
    if get_loan(store) is not None:
        raise JournalError("A loan is already set up. Record rate/EMI changes and prepayments against it instead.")
    if emi_lakhs is None and remaining_months is None:
        raise JournalError("Provide either emi_lakhs or remaining_months.")
    as_of = _iso(as_of, "as_of")
    if emi_lakhs is None:
        emi_lakhs = emi_from_tenure(outstanding_lakhs, annual_rate_pct, remaining_months)
    loan_id = store.add_loan(name, now)
    store.add_observation("loan", loan_id, as_of, outstanding_lakhs, "loan_setup", None, now)
    store.add_terms(loan_id, as_of, annual_rate_pct, emi_lakhs, "Opening terms", now)
    store.commit()
    return loan_id


def add_loan_terms(store, now, *, effective_from, annual_rate_pct, emi_lakhs, note=None, statement_balance_lakhs=None):
    """Record a rate and/or EMI change. This never creates a balance
    observation by itself; one is added only if the caller also supplies the
    statement balance as of the effective date."""
    loan = _require_loan(store)
    effective_from = _iso(effective_from, "effective_from")
    terms_id = store.add_terms(loan["id"], effective_from, annual_rate_pct, emi_lakhs, note, now)
    if statement_balance_lakhs is not None:
        store.add_observation(
            "loan", loan["id"], effective_from, statement_balance_lakhs, "terms_change_statement", note, now
        )
    store.commit()
    return terms_id


def void_loan_terms(store, now, terms_id):
    loan = _require_loan(store)
    remaining = store.terms(loan["id"])
    if not any(t["id"] == terms_id for t in remaining):
        raise JournalError("Unknown loan terms record.")
    if len(remaining) == 1:
        raise JournalError("The loan's only terms record can't be voided.")
    store.void_terms(terms_id, now)
    store.commit()


def add_prepayment(store, now, *, entry_date, amount_lakhs, note=None):
    loan = _require_loan(store)
    entry_id = store.add_entry({
        "kind": "prepayment", "entry_date": _iso(entry_date, "entry_date"), "amount_lakhs": amount_lakhs,
        "ref_id": loan["id"], "note": note, "created_at": now,
    })
    store.commit()
    return entry_id


def add_loan_statement(store, now, *, as_of, outstanding_lakhs, note=None):
    """A bank-statement balance (a loan observation)."""
    loan = _require_loan(store)
    obs_id = store.add_observation("loan", loan["id"], _iso(as_of, "as_of"), outstanding_lakhs, "statement", note, now)
    store.commit()
    return obs_id


def void_observation(store, now, observation_id):
    target = next((o for o in store.observations() if o["id"] == observation_id), None)
    if target is None:
        raise JournalError("Unknown balance observation.")
    if target["bucket"] == "loan":
        same_loan = store.observations("loan", target["ref_id"])
        if len(same_loan) == 1:
            raise JournalError("The loan's only balance observation can't be voided.")
    store.void_observation(observation_id, now)
    store.commit()


def _require_loan(store):
    loan = get_loan(store)
    if loan is None:
        raise JournalError("No loan is set up yet.")
    return loan


def loan_inputs(store):
    """(loan, terms, loan observations, prepayments) or None without a loan."""
    loan = get_loan(store)
    if loan is None:
        return None
    observations = store.observations("loan", loan["id"])
    if not observations:
        return None
    prepayments = [
        {"date": e["entry_date"], "amount_lakhs": e["amount_lakhs"]}
        for e in store.entries("prepayment")
        if e["ref_id"] == loan["id"]
    ]
    return loan, store.terms(loan["id"]), observations, prepayments


def loan_view(store, today: date, start_year, end_year):
    """Everything the loan section shows."""
    inputs = loan_inputs(store)
    if inputs is None:
        return {"loan": None}
    loan, terms, observations, prepayments = inputs
    today_month = today.strftime("%Y-%m")
    return {
        "loan": loan,
        "summary": summarize_schedule(observations, terms, prepayments, today_month),
        "terms": terms,
        "observations": observations,
        "prepayments": [e for e in store.entries("prepayment") if e["ref_id"] == loan["id"]],
        "schedule": forward_schedule(observations, terms, prepayments),
        "timeline": build_timeline(observations, terms, prepayments, start_year, end_year),
    }


def prepayment_preview(store, *, entry_date, amount_lakhs):
    inputs = loan_inputs(store)
    if inputs is None:
        raise JournalError("No loan is set up yet.")
    _, terms, observations, prepayments = inputs
    return preview_prepayment(
        observations, terms, prepayments, {"date": _iso(entry_date, "entry_date"), "amount_lakhs": amount_lakhs}
    )


# --------------------------------------------------------------------------- #
# Check-ins
# --------------------------------------------------------------------------- #

def record_check_in(store, now, *, as_of, cash_lakhs=None, equity_lakhs=None, debt_lakhs=None,
                    loan_outstanding_lakhs=None, note=None):
    """One dated snapshot: each supplied balance becomes an observation."""
    balances = {
        "cash": cash_lakhs, "equity": equity_lakhs, "debt": debt_lakhs, "loan": loan_outstanding_lakhs,
    }
    supplied = {bucket: value for bucket, value in balances.items() if value is not None}
    if not supplied:
        raise JournalError("A check-in needs at least one balance.")
    as_of = _iso(as_of, "as_of")
    loan = get_loan(store)
    if "loan" in supplied and loan is None:
        raise JournalError("No loan is set up yet, so there is no loan balance to check in.")
    ids = {}
    for bucket, value in supplied.items():
        ref_id = loan["id"] if bucket == "loan" else None
        ids[bucket] = store.add_observation(bucket, ref_id, as_of, value, "check_in", note, now)
    store.commit()
    return ids


def check_in_history(store):
    """Observations grouped by date (newest first) for the journal list."""
    grouped: dict[str, dict] = {}
    for obs in store.observations():
        entry = grouped.setdefault(obs["as_of"], {"as_of": obs["as_of"], "balances": {}, "ids": {}, "note": obs["note"]})
        entry["balances"][obs["bucket"]] = obs["amount_lakhs"]
        entry["ids"][obs["bucket"]] = obs["id"]
    return sorted(grouped.values(), key=lambda g: g["as_of"], reverse=True)


# --------------------------------------------------------------------------- #
# Extra income
# --------------------------------------------------------------------------- #

def add_income(store, now, *, entry_date, amount_lakhs, note=None, status="planned", approximate=False):
    if status not in INCOME_STATUSES:
        raise JournalError(f"status must be one of {INCOME_STATUSES}")
    entry_id = store.add_entry({
        "kind": "income", "entry_date": _iso(entry_date, "entry_date"), "amount_lakhs": amount_lakhs,
        "status": status, "approximate": approximate, "note": note, "created_at": now,
    })
    store.commit()
    return entry_id


def extra_income_by_year(store) -> dict[int, float]:
    """Planned and received income both count towards the year's projection;
    a cancelled entry is voided and so excluded."""
    totals: dict[int, float] = {}
    for entry in store.entries("income"):
        year = int(entry["entry_date"][:4])
        totals[year] = totals.get(year, 0.0) + entry["amount_lakhs"]
    return totals


def extra_income_by_year_list(store) -> list[dict]:
    """The pre-journal response shape: one {year, amount_lakhs, note} per year."""
    by_year: dict[int, dict] = {}
    for entry in store.entries("income"):
        year = int(entry["entry_date"][:4])
        row = by_year.setdefault(year, {"year": year, "amount_lakhs": 0.0, "note": entry["note"]})
        row["amount_lakhs"] += entry["amount_lakhs"]
    return [by_year[y] for y in sorted(by_year)]


def set_extra_income_for_year(store, now, year, amount_lakhs, note=None):
    """Legacy wrapper (year-only, one figure per year): replaces the year's
    income entries with a single approximate mid-year one."""
    for entry in store.entries("income"):
        if int(entry["entry_date"][:4]) == year:
            store.void_entry(entry["id"], now)
    store.add_entry({
        "kind": "income", "entry_date": f"{year}-{LEGACY_INCOME_MONTH_DAY}", "amount_lakhs": amount_lakhs,
        "status": "planned", "approximate": True, "note": note, "created_at": now,
    })
    store.commit()


def remove_extra_income_for_year(store, now, year):
    for entry in store.entries("income"):
        if int(entry["entry_date"][:4]) == year:
            store.void_entry(entry["id"], now)
    store.commit()


def migrate_legacy_extra_income(store, now) -> int:
    """Copy pre-journal year-only rows into the ledger as approximate, planned,
    mid-year entries. Idempotent: a year already migrated (even if since
    voided) is never copied again. Returns how many were copied."""
    already = {
        e["legacy_year"]
        for e in store.entries("income", include_voided=True)
        if e["source"] == LEGACY_INCOME_SOURCE
    }
    copied = 0
    for row in store.legacy_extra_income_rows():
        if row["year"] in already:
            continue
        store.add_entry({
            "kind": "income", "entry_date": f"{row['year']}-{LEGACY_INCOME_MONTH_DAY}",
            "amount_lakhs": row["amount_lakhs"], "status": "planned", "approximate": True,
            "note": row["note"], "source": LEGACY_INCOME_SOURCE, "legacy_year": row["year"], "created_at": now,
        })
        copied += 1
    if copied:
        store.commit()
    return copied


def confirm_income(store, entry_id, status):
    """Mark an income entry received (or back to planned)."""
    if status not in INCOME_STATUSES:
        raise JournalError(f"status must be one of {INCOME_STATUSES}")
    entry = store.entry(entry_id)
    if entry is None or entry["kind"] != "income" or entry["voided_at"]:
        raise JournalError("Unknown income entry.")
    store.set_entry_status(entry_id, status)
    store.commit()


# --------------------------------------------------------------------------- #
# Invest / withdraw transfers
# --------------------------------------------------------------------------- #

def add_transfer(store, now, *, kind, entry_date, amount_lakhs, bucket, note=None):
    """invest: cash -> equity|debt.  withdraw: equity|debt -> cash."""
    if kind not in ("invest", "withdraw"):
        raise JournalError("kind must be 'invest' or 'withdraw'")
    if bucket not in INVESTABLE:
        raise JournalError(f"bucket must be one of {INVESTABLE}")
    from_bucket, to_bucket = ("cash", bucket) if kind == "invest" else (bucket, "cash")
    entry_id = store.add_entry({
        "kind": kind, "entry_date": _iso(entry_date, "entry_date"), "amount_lakhs": amount_lakhs,
        "from_bucket": from_bucket, "to_bucket": to_bucket, "note": note, "created_at": now,
    })
    store.commit()
    return entry_id


def transfers_for_projection(store) -> list[dict]:
    return [
        {
            "date": e["entry_date"], "amount_lakhs": e["amount_lakhs"],
            "from_bucket": e["from_bucket"], "to_bucket": e["to_bucket"],
        }
        for kind in ("invest", "withdraw")
        for e in store.entries(kind)
    ]


# --------------------------------------------------------------------------- #
# Entries (shared) and the journal view
# --------------------------------------------------------------------------- #

def void_entry(store, now, entry_id):
    entry = store.entry(entry_id)
    if entry is None or entry["voided_at"]:
        raise JournalError("Unknown entry.")
    store.void_entry(entry_id, now)
    store.commit()


def journal_entries(store, today: date) -> list[dict]:
    """All live ledger entries, newest first. A planned entry whose date has
    passed is flagged `needs_confirmation` (did it actually happen?)."""
    entries = store.entries()
    for entry in entries:
        entry["needs_confirmation"] = (
            entry["kind"] == "income" and entry["status"] == "planned" and entry["entry_date"] < today.isoformat()
        )
    return sorted(entries, key=lambda e: (e["entry_date"], e["id"]), reverse=True)


def projection_inputs(store, start_year, end_year, include_actuals: bool) -> dict:
    """Everything run_projection() takes from the journal. With an empty
    journal every value is empty/None, so the projection is the plain forecast.

    include_actuals=False is the PLAN view: extra income (planned or received)
    and the loan as set up (opening balance + terms changes, no prepayments, no
    later statement balances). include_actuals=True adds what really happened:
    prepayments, later loan balances, cash/equity/debt check-ins and
    invest/withdraw transfers.
    """
    loan_timeline = None
    inputs = loan_inputs(store)
    if inputs is not None:
        _, terms, observations, prepayments = inputs
        if not include_actuals:
            observations = [o for o in observations if o["source"] == "loan_setup"] or observations[:1]
            prepayments = []
        loan_timeline = build_timeline(observations, terms, prepayments, start_year, end_year) or None
    return {
        "extra_income_by_year": extra_income_by_year(store),
        "loan_timeline": loan_timeline,
        "observations": (
            [o for o in store.observations() if o["bucket"] in ("cash", "equity", "debt")]
            if include_actuals else []
        ),
        "transfers": transfers_for_projection(store) if include_actuals else [],
    }
