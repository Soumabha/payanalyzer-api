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
    deleted_at TEXT
);
"""

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def create_schema():
    conn = get_db_connection()
    conn.execute(EXPENSES_SCHEMA)
    conn.commit()
    conn.close()

def upsert_expense(exp):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO expenses (id, date, description, cost, category, group_id, updated_at, deleted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            date=excluded.date,
            description=excluded.description,
            cost=excluded.cost,
            category=excluded.category,
            group_id=excluded.group_id,
            updated_at=excluded.updated_at,
            deleted_at=excluded.deleted_at
        """,
        (
            exp.getId(),
            exp.getDate()[:10] if exp.getDate() else None,
            exp.getDescription() or "",
            float(exp.getCost() or 0),
            exp.getCategory().getName() if exp.getCategory() else None,
            exp.getGroupId(),
            exp.getUpdatedAt() or exp.getDate() or datetime.now().isoformat(),
            exp.getDeletedAt() if hasattr(exp, 'getDeletedAt') else None
        )
    )
    conn.commit()
    conn.close()

def get_latest_expense_update(group_id):
    conn = get_db_connection()
    cur = conn.execute(
        "SELECT MAX(updated_at) as latest FROM expenses WHERE group_id = ?",
        (group_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row["latest"] if row and row["latest"] else None 