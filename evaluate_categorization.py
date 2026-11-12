"""Backtest harness for category_inference.py.

Evaluates the exact+fuzzy category inference pipeline against a date range of
already-categorized expenses, as if those rows didn't exist yet (excluded
from the historical lookup pool), and compares predictions to their actual
(ground-truth) categories.

This performs NO writes to the database - it's a read-only evaluation tool.

Usage:
    python evaluate_categorization.py 2026-09-01 2026-10-31
"""
import sys
from collections import Counter

from category_inference import infer_category
from splitwise_data import get_db_connection


def evaluate(start_date: str, end_date: str, ids: list[int] | None = None):
    conn = get_db_connection()
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, date, description, cost, category
            FROM expenses
            WHERE id IN ({placeholders}) AND deleted_at IS NULL AND category IS NOT NULL
            ORDER BY date
            """,
            ids,
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, date, description, cost, category
            FROM expenses
            WHERE date >= ? AND date <= ? AND deleted_at IS NULL AND category IS NOT NULL
            ORDER BY date
            """,
            (start_date, end_date),
        ).fetchall()

    results = []
    for row in rows:
        predicted, tier, info = infer_category(conn, row["description"], exclude_start=start_date, exclude_end=end_date)
        results.append(
            {
                "date": row["date"],
                "description": row["description"],
                "cost": row["cost"],
                "actual": row["category"],
                "predicted": predicted,
                "tier": tier,
                "info": info,
            }
        )
    conn.close()
    return results


def report(results):
    total = len(results)
    by_tier = Counter(r["tier"] for r in results)
    covered = [r for r in results if r["predicted"] is not None]
    correct = [r for r in covered if r["predicted"] == r["actual"]]
    incorrect = [r for r in covered if r["predicted"] != r["actual"]]

    print(f"Total items evaluated:        {total}")
    print(f"  exact-tier predictions:     {by_tier.get('exact', 0)}")
    print(f"  fuzzy-tier predictions:     {by_tier.get('fuzzy', 0)}")
    print(f"  no prediction (none):       {by_tier.get('none', 0)}")
    print()
    coverage = len(covered) / total if total else 0
    precision = len(correct) / len(covered) if covered else 0
    overall_accuracy = len(correct) / total if total else 0
    print(f"Coverage (made a prediction):            {coverage:.1%}  ({len(covered)}/{total})")
    print(f"Precision (correct, among predictions):  {precision:.1%}  ({len(correct)}/{len(covered)})")
    print(f"Overall accuracy (correct / all items):  {overall_accuracy:.1%}  ({len(correct)}/{total})")
    print()

    for tier in ("exact", "fuzzy"):
        tier_results = [r for r in results if r["tier"] == tier]
        if not tier_results:
            continue
        tier_correct = [r for r in tier_results if r["predicted"] == r["actual"]]
        tier_precision = len(tier_correct) / len(tier_results)
        print(f"  {tier}-tier precision: {tier_precision:.1%}  ({len(tier_correct)}/{len(tier_results)})")

    if incorrect:
        print()
        print(f"Incorrect predictions ({len(incorrect)}):")
        for r in incorrect:
            print(f"  [{r['tier']}] {r['date']}  {r['description']:<35}  predicted={r['predicted']!r:<20} actual={r['actual']!r}")

    none_results = [r for r in results if r["tier"] == "none"]
    if none_results:
        print()
        print(f"No prediction made ({len(none_results)}) - would remain 'uncategorized' same as before:")
        for r in none_results:
            print(f"  {r['date']}  {r['description']:<35}  actual={r['actual']!r}")

    fuzzy_results = [r for r in results if r["tier"] == "fuzzy"]
    if fuzzy_results:
        print()
        print(f"Fuzzy-tier detail ({len(fuzzy_results)}):")
        for r in fuzzy_results:
            mark = "OK" if r["predicted"] == r["actual"] else "WRONG"
            supporters = ", ".join(sorted({m["description"] for m in r["info"]["matches"]}))
            print(f"  [{mark}] {r['date']}  {r['description']:<30} -> {r['predicted']!r} (actual={r['actual']!r})  like: {supporters}")


if __name__ == "__main__":
    start, end = sys.argv[1], sys.argv[2]
    id_args = [int(a) for a in sys.argv[3:]] if len(sys.argv) > 3 else None
    results = evaluate(start, end, ids=id_args)
    report(results)
