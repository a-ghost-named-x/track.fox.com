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

const SHIFT_ORDER = window.SHIFT_ORDER;

// Standards sit at 75% of theoretical max, so 0.75 is "hit target exactly".
// The bands are derived from it rather than hardcoded, so if the floor ever
// revises that assumption the colours move with it.
const AT_STANDARD = window.STANDARD_PCT_OF_IDEAL || 0.75;
const WORLD_CLASS = 0.85;

const QUICK_PICK_COUNT = 7;

/**
 * Hard flags: the number cannot be right whatever the machine's rate is, so
 * the shift is excluded from OEE and from the rollups.
 *
 * Each has a short `title` naming the problem and a `detail` saying what to do.
 * The banner groups machines UNDER these rather than repeating the text per
 * machine — six machines carrying two flags each once rendered as a solid
 * paragraph of duplicated prose that nobody would read.
 */
const FLAG_LABELS = {
    final_reading_low: {
        title: "Last checkpoint is lower than an earlier one",
        detail: "A shift's production is its last reading, so that reading being " +
            "below an earlier one makes the whole shift total wrong. Fix it at /console.",
    },
    implausible_units: {
        title: "More than double the machine's theoretical maximum",
        detail: "Usually a transposed digit, or a whole day's total typed into a box that means one shift.",
    },
    downtime_over_shift: {
        title: "More downtime than fits in the shift",
        detail: "A shift holds at most its own length: 480 minutes on an 8-hour day, " +
            "600 on a 10-hour one, 720 on a 12-hour one. Check the minutes on /console/oee.",
    },
    no_ideal_rate: {
        title: "No ideal rate seeded",
        detail: "OEE cannot be computed for this machine until IT adds one.",
    },
};

/**
 * Soft warnings. Unlike the flags above these invalidate nothing: the machine
 * still counts and its value is shown as-is, never clamped.
 */
const WARNING_LABELS = {
    units_went_backwards: {
        title: "A checkpoint is lower than the one before it",
        detail: "Worth fixing at /console, but a later reading is higher so the " +
            "shift total is still correct and the machine still counts.",
    },
    over_100: {
        title: "Beat the maximum derived from its standard",
        detail: "Still counted. The machine produced more than an 8-hour shift " +
            "at its rated speed allows, so its numbers are shown as-is.",
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
const paretoScope = document.getElementById("pareto-scope");
const paretoPicker = document.getElementById("pareto-picker");
const paretoEmpty = document.getElementById("pareto-empty");
const loadedAt = document.getElementById("loaded-at");

// Zones in page order, as the template rendered them: [{slug, label, machine_ids}].
const ZONES = window.ZONES || [];
const ALL_MACHINE_IDS = ZONES.flatMap((zone) => zone.machine_ids);

/**
 * Which machines feed the downtime Pareto. One Set, always a subset of
 * ALL_MACHINE_IDS; the zone chips and the checkbox list are two views of it.
 * Starts from ?pareto=C1,C2,C3 when present so a view can be bookmarked;
 * unknown ids are dropped and an empty result falls back to everything.
 */
let paretoSelection = (() => {
    const wanted = new Set(
        (window.INITIAL_PARETO || "").split(",").map((s) => s.trim()).filter(Boolean)
    );
    const known = ALL_MACHINE_IDS.filter((id) => wanted.has(id));
    return new Set(known.length ? known : ALL_MACHINE_IDS);
})();

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

/** The shift currently shown, as the floor says it: "3rd", not "3rd Shift". */
function shortShift(label) {
    return (label || "").split(" ")[0];
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

function syncUrl() {
    if (!payload) return;
    let url = `/oee?date=${encodeURIComponent(payload.date)}&shift=${encodeURIComponent(currentShift)}`;
    if (!isWholeFloor()) {
        url += `&pareto=${encodeURIComponent(ALL_MACHINE_IDS.filter((id) => paretoSelection.has(id)).join(","))}`;
    }
    window.history.replaceState(null, "", url);
}

/** Why a machine has no OEE, phrased as the thing that's missing. */
function missingReason(machine) {
    const missing = [];
    if (!machine.has_production) missing.push("production");
    if (!machine.has_downtime) missing.push("downtime");
    if (missing.length) {
        return `Not entered: ${missing.join(" and ")}.` +
            (missing.includes("downtime")
                ? " Downtime is entered once per shift on /console/oee."
                : "");
    }
    if (machine.flags && machine.flags.length) {
        return machine.flags.map((f) => describeCode(f, FLAG_LABELS)).join("\n");
    }
    return "";
}

/**
 * The checkpoint sequence behind a machine's Good number.
 *
 * The per-slot columns are gone from the table, but this is still how you find
 * out WHICH reading is wrong when a machine is flagged, so it lives on the Good
 * cell's tooltip rather than disappearing with them.
 */
function checkpointTitle(machine) {
    const lines = [
        `Production checkpoints (${machine.shift_hours}h shift, ${machine.span}) — ` +
        "cumulative, blank means unchanged:",
    ];
    for (const point of machine.checkpoints) {
        if (!point.elapsed) continue;
        const reading = point.reported ? count(point.reading) : "(blank)";
        const made = point.produced === null || point.produced === undefined
            ? "unknown"
            : count(point.produced);
        // A long 2nd Shift runs past midnight, and its last checkpoints
        // carry the next calendar date. Say so, or "6AM" reads as this
        // morning rather than tomorrow's.
        const when = payload && point.date && point.date !== payload.date
            ? `${point.slot} (${formatQuickPick(point.date)})`
            : point.slot;
        lines.push(`   ${when}: reading ${reading}, made ${made}`);
    }
    lines.push("A shift's production is its last reading, so interior blanks can't change it.");
    return lines.join("\n");
}

/** "Lack of Operator 240 · Setup 30", longest first, capped so the cell fits. */
function reasonSummary(reasons, maxShown) {
    if (!reasons || !reasons.length) return "—";
    const shown = reasons.slice(0, maxShown)
        .map((r) => `${r.label} ${r.minutes}`)
        .join(" · ");
    const hidden = reasons.length - Math.min(reasons.length, maxShown);
    return hidden ? `${shown} +${hidden}` : shown;
}

function clearGrid() {
    document.querySelectorAll(".oee-grid .sum").forEach((cell) => {
        cell.textContent = "—";
        cell.removeAttribute("data-band");
        cell.removeAttribute("data-warned");
        cell.removeAttribute("title");
    });
    document.querySelectorAll(".oee-grid tr[data-machine]").forEach((row) => {
        row.removeAttribute("data-unscheduled");
        const operator = row.querySelector(".operator");
        if (operator) operator.textContent = "";
        const length = row.querySelector(".shift-length");
        if (length) {
            length.textContent = "";
            length.removeAttribute("title");
            length.removeAttribute("data-long");
        }
    });
    document.querySelectorAll("[data-rollup]").forEach((el) => {
        el.textContent = "";
    });
}

/**
 * The "8h" / "10h" / "12h" label next to a machine's name: which OEE this
 * row is — an 8-hour one judged against 480 minutes, or a 10/12-hour one
 * against 600/720. Always shown, so a reader never has to infer the length
 * from the minutes; the 8-hour label is dim and the longer ones are lit,
 * because the longer ones are what makes two rows in the same zone not
 * directly comparable.
 */
function fillShiftLength(row, machine) {
    const tag = row.querySelector(".shift-length");
    if (!tag) return;
    const hours = machine.shift_hours;
    if (!hours) return;
    tag.textContent = `${hours}h`;
    if (hours !== 8) tag.setAttribute("data-long", "");
    if (!machine.shift_exists) {
        // The day has no room for this shift; the tag shows what took it.
        const by = machine.covered_by || {};
        tag.textContent = by.hours ? `${by.hours}h` : "";
        tag.title = by.shift
            ? `${by.shift} ran ${by.hours} hours (${by.span}), which covers the night.`
            : "";
        return;
    }
    tag.title =
        `${hours}-hour OEE: this shift is ${machine.span}, so the machine is judged ` +
        `against ${machine.shift_minutes} minutes and ${machine.checkpoints.length} ` +
        "checkpoints. Shift length is set on /console/oee.";
}

function fillMachineRow(row, machine) {
    const operator = row.querySelector(".operator");
    if (operator) operator.textContent = machine.operator || "";
    fillShiftLength(row, machine);

    const set = (field, text, title) => {
        const cell = row.querySelector(`[data-field="${field}"]`);
        if (!cell) return cell;
        cell.textContent = text;
        if (title) cell.title = title;
        return cell;
    };

    if (machine.shift_exists === false) {
        // After a 10 or 12-hour 2nd Shift there is no 3rd: the night's hours
        // are on the 2nd Shift, where they're counted.
        const by = machine.covered_by || {};
        row.setAttribute("data-unscheduled", "");
        set("oee", `no ${shortShift(currentShift)} shift`,
            `${by.shift || "The previous shift"} ran ${by.hours} hours (${by.span}), ` +
            "which covers the night. Left out of the rollup entirely.");
        return;
    }

    if (!machine.scheduled) {
        // Not scheduled is not a zero. The machine is excluded from the rollup
        // entirely rather than scoring 0% and dragging the zone down with it.
        row.setAttribute("data-unscheduled", "");
        set("oee", "not scheduled",
            machine.shift_hours !== 8 && currentShift !== SHIFT_ORDER[0]
                ? `A ${machine.shift_hours}-hour night crew (${machine.span}) is the exception, ` +
                  "so this shift starts as not scheduled. Tick Scheduled on /console/oee " +
                  "if one ran. Left out of the rollup entirely — not 0%."
                : "Marked as not scheduled to run this shift on /console/oee, so it is " +
                  "left out of the zone rollup entirely — not counted as 0%.");
        return;
    }

    // Downtime and its reasons are worth showing even when OEE can't be
    // computed: a machine missing its production numbers may still have a
    // perfectly good downtime record, and that record is what the Pareto is
    // built from.
    if (machine.has_downtime) {
        const reasons = machine.reasons || [];
        const unplanned = reasons.filter((r) => !r.is_planned)
            .reduce((sum, r) => sum + r.minutes, 0);
        const planned = reasons.filter((r) => r.is_planned)
            .reduce((sum, r) => sum + r.minutes, 0);
        set("downtime", planned ? `${unplanned} +${planned}p` : `${unplanned}`,
            reasons.length
                ? `${unplanned} min unplanned` + (planned ? `, ${planned} min planned` : "")
                : "Recorded as ran clean for the whole shift: no downtime.");
        set("reasons", reasonSummary(reasons, 2),
            reasons.map((r) => `${r.label}: ${r.minutes} min`).join("\n")
                + (machine.note ? `\nNote: ${machine.note}` : ""));
    }

    if (machine.has_scrap) set("scrap", count(machine.scrap));

    const shift = machine.shift;
    if (!shift) {
        // No OEE, but say WHY rather than leaving a bare dash.
        const reason = missingReason(machine);
        const oeeCell = row.querySelector('[data-field="oee"]');
        if (oeeCell && reason) oeeCell.title = reason;
        if (machine.flags && machine.flags.length) {
            oeeCell.textContent = "!";
            oeeCell.setAttribute("data-band", "poor");
        }
        if (machine.has_production) {
            set("good", count(machine.checkpoints
                .filter((p) => p.elapsed && p.produced)
                .reduce((sum, p) => sum + p.produced, 0)), checkpointTitle(machine));
        }
        return;
    }

    setBand(set("oee", pct(shift.oee)), shift.oee);
    if (machine.warnings && machine.warnings.length) {
        const cell = row.querySelector('[data-field="oee"]');
        cell.setAttribute("data-warned", "");
        cell.title = machine.warnings
            .map((w) => describeCode(w, WARNING_LABELS)).join("\n");
    }

    set("availability", pct(shift.availability),
        `Ran ${shift.run_minutes} of ${shift.ppt_minutes} scheduled minutes`);
    set("performance", pct(shift.performance),
        machine.scrap_known ? "" : "Needs scrap to separate Performance from Quality");
    set("quality", pct(shift.quality),
        machine.scrap_known ? "" : "Scrap not entered for this shift");
    set("pct_of_standard", pct(shift.pct_of_standard),
        `Good ${count(shift.good)} against a shift standard of ${count(shift.standard)}`);
    set("good", count(shift.good), checkpointTitle(machine));
    set("scrap", count(shift.scrap),
        machine.scrap_known ? "" : "Scrap not entered for this shift");
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

    // Every count is a number of MACHINES, out of those scheduled to run.
    const explain = {
        production: "machines with at least one production reading this shift. " +
            "A blank checkpoint means unchanged, so one reading determines the " +
            "whole shift — only a machine with no reading at all is missing. " +
            "Entered on the 2-hour rounds at /console.",
        downtime: "machines with a downtime record for this shift. Entered once " +
            "at the end of the shift on /console/oee. A machine that ran clean " +
            "still needs one — tick \"no downtime\" — because without it OEE " +
            "stays blank rather than assuming zero.",
        scrap: "machines with a scrap total for this shift, entered on " +
            "/console/oee. Scrap only affects the Performance/Quality split; " +
            "OEE and Availability are computed without it.",
    };

    for (const field of ["production", "downtime", "scrap"]) {
        const item = completenessEl.querySelector(`[data-field="${field}"] b`);
        if (!item) continue;
        item.textContent = `${c[field]}/${c.machines}`;
        item.parentElement.classList.toggle("is-short", c[field] < c.machines);
        item.parentElement.title = explain[field];
    }

    const slotWord = `${c.slots_expected} elapsed slot${c.slots_expected === 1 ? "" : "s"}`;
    // The slot count is the 8-hour view; a machine on a 10 or 12-hour day has
    // more, and is judged on its own. Say how many there are so "4 slots"
    // isn't read as the whole story.
    const longNote = c.long_shift_machines
        ? ` (${c.long_shift_machines} machine${c.long_shift_machines === 1 ? "" : "s"} ` +
          "on 10h/12h shifts, with more)"
        : "";
    completenessNote.textContent =
        `across ${slotWord}${longNote}. Production comes from the 2-hour rounds; ` +
        "downtime and scrap are entered once at end of shift.";

    // The state that stops the page dead, called out rather than left for
    // someone to infer from a grid full of dashes.
    if (!c.downtime && c.production) {
        completenessNote.textContent =
            "No OEE can be shown yet: it needs a downtime record as well as " +
            "production. That is entered at the end of the shift on " +
            "/console/oee — tick \"no downtime\" for machines that ran clean " +
            "and the numbers appear.";
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

function isWholeFloor() {
    return paretoSelection.size === ALL_MACHINE_IDS.length;
}

/**
 * Downtime minutes by reason across the selected machines for one shift —
 * the same sum _build_pareto() does server-side for the whole floor, done
 * here because the picker can name any subset. Unscheduled machines and
 * shifts that don't exist are skipped, as there; a machine's reasons are
 * counted whether or not its OEE was computable, as there.
 *
 * This is safe to do in the browser where the OEE rollups are not: a Pareto
 * is a plain per-reason sum with no weighting, so there is nothing to get
 * subtly wrong. Keep it that way — if it ever needs a rate, move it server
 * side with the rest.
 */
function buildPareto(machines, selected) {
    const byCode = new Map();
    for (const [machineId, machine] of Object.entries(machines)) {
        if (!selected.has(machineId) || !machine.scheduled) continue;
        for (const reason of machine.reasons || []) {
            if (!byCode.has(reason.code)) {
                byCode.set(reason.code, {
                    code: reason.code, label: reason.label, is_planned: reason.is_planned,
                    minutes: 0, machines: 0,
                });
            }
            const bucket = byCode.get(reason.code);
            bucket.minutes += reason.minutes;
            bucket.machines += 1;
        }
    }
    const ranked = Array.from(byCode.values()).sort((a, b) => b.minutes - a.minutes);
    const unplannedTotal = ranked.filter((i) => !i.is_planned).reduce((s, i) => s + i.minutes, 0);
    for (const item of ranked) {
        item.pct_of_unplanned =
            unplannedTotal && !item.is_planned ? item.minutes / unplannedTotal : null;
    }
    return ranked;
}

/** "all machines", a zone's name when the selection is exactly that zone, or the ids. */
function paretoScopeLabel() {
    if (isWholeFloor()) return "all machines";
    for (const zone of ZONES) {
        if (zone.machine_ids.length === paretoSelection.size
            && zone.machine_ids.every((id) => paretoSelection.has(id))) {
            return zone.label;
        }
    }
    const ids = ALL_MACHINE_IDS.filter((id) => paretoSelection.has(id));
    return ids.length > 6 ? `${ids.slice(0, 6).join(", ")} +${ids.length - 6}` : ids.join(", ");
}

/** Pushes the selection into the chips and checkboxes, so they always agree with it. */
function syncParetoPicker() {
    paretoPicker.querySelectorAll('input[type="checkbox"]').forEach((box) => {
        box.checked = paretoSelection.has(box.value);
    });
    paretoPicker.querySelectorAll(".pareto-chip").forEach((chip) => {
        const slug = chip.dataset.zone;
        const active = slug === "*"
            ? isWholeFloor()
            : (() => {
                const zone = ZONES.find((z) => z.slug === slug);
                return !!zone && !isWholeFloor()
                    && zone.machine_ids.length === paretoSelection.size
                    && zone.machine_ids.every((id) => paretoSelection.has(id));
            })();
        chip.classList.toggle("is-active", active);
    });
}

function renderPareto(shiftData) {
    paretoEl.textContent = "";
    paretoEmpty.hidden = true;

    // Hide the whole section only when NO machine has any downtime this
    // shift. A selection with nothing in it keeps the section (and the
    // picker) on screen, or there'd be no way to pick something else.
    const anyDowntime = Object.values(shiftData.machines)
        .some((m) => m.scheduled && (m.reasons || []).length);
    if (!anyDowntime) {
        paretoSection.hidden = true;
        return;
    }
    paretoSection.hidden = false;
    syncParetoPicker();
    paretoScope.textContent = paretoScopeLabel();

    const items = buildPareto(shiftData.machines, paretoSelection);
    if (!items.length) {
        paretoTotal.textContent = "";
        paretoEmpty.textContent = `No downtime recorded for ${paretoScopeLabel()} this shift.`;
        paretoEmpty.hidden = false;
        return;
    }

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
        rowEl.title = `${item.machines} machine(s) reported this reason`;
        paretoEl.appendChild(rowEl);
    }
}

function render() {
    shiftToggle.querySelectorAll(".shift-option").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.shift === currentShift);
    });

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
            if (machine) fillMachineRow(row, machine);
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

// The Pareto picker only ever changes paretoSelection, then re-renders the
// Pareto from the payload already loaded. A zone chip is a shortcut for
// "exactly that zone's machines"; a checkbox adjusts one machine. Ticking the
// last box off is refused — an empty Pareto says nothing.
paretoPicker.addEventListener("click", (event) => {
    const chip = event.target.closest(".pareto-chip");
    if (!chip) return;
    const slug = chip.dataset.zone;
    const zone = ZONES.find((z) => z.slug === slug);
    paretoSelection = new Set(slug === "*" || !zone ? ALL_MACHINE_IDS : zone.machine_ids);
    if (payload && payload.shifts[currentShift]) renderPareto(payload.shifts[currentShift]);
    syncUrl();
});

paretoPicker.addEventListener("change", (event) => {
    const box = event.target.closest('input[type="checkbox"]');
    if (!box) return;
    if (box.checked) {
        paretoSelection.add(box.value);
    } else if (paretoSelection.size > 1) {
        paretoSelection.delete(box.value);
    } else {
        box.checked = true; // keep at least one machine
    }
    if (payload && payload.shifts[currentShift]) renderPareto(payload.shifts[currentShift]);
    syncUrl();
});

load(window.INITIAL_DATE || null);
