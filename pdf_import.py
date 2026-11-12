"""Import expenses from a Splitwise "printable summary" PDF.

This is a stop-gap data-entry path for months where the live Splitwise API
sync can't run (e.g. no active Splitwise Pro subscription on the registered
app, see main.py's sync_splitwise_expenses). Any group member can download a
printable summary for free from Splitwise for a given month, e.g.:

    https://secure.splitwise.com/api/v3.0/get_printable_summary?group_id=<id>&date=<YYYY-MM>

(or via the "Export as PDF" option in the Splitwise group UI), and this
script parses that PDF and upserts the expenses into the same
splitwise_expenses.db SQLite database used by the FastAPI app.

Usage:
    python pdf_import.py path/to/summary.pdf [more.pdf ...] [--dry-run]

How it avoids double-counting against already API-synced data
---------------------------------------------------------------
A given month's PDF may overlap with dates that were already synced live via
the API before access broke (e.g. the month is only partially missing). To
avoid duplicating those, every parsed PDF row is first matched by content
(group_id, date, normalized description, cost) against existing source='api'
rows for the same group/month; exact matches are assumed to already be
correctly captured and are skipped entirely. Only rows with no exact match
are imported, tagged source='manual_pdf'.

Rows that don't exactly match but share the same (description, cost) as an
existing api row on a *different* date are flagged as "possible edits" and
are NOT imported automatically - this usually means the expense's date was
changed in Splitwise after it was synced (our local copy is stale), and
blindly importing it would create a near-duplicate under a different date.
These are reported so you can fix the stale row by hand, e.g.:

    UPDATE expenses SET date = '<correct date>' WHERE id = <id>;

Other notes / known limitations
--------------------------------
  - The PDF doesn't include expense categories, so categories are inferred
    (see category_inference.py: exact match first, then a guarded fuzzy
    match). Rows with no match are left uncategorized and reported at the
    end so they can be filled in manually (e.g. directly in the DB, or via a
    future UI).
  - Re-importing a month (e.g. a corrected/re-downloaded PDF) purges and
    replaces that month's manual_pdf rows, which would otherwise silently
    wipe out any categories you'd manually corrected since the last import.
    To prevent that, each purged row's (date, description, cost) -> category
    is captured in memory first; if a freshly re-parsed row has the same key
    but neither the exact nor fuzzy tier can categorize it, its previous
    category is carried forward rather than reverting to uncategorized.
  - Only the group-overview section (the page(s) before the first
    per-member breakdown) is parsed, since it has one row per expense with
    the *total* cost of the expense - matching what the live API sync
    stores (per-person paid/owed splits are not captured, consistent with
    the existing schema).
  - Rows imported this way are tagged source='manual_pdf' (vs 'api' for rows
    from the live Splitwise sync) and are keyed by a deterministic id derived
    from (group_id, date, description, cost), so a given expense's id is
    always negative and can never collide with a real (positive) Splitwise
    expense id. Re-running the import on the same (or a corrected,
    re-downloaded) PDF is safe: for every group_id/year-month found in the
    PDF, existing manual_pdf rows for that group_id/year-month are purged
    and replaced with what's currently in the PDF.
  - When the live Splitwise sync resumes for a month that was previously
    manually imported, splitwise_data.upsert_expense() removes any
    manual_pdf row matching the newly-synced api row's content, so
    duplicates are cleaned up automatically going forward.
"""
import argparse
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from pypdf import PdfReader

from category_inference import infer_category
from splitwise_data import create_schema, get_db_connection, make_manual_id, normalize_description

logger = logging.getLogger("pdf_import")

MONTH_ALTERNATION = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"

# Matches one expense row from the printable summary's group-overview
# section, e.g. "Aug 02 Medicine and glucometer ₹ 3513.00 ₹ 0.00 ₹ 0.00".
# Only the first ₹ amount (the expense's total cost) is captured; any
# trailing per-member settled-up amounts are ignored. Applied against text
# with all whitespace (including page-wrapped newlines) collapsed to single
# spaces, since pypdf often splits the month, day, and multi-word
# descriptions of a single row across several lines when the table is wide
# or the month has many rows.
ROW_RE = re.compile(r"\b(" + MONTH_ALTERNATION + r")\b\s+(\d{1,2})\b\s+(.+?)\s*₹\s*([\d,]+\.\d{2})")

# Pulled from the footer URL present on every page, e.g.:
#   ".../get_printable_summary?group_id=25397011&date=2026-08"
GROUP_AND_MONTH_RE = re.compile(r"group_id=(\d+)&date=(\d{4})-(\d{2})")


class PdfParseError(ValueError):
    pass


def _collect_group_overview_text(reader: PdfReader) -> str:
    """Concatenate the group-overview pages (page 1 plus any continuation
    pages) and stop once the closing "TOTAL BALANCE as of ..." summary is
    seen, so we never wander into the per-member breakdown sections that
    follow (which would repeat every expense once per member)."""
    chunks = []
    for page in reader.pages:
        text = page.extract_text() or ""
        chunks.append(text)
        if "TOTAL BALANCE as of" in text:
            break
    return re.sub(r"\s+", " ", " ".join(chunks))


def parse_printable_summary(path: str) -> list[dict]:
    """Parse the group-overview section of a Splitwise printable summary PDF.

    Returns a list of dicts: {date, description, cost, group_id}
    """
    reader = PdfReader(path)
    if not reader.pages:
        raise PdfParseError(f"{path}: PDF has no pages")
    text = _collect_group_overview_text(reader)

    m = GROUP_AND_MONTH_RE.search(text)
    if not m:
        raise PdfParseError(
            f"{path}: couldn't find the 'group_id=...&date=YYYY-MM' footer URL; "
            "is this a Splitwise printable summary PDF?"
        )
    group_id = int(m.group(1))
    year, month = m.group(2), m.group(3)

    rows = []
    for _mon_abbr, day, description, cost_str in ROW_RE.findall(text):
        rows.append(
            {
                "date": f"{year}-{month}-{int(day):02d}",
                "description": normalize_description(description),
                "cost": float(cost_str.replace(",", "")),
                "group_id": group_id,
            }
        )

    if not rows:
        logger.warning(f"{path}: no expense rows found on the overview page (is the month empty?)")

    return rows


def import_rows(rows: list[dict], dry_run: bool = False) -> tuple[int, list[dict], list[dict], list[dict], list[dict]]:
    """Upsert parsed rows into the DB, grouped by (group_id, year-month).

    Returns (written_count, fuzzy_categorized_rows, preserved_categorized_rows, uncategorized_rows, possible_edit_rows).
    """
    if not rows:
        return 0, [], [], [], []

    conn = get_db_connection()
    uncategorized = []
    fuzzy_categorized = []
    preserved_categorized = []
    possible_edits = []
    to_insert = []
    previous_manual_categories = {}
    now = datetime.now(timezone.utc).isoformat()

    periods = {(row["group_id"], row["date"][:7]) for row in rows}
    for group_id, year_month in periods:
        api_rows = conn.execute(
            """
            SELECT date, description, cost FROM expenses
            WHERE source='api' AND group_id=? AND strftime('%Y-%m', date)=? AND deleted_at IS NULL
            """,
            (group_id, year_month),
        ).fetchall()
        # Exact-match counter: (date, normalized description, cost) -> count.
        # Rows found here are assumed already correctly captured via the API.
        exact_counter = Counter(
            (row["date"], normalize_description(row["description"] or ""), round(row["cost"], 2))
            for row in api_rows
        )
        # Fuzzy index ignoring date, to spot same expense re-dated in Splitwise
        # after it was synced (so an exact match will never be found for it).
        fuzzy_dates_by_desc_cost = defaultdict(list)
        for row in api_rows:
            key = (normalize_description(row["description"] or ""), round(row["cost"], 2))
            fuzzy_dates_by_desc_cost[key].append(row["date"])

        # Snapshot any already-categorized manual_pdf rows for this period
        # before purging them, so a manual category correction made after a
        # previous import isn't silently lost on re-import (see module
        # docstring). Done unconditionally (even in dry-run) so the preview
        # reflects what a real run would do.
        existing_manual_rows = conn.execute(
            """
            SELECT date, description, cost, category FROM expenses
            WHERE source='manual_pdf' AND group_id=? AND strftime('%Y-%m', date)=? AND category IS NOT NULL
            """,
            (group_id, year_month),
        ).fetchall()
        for r in existing_manual_rows:
            key = (group_id, r["date"], normalize_description(r["description"] or "").lower(), round(r["cost"], 2))
            previous_manual_categories[key] = r["category"]

        if dry_run:
            existing_manual = conn.execute(
                "SELECT COUNT(*) as n FROM expenses WHERE source='manual_pdf' AND group_id=? "
                "AND strftime('%Y-%m', date)=?",
                (group_id, year_month),
            ).fetchone()["n"]
            if existing_manual:
                logger.info(
                    f"[dry-run] would purge {existing_manual} existing manual_pdf row(s) for "
                    f"group {group_id}, {year_month}"
                )
        else:
            deleted = conn.execute(
                "DELETE FROM expenses WHERE source='manual_pdf' AND group_id=? AND strftime('%Y-%m', date)=?",
                (group_id, year_month),
            ).rowcount
            if deleted:
                logger.info(f"Purged {deleted} previous manual_pdf row(s) for group {group_id}, {year_month}")

        period_rows = [row for row in rows if row["group_id"] == group_id and row["date"][:7] == year_month]
        for row in period_rows:
            exact_key = (row["date"], row["description"], round(row["cost"], 2))
            if exact_counter.get(exact_key, 0) > 0:
                exact_counter[exact_key] -= 1
                continue  # already synced via the live API - skip, avoids a duplicate

            fuzzy_key = (row["description"], round(row["cost"], 2))
            other_dates = [d for d in fuzzy_dates_by_desc_cost.get(fuzzy_key, []) if d != row["date"]]
            if other_dates:
                # Same description+cost already exists under a different date -
                # most likely this expense's date was edited in Splitwise after
                # sync broke. Don't auto-import (would likely double count);
                # flag it for manual reconciliation instead.
                possible_edits.append({**row, "existing_api_dates": other_dates})
                continue

            to_insert.append(row)

    written = 0
    for row in to_insert:
        category, tier, info = infer_category(conn, row["description"])
        if category is None:
            preserve_key = (
                row["group_id"],
                row["date"],
                normalize_description(row["description"]).lower(),
                round(row["cost"], 2),
            )
            preserved_category = previous_manual_categories.get(preserve_key)
            if preserved_category is not None:
                category = preserved_category
                preserved_categorized.append({**row, "category": category})
            else:
                uncategorized.append(row)
        elif tier == "fuzzy":
            fuzzy_categorized.append({**row, "category": category, "matches": info["matches"]})
        if dry_run:
            written += 1
            continue
        manual_id = make_manual_id(row["group_id"], row["date"], row["description"], row["cost"])
        conn.execute(
            """
            INSERT INTO expenses (id, date, description, cost, category, group_id, updated_at, deleted_at, source)
            VALUES (:id, :date, :description, :cost, :category, :group_id, :updated_at, NULL, 'manual_pdf')
            ON CONFLICT(id) DO UPDATE SET
                date=excluded.date,
                description=excluded.description,
                cost=excluded.cost,
                category=excluded.category,
                group_id=excluded.group_id,
                updated_at=excluded.updated_at,
                source='manual_pdf'
            """,
            {
                "id": manual_id,
                "date": row["date"],
                "description": row["description"],
                "cost": row["cost"],
                "category": category,
                "group_id": row["group_id"],
                "updated_at": now,
            },
        )
        written += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return written, fuzzy_categorized, preserved_categorized, uncategorized, possible_edits


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Import expenses from one or more Splitwise printable-summary PDFs into the local DB."
    )
    parser.add_argument("pdf_paths", nargs="+", help="Path(s) to printable summary PDF file(s)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing to the database")
    args = parser.parse_args()

    create_schema()

    total_written = 0
    all_uncategorized: list[dict] = []
    all_fuzzy_categorized: list[dict] = []
    all_preserved_categorized: list[dict] = []
    all_possible_edits: list[dict] = []
    for path in args.pdf_paths:
        try:
            rows = parse_printable_summary(path)
        except PdfParseError as e:
            logger.error(str(e))
            continue
        logger.info(f"{path}: parsed {len(rows)} expense row(s)")
        written, fuzzy_categorized, preserved_categorized, uncategorized, possible_edits = import_rows(
            rows, dry_run=args.dry_run
        )
        total_written += written
        all_uncategorized.extend(uncategorized)
        all_fuzzy_categorized.extend(fuzzy_categorized)
        all_preserved_categorized.extend(preserved_categorized)
        all_possible_edits.extend(possible_edits)

    verb = "Would import" if args.dry_run else "Imported"
    logger.info(f"{verb} {total_written} expense row(s) total.")

    if all_possible_edits:
        logger.warning(
            f"{len(all_possible_edits)} row(s) were NOT imported because they look like an "
            "already-synced expense whose date changed in Splitwise (same description+cost, "
            "different date already in the DB). Fix the stale row's date, e.g.:"
        )
        for row in all_possible_edits:
            existing = ", ".join(row["existing_api_dates"])
            logger.warning(
                f"  PDF says {row['date']}  {row['description']:<30}  Rs. {row['cost']:.2f}"
                f"   (existing synced row currently dated: {existing})"
            )

    if all_fuzzy_categorized:
        logger.warning(
            f"{len(all_fuzzy_categorized)} row(s) were fuzzy-matched (no exact historical description, "
            "inferred from similarly-worded entries) - please verify:"
        )
        for row in all_fuzzy_categorized:
            supporters = ', '.join(sorted({m['description'] for m in row['matches']}))
            logger.warning(
                f"  {row['date']}  {row['description']:<30}  Rs. {row['cost']:.2f}  -> {row['category']}"
                f"   (like: {supporters})"
            )

    if all_preserved_categorized:
        logger.info(
            f"{len(all_preserved_categorized)} row(s) kept their previously-set category from before this "
            "re-import (neither exact nor fuzzy matching found a source for them):"
        )
        for row in all_preserved_categorized:
            logger.info(f"  {row['date']}  {row['description']:<30}  Rs. {row['cost']:.2f}  -> {row['category']}")

    if all_uncategorized:
        logger.warning(f"{len(all_uncategorized)} row(s) have no historical category match - set manually:")
        for row in all_uncategorized:
            logger.warning(f"  {row['date']}  {row['description']:<30}  Rs. {row['cost']:.2f}")


if __name__ == "__main__":
    main()
