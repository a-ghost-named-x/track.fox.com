/**
 * /oee: Overall Equipment Effectiveness by machine and shift.
 *
 * Fetches a whole production day (all three shifts) from /api/oee-data and
 * switches shifts in the browser. Doesn't poll.
 *
 * A missing value renders as "—", never as zero or 100%. The server returns
 * null for anything it can't compute.
 */

const SHIFT_ORDER = window.SHIFT_ORDER;

// Standards are 75% of theoretical max, so 0.75 means "hit target exactly".
// The colour bands are derived from it.
const AT_STANDARD = window.STANDARD_PCT_OF_IDEAL || 0.75;
const WORLD_CLASS = 0.85;

const QUICK_PICK_COUNT = 7;

// A short day's shifts are 6 hours, and a stored length of 2, 4 or 6 means one.
const SHORT_PATTERN_HOURS = window.SHORT_PATTERN_HOURS || 6;

/**
 * Hard flags: the numbers can't be right, so the machine is excluded from OEE
 * and the rollups. `title` names the problem and `detail` says what to do.
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
            "600 on a 10-hour one, 720 on a 12-hour one, 60 per hour typed on a short day. " +
            "Check the minutes, and the hours, on /console/oee.",
    },
    no_ideal_rate: {
        title: "No ideal rate seeded",
        detail: "OEE cannot be computed for this machine until IT adds one.",
    },
};

/** Soft warnings: the machine still counts and its value is shown as-is. */
const WARNING_LABELS = {
    units_went_backwards: {
        title: "A checkpoint is lower than the one before it",
        detail: "Worth fixing at /console, but a later reading is higher so the " +
            "shift total is still correct and the machine still counts.",
    },
    over_100: {
        title: "Beat the maximum derived from its standard",
        detail: "Still counted. The machine produced more than its shift's hours " +
            "at its rated speed allow, so its numbers are shown as-is. On a machine " +
            "that ran long or short, check its shift length on /console/oee first.",
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
const paretoPeriodPicker = document.getElementById("pareto-period");
const paretoEmpty = document.getElementById("pareto-empty");
const paretoCoverage = document.getElementById("pareto-coverage");
const loadedAt = document.getElementById("loaded-at");

// Zones in page order, as the template rendered them: [{slug, label, machine_ids}].
const ZONES = window.ZONES || [];
const ALL_MACHINE_IDS = ZONES.flatMap((zone) => zone.machine_ids);

/**
 * Which machines feed the downtime Pareto. Starts from ?pareto=C1,C2,C3
 * when present; unknown ids are dropped, and an empty result means all.
 */
let paretoSelection = (() => {
    const wanted = new Set(
        (window.INITIAL_PARETO || "").split(",").map((s) => s.trim()).filter(Boolean)
    );
    const known = ALL_MACHINE_IDS.filter((id) => wanted.has(id));
    return new Set(known.length ? known : ALL_MACHINE_IDS);
})();

/**
 * The Pareto's period: "shift" (the selected shift) or a number of days
 * ending on the page's date, all shifts, from /api/oee-pareto.
 */
const PARETO_RANGE_DAYS = window.PARETO_RANGE_DAYS || [7, 30];
let paretoPeriod = PARETO_RANGE_DAYS.includes(window.INITIAL_PERIOD)
    ? window.INITIAL_PERIOD
    : "shift";

/**
 * 7/30-day payloads keyed by "date|days", kept for the visit. A pending
 * fetch is stored as "loading" and a failed one as "error".
 */
const rangeCache = new Map();

let payload = null;
let currentShift = SHIFT_ORDER.includes(window.INITIAL_SHIFT)
    ? window.INITIAL_SHIFT
    : SHIFT_ORDER[0];

/**
 * Parses "2026-09-01" into a local-midnight Date. `new Date(iso)` would give
 * UTC midnight, which shows as the previous day in US timezones.
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

/** Percentage, or "—" when the value is unknown. */
function pct(value, digits) {
    if (value === null || value === undefined) return "—";
    return `${(value * 100).toFixed(digits === undefined ? 1 : digits)}%`;
}

function count(value) {
    if (value === null || value === undefined) return "—";
    return value.toLocaleString("en-US");
}

/**
 * Which colour band an OEE value falls in, or null (no colour) when unknown.
 * Missing data shouldn't look like a bad score.
 */
function bandFor(value) {
    if (value === null || value === undefined) return null;
    if (value >= WORLD_CLASS) return "excellent";
    if (value >= AT_STANDARD) return "good";
    // 80% of standard: 60% OEE on the default 0.75.
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
    if (paretoPeriod !== "shift") url += `&period=${paretoPeriod}`;
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
 * The checkpoint readings behind a machine's Good number, for its tooltip.
 * This is how to find which reading is wrong when a machine is flagged.
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
        // Label checkpoints filed under the next calendar date.
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
 * The "8h" / "10h" / "12h" label next to a machine's name. Anything other
 * than 8 hours is highlighted, since those rows aren't directly comparable
 * with their neighbours.
 */
function fillShiftLength(row, machine) {
    const tag = row.querySelector(".shift-length");
    if (!tag) return;
    const hours = machine.shift_hours;
    if (!hours) return;
    tag.textContent = `${hours}h`;
    if (hours !== 8) tag.setAttribute("data-long", "");
    if (!machine.shift_exists) {
        // The shift doesn't exist; show the length of the one covering it.
        const by = machine.covered_by || {};
        tag.textContent = by.hours ? `${by.hours}h` : "";
        tag.title = by.shift
            ? `${by.shift} ran ${by.hours} hours (${by.span}), which covers the night.`
            : "";
        return;
    }
    if (machine.window_span && machine.window_span !== machine.span) {
        // A short day: scheduled for part of a 6-hour shift.
        tag.title =
            `Short day: scheduled ${hours} hour${hours === 1 ? "" : "s"} (${machine.span}) of ` +
            `the ${machine.window_span} shift, so the machine is judged against ` +
            `${machine.shift_minutes} minutes. Production is the last reading in that ` +
            "shift's checkpoints. Set on /console/oee.";
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
        // No 3rd Shift after a 10 or 12-hour 2nd.
        const by = machine.covered_by || {};
        row.setAttribute("data-unscheduled", "");
        set("oee", `no ${shortShift(currentShift)} shift`,
            `${by.shift || "The previous shift"} ran ${by.hours} hours (${by.span}), ` +
            "which covers the night. Left out of the rollup entirely.");
        return;
    }

    if (!machine.scheduled) {
        // Not scheduled is excluded from the rollup, not scored 0%.
        row.setAttribute("data-unscheduled", "");
        const lateShift = currentShift !== SHIFT_ORDER[0];
        let why = "Marked as not scheduled to run this shift on /console/oee, so it is " +
            "left out of the zone rollup entirely — not counted as 0%.";
        if (lateShift && machine.shift_hours <= SHORT_PATTERN_HOURS) {
            why = `On a short day this shift (${machine.window_span}) starts as not ` +
                "scheduled, since usually only the morning crew runs. Tick Scheduled on " +
                "/console/oee if this machine ran. Left out of the rollup entirely — not 0%.";
        } else if (lateShift && machine.shift_hours !== 8) {
            why = `A ${machine.shift_hours}-hour night crew (${machine.span}) is the exception, ` +
                "so this shift starts as not scheduled. Tick Scheduled on /console/oee " +
                "if one ran. Left out of the rollup entirely — not 0%.";
        }
        set("oee", "not scheduled", why);
        return;
    }

    // Show downtime even when OEE can't be computed.
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
        // No OEE; the tooltip says why.
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
 * Renders a zone's rollup as computed by the server. Not recomputed here,
 * because the aggregation rules (no averaged percentages, capacity-weighted
 * availability) live in _build_rollup() and _aggregate() in app/db/oee.py.
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
    // The slot count is the 8-hour view; mention machines on other lengths.
    const others = c.other_length_machines;
    const longNote = others
        ? ` (${others} machine${others === 1 ? "" : "s"} on other shift lengths, ` +
          "judged on their own hours)"
        : "";
    completenessNote.textContent =
        `across ${slotWord}${longNote}. Production comes from the 2-hour rounds; ` +
        "downtime and scrap are entered once at end of shift.";

    // Explain the common "no downtime entered yet" state.
    if (!c.downtime && c.production) {
        completenessNote.textContent =
            "No OEE can be shown yet: it needs a downtime record as well as " +
            "production. That is entered at the end of the shift on " +
            "/console/oee — tick \"no downtime\" for machines that ran clean " +
            "and the numbers appear.";
    }
}

/**
 * Groups machines by problem, so each explanation appears once with a list
 * of machines after it.
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
 * Downtime minutes by reason, summed across machines and ranked. Each list
 * is one machine's reasons for a shift, or its totals over 7/30 days (where
 * each reason also carries `shifts`, its machine-shift count).
 *
 * Done in the browser so the picker can select any machines. That's safe
 * because it's a plain sum; anything weighted belongs on the server.
 */
function rankReasons(reasonLists) {
    const byCode = new Map();
    for (const reasons of reasonLists) {
        for (const reason of reasons) {
            if (!byCode.has(reason.code)) {
                byCode.set(reason.code, {
                    code: reason.code, label: reason.label, is_planned: reason.is_planned,
                    minutes: 0, count: 0,
                });
            }
            const bucket = byCode.get(reason.code);
            bucket.minutes += reason.minutes;
            bucket.count += reason.shifts || 1;
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
    paretoPeriodPicker.querySelectorAll(".period-chip").forEach((chip) => {
        chip.classList.toggle("is-active", chip.dataset.period === String(paretoPeriod));
    });
}

/**
 * The 7/30-day payload ending on `date`: the data, null while loading (the
 * fetch starts here and redraws when done), or "error". An error is returned
 * once and then forgotten, so the next redraw retries.
 */
function rangeFor(date, days) {
    const key = `${date}|${days}`;
    const cached = rangeCache.get(key);
    if (cached === "loading") return null;
    if (cached === "error") {
        rangeCache.delete(key);
        return "error";
    }
    if (cached) return cached;

    rangeCache.set(key, "loading");
    fetch(`/api/oee-pareto?end=${encodeURIComponent(date)}&days=${days}`)
        .then((res) => {
            if (!res.ok) throw new Error(`Request failed: ${res.status}`);
            return res.json();
        })
        .then((data) => rangeCache.set(key, data))
        .catch((err) => {
            rangeCache.set(key, "error");
            console.error("Pareto range load failed:", err);
        })
        .finally(() => {
            // Only if the page still wants this range.
            if (payload && payload.date === date && paretoPeriod === days) renderPareto();
        });
    return null;
}

/**
 * Coverage line under the 7/30-day bars: how many machine-shifts they're
 * built from, and how many reported production but have no downtime entered.
 */
function renderCoverage(range) {
    let records = 0;
    let missing = 0;
    for (const [machineId, machine] of Object.entries(range.machines)) {
        if (!paretoSelection.has(machineId)) continue;
        records += machine.records;
        missing += machine.missing;
    }
    paretoCoverage.textContent =
        `Built from ${count(records)} machine-shift${records === 1 ? "" : "s"} with downtime entered` +
        (missing
            ? `. ${count(missing)} more reported production but have no downtime entered, ` +
              "so whatever they lost isn't in these bars — enter them on /console/oee."
            : ". Every shift that reported production has its downtime entered.");
    paretoCoverage.classList.toggle("is-short", missing > 0);
    paretoCoverage.hidden = false;
}

/** "1,234 min", with hours alongside once the number stops being readable as minutes. */
function minutesText(minutes) {
    return minutes >= 600 ? `${count(minutes)} min (${(minutes / 60).toFixed(0)} h)` : `${count(minutes)} min`;
}

function renderPareto() {
    paretoEl.textContent = "";
    paretoEmpty.hidden = true;
    paretoCoverage.hidden = true;

    // Shown whenever there's data, even with no downtime this shift, because
    // the period and machine pickers live here.
    if (!payload || !payload.has_any_data) {
        paretoSection.hidden = true;
        return;
    }
    paretoSection.hidden = false;
    syncParetoPicker();

    let items;
    let when;
    let unit;
    if (paretoPeriod === "shift") {
        const shiftData = payload.shifts[currentShift] || { machines: {} };
        // Same rules as _build_pareto(): skip unscheduled machines.
        items = rankReasons(
            Object.entries(shiftData.machines)
                .filter(([machineId, machine]) => paretoSelection.has(machineId) && machine.scheduled)
                .map(([, machine]) => machine.reasons || [])
        );
        when = `${shortShift(currentShift)} Shift, ${formatQuickPick(payload.date)}`;
        unit = "machine";
    } else {
        const range = rangeFor(payload.date, paretoPeriod);
        paretoScope.textContent = `${paretoScopeLabel()} · last ${paretoPeriod} days`;
        if (range === null || range === "error") {
            paretoTotal.textContent = "";
            paretoEmpty.textContent = range === null
                ? `Loading the last ${paretoPeriod} days…`
                : "Couldn't load that period. Check the connection and pick it again.";
            paretoEmpty.hidden = false;
            return;
        }
        items = rankReasons(
            Object.entries(range.machines)
                .filter(([machineId]) => paretoSelection.has(machineId))
                .map(([, machine]) => machine.reasons)
        );
        when = `${formatQuickPick(range.start)} – ${formatQuickPick(range.end)}, all shifts`;
        unit = "machine-shift";
        renderCoverage(range);
    }
    paretoScope.textContent = `${paretoScopeLabel()} · ${when}`;

    if (!items.length) {
        paretoTotal.textContent = "";
        paretoEmpty.textContent = paretoPeriod === "shift"
            ? `No downtime recorded for ${paretoScopeLabel()} this shift.`
            : `No downtime recorded for ${paretoScopeLabel()} in these ${paretoPeriod} days.`;
        paretoEmpty.hidden = false;
        return;
    }

    const unplanned = items.filter((i) => !i.is_planned);
    const unplannedTotal = unplanned.reduce((sum, i) => sum + i.minutes, 0);
    const plannedTotal = items.filter((i) => i.is_planned).reduce((s, i) => s + i.minutes, 0);
    paretoTotal.textContent =
        `${minutesText(unplannedTotal)} unplanned` +
        (plannedTotal ? ` · ${minutesText(plannedTotal)} planned` : "");

    // Scale bars to the largest reason, not the total.
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
            ? `${count(item.minutes)} min (planned)`
            : `${count(item.minutes)} min · ${pct(item.pct_of_unplanned, 0)}`;

        rowEl.appendChild(label);
        rowEl.appendChild(track);
        rowEl.appendChild(value);
        rowEl.title =
            `Reported on ${item.count} ${unit}${item.count === 1 ? "" : "s"}` +
            (item.minutes >= 60 ? ` · ${(item.minutes / 60).toFixed(1)} hours` : "");
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
    renderPareto();
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
    render(); // all three shifts are already loaded
});

// A zone chip selects exactly that zone's machines; a checkbox toggles one.
// The last machine can't be unticked.
paretoPicker.addEventListener("click", (event) => {
    const chip = event.target.closest(".pareto-chip");
    if (!chip) return;
    const slug = chip.dataset.zone;
    const zone = ZONES.find((z) => z.slug === slug);
    paretoSelection = new Set(slug === "*" || !zone ? ALL_MACHINE_IDS : zone.machine_ids);
    renderPareto();
    syncUrl();
});

// "This shift" uses the loaded payload; 7 and 30 days are fetched once per
// date and cached.
paretoPeriodPicker.addEventListener("click", (event) => {
    const chip = event.target.closest(".period-chip");
    if (!chip) return;
    const days = parseInt(chip.dataset.period, 10);
    paretoPeriod = PARETO_RANGE_DAYS.includes(days) ? days : "shift";
    renderPareto();
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
    renderPareto();
    syncUrl();
});

load(window.INITIAL_DATE || null);
