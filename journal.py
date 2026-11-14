"""
Journal business logic: loan setup/terms/prepayments, check-ins, extra income,
invest/withdraw transfers, and assembling the journal inputs the projection
engine consumes.

Pure apart from the injected `store` (see journal_data.SqliteJournalStore and
fake_journal_store.InMemoryJournalStore). Clock values (`now`, `today`) are
always passed in. Amounts are lakhs.

Only structural checks live here (a date must parse, a bucket must be one of
the known ones, the loan being referred to must exist) plus one rule: things
that have happened (balances, check-ins, prepayments, transfers) can't be dated
in the future. Planned things (extra income, a future rate change) can. There
are deliberately no other business-rule validations (sign or size of amounts,
rate ranges, ...).
"""

from datetime import date

from loans import (
    build_timeline,
    emi_from_tenure,
    forward_schedule,
    merge_timelines,
    monthly_emi_in_force,
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


def _today(now: str) -> str:
    return now[:10]


def _recorded_date(value, now, field="date") -> str:
    """A date for something that HAS happened (a balance, a payment, a move of
    money): it can't be in the future. Planned things (extra income, a future
    rate change) use _iso() and may be."""
    iso = _iso(value, field)
    if iso > _today(now):
        raise JournalError(f"{field} {iso} is in the future; only things that have happened can be recorded.")
    return iso


# --------------------------------------------------------------------------- #
# Loan
# --------------------------------------------------------------------------- #

def get_loans(store):
    """Every open loan, oldest first."""
    return store.loans()


def _require_loan(store, loan_id):
    loan = next((l for l in store.loans() if l["id"] == loan_id), None)
    if loan is None:
        raise JournalError("Unknown loan.")
    return loan


def setup_loan(store, now, *, name, outstanding_lakhs, as_of, annual_rate_pct, emi_lakhs=None, remaining_months=None):
    """Create a loan with its opening balance (the first observation) and
    opening terms. Give either the EMI or the remaining months; the other is
    derived. Any number of loans can be set up."""
    if emi_lakhs is None and remaining_months is None:
        raise JournalError("Provide either emi_lakhs or remaining_months.")
    as_of = _recorded_date(as_of, now, "as_of")
    if emi_lakhs is None:
        emi_lakhs = emi_from_tenure(outstanding_lakhs, annual_rate_pct, remaining_months)
    loan_id = store.add_loan(name, now)
    store.add_observation("loan", loan_id, as_of, outstanding_lakhs, "loan_setup", None, now)
    store.add_terms(loan_id, as_of, annual_rate_pct, emi_lakhs, "Opening terms", now)
    store.commit()
    return loan_id


def add_loan_terms(store, now, *, loan_id, effective_from, annual_rate_pct, emi_lakhs, note=None,
                   statement_balance_lakhs=None):
    """Record a rate and/or EMI change. This never creates a balance
    observation by itself; one is added only if the caller also supplies the
    statement balance as of the effective date."""
    loan = _require_loan(store, loan_id)
    effective_from = _iso(effective_from, "effective_from")
    if statement_balance_lakhs is not None:
        _recorded_date(effective_from, now, "effective_from (a statement balance is a recorded fact)")
    terms_id = store.add_terms(loan["id"], effective_from, annual_rate_pct, emi_lakhs, note, now)
    if statement_balance_lakhs is not None:
        store.add_observation(
            "loan", loan["id"], effective_from, statement_balance_lakhs, "terms_change_statement", note, now
        )
    store.commit()
    return terms_id


def _find_terms(store, terms_id):
    """(loan, that loan's live terms, the record) for a terms id."""
    for loan in store.loans():
        live = store.terms(loan["id"])
        if any(t["id"] == terms_id for t in live):
            return loan, live
    raise JournalError("Unknown loan terms record.")


def edit_loan_terms(store, now, terms_id, *, effective_from, annual_rate_pct, emi_lakhs, note=None):
    """Correct a terms record. The old record is voided and a corrected one
    added, so the history keeps what was entered before."""
    loan, _ = _find_terms(store, terms_id)
    store.add_terms(loan["id"], _iso(effective_from, "effective_from"), annual_rate_pct, emi_lakhs, note, now)
    store.void_terms(terms_id, now)
    store.commit()


def void_loan_terms(store, now, terms_id):
    _, live = _find_terms(store, terms_id)
    if len(live) == 1:
        raise JournalError("The loan's only terms record can't be voided.")
    store.void_terms(terms_id, now)
    store.commit()


def add_prepayment(store, now, *, loan_id, entry_date, amount_lakhs, note=None):
    loan = _require_loan(store, loan_id)
    entry_id = store.add_entry({
        "kind": "prepayment", "entry_date": _recorded_date(entry_date, now, "entry_date"), "amount_lakhs": amount_lakhs,
        "ref_id": loan["id"], "note": note, "created_at": now,
    })
    store.commit()
    return entry_id


def add_loan_statement(store, now, *, loan_id, as_of, outstanding_lakhs, note=None):
    """A bank-statement balance (a loan observation)."""
    loan = _require_loan(store, loan_id)
    obs_id = store.add_observation(
        "loan", loan["id"], _recorded_date(as_of, now, "as_of"), outstanding_lakhs, "statement", note, now
    )
    store.commit()
    return obs_id


def loan_inputs(store, loan):
    """(loan, terms, loan observations, prepayments) or None if it has no balance yet."""
    observations = store.observations("loan", loan["id"])
    if not observations:
        return None
    prepayments = [
        {"date": e["entry_date"], "amount_lakhs": e["amount_lakhs"]}
        for e in store.entries("prepayment")
        if e["ref_id"] == loan["id"]
    ]
    return loan, store.terms(loan["id"]), observations, prepayments


def _single_loan_view(store, loan, today: date, start_year, end_year):
    inputs = loan_inputs(store, loan)
    if inputs is None:
        return None
    _, terms, observations, prepayments = inputs
    today_month = today.strftime("%Y-%m")
    return {
        "loan": loan,
        "summary": {
            **summarize_schedule(observations, terms, prepayments, today_month),
            "monthly_emi": monthly_emi_in_force(terms, today_month),
        },
        "terms": terms,
        "observations": observations,
        "prepayments": [e for e in store.entries("prepayment") if e["ref_id"] == loan["id"]],
        "schedule": forward_schedule(observations, terms, prepayments),
        "timeline": build_timeline(observations, terms, prepayments, start_year, end_year),
    }


def combined_summary(views):
    """Totals across loans for the strip above them (None with fewer than two)."""
    if len(views) < 2:
        return None
    paid_off = [v["summary"]["payoff_month"] for v in views]
    return {
        "current_balance": round(sum(v["summary"]["current_balance"] for v in views), 4),
        "monthly_emi": round(sum(v["summary"]["monthly_emi"] for v in views), 4),
        "total_interest_remaining": round(sum(v["summary"]["total_interest_remaining"] for v in views), 4),
        "payoff_month": None if any(p is None for p in paid_off) else max(paid_off),
    }


def loan_views(store, today: date, start_year, end_year):
    """Everything the loan section shows: one view per loan plus the totals."""
    views = [
        view for loan in get_loans(store)
        if (view := _single_loan_view(store, loan, today, start_year, end_year)) is not None
    ]
    return {"loans": views, "combined": combined_summary(views)}


def prepayment_preview(store, *, loan_id, entry_date, amount_lakhs):
    inputs = loan_inputs(store, _require_loan(store, loan_id))
    if inputs is None:
        raise JournalError("This loan has no balance yet.")
    _, terms, observations, prepayments = inputs
    return preview_prepayment(
        observations, terms, prepayments, {"date": _iso(entry_date, "entry_date"), "amount_lakhs": amount_lakhs}
    )


# --------------------------------------------------------------------------- #
# Check-ins
#
# A check-in is a dated group of balances, keyed "cash", "equity", "debt" and
# "loan_<id>" (one per loan).
# --------------------------------------------------------------------------- #

def _loan_key(loan_id) -> str:
    return f"loan_{loan_id}"


def _split_balances(cash_lakhs, equity_lakhs, debt_lakhs, loan_balances):
    """{key: value} for every supplied balance; loan_balances is {loan id: value}."""
    values = {"cash": cash_lakhs, "equity": equity_lakhs, "debt": debt_lakhs}
    for loan_id, value in (loan_balances or {}).items():
        values[_loan_key(int(loan_id))] = value
    return values


def _observation_target(store, key):
    """(bucket, ref_id) a balance key stands for; loan keys must name a real loan."""
    if key in ("cash", "equity", "debt"):
        return key, None
    if key.startswith("loan_"):
        loan_id = int(key[5:])
        _require_loan(store, loan_id)
        return "loan", loan_id
    raise JournalError(f"Unknown balance {key!r}.")


def record_check_in(store, now, *, as_of, cash_lakhs=None, equity_lakhs=None, debt_lakhs=None,
                    loan_balances=None, note=None):
    """One dated snapshot: each supplied balance becomes an observation."""
    supplied = {k: v for k, v in _split_balances(cash_lakhs, equity_lakhs, debt_lakhs, loan_balances).items()
                if v is not None}
    if not supplied:
        raise JournalError("A check-in needs at least one balance.")
    as_of = _recorded_date(as_of, now, "as_of")
    targets = {key: _observation_target(store, key) for key in supplied}
    ids = {}
    for key, value in supplied.items():
        bucket, ref_id = targets[key]
        ids[key] = store.add_observation(bucket, ref_id, as_of, value, "check_in", note, now)
    store.commit()
    return ids


def _observation_key(observation) -> str:
    return _loan_key(observation["ref_id"]) if observation["bucket"] == "loan" else observation["bucket"]


def _check_in_balances(store, ids):
    """The live observations a check-in group's {key: id} refers to."""
    live = {o["id"]: o for o in store.observations()}
    found = {}
    for key, observation_id in ids.items():
        observation = live.get(observation_id)
        if observation is None or _observation_key(observation) != key:
            raise JournalError("Unknown balance in this check-in.")
        found[key] = observation
    return found


def _a_loan_would_lose_all_balances(store, removing):
    """True if voiding these observations would leave some loan with none."""
    gone: dict[int, set] = {}
    for observation in removing:
        if observation["bucket"] == "loan":
            gone.setdefault(observation["ref_id"], set()).add(observation["id"])
    return any(
        not [o for o in store.observations("loan", loan_id) if o["id"] not in ids]
        for loan_id, ids in gone.items()
    )


def edit_check_in(store, now, *, ids, as_of, cash_lakhs=None, equity_lakhs=None, debt_lakhs=None,
                  loan_balances=None, note=None):
    """Correct a check-in: `ids` is the {key: observation id} of the group being
    edited. A blank balance removes it from the group, a new one adds it;
    changed balances are replaced (old voided, new added, source kept)."""
    as_of = _recorded_date(as_of, now, "as_of")
    old = _check_in_balances(store, ids)
    values = _split_balances(cash_lakhs, equity_lakhs, debt_lakhs, loan_balances)
    for key in old:
        values.setdefault(key, None)            # a balance not mentioned at all is removed
    if all(value is None for value in values.values()):
        raise JournalError("A check-in needs at least one balance; delete it instead.")
    targets = {key: _observation_target(store, key) for key, value in values.items() if value is not None}
    removed = [old[key] for key, value in values.items() if value is None and key in old]
    if _a_loan_would_lose_all_balances(store, removed):
        raise JournalError("A loan's only balance observation can't be removed.")

    for key, value in values.items():
        previous = old.get(key)
        if value is None:
            if previous:
                store.void_observation(previous["id"], now)
        elif previous is None:
            bucket, ref_id = targets[key]
            store.add_observation(bucket, ref_id, as_of, value, "check_in", note, now)
        elif (previous["amount_lakhs"], previous["as_of"], previous["note"] or None) != (value, as_of, note or None):
            store.add_observation(previous["bucket"], previous["ref_id"], as_of, value, previous["source"], note, now)
            store.void_observation(previous["id"], now)
    store.commit()


def void_check_in(store, now, ids):
    """Delete a check-in (all of its balances). Voided, not erased."""
    if not isinstance(ids, dict):
        raise JournalError("ids must be a {balance: id} mapping.")
    removing = list(_check_in_balances(store, ids).values())
    if not removing:
        raise JournalError("Nothing to delete.")
    if _a_loan_would_lose_all_balances(store, removing):
        raise JournalError("A loan's only balance observation can't be deleted.")
    for observation in removing:
        store.void_observation(observation["id"], now)
    store.commit()


def check_in_history(store):
    """Observations grouped by date (newest first) for the journal list."""
    grouped: dict[str, dict] = {}
    for obs in store.observations():
        entry = grouped.setdefault(obs["as_of"], {"as_of": obs["as_of"], "balances": {}, "ids": {}, "note": obs["note"]})
        key = _observation_key(obs)
        entry["balances"][key] = obs["amount_lakhs"]
        entry["ids"][key] = obs["id"]
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
        "kind": kind, "entry_date": _recorded_date(entry_date, now, "entry_date"), "amount_lakhs": amount_lakhs,
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

def edit_entry(store, now, entry_id, *, entry_date, amount_lakhs, note=None, status=None, bucket=None):
    """Correct a ledger entry of any kind. The old entry is voided and a
    corrected one added (same kind and links). `status` applies to income,
    `bucket` to invest/withdraw; both default to the entry's current value."""
    old = store.entry(entry_id)
    if old is None or old["voided_at"]:
        raise JournalError("Unknown entry.")
    kind = old["kind"]
    new = {"kind": kind, "amount_lakhs": amount_lakhs, "note": note, "created_at": now, "ref_id": old["ref_id"]}
    if kind == "income":
        status = status or old["status"]
        if status not in INCOME_STATUSES:
            raise JournalError(f"status must be one of {INCOME_STATUSES}")
        entry_date = _iso(entry_date, "entry_date")
        new.update(
            entry_date=entry_date, status=status,
            approximate=bool(old["approximate"]) and entry_date == old["entry_date"],
        )
    elif kind == "prepayment":
        new.update(entry_date=_recorded_date(entry_date, now, "entry_date"))
    elif kind in ("invest", "withdraw"):
        bucket = bucket or (old["to_bucket"] if kind == "invest" else old["from_bucket"])
        if bucket not in INVESTABLE:
            raise JournalError(f"bucket must be one of {INVESTABLE}")
        from_bucket, to_bucket = ("cash", bucket) if kind == "invest" else (bucket, "cash")
        new.update(
            entry_date=_recorded_date(entry_date, now, "entry_date"), from_bucket=from_bucket, to_bucket=to_bucket
        )
    else:
        raise JournalError("This kind of entry can't be edited.")
    store.add_entry(new)
    store.void_entry(entry_id, now)
    store.commit()


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


def _legacy_monthly_emi(legacy, year: int) -> float:
    """The old flat EMI assumption as a monthly figure for `year` (0 after its end year)."""
    if not legacy or year > legacy.get("loan_end_year", 0):
        return 0.0
    return legacy.get("annual_emi_lakhs", 0.0) / 12


def _with_legacy_before_loans(timeline, first_loan_month, legacy):
    """Months before the earliest loan entry keep what the assumptions said (the
    flat EMI); from the month a loan is entered the loans' own schedules take
    over and carry on from there."""
    if not timeline:
        return timeline
    return [
        {**row, "emi_paid": _legacy_monthly_emi(legacy, int(row["month"][:4]))}
        if row["month"] < first_loan_month else row
        for row in timeline
    ]


def projection_inputs(store, start_year, end_year, include_actuals: bool, legacy=None) -> dict:
    """Everything run_projection() takes from the journal. With an empty
    journal every value is empty/None, so the projection is the plain forecast.

    The loans are merged into ONE monthly timeline (their EMI, interest,
    principal, prepayments and balances summed), so the projection has the same
    rows however many loans there are. Months before the earliest loan entry
    use the old flat-EMI assumption (`legacy`: annual_emi_lakhs, loan_end_year),
    so entering a loan changes nothing before it.

    include_actuals=False is the PLAN view: extra income (planned or received)
    and each loan as set up (opening balance + terms changes, no prepayments, no
    later statement balances). include_actuals=True adds what really happened:
    prepayments, later loan balances, cash/equity/debt check-ins and
    invest/withdraw transfers.
    """
    timelines = []
    first_loan_month = None
    for loan in get_loans(store):
        inputs = loan_inputs(store, loan)
        if inputs is None:
            continue
        _, terms, observations, prepayments = inputs
        if not include_actuals:
            observations = [o for o in observations if o["source"] == "loan_setup"] or observations[:1]
            prepayments = []
        started = min(o["as_of"][:7] for o in observations)
        first_loan_month = started if first_loan_month is None else min(first_loan_month, started)
        timelines.append(build_timeline(observations, terms, prepayments, start_year, end_year))
    merged = merge_timelines(timelines)
    if merged:
        merged = _with_legacy_before_loans(merged, first_loan_month, legacy)
    return {
        "extra_income_by_year": extra_income_by_year(store),
        "loan_timeline": merged or None,
        "observations": (
            [o for o in store.observations() if o["bucket"] in ("cash", "equity", "debt")]
            if include_actuals else []
        ),
        "transfers": transfers_for_projection(store) if include_actuals else [],
    }
