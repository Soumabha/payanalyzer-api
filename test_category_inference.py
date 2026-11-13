"""
Unit tests for category_inference.py: the deterministic, non-LLM categorizer used
for imported rows. Tier 1 is a case/whitespace-insensitive exact match (majority
category); tier 2 is a guarded fuzzy match that needs BOTH stemmed-token overlap
and character similarity, a plurality vote among distinct historical
descriptions, and skips short descriptions entirely.

The functions are pure: they take the evidence as a plain list of row dicts, so
these tests need no database and no store - just lists. The similarity numbers
quoted in comments were computed from the module's own helpers, not guessed.

Run with: python -m pytest test_category_inference.py -v
"""

import pytest

from category_inference import (
    FUZZY_CHAR_RATIO_THRESHOLD,
    FUZZY_JACCARD_THRESHOLD,
    FUZZY_MIN_TOKENS,
    _char_ratio,
    _jaccard,
    _stem_set,
    exact_match_category,
    fuzzy_match_category,
    infer_category,
)


def row(description, category, date="2026-07-01"):
    return {"id": None, "date": date, "description": description, "category": category}


# --------------------------------------------------------------------------- #
# Tier 1: exact
# --------------------------------------------------------------------------- #


def test_exact_match_returns_the_category_of_an_identical_description():
    assert exact_match_category([row("Gas Cylinder", "Utilities")], "Gas Cylinder") == "Utilities"


@pytest.mark.parametrize("query", ["gas cylinder", "GAS CYLINDER", "  Gas   cylinder  ", "Gas\tCylinder"])
def test_exact_match_ignores_case_and_whitespace_differences(query):
    assert exact_match_category([row("Gas Cylinder", "Utilities")], query) == "Utilities"


def test_exact_match_sees_through_internal_whitespace_in_the_stored_description():
    # Splitwise API rows are stored raw, so a doubled space can be in the history.
    assert exact_match_category([row("Gas  cylinder", "Utilities")], "Gas cylinder") == "Utilities"


def test_exact_match_folds_non_ascii_case():
    assert exact_match_category([row("CAF\u00c9 Latte", "Food")], "caf\u00e9 latte") == "Food"


def test_exact_match_uses_the_majority_category():
    history = [row("Cab", "Transport")] * 3 + [row("Cab", "Travel")]
    assert exact_match_category(history, "Cab") == "Transport"


def test_exact_match_breaks_a_tie_alphabetically_so_row_order_cannot_change_the_answer():
    forwards = [row("Cab", "Travel"), row("Cab", "Transport")]
    assert exact_match_category(forwards, "Cab") == "Transport"
    assert exact_match_category(list(reversed(forwards)), "Cab") == "Transport"


def test_exact_match_returns_none_for_an_unknown_description_or_empty_history():
    assert exact_match_category([row("Cab", "Transport")], "Rickshaw") is None
    assert exact_match_category([], "Cab") is None


def test_exact_match_honours_the_date_range_exclusion_used_by_backtests():
    history = [row("Cab", "Transport", date="2026-09-10")]
    assert exact_match_category(history, "Cab") == "Transport"
    assert exact_match_category(history, "Cab", exclude_start="2026-09-01", exclude_end="2026-09-30") is None


def test_date_range_exclusion_is_inclusive_at_both_ends_and_keeps_rows_outside_it():
    history = [
        row("Cab", "Before", date="2026-08-31"),
        row("Cab", "Start", date="2026-09-01"),
        row("Cab", "End", date="2026-09-30"),
        row("Cab", "After", date="2026-10-01"),
    ]
    kept = {
        cat
        for cat in ("Before", "Start", "End", "After")
        if exact_match_category(
            [r for r in history if r["category"] == cat], "Cab", exclude_start="2026-09-01", exclude_end="2026-09-30"
        )
    }
    assert kept == {"Before", "After"}


def test_a_row_with_no_date_is_dropped_when_a_date_range_is_being_excluded():
    history = [row("Cab", "Transport", date=None)]
    assert exact_match_category(history, "Cab") == "Transport"
    assert exact_match_category(history, "Cab", exclude_start="2026-09-01", exclude_end="2026-09-30") is None


# --------------------------------------------------------------------------- #
# Tier 2: fuzzy
# --------------------------------------------------------------------------- #


def test_fuzzy_match_accepts_a_typo_that_clears_both_thresholds():
    # jaccard 0.50 (>= 0.5), char ratio 0.98 (>= 0.85)
    category, matches = fuzzy_match_category([row("Groceries - Big Bazaar", "Groceries")], "Groceries - Big Bazar")
    assert category == "Groceries"
    assert [m["description"] for m in matches] == ["Groceries - Big Bazaar"]
    assert matches[0]["jaccard"] >= FUZZY_JACCARD_THRESHOLD
    assert matches[0]["char_ratio"] >= FUZZY_CHAR_RATIO_THRESHOLD


def test_fuzzy_match_rejects_lexically_close_but_semantically_different():
    # "Cab Fare" vs "Cap Fare": char ratio 0.88 passes, but only 1 of 3 distinct
    # stems is shared (jaccard 0.33) - both must pass, so no match.
    assert _char_ratio("Cab Fare", "Cap Fare") >= FUZZY_CHAR_RATIO_THRESHOLD
    assert _jaccard(_stem_set("Cab Fare"), _stem_set("Cap Fare")) < FUZZY_JACCARD_THRESHOLD
    assert fuzzy_match_category([row("Cap Fare", "Clothing")], "Cab Fare") == (None, [])


def test_fuzzy_match_rejects_shared_words_in_a_different_arrangement():
    # jaccard 0.75 passes but char ratio 0.41 does not.
    assert fuzzy_match_category([row("Groceries Delivery Big Bazaar", "Groceries")], "Big Bazaar Groceries") == (None, [])


def test_fuzzy_match_skips_descriptions_shorter_than_the_minimum_token_count():
    assert FUZZY_MIN_TOKENS == 2
    assert fuzzy_match_category([row("Taxis", "Transport")], "Taxi") == (None, [])  # one token: exact-or-nothing


def test_fuzzy_match_skips_the_identical_description_that_tier_one_owns():
    assert fuzzy_match_category([row("Monthly Gym Fee", "Health")], "monthly  gym fee") == (None, [])


def test_fuzzy_match_takes_a_plurality_vote_across_distinct_descriptions():
    history = [
        row("Monthly Gym Membership Fees", "Health"),
        row("Monthly Gym Membership Fee Renewal", "Health"),
        row("Monthly Gym Membership Fee Paid", "Fitness"),
    ]
    category, matches = fuzzy_match_category(history, "Monthly Gym Membership Fee")
    assert category == "Health"
    assert len(matches) == 3


def test_fuzzy_match_refuses_to_guess_on_a_tie_but_still_reports_the_candidates():
    history = [row("Monthly Gym Membership Fees", "Health"), row("Monthly Gym Membership Fee Paid", "Fitness")]
    category, matches = fuzzy_match_category(history, "Monthly Gym Membership Fee")
    assert category is None
    assert len(matches) == 2


def test_fuzzy_corpus_counts_each_distinct_description_once_not_once_per_row():
    # 5 rows of a single description is still one vote, so it can't outvote
    # two *different* descriptions that agree with each other.
    history = (
        [row("Monthly Gym Membership Fees", "Fitness")] * 5
        + [row("Monthly Gym Membership Fee Renewal", "Health"), row("Monthly Gym Membership Fee Paid", "Health")]
    )
    assert fuzzy_match_category(history, "Monthly Gym Membership Fee")[0] == "Health"


def test_fuzzy_corpus_merges_descriptions_that_differ_only_by_whitespace_or_case():
    history = [row("Groceries - Big  Bazaar", "Groceries"), row("groceries - big bazaar", "Groceries")]
    category, matches = fuzzy_match_category(history, "Groceries - Big Bazar")
    assert category == "Groceries"
    assert len(matches) == 1


def test_fuzzy_matches_report_the_whitespace_normalized_description():
    _, matches = fuzzy_match_category([row("Groceries - Big  Bazaar", "Groceries")], "Groceries - Big Bazar")
    assert matches[0]["description"] == "Groceries - Big Bazaar"


def test_fuzzy_match_honours_the_date_range_exclusion():
    history = [row("Groceries - Big Bazaar", "Groceries", date="2026-09-10")]
    assert fuzzy_match_category(history, "Groceries - Big Bazar")[0] == "Groceries"
    assert fuzzy_match_category(history, "Groceries - Big Bazar", "2026-09-01", "2026-09-30") == (None, [])


def test_fuzzy_match_on_empty_history():
    assert fuzzy_match_category([], "Groceries - Big Bazar") == (None, [])


# --------------------------------------------------------------------------- #
# infer_category (tier ordering)
# --------------------------------------------------------------------------- #


def test_infer_category_prefers_exact_over_fuzzy():
    history = [row("Groceries - Big Bazar", "Shopping"), row("Groceries - Big Bazaar", "Groceries")]
    assert infer_category(history, "Groceries - Big Bazar") == ("Shopping", "exact", {})


def test_infer_category_falls_back_to_fuzzy_and_reports_its_evidence():
    category, tier, info = infer_category([row("Groceries - Big Bazaar", "Groceries")], "Groceries - Big Bazar")
    assert (category, tier) == ("Groceries", "fuzzy")
    assert [m["description"] for m in info["matches"]] == ["Groceries - Big Bazaar"]


def test_infer_category_returns_none_when_nothing_matches():
    assert infer_category([row("Cab", "Transport")], "Completely Unrelated Thing") == (None, "none", {})


def test_infer_category_on_an_empty_history():
    assert infer_category([], "Anything At All") == (None, "none", {})


def test_infer_category_does_not_modify_the_history_it_is_given():
    history = [row("Groceries - Big Bazaar", "Groceries"), row("Cab", "Transport")]
    snapshot = [dict(r) for r in history]
    infer_category(history, "Groceries - Big Bazar", "2026-09-01", "2026-09-30")
    assert history == snapshot
