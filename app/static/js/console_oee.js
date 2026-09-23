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
 *   - a running total per machine, warning past one shift's worth — THAT
 *     machine's shift, which is 480, 600 or 720 minutes depending on the
 *     shift length picked for its day, or 60 x the hours on a short day
 *   - a "tick no downtime on every untouched machine" shortcut
 *   - typing in a short-day box picks the short day, and a "set every
 *     machine to N hours" shortcut for a Saturday
 */

const MAX_SHIFT_MINUTES = window.MAX_SHIFT_MINUTES || 720;
const SHIFT_INDEX = window.SHIFT_INDEX || 0;
const DAY_START_HOUR = window.DAY_START_HOUR === undefined ? 6 : window.DAY_START_HOUR;
const SHORT_PATTERN_HOURS = window.SHORT_PATTERN_HOURS || 6;
const DEFAULT_SHIFT_HOURS = window.DEFAULT_SHIFT_HOURS || 8;

/**
 * How long this machine's shift is, read off its row. The server renders it
 * from the record; the length control can change it without a reload, and
 * applyLength() keeps the attribute in step.
 */
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
    const cap = shiftMinutesFor(cell);
    const over = minutes > cap;
    summary.classList.toggle("is-over", over);
    if (over) {
        summary.textContent += ` — over ${cap} min`;
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

/** 24h hour -> "6PM", the same labels as _hour_label() in app/models.py. */
function hourLabel(hour) {
    const h = ((hour % 24) + 24) % 24;
    if (h === 0) return "12AM";
    if (h < 12) return `${h}AM`;
    if (h === 12) return "12PM";
    return `${h - 12}PM`;
}

/**
 * The clock span a length means on this page's shift — shift_span() in
 * app/models.py. The Nth shift of an L-hour pattern starts at 6AM + N x L,
 * and a short day's 1-6 all sit in the 6-hour pattern.
 */
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
    return Number.isInteger(value) && value >= 1 && value <= SHORT_PATTERN_HOURS ? value : null;
}

/**
 * Changing the length changes how many minutes of downtime fit, so the row's
 * cap, its minutes boxes' max and the span under the control follow the
 * choice immediately rather than after a save-and-reload.
 */
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
 * On the 2nd and 3rd Shift pages (rows carry data-night-crew) choosing
 * anything but an ordinary 8 hours also ticks Scheduled. A long 2nd Shift is
 * a night crew and a short day's afternoon and evening shifts usually don't
 * run, so both default to not scheduled — and choosing the length IS saying
 * this one ran. The person can untick it again; the server never assumes.
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
    // The box only counts next to the short day, and the server refuses a
    // number in it beside any other choice, so it is cleared rather than
    // left holding a value that no longer means anything.
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

    // Typing hours IS picking the short day. Deliberately on input rather
    // than focus: tabbing through the box on the way to Scheduled mustn't
    // switch a machine to a short day with no hours in it.
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
 * "Set every machine to N hours" — a short Saturday is 34 machines at the
 * same number, and typing it 34 times is how one gets missed. Fills the form
 * only; nothing is saved until Save. Machines where that length would
 * overlap a neighbouring shift are skipped and named, not forced.
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
        const hours = Number(setAllInput.value);
        const short = Number.isInteger(hours) && hours >= 1 && hours <= SHORT_PATTERN_HOURS;
        if (!short && !longValues.has(String(hours))) {
            setAllResult.textContent =
                `Type 1–${SHORT_PATTERN_HOURS}` +
                (longValues.size ? `, or ${Array.from(longValues).join(", ")}` : "") + ".";
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
            `Set ${set} machine${set === 1 ? "" : "s"} to ${hours}h — not saved until you press Save.` +
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
