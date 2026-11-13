"""
In-memory stand-in for splitwise_data.SqliteExpenseStore, for unit tests.

Rows live in a plain dict keyed by id (mirroring the table's primary key); no
database connection of any kind is created. Each read method applies the same
simple row filter as its SQLite counterpart's WHERE clause, and each write
method has the same effect as its SQL statement - nothing more. All the real
decisions (normalization, matching, what gets replaced, categorization) live in
the code under test and only ever see the plain dicts this returns.

This is test support, not production code. The one thing no unit test can
check is that SqliteExpenseStore's SQL really filters the way this fake does;
keep the two in step when either changes.
"""

import copy
import itertools


class InMemoryExpenseStore:
    def __init__(self):
        self._rows = {}
        self._api_ids = itertools.count(1)
        self._manual_ids = itertools.count(1)
        self.commits = 0
        self.closed = False

    # -- test helpers ------------------------------------------------------- #

    def add(
        self,
        description,
        *,
        date="2026-07-01",
        cost=100.0,
        category=None,
        group_id=1,
        source="api",
        deleted_at=None,
        updated_at="2026-07-01T00:00:00",
        id=None,
    ):
        """Seed a row; returns its id. Auto ids: positive for api, negative for manual."""
        if id is None:
            id = next(self._api_ids) if source == "api" else -next(self._manual_ids)
        self._rows[id] = {
            "id": id, "date": date, "description": description, "cost": cost, "category": category,
            "group_id": group_id, "updated_at": updated_at, "deleted_at": deleted_at, "source": source,
        }
        return id

    def set_category(self, description, category):
        """Simulate correcting a category by hand."""
        for row in self._rows.values():
            if row["description"] == description:
                row["category"] = category

    def rows(self):
        """Every row, copied, in a stable order."""
        return sorted(
            (copy.deepcopy(r) for r in self._rows.values()),
            key=lambda r: (r["date"] or "", r["description"], r["cost"], r["id"]),
        )

    def manual(self):
        return [r for r in self.rows() if r["source"] == "manual_pdf"]

    # -- the SqliteExpenseStore interface ----------------------------------- #

    def close(self):
        self.closed = True

    def commit(self):
        self.commits += 1

    def api_rows(self, group_id, year_month):
        return [
            {"date": r["date"], "description": r["description"], "cost": r["cost"]}
            for r in self._rows.values()
            if r["source"] == "api" and r["group_id"] == group_id and _in_month(r["date"], year_month)
            and r["deleted_at"] is None
        ]

    def manual_rows(self, group_id, year_month):
        return [
            {k: r[k] for k in ("id", "date", "description", "cost", "category", "group_id")}
            for r in self._rows.values()
            if r["source"] == "manual_pdf" and r["group_id"] == group_id and _in_month(r["date"], year_month)
        ]

    def categorized_history(self):
        return [
            {k: r[k] for k in ("id", "date", "description", "category")}
            for r in self._rows.values()
            if r["category"] is not None and r["deleted_at"] is None
        ]

    def rows_for_evaluation(self, start_date, end_date, ids=None):
        if ids:
            picked = [r for r in self._rows.values() if r["id"] in set(ids)]
        else:
            picked = [r for r in self._rows.values() if r["date"] is not None and start_date <= r["date"] <= end_date]
        picked = [r for r in picked if r["deleted_at"] is None and r["category"] is not None]
        return [
            {k: r[k] for k in ("id", "date", "description", "cost", "category")}
            for r in sorted(picked, key=lambda r: r["date"] or "")
        ]

    def upsert_api_row(self, row):
        self._rows[row["id"]] = {**row, "source": "api"}

    def upsert_manual_rows(self, rows):
        for row in rows:
            self._rows[row["id"]] = {**row, "deleted_at": None, "source": "manual_pdf"}

    def delete_ids(self, ids):
        for id in ids:
            self._rows.pop(id, None)


def _in_month(date, year_month):
    return date is not None and date[:7] == year_month

