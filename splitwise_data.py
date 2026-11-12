import hashlib
import re
import sqlite3
from datetime import datetime

DB_PATH = "splitwise_expenses.db"

EXPENSES_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    date TEXT,
    description TEXT,
    cost REAL,
    category TEXT,
    group_id INTEGER,
    updated_at TEXT,
    deleted_at TEXT,
    source TEXT DEFAULT 'api'
);
"""

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def create_schema():
    conn = get_db_connection()
    conn.execute(EXPENSES_SCHEMA)
    # Migrate older DBs created before the 'source' column existed.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(expenses)").fetchall()}
    if "source" not in existing_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN source TEXT DEFAULT 'api'")
    conn.commit()
    conn.close()

def upsert_expense(exp):
    conn = get_db_connection()
    date = exp.getDate()[:10] if exp.getDate() else None
    description = exp.getDescription() or ""
    cost = float(exp.getCost() or 0)
    group_id = exp.getGroupId()
    conn.execute(
        """
        INSERT INTO expenses (id, date, description, cost, category, group_id, updated_at, deleted_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'api')
        ON CONFLICT(id) DO UPDATE SET
            date=excluded.date,
            description=excluded.description,
            cost=excluded.cost,
            category=excluded.category,
            group_id=excluded.group_id,
            updated_at=excluded.updated_at,
            deleted_at=excluded.deleted_at,
            source='api'
        """,
        (
            exp.getId(),
            date,
            description,
            cost,
            exp.getCategory().getName() if exp.getCategory() else None,
            group_id,
            exp.getUpdatedAt() or exp.getDate() or datetime.now().isoformat(),
            exp.getDeletedAt() if hasattr(exp, 'getDeletedAt') else None
        )
    )
    # This real, API-synced expense supersedes any manually-imported
    # placeholder for the same real-world expense (see pdf_import.py) -
    # remove it now so the two don't double-count.
    conn.execute(
        """
        DELETE FROM expenses
        WHERE source = 'manual_pdf'
          AND group_id = ?
          AND date = ?
          AND TRIM(description) = ?
          AND ROUND(cost, 2) = ROUND(?, 2)
        """,
        (group_id, date, normalize_description(description), cost)
    )
    conn.commit()
    conn.close()

def get_latest_expense_update(group_id):
    # Only consider source='api' rows: manual_pdf rows carry import-time
    # timestamps (not real Splitwise server timestamps), and would otherwise
    # push this cursor artificially forward, causing a resumed live sync to
    # skip genuine Splitwise updates that happened while the API was down.
    conn = get_db_connection()
    cur = conn.execute(
        "SELECT MAX(updated_at) as latest FROM expenses WHERE group_id = ? AND source = 'api'",
        (group_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row["latest"] if row and row["latest"] else None

def normalize_description(description):
    """Collapse all whitespace and strip ends, for content-based matching.

    Splitwise descriptions occasionally carry stray leading/trailing or
    doubled-up whitespace (e.g. "Cigarettes "), which a PDF-rendered copy of
    the same expense won't reproduce. Comparing on this normalized form lets
    manual PDF imports reliably recognize an expense that's already been
    synced via the API.
    """
    return re.sub(r"\s+", " ", (description or "").strip())

def make_manual_id(group_id, date, description, cost):
    """Deterministic placeholder id for a manually-imported (non-API) expense.

    Derived from (group_id, date, description, cost) so re-importing the same
    PDF is idempotent. Always negative so it can never collide with a real
    (positive) Splitwise expense id.
    """
    key = f"{group_id}|{date}|{normalize_description(description)}|{float(cost):.2f}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return -(int(digest[:15], 16) % (2**62)) 