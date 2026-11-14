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


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O): everything the import/sync logic *decides* lives in
# functions like these, so it can be unit-tested without a database.
# --------------------------------------------------------------------------- #

def normalize_description(description):
    """Collapse all whitespace and strip ends, for content-based matching.

    Splitwise descriptions occasionally carry stray leading/trailing or
    doubled-up whitespace (e.g. "Cigarettes "), which a PDF-rendered copy of
    the same expense won't reproduce. Comparing on this normalized form lets
    manual PDF imports reliably recognize an expense that's already been
    synced via the API.
    """
    return re.sub(r"\s+", " ", (description or "").strip())

def description_key(description):
    """normalize_description(), lower-cased: the case-insensitive key used
    wherever descriptions are compared for *similarity* (category inference).
    Identity matching - is this PDF row the same expense as an API row? -
    deliberately stays case-sensitive and uses normalize_description() alone.
    """
    return normalize_description(description).lower()

def make_manual_id(group_id, date, description, cost):
    """Deterministic placeholder id for a manually-imported (non-API) expense.

    Derived from (group_id, date, description, cost) so re-importing the same
    PDF is idempotent. Always negative so it can never collide with a real
    (positive) Splitwise expense id.
    """
    key = f"{group_id}|{date}|{normalize_description(description)}|{float(cost):.2f}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return -(int(digest[:15], 16) % (2**62))

def expense_to_row(exp):
    """Map a Splitwise Expense object to the plain row dict we store."""
    date = exp.getDate()[:10] if exp.getDate() else None
    return {
        "id": exp.getId(),
        "date": date,
        "description": exp.getDescription() or "",
        "cost": float(exp.getCost() or 0),
        "category": exp.getCategory().getName() if exp.getCategory() else None,
        "group_id": exp.getGroupId(),
        "updated_at": exp.getUpdatedAt() or exp.getDate() or datetime.now().isoformat(),
        "deleted_at": exp.getDeletedAt() if hasattr(exp, "getDeletedAt") else None,
    }

def superseded_manual_ids(api_row, manual_rows):
    """Ids of the manual_pdf rows that are the same real-world expense as
    `api_row`: same group, same date, same whitespace-normalized description
    (case-sensitive) and same cost to 2 decimal places."""
    description = normalize_description(api_row["description"])
    cost = round(api_row["cost"], 2)
    return [
        m["id"]
        for m in manual_rows
        if m["group_id"] == api_row["group_id"]
        and m["date"] == api_row["date"]
        and normalize_description(m["description"]) == description
        and round(m["cost"], 2) == cost
    ]


# --------------------------------------------------------------------------- #
# Persistence boundary
# --------------------------------------------------------------------------- #

_ID_CHUNK = 500  # stay well under SQLite's bound-variable limit on older builds


class SqliteExpenseStore:
    """The only place the import/sync logic touches SQL.

    Deliberately thin: each method is one plain query/statement that decides
    *which rows to fetch or write* and nothing else - no normalization, no
    matching, no business rules. Those live in the pure functions above and in
    pdf_import / category_inference, which only ever see plain dicts. Tests
    substitute an in-memory fake with the same methods.
    """

    def __init__(self, conn=None):
        self._conn = conn if conn is not None else get_db_connection()

    def close(self):
        self._conn.close()

    def commit(self):
        self._conn.commit()

    # -- reads ------------------------------------------------------------- #

    def api_rows(self, group_id, year_month):
        """Live (non-deleted) API rows for a group in a 'YYYY-MM' month."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT date, description, cost FROM expenses "
                "WHERE source='api' AND group_id=? AND strftime('%Y-%m', date)=? AND deleted_at IS NULL",
                (group_id, year_month),
            )
        ]

    def manual_rows(self, group_id, year_month):
        """All manual_pdf rows for a group in a 'YYYY-MM' month."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, date, description, cost, category, group_id FROM expenses "
                "WHERE source='manual_pdf' AND group_id=? AND strftime('%Y-%m', date)=?",
                (group_id, year_month),
            )
        ]

    def categorized_history(self):
        """Every live row that has a category, from any source and group: the
        evidence category inference learns from."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, date, description, category FROM expenses "
                "WHERE category IS NOT NULL AND deleted_at IS NULL"
            )
        ]

    def rows_for_evaluation(self, start_date, end_date, ids=None):
        """Categorized live rows to backtest, by id list or by date range."""
        if ids:
            return [
                dict(r)
                for r in self._conn.execute(
                    f"SELECT id, date, description, cost, category FROM expenses "
                    f"WHERE id IN ({','.join('?' * len(ids))}) AND deleted_at IS NULL AND category IS NOT NULL "
                    f"ORDER BY date",
                    list(ids),
                )
            ]
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, date, description, cost, category FROM expenses "
                "WHERE date >= ? AND date <= ? AND deleted_at IS NULL AND category IS NOT NULL ORDER BY date",
                (start_date, end_date),
            )
        ]

    def monthly_totals(self, since_date):
        """Total cost per 'YYYY-MM' month, in the stored unit (rupees), over live
        rows of every source and group dated on or after since_date."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT strftime('%Y-%m', date) AS month, SUM(cost) AS total FROM expenses "
                "WHERE deleted_at IS NULL AND date IS NOT NULL AND date >= ? "
                "GROUP BY month ORDER BY month",
                (since_date,),
            )
        ]

    def latest_api_update(self, group_id):
        """Newest updated_at among a group's API rows (None if there are none).

        Only source='api' rows count: manual_pdf rows carry import-time
        timestamps (not real Splitwise server timestamps), and would otherwise
        push the sync cursor artificially forward, causing a resumed live sync
        to skip genuine Splitwise updates made while the API was down.
        """
        row = self._conn.execute(
            "SELECT MAX(updated_at) AS latest FROM expenses WHERE group_id = ? AND source = 'api'",
            (group_id,),
        ).fetchone()
        return row["latest"] if row and row["latest"] else None

    # -- writes ------------------------------------------------------------ #

    def upsert_api_row(self, row):
        self._conn.execute(
            """
            INSERT INTO expenses (id, date, description, cost, category, group_id, updated_at, deleted_at, source)
            VALUES (:id, :date, :description, :cost, :category, :group_id, :updated_at, :deleted_at, 'api')
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
            row,
        )

    def upsert_manual_rows(self, rows):
        for row in rows:
            self._conn.execute(
                """
                INSERT INTO expenses (id, date, description, cost, category, group_id, updated_at, deleted_at, source)
                VALUES (:id, :date, :description, :cost, :category, :group_id, :updated_at, NULL, 'manual_pdf')
                ON CONFLICT(id) DO UPDATE SET
                    date=excluded.date,
                    description=excluded.description,
                    cost=excluded.cost,
                    category=excluded.category,
                    group_id=excluded.group_id,
                    updated_at=excluded.updated_at,
                    source='manual_pdf'
                """,
                row,
            )

    def delete_ids(self, ids):
        ids = list(ids)
        for i in range(0, len(ids), _ID_CHUNK):
            chunk = ids[i : i + _ID_CHUNK]
            self._conn.execute(f"DELETE FROM expenses WHERE id IN ({','.join('?' * len(chunk))})", chunk)


# --------------------------------------------------------------------------- #
# Sync entry points (signatures unchanged; `store` is an injection point)
# --------------------------------------------------------------------------- #

def upsert_expense(exp, store=None):
    row = expense_to_row(exp)
    owns_store = store is None
    store = store if store is not None else SqliteExpenseStore()
    try:
        store.upsert_api_row(row)
        # This real, API-synced expense supersedes any manually-imported
        # placeholder for the same real-world expense (see pdf_import.py) -
        # remove it now so the two don't double-count.
        if row["date"] and row["group_id"] is not None:
            manual = store.manual_rows(row["group_id"], row["date"][:7])
            store.delete_ids(superseded_manual_ids(row, manual))
        store.commit()
    finally:
        if owns_store:
            store.close()

def get_latest_expense_update(group_id, store=None):
    owns_store = store is None
    store = store if store is not None else SqliteExpenseStore()
    try:
        return store.latest_api_update(group_id)
    finally:
        if owns_store:
            store.close()
