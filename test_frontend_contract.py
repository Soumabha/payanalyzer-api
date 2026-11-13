"""
Backend <-> frontend contract tests for the projection page.

There is no JS test runner here, and no shared runtime between Python and the
browser, so a few names and constants are duplicated by hand in
static/projection.js / projection.html. These tests parse those files as text
and assert they still agree with the Python side, so a rename or a changed
year constant on one side can't silently break the page.

Pure and DB-free: reads source files only.
"""

import re
from pathlib import Path

from projections import (
    ASSUMPTION_FIELDS,
    DEFAULT_ASSUMPTIONS,
    PROJECTION_END_YEAR,
    PROJECTION_START_YEAR,
    run_projection,
)

STATIC = Path(__file__).parent / "static"
PROJECTION_JS = (STATIC / "projection.js").read_text()
PROJECTION_HTML = (STATIC / "projection.html").read_text()


def _js_block(name: str) -> str:
    """Source text of `const NAME = [ ... ];` or `const NAME = new Set([ ... ]);`."""
    match = re.search(rf"const {name} = (?:new Set\()?\[(.*?)\]", PROJECTION_JS, re.DOTALL)
    assert match, f"{name} not found in static/projection.js"
    return match.group(1)


def _strings(block: str) -> list[str]:
    return re.findall(r'"([^"]+)"', block)


def _engine_row_keys() -> set[str]:
    return set(run_projection(DEFAULT_ASSUMPTIONS)[0])


def test_every_table_column_exists_in_engine_output():
    column_keys = re.findall(r'\{\s*key:\s*"(\w+)"', _js_block("COLUMN_DEFS"))
    assert column_keys, "no COLUMN_DEFS keys parsed"
    assert set(column_keys) <= _engine_row_keys()


def test_every_row_field_read_by_the_chart_and_milestones_exists_in_engine_output():
    # Matches `r.total_portfolio` in the chart mappers and `row.year` in the
    # table renderer - the only places the JS reads row fields outside COLUMN_DEFS.
    used = set(re.findall(r"\b(?:row|r)\.([a-z_]+)\b", PROJECTION_JS))
    assert {"year", "total_portfolio"} <= used, "parser no longer sees the expected row fields"
    assert used <= _engine_row_keys()


def test_js_assumption_fields_match_the_backend_exactly():
    assert _strings(_js_block("ASSUMPTION_FIELDS")) == ASSUMPTION_FIELDS


def test_every_assumption_field_has_a_form_input_with_that_id():
    html_ids = set(re.findall(r'id="([a-z_A-Z]+)"', PROJECTION_HTML))
    assert set(ASSUMPTION_FIELDS) <= html_ids


def test_js_integer_fields_are_valid_assumption_fields():
    assert set(_strings(_js_block("INTEGER_FIELDS"))) <= set(ASSUMPTION_FIELDS)


def test_js_year_select_ids_are_assumption_fields_or_the_extra_income_picker():
    ids = set(_strings(_js_block("YEAR_SELECT_IDS")))
    assert ids - {"extraIncomeYear"} <= set(ASSUMPTION_FIELDS)


def test_js_projection_year_constants_match_the_backend():
    start = int(re.search(r"const PROJECTION_START_YEAR = (\d+)", PROJECTION_JS).group(1))
    end = int(re.search(r"const PROJECTION_END_YEAR = (\d+)", PROJECTION_JS).group(1))
    assert (start, end) == (PROJECTION_START_YEAR, PROJECTION_END_YEAR)
