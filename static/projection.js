let portfolioChartInstance = null;

// Matches the fixed projection horizon used server-side (see /api/fire-projection).
const PROJECTION_START_YEAR = 2026;
const PROJECTION_END_YEAR = 2070;

// Every assumption field that represents a calendar year, rendered as a
// <select> dropdown instead of a free-text number input so the user can't
// key in an out-of-range or nonsensical year.
const YEAR_SELECT_IDS = [
    "kids_expense_start_year", "kids_expense_end_year",
    "parents_expense_start_year", "parents_expense_end_year",
    "loan_end_year", "retirement_year",
];

function populateYearSelect(selectId, startYear, endYear) {
    const select = document.getElementById(selectId);
    if (!select) return;
    select.innerHTML = "";
    for (let year = startYear; year <= endYear; year++) {
        const option = document.createElement("option");
        option.value = year;
        option.textContent = year;
        select.appendChild(option);
    }
}

function initYearSelects() {
    YEAR_SELECT_IDS.forEach((id) => populateYearSelect(id, PROJECTION_START_YEAR, PROJECTION_END_YEAR));
}

const ASSUMPTION_FIELDS = [
    "current_annual_expense_lakhs", "inflation_pct", "education_inflation_pct",
    "healthcare_inflation_pct", "kids_expense_start_year", "kids_school_start_age",
    "kids_expense_end_year", "parents_expense_start_year", "parents_expense_end_year",
    "loan_outstanding_lakhs", "annual_emi_lakhs", "loan_end_year",
    "rental_income_lakhs", "rent_increase_pct", "current_annual_salary_lakhs",
    "salary_growth_pct", "retirement_year", "starting_cash_lakhs",
    "equity_portfolio_lakhs", "equity_return_pct", "debt_portfolio_lakhs", "debt_return_pct"
];

// Fields that should be parsed as whole numbers - either an absolute year
// or (for kids_school_start_age) an age in years.
const INTEGER_FIELDS = new Set([
    "kids_expense_start_year", "kids_school_start_age", "kids_expense_end_year",
    "parents_expense_start_year", "parents_expense_end_year",
    "loan_end_year", "retirement_year"
]);

// `group` drives both the visual column-section tinting and the grouped
// header row in the projection table. `emi_active` is intentionally not
// listed here - it's still returned by the API, just not shown, since the
// EMI amount (which already reflects it) is more useful to see directly.
const COLUMN_DEFS = [
    { key: "year", label: "Year", group: null, plain: true },

    { key: "base_expense", label: "Base Exp.", group: "expense" },
    { key: "kids_expense", label: "Kids Exp.", group: "expense" },
    { key: "parents_expense", label: "Parents Exp.", group: "expense" },
    { key: "emi_amount", label: "EMI Amt", group: "expense" },
    { key: "total_expense", label: "Total Exp.", group: "expense" },

    // Present only once a loan is set up in the Journal (see renderProjectionTable).
    { key: "loan_interest", label: "Loan Interest", group: "loan", optional: true },
    { key: "loan_principal", label: "Loan Principal", group: "loan", optional: true },
    { key: "loan_prepayment", label: "Prepaid", group: "loan", optional: true },
    { key: "loan_balance_end", label: "Loan Balance", group: "loan", optional: true },

    { key: "rental_income", label: "Rental Inc.", group: "income" },
    { key: "salary", label: "Salary", group: "income" },
    { key: "extra_income", label: "Extra Inc.", group: "income" },
    { key: "total_income", label: "Total Inc.", group: "income" },

    { key: "net_expense", label: "Net Exp.", group: "cashflow" },
    { key: "reserve_contribution", label: "Reserve Contrib.", group: "cashflow" },
    { key: "cash_start", label: "Cash Start", group: "cashflow" },
    { key: "net_cash_flow", label: "Net Cash Flow", group: "cashflow" },
    { key: "cash_end", label: "Cash End", group: "cashflow" },

    { key: "equity_start", label: "Equity Start", group: "equity" },
    { key: "equity_growth", label: "Equity Growth", group: "equity" },
    { key: "equity_end", label: "Equity End", group: "equity" },

    { key: "debt_start", label: "Debt Start", group: "debt" },
    { key: "debt_growth", label: "Debt Growth", group: "debt" },
    { key: "debt_end", label: "Debt End", group: "debt" },

    { key: "total_portfolio", label: "Total Portfolio", group: "total" },

];

const GROUP_LABELS = {
    expense: "Expense",
    loan: "Loan",
    income: "Income",
    cashflow: "Cash Flow",
    equity: "Equity",
    debt: "Debt",
    total: "Total",
};

function fmt(value, isPlain = false) {
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "number") {
        // Plain values (e.g. calendar years) should never get thousands
        // separators, otherwise 2027 renders as "2,027".
        if (isPlain) return String(value);
        return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return value ?? "";
}

async function loadAssumptionsIntoForm() {
    const res = await fetch("/api/fire-projection/assumptions");
    const data = await res.json();
    ASSUMPTION_FIELDS.forEach((field) => {
        const input = document.getElementById(field);
        if (input && data.assumptions[field] !== undefined && data.assumptions[field] !== null) {
            input.value = data.assumptions[field];
        }
    });
    return data.assumptions;
}

function milestoneYearsFrom(assumptions) {
    return new Set([
        assumptions.kids_expense_start_year,
        assumptions.kids_expense_start_year + assumptions.kids_school_start_age,
        assumptions.kids_expense_end_year,
        assumptions.parents_expense_start_year,
        assumptions.parents_expense_end_year,
        assumptions.retirement_year,
        assumptions.loan_end_year,
    ]);
}

function buildGroupHeaderRow(columns) {
    let html = "<tr class=\"group-header-row\">";
    let i = 0;
    while (i < columns.length) {
        const group = columns[i].group;
        let span = 1;
        while (i + span < columns.length && columns[i + span].group === group) {
            span++;
        }
        const groupClass = group ? `col-group-${group}` : "";
        const label = group ? GROUP_LABELS[group] : "";
        html += `<th colspan="${span}" class="group-header-cell ${groupClass}">${label}</th>`;
        i += span;
    }
    html += "</tr>";
    return html;
}

const MONTH_STATUS = {
    actual: { label: "Actual", cls: "month-actual" },
    in_progress: { label: "In progress (forecast used)", cls: "month-in-progress" },
    forecast: { label: "Forecast", cls: "month-forecast" },
    no_data: { label: "No data - forecast used", cls: "month-no-data" },
};

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Years whose month sub-rows are open. null = not initialised yet (first
// render opens the current, partly-actual year); afterwards it persists
// across re-renders so saving assumptions doesn't collapse what you opened.
let expandedYears = null;

function yearBadges(row) {
    let html = "";
    if (row.expense_source === "actual") {
        html += `<span class="source-badge source-actual">Actual</span>`;
    } else if (row.expense_source === "blended") {
        html += `<span class="source-badge source-blended">Blended</span>`;
    }
    if (row.checkin_months) {
        html += `<span class="source-badge source-checkin" title="Balances were reset by a check-in in ${escapeHtml(row.checkin_months.join(", "))}. Start columns still show the forecast; the green/red chips on the End columns show the difference the check-in made.">Check-in</span>`;
    }
    if (row.no_data_months > 0) {
        html += `<span class="source-badge source-gap" title="Closed months with no recorded expenses - the forecast was used. Sync or import them.">${row.no_data_months} gap${row.no_data_months > 1 ? "s" : ""}</span>`;
    }
    return html;
}

// A month sub-row only has data for some columns; the rest stay blank.
// Base Exp. + EMI Amt = Total Exp., so an actual month's Base Exp. matches
// the Expenses tab for that month.
function monthCellValue(month, colKey) {
    switch (colKey) {
        case "year": return null;
        // Non-loan spend: the recorded total for actual months (what the
        // Expenses tab shows for that month), the forecast otherwise.
        case "base_expense": return month.spend;
        case "emi_amount": return month.loan_payment;
        case "total_expense": return month.expense;
        case "total_income": return month.income;
        case "net_expense": return Math.max(0, -month.net);
        case "reserve_contribution": return Math.max(0, month.net);
        case "net_cash_flow": return month.net;
        case "cash_end": return month.cash_end;
        case "equity_end": return month.equity_end;
        case "debt_end": return month.debt_end;
        case "total_portfolio": return month.total_portfolio;
        case "loan_interest": return month.loan_interest;
        case "loan_principal": return month.loan_principal;
        case "loan_prepayment": return month.loan_prepayment;
        case "loan_balance_end": return month.loan_balance_end;
        default: return undefined;
    }
}

// Variance of a check-in vs what the forecast expected, per column.
// Higher than expected is green, lower is red; the loan balance is the
// reverse (owing less is good).
function monthVariance(month, colKey) {
    const marks = month.checkin;
    if (!marks) return null;
    switch (colKey) {
        case "cash_end": return marks.cash ? { value: marks.cash.variance, goodWhenUp: true } : null;
        case "equity_end": return marks.equity ? { value: marks.equity.variance, goodWhenUp: true } : null;
        case "debt_end": return marks.debt ? { value: marks.debt.variance, goodWhenUp: true } : null;
        case "total_portfolio":
            return month.checkin_total_variance === null || month.checkin_total_variance === undefined
                ? null : { value: month.checkin_total_variance, goodWhenUp: true };
        case "loan_balance_end": return marks.loan ? { value: marks.loan.variance, goodWhenUp: false } : null;
        default: return null;
    }
}

function varianceChip(variance) {
    if (!variance) return "";
    const value = variance.value;
    if (Math.abs(value) < 0.005) return ` <span class="var-chip var-flat" title="Matched the forecast">0.00</span>`;
    const good = variance.goodWhenUp ? value > 0 : value < 0;
    const sign = value > 0 ? "+" : "\u2212";
    const text = Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return ` <span class="var-chip ${good ? "var-good" : "var-bad"}" title="Check-in vs forecast: ${sign}${text} L">${sign}${text}</span>`;
}

// A collapsed year row has no month chips, so its End cells carry the year's
// total check-in variance (the sum of that year's check-in variances).
function yearVariance(row, colKey) {
    if (!row.checkin_months) return null;
    const adjustments = { cash_end: row.cash_adjustment, equity_end: row.equity_adjustment, debt_end: row.debt_adjustment };
    if (colKey in adjustments) return { value: adjustments[colKey], goodWhenUp: true };
    if (colKey === "total_portfolio") {
        return { value: row.cash_adjustment + row.equity_adjustment + row.debt_adjustment, goodWhenUp: true };
    }
    return null;
}

function renderMonthRows(row, columns) {
    let html = "";
    row.months.forEach((month) => {
        const status = MONTH_STATUS[month.status] || { label: escapeHtml(month.status), cls: "" };
        const monthIndex = parseInt(month.month.slice(5, 7), 10) - 1;
        const spendNote = month.actual_spend !== null
            ? ` title="Recorded spend ${month.actual_spend.toFixed(2)} L${month.status === "actual" ? " + loan payment" : " so far this month"}"`
            : "";
        const checkinClass = month.checkin ? " month-checkin" : "";
        html += `<tr class="month-row ${status.cls}${checkinClass}${expandedYears.has(row.year) ? "" : " hidden"}" data-month-of="${row.year}"${spendNote}>`;
        columns.forEach((col) => {
            const groupClass = col.group ? `col-group-${col.group}` : "";
            if (col.key === "year") {
                html += `<td class="${groupClass}">${MONTH_NAMES[monthIndex]} &middot; ${status.label}</td>`;
                return;
            }
            const value = monthCellValue(month, col.key);
            html += `<td class="${groupClass}">${value === undefined ? "" : fmt(value, col.plain)}${varianceChip(monthVariance(month, col.key))}</td>`;
        });
        html += "</tr>";
    });
    return html;
}

function renderProjectionTable(rows, milestones) {
    const container = document.getElementById("projectionTable");
    container.classList.add("dense-table");
    if (expandedYears === null) {
        const current = rows.find((row) => row.expense_source === "blended");
        expandedYears = new Set(current ? [current.year] : []);
    }
    const columns = COLUMN_DEFS.filter((col) => !col.optional || rows.some((row) => row[col.key] !== undefined));
    let html = "<table><thead>";
    html += buildGroupHeaderRow(columns);
    html += "<tr>";
    columns.forEach((col) => {
        const groupClass = col.group ? `col-group-${col.group}` : "";
        html += `<th class="${groupClass}">${col.label}</th>`;
    });
    html += "</tr></thead><tbody>";
    rows.forEach((row) => {
        const rowClass = milestones.has(row.year) ? "milestone-row" : "";
        const hasMonths = Array.isArray(row.months);
        html += `<tr class="${rowClass}">`;
        columns.forEach((col) => {
            const groupClass = col.group ? `col-group-${col.group}` : "";
            if (col.key === "year" && hasMonths) {
                const open = expandedYears.has(row.year);
                html += `<td class="${groupClass} year-toggle${open ? " expanded" : ""}" data-year="${row.year}"><span class="chevron">&#9656;</span>${row.year}${yearBadges(row)}</td>`;
                return;
            }
            if (col.key === "year") {
                html += `<td class="${groupClass}">${row.year}${yearBadges(row)}</td>`;
                return;
            }
            html += `<td class="${groupClass}">${fmt(row[col.key], col.plain)}${varianceChip(yearVariance(row, col.key))}</td>`;
        });
        html += "</tr>";
        if (hasMonths) html += renderMonthRows(row, columns);
    });
    html += "</tbody></table>";
    container.innerHTML = html;

    container.querySelectorAll(".year-toggle").forEach((cell) => {
        cell.addEventListener("click", () => {
            const year = parseInt(cell.dataset.year, 10);
            if (expandedYears.has(year)) expandedYears.delete(year); else expandedYears.add(year);
            cell.classList.toggle("expanded", expandedYears.has(year));
            container.querySelectorAll(`tr[data-month-of="${year}"]`).forEach((tr) => {
                tr.classList.toggle("hidden", !expandedYears.has(year));
            });
        });
    });
}

function renderPortfolioChart(rows, planRows) {
    const canvas = document.getElementById("portfolioChart");
    const ctx = canvas.getContext("2d");
    const totalGradient = ctx.createLinearGradient(0, 0, 0, 400);
    totalGradient.addColorStop(0, "rgba(0, 210, 211, 0.35)");
    totalGradient.addColorStop(1, "rgba(0, 210, 211, 0)");

    if (portfolioChartInstance) portfolioChartInstance.destroy();
    portfolioChartInstance = new Chart(ctx, {
        type: "line",
        data: {
            labels: rows.map((r) => r.year),
            datasets: [
                {
                    label: "Plan (assumptions only)",
                    data: planRows.map((r) => r.total_portfolio),
                    borderColor: "#a0a5b8",
                    backgroundColor: "transparent",
                    borderWidth: 2,
                    borderDash: [8, 4],
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: "Total Portfolio (actual + forecast)",
                    data: rows.map((r) => r.total_portfolio),
                    borderColor: "#00d2d3",
                    backgroundColor: totalGradient,
                    borderWidth: 3,
                    fill: true,
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: "Equity",
                    data: rows.map((r) => r.equity_end),
                    borderColor: "#6c5ce7",
                    backgroundColor: "transparent",
                    borderWidth: 2,
                    borderDash: [4, 3],
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: "Debt",
                    data: rows.map((r) => r.debt_end),
                    borderColor: "#ff9f40",
                    backgroundColor: "transparent",
                    borderWidth: 2,
                    borderDash: [4, 3],
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: "Cash",
                    data: rows.map((r) => r.cash_end),
                    borderColor: "#ffce56",
                    backgroundColor: "transparent",
                    borderWidth: 2,
                    borderDash: [4, 3],
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    labels: { color: "#a0a5b8", usePointStyle: true, pointStyle: "circle", boxWidth: 8 },
                },
                tooltip: {
                    backgroundColor: "#1a1d2d",
                    padding: 10,
                    callbacks: {
                        label: (ctx) => `${ctx.dataset.label}: ₹${ctx.parsed.y.toLocaleString(undefined, { minimumFractionDigits: 2 })} L`,
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: false,
                    ticks: { callback: (v) => `₹${v}L` },
                },
            },
        },
    });
}

async function refreshProjection() {
    const assumptionsRes = await fetch("/api/fire-projection/assumptions");
    const assumptionsData = await assumptionsRes.json();
    const milestones = milestoneYearsFrom(assumptionsData.assumptions);

    // Plan = assumptions only; the table and the main chart lines blend in
    // recorded spend for months that have already closed.
    const [planRes, actualRes] = await Promise.all([
        fetch("/api/fire-projection"),
        fetch("/api/fire-projection?actuals=true"),
    ]);
    const planRows = await planRes.json();
    const rows = await actualRes.json();
    renderProjectionTable(rows, milestones);
    renderPortfolioChart(rows, planRows);
}

async function refreshAll() {
    await loadAssumptionsIntoForm();
    await refreshProjection();
}

function collectAssumptionsFromForm() {
    const data = {};
    ASSUMPTION_FIELDS.forEach((field) => {
        const input = document.getElementById(field);
        if (!input || input.value === "") return;
        data[field] = INTEGER_FIELDS.has(field) ? parseInt(input.value, 10) : parseFloat(input.value);
    });
    return data;
}

document.getElementById("assumptionsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const statusEl = document.getElementById("assumptionsSaveStatus");
    statusEl.textContent = "Saving...";
    try {
        const payload = collectAssumptionsFromForm();
        const res = await fetch("/api/fire-projection/assumptions", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.detail || "Request failed");
        }
        statusEl.textContent = "Saved \u2713";
        await refreshProjection();
    } catch (err) {
        statusEl.textContent = `Failed: ${err.message}`;
        console.error(err);
    } finally {
        setTimeout(() => { statusEl.textContent = ""; }, 4000);
    }
});

function initDrawer() {
    const drawer = document.getElementById("assumptionsDrawer");
    const overlay = document.getElementById("drawerOverlay");
    const openBtn = document.getElementById("openDrawerBtn");
    const closeBtn = document.getElementById("closeDrawerBtn");

    const open = () => {
        drawer.classList.add("open");
        overlay.classList.add("open");
    };
    const close = () => {
        drawer.classList.remove("open");
        overlay.classList.remove("open");
    };

    openBtn.addEventListener("click", open);
    closeBtn.addEventListener("click", close);
    overlay.addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") close();
    });
}

initYearSelects();
initDrawer();
refreshAll();
