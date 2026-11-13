"""Deterministic (non-LLM) category inference for manually-imported expenses.

Two tiers, tried in order:

  1. Exact match: case/whitespace-insensitive comparison against the most
     common historical category for an identical description (e.g. "Gas
     Cylinder" vs "Gas cylinder" - same real-world thing, different casing).

  2. Fuzzy match: for descriptions with no exact historical match, compare
     against every distinct historical description using two independent
     similarity measures that must BOTH clear a threshold:

       - Stemmed-token overlap (Jaccard similarity), using nltk's
         PorterStemmer (already a dependency, used elsewhere in main.py for
         the same purpose - merging similar recurring-expense descriptions).
         This normalizes word *forms* (plurals, tenses) but does nothing for
         misspellings.
       - Character-level similarity (difflib.SequenceMatcher ratio), which
         catches typos/near-identical spellings but is unreliable on short
         strings, where a couple of edits can make two unrelated words look
         deceptively similar (e.g. "Cab" vs "Cap").

     Guardrails against bad fuzzy merges:
       - Descriptions shorter than FUZZY_MIN_TOKENS words skip fuzzy
         matching entirely (exact match or nothing) - this is exactly where
         character-ratio false positives are most likely and typos are
         least likely to matter.
       - Both thresholds must pass simultaneously; a match that's lexically
         close but shares no stemmed words (or vice versa) is rejected.
       - Multiple distinct historical descriptions must agree on the same
         category (plurality vote, not nearest-neighbor) before a fuzzy
         result is trusted; a tie yields no result.

What this deliberately will NOT catch: two historical descriptions that
belong to the same real category but share no common words (e.g. "Massage"
and "Saloon - Waxing" are both personal-care expenses, but there's no lexical
overlap for stemming/edit-distance to find). Recognizing that kind of
semantic relationship needs real-world/semantic knowledge (embeddings or an
LLM), which is intentionally out of scope here - such cases correctly fall
through to "no match" rather than risk a wrong guess.
"""
import re
from collections import Counter
from difflib import SequenceMatcher

from nltk.stem import PorterStemmer

from splitwise_data import description_key, normalize_description

_stemmer = PorterStemmer()

FUZZY_MIN_TOKENS = 2
FUZZY_JACCARD_THRESHOLD = 0.5
FUZZY_CHAR_RATIO_THRESHOLD = 0.85


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z]+", (text or "").lower())


def _stem_set(text: str) -> frozenset:
    return frozenset(_stemmer.stem(w) for w in _tokens(text))


def _jaccard(a: frozenset, b: frozenset) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _char_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _evidence(history, exclude_start=None, exclude_end=None):
    """The rows inference may learn from.

    `history` is a list of plain row dicts with at least date, description and
    category - already limited to live, categorized rows by whoever supplied it
    (see SqliteExpenseStore.categorized_history). exclude_start/exclude_end
    (both 'YYYY-MM-DD') drop a date range from it, for backtesting: "what would
    this predict if these rows didn't exist yet". A row with no date can't be
    shown to fall outside the range, so it is dropped too.
    """
    if not (exclude_start and exclude_end):
        return history
    return [
        r for r in history
        if r.get("date") is not None and (r["date"] < exclude_start or r["date"] > exclude_end)
    ]


def _majority(votes: Counter):
    """Most-voted category; ties go to the alphabetically first, so the answer
    never depends on the order rows happen to arrive in."""
    return min(votes.items(), key=lambda kv: (-kv[1], kv[0]))


def exact_match_category(history, description, exclude_start=None, exclude_end=None):
    """Tier 1: case/whitespace-insensitive exact match, majority category."""
    key = description_key(description)
    votes = Counter(
        r["category"] for r in _evidence(history, exclude_start, exclude_end)
        if description_key(r["description"]) == key
    )
    return _majority(votes)[0] if votes else None


def _distinct_historical_descriptions(history, exclude_start=None, exclude_end=None):
    """One entry per distinct (case/whitespace-normalized) historical
    description, paired with its own dominant category. Used as the fuzzy
    comparison corpus, so a single very-frequently-repeated description
    counts as one data point, not one vote per row. Descriptions are returned
    whitespace-normalized, the same form every other comparison uses."""
    by_key = {}
    for r in _evidence(history, exclude_start, exclude_end):
        key = description_key(r["description"])
        if not key:
            continue
        entry = by_key.setdefault(key, (normalize_description(r["description"]), Counter()))
        entry[1][r["category"]] += 1
    corpus = []
    for text, votes in by_key.values():
        category, n = _majority(votes)
        corpus.append((text, category, n))
    return corpus


def fuzzy_match_category(history, description, exclude_start=None, exclude_end=None):
    """Tier 2: fuzzy match against similarly-worded historical descriptions.

    Returns (category_or_None, supporting_matches); supporting_matches lists
    every historical description that passed both thresholds, for auditing.
    """
    tokens = _tokens(description)
    if len(tokens) < FUZZY_MIN_TOKENS:
        return None, []

    norm_target = description_key(description)
    target_text = normalize_description(description)
    target_stems = _stem_set(description)
    corpus = _distinct_historical_descriptions(history, exclude_start, exclude_end)

    matches = []
    for hist_desc, hist_category, _n in corpus:
        if description_key(hist_desc) == norm_target:
            continue  # identical string is Tier 1's job, not fuzzy's
        jaccard = _jaccard(target_stems, _stem_set(hist_desc))
        ratio = _char_ratio(target_text, hist_desc)
        if jaccard >= FUZZY_JACCARD_THRESHOLD and ratio >= FUZZY_CHAR_RATIO_THRESHOLD:
            matches.append(
                {"description": hist_desc, "category": hist_category, "jaccard": jaccard, "char_ratio": ratio}
            )

    if not matches:
        return None, []

    votes = Counter(m["category"] for m in matches)
    ranked = votes.most_common()
    top_category, top_count = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == top_count:
        return None, matches  # tie between categories - too ambiguous to trust
    return top_category, matches


def infer_category(history, description, exclude_start=None, exclude_end=None):
    """Layered category inference. Returns (category_or_None, tier, info).

    tier is one of "exact", "fuzzy", "none". `history` is the evidence (see
    _evidence); exclude_start/exclude_end drop a date range from it, for
    backtesting against data that's already in the DB.
    """
    exact = exact_match_category(history, description, exclude_start, exclude_end)
    if exact is not None:
        return exact, "exact", {}

    fuzzy_category, matches = fuzzy_match_category(history, description, exclude_start, exclude_end)
    if fuzzy_category is not None:
        return fuzzy_category, "fuzzy", {"matches": matches}

    return None, "none", {}
