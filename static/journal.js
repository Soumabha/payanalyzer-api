// Journal page: check-ins, loans (any number), and ledger entries (extra income
// and moves between cash and investments). All amounts are in lakhs.
//
// Every server- or user-supplied string that reaches innerHTML goes through
// escapeHtml() (static/html-utils.js).

const SCHEDULE_PREVIEW_ROWS = 24;
const expandedSchedules = new Set();   // loan ids whose full schedule is open
const expandedLoans = new Set();       // loan ids whose section is open (all start collapsed)
let lastJournal = null;
let lastLoans = null;                  // {loans: [view...], combined, legacy}

function loanViews() {
    return (lastLoans && lastLoans.loans) || [];
}

function loanName(loanId) {
    const view = loanViews().find((v) => v.loan.id === Number(loanId));
    return view ? view.loan.name : `loan ${loanId}`;
}

function lakhs(value) {
    if (value === null || value === undefined) return "&ndash;";
    return Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function todayLocal() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

async function api(method, url, body) {
    const res = await fetch(url, {
        method,
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
        throw new Error(detail || "Request failed");
    }
    return data;
}

// ---------------------------------------------------------------- modal ----

const overlay = document.getElementById("modalOverlay");
const modal = document.getElementById("modal");

function closeModal() {
    overlay.classList.remove("open");
    modal.innerHTML = "";
}

overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });

function fieldHtml(field) {
    const id = `f_${field.name}`;
    if (field.type === "checkbox") {
        return `<label class="field-check"><input type="checkbox" id="${id}" ${field.value ? "checked" : ""}> ${escapeHtml(field.label)}</label>`;
    }
    const value = field.value === undefined || field.value === null ? "" : escapeHtml(field.value);
    let control;
    if (field.type === "select") {
        control = `<select id="${id}">${field.options.map(([v, l]) => `<option value="${escapeHtml(v)}" ${String(v) === String(field.value) ? "selected" : ""}>${escapeHtml(l)}</option>`).join("")}</select>`;
    } else {
        const step = field.type === "number" ? ` step="${field.step || "0.01"}"` : "";
        const max = field.max ? ` max="${escapeHtml(field.max)}"` : "";
        control = `<input type="${field.type}" id="${id}"${step}${max} value="${value}" ${field.required ? "required" : ""}>`;
    }
    return `<div class="form-field"><label for="${id}">${escapeHtml(field.label)}</label>${control}</div>`;
}

function readFields(fields) {
    const values = {};
    fields.forEach((field) => {
        const el = document.getElementById(`f_${field.name}`);
        if (field.type === "checkbox") values[field.name] = el.checked;
        else if (field.type === "number") values[field.name] = el.value === "" ? null : parseFloat(el.value);
        else values[field.name] = el.value === "" ? null : el.value;
    });
    return values;
}

function openModal({ title, fields, submitLabel, onSubmit, onChange, intro }) {
    modal.innerHTML = `
        <h3>${escapeHtml(title)}</h3>
        ${intro ? `<div class="hint-text" style="margin-top:0">${escapeHtml(intro)}</div>` : ""}
        <form id="modalForm">
            ${fields.map(fieldHtml).join("")}
            <div class="modal-note" id="modalNote"></div>
            <div class="modal-error" id="modalError"></div>
            <div class="modal-actions">
                <button type="button" class="btn btn-secondary" id="modalCancel">Cancel</button>
                <button type="submit" class="btn">${escapeHtml(submitLabel || "Save")}</button>
            </div>
        </form>`;
    overlay.classList.add("open");
    document.getElementById("modalCancel").addEventListener("click", closeModal);
    const note = document.getElementById("modalNote");
    const error = document.getElementById("modalError");
    const form = document.getElementById("modalForm");

    if (onChange) {
        let timer = null;
        form.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(async () => {
                try {
                    note.textContent = (await onChange(readFields(fields))) || "";
                } catch (err) {
                    note.textContent = "";
                }
            }, 250);
        });
    }

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        error.textContent = "";
        try {
            await onSubmit(readFields(fields));
            closeModal();
            await load();
        } catch (err) {
            error.textContent = err.message;
        }
    });
}

// ------------------------------------------------------------- check-ins ----

function actionButtons(editAction, deleteAction, id, { canDelete = true, deleteTitle = "Delete" } = {}) {
    return `<div class="row-actions">
        <button class="icon-btn" title="Edit" data-action="${editAction}" data-id="${id}"><i class="fa-solid fa-pen"></i></button>
        <button class="icon-btn danger" title="${escapeHtml(deleteTitle)}" data-action="${deleteAction}" data-id="${id}" ${canDelete ? "" : "disabled"}><i class="fa-solid fa-trash"></i></button>
    </div>`;
}

function renderCheckIns(checkIns) {
    const el = document.getElementById("checkIns");
    if (!checkIns.length) {
        el.innerHTML = `<div class="hint-text"><i class="fa-solid fa-circle-info"></i> No check-ins yet.</div>`;
        return;
    }
    const loans = loanViews().map((v) => v.loan);
    const cell = (entry, key) => `<td class="num">${entry.balances[key] === undefined ? "&ndash;" : lakhs(entry.balances[key])}</td>`;
    const loanHeads = loans.map((l) => `<th class="num">${escapeHtml(l.name)} outstanding</th>`).join("");
    let html = `<table><thead><tr><th>Date</th><th class="num">Cash</th><th class="num">Equity</th><th class="num">Debt</th>${loanHeads}<th>Note</th><th></th></tr></thead><tbody>`;
    checkIns.forEach((entry) => {
        const loanCells = loans.map((l) => cell(entry, `loan_${l.id}`)).join("");
        html += `<tr><td>${escapeHtml(entry.as_of)}</td>${cell(entry, "cash")}${cell(entry, "equity")}${cell(entry, "debt")}${loanCells}<td>${escapeHtml(entry.note || "")}</td><td>${actionButtons("edit-checkin", "delete-checkin", escapeHtml(entry.as_of))}</td></tr>`;
    });
    el.innerHTML = html + "</tbody></table>";
}

function openCheckIn(group) {
    const editing = !!group;
    const loans = loanViews().map((v) => v.loan);
    const value = (key) => (editing ? group.balances[key] : undefined);
    const fields = [
        { name: "as_of", label: "Date", type: "date", value: editing ? group.as_of : todayLocal(), max: todayLocal(), required: true },
        { name: "cash_lakhs", label: "Cash (Lakhs)", type: "number", value: value("cash") },
        { name: "equity_lakhs", label: "Equity portfolio (Lakhs)", type: "number", value: value("equity") },
        { name: "debt_lakhs", label: "Debt portfolio (Lakhs)", type: "number", value: value("debt") },
    ];
    loans.forEach((loan) => {
        fields.push({ name: `loan_${loan.id}`, label: `${loan.name} outstanding (Lakhs)`, type: "number", value: value(`loan_${loan.id}`) });
    });
    fields.push({ name: "note", label: "Note (optional)", type: "text", value: editing ? group.note : undefined });

    // The form has one field per loan; the API takes them as {loan id: balance}.
    const payload = (v) => {
        const body = { ...v, loan_balances: {} };
        loans.forEach((loan) => {
            const key = `loan_${loan.id}`;
            if (body[key] !== null && body[key] !== undefined) body.loan_balances[loan.id] = body[key];
            delete body[key];
        });
        return body;
    };
    openModal({
        title: editing ? "Edit check-in" : "New check-in",
        intro: editing ? "Clear a balance to remove it from this check-in." : "Fill only the balances you know today.",
        fields,
        submitLabel: editing ? "Save changes" : "Save check-in",
        onSubmit: (v) => (editing
            ? api("PUT", "/api/journal/check-in", { ...payload(v), ids: group.ids })
            : api("POST", "/api/journal/check-in", payload(v))),
    });
}

// ----------------------------------------------------------------- loans ----

// What the projection is doing about loans while none is set up, built only
// from what the assumptions actually say (their values come from /api/loans).
function legacyLoanNotice(legacy) {
    const emi = Number(legacy.annual_emi_lakhs);
    const outstanding = Number(legacy.loan_outstanding_lakhs);
    const endYear = Number(legacy.loan_end_year);
    const hasEndYear = Number.isFinite(endYear) && endYear > 0;
    const pitch = "Add them to track interest vs principal, prepayments and the payoff date.";
    if (!(Number.isFinite(emi) && emi > 0)) return `No loan is set up. ${pitch}`;
    if (hasEndYear && endYear < new Date().getFullYear()) {
        return `No loan is set up, and the flat EMI in your assumptions ended in ${endYear}, so the projection has no loan payments. ${pitch}`;
    }
    let text = `No loan is set up, so the projection assumes a flat EMI of ${lakhs(emi)} L a year`;
    if (hasEndYear) text += ` until ${endYear}`;
    if (Number.isFinite(outstanding) && outstanding > 0) text += ` (outstanding ${lakhs(outstanding)} L)`;
    return `${text}. ${pitch}`;
}

function renderLoanActions(data) {
    const el = document.getElementById("loanActions");
    const first = !data.loans.length;
    el.innerHTML = `<button class="btn" data-action="loan-setup"><i class="fa-solid fa-plus"></i> ${first ? "Set up loan" : "Add loan"}</button>`;
}

function combinedStrip(combined) {
    return `<div class="stat-grid">
        <div class="stat"><div class="stat-label">All loans: balance now</div><div class="stat-value">${lakhs(combined.current_balance)} L</div></div>
        <div class="stat"><div class="stat-label">All loans: monthly EMI</div><div class="stat-value">${lakhs(combined.monthly_emi)} L</div></div>
        <div class="stat"><div class="stat-label">Last loan clears</div><div class="stat-value">${escapeHtml(combined.payoff_month || "never")}</div></div>
        <div class="stat"><div class="stat-label">Interest still to pay</div><div class="stat-value">${lakhs(combined.total_interest_remaining)} L</div></div>
    </div>`;
}

function loanBlock(view) {
    const id = view.loan.id;
    const s = view.summary;
    const open = expandedLoans.has(id);
    const drifts = view.timeline.filter((row) => row.drift !== null && row.drift !== undefined && Math.abs(row.drift) >= 0.005);
    const summary = `${lakhs(s.current_balance)} L left &middot; EMI ${lakhs(s.monthly_emi)} L a month &middot; clears ${escapeHtml(s.payoff_month || "never")}`;
    const flag = drifts.length ? ` <span class="tag tag-approx" title="A statement balance differs from the computed schedule">statement differs</span>` : "";
    let html = `<div class="loan-block">
        <button type="button" class="loan-toggle${open ? " expanded" : ""}" data-action="toggle-loan" data-loan-id="${id}" aria-expanded="${open}">
            <span class="chevron">&#9656;</span>
            <span class="loan-title"><i class="fa-solid fa-landmark"></i> ${escapeHtml(view.loan.name)}</span>
            <span class="loan-summary">${summary}${flag}</span>
        </button>
        <div class="loan-body${open ? "" : " hidden"}" id="loanBody${id}">
        <div class="card-actions loan-body-actions">
            <button class="btn" data-action="prepayment" data-loan-id="${id}"><i class="fa-solid fa-bolt"></i> Add prepayment</button>
            <button class="btn btn-secondary" data-action="terms" data-loan-id="${id}"><i class="fa-solid fa-percent"></i> Rate / EMI changed</button>
            <button class="btn btn-secondary" data-action="statement" data-loan-id="${id}"><i class="fa-solid fa-file-invoice"></i> Update from statement</button>
        </div>
        <div class="stat-grid">
            <div class="stat"><div class="stat-label">Balance now</div><div class="stat-value">${lakhs(s.current_balance)} L</div></div>
            <div class="stat"><div class="stat-label">Payoff</div><div class="stat-value">${escapeHtml(s.payoff_month || "never")}</div></div>
            <div class="stat"><div class="stat-label">Months left</div><div class="stat-value">${s.months_remaining === null ? "&ndash;" : s.months_remaining}</div></div>
            <div class="stat"><div class="stat-label">Interest still to pay</div><div class="stat-value">${lakhs(s.total_interest_remaining)} L</div></div>
            <div class="stat"><div class="stat-label">Prepayments planned/made ahead</div><div class="stat-value">${lakhs(s.total_prepaid_remaining)} L</div></div>
        </div>
        <div class="hint-text"><i class="fa-solid fa-circle-info"></i> Balance last confirmed ${escapeHtml(s.balance_as_of)} (${lakhs(s.balance_observed)} L). Paying more than the EMI always goes to principal, so a prepayment shortens the tenure and the EMI stays as set.</div>`;
    drifts.forEach((row) => {
        html += `<div class="notice">${escapeHtml(row.month)}: the schedule computed ${lakhs(row.closing + row.drift)} L but your statement says ${lakhs(row.closing)} L (difference ${lakhs(row.drift)} L). A prepayment or rate change may be missing.</div>`;
    });

    html += `<h4>Terms history</h4><div class="journal-table table-container"><table><thead><tr><th>Effective from</th><th class="num">Rate %</th><th class="num">EMI (L / month)</th><th>Note</th><th></th></tr></thead><tbody>`;
    view.terms.forEach((t) => {
        const only = view.terms.length === 1;
        const actions = actionButtons("edit-terms", "delete-terms", t.id, { canDelete: !only, deleteTitle: only ? "The loan's only terms record can't be deleted" : "Delete" });
        html += `<tr><td>${escapeHtml(t.effective_from)}</td><td class="num">${lakhs(t.annual_rate_pct)}</td><td class="num">${lakhs(t.emi_lakhs)}</td><td>${escapeHtml(t.note || "")}</td><td>${actions}</td></tr>`;
    });
    html += `</tbody></table></div>`;

    const expanded = expandedSchedules.has(id);
    const rows = expanded ? view.schedule : view.schedule.slice(0, SCHEDULE_PREVIEW_ROWS);
    html += `<h4>Schedule</h4><div class="hint-text" style="margin-top:0"><i class="fa-solid fa-circle-info"></i> Computed from the terms, balances and prepayments - change those to change it.</div><div class="journal-table table-container"><table><thead><tr><th>Month</th><th class="num">Opening</th><th class="num">Interest</th><th class="num">Principal</th><th class="num">EMI</th><th class="num">Prepaid</th><th class="num">Closing</th></tr></thead><tbody>`;
    rows.forEach((r) => {
        html += `<tr><td>${escapeHtml(r.month)}</td><td class="num">${lakhs(r.opening)}</td><td class="num">${lakhs(r.interest)}</td><td class="num">${lakhs(r.principal)}</td><td class="num">${lakhs(r.emi_paid)}</td><td class="num">${r.prepayment ? lakhs(r.prepayment) : "&ndash;"}</td><td class="num">${lakhs(r.closing)}</td></tr>`;
    });
    html += `</tbody></table></div>`;
    if (view.schedule.length > SCHEDULE_PREVIEW_ROWS) {
        html += `<div class="card-actions" style="justify-content:flex-start;margin-top:8px"><button class="btn btn-secondary" data-action="toggle-schedule" data-loan-id="${id}">${expanded ? "Show fewer months" : `Show all ${view.schedule.length} months`}</button></div>`;
    }
    return html + `</div></div>`;
}

function renderLoans(data) {
    renderLoanActions(data);
    const el = document.getElementById("loanSection");
    if (!data.loans.length) {
        el.innerHTML = `<div class="notice info">${legacyLoanNotice(data.legacy || {})}</div>`;
        return;
    }
    let html = "";
    if (data.combined) html += combinedStrip(data.combined);
    html += data.loans.map(loanBlock).join("");
    el.innerHTML = html;
}

function currentTerms(view) {
    const terms = (view && view.terms) || [];
    return terms.length ? terms[terms.length - 1] : {};
}

function openLoanSetup() {
    const existing = loanViews().length;
    const legacy = (lastLoans && lastLoans.legacy) || {};
    // The flat-EMI assumptions describe the loans in total, so they only seed the first loan.
    const seed = existing === 0;
    const monthlyEmi = seed && legacy.annual_emi_lakhs ? Math.round((legacy.annual_emi_lakhs / 12) * 10000) / 10000 : null;
    openModal({
        title: existing ? "Add another loan" : "Set up loan",
        intro: "Enter the balance your bank shows today, the annual interest rate, and either the monthly EMI or the months remaining (the other is worked out).",
        fields: [
            { name: "name", label: "Name", type: "text", value: existing ? `Loan ${existing + 1}` : "Home loan", required: true },
            { name: "outstanding_lakhs", label: "Outstanding principal (Lakhs)", type: "number", value: seed ? legacy.loan_outstanding_lakhs : undefined, required: true },
            { name: "as_of", label: "As of", type: "date", value: todayLocal(), max: todayLocal(), required: true },
            { name: "annual_rate_pct", label: "Annual interest rate (%)", type: "number", required: true },
            { name: "emi_lakhs", label: "Monthly EMI (Lakhs)", type: "number", step: "0.0001", value: monthlyEmi },
            { name: "remaining_months", label: "Or months remaining", type: "number", step: "1" },
        ],
        submitLabel: "Create loan",
        onSubmit: (v) => api("POST", "/api/loans", v),
    });
}

function openTermsChange(loanId, existing) {
    const editing = !!existing;
    const view = loanViews().find((v) => v.loan.id === Number(loanId));
    const now = editing ? existing : currentTerms(view);
    const fields = [
        { name: "effective_from", label: "Effective from", type: "date", value: editing ? existing.effective_from : todayLocal(), required: true },
        { name: "annual_rate_pct", label: "Annual interest rate (%)", type: "number", value: now.annual_rate_pct, required: true },
        { name: "emi_lakhs", label: "Monthly EMI (Lakhs)", type: "number", step: "0.0001", value: now.emi_lakhs, required: true },
    ];
    if (!editing) fields.push({ name: "statement_balance_lakhs", label: "Statement balance on that date (optional)", type: "number" });
    fields.push({ name: "note", label: "Note (optional)", type: "text", value: editing ? existing.note : undefined });
    openModal({
        title: `${editing ? "Edit rate / EMI" : "Rate / EMI changed"} - ${loanName(loanId)}`,
        intro: "Applies from the month of the effective date. This does not change the recorded balance"
            + (editing ? "." : " unless you also enter the statement balance."),
        fields,
        submitLabel: editing ? "Save changes" : "Save",
        onSubmit: (v) => (editing ? api("PUT", `/api/loan-terms/${existing.id}`, v) : api("POST", `/api/loans/${loanId}/terms`, v)),
    });
}

function openStatement(loanId) {
    openModal({
        title: `Update from statement - ${loanName(loanId)}`,
        intro: "The balance your bank shows. The schedule restarts from it, and any difference from the computed balance is shown.",
        fields: [
            { name: "as_of", label: "As of", type: "date", value: todayLocal(), max: todayLocal(), required: true },
            { name: "outstanding_lakhs", label: "Outstanding principal (Lakhs)", type: "number", required: true },
            { name: "note", label: "Note (optional)", type: "text" },
        ],
        onSubmit: (v) => api("POST", `/api/loans/${loanId}/statement`, v),
    });
}

function openPrepayment(loanId, existing) {
    const editing = !!existing;
    openModal({
        title: `${editing ? "Edit prepayment" : "Add prepayment"} - ${loanName(loanId)}`,
        intro: "Paid from your cash. It reduces the principal and shortens the tenure; the EMI stays the same.",
        fields: [
            { name: "entry_date", label: "Date", type: "date", value: editing ? existing.entry_date : todayLocal(), max: todayLocal(), required: true },
            { name: "amount_lakhs", label: "Amount (Lakhs)", type: "number", value: editing ? existing.amount_lakhs : undefined, required: true },
            { name: "note", label: "Note (optional)", type: "text", value: editing ? existing.note : undefined },
        ],
        submitLabel: editing ? "Save changes" : "Add prepayment",
        // The what-if preview would count an edited prepayment twice, so it is for new ones only.
        onChange: editing ? undefined : async (v) => {
            if (!v.entry_date || !v.amount_lakhs) return "";
            const p = await api("POST", `/api/loans/${loanId}/prepayment-preview`, { entry_date: v.entry_date, amount_lakhs: v.amount_lakhs });
            if (!p.payoff_month_after) return "This would not clear the loan.";
            const months = p.months_saved === null ? "" : `${p.months_saved} month(s) earlier, `;
            return `Loan clears ${p.payoff_month_after} instead of ${p.payoff_month_before}: ${months}saving about ${lakhs(p.interest_saved)} L interest.`;
        },
        onSubmit: (v) => (editing
            ? api("PUT", `/api/journal/entries/${existing.id}`, { entry_date: v.entry_date, amount_lakhs: v.amount_lakhs, note: v.note })
            : api("POST", `/api/loans/${loanId}/prepayments`, { entry_date: v.entry_date, amount_lakhs: v.amount_lakhs, note: v.note })),
    });
}

// --------------------------------------------------------------- entries ----

const KIND_LABELS = {
    income: "Extra income",
    prepayment: "Loan prepayment",
    invest: "Invest (cash \u2192 investment)",
    withdraw: "Withdraw (investment \u2192 cash)",
};

function entryDetail(entry) {
    if (entry.kind === "invest" || entry.kind === "withdraw") {
        return `${escapeHtml(entry.from_bucket)} \u2192 ${escapeHtml(entry.to_bucket)}`;
    }
    if (entry.kind === "prepayment") return `&middot; ${escapeHtml(loanName(entry.ref_id))}`;
    return "";
}

function renderEntries(entries) {
    const el = document.getElementById("entries");
    if (!entries.length) {
        el.innerHTML = `<div class="hint-text"><i class="fa-solid fa-circle-info"></i> No entries yet.</div>`;
        return;
    }
    let html = `<table><thead><tr><th>Date</th><th>Type</th><th class="num">Amount (L)</th><th>Status</th><th>Note</th><th></th></tr></thead><tbody>`;
    entries.forEach((entry) => {
        const label = KIND_LABELS[entry.kind] || escapeHtml(entry.kind);
        let status = "";
        if (entry.kind === "income") {
            status = `<span class="tag tag-${escapeHtml(entry.status)}">${escapeHtml(entry.status)}</span>`;
        }
        const approx = entry.approximate ? ` <span class="tag tag-approx" title="Only the year was known; placed mid-year.">date approximate</span>` : "";
        let actions = "";
        if (entry.needs_confirmation) {
            actions += `<span class="hint-text" style="margin:0 6px 0 0">Did this happen?</span><button class="btn-link" data-action="confirm-entry" data-id="${entry.id}">Mark received</button>`;
        }
        actions += actionButtons("edit-entry", "delete-entry", entry.id);
        html += `<tr class="${entry.needs_confirmation ? "needs-confirmation" : ""}"><td>${escapeHtml(entry.entry_date)}${approx}</td><td>${label} ${entryDetail(entry)}</td><td class="num">${lakhs(entry.amount_lakhs)}</td><td>${status}</td><td>${escapeHtml(entry.note || "")}</td><td>${actions}</td></tr>`;
    });
    el.innerHTML = html + "</tbody></table>";
}

function openIncome(existing) {
    const editing = !!existing;
    openModal({
        title: editing ? "Edit extra income" : "Add extra income",
        intro: "A future date is a plan and feeds the projection; a past date should be marked received.",
        fields: [
            { name: "entry_date", label: "Date", type: "date", value: editing ? existing.entry_date : todayLocal(), required: true },
            { name: "amount_lakhs", label: "Amount (Lakhs)", type: "number", value: editing ? existing.amount_lakhs : undefined, required: true },
            { name: "status", label: "Status", type: "select", value: editing ? existing.status : "planned", options: [["planned", "Planned"], ["received", "Received"]] },
            { name: "note", label: "Note (optional)", type: "text", value: editing ? existing.note : undefined },
        ],
        submitLabel: editing ? "Save changes" : "Save",
        onSubmit: (v) => (editing ? api("PUT", `/api/journal/entries/${existing.id}`, v) : api("POST", "/api/journal/income", v)),
    });
}

function openTransfer(kind, existing) {
    const editing = !!existing;
    const invest = kind === "invest";
    const currentBucket = editing ? (invest ? existing.to_bucket : existing.from_bucket) : "equity";
    openModal({
        title: `${editing ? "Edit: " : ""}${invest ? "Invest from cash" : "Withdraw to cash"}`,
        intro: invest
            ? "Moves money out of cash into the chosen investment. Your next check-in overrides any drift."
            : "Moves money from the chosen investment back into cash.",
        fields: [
            { name: "entry_date", label: "Date", type: "date", value: editing ? existing.entry_date : todayLocal(), max: todayLocal(), required: true },
            { name: "amount_lakhs", label: "Amount (Lakhs)", type: "number", value: editing ? existing.amount_lakhs : undefined, required: true },
            { name: "bucket", label: invest ? "Into" : "From", type: "select", value: currentBucket, options: [["equity", "Equity"], ["debt", "Debt"]] },
            { name: "note", label: "Note (optional)", type: "text", value: editing ? existing.note : undefined },
        ],
        submitLabel: editing ? "Save changes" : "Save",
        onSubmit: (v) => (editing
            ? api("PUT", `/api/journal/entries/${existing.id}`, v)
            : api("POST", "/api/journal/transfer", { ...v, kind })),
    });
}

function openEntryEditor(entry) {
    if (entry.kind === "income") openIncome(entry);
    else if (entry.kind === "prepayment") openPrepayment(entry.ref_id, entry);
    else openTransfer(entry.kind, entry);
}

// ---------------------------------------------------------------- wiring ----

document.addEventListener("click", async (e) => {
    const target = e.target.closest("[data-action]");
    if (!target) return;
    const id = target.dataset.id;
    const entry = () => lastJournal.entries.find((x) => String(x.id) === id);
    const group = () => lastJournal.check_ins.find((x) => x.as_of === id);
    const loanId = target.dataset.loanId;
    const terms = () => loanViews().flatMap((v) => v.terms).find((x) => String(x.id) === id);
    try {
        switch (target.dataset.action) {
            case "loan-setup": openLoanSetup(); break;
            case "prepayment": openPrepayment(loanId); break;
            case "terms": openTermsChange(loanId); break;
            case "statement": openStatement(loanId); break;
            case "toggle-loan": {
                const key = Number(loanId);
                const open = !expandedLoans.has(key);
                if (open) expandedLoans.add(key); else expandedLoans.delete(key);
                target.classList.toggle("expanded", open);
                target.setAttribute("aria-expanded", String(open));
                document.getElementById(`loanBody${key}`).classList.toggle("hidden", !open);
                break;
            }
            case "toggle-schedule": {
                const key = Number(loanId);
                if (expandedSchedules.has(key)) expandedSchedules.delete(key); else expandedSchedules.add(key);
                renderLoans(lastLoans);
                break;
            }
            case "edit-checkin": openCheckIn(group()); break;
            case "edit-terms": { const record = terms(); openTermsChange(record.loan_id, record); break; }
            case "edit-entry": openEntryEditor(entry()); break;
            case "delete-checkin":
                if (confirm(`Delete the check-in of ${id}? It stays in the history but no longer counts.`)) {
                    await api("POST", "/api/journal/check-in/void", { ids: group().ids }); await load();
                }
                break;
            case "delete-terms":
                if (confirm("Delete this rate / EMI record?")) { await api("POST", `/api/loan-terms/${id}/void`); await load(); }
                break;
            case "delete-entry":
                if (confirm("Delete this entry? It stays in the history but no longer counts.")) {
                    await api("POST", `/api/journal/entries/${id}/void`); await load();
                }
                break;
            case "confirm-entry":
                await api("POST", `/api/journal/entries/${id}/status`, { status: "received" }); await load();
                break;
        }
    } catch (err) {
        alert(err.message);
    }
});

document.getElementById("newCheckInBtn").addEventListener("click", () => openCheckIn());
document.getElementById("addIncomeBtn").addEventListener("click", () => openIncome());
document.getElementById("investBtn").addEventListener("click", () => openTransfer("invest"));
document.getElementById("withdrawBtn").addEventListener("click", () => openTransfer("withdraw"));

async function load() {
    const [journalData, loanData] = await Promise.all([api("GET", "/api/journal"), api("GET", "/api/loans")]);
    lastJournal = journalData;
    lastLoans = loanData;
    renderCheckIns(journalData.check_ins);
    renderLoans(loanData);
    renderEntries(journalData.entries);
}

load().catch((err) => {
    document.getElementById("loanSection").innerHTML = `<div class="notice">${escapeHtml(err.message)}</div>`;
});
