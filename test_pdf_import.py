"""
Tests for pdf_import.py.

Two layers:
  * Parsing - turning printable-summary text into rows. Real PDFs need a real
    PDF file, so these feed the same extracted-text shape the parser expects
    (and a stand-in for pypdf's PdfReader for the multi-page logic). NOTE: no
    real Splitwise PDF is checked in, so the text format here is modelled on the
    parser's own documented format, not captured from a live export.
  * Importing - import_rows() driven through an in-memory store (the `store`
    fixture; no database is ever opened): API-dedupe, possible-edit flagging,
    idempotence, the "replace only the rows on dates the PDF lists" rule,
    categorization that counts the rows being replaced as evidence, and the
    guarantee that --dry-run reports exactly what a real run writes.

Run with: python -m pytest test_pdf_import.py -v
"""

import logging
import sys

import pytest

import pdf_import
from pdf_import import (
    PdfParseError,
    _collect_group_overview_text,
    _rows_from_text,
    import_rows,
    main,
    parse_printable_summary,
)
from splitwise_data import make_manual_id

GROUP = 25397011


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def pdf_text(rows, ym="2026-08", group=GROUP, tail="TOTAL BALANCE as of Aug 31"):
    """Collapsed page text in the shape the parser expects.
    rows: iterable of (day, description, cost_string)."""
    body = " ".join(f"Aug {day:02d} {desc} \u20b9 {cost} \u20b9 0.00 \u20b9 0.00" for day, desc, cost in rows)
    footer = f"https://secure.splitwise.com/api/v3.0/get_printable_summary?group_id={group}&date={ym}"
    return f"Group overview {body} {tail} {footer}"


def row(date, description, cost, group_id=GROUP):
    return {"date": date, "description": description, "cost": cost, "group_id": group_id}


def by_key(rows):
    return {(r["date"], r["description"]): r for r in rows}


class _Page:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _Reader:
    def __init__(self, page_texts):
        self.pages = [_Page(t) for t in page_texts]


@pytest.fixture
def fake_pdfs(monkeypatch):
    """fake_pdfs["a.pdf"] = [page_text, ...] makes PdfReader("a.pdf") return those pages."""
    registry = {}
    monkeypatch.setattr(pdf_import, "PdfReader", lambda path: _Reader(registry[path]))
    return registry


# --------------------------------------------------------------------------- #
# parsing: text -> rows
# --------------------------------------------------------------------------- #


def test_rows_from_text_reads_group_month_and_each_expense_row():
    rows = _rows_from_text(pdf_text([(2, "Medicine and glucometer", "3513.00"), (20, "Cab", "200.00")]))
    assert rows == [
        {"date": "2026-08-02", "description": "Medicine and glucometer", "cost": 3513.0, "group_id": GROUP},
        {"date": "2026-08-20", "description": "Cab", "cost": 200.0, "group_id": GROUP},
    ]


def test_rows_from_text_keeps_only_the_first_amount_of_each_row():
    # Trailing columns are per-member settled-up amounts, not part of the cost.
    (parsed,) = _rows_from_text(pdf_text([(2, "Tea", "10.00")]))
    assert parsed["cost"] == 10.0


@pytest.mark.parametrize("amount,expected", [("1,234.50", 1234.5), ("1,23,456.00", 123456.0), ("0.99", 0.99)])
def test_rows_from_text_parses_thousands_separators(amount, expected):
    (parsed,) = _rows_from_text(pdf_text([(2, "Rent", amount)]))
    assert parsed["cost"] == pytest.approx(expected)


def test_rows_from_text_zero_pads_single_digit_days():
    text = pdf_text([]).replace("Group overview", "Group overview Aug 2 Tea \u20b9 10.00")
    (parsed,) = _rows_from_text(text)
    assert parsed["date"] == "2026-08-02"


def test_rows_from_text_normalizes_descriptions():
    (parsed,) = _rows_from_text(pdf_text([(2, "Gas   cylinder", "900.00")]))
    assert parsed["description"] == "Gas cylinder"


def test_rows_from_text_requires_the_footer_url():
    with pytest.raises(PdfParseError, match="group_id"):
        _rows_from_text("Aug 02 Tea \u20b9 10.00 \u20b9 0.00")


def test_rows_from_text_with_no_expense_rows_returns_empty_and_warns(caplog):
    caplog.set_level(logging.WARNING, logger="pdf_import")
    assert _rows_from_text(pdf_text([])) == []
    assert "no expense rows found" in caplog.text


def test_collect_overview_text_stops_at_the_balance_summary_and_collapses_whitespace():
    reader = _Reader(
        [
            "Group overview\nAug 02\nTea \u20b9 10.00",
            None,  # a page with no extractable text must not break anything
            "Aug 03   Cab \u20b9 200.00\nTOTAL BALANCE as of Aug 31",
            "PER-MEMBER BREAKDOWN Aug 02 Tea \u20b9 10.00",  # repeats expenses per member: must be ignored
        ]
    )
    text = _collect_group_overview_text(reader)
    assert "Aug 02 Tea" in text and "Aug 03 Cab" in text
    assert "\n" not in text and "  " not in text
    assert "PER-MEMBER" not in text


def test_parse_printable_summary_reads_every_overview_page_but_not_the_member_pages(fake_pdfs):
    fake_pdfs["aug.pdf"] = [
        pdf_text([(2, "Tea", "10.00")], tail=""),
        pdf_text([(3, "Cab", "200.00")]),  # carries the TOTAL BALANCE marker
        pdf_text([(2, "Tea", "10.00"), (3, "Cab", "200.00")], tail=""),  # member breakdown
    ]
    rows = parse_printable_summary("aug.pdf")
    assert [(r["date"], r["description"]) for r in rows] == [("2026-08-02", "Tea"), ("2026-08-03", "Cab")]


def test_parse_printable_summary_rejects_a_pdf_with_no_pages(fake_pdfs):
    fake_pdfs["empty.pdf"] = []
    with pytest.raises(PdfParseError, match="no pages"):
        parse_printable_summary("empty.pdf")


# --------------------------------------------------------------------------- #
# import_rows: basics
# --------------------------------------------------------------------------- #


def test_import_rows_with_nothing_to_import_is_a_no_op(store):
    assert import_rows([], store=store) == (0, [], [], [])
    assert store.rows() == [] and store.commits == 0


def test_import_rows_writes_manual_rows_with_deterministic_negative_ids(store):
    written, *_ = import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    assert written == 1
    (stored,) = store.rows()
    assert stored["source"] == "manual_pdf"
    assert stored["id"] == make_manual_id(GROUP, "2026-08-02", "Tea", 10.0) < 0
    assert stored["deleted_at"] is None and stored["updated_at"]
    assert (stored["date"], stored["description"], stored["cost"], stored["group_id"]) == ("2026-08-02", "Tea", 10.0, GROUP)


def test_import_rows_commits_a_real_run_and_leaves_a_borrowed_store_open(store):
    import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    assert store.commits == 1
    assert store.closed is False


def test_import_rows_opens_and_closes_its_own_store_when_none_is_given(monkeypatch, store):
    monkeypatch.setattr(pdf_import, "SqliteExpenseStore", lambda: store)
    import_rows([row("2026-08-02", "Tea", 10.0)])
    assert store.closed is True
    assert [r["description"] for r in store.rows()] == ["Tea"]


def test_import_rows_infers_categories_from_history_and_reports_the_rest(store):
    store.add("Cab", category="Transport", date="2026-07-01", group_id=GROUP)
    store.add("Groceries - Big Bazaar", category="Groceries", date="2026-07-02", group_id=GROUP)
    written, fuzzy, uncategorized, edits = import_rows(
        [
            row("2026-08-02", "Cab", 200.0),  # exact
            row("2026-08-03", "Groceries - Big Bazar", 510.0),  # fuzzy
            row("2026-08-04", "Brand New Thing", 5.0),  # nothing to go on
        ],
        store=store,
    )
    assert written == 3 and edits == []
    assert [(r["description"], r["category"]) for r in fuzzy] == [("Groceries - Big Bazar", "Groceries")]
    assert [r["description"] for r in uncategorized] == ["Brand New Thing"]
    cats = {r["description"]: r["category"] for r in store.manual()}
    assert cats == {"Cab": "Transport", "Groceries - Big Bazar": "Groceries", "Brand New Thing": None}


def test_import_rows_skips_rows_already_synced_via_the_api(store):
    store.add("Gas  cylinder ", date="2026-08-02", cost=900.0, category="Utilities", group_id=GROUP, id=11)  # raw API text
    written, *_ = import_rows([row("2026-08-02", "Gas cylinder", 900.0)], store=store)
    assert written == 0
    assert [r["id"] for r in store.rows()] == [11]


def test_import_rows_ignores_deleted_api_rows_when_deduping(store):
    store.add("Tea", date="2026-08-02", cost=10.0, group_id=GROUP, id=11, deleted_at="2026-08-03T00:00:00")
    written, *_ = import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    assert written == 1


def test_import_rows_matches_api_rows_one_for_one(store):
    # Two identical expenses in the PDF, only one of them synced so far: the
    # other must still come in rather than being swallowed as a duplicate.
    store.add("Auto", date="2026-08-09", cost=50.0, group_id=GROUP, id=11)
    written, *_ = import_rows([row("2026-08-09", "Auto", 50.0), row("2026-08-09", "Auto", 50.0)], store=store)
    assert written == 1
    assert sorted(r["source"] for r in store.rows()) == ["api", "manual_pdf"]


def test_import_rows_flags_a_redated_expense_instead_of_importing_a_near_duplicate(store):
    store.add("Rent", date="2026-08-01", cost=1000.0, group_id=GROUP, id=11)
    written, _, _, edits = import_rows([row("2026-08-03", "Rent", 1000.0)], store=store)
    assert written == 0
    assert [(e["date"], e["description"], e["existing_api_dates"]) for e in edits] == [("2026-08-03", "Rent", ["2026-08-01"])]
    assert [r["id"] for r in store.rows()] == [11]


def test_import_rows_is_idempotent(store):
    rows = [row("2026-08-02", "Tea", 10.0), row("2026-08-20", "Cab", 200.0)]
    def content():  # everything except updated_at, which a re-import legitimately refreshes
        return [{k: v for k, v in r.items() if k != "updated_at"} for r in store.rows()]

    import_rows(rows, store=store)
    first = content()
    import_rows(rows, store=store)
    assert content() == first and len(first) == 2


# --------------------------------------------------------------------------- #
# import_rows: which existing manual rows get replaced. The rule is group +
# DATE: every manual row of the group on a date the PDF lists is replaced,
# whatever its description or cost; nothing else is ever touched.
# --------------------------------------------------------------------------- #


def test_partial_pdf_does_not_delete_manual_rows_on_dates_it_does_not_list(store):
    import_rows([row("2026-08-02", "Tea", 10.0), row("2026-08-20", "Cab", 200.0)], store=store)
    store.set_category("Cab", "Transport")  # a hand correction

    import_rows([row("2026-08-02", "Tea", 10.0)], store=store)  # partial PDF: same month, one date

    rows = by_key(store.rows())
    assert set(rows) == {("2026-08-02", "Tea"), ("2026-08-20", "Cab")}
    assert rows[("2026-08-20", "Cab")]["category"] == "Transport"


def test_every_manual_row_on_a_listed_date_is_replaced_whatever_its_description_or_cost(store):
    import_rows([row("2026-08-02", "Tea", 10.0), row("2026-08-02", "Samosa", 20.0)], store=store)
    import_rows([row("2026-08-02", "Tea", 12.0)], store=store)  # Tea's cost edited; Samosa deleted in Splitwise
    assert [(r["description"], r["cost"]) for r in store.rows()] == [("Tea", 12.0)]


def test_a_row_edited_on_the_same_date_is_replaced_not_duplicated(store):
    import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    import_rows([row("2026-08-02", "Chai", 10.0)], store=store)  # renamed in Splitwise
    assert [r["description"] for r in store.rows()] == ["Chai"]


def test_a_row_re_dated_to_a_date_the_pdf_no_longer_lists_stays_as_a_known_duplicate(store):
    import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    import_rows([row("2026-08-03", "Tea", 10.0)], store=store)  # Tea was moved from the 2nd to the 3rd
    assert sorted(r["date"] for r in store.rows()) == ["2026-08-02", "2026-08-03"]


def test_api_rows_are_never_replaced_even_on_a_listed_date(store):
    store.add("Api Thing", date="2026-08-02", source="api", group_id=GROUP, id=3)
    import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    assert {r["description"] for r in store.rows()} == {"Api Thing", "Tea"}


def test_other_months_and_other_groups_are_never_touched(store):
    store.add("July Thing", date="2026-07-02", source="manual_pdf", id=-1, group_id=GROUP)
    store.add("Other Group Thing", date="2026-08-02", source="manual_pdf", id=-2, group_id=999)
    import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    assert {r["description"] for r in store.rows()} == {"July Thing", "Other Group Thing", "Tea"}


def test_a_multi_month_import_replaces_only_the_dates_listed_in_each_month(store):
    store.add("Aug Keep", date="2026-08-25", source="manual_pdf", id=-1, group_id=GROUP)
    store.add("Aug Replace", date="2026-08-02", source="manual_pdf", id=-2, group_id=GROUP)
    store.add("Sep Keep", date="2026-09-25", source="manual_pdf", id=-3, group_id=GROUP)
    store.add("Sep Replace", date="2026-09-02", source="manual_pdf", id=-4, group_id=GROUP)
    import_rows([row("2026-08-02", "Aug New", 1.0), row("2026-09-02", "Sep New", 2.0)], store=store)
    assert {r["description"] for r in store.rows()} == {"Aug Keep", "Aug New", "Sep Keep", "Sep New"}


def test_replaces_every_row_on_a_listed_date_however_many_there_are(store):
    for i in range(1100):
        store.add(f"Item {i}", date="2026-08-10", cost=1.0, category="Misc", source="manual_pdf", group_id=GROUP, id=-(i + 1))
    import_rows([row("2026-08-10", "Only One", 5.0)], store=store)
    assert [r["description"] for r in store.rows()] == ["Only One"]


def test_the_run_logs_how_many_manual_rows_it_replaced_and_left_alone(store, caplog):
    import_rows([row("2026-08-02", "Tea", 10.0), row("2026-08-20", "Cab", 200.0)], store=store)
    caplog.set_level(logging.INFO, logger="pdf_import")
    import_rows([row("2026-08-02", "Tea", 10.0)], store=store)
    assert "Replacing 1 existing manual_pdf row(s)" in caplog.text
    assert "1 existing manual_pdf row(s)" in caplog.text and "left untouched" in caplog.text


# --------------------------------------------------------------------------- #
# categorization: the rows being replaced count as evidence
# --------------------------------------------------------------------------- #


def test_a_re_imported_row_keeps_the_category_its_previous_version_had(store):
    same = row("2026-08-06", "Mystery Item", 5.0)
    import_rows([same], store=store)
    store.set_category("Mystery Item", "Misc")  # corrected by hand after the first import

    _, fuzzy, uncategorized, _ = import_rows([same], store=store)

    assert fuzzy == [] and uncategorized == []
    assert store.manual()[0]["category"] == "Misc"


def test_rows_about_to_be_replaced_still_count_as_evidence_for_their_replacements(store):
    # The old row is the only "Taxi Ride" anywhere; it is on a date the PDF lists,
    # so it is replaced - but it is still what tells us the new row's category.
    store.add("Taxi Ride", date="2026-08-06", cost=80.0, category="Transport", source="manual_pdf", group_id=GROUP)
    _, fuzzy, uncategorized, _ = import_rows([row("2026-08-06", "Taxi Ride", 80.0)], store=store)
    assert fuzzy == [] and uncategorized == []
    assert store.manual()[0]["category"] == "Transport"


def test_a_renamed_row_is_fuzzy_matched_against_the_hand_categorized_row_it_replaces(store):
    store.add("Groceries - Big Bazaar", date="2026-08-05", cost=500.0, category="Groceries", source="manual_pdf", group_id=GROUP)
    _, fuzzy, uncategorized, _ = import_rows([row("2026-08-05", "Groceries - Big Bazar", 510.0)], store=store)
    assert [(r["description"], r["category"]) for r in fuzzy] == [("Groceries - Big Bazar", "Groceries")]
    assert uncategorized == []
    assert [(r["description"], r["category"]) for r in store.manual()] == [("Groceries - Big Bazar", "Groceries")]


def test_when_other_rows_outvote_it_a_hand_correction_does_not_survive_a_re_import(store):
    # Documents the current precedence rather than endorsing it: the exact tier
    # takes the MAJORITY category among all rows with that description, so one
    # hand-corrected row (Travel) loses to three others (Transport).
    for day in (1, 2, 3):
        store.add("Cab", date=f"2026-07-0{day}", category="Transport", group_id=GROUP)
    store.add("Cab", date="2026-08-02", cost=250.0, category="Travel", source="manual_pdf", group_id=GROUP)

    import_rows([row("2026-08-02", "Cab", 250.0)], store=store)

    assert store.manual()[0]["category"] == "Transport"


# --------------------------------------------------------------------------- #
# --dry-run must report exactly what a real run writes
# --------------------------------------------------------------------------- #


def assert_dry_run_matches_real_run(rows, store):
    """Run the import as a dry run, then for real, from the same state.
    Checks: the dry run wrote nothing; both runs reported the same thing; and
    what the real run stored is what was reported. Returns the real result."""
    before = store.rows()
    commits_before = store.commits
    dry = import_rows(rows, dry_run=True, store=store)
    assert store.rows() == before and store.commits == commits_before, "a dry run must not write anything"

    real = import_rows(rows, store=store)
    assert dry == real, "dry-run report differs from what the real run did"

    stored = {(r["date"], r["description"]): r["category"] for r in store.manual()}
    _, fuzzy, uncategorized, _ = real
    for r in fuzzy:
        assert stored[(r["date"], r["description"])] == r["category"]
    for r in uncategorized:
        assert stored[(r["date"], r["description"])] is None
    return real


def test_dry_run_matches_real_when_a_renamed_row_replaces_a_hand_categorized_one(store):
    store.add("Groceries - Big Bazaar", date="2026-08-05", cost=500.0, category="Groceries", source="manual_pdf", group_id=GROUP)
    _, fuzzy, uncategorized, _ = assert_dry_run_matches_real_run([row("2026-08-05", "Groceries - Big Bazar", 510.0)], store)
    assert [(r["description"], r["category"]) for r in fuzzy] == [("Groceries - Big Bazar", "Groceries")]
    assert uncategorized == []


def test_dry_run_matches_real_when_the_similar_row_is_on_a_date_the_pdf_does_not_list(store):
    store.add("Groceries - Big Bazaar", date="2026-08-05", cost=500.0, category="Groceries", source="manual_pdf", group_id=GROUP)
    _, fuzzy, uncategorized, _ = assert_dry_run_matches_real_run([row("2026-08-06", "Groceries - Big Bazar", 510.0)], store)
    assert [(r["description"], r["category"]) for r in fuzzy] == [("Groceries - Big Bazar", "Groceries")]
    assert uncategorized == []


def test_dry_run_matches_real_for_a_re_imported_hand_corrected_row(store):
    same = row("2026-08-06", "Mystery Item", 5.0)
    import_rows([same], store=store)
    store.set_category("Mystery Item", "Misc")
    assert_dry_run_matches_real_run([same], store)
    assert store.manual()[0]["category"] == "Misc"


def test_dry_run_matches_real_for_exact_history_matches(store):
    store.add("Cab", date="2026-07-01", category="Transport", group_id=GROUP)
    assert_dry_run_matches_real_run([row("2026-08-02", "Cab", 200.0)], store)
    assert store.manual()[0]["category"] == "Transport"


def test_inferred_categories_do_not_become_evidence_for_each_other_within_one_import(store):
    # B is similar to A, but not to anything in history (A -> history jaccard
    # 0.50, B -> A 0.50, B -> history 0.20). A real run that wrote A before
    # categorizing B would chain B onto A's *inferred* category; a dry run can't
    # do that, so the two used to disagree. Now neither does.
    store.add("weekend vegetable market", date="2026-07-01", category="Groceries", group_id=GROUP)
    _, fuzzy, uncategorized, _ = assert_dry_run_matches_real_run(
        [row("2026-08-02", "weekend vegetable markit", 40.0), row("2026-08-02", "weekend vegetabel markit", 45.0)],
        store,
    )
    assert [r["description"] for r in fuzzy] == ["weekend vegetable markit"]
    assert [r["description"] for r in uncategorized] == ["weekend vegetabel markit"]


def test_dry_run_matches_real_with_api_duplicates_and_possible_edits_in_the_mix(store):
    store.add("Tea", date="2026-08-02", cost=10.0, group_id=GROUP, id=11)  # already synced: skipped
    store.add("Rent", date="2026-08-01", cost=1000.0, group_id=GROUP, id=12)  # re-dated: flagged
    written, _, _, edits = assert_dry_run_matches_real_run(
        [row("2026-08-02", "Tea", 10.0), row("2026-08-03", "Rent", 1000.0), row("2026-08-04", "Fresh", 7.0)],
        store,
    )
    assert written == 1
    assert [e["description"] for e in edits] == ["Rent"]


@pytest.mark.xfail(
    strict=True,
    reason="Known gap, not fixed here: two genuinely identical expenses on one day share a "
    "make_manual_id, so the second write overwrites the first and one expense is silently lost "
    "(and counted as written). Needs an occurrence index in the id; remove this marker once fixed.",
)
def test_two_identical_expenses_on_the_same_day_are_both_kept(store):
    written, *_ = import_rows([row("2026-08-09", "Auto", 50.0), row("2026-08-09", "Auto", 50.0)], store=store)
    assert written == 2
    assert len(store.rows()) == 2


# --------------------------------------------------------------------------- #
# main() (CLI wiring)
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli(monkeypatch, store):
    """Run main() against the in-memory store: no schema creation, no real import target."""
    real_import_rows = pdf_import.import_rows
    monkeypatch.setattr(pdf_import, "create_schema", lambda: None)
    monkeypatch.setattr(
        pdf_import, "import_rows", lambda rows, dry_run=False: real_import_rows(rows, dry_run=dry_run, store=store)
    )

    def run(*argv, parsed=None):
        monkeypatch.setattr(pdf_import, "parse_printable_summary", parsed)
        monkeypatch.setattr(sys, "argv", ["pdf_import.py", *argv])
        main()

    return run


def test_main_dry_run_reports_without_writing(cli, store, caplog):
    caplog.set_level(logging.INFO, logger="pdf_import")
    cli("aug.pdf", "--dry-run", parsed=lambda path: [row("2026-08-02", "Tea", 10.0)])
    assert store.rows() == []
    assert "Would import 1 expense row(s) total." in caplog.text


def test_main_keeps_going_after_an_unparseable_pdf(cli, store, caplog):
    def parsed(path):
        if path == "bad.pdf":
            raise PdfParseError("bad.pdf: not a Splitwise summary")
        return [row("2026-08-02", "Tea", 10.0)]

    caplog.set_level(logging.INFO, logger="pdf_import")
    cli("bad.pdf", "good.pdf", parsed=parsed)
    assert "bad.pdf: not a Splitwise summary" in caplog.text
    assert "Imported 1 expense row(s) total." in caplog.text
    assert [r["description"] for r in store.rows()] == ["Tea"]


def test_main_reports_every_kind_of_follow_up_the_user_has_to_act_on(cli, store, caplog):
    store.add("Groceries - Big Bazaar", date="2026-07-02", category="Groceries", group_id=GROUP)  # fuzzy evidence
    store.add("Rent", date="2026-08-01", cost=1000.0, group_id=GROUP, id=50)  # re-dated -> possible edit
    caplog.set_level(logging.INFO, logger="pdf_import")

    cli(
        "aug.pdf",
        parsed=lambda path: [
            row("2026-08-03", "Rent", 1000.0),
            row("2026-08-04", "Groceries - Big Bazar", 510.0),
            row("2026-08-07", "Brand New Thing", 5.0),
        ],
    )

    text = caplog.text
    assert "were NOT imported" in text and "Rent" in text
    assert "were fuzzy-matched" in text and "Groceries - Big Bazar" in text
    assert "have no historical category match" in text and "Brand New Thing" in text
