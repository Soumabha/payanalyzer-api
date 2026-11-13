"""
Golden-snapshot tests for projections.run_projection().

Pure and DB-free, like test_projections.py. Where that suite checks individual
formulas with hand-picked inputs, this one pins the ENTIRE output (every row,
every column) for two fixed scenarios, so any unintended change to the engine
- including ones in code paths no specific test targets - fails loudly.

The expected output lives in golden/projection_golden.json.

When a test here fails:
  * unintended failure -> you broke the engine; fix the code.
  * intended change (you deliberately changed a formula, a default, or added a
    column) -> review the diff of the golden file, then regenerate it:

        env/bin/python test_projection_golden.py --update

    and commit the updated JSON together with the change that caused it. Phases
    that claim "no change to existing numbers" must NOT touch the golden file.
"""

import json
import sys
from pathlib import Path

import pytest

from projections import (
    DEFAULT_ASSUMPTIONS,
    PROJECTION_END_YEAR,
    PROJECTION_START_YEAR,
    run_projection,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "projection_golden.json"


def _scenarios():
    """
    Name -> kwargs for run_projection(). Built from the engine's own year
    constants so changing PROJECTION_START_YEAR/END_YEAR doesn't silently
    invalidate the scenario shapes (the golden file will still flag the
    resulting number changes, as it should).
    """
    rich = {
        **DEFAULT_ASSUMPTIONS,
        "current_annual_expense_lakhs": 14.5,
        "inflation_pct": 6.5,
        "education_inflation_pct": 9.0,
        "healthcare_inflation_pct": 8.5,
        "kids_expense_start_year": PROJECTION_START_YEAR + 2,
        "kids_school_start_age": 4,
        "kids_expense_end_year": PROJECTION_START_YEAR + 30,
        "parents_expense_start_year": PROJECTION_START_YEAR + 8,
        "parents_expense_end_year": PROJECTION_START_YEAR + 25,
        "annual_emi_lakhs": 9.5,
        "loan_end_year": PROJECTION_START_YEAR + 6,
        "rental_income_lakhs": 4.0,
        "rent_increase_pct": 4.0,
        "current_annual_salary_lakhs": 30.0,
        "salary_growth_pct": 7.0,
        "retirement_year": PROJECTION_START_YEAR + 18,
        "starting_cash_lakhs": 8.0,
        "equity_portfolio_lakhs": 60.0,
        "equity_return_pct": 11.0,
        "debt_portfolio_lakhs": 25.0,
        "debt_return_pct": 6.5,
    }
    return {
        "default_assumptions": {
            "assumptions": dict(DEFAULT_ASSUMPTIONS),
            "extra_income_by_year": None,
        },
        # Exercises kids (basic + education), parents, EMI end, retirement
        # (salary cut-off -> waterfall), extra income, and a custom end year.
        "rich_scenario": {
            "assumptions": rich,
            "extra_income_by_year": {
                PROJECTION_START_YEAR + 1: 10.0,
                PROJECTION_START_YEAR + 14: 5.5,
            },
            "end_year": PROJECTION_END_YEAR - 5,
        },
    }


def _compute():
    return {name: run_projection(**kwargs) for name, kwargs in _scenarios().items()}


def _load_golden():
    return json.loads(GOLDEN_PATH.read_text())


@pytest.mark.parametrize("name", ["default_assumptions", "rich_scenario"])
def test_projection_output_matches_golden_snapshot(name):
    expected = _load_golden()[name]
    actual = _compute()[name]
    assert len(actual) == len(expected), "row count changed"
    for actual_row, expected_row in zip(actual, expected):
        # Compare row by row so a failure names the exact year and columns.
        assert actual_row == expected_row, f"year {expected_row['year']} differs"


def test_golden_file_covers_exactly_the_defined_scenarios():
    assert set(_load_golden()) == set(_scenarios())


def test_default_scenario_spans_the_full_projection_horizon():
    rows = _load_golden()["default_assumptions"]
    assert rows[0]["year"] == PROJECTION_START_YEAR
    assert rows[-1]["year"] == PROJECTION_END_YEAR


def test_rich_scenario_actually_exercises_every_feature():
    """Guards the snapshot itself: if the scenario stopped covering a
    feature, a passing golden comparison would stop meaning much."""
    rows = {r["year"]: r for r in _load_golden()["rich_scenario"]}
    assert any(r["kids_expense"] > 0 for r in rows.values())
    assert any(r["parents_expense"] > 0 for r in rows.values())
    assert any(r["emi_amount"] > 0 for r in rows.values())
    assert any(r["emi_amount"] == 0 for r in rows.values())
    assert any(r["salary"] > 0 for r in rows.values())
    assert any(r["salary"] == 0 for r in rows.values())
    assert any(r["extra_income"] > 0 for r in rows.values())
    assert any(r["net_expense"] > 0 for r in rows.values())


if __name__ == "__main__":
    if "--update" not in sys.argv:
        sys.exit("Pass --update to rewrite golden/projection_golden.json")
    GOLDEN_PATH.parent.mkdir(exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(_compute(), indent=1, sort_keys=False) + "\n")
    print(f"Wrote {GOLDEN_PATH}")
