/**
 * /oee — Overall Equipment Effectiveness by machine and shift.
 *
 * Fetches one whole production date at a time (all three shifts) from
 * /api/oee-data, then switches shifts entirely client-side. Only changing the
 * DATE hits the network, the same trade supervisor.js makes.
 *
 * Deliberately does NOT poll — see the module docstring in
 * app/routers/oee.py for why this follows /supervisor's conventions rather
 * than the live dashboard's.
 *
 * The one rule that governs every render function below: a missing input
 * renders as "—", never as a zero or a flattering default. Availability of
 * 100% and Quality of 100% are the values you get by defaulting missing
 * downtime and scrap to zero, and both are lies that would average into every
 * rollup on the page. The server already returns null for these; this file's
 * job is to not paper over it.
 */

const SHIFT_SLOTS = window.SHIFT_SLOTS;
const TIME_SLOTS = window.TIME_SLOTS;
const SHIFT_ORDER = window.SHIFT_ORDER;

// Standards sit at 75% of theoretical max, so 0.75 is "hit target exactly".
// The bands are derived from it rather than hardcoded, so if the floor ever
// revises that assumption the colours move with it.
const AT_STANDARD = window.STANDARD_PCT_OF_IDEAL || 0.75;
const WORLD_CLASS = 0.85;

const QUICK_PICK_COUNT = 7;

// Human wording for the impossible-number flags the server raises. These
// matter more than missing data: a blank cell is visible and someone chases
// it, whereas a transposed digit produces a plausible wrong number nobody
// questions.
/**
 * Hard flags: the number itself cannot be right, whatever the machine's rate
 * is, so the slot is excluded from OEE.
 *
 * Each has a short `title` naming the problem and a `detail` saying what to do
 * about it. The banner groups machines UNDER these rather than repeating the
 * text per machine — six machines with two flags each produced a solid
 * paragraph of duplicated prose that nobody would read.
 */
const FLAG_LABELS = {
    negative_units: {
        title: "Cumulative units went backwards",
        detail: "A counter cannot decrease. Check the reading against the one before it.",
    },
    negative_scrap: {
        title: "Cumulative scrap went backwards",
        detail: "A counter cannot decrease. Check the reading against the one before it.",
    },
    implausible_units: {
        title: "More than double the machine's theoretical maximum",
        detail: "Usually a transposed digit, or a whole day's total typed into a box that means one shift.",
    },
    downtime_over_slot: {
        title: "More downtime than fits in the slot",
        detail: "A 2-hour slot holds at most 120 minutes.",
    },
    baseline_suspect: {
        title: "Measured from a checkpoint that is itself wrong",
        detail: "Fix the earlier checkpoint and these slots resolve on their own.",
    },
    no_ideal_rate: {
        title: "No ideal rate seeded",
        detail: "OEE cannot be computed. Run docs/sql/08_seed_ideal_rates.sql.",
    },
};

/**
 * Soft warnings. Unlike the flags above these invalidate nothing: the slot
 * still counts and the value is shown as-is, never clamped. They mean two
 * inputs disagree, and the wrong one is usually the machine's configured rate
 * rather than what the floor counted.
 */
const WARNING_LABELS = {
    over_100: {
        title: "Beat the maximum derived from its standard, across the whole shift",
        detail: "Still counted. Either planned downtime was over-reported, or the " +
            "ideal rate is set too low. docs/sql/10_diagnose_over_ceiling.sql works out which.",
        tooltip: "Above this machine's derived maximum for the time it was scheduled. " +
            "On a single slot that is usually just a checkpoint read late or early, " +
            "so the units belong to the neighbouring slot and it evens out across " +
            "the shift. Counted either way, never clamped.",
    },
};

/** Short one-line form, for tooltips on an individual cell. */
function describeCode(code, table) {
    const entry = table[code];
    if (!entry) return code;
    return entry.tooltip || `${entry.title}. ${entry.detail}`;
}

const dateInput = document.getElementById("review-date");
const quickPicksEl = document.getElementById("quick-picks");
const shiftToggle = document.getElementById("shift-toggle");
const completenessEl = document.getElementById("completeness");
const completenessNote = document.getElementById("completeness-note");
const flagsBanner = document.getElementById("quality-flags");
const emptyNotice = document.getElementById("oee-empty");
const zoneSections = document.getElementById("zone-sections");
const paretoSection = document.getElementById("pareto-section");
const paretoEl = document.getElementById("pareto");
const paretoTotal = document.getElementById("pareto-total");
const loadedAt = document.getElementById("loaded-at");

let payload = null;
let currentShift = SHIFT_ORDER.includes(window.INITIAL_SHIFT)
    ? window.INITIAL_SHIFT
    : SHIFT_ORDER[0];

/**
 * Parses "2026-09-01" into a LOCAL-midnight Date. Deliberately not
 * `new Date(iso)`, which reads a bare YYYY-MM-DD as UTC midnight and renders
 * as the previous day west of Greenwich. Same helper as supervisor.js.
 */
function parseISODate(iso) {
    const [year, month, day] = iso.split("-").map(Number);
    return new Date(year, month - 1, day);
}

function formatDate(iso) {
    return parseISODate(iso).toLocaleDateString("en-US", {
        weekday: "short", month: "short", day: "numeric", year: "numeric",
    });
}

function formatQuickPick(iso) {
    return parseISODate(iso).toLocaleDateString("en-US", {
        weekday: "short", month: "numeric", day: "numeric",
    });
}

/** Percentage, or an em dash when the value is genuinely unknown. */
function pct(value, digits) {
    if (value === null || value === undefined) return "—";
    return `${(value * 100).toFixed(digits === undefined ? 1 : digits)}%`;
}

function count(value) {
    if (value === null || value === undefined) return "—";
    return value.toLocaleString("en-US");
}

/**
 * Which colour band an OEE value falls in. Null for unknown — an unknown OEE
 * gets no colour at all rather than borrowing the "poor" red, since "nobody
 * entered the data" and "the machine ran badly" are different findings.
 */
function bandFor(value) {
    if (value === null || value === undefined) return null;
    if (value >= WORLD_CLASS) return "excellent";
    if (value >= AT_STANDARD) return "good";
    // 0.8 of standard — 60% on the default 0.75, which is roughly the
    // industry-typical figure and a sensible floor for "needs attention".
    if (value >= AT_STANDARD * 0.8) return "fair";
    return "poor";
}

function setBand(el, value) {
    const band = bandFor(value);
    if (band) {
        el.setAttribute("data-band", band);
    } else {
        el.removeAttribute("data-band");
    }
}

function slotsFor(shift) {
    return SHIFT_SLOTS[shift] || [];
}

function applyActiveSlots(slots) {
    const active = new Set(slots);
    document.querySelectorAll("#zone-sections [data-slot]").forEach((el) => {
        el.classList.toggle("slot-hidden", !active.has(el.getAttribute("data-slot")));
    });
}

function syncUrl() {
    if (!payload) return;
    window.history.replaceState(
        null,
        "",
        `/oee?date=${encodeURIComponent(payload.date)}&shift=${encodeURIComponent(currentShift)}`
    );
}

/** Why a given slot has no OEE, phrased as the thing that's missing. */
function missingReason(slot) {
    const missing = [];
    if (!slot.has_units) missing.push("units");
    if (!slot.has_downtime) missing.push("downtime");
    if (missing.length) return `Not entered: ${missing.join(", ")}`;
    if (slot.flags && slot.flags.length) {
        return slot.flags.map((f) => describeCode(f, FLAG_LABELS)).join("\n");
    }
    if (slot.ppt_minutes === 0) return "Entire slot was planned downtime";
    return "";
}

/** Tooltip detail for one slot cell — the numbers behind the percentage. */
function slotTitle(slot) {
    const lines = [];
    // A blank checkpoint means the number hadn't moved, so the slot produced
    // zero. Saying so distinguishes an inferred zero from a typed one — the
    // maths treats them identically, but a reader shouldn't have to guess.
    if (slot.has_units && !slot.units_reported) {
        lines.push("No checkpoint entered, so the number had not moved: this slot produced zero.");
    }
    lines.push(`Good: ${count(slot.good)}`);
    lines.push(`Scrap: ${count(slot.scrap)}`
        + (slot.has_scrap && !slot.scrap_reported ? " (carried forward)" : ""));
    lines.push(`Total: ${count(slot.total)}`);
    if (slot.standard !== null && slot.standard !== undefined) {
        lines.push(`Standard: ${count(slot.standard)}`);
    }
    if (slot.has_downtime) {
        lines.push(`Downtime: ${slot.unplanned_minutes} min unplanned, ${slot.planned_minutes} min planned`);
        lines.push(`Run time: ${slot.run_minutes} of ${slot.ppt_minutes} min`);
        for (const reason of slot.reasons) {
            lines.push(`  • ${reason.label}: ${reason.minutes} min${reason.is_planned ? " (planned)" : ""}`);
        }
        if (slot.note) lines.push(`Note: ${slot.note}`);
    }
    const reason = missingReason(slot);
    if (reason) lines.push(reason);
    for (const warning of slot.warnings || []) {
        lines.push(describeCode(warning, WARNING_LABELS));
    }
    return lines.join("\n");
}

function clearGrid() {
    document.querySelectorAll(".oee-grid .slot-cell").forEach((cell) => {
        cell.querySelector(".cell-value").textContent = "—";
        cell.removeAttribute("data-band");
        cell.removeAttribute("data-flagged");
        cell.removeAttribute("data-warned");
        cell.removeAttribute("title");
    });
    document.querySelectorAll(".oee-grid .sum").forEach((cell) => {
        cell.textContent = "—";
        cell.removeAttribute("data-band");
        cell.removeAttribute("title");
    });
    document.querySelectorAll(".oee-grid tr[data-machine]").forEach((row) => {
        row.removeAttribute("data-unscheduled");
        const operator = row.querySelector(".operator");
        if (operator) operator.textContent = "";
    });
    document.querySelectorAll("[data-rollup]").forEach((el) => {
        el.textContent = "";
    });
}

function fillMachineRow(row, machine, slots) {
    const operator = row.querySelector(".operator");
    if (operator) operator.textContent = machine.operator || "";

    if (!machine.scheduled) {
        // Not scheduled is not a zero. The machine is excluded from the
        // rollup entirely rather than scoring 0% and dragging the zone down.
        row.setAttribute("data-unscheduled", "");
        const oeeCell = row.querySelector('[data-field="oee"]');
        oeeCell.textContent = "not scheduled";
        oeeCell.title = "Marked as not scheduled to run this shift, so it is left out of the zone rollup.";
        return;
    }

    for (const slotName of slots) {
        const cell = row.querySelector(`.slot-cell[data-slot="${slotName}"]`);
        if (!cell) continue;
        const slot = machine.slots[slotName];
        if (!slot) continue;

        cell.title = slotTitle(slot);
        if (slot.flags && slot.flags.length) {
            cell.setAttribute("data-flagged", "");
            cell.querySelector(".cell-value").textContent = "!";
        } else if (slot.oee !== null && slot.oee !== undefined) {
            cell.querySelector(".cell-value").textContent = pct(slot.oee, 0);
            setBand(cell, slot.oee);
            // Marked but still shown at its real value — see WARNING_LABELS.
            if (slot.warnings && slot.warnings.length) {
                cell.setAttribute("data-warned", "");
            }
        } else if (!slot.is_elapsed) {
            // The rest of today isn't missing data, it just hasn't happened.
            cell.querySelector(".cell-value").textContent = "·";
        }
    }

    const shift = machine.shift;
    if (!shift) {
        const oeeCell = row.querySelector('[data-field="oee"]');
        oeeCell.title = machine.flags && machine.flags.length
            ? machine.flags.map((f) => describeCode(f, FLAG_LABELS)).join("\n")
            : "No slot in this shift has both units and downtime entered.";
        return;
    }

    const set = (field, text, title) => {
        const cell = row.querySelector(`[data-field="${field}"]`);
        if (!cell) return cell;
        cell.textContent = text;
        if (title) cell.title = title;
        return cell;
    };

    setBand(set("oee", pct(shift.oee)), shift.oee);
    set("availability", pct(shift.availability),
        `Run ${shift.run_minutes} of ${shift.ppt_minutes} planned minutes`);
    set("performance", pct(shift.performance),
        machine.scrap_complete ? "" : "Needs scrap to separate Performance from Quality");
    set("quality", pct(shift.quality),
        machine.scrap_complete ? "" : "Scrap not fully entered for this shift");
    set("pct_of_standard", pct(shift.pct_of_standard),
        `Good ${count(shift.good)} against standard ${count(shift.standard)}`);
    set("good", count(shift.good));
    set("scrap", count(shift.scrap),
        machine.scrap_complete ? "" : "Scrap not entered for every counted slot");

    // Taken from the payload rather than derived from ppt: backing planned
    // downtime out of PPT needs the counted-slot count too, and assuming a
    // full four-slot shift would misreport every partial one.
    const planned = shift.planned_minutes;
    const unplanned = shift.unplanned_minutes;
    set("downtime", planned ? `${unplanned} +${planned}p` : `${unplanned}`,
        `${unplanned} min unplanned, ${planned} min planned ` +
        "(planned time is excluded from the OEE denominator, not counted as a loss)");

    const slotsCell = set("slots", `${machine.slots_counted}/${machine.slots_elapsed}`,
        machine.slots_counted < machine.slots_elapsed
            ? "Some elapsed slots are missing data, or were excluded for an impossible value. OEE covers only the counted ones."
            : "");
    if (slotsCell && machine.slots_counted < machine.slots_elapsed) {
        slotsCell.setAttribute("data-band", "fair");
    }
}

/**
 * Renders a zone's rollup, which the SERVER computes.
 *
 * Deliberately not recomputed here from the per-machine numbers, even though
 * every component needed is already in the payload. The aggregation has two
 * traps in it — percentages must never be averaged, and the availability
 * factor has to be weighted by capacity rather than clock time or A x P x Q
 * stops reconstructing OEE — and a second implementation of that in the
 * browser is a second thing to get wrong. See _build_rollup() and _aggregate()
 * in app/db/oee.py.
 */
function fillZoneRollup(section, shiftData) {
    const target = section.querySelector("[data-rollup]");
    if (!target) return;

    const rollup = (shiftData.zones || {})[section.dataset.zone];
    if (!rollup) {
        target.textContent = "no OEE: nothing counted this shift";
        return;
    }

    target.textContent = [
        `OEE ${pct(rollup.oee)}`,
        `A ${pct(rollup.availability, 0)}`,
        `P ${pct(rollup.performance, 0)}`,
        `Q ${pct(rollup.quality, 1)}`,
        `${pct(rollup.pct_of_standard, 0)} of std`,
    ].join(" · ");
    target.setAttribute("data-band", bandFor(rollup.oee) || "unknown");
    target.title =
        `${rollup.machines} machine(s) counted for OEE and Availability` +
        (rollup.split_machines === rollup.machines
            ? ""
            : `; only ${rollup.split_machines} have complete scrap, so Performance ` +
              "and Quality cover just those");
}

function renderCompleteness(shiftData) {
    const c = shiftData.completeness;
    if (!c || !c.slots_expected) {
        completenessEl.hidden = true;
        return;
    }
    completenessEl.hidden = false;

    // Spelled out because the counts are stricter than they look: a machine
    // has to have data for EVERY elapsed slot to be counted, so three of four
    // checkpoints contributes 0, not 0.75.
    const slotWord = `${c.slots_expected} elapsed slot${c.slots_expected === 1 ? "" : "s"}`;
    const explain = {
        units: "machines whose production is known across the shift. A blank " +
            "checkpoint counts as unchanged, so one reading is enough to " +
            "determine the whole shift. Only a machine with NO reading at " +
            "all is missing.",
        downtime: `machines with a downtime entry for all ${slotWord}. ` +
            "Downtime does NOT carry forward the way units do. It is not " +
            "cumulative, so a blank is genuinely unentered. A machine that ran " +
            "clean still needs one: tick \"none\" on /console. Without it OEE " +
            "stays blank rather than assuming zero downtime.",
        scrap: "machines whose scrap is known across the shift. Cumulative " +
            "like units, so a blank counts as unchanged. Scrap only affects " +
            "the Performance/Quality split. OEE and Availability are " +
            "computed without it.",
    };

    for (const field of ["units", "downtime", "scrap"]) {
        const item = completenessEl.querySelector(`[data-field="${field}"] b`);
        if (!item) continue;
        item.textContent = `${c[field]}/${c.machines}`;
        item.parentElement.classList.toggle("is-short", c[field] < c.machines);
        item.parentElement.title = explain[field];
    }

    completenessNote.textContent = `across ${slotWord}. A blank checkpoint ` +
        "means unchanged, so units and scrap need only one reading per shift; " +
        "downtime needs one per slot.";

    // The state that stops the page dead, called out rather than left for
    // someone to infer from a grid full of dashes.
    if (!c.downtime && c.units) {
        completenessNote.textContent =
            `across ${slotWord}. No OEE can be shown yet: it needs a downtime ` +
            'entry as well as units. Tick "none" on /console for machines that ' +
            "ran clean and the numbers appear.";
    }
}

/**
 * Groups machines BY PROBLEM rather than listing problems per machine.
 *
 * The obvious way round produced a wall of text: six machines carrying two
 * flags each repeated the same two explanations six times, in one run-on
 * paragraph. Inverted, the same information is two lines with a machine list
 * on each, and the shared cause is visible at a glance.
 */
function groupByCode(machines, field) {
    const groups = new Map();
    for (const [machineId, machine] of Object.entries(machines)) {
        for (const code of machine[field] || []) {
            if (!groups.has(code)) groups.set(code, []);
            groups.get(code).push(machineId);
        }
    }
    return groups;
}

function appendFlagGroup(parent, groups, table) {
    for (const [code, machineIds] of groups) {
        const entry = table[code] || { title: code, detail: "" };
        const row = document.createElement("div");
        row.className = "flag-row";

        const heading = document.createElement("span");
        heading.className = "flag-title";
        heading.textContent = `${entry.title}:`;

        const list = document.createElement("span");
        list.className = "flag-machines";
        list.textContent = machineIds.join(", ");

        const detail = document.createElement("span");
        detail.className = "flag-detail";
        detail.textContent = entry.detail;

        row.appendChild(heading);
        row.appendChild(list);
        if (entry.detail) row.appendChild(detail);
        parent.appendChild(row);
    }
}

function renderFlags(shiftData) {
    const flagGroups = groupByCode(shiftData.machines, "flags");
    const warningGroups = groupByCode(shiftData.machines, "warnings");

    flagsBanner.textContent = "";
    if (!flagGroups.size && !warningGroups.size) {
        flagsBanner.hidden = true;
        return;
    }
    flagsBanner.hidden = false;

    if (flagGroups.size) {
        const lead = document.createElement("div");
        lead.className = "flag-lead";
        lead.textContent = "Excluded from OEE. File corrections at /console.";
        flagsBanner.appendChild(lead);
        appendFlagGroup(flagsBanner, flagGroups, FLAG_LABELS);
    }

    if (warningGroups.size) {
        const lead = document.createElement("div");
        lead.className = "flag-lead flag-lead-warning";
        lead.textContent = "Worth a look, but still counted.";
        flagsBanner.appendChild(lead);
        appendFlagGroup(flagsBanner, warningGroups, WARNING_LABELS);
    }
}

function renderPareto(shiftData) {
    const items = shiftData.pareto || [];
    paretoEl.textContent = "";
    if (!items.length) {
        paretoSection.hidden = true;
        return;
    }
    paretoSection.hidden = false;

    const unplanned = items.filter((i) => !i.is_planned);
    const unplannedTotal = unplanned.reduce((sum, i) => sum + i.minutes, 0);
    const plannedTotal = items.filter((i) => i.is_planned).reduce((s, i) => s + i.minutes, 0);
    paretoTotal.textContent =
        `${unplannedTotal} min unplanned` + (plannedTotal ? ` · ${plannedTotal} min planned` : "");

    // Bars are scaled to the largest bar, not to the total, so the shape of
    // the distribution is readable even when one reason dominates.
    const widest = Math.max(...items.map((i) => i.minutes));

    for (const item of items) {
        const rowEl = document.createElement("div");
        rowEl.className = "pareto-row";
        if (item.is_planned) rowEl.setAttribute("data-planned", "");

        const label = document.createElement("span");
        label.className = "pareto-label";
        label.textContent = item.label;

        const track = document.createElement("span");
        track.className = "pareto-track";
        const bar = document.createElement("span");
        bar.className = "pareto-bar";
        bar.style.width = `${(item.minutes / widest) * 100}%`;
        track.appendChild(bar);

        const value = document.createElement("span");
        value.className = "pareto-value";
        value.textContent = item.is_planned
            ? `${item.minutes} min (planned)`
            : `${item.minutes} min · ${pct(item.pct_of_unplanned, 0)}`;

        rowEl.appendChild(label);
        rowEl.appendChild(track);
        rowEl.appendChild(value);
        rowEl.title = `${item.occurrences} slot(s) reported this reason`;
        paretoEl.appendChild(rowEl);
    }
}

function render() {
    shiftToggle.querySelectorAll(".shift-option").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.shift === currentShift);
    });

    const slots = slotsFor(currentShift);
    applyActiveSlots(slots);
    clearGrid();

    if (!payload) {
        syncUrl();
        return;
    }

    const shiftData = payload.shifts[currentShift];
    if (!shiftData) {
        syncUrl();
        return;
    }

    document.querySelectorAll(".zone-section").forEach((section) => {
        section.querySelectorAll("tr[data-machine]").forEach((row) => {
            const machine = shiftData.machines[row.dataset.machine];
            if (machine) fillMachineRow(row, machine, slots);
        });
        fillZoneRollup(section, shiftData);
    });

    renderCompleteness(shiftData);
    renderFlags(shiftData);
    renderPareto(shiftData);
    syncUrl();
}

function renderQuickPicks(dates) {
    quickPicksEl.textContent = "";
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

function showEmpty(message) {
    emptyNotice.textContent = message;
    emptyNotice.hidden = false;
    zoneSections.hidden = true;
    paretoSection.hidden = true;
    completenessEl.hidden = true;
}

function hideEmpty() {
    emptyNotice.hidden = true;
    zoneSections.hidden = false;
}

async function load(isoDate) {
    try {
        const query = isoDate ? `?date=${encodeURIComponent(isoDate)}` : "";
        const res = await fetch(`/api/oee-data${query}`);
        if (!res.ok) throw new Error(`Request failed: ${res.status}`);
        payload = await res.json();

        dateInput.value = payload.date;
        if (payload.available_dates.length) {
            dateInput.max = payload.available_dates[0];
            dateInput.min = payload.available_dates[payload.available_dates.length - 1];
        }
        renderQuickPicks(payload.available_dates);

        const anyMachine = Object.values(payload.shifts).some((shift) =>
            Object.values(shift.machines).some((m) => m.shift !== null)
        );
        if (!payload.has_any_data) {
            showEmpty("No production entries have been recorded yet.");
        } else if (!anyMachine) {
            showEmpty(
                `No OEE can be computed for ${formatDate(payload.date)}. ` +
                "OEE needs both production units and a downtime entry for a slot, " +
                "and at least one of those is missing for every machine that day."
            );
        } else {
            hideEmpty();
        }

        render();
        loadedAt.textContent = `Loaded at ${new Date().toLocaleTimeString()}`;
    } catch (err) {
        loadedAt.textContent = "Could not load data. Check the connection and reload.";
        console.error("OEE load failed:", err);
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
    render(); // no fetch — all three shifts are already loaded
});

load(window.INITIAL_DATE || null);
