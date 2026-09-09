/**
 * /console/oee — end-of-shift OEE entry.
 *
 * Convenience only. Every rule enforced here is enforced again server-side in
 * app/routers/console_oee.py, which rejects a contradictory submission with a
 * per-machine message — so the form still behaves correctly with JS disabled,
 * it is just more typing.
 *
 * What this adds:
 *   - a live summary on each machine's collapsed <details>, so the page can be
 *     scanned without opening 34 disclosures
 *   - mutual exclusion between "no downtime" and any ticked reason
 *   - a minutes box that follows its checkbox, in both directions
 *   - a running total per machine, warning past one shift's worth
 *   - a "tick no downtime on every untouched machine" shortcut
 */

const SHIFT_MINUTES = window.SHIFT_MINUTES || 480;

/** Every reason row inside one machine's downtime cell. */
function reasonRows(cell) {
    return Array.from(cell.querySelectorAll(".dt-reason-row"));
}

function tickedRows(cell) {
    return reasonRows(cell).filter((row) => row.querySelector(".dt-reason-box").checked);
}

function totalMinutes(cell) {
    return tickedRows(cell).reduce((sum, row) => {
        const value = parseInt(row.querySelector(".dt-min").value, 10);
        return sum + (Number.isFinite(value) ? value : 0);
    }, 0);
}

/**
 * Rewrites the collapsed summary so the state of a machine is readable without
 * expanding it. Mirrors the wording the server renders on first load.
 */
function refreshSummary(cell) {
    const summary = cell.querySelector(".dt-summary");
    const noneBox = cell.querySelector(".dt-none-box");
    if (!summary) return;

    const ticked = tickedRows(cell);
    const minutes = totalMinutes(cell);

    if (noneBox && noneBox.checked) {
        summary.textContent = "No downtime";
    } else if (ticked.length) {
        summary.textContent =
            `${ticked.length} reason${ticked.length === 1 ? "" : "s"}` +
            (minutes ? ` · ${minutes} min` : "");
    } else {
        summary.textContent = "Not entered";
    }

    // Over a full shift is impossible, and the server rejects it. Flagging it
    // here saves a round trip through 34 machines to find the one that's wrong.
    const over = minutes > SHIFT_MINUTES;
    summary.classList.toggle("is-over", over);
    if (over) {
        summary.textContent += ` — over ${SHIFT_MINUTES} min`;
    }
}

/**
 * A ticked reason needs its minutes box, and a minutes value implies its
 * reason. Keeping the two in step both ways means the person can work from
 * either control, which matters when they're copying off a paper sheet that
 * only lists the causes that happened.
 */
function wireCell(cell) {
    const noneBox = cell.querySelector(".dt-none-box");

    reasonRows(cell).forEach((row) => {
        const box = row.querySelector(".dt-reason-box");
        const minutes = row.querySelector(".dt-min");

        box.addEventListener("change", () => {
            if (box.checked) {
                // "No downtime" and a reason are contradictory statements about
                // the same shift, so ticking one clears the other rather than
                // leaving the server to reject the pair.
                if (noneBox) noneBox.checked = false;
                minutes.focus();
            } else {
                minutes.value = "";
            }
            refreshSummary(cell);
        });

        minutes.addEventListener("input", () => {
            if (minutes.value) {
                box.checked = true;
                if (noneBox) noneBox.checked = false;
            }
            refreshSummary(cell);
        });
    });

    if (noneBox) {
        noneBox.addEventListener("change", () => {
            if (noneBox.checked) {
                reasonRows(cell).forEach((row) => {
                    row.querySelector(".dt-reason-box").checked = false;
                    row.querySelector(".dt-min").value = "";
                });
            }
            refreshSummary(cell);
        });
    }

    refreshSummary(cell);
}

document.querySelectorAll(".dt-cell").forEach(wireCell);

/**
 * Ticks "no downtime" on every machine that has nothing entered yet.
 *
 * Deliberately skips machines that already have reasons or an existing
 * submission, so it can't wipe work already done — it only fills in the
 * "these all ran clean" majority, which is the tedious part of the job.
 */
const markAll = document.getElementById("mark-all-clean");
if (markAll) {
    markAll.addEventListener("click", () => {
        let filled = 0;
        document.querySelectorAll(".dt-cell").forEach((cell) => {
            const noneBox = cell.querySelector(".dt-none-box");
            if (!noneBox || noneBox.checked) return;
            if (tickedRows(cell).length) return; // already has downtime recorded
            noneBox.checked = true;
            refreshSummary(cell);
            filled += 1;
        });
        markAll.textContent =
            filled === 0
                ? "Every machine already has downtime entered"
                : `Ticked “no downtime” on ${filled} machine${filled === 1 ? "" : "s"}`;
    });
}

/**
 * Keeps the shift toggle's links pointing at whatever is in the Date box, so
 * switching shifts after changing the date doesn't snap the date back to the
 * one the page loaded with. Convenience only — the date that gets saved is
 * always the one in the box, which posts with the form.
 */
const dateInput = document.getElementById("entry-date");
if (dateInput) {
    dateInput.addEventListener("change", () => {
        document.querySelectorAll(".shift-option").forEach((link) => {
            const url = new URL(link.href);
            url.searchParams.set("entry_date", dateInput.value);
            link.href = url.toString();
        });
    });
}
