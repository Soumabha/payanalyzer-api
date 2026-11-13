"""
Unit tests for splitwise_data.py: the shared description-normalization helpers,
deterministic manual ids, the pure row-mapping / same-expense logic, and the
sync entry point (upsert_expense) driven through an in-memory store.

No database is ever opened (conftest turns any attempt into a failure). Parts of
this module that are nothing but SQL - get_latest_expense_update and
SqliteExpenseStore's queries - cannot be meaningfully unit-tested without a
database engine and are deliberately not covered here.

Run with: python -m pytest test_splitwise_data.py -v
"""

import sqlite3

import pytest

import splitwise_data
from splitwise_data import (
    EXPENSES_SCHEMA,
    create_schema,
    description_key,
    expense_to_row,
    SqliteExpenseStore,
    make_manual_id,
    normalize_description,
    superseded_manual_ids,
    upsert_expense,
)


# --------------------------------------------------------------------------- #
# normalize_description / description_key
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Gas cylinder", "Gas cylinder"),
        ("  Gas   cylinder ", "Gas cylinder"),
        ("Gas\tcylinder\n", "Gas cylinder"),
        ("Tea\u00a0Stall", "Tea Stall"),  # non-breaking space is whitespace too
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_description_collapses_whitespace_and_strips(raw, expected):
    assert normalize_description(raw) == expected


def test_normalize_description_preserves_case():
    assert normalize_description("  Gas  CYLINDER ") == "Gas CYLINDER"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Gas   CYLINDER ", "gas cylinder"),
        (None, ""),
        ("CAF\u00c9  Latte", "caf\u00e9 latte"),  # full Unicode case-folding, not just ASCII
    ],
)
def test_description_key_is_normalized_and_lowercased(raw, expected):
    assert description_key(raw) == expected


# --------------------------------------------------------------------------- #
# make_manual_id
# --------------------------------------------------------------------------- #


def test_make_manual_id_is_always_negative_so_it_cannot_collide_with_real_ids():
    for i in range(200):
        assert make_manual_id(1, "2026-08-02", f"Item {i}", 10.0 + i) < 0


def test_make_manual_id_is_deterministic():
    assert make_manual_id(1, "2026-08-02", "Tea", 10.0) == make_manual_id(1, "2026-08-02", "Tea", 10.0)


def test_make_manual_id_ignores_whitespace_noise_and_cost_formatting():
    base = make_manual_id(1, "2026-08-02", "Gas cylinder", 900)
    assert make_manual_id(1, "2026-08-02", "  Gas   cylinder ", 900.0) == base
    assert make_manual_id(1, "2026-08-02", "Gas cylinder", 900.001) == base  # formatted to 2dp


@pytest.mark.parametrize(
    "args",
    [
        (2, "2026-08-02", "Tea", 10.0),  # group
        (1, "2026-08-03", "Tea", 10.0),  # date
        (1, "2026-08-02", "Coffee", 10.0),  # description
        (1, "2026-08-02", "Tea", 11.0),  # cost
    ],
)
def test_make_manual_id_changes_when_any_identifying_field_changes(args):
    assert make_manual_id(*args) != make_manual_id(1, "2026-08-02", "Tea", 10.0)


# --------------------------------------------------------------------------- #
# expense_to_row
# --------------------------------------------------------------------------- #


class _Category:
    def __init__(self, name):
        self._name = name

    def getName(self):
        return self._name


class FakeExpense:
    """The slice of a splitwise Expense that expense_to_row actually reads."""

    def __init__(
        self,
        id=1001,
        date="2026-08-02T10:30:00Z",
        description="Tea",
        cost="10.00",
        group_id=1,
        category="Food",
        updated_at="2026-08-03T00:00:00Z",
        deleted_at=None,
    ):
        self._v = dict(
            id=id, date=date, description=description, cost=cost, group_id=group_id,
            category=category, updated_at=updated_at, deleted_at=deleted_at,
        )

    def getId(self): return self._v["id"]
    def getDate(self): return self._v["date"]
    def getDescription(self): return self._v["description"]
    def getCost(self): return self._v["cost"]
    def getGroupId(self): return self._v["group_id"]
    def getCategory(self): return _Category(self._v["category"]) if self._v["category"] else None
    def getUpdatedAt(self): return self._v["updated_at"]
    def getDeletedAt(self): return self._v["deleted_at"]


def test_expense_to_row_maps_every_field():
    assert expense_to_row(FakeExpense(deleted_at="2026-08-04T00:00:00Z")) == {
        "id": 1001,
        "date": "2026-08-02",  # time component truncated
        "description": "Tea",
        "cost": 10.0,
        "category": "Food",
        "group_id": 1,
        "updated_at": "2026-08-03T00:00:00Z",
        "deleted_at": "2026-08-04T00:00:00Z",
    }


def test_expense_to_row_tolerates_missing_optional_fields():
    row = expense_to_row(FakeExpense(date=None, description=None, cost=None, category=None, updated_at=None))
    assert (row["date"], row["description"], row["cost"], row["category"]) == (None, "", 0.0, None)
    assert row["updated_at"]  # falls back to "now" rather than storing NULL


def test_expense_to_row_falls_back_to_the_expense_date_for_updated_at():
    assert expense_to_row(FakeExpense(updated_at=None))["updated_at"] == "2026-08-02T10:30:00Z"


def test_expense_to_row_copes_with_an_expense_object_that_has_no_getdeletedat():
    class WithoutGetDeletedAt:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "getDeletedAt":
                raise AttributeError(name)  # older Splitwise Expense objects don't have it
            return getattr(self._inner, name)

    assert expense_to_row(WithoutGetDeletedAt(FakeExpense()))["deleted_at"] is None


# --------------------------------------------------------------------------- #
# superseded_manual_ids: is this manual row the same expense as the API row?
# --------------------------------------------------------------------------- #


def _api(**kw):
    return {"id": 1001, "date": "2026-08-02", "description": "Tea", "cost": 10.0, "group_id": 1, **kw}


def _manual(id=-5, **kw):
    return {"id": id, "date": "2026-08-02", "description": "Tea", "cost": 10.0, "group_id": 1, **kw}


def test_identical_manual_row_is_superseded():
    assert superseded_manual_ids(_api(), [_manual()]) == [-5]


@pytest.mark.parametrize(
    "api_description,manual_description",
    [
        ("Gas  cylinder ", "Gas cylinder"),  # API text carries doubled/trailing spaces
        ("Auto fare", "Auto  fare"),  # ...and the other way round
        ("Tea\u00a0Stall", "Tea Stall"),
    ],
)
def test_whitespace_differences_do_not_hide_a_match(api_description, manual_description):
    assert superseded_manual_ids(_api(description=api_description), [_manual(description=manual_description)]) == [-5]


def test_cost_is_compared_to_two_decimal_places():
    assert superseded_manual_ids(_api(cost=10.004), [_manual(cost=10.0)]) == [-5]


@pytest.mark.parametrize(
    "manual_overrides",
    [
        dict(date="2026-08-03"),
        dict(cost=11.0),
        dict(group_id=2),
        dict(description="Coffee"),
        dict(description="tea"),  # same-expense detection is case-sensitive
    ],
)
def test_a_manual_row_differing_in_any_identifying_field_is_kept(manual_overrides):
    assert superseded_manual_ids(_api(), [_manual(**manual_overrides)]) == []


def test_every_matching_manual_row_is_returned_and_others_are_not():
    rows = [_manual(id=-1), _manual(id=-2, cost=99.0), _manual(id=-3)]
    assert superseded_manual_ids(_api(), rows) == [-1, -3]


def test_no_manual_rows_means_nothing_superseded():
    assert superseded_manual_ids(_api(), []) == []


# --------------------------------------------------------------------------- #
# upsert_expense (through an in-memory store)
# --------------------------------------------------------------------------- #


def test_upsert_expense_stores_an_api_row(store):
    upsert_expense(FakeExpense(), store=store)
    (row,) = store.rows()
    assert row["source"] == "api"
    assert (row["id"], row["date"], row["description"], row["cost"], row["category"], row["group_id"]) == (
        1001, "2026-08-02", "Tea", 10.0, "Food", 1,
    )


def test_upsert_expense_removes_the_manual_row_for_the_same_expense(store):
    store.add("Tea", date="2026-08-02", cost=10.0, source="manual_pdf", id=-5)
    upsert_expense(FakeExpense(), store=store)
    assert [(r["id"], r["source"]) for r in store.rows()] == [(1001, "api")]


def test_upsert_expense_removes_a_manual_row_despite_whitespace_noise_in_the_api_text(store):
    store.add("Gas cylinder", date="2026-08-02", cost=900.0, source="manual_pdf", id=-5)
    upsert_expense(FakeExpense(id=2001, description="Gas  cylinder ", cost="900.00"), store=store)
    assert [r["id"] for r in store.rows()] == [2001]


@pytest.mark.parametrize(
    "manual_kwargs",
    [
        dict(description="Tea", date="2026-08-03", cost=10.0, group_id=1),
        dict(description="Tea", date="2026-08-02", cost=11.0, group_id=1),
        dict(description="Tea", date="2026-08-02", cost=10.0, group_id=2),
        dict(description="Coffee", date="2026-08-02", cost=10.0, group_id=1),
    ],
)
def test_upsert_expense_keeps_manual_rows_for_other_expenses(store, manual_kwargs):
    description = manual_kwargs.pop("description")
    store.add(description, source="manual_pdf", id=-5, **manual_kwargs)
    upsert_expense(FakeExpense(), store=store)
    assert {r["id"] for r in store.rows()} == {-5, 1001}


@pytest.mark.parametrize("overrides", [dict(date=None), dict(group_id=None)])
def test_upsert_expense_skips_dedupe_when_it_cannot_identify_the_expense(store, overrides):
    store.add("Tea", date="2026-08-02", cost=10.0, source="manual_pdf", id=-5)
    upsert_expense(FakeExpense(**overrides), store=store)
    assert {r["id"] for r in store.rows()} == {-5, 1001}


def test_upsert_expense_commits_once_and_leaves_a_borrowed_store_open(store):
    upsert_expense(FakeExpense(), store=store)
    assert store.commits == 1
    assert store.closed is False


def test_upsert_expense_opens_and_closes_its_own_store_when_none_is_given(monkeypatch, store):
    monkeypatch.setattr(splitwise_data, "SqliteExpenseStore", lambda: store)
    upsert_expense(FakeExpense())
    assert store.closed is True
    assert [r["id"] for r in store.rows()] == [1001]


# --------------------------------------------------------------------------- #
# create_schema: the migration decision, with a recording stand-in connection
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class RecordingConnection:
    """Answers PRAGMA table_info with the given column names and records every
    statement it is asked to run. Not a database: it just lets us observe what
    create_schema decides to do."""

    def __init__(self, columns):
        self._columns = columns
        self.statements = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=()):
        self.statements.append(" ".join(sql.split()))
        if sql.strip().upper().startswith("PRAGMA TABLE_INFO"):
            return _Result([(i, name, "TEXT", 0, None, 0) for i, name in enumerate(self._columns)])
        return _Result([])

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _run_create_schema(monkeypatch, columns):
    conn = RecordingConnection(columns)
    monkeypatch.setattr(splitwise_data, "get_db_connection", lambda: conn)
    create_schema()
    return conn


def test_the_schema_defines_source_with_an_api_default():
    assert "source TEXT DEFAULT 'api'" in EXPENSES_SCHEMA


def test_create_schema_adds_the_source_column_to_a_pre_source_table(monkeypatch):
    legacy = ["id", "date", "description", "cost", "category", "group_id", "updated_at", "deleted_at"]
    conn = _run_create_schema(monkeypatch, legacy)
    assert "ALTER TABLE expenses ADD COLUMN source TEXT DEFAULT 'api'" in conn.statements


def test_create_schema_leaves_a_table_that_already_has_source_alone(monkeypatch):
    conn = _run_create_schema(monkeypatch, ["id", "date", "source"])
    assert not any(s.startswith("ALTER TABLE") for s in conn.statements)


def test_create_schema_creates_the_table_first_then_commits_and_closes(monkeypatch):
    conn = _run_create_schema(monkeypatch, ["id", "source"])
    assert conn.statements[0].startswith("CREATE TABLE IF NOT EXISTS expenses")
    assert conn.committed and conn.closed


# --------------------------------------------------------------------------- #
# the guarantee that these are unit tests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "open_database",
    [
        lambda: sqlite3.connect(":memory:"),
        lambda: splitwise_data.get_db_connection(),
        lambda: SqliteExpenseStore(),  # what code does by default when no store is injected
        lambda: upsert_expense(FakeExpense()),
    ],
)
def test_nothing_in_the_unit_test_run_can_open_a_database(open_database):
    with pytest.raises(AssertionError, match="must not open a database connection"):
        open_database()
