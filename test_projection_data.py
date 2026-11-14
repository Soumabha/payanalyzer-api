"""Pure tests for projection_data's retired-column logic (no database)."""

from projection_data import RETIRED_ASSUMPTION_COLUMNS, columns_to_retire
from projections import ASSUMPTION_FIELDS, DEFAULT_ASSUMPTIONS


def test_the_flat_emi_assumptions_are_no_longer_assumption_fields():
    for column in RETIRED_ASSUMPTION_COLUMNS:
        assert column not in ASSUMPTION_FIELDS and column not in DEFAULT_ASSUMPTIONS


def test_only_the_retired_columns_a_table_still_has_are_dropped():
    existing = ["id", "inflation_pct", "loan_outstanding_lakhs", "retirement_year", "loan_end_year", "updated_at"]
    assert columns_to_retire(existing) == ["loan_outstanding_lakhs", "loan_end_year"]


def test_nothing_is_dropped_from_an_already_clean_table():
    assert columns_to_retire(["id", *ASSUMPTION_FIELDS, "updated_at"]) == []
    assert columns_to_retire([]) == []
