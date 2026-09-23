/**
 * Polls /api/dashboard-data on an interval and fills the grid cells.
 * Each entry has machine_id, time_slot, units_produced, status, etc.
 * Cells are matched via data-machine / data-slot attributes set server-side
 * in dashboard.html.
 */

// Issue lines per cell before older ones collapse into "+N earlier".
const MAX_ISSUE_LINES = 3;

// Shows only the current shift's four slot columns. Runs every poll so the
// board switches shifts without a reload.
function applyActiveSlots(activeSlots) {
    const activeSet = new Set(activeSlots);
    document.querySelectorAll("[data-slot]").forEach((el) => {
        el.classList.toggle("slot-hidden", !activeSet.has(el.getAttribute("data-slot")));
    });
}

// Hides machines with no entry in the current shift. Uses machine_activity
// (current shift only) rather than the whole day's entries, so a machine
// that only reported earlier today doesn't show as a row of blanks. Right
// after a changeover the board fills in as the first entries arrive.
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

        // Clear everything first so a slot with no entry doesn't keep a
        // stale value from the previous poll.
        document.querySelectorAll(".cell").forEach((cell) => {
            cell.innerHTML = '<span class="cell-value">—</span>';
            cell.removeAttribute("data-status");
            cell.removeAttribute("data-has-issue");
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
            if (!row) continue; // machine not on this board

            const cell = row.querySelector(`td[data-slot="${entry.time_slot}"]`);
            if (!cell) continue;

            // Status is shown by colour (data-status drives the CSS).
            cell.querySelector(".cell-value").textContent = entry.units_produced.toLocaleString("en-US");
            cell.setAttribute("data-status", entry.status);

            // Corner flag on the slot that reported an issue, readable from
            // across the floor.
            if (entry.issue) cell.setAttribute("data-has-issue", "");
        }

        // Operator and the shift's issues, once per machine row.
        for (const [machineId, activity] of Object.entries(data.machine_activity || {})) {
            const row = document.querySelector(`tr[data-machine="${machineId}"]`);
            if (!row) continue;

            const operatorEl = row.querySelector(".operator");
            if (operatorEl) operatorEl.textContent = activity.operator || "";

            const issueEl = row.querySelector("td.issue-col");
            if (issueEl) renderIssueCell(issueEl, activity.issues, MAX_ISSUE_LINES);
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
