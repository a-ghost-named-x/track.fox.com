/**
 * /supervisor — historical shift review.
 *
 * Fetches one whole production date at a time (all 12 time slots) from
 * /api/supervisor-data, then switches between shifts entirely client-side by
 * showing and hiding columns. Only changing the DATE hits the network; the
 * shift toggle never does, which is what makes it feel instant.
 *
 * Deliberately does NOT poll, unlike dashboard.js — a fixed past date has
 * nothing to poll for. See the module docstring in app/routers/supervisor.py
 * for the full list of differences from the live dashboard.
 */

const ALL_DAY = window.ALL_DAY_LABEL;
const SHIFT_SLOTS = window.SHIFT_SLOTS;
const TIME_SLOTS = window.TIME_SLOTS;
const SHIFT_ORDER = window.SHIFT_ORDER;

// How many recent dates get a one-click button. Enough to cover "yesterday"
// and "sometime last week" without the row wrapping across the page; anything
// older is a calendar trip.
const QUICK_PICK_COUNT = 7;

const container = document.getElementById("supervisor");
const dateInput = document.getElementById("review-date");
const quickPicksEl = document.getElementById("quick-picks");
const shiftToggle = document.getElementById("shift-toggle");
const shiftNote = document.getElementById("shift-note");
const emptyNotice = document.getElementById("supervisor-empty");
const zoneSections = document.getElementById("zone-sections");
const loadedAt = document.getElementById("loaded-at");

let payload = null;
let currentShift =
    SHIFT_ORDER.includes(window.INITIAL_SHIFT) || window.INITIAL_SHIFT === ALL_DAY
        ? window.INITIAL_SHIFT
        : SHIFT_ORDER[0];

/**
 * Parses "2026-09-01" into a LOCAL-midnight Date.
 *
 * Deliberately not `new Date(iso)`, which treats a bare YYYY-MM-DD as UTC
 * midnight — west of Greenwich that renders as the previous day, so every
 * date on the page would silently read one off. Only used for display
 * formatting; the ISO string itself stays the source of truth everywhere else.
 */
function parseISODate(iso) {
    const [year, month, day] = iso.split("-").map(Number);
    return new Date(year, month - 1, day);
}

function formatDate(iso) {
    return parseISODate(iso).toLocaleDateString("en-US", {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric",
    });
}

function formatQuickPick(iso) {
    return parseISODate(iso).toLocaleDateString("en-US", {
        weekday: "short",
        month: "numeric",
        day: "numeric",
    });
}

function slotsFor(shift) {
    return shift === ALL_DAY ? TIME_SLOTS : SHIFT_SLOTS[shift] || [];
}

// Shows only the selected shift's slot columns (header + cells), hiding the
// rest — the same approach dashboard.js uses, since both grids render all 12
// columns server-side and narrow down with CSS.
function applyActiveSlots(slots) {
    const activeSet = new Set(slots);
    document.querySelectorAll("[data-slot]").forEach((el) => {
        el.classList.toggle("slot-hidden", !activeSet.has(el.getAttribute("data-slot")));
    });
}

/**
 * Keeps the URL in step with the controls so a particular view can be
 * bookmarked or pasted to someone. replaceState rather than pushState — the
 * back button should leave /supervisor, not walk back through every shift
 * button the user happened to click.
 */
function syncUrl() {
    if (!payload) return;
    const url = `/supervisor?date=${encodeURIComponent(payload.date)}` +
        `&shift=${encodeURIComponent(currentShift)}`;
    window.history.replaceState(null, "", url);
}

function renderShiftNote() {
    // The date is the day the shift STARTED. 3rd Shift's four slots (12AM-6AM)
    // land the next morning — and the rounds file them under that morning's
    // date — so say on-screen which night this is, or the date reads as the
    // morning the crew clocked out.
    if (!payload) {
        shiftNote.hidden = true;
        return;
    }
    const nextDay = parseISODate(payload.date);
    nextDay.setDate(nextDay.getDate() + 1);
    const nextLabel = nextDay.toLocaleDateString("en-US", {
        weekday: "short", month: "short", day: "numeric",
    });
    if (currentShift === "3rd Shift") {
        shiftNote.textContent =
            `3rd Shift of ${formatDate(payload.date)} runs 10PM that evening through 6AM ${nextLabel}. ` +
            "(On the 2-hour rounds those four readings are entered under the morning's date.)";
        shiftNote.hidden = false;
    } else if (currentShift === ALL_DAY) {
        shiftNote.textContent =
            `The production day: 6AM ${formatDate(payload.date)} through 6AM ${nextLabel}.`;
        shiftNote.hidden = false;
    } else {
        shiftNote.hidden = true;
    }
}

function renderQuickPicks(dates) {
    quickPicksEl.innerHTML = "";
    dates.slice(0, QUICK_PICK_COUNT).forEach((iso) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "quick-pick";
        button.dataset.date = iso;
        button.textContent = formatQuickPick(iso);
        button.classList.toggle("is-active", payload && iso === payload.date);
        quickPicksEl.appendChild(button);
    });
}

function clearGrid() {
    document.querySelectorAll(".review-grid .cell").forEach((cell) => {
        cell.innerHTML = '<span class="cell-value">—</span>';
        cell.removeAttribute("data-status");
        cell.removeAttribute("data-has-issue");
    });
    document.querySelectorAll(".review-grid .operator").forEach((el) => {
        el.textContent = "";
    });
    document.querySelectorAll(".review-grid tbody .issue-col").forEach((cell) => {
        cell.textContent = "—";
        cell.classList.remove("has-issue");
    });
}

function fillGrid() {
    clearGrid();
    if (!payload) return;

    // Every slot is filled, not just the visible shift's — the hidden columns
    // are already correct when the toggle switches, so no refill is needed.
    for (const entry of payload.entries) {
        const row = document.querySelector(`.review-grid tr[data-machine="${entry.machine_id}"]`);
        if (!row) continue; // machine not in any zone's roster

        const cell = row.querySelector(`td[data-slot="${entry.time_slot}"]`);
        if (!cell) continue;

        cell.querySelector(".cell-value").textContent =
            entry.units_produced.toLocaleString("en-US");
        cell.setAttribute("data-status", entry.status);

        // Corner flag on the slot whose entry reported an issue. Marked on
        // all 12 slots, not just the visible shift's, which is what makes it
        // useful in All Day mode: the Issue column is hidden there, but the
        // markers still show which slots across the whole day had trouble.
        if (entry.issue) cell.setAttribute("data-has-issue", "");
    }

    // Operator and issues are per-shift values (get_shift_activity is scoped
    // to one shift's four slots), so they can't be shown in All Day mode —
    // three shifts' worth of operators don't fit one cell. The CSS hides both
    // columns there instead of showing a misleading single value. (Now that
    // every issue line carries its own slot label, the Issue column alone
    // COULD survive All Day — it would need a 12-slot get_shift_activity()
    // call, which is a separate change and isn't made here.)
    if (currentShift === ALL_DAY) return;

    const activity = (payload.shift_activity || {})[currentShift] || {};
    for (const [machineId, machineActivity] of Object.entries(activity)) {
        const row = document.querySelector(`.review-grid tr[data-machine="${machineId}"]`);
        if (!row) continue;

        const operatorEl = row.querySelector(".operator");
        if (operatorEl) operatorEl.textContent = machineActivity.operator || "";

        // No line cap here, unlike the boards — a desk monitor scrolls, and a
        // supervisor reviewing a bad shift wants all four lines.
        const issueEl = row.querySelector(".issue-col");
        if (issueEl) renderIssueCell(issueEl, machineActivity.issues, 0);
    }
}

function render() {
    shiftToggle.querySelectorAll(".shift-option").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.shift === currentShift);
    });
    container.classList.toggle("is-all-day", currentShift === ALL_DAY);

    applyActiveSlots(slotsFor(currentShift));
    renderShiftNote();
    fillGrid();
    syncUrl();
}

function showEmpty(message) {
    emptyNotice.textContent = message;
    emptyNotice.hidden = false;
    zoneSections.hidden = true;
}

function hideEmpty() {
    emptyNotice.hidden = true;
    zoneSections.hidden = false;
}

async function load(isoDate) {
    try {
        const query = isoDate ? `?date=${encodeURIComponent(isoDate)}` : "";
        const res = await fetch(`/api/supervisor-data${query}`);
        if (!res.ok) throw new Error(`Request failed: ${res.status}`);
        payload = await res.json();

        dateInput.value = payload.date;
        if (payload.available_dates.length) {
            // available_dates is newest-first, so the ends of the range are
            // its first and last elements. Clamping here is what makes a date
            // with no possible data unpickable in the calendar itself.
            dateInput.max = payload.available_dates[0];
            dateInput.min = payload.available_dates[payload.available_dates.length - 1];
        }
        renderQuickPicks(payload.available_dates);

        if (!payload.has_any_data) {
            showEmpty("No production entries have been recorded yet.");
        } else if (!payload.entries.length) {
            showEmpty(`No entries were recorded for ${formatDate(payload.date)}.`);
        } else {
            hideEmpty();
        }

        render();
        loadedAt.textContent = `Loaded at ${new Date().toLocaleTimeString()}`;
    } catch (err) {
        loadedAt.textContent = "Could not load data — check the connection and reload.";
        console.error("Supervisor load failed:", err);
    }
}

dateInput.addEventListener("change", () => {
    if (dateInput.value) load(dateInput.value);
});

quickPicksEl.addEventListener("click", (event) => {
    const button = event.target.closest(".quick-pick");
    if (button) load(button.dataset.date);
});

shiftToggle.addEventListener("click", (event) => {
    const button = event.target.closest(".shift-option");
    if (!button) return;
    currentShift = button.dataset.shift;
    render(); // no fetch — the whole day is already loaded
});

load(window.INITIAL_DATE || null);
