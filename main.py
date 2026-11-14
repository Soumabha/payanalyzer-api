import os
from typing import Optional
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from splitwise import Splitwise
from splitwise.exception import SplitwiseBaseException
from requests.exceptions import RequestException
from dotenv import load_dotenv
from splitwise_data import create_schema, upsert_expense, get_latest_expense_update, get_db_connection, SqliteExpenseStore
from actual_spend import load_actual_monthly_spend
from projections import run_projection, PROJECTION_START_YEAR, PROJECTION_END_YEAR
from projection_data import create_projection_schema, get_assumptions, save_assumptions
import journal
from journal_data import SqliteJournalStore, create_journal_schema
from datetime import datetime, timedelta
from collections import defaultdict
import calendar
import numpy as np
from dateutil.relativedelta import relativedelta
import logging
from nltk.stem import PorterStemmer
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("splitapi")

app = FastAPI()

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Load environment variables
load_dotenv()

CONSUMER_KEY = os.getenv("SPLITWISE_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("SPLITWISE_CONSUMER_SECRET")
API_KEY = os.getenv("API_KEY")
GROUP_NAME = os.getenv("SPLITWISE_GROUP_NAME")
if not GROUP_NAME:
    raise Exception("SPLITWISE_GROUP_NAME environment variable must be set.")
GROUP_ID = None  # Will be set at startup

class AssumptionsUpdate(BaseModel):
    current_annual_expense_lakhs: Optional[float] = None
    inflation_pct: Optional[float] = None
    education_inflation_pct: Optional[float] = None
    healthcare_inflation_pct: Optional[float] = None
    kids_expense_start_year: Optional[int] = None
    kids_school_start_age: Optional[int] = None
    kids_expense_end_year: Optional[int] = None
    parents_expense_start_year: Optional[int] = None
    parents_expense_end_year: Optional[int] = None
    loan_outstanding_lakhs: Optional[float] = None
    annual_emi_lakhs: Optional[float] = None
    loan_end_year: Optional[int] = None
    rental_income_lakhs: Optional[float] = None
    rent_increase_pct: Optional[float] = None
    current_annual_salary_lakhs: Optional[float] = None
    salary_growth_pct: Optional[float] = None
    retirement_year: Optional[int] = None
    starting_cash_lakhs: Optional[float] = None
    equity_portfolio_lakhs: Optional[float] = None
    equity_return_pct: Optional[float] = None
    debt_portfolio_lakhs: Optional[float] = None
    debt_return_pct: Optional[float] = None


class ExtraIncomeEntry(BaseModel):
    year: int
    amount_lakhs: float
    note: Optional[str] = None


class LoanSetup(BaseModel):
    name: str = "Home loan"
    outstanding_lakhs: float
    as_of: str
    annual_rate_pct: float
    emi_lakhs: Optional[float] = None
    remaining_months: Optional[int] = None


class LoanTermsChange(BaseModel):
    effective_from: str
    annual_rate_pct: float
    emi_lakhs: float
    note: Optional[str] = None
    statement_balance_lakhs: Optional[float] = None


class LoanTermsEdit(BaseModel):
    effective_from: str
    annual_rate_pct: float
    emi_lakhs: float
    note: Optional[str] = None


class PrepaymentIn(BaseModel):
    entry_date: str
    amount_lakhs: float
    note: Optional[str] = None


class LoanStatement(BaseModel):
    as_of: str
    outstanding_lakhs: float
    note: Optional[str] = None


class CheckIn(BaseModel):
    as_of: str
    cash_lakhs: Optional[float] = None
    equity_lakhs: Optional[float] = None
    debt_lakhs: Optional[float] = None
    loan_outstanding_lakhs: Optional[float] = None
    note: Optional[str] = None


class CheckInEdit(CheckIn):
    ids: dict[str, int]


class CheckInVoid(BaseModel):
    ids: dict[str, int]


class EntryEdit(BaseModel):
    entry_date: str
    amount_lakhs: float
    note: Optional[str] = None
    status: Optional[str] = None
    bucket: Optional[str] = None


class IncomeIn(BaseModel):
    entry_date: str
    amount_lakhs: float
    note: Optional[str] = None
    status: str = "planned"


class TransferIn(BaseModel):
    kind: str
    entry_date: str
    amount_lakhs: float
    bucket: str
    note: Optional[str] = None


class StatusIn(BaseModel):
    status: str


@app.on_event("startup")
def startup_event():
    create_schema()
    create_projection_schema()
    create_journal_schema()
    store = SqliteJournalStore()
    try:
        journal.migrate_legacy_extra_income(store, _now())
    finally:
        store.close()
    sync_splitwise_expenses()


def sync_splitwise_expenses():
    """Best-effort sync of Splitwise expenses into the local SQLite DB.

    Splitwise access is NOT a hard requirement to run this app: all read
    endpoints operate purely off the local SQLite DB, which can also be kept
    up to date via manual imports (see pdf_import.py) when the Splitwise API
    isn't reachable (e.g. no active Splitwise Pro subscription on the
    registered app, no network, revoked token, etc). On any failure here we
    log the reason and let the app continue starting up.
    """
    global GROUP_ID
    if not (CONSUMER_KEY and CONSUMER_SECRET and API_KEY):
        logger.warning(
            "Splitwise initial sync not performed. Reason: one or more of "
            "SPLITWISE_CONSUMER_KEY / SPLITWISE_CONSUMER_SECRET / API_KEY is not set."
        )
        return
    try:
        sObj = Splitwise(CONSUMER_KEY, CONSUMER_SECRET, api_key=API_KEY)
        # Find group ID by name
        groups = sObj.getGroups()
        for group in groups:
            if group.getName() == GROUP_NAME:
                GROUP_ID = group.getId()
                break
        if GROUP_ID is None:
            logger.warning(
                f"Splitwise initial sync not performed. Reason: group '{GROUP_NAME}' "
                "not found for the current user."
            )
            return
        latest_update = get_latest_expense_update(int(GROUP_ID))
        offset = 0
        limit = 50
        synced = 0
        while True:
            batch = sObj.getExpenses(offset=offset, limit=limit, group_id=int(GROUP_ID), updated_after=latest_update)
            if not batch:
                break
            for exp in batch:
                upsert_expense(exp)
            synced += len(batch)
            if len(batch) < limit:
                break
            offset += limit
        logger.info(
            f"Splitwise expenses synced to SQLite for group '{GROUP_NAME}' (ID: {GROUP_ID}). "
            f"{synced} expense(s) updated."
        )
    except SplitwiseBaseException as e:
        reason = "; ".join(e.errors.get("base", [str(e)])) if getattr(e, "errors", None) else str(e)
        logger.warning(f"Splitwise initial sync not performed. Reason: {reason}")
    except RequestException as e:
        logger.warning(f"Splitwise initial sync not performed. Reason: network error - {e}")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/api/stats")
def get_stats():
    conn = get_db_connection()
    cur = conn.execute("SELECT COUNT(*) as count, SUM(cost) as total FROM expenses WHERE deleted_at IS NULL")
    row = cur.fetchone()
    cur2 = conn.execute("SELECT MIN(date) as min_date, MAX(date) as max_date FROM expenses WHERE deleted_at IS NULL")
    row2 = cur2.fetchone()
    conn.close()
    total_spent = row["total"] or 0
    min_date = row2["min_date"]
    max_date = row2["max_date"]
    # Calculate number of months between min_date and max_date (inclusive)
    if min_date and max_date:
        d1 = datetime.strptime(min_date, "%Y-%m-%d")
        d2 = datetime.strptime(max_date, "%Y-%m-%d")
        num_months = (d2.year - d1.year) * 12 + (d2.month - d1.month) + 1
    else:
        num_months = 1
    avg_monthly = total_spent / num_months if num_months > 0 else 0
    return {
        "total_expenses": row["count"],
        "total_spent": total_spent,
        "average_expense": avg_monthly,
        "date_range": {"start": min_date, "end": max_date}
    }

@app.get("/api/monthly")
def get_monthly(start: str = Query(None), end: str = Query(None), category: str = Query(None)):
    logging.basicConfig(level=logging.INFO)
    conn = get_db_connection()
    params = []
    where_clauses = ["deleted_at IS NULL"]
    if start:
        where_clauses.append("strftime('%Y-%m', date) >= ?")
        params.append(start)
    if end:
        where_clauses.append("strftime('%Y-%m', date) <= ?")
        params.append(end)
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"SELECT strftime('%Y-%m', date) as month, SUM(cost) as total FROM expenses {where_sql} GROUP BY month ORDER BY month ASC"
    cur = conn.execute(sql, params)
    data = [{"month": row["month"], "total": row["total"]} for row in cur.fetchall()]
    conn.close()
    return data

@app.get("/api/predict")
def get_prediction():
    # Use linear regression on monthly totals to predict next 6 months
    conn = get_db_connection()
    cur = conn.execute("SELECT strftime('%Y-%m', date) as month, SUM(cost) as total FROM expenses WHERE deleted_at IS NULL GROUP BY month ORDER BY month ASC")
    rows = cur.fetchall()
    if not rows or len(rows) < 2:
        return {"prediction": []}
    y = np.array([row["total"] for row in rows])
    x = np.arange(len(y))
    # Linear regression: y = m*x + c
    m, c = np.polyfit(x, y, 1)
    # Predict next 6 months
    from datetime import datetime
    last_month = datetime.strptime(rows[-1]["month"], "%Y-%m")
    prediction = []
    for i in range(1, 7):
        next_month = last_month.replace(day=1) + relativedelta(months=+i)
        month_str = next_month.strftime("%Y-%m")
        pred_value = float(m * (len(y) + i - 1) + c)
        prediction.append({"month": month_str, "predicted": round(max(pred_value, 0), 2)})
    conn.close()
    return {"prediction": prediction}

@app.get("/api/category-totals")
def get_category_totals(start: str = Query(None), end: str = Query(None), category: str = Query(None)):
    conn = get_db_connection()
    params = []
    where_clauses = ["deleted_at IS NULL"]
    if start and end:
        where_clauses.append("strftime('%Y-%m', date) >= ?")
        params.append(start)
        where_clauses.append("strftime('%Y-%m', date) <= ?")
        params.append(end)
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    where_sql = " AND ".join(where_clauses)
    sql = f"SELECT category, SUM(cost) as total FROM expenses WHERE {where_sql} GROUP BY category ORDER BY total DESC"
    cur = conn.execute(sql, params)
    data = [{"category": row["category"], "total": row["total"]} for row in cur.fetchall()]
    conn.close()
    return data

@app.get("/api/monthly-trend")
def get_monthly_trend():
    conn = get_db_connection()
    cur = conn.execute("SELECT strftime('%Y-%m', date) as month, SUM(cost) as total FROM expenses WHERE deleted_at IS NULL GROUP BY month ORDER BY month ASC")
    data = [{"month": row["month"], "total": row["total"]} for row in cur.fetchall()]
    conn.close()
    return data

@app.get("/api/top-expenses/{start}/{end}")
def get_top_expenses(start: str, end: str, limit: int = Query(10, ge=1, le=100), category: str = Query(None)):
    conn = get_db_connection()
    params = [start, end]
    where_clauses = ["strftime('%Y-%m', date) >= ?", "strftime('%Y-%m', date) <= ?", "deleted_at IS NULL"]
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    where_sql = " AND ".join(where_clauses)
    sql = f"""
        SELECT * FROM expenses
        WHERE {where_sql}
        ORDER BY cost DESC, date DESC LIMIT ?
    """
    params.append(limit)
    expenses = [dict(row) for row in conn.execute(sql, params).fetchall()]
    conn.close()
    return expenses

@app.get("/api/categories")
def get_categories():
    conn = get_db_connection()
    cur = conn.execute("SELECT DISTINCT category FROM expenses WHERE deleted_at IS NULL ORDER BY category ASC")
    categories = [row["category"] for row in cur.fetchall() if row["category"]]
    conn.close()
    return {"categories": categories}

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_journal_store():
    store = SqliteJournalStore()
    try:
        yield store
    finally:
        store.close()


@app.exception_handler(journal.JournalError)
def journal_error_handler(request: Request, exc: journal.JournalError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/projection")
def projection_page():
    return FileResponse("static/projection.html")


@app.get("/journal")
def journal_page():
    return FileResponse("static/journal.html")


@app.get("/api/fire-projection/assumptions")
def get_fire_assumptions(store=Depends(get_journal_store)):
    return {"assumptions": get_assumptions(), "extra_income": journal.extra_income_by_year_list(store)}


@app.put("/api/fire-projection/assumptions")
def update_fire_assumptions(payload: AssumptionsUpdate):
    partial_data = {k: v for k, v in payload.dict().items() if v is not None}
    try:
        updated = save_assumptions(partial_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"assumptions": updated}


# Pre-journal extra-income endpoints, kept as thin wrappers over the ledger.
@app.get("/api/fire-projection/extra-income")
def list_extra_income(store=Depends(get_journal_store)):
    return journal.extra_income_by_year_list(store)


@app.post("/api/fire-projection/extra-income")
def create_extra_income(entry: ExtraIncomeEntry, store=Depends(get_journal_store)):
    journal.set_extra_income_for_year(store, _now(), entry.year, entry.amount_lakhs, entry.note)
    return journal.extra_income_by_year_list(store)


@app.delete("/api/fire-projection/extra-income/{year}")
def remove_extra_income(year: int, store=Depends(get_journal_store)):
    journal.remove_extra_income_for_year(store, _now(), year)
    return journal.extra_income_by_year_list(store)


@app.get("/api/fire-projection")
def get_fire_projection(
    start_year: int = Query(PROJECTION_START_YEAR),
    end_year: int = Query(PROJECTION_END_YEAR),
    actuals: bool = Query(False),
    journal_store=Depends(get_journal_store),
):
    assumptions = get_assumptions()
    inputs = journal.projection_inputs(journal_store, start_year, end_year, include_actuals=actuals)
    actual_monthly_spend = None
    if actuals:
        store = SqliteExpenseStore()
        try:
            actual_monthly_spend = load_actual_monthly_spend(store, start_year)
        finally:
            store.close()
    try:
        return run_projection(
            assumptions, inputs["extra_income_by_year"], start_year=start_year, end_year=end_year,
            actual_monthly_spend=actual_monthly_spend,
            loan_timeline=inputs["loan_timeline"],
            observations=inputs["observations"],
            transfers=inputs["transfers"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Journal: loan, check-ins, income, transfers ---------------------------

@app.get("/api/loan")
def get_loan_view(store=Depends(get_journal_store)):
    view = journal.loan_view(store, datetime.now().date(), PROJECTION_START_YEAR, PROJECTION_END_YEAR)
    view["legacy"] = {
        key: value for key, value in get_assumptions().items()
        if key in ("loan_outstanding_lakhs", "annual_emi_lakhs", "loan_end_year")
    }
    return view


@app.post("/api/loan")
def create_loan(payload: LoanSetup, store=Depends(get_journal_store)):
    journal.setup_loan(store, _now(), **payload.dict())
    return get_loan_view(store)


@app.post("/api/loan/terms")
def create_loan_terms(payload: LoanTermsChange, store=Depends(get_journal_store)):
    journal.add_loan_terms(store, _now(), **payload.dict())
    return get_loan_view(store)


@app.put("/api/loan/terms/{terms_id}")
def update_loan_terms(terms_id: int, payload: LoanTermsEdit, store=Depends(get_journal_store)):
    journal.edit_loan_terms(store, _now(), terms_id, **payload.dict())
    return get_loan_view(store)


@app.post("/api/loan/terms/{terms_id}/void")
def void_loan_terms(terms_id: int, store=Depends(get_journal_store)):
    journal.void_loan_terms(store, _now(), terms_id)
    return get_loan_view(store)


@app.post("/api/loan/prepayments")
def create_prepayment(payload: PrepaymentIn, store=Depends(get_journal_store)):
    journal.add_prepayment(store, _now(), **payload.dict())
    return get_loan_view(store)


@app.post("/api/loan/prepayment-preview")
def preview_prepayment(payload: PrepaymentIn, store=Depends(get_journal_store)):
    return journal.prepayment_preview(store, entry_date=payload.entry_date, amount_lakhs=payload.amount_lakhs)


@app.post("/api/loan/statement")
def create_loan_statement(payload: LoanStatement, store=Depends(get_journal_store)):
    journal.add_loan_statement(store, _now(), **payload.dict())
    return get_loan_view(store)


@app.get("/api/journal")
def get_journal(store=Depends(get_journal_store)):
    return {
        "entries": journal.journal_entries(store, datetime.now().date()),
        "check_ins": journal.check_in_history(store),
    }


@app.post("/api/journal/check-in")
def create_check_in(payload: CheckIn, store=Depends(get_journal_store)):
    journal.record_check_in(store, _now(), **payload.dict())
    return get_journal(store)


@app.post("/api/journal/income")
def create_income(payload: IncomeIn, store=Depends(get_journal_store)):
    journal.add_income(store, _now(), **payload.dict())
    return get_journal(store)


@app.post("/api/journal/transfer")
def create_transfer(payload: TransferIn, store=Depends(get_journal_store)):
    journal.add_transfer(store, _now(), **payload.dict())
    return get_journal(store)


@app.post("/api/journal/entries/{entry_id}/status")
def set_income_status(entry_id: int, payload: StatusIn, store=Depends(get_journal_store)):
    journal.confirm_income(store, entry_id, payload.status)
    return get_journal(store)


@app.post("/api/journal/entries/{entry_id}/void")
def void_journal_entry(entry_id: int, store=Depends(get_journal_store)):
    journal.void_entry(store, _now(), entry_id)
    return get_journal(store)


@app.put("/api/journal/check-in")
def update_check_in(payload: CheckInEdit, store=Depends(get_journal_store)):
    journal.edit_check_in(store, _now(), **payload.dict())
    return get_journal(store)


@app.post("/api/journal/check-in/void")
def void_check_in(payload: CheckInVoid, store=Depends(get_journal_store)):
    journal.void_check_in(store, _now(), payload.ids)
    return get_journal(store)


@app.put("/api/journal/entries/{entry_id}")
def update_journal_entry(entry_id: int, payload: EntryEdit, store=Depends(get_journal_store)):
    journal.edit_entry(store, _now(), entry_id, **payload.dict())
    return get_journal(store)


@app.get("/api/top-recurring-expenses")
def get_top_recurring_expenses(
    start: str = Query(None),
    end: str = Query(None),
    category: str = Query(None),
    limit: int = Query(3, ge=1, le=50)
):
    # Use stemming to merge similar descriptions
    conn = get_db_connection()
    params = []
    where_clauses = ["deleted_at IS NULL"]
    if start:
        where_clauses.append("strftime('%Y-%m', date) >= ?")
        params.append(start)
    if end:
        where_clauses.append("strftime('%Y-%m', date) <= ?")
        params.append(end)
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"""
        SELECT description, strftime('%Y-%m', date) as month, cost
        FROM expenses
        {where_sql}
    """
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    # Stem and group descriptions
    ps = PorterStemmer()
    def stem_text(text):
        # Lowercase, remove non-alphabetic, stem each word
        words = re.findall(r'[a-zA-Z]+', (text or '').lower())
        return ' '.join(ps.stem(w) for w in words)
    grouped = {}
    for row in rows:
        stemmed = stem_text(row["description"])
        if not stemmed:
            continue
        key = stemmed
        if key not in grouped:
            grouped[key] = {
                "originals": set(),
                "months": set(),
                "count": 0,
                "total": 0.0,
                "description": row["description"]
            }
        grouped[key]["originals"].add(row["description"])
        grouped[key]["months"].add(row["month"])
        grouped[key]["count"] += 1
        grouped[key]["total"] += float(row["cost"])
    # Only keep those that appear in more than 1 month
    merged = [
        {
            "description": ', '.join(sorted(list(g["originals"])))[:100],
            "months": len(g["months"]),
            "count": g["count"],
            "total": g["total"],
            "avg_per_occurrence": g["total"] / g["count"] if g["count"] else 0
        }
        for g in grouped.values() if len(g["months"]) > 1
    ]
    merged.sort(key=lambda x: x["total"], reverse=True)
    return merged[:limit]
