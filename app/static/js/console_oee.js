/**
 * /console/oee: end-of-shift OEE entry.
 *
 * Convenience only. The server enforces every rule again, so the form works
 * without JS. This adds:
 *   - a live summary on each machine's collapsed downtime picker
 *   - "no downtime" and ticked reasons clearing each other
 *   - minutes boxes that follow their checkboxes, both ways
 *   - a per-machine total that warns past that machine's shift length
 *   - a "tick no downtime on every untouched machine" button
 *   - typing short-day hours picks the short day, plus a "set every machine
 *     to N hours" button
 */

const MAX_SHIFT_MINUTES = window.MAX_SHIFT_MINUTES || 720;
const SHIFT_INDEX = window.SHIFT_INDEX || 0;
const DAY_START_HOUR = window.DAY_START_HOUR === undefined ? 6 : window.DAY_START_HOUR;
const SHORT_PATTERN_HOURS = window.SHORT_PATTERN_HOURS || 6;
// Production is read every 2 hours, so a short day is 2, 4 or 6 hours.
const SHORT_SHIFT_HOURS = window.SHORT_SHIFT_HOURS || [2, 4, 6];
const DEFAULT_SHIFT_HOURS = window.DEFAULT_SHIFT_HOURS || 8;

/** This machine's shift length in minutes, from its row (kept current by applyLength()). */
function shiftMinutesFor(cell) {
    const row = cell.closest("tr");
    const value = row ? parseInt(row.dataset.shiftMinutes, 10) : NaN;
    return Number.isFinite(value) && value > 0 ? value : MAX_SHIFT_MINUTES;
}

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

/** Rewrites the collapsed summary, matching the server's wording on first load. */
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

    // The server rejects more downtime than the shift has; flag it early.
    const cap = shiftMinutesFor(cell);
    const over = minutes > cap;
    summary.classList.toggle("is-over", over);
    if (over) {
        summary.textContent += ` — over ${cap} min`;
    }
}

/**
 * Keeps each reason's checkbox and minutes box in step, both ways, and clears
 * "no downtime" when a reason is used.
 */
function wireCell(cell) {
    const noneBox = cell.querySelector(".dt-none-box");

    reasonRows(cell).forEach((row) => {
        const box = row.querySelector(".dt-reason-box");
        const minutes = row.querySelector(".dt-min");

        box.addEventListener("change", () => {
            if (box.checked) {
                // "No downtime" and a reason contradict each other.
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

/** 24h hour -> "6PM", the same labels as _hour_label() in app/models.py. */
function hourLabel(hour) {
    const h = ((hour % 24) + 24) % 24;
    if (h === 0) return "12AM";
    if (h < 12) return `${h}AM`;
    if (h === 12) return "12PM";
    return `${h - 12}PM`;
}

/** The clock span a length means on this page's shift; see shift_span() in app/models.py. */
function spanFor(hours) {
    const pattern = hours <= SHORT_PATTERN_HOURS ? SHORT_PATTERN_HOURS : hours;
    const start = DAY_START_HOUR + SHIFT_INDEX * pattern;
    return `${hourLabel(start)}-${hourLabel(start + hours)}`;
}

/**
 * The hours a row's length control says right now, or null while the short
 * day is picked with no valid number in its box yet.
 */
function chosenHours(row) {
    const picked = row.querySelector(".hours-toggle input[type=radio]:checked");
    if (!picked) return null;
    if (picked.value !== "short") return parseInt(picked.value, 10);
    const box = row.querySelector(".hours-short");
    const value = box ? Number(box.value) : NaN;
    return SHORT_SHIFT_HOURS.includes(value) ? value : null;
}

/** Updates the row's downtime cap, minutes max and span line for a new length. */
function applyLength(row) {
    const hours = chosenHours(row);
    const spanLine = row.querySelector(".hours-span-line");
    if (hours === null) {
        if (spanLine) spanLine.hidden = true;
        return;
    }
    const minutes = hours * 60;
    row.dataset.shiftMinutes = String(minutes);
    row.querySelectorAll(".dt-min").forEach((box) => { box.max = String(minutes); });
    const cell = row.querySelector(".dt-cell");
    if (cell) refreshSummary(cell);
    if (spanLine) {
        spanLine.textContent = spanFor(hours);
        spanLine.hidden = hours === DEFAULT_SHIFT_HOURS;
    }
}

/**
 * On the 2nd and 3rd Shift pages, picking anything but 8 hours ticks
 * Scheduled, since those shifts default to not scheduled and picking a length
 * says a crew ran. It can be unticked.
 */
function tickIfCrewRan(row) {
    if (!row.dataset.nightCrew) return;
    const scheduled = row.querySelector('input[type="checkbox"][name^="scheduled_"]');
    const hours = chosenHours(row);
    if (scheduled && hours !== null && hours !== DEFAULT_SHIFT_HOURS) scheduled.checked = true;
}

/**
 * Picks `value` ("8", "10", "12" or "short") on a row, with `shortHours` in
 * the box for the short day. Returns false, touching nothing, when that
 * option isn't on the row or would overlap a neighbouring shift.
 */
function pickLength(row, value, shortHours) {
    const radio = row.querySelector(`.hours-toggle input[type=radio][value="${value}"]`);
    if (!radio || radio.disabled) return false;
    radio.checked = true;
    const box = row.querySelector(".hours-short");
    // The server rejects a number in the box beside any other choice.
    if (box) box.value = value === "short" ? String(shortHours) : "";
    applyLength(row);
    tickIfCrewRan(row);
    return true;
}

function wireLengthToggle(row) {
    const radios = row.querySelectorAll(".hours-toggle input[type=radio]");
    if (!radios.length) return;
    const shortRadio = row.querySelector('.hours-toggle input[type=radio][value="short"]');
    const shortBox = row.querySelector(".hours-short");

    radios.forEach((radio) => {
        radio.addEventListener("change", () => {
            if (!radio.checked) return;
            if (radio.value === "short") {
                if (shortBox) shortBox.focus();
            } else if (shortBox) {
                shortBox.value = "";
            }
            applyLength(row);
            tickIfCrewRan(row);
        });
    });

    // Typing hours picks the short day. On input, not focus, so tabbing
    // through doesn't switch a machine to an empty short day.
    if (shortRadio && shortBox) {
        shortBox.addEventListener("input", () => {
            if (shortBox.value && !shortRadio.disabled) shortRadio.checked = true;
            applyLength(row);
            tickIfCrewRan(row);
        });
    }
}

document.querySelectorAll("tr[data-shift-minutes]").forEach(wireLengthToggle);

/**
 * "Set every machine to N hours". Fills the form only; nothing is saved
 * until Save. Machines where that length would overlap another shift are
 * skipped and listed.
 */
const setAllInput = document.getElementById("set-all-hours");
const setAllApply = document.getElementById("set-all-apply");
const setAllResult = document.getElementById("set-all-result");
if (setAllInput && setAllApply) {
    const rows = Array.from(document.querySelectorAll("tr[data-machine]"))
        .filter((row) => row.querySelector(".hours-toggle"));
    const longValues = new Set(
        Array.from(document.querySelectorAll('.hours-toggle input[type=radio]:not([value="short"])'))
            .map((radio) => radio.value)
    );

    if (!rows.length) {
        // A 3rd Shift page where no machine's day allows a choice.
        setAllInput.closest(".set-all-hours").hidden = true;
    }

    const apply = () => {
        const hours = Number(setAllInput.value.trim());
        const short = SHORT_SHIFT_HOURS.includes(hours);
        if (!short && !longValues.has(String(hours))) {
            const allowed = [...SHORT_SHIFT_HOURS, ...longValues];
            setAllResult.textContent =
                `Type ${allowed.slice(0, -1).join(", ")} or ${allowed[allowed.length - 1]}.`;
            return;
        }
        const skipped = [];
        let set = 0;
        rows.forEach((row) => {
            if (pickLength(row, short ? "short" : String(hours), hours)) {
                set += 1;
            } else {
                skipped.push(row.dataset.machine);
            }
        });
        setAllResult.textContent =
            `Set ${set} machine${set === 1 ? "" : "s"} to ${hours}h. Not saved until you press Save.` +
            (skipped.length
                ? ` Skipped ${skipped.join(", ")}: that length would overlap another shift on their day.`
                : "");
    };

    setAllApply.addEventListener("click", apply);
    // Enter in this box would otherwise submit the whole form.
    setAllInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            apply();
        }
    });
}

/**
 * Ticks "no downtime" on every machine that has nothing entered yet. Machines
 * with reasons are left alone.
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

/** Keeps the shift toggle's links pointing at the date in the Date box. */
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
