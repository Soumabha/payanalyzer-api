# Phase 1 — Projection Engine

Status: inputs and formulas confirmed, including the loan parameter
(`loan_end_year`) and the Kids Expense structure (now symmetric with how
Parents Expense is modeled — see §2). Scope includes the static form/UI
needed for you to key in values and validate against the real spreadsheet
directly.

---

## 1. Design change from v1: DB-backed, editable, not just config

Per your feedback, this is **not** a static config file. It's stored in the
database, editable via API (and a form in the existing `static/` frontend),
and recomputed on demand — so changing an assumption and re-fetching the
projection reflects it immediately. The schema is also shaped so that
Phase 6 (wiring in live broker/bank/real-estate data) can override
individual fields with computed values later without a schema rewrite —
each "seed" field here is a stand-in for what will eventually be a live
lookup.

### `projection_assumptions` (single row for now — one scenario)

| Field | Type | Meaning |
|---|---|---|
| `current_annual_expense_lakhs` | REAL | Seed for Base Expense (C), year 2026 |
| `inflation_pct` | REAL | General inflation — Base Expense and the Kids "basic" sub-component |
| `education_inflation_pct` | REAL | Kids "education" sub-component only |
| `healthcare_inflation_pct` | REAL | Parents Expense only (not general inflation) |
| `kids_expense_start_year` | INTEGER | First year Kids Expense (D) becomes non-zero |
| `kids_school_start_age` | INTEGER | Age at which the Kids Education sub-component (10%) begins; its snapshot year is `kids_expense_start_year + kids_school_start_age`. Validated: must be `> 0` |
| `kids_expense_end_year` | INTEGER | Last year (inclusive) Kids Expense (D) is non-zero |
| `parents_expense_start_year` | INTEGER | First year Parents Expense (E) becomes non-zero |
| `parents_expense_end_year` | INTEGER | Last year (inclusive) Parents Expense (E) is non-zero |
| `loan_outstanding_lakhs` | REAL | Informational / future use |
| `annual_emi_lakhs` | REAL | EMI amount while active |
| `loan_end_year` | INTEGER | Last year (inclusive) the EMI is active; `F(y) = True` while `year <= loan_end_year` |
| `rental_income_lakhs` | REAL | Seed for Rental Income (I), year 2026 |
| `rent_increase_pct` | REAL | Annual growth for Rental Income |
| `current_annual_salary_lakhs` | REAL | Seed for Salary (J), year 2026 |
| `salary_growth_pct` | REAL | Flat annual increase for Salary |
| `retirement_year` | INTEGER | First year Salary (J) becomes 0. Validated: must be `> PROJECTION_START_YEAR` (2026) |
| `starting_cash_lakhs` | REAL | Cash Start (O) for year 2026 |
| `equity_portfolio_lakhs` | REAL | Seed for Equity Start (R), year 2026 |
| `equity_return_pct` | REAL | Annual equity growth rate |
| `debt_portfolio_lakhs` | REAL | Seed for Debt Start (U), year 2026 |
| `debt_return_pct` | REAL | Annual debt growth rate |

### `projection_extra_income` (manual, ad hoc — feeds column K)

| Field | Type | Meaning |
|---|---|---|
| `year` | INTEGER, unique | Which year this applies to |
| `amount_lakhs` | REAL | One-off/ad hoc extra income for that year |
| `note` | TEXT, optional | e.g. "bonus", "asset sale" |

Years with no row default to `K(y) = 0`.

---

## 2. Formulas, explained plainly with worked numbers

To make the mechanics concrete rather than spreadsheet-shorthand, here's a
full walkthrough of a normal year using made-up sample numbers, followed by
a separate isolated example just for the withdrawal waterfall (since that's
the part with the most moving pieces).

**Sample assumptions used only for this illustration** (not real inputs):
`current_annual_expense=12`, `inflation=6%`, `rental_income=3`,
`rent_increase=5%`, `salary=25`, `salary_growth=8%`, `starting_cash=5`,
`equity_portfolio=50`, `equity_return=12%`, `debt_portfolio=30`,
`debt_return=7%`, `annual_emi=8`, `loan_end_year=2031` (so EMI active
2026–2031 inclusive). Kids/Parents not yet triggered in this example year.

### Year 2026 (a normal, no-shortfall year)

**Expenses**
- Base Expense `C = 12` (seed value, no growth applied in year 1 — growth
  starts from year 2 onward)
- Kids Expense `D = 0` (before start year)
- Parents Expense `E = 0` (before start year)
- EMI Active `F = True` (2026 ≤ `loan_end_year` of 2031), EMI Amount `G = 8`
- **Total Expense `H = C + D + E + G = 12 + 0 + 0 + 8 = 20`**

**Income**
- Rental Income `I = 3` (seed)
- Salary `J = 25` (seed, before retirement year)
- Extra Income `K = 0` (no manual entry this year)
- **Total Income `L = I + J + K = 3 + 25 + 0 = 28`**

**Cash flow** (income exceeds expense this year)
- Net Expense `M = MAX(0, H-L) = MAX(0, 20-28) = 0`
- Reserve Contribution `N = MAX(0, L-H) = MAX(0, 28-20) = 8`
- Cash Start `O = 5` (seed)
- Net Cash Flow `P = N - M = 8 - 0 = 8` — note this always equals `L - H`
  signed; M/N just split it into two non-negative display columns
- Cash End `Q = MAX(0, O+P) = MAX(0, 5+8) = 13`

**Equity / Debt** (no shortfall this year, so no withdrawal — see next
section for what happens when there is one)
- Equity Start `R = 50`, Growth `S = R × 12% = 6`, Equity End `T = R+S = 56`
- Debt Start `U = 30`, Growth `V = U × 7% = 2.1`, Debt End `W = U+V = 32.1`
- **Total Portfolio `X = T + W + Q = 56 + 32.1 + 13 = 101.1`**

### Isolated example: the withdrawal waterfall (cash → debt → equity)

This only kicks in when `O + P` (what cash would be, before flooring at
zero) goes negative — i.e. expenses outran income and the existing cash
buffer isn't enough either. Suppose, in some hypothetical year:

- `O + P = -10` (i.e. a ₹10L shortfall after using all this year's income
  and existing cash)
- Debt balance before withdrawal, `U + V = 6`
- Equity balance before withdrawal, `R + S = 40`

Then:
```
cash_shortfall    = MAX(0, -(O+P))            = MAX(0, 10) = 10
debt_withdrawal   = MIN(cash_shortfall, U+V)  = MIN(10, 6) = 6   ← drain debt first
equity_withdrawal = MAX(0, cash_shortfall - (U+V)) = MAX(0, 10-6) = 4  ← only remainder from equity
```
Result: `W (Debt End) = U+V - 6 = 0` (fully drained), `T (Equity End) = R+S - 4 = 36`,
`Q (Cash End) = MAX(0, O+P) = 0` (floored — cash never goes negative on the sheet).

### Kids Expense (D) and Parents Expense (E) — now the same shape

Both are two-state formulas: zero before a start year, then **each
sub-component is snapshotted once at the start year and grows independently
at its own rate from then on** — this is the same "snapshot, then compound
independently" pattern for every sub-component, just with different
percentages and different growth rates:

| Expense | Sub-component | % of Base Expense at start year | Growth rate after snapshot |
|---|---|---|---|
| Kids (D) | Basic | 30% | `inflation_pct` (general inflation) |
| Kids (D) | Education | 10% | `education_inflation_pct` |
| Parents (E) | (single component) | 40% | `healthcare_inflation_pct` |

```
# Kids Expense
for y < kids_expense_start_year:      D(y) = 0
for y == kids_expense_start_year:     BasicPortion(y) = 0.30 × C(y)      ← snapshot
                                       EduPortion(y)   = 0.10 × C(y)      ← snapshot
                                       D(y) = BasicPortion(y) + EduPortion(y)
for y > kids_expense_start_year:      BasicPortion(y) = BasicPortion(y-1) × (1 + inflation_pct)
                                       EduPortion(y)   = EduPortion(y-1) × (1 + education_inflation_pct)
                                       D(y) = BasicPortion(y) + EduPortion(y)

# Parents Expense
for y < parents_expense_start_year:   E(y) = 0
for y == parents_expense_start_year:  E(y) = 0.40 × C(y)                 ← snapshot
for y > parents_expense_start_year:   E(y) = E(y-1) × (1 + healthcare_inflation_pct)
```

Note: since `BasicPortion` uses the *same* rate (`inflation_pct`) that
drives `C`'s own growth, snapshotting-and-compounding it produces
identical numbers to recomputing `0.30 × C(y)` fresh every year — so this
restructuring doesn't change any of the numbers, it just makes the Kids and
Parents formulas structurally identical (every sub-component = snapshot at
start year + independent compounding), which keeps the implementation
consistent and lets both share one helper function in code.

**Worked example** (continuing the sample assumptions, `inflation=6%`,
`education_inflation=10%`, `kids_expense_start_year=2029`):
- `C(2027)=12.72`, `C(2028)=13.4832`, `C(2029)=14.2922`
- 2029 (snapshot year): `BasicPortion = 0.30×14.2922 = 4.29`,
  `EduPortion = 0.10×14.2922 = 1.43` → `D(2029) = 5.72`
- 2030: `BasicPortion = 4.29 × 1.06 = 4.54` (grown at general inflation,
  independent of `C(2030)` now); `EduPortion = 1.43 × 1.10 = 1.57` (grown at
  education inflation) → `D(2030) = 6.12` — same result as the previous
  "recompute off `C(y)`" version, confirming the two formulations agree.

### Total Expense (confirmed)
```
G(y) = annual_emi_lakhs   if F(y) is True   else 0
H(y) = C(y) + D(y) + E(y) + G(y)
```

### Salary (J) — confirmed: flat growth with a retirement cutoff
```
for y <= retirement_year - 1:  J(2026) = current_annual_salary_lakhs (seed)
                                J(y) = J(y-1) × (1 + salary_growth_pct)  for y > 2026
for y >= retirement_year:      J(y) = 0
```

### Total Income (confirmed typo fix)
```
L(y) = I(y) + J(y) + K(y)
```

---

## 3. Full column reference (confirmed)

| Col | Name | Formula |
|---|---|---|
| A | Year | 2026 → 2070 |
| B | Offset | `year - 2026` |
| C | Base Expense | seed year 1; `C(y)=C(y-1)×(1+inflation_pct)` after |
| D | Kids Expense | see §2 — snapshot + independent growth per sub-component |
| E | Parents Expense | see §2 — snapshot + independent growth, healthcare inflation |
| F | EMI Active | `True` while `year <= loan_end_year` |
| G | EMI Amount | `annual_emi_lakhs` if F else `0` |
| H | Total Expense | `C+D+E+G` |
| I | Rental Income | seed year 1; grows by `rent_increase_pct` after |
| J | Salary | flat growth, `0` from `retirement_year` onward |
| K | Extra Income | manual entry from `projection_extra_income`, default `0` |
| L | Total Income | `I+J+K` |
| M | Net Expense | `MAX(0, H-L)` |
| N | Reserve Contribution | `MAX(0, L-H)` |
| O | Cash Start | `Q(y-1)`; seed = `starting_cash_lakhs` in 2026 |
| P | Net Cash Flow | `N-M` (= `L-H` signed) |
| Q | Cash End | `MAX(0, O+P)` |
| R | Equity Start | `T(y-1)`; seed = `equity_portfolio_lakhs` in 2026 |
| S | Equity Growth | `R × equity_return_pct` |
| T | Equity End | `R+S-equity_withdrawal` (waterfall, §2) |
| U | Debt Start | `W(y-1)`; seed = `debt_portfolio_lakhs` in 2026 |
| V | Debt Growth | `U × debt_return_pct` |
| W | Debt End | `U+V-debt_withdrawal` (waterfall, §2) |
| X | Total Portfolio | `T+W+Q` (year-end snapshot only, doesn't feed back into T/W) |

---

## 4. API design

- `GET /api/fire-projection/assumptions` — current stored assumptions row
  plus the list of extra-income entries.
- `PUT /api/fire-projection/assumptions` — update assumptions (full or
  partial); validates types/ranges before saving.
- `GET|POST|DELETE /api/fire-projection/extra-income` — manage manual extra
  income entries.
- `GET /api/fire-projection` — recomputes and returns the full year-by-year
  table (2026–2070, all columns A–X) from whatever is currently stored.
  Cheap enough (45 rows) to recompute on every call rather than caching —
  so editing an assumption and re-fetching reflects it immediately.

---

## 5. Static form & UI scope

Since validation happens by you keying in real values and comparing output
row-by-row against the spreadsheet, the UI is in scope for Phase 1, not
deferred to later. It follows the existing app's conventions: plain
HTML/CSS/JS with no build step, Chart.js from CDN, FontAwesome icons, and
the same `card`-based grid layout used in `static/index.html` /
`dashboard.js`. `loan_end_year` is a settled, ordinary field in the
assumptions form — no special handling needed for it beyond the other
`*_year` inputs.

### New route
- `GET /projection` in `main.py` → serves a new `static/projection.html`
  (mirrors how `/` serves `static/index.html`).
- A small nav link added to both pages' headers ("Expenses" ↔ "Wealth
  Projection") so you can move between the two dashboards.

### New files
- `static/projection.html` — page shell, same header/card-grid pattern as
  `index.html`.
- `static/projection.js` — all fetch/render logic for this page (mirrors
  `dashboard.js`'s role for the expense dashboard).

### UI pieces, in priority order (priority driven by what you need to validate)

1. **Assumptions form** (highest priority) — one input per field in
   `projection_assumptions` (§1 table), grouped into logical sections
   (Expense & Inflation / Loan / Income / Portfolio) for readability. Number
   inputs for `_lakhs` and `_pct` fields, plain integer inputs for the
   `*_year` fields (including `loan_end_year`, `kids_expense_start_year`,
   `parents_expense_start_year`, `retirement_year` — all treated uniformly).
   A **Save** button calls `PUT /api/fire-projection/assumptions`, then
   immediately re-fetches and re-renders the projection table below — this
   tight edit → recompute → inspect loop is the whole point of scoping the
   UI now.
2. **Extra Income manager** — a small table-container listing existing
   `projection_extra_income` rows (year, amount, note) with a delete button
   per row, plus a compact add-row form (year / amount / note) at the top.
   Saves trigger a projection re-fetch the same way.
3. **Projection table** (this is the one you'll actually use to validate
   row-by-row against the spreadsheet) — a wide, scrollable table rendering
   **every column A–X** for every year 2026–2070, in the same table-based
   pattern already used for `topExpenses` / `topRecurringExpenses`. Given
   the goal is direct comparison, this needs to be a literal data table, not
   a chart, though visually marking the `kids_expense_start_year`,
   `parents_expense_start_year`, `retirement_year`, and `loan_end_year`
   transition rows (e.g. a subtle row highlight) would make it faster to
   eyeball while validating.
4. **Summary chart** (lowest priority, nice-to-have) — a Chart.js line chart
   of Total Portfolio (`X`) across all years, consistent with the existing
   dashboard's chart style. Only worth doing once the table itself is
   confirmed correct — treat as optional polish, not blocking.

---

## 6. Implementation sub-tasks

1. Migration: create `projection_assumptions` (single row) and
   `projection_extra_income` tables.
2. `splitapi/projections.py`: pure function
   `run_projection(assumptions, extra_income_by_year, start_year=2026, end_year=2070) -> list[dict]`,
   one dict per year with keys matching columns A–X. Share one helper for
   the "snapshot at start year, then compound independently" pattern used by
   both Kids sub-components and Parents Expense.
3. Wire up the four endpoints in §4.
4. `GET /projection` route + `static/projection.html` + `static/projection.js`
   implementing the four UI pieces in §5, in that priority order.
5. Validate against your real spreadsheet by keying in your actual values
   into the form and comparing rows directly in the projection table.
6. (Deferred, per your note) Monthly breakdown view — only after yearly
   logic is confirmed correct.
