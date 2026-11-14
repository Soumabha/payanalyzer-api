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

from datetime import date

from loans import build_timeline
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
JOURNAL_JS = (STATIC / "journal.js").read_text()
JOURNAL_HTML = (STATIC / "journal.html").read_text()
MAIN_PY = (Path(__file__).parent / "main.py").read_text()


def _js_block(name: str) -> str:
    """Source text of `const NAME = [ ... ];` or `const NAME = new Set([ ... ]);`."""
    match = re.search(rf"const {name} = (?:new Set\()?\[(.*?)\]", PROJECTION_JS, re.DOTALL)
    assert match, f"{name} not found in static/projection.js"
    return match.group(1)


def _js_block_object(name: str) -> str:
    match = re.search(rf"const {name} = \{{(.*?)\n\}};", PROJECTION_JS, re.DOTALL)
    assert match, f"{name} not found in static/projection.js"
    return match.group(1)


def _strings(block: str) -> list[str]:
    return re.findall(r'"([^"]+)"', block)


def _engine_row_keys() -> set[str]:
    """Keys of every response shape: the plain forecast, the actuals mode
    (current-year row carries month sub-rows), and rows with journal inputs
    (loan schedule, check-in anchors, transfers)."""
    today = date(PROJECTION_START_YEAR, 6, 1)
    plain = set(run_projection(DEFAULT_ASSUMPTIONS)[0])
    blended = set(run_projection(DEFAULT_ASSUMPTIONS, actual_monthly_spend={}, today=today)[0])
    timeline = build_timeline(
        [{"as_of": f"{PROJECTION_START_YEAR - 1}-12-31", "amount_lakhs": 10.0}],
        [{"effective_from": f"{PROJECTION_START_YEAR - 1}-12-31", "annual_rate_pct": 9.0, "emi_lakhs": 1.0}],
        [], PROJECTION_START_YEAR, PROJECTION_END_YEAR,
    )
    journal_row = run_projection(
        DEFAULT_ASSUMPTIONS,
        actual_monthly_spend={},
        today=today,
        loan_timeline=timeline,
        observations=[{"bucket": "cash", "as_of": f"{PROJECTION_START_YEAR}-03-01", "amount_lakhs": 1.0}],
        transfers=[{"date": f"{PROJECTION_START_YEAR}-04-01", "amount_lakhs": 1.0,
                    "from_bucket": "cash", "to_bucket": "equity"}],
    )[0]
    return plain | blended | set(journal_row)


def _engine_month_keys() -> set[str]:
    """Keys of a month sub-row, with a loan and a check-in so every optional
    field (loan columns, check-in variances) is present."""
    timeline = build_timeline(
        [{"as_of": f"{PROJECTION_START_YEAR - 1}-12-31", "amount_lakhs": 10.0}],
        [{"effective_from": f"{PROJECTION_START_YEAR - 1}-12-31", "annual_rate_pct": 9.0, "emi_lakhs": 1.0}],
        [], PROJECTION_START_YEAR, PROJECTION_END_YEAR,
    )
    row = run_projection(
        DEFAULT_ASSUMPTIONS, actual_monthly_spend={}, today=date(PROJECTION_START_YEAR, 6, 1), loan_timeline=timeline,
        observations=[{"bucket": "cash", "as_of": f"{PROJECTION_START_YEAR}-03-01", "amount_lakhs": 1.0}],
    )[0]
    return set(row["months"][0]) | set(row["months"][2])


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


def test_every_month_field_read_by_the_sub_row_renderer_exists_in_engine_output():
    used = set(re.findall(r"\bmonth\.([a-z_]+)\b", PROJECTION_JS))
    assert {"status", "expense", "income", "net", "actual_spend", "cash_end", "checkin"} <= used, \
        "parser no longer sees month fields"
    assert used <= _engine_month_keys()


def test_every_month_status_the_engine_emits_has_a_style_and_label():
    statuses = set(re.findall(r'(\w+):\s*\{\s*label:', _js_block_object("MONTH_STATUS")))
    assert statuses == {"actual", "in_progress", "forecast", "no_data"}


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


# -- journal page ---------------------------------------------------------------


def _routes() -> set[tuple[str, str]]:
    found = re.findall(r'@app\.(get|post|put|delete)\("([^"]+)"', MAIN_PY)
    return {(method.upper(), re.sub(r"\{\w+\}", "{}", path)) for method, path in found}


def _js_api_calls(source: str) -> set[tuple[str, str]]:
    calls = re.findall(r'api\("(GET|POST|PUT|DELETE)",\s*[`"](/api/[^`"?]+)[`"]', source)
    return {(method, re.sub(r"\$\{[^}]+\}", "{}", path)) for method, path in calls}


def _pydantic_fields() -> set[str]:
    fields: set[str] = set()
    for body in re.findall(r"class \w+\(BaseModel\):\n((?:    .+\n|\n)+)", MAIN_PY):
        fields |= set(re.findall(r"^    (\w+):", body, re.MULTILINE))
    return fields


def test_every_endpoint_the_journal_page_calls_exists_in_main():
    calls = _js_api_calls(JOURNAL_JS)
    assert len(calls) >= 10, "parser no longer sees the journal API calls"
    assert calls <= _routes()


def test_every_form_field_name_in_the_journal_page_is_a_request_model_field():
    names = set(re.findall(r'\bname:\s*"(\w+)"', JOURNAL_JS))
    assert {"as_of", "entry_date", "amount_lakhs", "annual_rate_pct"} <= names
    assert names <= _pydantic_fields()


def test_every_element_id_the_journal_script_looks_up_exists_in_the_page():
    wanted = set(re.findall(r'getElementById\("(\w+)"\)', JOURNAL_JS))
    # Ids come from the page itself or from markup the script builds (the modal).
    present = set(re.findall(r'id="(\w+)"', JOURNAL_HTML)) | set(re.findall(r'id="(\w+)"', JOURNAL_JS))
    assert wanted - present == set()


def test_the_journal_page_is_linked_from_every_page_nav():
    for page in ("index.html", "projection.html", "journal.html"):
        assert 'href="/journal"' in (STATIC / page).read_text(), page


# -- stylesheet ---------------------------------------------------------------------


STYLE_CSS = (STATIC / "style.css").read_text()

# Classes the scripts use only as markers (queried or toggled in JS), with no rules of their own.
HOOK_ONLY_CLASSES: set[str] = set()


def _classes_used(*sources: str) -> set[str]:
    used: set[str] = set()
    for source in sources:
        for value in re.findall(r'class="([^"]*)"', source):
            # Only plain class names; template expressions (${...}, ternaries) are skipped.
            used.update(token for token in value.split() if re.fullmatch(r"[A-Za-z_][\w-]*", token))
        for value in re.findall(r"classList\.(?:add|remove|toggle)\(\"([\w-]+)\"", source):
            used.add(value)
    return {c for c in used if not c.startswith("fa-") and c != "fa-solid"}


def _classes_defined() -> set[str]:
    return set(re.findall(r"\.([A-Za-z_][\w-]*)", STYLE_CSS))


def test_every_css_class_the_pages_use_is_defined_in_the_stylesheet():
    """Guards against rules disappearing from style.css (an edit once truncated
    the file and silently unstyled the Journal page)."""
    used = _classes_used(JOURNAL_HTML, JOURNAL_JS, PROJECTION_HTML, PROJECTION_JS)
    assert {"btn", "modal", "icon-btn", "card-header", "var-chip", "status-legend"} <= used, "parser lost track of classes"
    missing = used - _classes_defined() - HOOK_ONLY_CLASSES
    assert missing == set(), f"classes used by the pages but not styled: {sorted(missing)}"


def test_the_projection_table_has_no_loan_detail_columns():
    """The loan interest/principal/balance live on the Journal page; the projection
    table keeps its original columns so it stays easy to match to the spreadsheet."""
    keys = re.findall(r'\{\s*key:\s*"(\w+)"', _js_block("COLUMN_DEFS"))
    assert keys and not [k for k in keys if k.startswith("loan_")]
    assert 'group: "loan"' not in PROJECTION_JS
