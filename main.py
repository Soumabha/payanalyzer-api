import os
from fastapi import FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from splitwise import Splitwise
from dotenv import load_dotenv
from splitwise_data import create_schema, upsert_expense, get_latest_expense_update, get_db_connection
from datetime import datetime, timedelta
from collections import defaultdict
import calendar
import numpy as np
from dateutil.relativedelta import relativedelta
import logging
from nltk.stem import PorterStemmer
import re

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

@app.on_event("startup")
def startup_event():
    global GROUP_ID
    create_schema()
    sObj = Splitwise(CONSUMER_KEY, CONSUMER_SECRET, api_key=API_KEY)
    # Find group ID by name
    groups = sObj.getGroups()
    for group in groups:
        if group.getName() == GROUP_NAME:
            GROUP_ID = group.getId()
            break
    if GROUP_ID is None:
        raise Exception(f"Group '{GROUP_NAME}' not found for the current user.")
    latest_update = get_latest_expense_update(int(GROUP_ID))
    offset = 0
    limit = 50
    while True:
        batch = sObj.getExpenses(offset=offset, limit=limit, group_id=int(GROUP_ID), updated_after=latest_update)
        if not batch:
            break
        for exp in batch:
            upsert_expense(exp)
        if len(batch) < limit:
            break
        offset += limit
    print(f"Splitwise expenses synced to SQLite for group '{GROUP_NAME}' (ID: {GROUP_ID}).")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/api/stats")
def get_stats():
    conn = get_db_connection()
    cur = conn.execute("SELECT COUNT(*) as count, SUM(cost) as total FROM expenses")
    row = cur.fetchone()
    cur2 = conn.execute("SELECT MIN(date) as min_date, MAX(date) as max_date FROM expenses")
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
    return data[::-1]  # Return in chronological order

@app.get("/api/predict")
def get_prediction():
    # Use linear regression on monthly totals to predict next 6 months
    conn = get_db_connection()
    cur = conn.execute("SELECT strftime('%Y-%m', date) as month, SUM(cost) as total FROM expenses GROUP BY month ORDER BY month ASC")
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
    cur = conn.execute("SELECT strftime('%Y-%m', date) as month, SUM(cost) as total FROM expenses GROUP BY month ORDER BY month ASC")
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