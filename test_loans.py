"""Pure, DB-free tests for loans.py (monthly amortization, prepayments,
rate/EMI changes, re-basing on observed balances)."""

import pytest

from loans import (
    amortize,
    build_timeline,
    emi_from_tenure,
    forward_schedule,
    monthly_cash_outflow,
    preview_prepayment,
    summarize_schedule,
    summarize_year,
    tenure_from_emi,
    terms_for_month,
)

RATE = 9.0
PRINCIPAL = 50.0
MONTHS = 120


def terms(rate=RATE, emi=None, effective_from="2026-01-01", **extra):
    emi = emi if emi is not None else emi_from_tenure(PRINCIPAL, RATE, MONTHS)
    return {"effective_from": effective_from, "annual_rate_pct": rate, "emi_lakhs": emi, **extra}


def obs(amount, as_of="2026-01-31"):
    return {"as_of": as_of, "amount_lakhs": amount}


# -- closed-form checks -------------------------------------------------------


def test_emi_matches_the_known_annuity_value():
    # Textbook: 1 lakh at 12% p.a. over 12 months -> Rs 8,884.88 a month.
    assert emi_from_tenure(1.0, 12.0, 12) == pytest.approx(0.0888488, abs=1e-6)


def test_zero_rate_emi_is_straight_division():
    assert emi_from_tenure(12.0, 0.0, 24) == pytest.approx(0.5)


def test_schedule_follows_the_closed_form_balance_for_every_month():
    emi = emi_from_tenure(PRINCIPAL, RATE, MONTHS)
    schedule = amortize(PRINCIPAL, "2026-01", [terms(emi=emi)], [])
    assert len(schedule) == MONTHS
    i = RATE / 1200
    for k, row in enumerate(schedule, start=1):
        expected = PRINCIPAL * (1 + i) ** k - emi * ((1 + i) ** k - 1) / i
        assert row["closing"] == pytest.approx(max(expected, 0.0), abs=1e-2), k
    assert schedule[0]["month"] == "2026-02" and schedule[-1]["month"] == "2036-01"
    assert schedule[-1]["closing"] == 0.0


def test_total_interest_equals_total_paid_minus_principal():
    emi = emi_from_tenure(PRINCIPAL, RATE, MONTHS)
    schedule = amortize(PRINCIPAL, "2026-01", [terms(emi=emi)], [])
    assert sum(r["principal"] for r in schedule) == pytest.approx(PRINCIPAL, abs=1e-2)
    assert sum(r["interest"] for r in schedule) == pytest.approx(emi * MONTHS - PRINCIPAL, abs=1e-2)


def test_each_emi_splits_into_interest_then_principal():
    first = amortize(PRINCIPAL, "2026-01", [terms()], [])[0]
    assert first["interest"] == pytest.approx(PRINCIPAL * RATE / 1200)  # 0.375
    assert first["principal"] == pytest.approx(first["emi_paid"] - first["interest"], abs=1e-4)
    assert first["closing"] == pytest.approx(PRINCIPAL - first["principal"], abs=1e-4)


def test_interest_share_falls_and_principal_share_rises_over_time():
    schedule = amortize(PRINCIPAL, "2026-01", [terms()], [])
    assert schedule[0]["interest"] > schedule[60]["interest"] > schedule[-2]["interest"]
    assert schedule[0]["principal"] < schedule[60]["principal"] < schedule[-2]["principal"]


def test_the_final_payment_clears_exactly_the_remaining_balance_plus_interest():
    schedule = amortize(PRINCIPAL, "2026-01", [terms()], [])
    last = schedule[-1]
    assert last["closing"] == 0.0
    assert last["emi_paid"] == pytest.approx(last["opening"] + last["interest"], abs=1e-3)
    assert last["emi_paid"] <= terms()["emi_lakhs"] + 1e-3


def test_tenure_from_emi_round_trips_and_detects_a_never_ending_loan():
    emi = emi_from_tenure(PRINCIPAL, RATE, MONTHS)
    assert tenure_from_emi(PRINCIPAL, RATE, emi) == MONTHS
    assert tenure_from_emi(PRINCIPAL, RATE, PRINCIPAL * RATE / 1200) is None


# -- prepayments and extra EMI ------------------------------------------------


def test_a_prepayment_shortens_the_tenure_and_leaves_the_emi_unchanged():
    base = amortize(PRINCIPAL, "2026-01", [terms()], [])
    prepaid = amortize(PRINCIPAL, "2026-01", [terms()], [{"date": "2027-03-10", "amount_lakhs": 5.0}])
    assert len(prepaid) < len(base)
    assert {r["emi_scheduled"] for r in prepaid} == {terms()["emi_lakhs"]}
    row = next(r for r in prepaid if r["month"] == "2027-03")
    assert row["prepayment"] == 5.0
    assert row["closing"] == pytest.approx(row["opening"] - row["principal"] - 5.0, abs=1e-4)
    assert sum(r["interest"] for r in prepaid) < sum(r["interest"] for r in base)


def test_a_prepayment_larger_than_the_balance_is_capped_and_clears_the_loan():
    schedule = amortize(10.0, "2026-01", [terms(emi=1.0)], [{"date": "2026-02-01", "amount_lakhs": 99.0}])
    assert len(schedule) == 1
    assert schedule[0]["closing"] == 0.0
    assert schedule[0]["prepayment"] < 99.0


def test_prepayments_in_or_before_the_opening_month_are_not_applied_again():
    plain = amortize(PRINCIPAL, "2026-01", [terms()], [])
    ignored = amortize(PRINCIPAL, "2026-01", [terms()], [{"date": "2026-01-20", "amount_lakhs": 5.0}])
    assert ignored == plain


def test_paying_more_than_the_scheduled_emi_puts_the_excess_on_principal():
    base = amortize(PRINCIPAL, "2026-01", [terms()], [])
    bigger = amortize(PRINCIPAL, "2026-01", [terms(emi=terms()["emi_lakhs"] + 0.5)], [])
    assert len(bigger) < len(base)
    first = bigger[0]
    assert first["interest"] == base[0]["interest"]
    assert first["principal"] == pytest.approx(base[0]["principal"] + 0.5, abs=1e-4)


def test_preview_reports_months_and_interest_saved():
    preview = preview_prepayment(
        [obs(PRINCIPAL, "2026-01-31")], [terms()], [], {"date": "2027-01-15", "amount_lakhs": 5.0}
    )
    assert preview["payoff_month_before"] == "2036-01"
    assert preview["months_saved"] > 0
    assert preview["payoff_month_after"] < preview["payoff_month_before"]
    assert preview["interest_saved"] > 0


# -- terms changes ------------------------------------------------------------


def test_rate_change_applies_from_its_month_and_keeps_the_emi():
    changed = [terms(), terms(rate=11.0, effective_from="2027-01-15", id=2)]
    schedule = amortize(PRINCIPAL, "2026-01", changed, [])
    by_month = {r["month"]: r for r in schedule}
    assert by_month["2026-12"]["annual_rate_pct"] == 9.0
    assert by_month["2027-01"]["annual_rate_pct"] == 11.0
    assert by_month["2027-01"]["interest"] == pytest.approx(by_month["2027-01"]["opening"] * 11 / 1200, abs=1e-4)
    assert len(schedule) > MONTHS  # higher rate, same EMI -> longer


def test_emi_change_applies_from_its_month():
    changed = [terms(), terms(emi=terms()["emi_lakhs"] + 1.0, effective_from="2028-06-01", id=2)]
    by_month = {r["month"]: r for r in amortize(PRINCIPAL, "2026-01", changed, [])}
    assert by_month["2028-05"]["emi_scheduled"] == terms()["emi_lakhs"]
    assert by_month["2028-06"]["emi_scheduled"] == terms()["emi_lakhs"] + 1.0


def test_terms_effective_in_the_same_month_resolve_to_the_later_record():
    both = [terms(rate=9.0, id=1), terms(rate=8.0, id=2)]
    assert terms_for_month(both, "2026-05")["annual_rate_pct"] == 8.0


def test_months_before_any_terms_use_the_earliest_terms():
    assert terms_for_month([terms(rate=7.0, effective_from="2027-01-01")], "2026-03")["annual_rate_pct"] == 7.0


# -- observations (re-basing) -------------------------------------------------


def test_timeline_marks_months_before_the_first_observation_as_estimated():
    timeline = build_timeline([obs(PRINCIPAL, "2026-03-31")], [terms()], [], 2026, 2026)
    by_month = {r["month"]: r for r in timeline}
    assert [by_month[m]["estimated"] for m in ("2026-01", "2026-02", "2026-03", "2026-04")] == [True, True, True, False]
    assert by_month["2026-01"]["emi_paid"] == terms()["emi_lakhs"]
    assert by_month["2026-01"]["closing"] is None
    assert by_month["2026-03"]["closing"] == PRINCIPAL
    assert by_month["2026-04"]["interest"] > 0


def test_a_later_observation_rebases_the_schedule_and_reports_drift():
    schedule = amortize(PRINCIPAL, "2026-01", [terms()], [])
    computed = {r["month"]: r["closing"] for r in schedule}["2026-07"]
    observed = computed - 2.0  # the bank says 2 lakhs less is owed (an unrecorded prepayment)
    timeline = build_timeline(
        [obs(PRINCIPAL, "2026-01-31"), obs(observed, "2026-07-31")], [terms()], [], 2026, 2027
    )
    by_month = {r["month"]: r for r in timeline}
    assert by_month["2026-07"]["closing"] == pytest.approx(observed, abs=1e-4)
    assert by_month["2026-07"]["drift"] == pytest.approx(2.0, abs=1e-3)
    assert by_month["2026-07"]["rebased"] is True
    assert by_month["2026-08"]["opening"] == pytest.approx(observed, abs=1e-4)
    assert by_month["2026-06"]["drift"] is None


def test_observed_balance_equal_to_the_computed_one_shows_no_drift():
    schedule = amortize(PRINCIPAL, "2026-01", [terms()], [])
    computed = {r["month"]: r["closing"] for r in schedule}["2026-07"]
    timeline = build_timeline([obs(PRINCIPAL, "2026-01-31"), obs(computed, "2026-07-31")], [terms()], [], 2026, 2026)
    assert {r["month"]: r for r in timeline}["2026-07"]["drift"] == pytest.approx(0.0, abs=1e-3)


def test_when_two_observations_share_a_month_the_latest_dated_wins():
    timeline = build_timeline([obs(40.0, "2026-03-05"), obs(39.0, "2026-03-28")], [terms()], [], 2026, 2026)
    assert {r["month"]: r for r in timeline}["2026-03"]["closing"] == 39.0


def test_no_observations_means_no_timeline():
    assert build_timeline([], [terms()], [], 2026, 2030) == []


def test_timeline_is_cut_to_the_requested_window():
    timeline = build_timeline([obs(PRINCIPAL, "2026-01-31")], [terms()], [], 2027, 2027)
    assert {r["month"][:4] for r in timeline} == {"2027"}
    assert len(timeline) == 12


# -- annual summaries -----------------------------------------------------------


def test_year_summary_adds_up_the_months_and_takes_the_year_end_balance():
    timeline = build_timeline([obs(PRINCIPAL, "2025-12-31")], [terms()], [], 2026, 2036)
    year = summarize_year(timeline, 2026)
    months = [r for r in timeline if r["month"].startswith("2026")]
    assert year["emi"] == pytest.approx(sum(r["emi_paid"] for r in months))
    assert year["interest"] + year["principal"] == pytest.approx(year["emi"], abs=1e-3)
    assert year["closing"] == months[-1]["closing"]
    assert year["estimated_months"] == 0


def test_years_after_payoff_are_zero():
    timeline = build_timeline([obs(5.0, "2026-01-31")], [terms(emi=1.0)], [], 2026, 2030)
    after = summarize_year(timeline, 2030)
    assert (after["emi"], after["interest"], after["principal"], after["closing"]) == (0.0, 0.0, 0.0, 0.0)


def test_year_summary_counts_prepayments_as_cash_out_but_not_as_emi():
    timeline = build_timeline(
        [obs(PRINCIPAL, "2025-12-31")], [terms()], [{"date": "2026-04-02", "amount_lakhs": 3.0}], 2026, 2026
    )
    year = summarize_year(timeline, 2026)
    assert year["prepayment"] == 3.0
    assert sum(monthly_cash_outflow(timeline, 2026)) == pytest.approx(year["emi"] + 3.0, abs=1e-3)


def test_prepayments_before_the_first_observation_are_still_cash_outflows():
    timeline = build_timeline(
        [obs(PRINCIPAL, "2026-06-30")], [terms()], [{"date": "2026-02-10", "amount_lakhs": 2.0}], 2026, 2026
    )
    assert {r["month"]: r for r in timeline}["2026-02"]["prepayment"] == 2.0


def test_schedule_summary_reports_balance_payoff_and_remaining_interest():
    summary = summarize_schedule([obs(PRINCIPAL, "2026-01-31")], [terms()], [], "2026-06")
    schedule = forward_schedule([obs(PRINCIPAL, "2026-01-31")], [terms()], [])
    assert summary["payoff_month"] == "2036-01"
    assert summary["months_remaining"] == len([r for r in schedule if r["month"] > "2026-06"])
    assert summary["current_balance"] == [r for r in schedule if r["month"] == "2026-06"][0]["closing"]
    assert summary["total_interest_remaining"] == pytest.approx(sum(r["interest"] for r in schedule), abs=1e-3)


# -- prepayments around an observation's own date -------------------------------------
# Regression: a prepayment made the same day the balance was recorded (the
# natural thing to do right after setting a loan up) used to be ignored.


def test_a_prepayment_on_the_observation_date_is_deducted_from_the_observed_balance():
    observations = [obs(PRINCIPAL, "2026-11-14")]
    prepay = [{"date": "2026-11-14", "amount_lakhs": 5.0}]
    plain = forward_schedule(observations, [terms()], [])
    paid = forward_schedule(observations, [terms()], prepay)
    assert len(paid) < len(plain)
    assert paid[0]["opening"] == pytest.approx(PRINCIPAL - 5.0, abs=1e-4)
    assert paid[0]["interest"] == pytest.approx((PRINCIPAL - 5.0) * RATE / 1200, abs=1e-4)


def test_a_prepayment_earlier_in_the_month_than_the_observation_is_already_in_the_balance():
    observations = [obs(PRINCIPAL, "2026-11-14")]
    plain = forward_schedule(observations, [terms()], [])
    earlier = forward_schedule(observations, [terms()], [{"date": "2026-11-05", "amount_lakhs": 5.0}])
    assert earlier == plain


def test_a_prepayment_after_the_observation_date_in_a_later_month_is_unchanged():
    observations = [obs(PRINCIPAL, "2026-11-14")]
    schedule = forward_schedule(observations, [terms()], [{"date": "2027-01-10", "amount_lakhs": 5.0}])
    assert {r["month"]: r for r in schedule}["2027-01"]["prepayment"] == 5.0


def test_preview_and_summary_see_a_same_day_prepayment():
    observations = [obs(PRINCIPAL, "2026-11-14")]
    preview = preview_prepayment(observations, [terms()], [], {"date": "2026-11-14", "amount_lakhs": 5.0})
    assert preview["months_saved"] > 0 and preview["interest_saved"] > 0
    summary = summarize_schedule(observations, [terms()], [{"date": "2026-11-14", "amount_lakhs": 5.0}], "2026-11")
    assert summary["current_balance"] == pytest.approx(PRINCIPAL - 5.0, abs=1e-4)
    assert summary["total_prepaid_remaining"] == 5.0


def test_timeline_month_of_the_observation_shows_the_balance_after_a_same_day_prepayment():
    timeline = build_timeline(
        [obs(PRINCIPAL, "2026-11-14")], [terms()], [{"date": "2026-11-14", "amount_lakhs": 5.0}], 2026, 2027
    )
    november = {r["month"]: r for r in timeline}["2026-11"]
    assert november["closing"] == pytest.approx(PRINCIPAL - 5.0, abs=1e-4)
    assert november["prepayment"] == 5.0  # still a cash outflow that month


def test_drift_compares_like_with_like_when_the_next_statement_month_has_a_late_prepayment():
    schedule = amortize(PRINCIPAL, "2026-01", [terms()], [])
    computed = {r["month"]: r["closing"] for r in schedule}["2026-07"]
    prepay = [{"date": "2026-07-20", "amount_lakhs": 2.0}]
    # Statement taken 1 July: it does not yet include the 20 July prepayment.
    timeline = build_timeline(
        [obs(PRINCIPAL, "2026-01-31"), obs(computed, "2026-07-01")], [terms()], prepay, 2026, 2026
    )
    july = {r["month"]: r for r in timeline}["2026-07"]
    assert july["drift"] == pytest.approx(0.0, abs=1e-3)
    assert july["closing"] == pytest.approx(computed - 2.0, abs=1e-3)
