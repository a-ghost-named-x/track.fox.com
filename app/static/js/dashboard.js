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

async function refreshDashboard() {
    try {
        const res = await fetch("/api/dashboard-data");
        if (!res.ok) throw new Error(`Request failed: ${res.status}`);
        const data = await res.json();

        document.getElementById("dashboard-date").textContent = data.date;
        document.getElementById("dashboard-shift").textContent = data.shift;
        applyActiveSlots(data.active_slots);

        // Clear all cells first so slots with no entry yet show as empty,
        // not a stale value from a previous poll. This also wipes any issue
        // badge from the previous poll so it doesn't double up below.
        document.querySelectorAll(".cell").forEach((cell) => {
            cell.innerHTML = '<span class="cell-value">—</span>';
            cell.removeAttribute("data-status");
        });
        document.querySelectorAll(".operator").forEach((el) => {
            el.textContent = "";
        });

        for (const entry of data.entries) {
            const row = document.querySelector(`tr[data-machine="${entry.machine_id}"]`);
            if (!row) continue; // machine not in the configured MACHINE_IDS list yet

            const cell = row.querySelector(`td[data-slot="${entry.time_slot}"]`);
            if (!cell) continue;

            // Status is conveyed by color (data-status drives the CSS), not
            // by appending ":)"/":(" text — keeps the cell to just the number.
            cell.querySelector(".cell-value").textContent = `${entry.units_produced}`;
            cell.setAttribute("data-status", entry.status);
        }

        // Operator (shown once per machine row) and carried issue (shown as
        // a badge on every active-shift cell for that machine). Backed by
        // get_shift_activity() server-side — "newest issue wins" and it
        // carries through the rest of the shift once reported, even on
        // slots logged afterward without repeating it.
        for (const [machineId, activity] of Object.entries(data.machine_activity || {})) {
            const row = document.querySelector(`tr[data-machine="${machineId}"]`);
            if (!row) continue;

            const operatorEl = row.querySelector(".operator");
            if (operatorEl) operatorEl.textContent = activity.operator || "";

            if (activity.issue) {
                row.querySelectorAll("td[data-slot]").forEach((cell) => {
                    if (!data.active_slots.includes(cell.getAttribute("data-slot"))) return;
                    const badge = document.createElement("span");
                    badge.className = "issue-badge";
                    badge.textContent = "⚠";
                    badge.title = activity.issue; // surfaces on hover; BrightSign won't show this, but useful when viewed in a browser
                    cell.appendChild(badge);
                });
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
