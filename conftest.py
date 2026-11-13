"""
Shared pytest setup. These are unit tests: none of them may open a database.

The import/sync logic reaches the database only through a store object
(splitwise_data.SqliteExpenseStore), so tests hand it an in-memory fake instead
(see fake_expense_store.py). To make that a guarantee rather than a habit, an
autouse fixture turns any attempt to open a SQLite connection into a failing
test.
"""

import sqlite3

import pytest

import splitwise_data
from fake_expense_store import InMemoryExpenseStore


@pytest.fixture(autouse=True)
def _forbid_database_connections(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(
            "unit tests must not open a database connection - pass an InMemoryExpenseStore "
            "(the `store` fixture) to the code under test instead"
        )

    monkeypatch.setattr(sqlite3, "connect", refuse)
    monkeypatch.setattr(splitwise_data, "get_db_connection", refuse)


@pytest.fixture
def store():
    """A fresh, empty in-memory expense store."""
    return InMemoryExpenseStore()
