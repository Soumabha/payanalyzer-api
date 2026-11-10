let portfolioChartInstance = null;

// Matches the fixed projection horizon used server-side (see /api/fire-projection).
const PROJECTION_START_YEAR = 2026;
const PROJECTION_END_YEAR = 2070;

// Every assumption field that represents a calendar year, rendered as a
// <select> dropdown instead of a free-text number input so the user can't
// key in an out-of-range or nonsensical year. extraIncomeYear is handled
// separately since it's not part of ASSUMPTION_FIELDS.
const YEAR_SELECT_IDS = [
    "kids_expense_start_year", "kids_expense_end_year",
    "parents_expense_start_year", "parents_expense_end_year",
    "loan_end_year", "retirement_year", "extraIncomeYear",
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
    income: "Income",
    cashflow: "Cash Flow",
    equity: "Equity",
    debt: "Debt",
    total: "Total",
};

function fmt(value, isInteger = false, isPlain = false) {
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "number") {
        // Plain values (e.g. calendar years) should never get thousands
        // separators, otherwise 2027 renders as "2,027".
        if (isPlain) return String(value);
        if (isInteger) return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
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
    renderExtraIncomeList(data.extra_income || []);
    return data.assumptions;
}

function renderExtraIncomeList(entries) {
    const container = document.getElementById("extraIncomeList");
    if (!entries.length) {
        container.innerHTML = `<div class="hint-text"><i class="fa-solid fa-circle-info"></i> No manual entries yet.</div>`;
        return;
    }
    let html = `<table><thead><tr><th>Year</th><th>Amount (Lakhs)</th><th>Note</th><th></th></tr></thead><tbody>`;
    entries.forEach((e) => {
        html += `<tr>
            <td>${e.year}</td>
            <td>${fmt(e.amount_lakhs)}</td>
            <td>${e.note || ""}</td>
            <td><button class="delete-row-btn" data-year="${e.year}" title="Delete"><i class="fa-solid fa-trash"></i></button></td>
        </tr>`;
    });
    html += `</tbody></table>`;
    container.innerHTML = html;
    container.querySelectorAll(".delete-row-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            await fetch(`/api/fire-projection/extra-income/${btn.dataset.year}`, { method: "DELETE" });
            await refreshAll();
        });
    });
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

function buildGroupHeaderRow() {
    let html = "<tr class=\"group-header-row\">";
    let i = 0;
    while (i < COLUMN_DEFS.length) {
        const group = COLUMN_DEFS[i].group;
        let span = 1;
        while (i + span < COLUMN_DEFS.length && COLUMN_DEFS[i + span].group === group) {
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

function renderProjectionTable(rows, milestones) {
    const container = document.getElementById("projectionTable");
    container.classList.add("dense-table");
    let html = "<table><thead>";
    html += buildGroupHeaderRow();
    html += "<tr>";
    COLUMN_DEFS.forEach((col) => {
        const groupClass = col.group ? `col-group-${col.group}` : "";
        html += `<th class="${groupClass}">${col.label}</th>`;
    });
    html += "</tr></thead><tbody>";
    rows.forEach((row) => {
        const rowClass = milestones.has(row.year) ? "milestone-row" : "";
        html += `<tr class="${rowClass}">`;
        COLUMN_DEFS.forEach((col) => {
            const groupClass = col.group ? `col-group-${col.group}` : "";
            html += `<td class="${groupClass}">${fmt(row[col.key], col.integer, col.plain)}</td>`;
        });
        html += "</tr>";
    });
    html += "</tbody></table>";
    container.innerHTML = html;
}

function renderPortfolioChart(rows) {
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
                    label: "Total Portfolio",
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

    const res = await fetch("/api/fire-projection");
    const rows = await res.json();
    renderProjectionTable(rows, milestones);
    renderPortfolioChart(rows);
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

document.getElementById("extraIncomeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const year = parseInt(document.getElementById("extraIncomeYear").value, 10);
    const amount_lakhs = parseFloat(document.getElementById("extraIncomeAmount").value);
    const note = document.getElementById("extraIncomeNote").value || null;
    await fetch("/api/fire-projection/extra-income", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ year, amount_lakhs, note }),
    });
    document.getElementById("extraIncomeForm").reset();
    await refreshAll();
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
