// Shared HTML-safety helpers, loaded before any page script that builds
// innerHTML from dynamic data (see static/projection.js, static/dashboard.js).
//
// Any free-text value that did NOT originate from a fixed, developer-written
// string (e.g. a user-entered note, description, or category) must be passed
// through escapeHtml() before being interpolated into an HTML template
// string that gets assigned via `.innerHTML = ...` - otherwise HTML/script
// content typed into a form field gets parsed as live markup instead of
// displayed as text (stored XSS).

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
    }[c]));
}
