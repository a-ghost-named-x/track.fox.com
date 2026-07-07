/**
 * Polls /api/dashboard-data on an interval and fills the grid cells.
 * Each entry has machine_id, time_slot, units_produced, status, etc.
 * Cells are matched via data-machine / data-slot attributes set server-side
 * in dashboard.html.
 */

// Shows only the current shift's 4 time-slot columns (header + cells),
// hiding the other 8. Runs every poll in case the shift changes (e.g. at
// 2PM) while the page stays open on BrightSign without a reload.
function applyActiveSlots(activeSlots) {
    const activeSet = new Set(activeSlots);
    document.querySelectorAll("[data-slot]").forEach((el) => {
        el.classList.toggle("slot-hidden", !activeSet.has(el.getAttribute("data-slot")));
    });
}

// Hides machine rows with no logged entry in the shift currently in
// progress. Deliberately scoped to `machine_activity` (server-side, backed
// by get_shift_activity() -> WHERE time_slot = ANY(active_slots)) rather
// than the full-day `entries` list. Using the full day here was tried first
// and rejected: a machine logged only in an earlier shift today would still
// show, as a row with every currently-visible column blank (its one entry
// sits in a now-hidden slot) — looking like it's running with no data
// instead of just not having reported this shift. Tradeoff accepted: right
// after a shift changeover, a machine won't (re)appear until its first entry
// for the new shift lands, which can be a real gap since manual entries only
// come in ~every 2 hours — rows may be sparse for a bit right after the
// change. Re-evaluated every poll, so this resolves itself as entries land,
// without needing a page reload.
function applyMachineVisibility(machineActivity) {
    const activeMachines = new Set(Object.keys(machineActivity || {}));
    document.querySelectorAll("tr[data-machine]").forEach((row) => {
        row.classList.toggle("row-hidden", !activeMachines.has(row.getAttribute("data-machine")));
    });
}

async function refreshDashboard() {
    try {
        const res = await fetch("/api/dashboard-data");
        if (!res.ok) throw new Error(`Request failed: ${res.status}`);
        const data = await res.json();

        document.getElementById("dashboard-date").textContent = data.date;
        document.getElementById("dashboard-shift").textContent = data.shift;
        applyActiveSlots(data.active_slots);
        applyMachineVisibility(data.machine_activity);

        // Clear all cells first so slots with no entry yet show as empty,
        // not a stale value from a previous poll.
        document.querySelectorAll(".cell").forEach((cell) => {
            cell.innerHTML = '<span class="cell-value">—</span>';
            cell.removeAttribute("data-status");
        });
        document.querySelectorAll(".operator").forEach((el) => {
            el.textContent = "";
        });
        document.querySelectorAll("td.issue-col").forEach((cell) => {
            cell.textContent = "—";
            cell.classList.remove("has-issue");
        });

        for (const entry of data.entries) {
            const row = document.querySelector(`tr[data-machine="${entry.machine_id}"]`);
            if (!row) continue; // machine not in the configured MACHINE_IDS list yet

            const cell = row.querySelector(`td[data-slot="${entry.time_slot}"]`);
            if (!cell) continue;

            // Status is conveyed by color (data-status drives the CSS), not
            // by appending ":)"/":(" text — keeps the cell to just the number.
            // toLocaleString adds thousands separators (e.g. 1,234).
            cell.querySelector(".cell-value").textContent = entry.units_produced.toLocaleString("en-US");
            cell.setAttribute("data-status", entry.status);
        }

        // Operator and carried issue, both shown once per machine row (not
        // repeated per slot cell). Backed by get_shift_activity() server-side
        // — "newest issue wins" and it carries in the Issue column through
        // the rest of the shift once reported, even on slots logged
        // afterward without repeating it.
        for (const [machineId, activity] of Object.entries(data.machine_activity || {})) {
            const row = document.querySelector(`tr[data-machine="${machineId}"]`);
            if (!row) continue;

            const operatorEl = row.querySelector(".operator");
            if (operatorEl) operatorEl.textContent = activity.operator || "";

            const issueEl = row.querySelector("td.issue-col");
            if (issueEl && activity.issue) {
                issueEl.textContent = activity.issue;
                issueEl.classList.add("has-issue");
            }
        }

        document.getElementById("last-updated").textContent =
            `Last updated: ${new Date().toLocaleTimeString()}`;
    } catch (err) {
        document.getElementById("last-updated").textContent =
            "Update failed — showing last known data";
        console.error("Dashboard refresh failed:", err);
    }
}

refreshDashboard();
setInterval(refreshDashboard, window.POLL_INTERVAL_MS || 60000);
