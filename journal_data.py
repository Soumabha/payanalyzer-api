"""
SQLite storage for the journal: loans and their terms, balance observations,
and ledger entries.

Deliberately thin, like SqliteExpenseStore: each method is one plain
query/statement that decides which rows to read or write and nothing else.
All rules (what a loan setup means, what counts as extra income, how legacy
rows migrate) live in journal.py, which only ever sees plain dicts and works
against any object with these methods (tests use fake_journal_store).

Amounts are in lakhs. Rows are never deleted: voiding sets voided_at, and every
read below skips voided rows unless asked otherwise.
"""

from splitwise_data import get_db_connection

JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS loan_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    annual_rate_pct REAL NOT NULL,
    emi_lakhs REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    voided_at TEXT
);

CREATE TABLE IF NOT EXISTS balance_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    ref_id INTEGER,
    as_of TEXT NOT NULL,
    amount_lakhs REAL NOT NULL,
    source TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    voided_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    amount_lakhs REAL NOT NULL,
    ref_id INTEGER,
    from_bucket TEXT,
    to_bucket TEXT,
    status TEXT NOT NULL DEFAULT 'done',
    approximate INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'journal',
    legacy_year INTEGER,
    created_at TEXT NOT NULL,
    voided_at TEXT
);
"""

# What an omitted ledger-entry field means; fake_journal_store applies the same.
ENTRY_DEFAULTS = {
    "ref_id": None, "from_bucket": None, "to_bucket": None, "status": "done", "approximate": False,
    "note": None, "source": "journal", "legacy_year": None,
}

_ENTRY_COLUMNS = (
    "id, kind, entry_date, amount_lakhs, ref_id, from_bucket, to_bucket, status, "
    "approximate, note, source, legacy_year, created_at, voided_at"
)


def create_journal_schema():
    conn = get_db_connection()
    conn.executescript(JOURNAL_SCHEMA)
    conn.commit()
    conn.close()


def _entry(row):
    entry = dict(row)
    entry["approximate"] = bool(entry["approximate"])
    return entry


class SqliteJournalStore:
    def __init__(self, conn=None):
        # check_same_thread=False: FastAPI opens a `yield` dependency's value in
        # one worker thread and runs the endpoint in another. Each request owns
        # its connection exclusively (opened per request, closed after), so
        # handing it across threads is safe; the default check made concurrent
        # page loads fail with sqlite3.ProgrammingError.
        self._conn = conn if conn is not None else get_db_connection(check_same_thread=False)

    def close(self):
        self._conn.close()

    def commit(self):
        self._conn.commit()

    # -- loans ------------------------------------------------------------- #

    def add_loan(self, name, created_at):
        cur = self._conn.execute("INSERT INTO loans (name, created_at) VALUES (?, ?)", (name, created_at))
        return cur.lastrowid

    def loans(self):
        """Open loans, oldest first."""
        return [
            dict(r)
            for r in self._conn.execute("SELECT id, name, created_at FROM loans WHERE closed_at IS NULL ORDER BY id")
        ]

    def close_loan(self, loan_id, closed_at):
        self._conn.execute("UPDATE loans SET closed_at = ? WHERE id = ? AND closed_at IS NULL", (closed_at, loan_id))

    def add_terms(self, loan_id, effective_from, annual_rate_pct, emi_lakhs, note, created_at):
        cur = self._conn.execute(
            "INSERT INTO loan_terms (loan_id, effective_from, annual_rate_pct, emi_lakhs, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (loan_id, effective_from, annual_rate_pct, emi_lakhs, note, created_at),
        )
        return cur.lastrowid

    def terms(self, loan_id):
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, loan_id, effective_from, annual_rate_pct, emi_lakhs, note, created_at FROM loan_terms "
                "WHERE loan_id = ? AND voided_at IS NULL ORDER BY effective_from, id",
                (loan_id,),
            )
        ]

    def void_terms(self, terms_id, voided_at):
        self._conn.execute("UPDATE loan_terms SET voided_at = ? WHERE id = ? AND voided_at IS NULL", (voided_at, terms_id))

    # -- balance observations ---------------------------------------------- #

    def add_observation(self, bucket, ref_id, as_of, amount_lakhs, source, note, created_at):
        cur = self._conn.execute(
            "INSERT INTO balance_observations (bucket, ref_id, as_of, amount_lakhs, source, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bucket, ref_id, as_of, amount_lakhs, source, note, created_at),
        )
        return cur.lastrowid

    def observations(self, bucket=None, ref_id=None):
        sql = (
            "SELECT id, bucket, ref_id, as_of, amount_lakhs, source, note, created_at FROM balance_observations "
            "WHERE voided_at IS NULL"
        )
        params = []
        if bucket is not None:
            sql += " AND bucket = ?"
            params.append(bucket)
        if ref_id is not None:
            sql += " AND ref_id = ?"
            params.append(ref_id)
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY as_of, id", params)]

    def void_observation(self, observation_id, voided_at):
        self._conn.execute(
            "UPDATE balance_observations SET voided_at = ? WHERE id = ? AND voided_at IS NULL",
            (voided_at, observation_id),
        )

    # -- ledger entries ---------------------------------------------------- #

    def add_entry(self, entry):
        cur = self._conn.execute(
            """
            INSERT INTO ledger_entries (kind, entry_date, amount_lakhs, ref_id, from_bucket, to_bucket,
                                        status, approximate, note, source, legacy_year, created_at)
            VALUES (:kind, :entry_date, :amount_lakhs, :ref_id, :from_bucket, :to_bucket,
                    :status, :approximate, :note, :source, :legacy_year, :created_at)
            """,
            {**(row := {**ENTRY_DEFAULTS, **entry}), "approximate": 1 if row["approximate"] else 0},
        )
        return cur.lastrowid

    def entries(self, kind=None, include_voided=False):
        sql = f"SELECT {_ENTRY_COLUMNS} FROM ledger_entries WHERE 1=1"
        params = []
        if not include_voided:
            sql += " AND voided_at IS NULL"
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        return [_entry(r) for r in self._conn.execute(sql + " ORDER BY entry_date, id", params)]

    def entry(self, entry_id):
        row = self._conn.execute(f"SELECT {_ENTRY_COLUMNS} FROM ledger_entries WHERE id = ?", (entry_id,)).fetchone()
        return _entry(row) if row else None

    def void_entry(self, entry_id, voided_at):
        self._conn.execute(
            "UPDATE ledger_entries SET voided_at = ? WHERE id = ? AND voided_at IS NULL", (voided_at, entry_id)
        )

    def set_entry_status(self, entry_id, status):
        self._conn.execute("UPDATE ledger_entries SET status = ? WHERE id = ?", (status, entry_id))

    # -- legacy ------------------------------------------------------------ #

    def drop_legacy_extra_income_table(self):
        self._conn.execute("DROP TABLE IF EXISTS projection_extra_income")

    def legacy_extra_income_rows(self):
        """Rows of the pre-journal projection_extra_income table (year-only)."""
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projection_extra_income'"
        ).fetchone()
        if not exists:
            return []
        return [
            dict(r)
            for r in self._conn.execute("SELECT year, amount_lakhs, note FROM projection_extra_income ORDER BY year")
        ]
