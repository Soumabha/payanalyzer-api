"""
Persistence layer for the FIRE / wealth projection's assumptions. Follows the
same sqlite3 + row_factory pattern as splitwise_data.py. Extra income now lives
in the journal ledger (see journal.py); the old projection_extra_income table is
left untouched in existing databases and only read once, to migrate its rows.
"""

from typing import Any

from splitwise_data import get_db_connection
from projections import ASSUMPTION_FIELDS, DEFAULT_ASSUMPTIONS, PROJECTION_START_YEAR

_ASSUMPTION_COLUMNS_SQL = ",\n    ".join(
    f"{field} REAL" if not field.endswith("_year") else f"{field} INTEGER"
    for field in ASSUMPTION_FIELDS
)

PROJECTION_ASSUMPTIONS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS projection_assumptions (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    {_ASSUMPTION_COLUMNS_SQL},
    updated_at TEXT
);
"""


def _ensure_assumption_columns(conn) -> None:
    """
    Adds any ASSUMPTION_FIELDS columns that are missing from an
    already-existing projection_assumptions table (e.g. the table was
    created before a new assumption field, such as an expense stop year,
    was introduced). CREATE TABLE IF NOT EXISTS alone won't add columns to
    a table that already exists with an older shape.
    """
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(projection_assumptions)").fetchall()
    }
    for field in ASSUMPTION_FIELDS:
        if field not in existing_columns:
            col_type = "INTEGER" if field.endswith("_year") else "REAL"
            conn.execute(f"ALTER TABLE projection_assumptions ADD COLUMN {field} {col_type}")


def create_projection_schema():
    conn = get_db_connection()
    conn.execute(PROJECTION_ASSUMPTIONS_SCHEMA)
    _ensure_assumption_columns(conn)
    conn.commit()
    conn.close()


def get_assumptions() -> dict[str, Any]:
    """Returns the stored assumptions row, or the defaults if none saved yet."""
    conn = get_db_connection()
    cur = conn.execute("SELECT * FROM projection_assumptions WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if row is None:
        return dict(DEFAULT_ASSUMPTIONS)
    data = dict(row)
    data.pop("id", None)
    data.pop("updated_at", None)
    # A row saved before a new assumption field existed will have that
    # column as NULL after the migration in _ensure_assumption_columns;
    # fall back to the default for anything missing so older saved rows
    # keep working without the user having to re-enter every field.
    for field in ASSUMPTION_FIELDS:
        if data.get(field) is None:
            data[field] = DEFAULT_ASSUMPTIONS[field]
    return data


def save_assumptions(partial_data: dict[str, Any]) -> dict[str, Any]:
    """
    Merges partial_data onto the current stored assumptions (or defaults if
    none exist yet), validates all fields are present, and upserts the
    single-row table. Returns the full, saved assumptions dict.
    """
    current = get_assumptions()
    current.update({k: v for k, v in partial_data.items() if k in ASSUMPTION_FIELDS})

    missing = [f for f in ASSUMPTION_FIELDS if f not in current or current[f] is None]
    if missing:
        raise ValueError(f"Missing assumption fields: {missing}")

    if current["retirement_year"] <= PROJECTION_START_YEAR:
        raise ValueError(
            f"retirement_year ({current['retirement_year']}) must be after "
            f"the projection start year ({PROJECTION_START_YEAR})"
        )
    if current["kids_school_start_age"] <= 0:
        raise ValueError(
            "kids_school_start_age must be greater than 0 - Kids Education "
            "expenses must start after kids_expense_start_year, not at or before it"
        )

    columns_sql = ", ".join(ASSUMPTION_FIELDS)
    placeholders_sql = ", ".join("?" for _ in ASSUMPTION_FIELDS)
    updates_sql = ", ".join(f"{f}=excluded.{f}" for f in ASSUMPTION_FIELDS)
    values = [current[f] for f in ASSUMPTION_FIELDS]

    conn = get_db_connection()
    conn.execute(
        f"""
        INSERT INTO projection_assumptions (id, {columns_sql}, updated_at)
        VALUES (1, {placeholders_sql}, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            {updates_sql},
            updated_at=excluded.updated_at
        """,
        values,
    )
    conn.commit()
    conn.close()
    return current
