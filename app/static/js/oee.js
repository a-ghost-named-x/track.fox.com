/**
 * /oee: Overall Equipment Effectiveness by machine and shift.
 *
 * Fetches a whole production day (all three shifts) from /api/oee-data and
 * switches shifts in the browser. The 7 and 30-day periods come from
 * /api/oee-range, fetched once per date and period. Doesn't poll.
 *
 * A missing value renders as "—", never as zero or 100%. The server returns
 * null for anything it can't compute.
 */

const SHIFT_ORDER = window.SHIFT_ORDER;
const ALL_SHIFTS = window.ALL_SHIFTS_LABEL || "All";

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

/**
 * Why a machine-shift was left out of a 7/30-day number: what's missing, or
 * one of the hard flags above.
 */
const LEFT_OUT_LABELS = {
    no_downtime: {
        title: "No downtime entered",
        detail: "Production was reported but the shift's downtime wasn't. Enter it on " +
            "/console/oee, ticking \"no downtime\" if the machine ran clean.",
    },
    no_production: {
        title: "No production entered",
        detail: "Downtime was entered but the shift has no production readings. Check " +
            "that shift's rounds at /console.",
    },
    ...FLAG_LABELS,
};

/** Short one-line form, for tooltips on an individual cell. */
function describeCode(code, table) {
    const entry = table[code];
    if (!entry) return code;
    return entry.tooltip || `${entry.title}. ${entry.detail}`;
}

const oeeRoot = document.getElementById("oee");
const dateInput = document.getElementById("review-date");
const quickPicksEl = document.getElementById("quick-picks");
const periodToggle = document.getElementById("period-toggle");
const shiftToggle = document.getElementById("shift-toggle");
const floorTotal = document.getElementById("floor-total");
const floorTotalOee = document.getElementById("floor-total-oee");
const floorTotalDetail = document.getElementById("floor-total-detail");
const floorTotalDays = document.getElementById("floor-total-days");
const leftOutEl = document.getElementById("left-out");
const leftOutCounts = document.getElementById("left-out-counts");
const leftOutDetails = document.getElementById("left-out-details");
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
const paretoCoverage = document.getElementById("pareto-coverage");
const loadedAt = document.getElementById("loaded-at");

// Zones in page order, as the template rendered them: [{slug, label, machine_ids}].
const ZONES = window.ZONES || [];
const ALL_MACHINE_IDS = ZONES.flatMap((zone) => zone.machine_ids);

/**
 * The machines ticked in the Pareto's picker, and the machines that feed it.
 * Nothing ticked means the whole floor, so picking a few machines starts
 * from empty boxes rather than unticking the rest. Starts from
 * ?pareto=C1,C2,C3 when present; unknown ids are dropped.
 */
let paretoPicked = new Set();
let paretoSelection = new Set(ALL_MACHINE_IDS);

function setParetoPick(ids) {
    paretoPicked = new Set(ids);
    paretoSelection = new Set(paretoPicked.size ? paretoPicked : ALL_MACHINE_IDS);
}

setParetoPick((() => {
    const wanted = new Set(
        (window.INITIAL_PARETO || "").split(",").map((s) => s.trim()).filter(Boolean)
    );
    return ALL_MACHINE_IDS.filter((id) => wanted.has(id));
})());

/**
 * The page's period: "shift" (one shift of the selected day) or a number of
 * days ending on it, from /api/oee-range. Drives the grid and the Pareto.
 */
const PERIOD_DAYS = window.PERIOD_DAYS || [7, 30];
let period = PERIOD_DAYS.includes(window.INITIAL_PERIOD) ? window.INITIAL_PERIOD : "shift";

/**
 * 7/30-day payloads keyed by "date|days", kept for the visit. A pending
 * fetch is stored as "loading" and a failed one as "error".
 */
const rangeCache = new Map();

let payload = null;

// The shift "This shift" shows. Over 7/30 days `rangeShift` is shown instead,
// which can also be All; picking a single shift there sets both.
let currentShift = [window.INITIAL_SHIFT, window.DEFAULT_SHIFT]
    .find((label) => SHIFT_ORDER.includes(label)) || SHIFT_ORDER[0];
let rangeShift = period !== "shift" && SHIFT_ORDER.includes(window.INITIAL_SHIFT)
    ? window.INITIAL_SHIFT
    : ALL_SHIFTS;

function isRange() {
    return period !== "shift";
}

function shownShift() {
    return isRange() ? rangeShift : currentShift;
}

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

/** Minutes as hours, for downtime over 7/30 days: "46 h", "2.5 h". */
function hoursText(minutes, unit = " h") {
    if (!minutes) return "0";
    const hours = minutes / 60;
    return `${hours.toFixed(hours < 10 ? 1 : 0)}${unit}`;
}

/**
 * "Tue 9/22 – Mon 9/28, all shifts", or "..., 2nd Shift". Starts on the day
 * OEE tracking began when that's inside the period.
 */
function periodPhrase(range) {
    const when = `${formatQuickPick(range.tracked_start || range.start)} – ${formatQuickPick(range.end)}`;
    return rangeShift === ALL_SHIFTS ? `${when}, all shifts` : `${when}, ${shortShift(rangeShift)} Shift`;
}

/**
 * The "▲3.1" beside a 7/30-day OEE: the change in percentage points from the
 * period before, or null when the server says the two can't be compared.
 */
function changeBadge(group) {
    if (group.change === null || group.change === undefined) return null;
    const points = Math.round(group.change * 1000) / 10;
    const badge = document.createElement("span");
    badge.className = "oee-change";
    if (points > 0) {
        badge.dataset.dir = "up";
        badge.textContent = `▲${points.toFixed(1)}`;
    } else if (points < 0) {
        badge.dataset.dir = "down";
        badge.textContent = `▼${Math.abs(points).toFixed(1)}`;
    } else {
        badge.dataset.dir = "flat";
        badge.textContent = "±0.0";
    }
    return badge;
}

/** Tooltip line explaining the change, or why there isn't one. */
function changeTitle(group, range) {
    const days = range.days;
    const before = `the previous ${days} days (${formatQuickPick(range.previous_start)} – ` +
        `${formatQuickPick(range.previous_end)})`;
    if (group.change !== null && group.change !== undefined) {
        const points = Math.abs(group.change * 100).toFixed(1);
        const dir = group.change > 0 ? "up" : group.change < 0 ? "down" : "unchanged,";
        return `${pct(group.previous.oee)} over ${before}, so ${dir} ${points} points.`;
    }
    const began = range.tracking_start ? ` (${formatQuickPick(range.tracking_start)})` : "";
    if (!range.previous_tracked_days) {
        return `Not compared: ${before} were before OEE tracking began${began}.`;
    }
    if (range.previous_tracked_days * 2 < days) {
        return `Not compared: only ${range.previous_tracked_days} of ${before} were after ` +
            `OEE tracking began${began}, too few for a fair comparison.`;
    }
    if (!group.previous) return `Nothing counted in ${before} to compare with.`;
    if (group.thin) return "Not compared with the period before: fewer than half this period's shifts counted.";
    const total = group.previous_counted + group.previous_left_out;
    return `Not compared: ${before} had only ${group.previous_counted} of ${total} shifts ` +
        `counted, so its ${pct(group.previous.oee)} isn't a fair comparison.`;
}

/** "1st 61 · 2nd 57 · 3rd —": each shift's OEE, shown under All. */
function byShiftLine(byKey) {
    const line = document.createElement("span");
    line.className = "oee-by-shift";
    line.textContent = SHIFT_ORDER.map((label) => {
        const current = byKey[label] && byKey[label].current;
        const value = current && current.oee !== null ? Math.round(current.oee * 100) : "—";
        return `${shortShift(label)} ${value}`;
    }).join(" · ");
    return line;
}

/** Tooltip lines for the by-shift figures, with how many shifts each covers. */
function byShiftTitle(byKey) {
    return SHIFT_ORDER.map((label) => {
        const group = byKey[label];
        if (!group || !group.current) return `   ${shortShift(label)}: nothing counted`;
        return `   ${shortShift(label)}: ${pct(group.current.oee)} over ${group.counted} ` +
            `shift${group.counted === 1 ? "" : "s"}`;
    }).join("\n");
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
    let url = `/oee?date=${encodeURIComponent(payload.date)}&shift=${encodeURIComponent(shownShift())}`;
    if (!isWholeFloor()) {
        url += `&pareto=${encodeURIComponent(ALL_MACHINE_IDS.filter((id) => paretoSelection.has(id)).join(","))}`;
    }
    if (isRange()) url += `&period=${period}`;
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

/**
 * "Lack of Operator 240 · Setup 30", longest first, capped so the cell fits.
 * `format` turns minutes into the cell's unit.
 */
function reasonSummary(reasons, maxShown, format = (minutes) => minutes) {
    if (!reasons || !reasons.length) return "—";
    const shown = reasons.slice(0, maxShown)
        .map((r) => `${r.label} ${format(r.minutes)}`)
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
    document.querySelectorAll(".oee-grid tr[data-machine], .oee-grid tr[data-zone-total]").forEach((row) => {
        row.removeAttribute("data-unscheduled");
        const machineCell = row.querySelector(".machine-col");
        if (machineCell) machineCell.removeAttribute("title");
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
        el.removeAttribute("data-band");
        el.removeAttribute("data-warned");
        el.removeAttribute("title");
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
    ].join(" · ");
    target.setAttribute("data-band", bandFor(rollup.oee) || "unknown");
    target.title =
        `${rollup.machines} machine(s) counted for OEE and Availability` +
        (rollup.split_machines === rollup.machines
            ? ""
            : `; only ${rollup.split_machines} have complete scrap, so Performance ` +
              "and Quality cover just those");
}

// ---------------------------------------------------------------------------
// 7 and 30 days. Every number comes from compute_oee_range() on the server;
// nothing is re-added here, for the same reason as fillZoneRollup().
// ---------------------------------------------------------------------------

/** How many shifts a 7/30-day figure is built from, and which were left out. */
function countedTitle(group, range) {
    const total = group.counted + group.left_out;
    const lines = [`${group.counted} of ${total} shift${total === 1 ? "" : "s"} counted for OEE, ` +
        `${periodPhrase(range)}.`];
    const leftOut = group.left_out_shifts || [];
    if (leftOut.length) {
        lines.push("Left out:");
        const shown = 15;
        for (const item of leftOut.slice(0, shown)) {
            const why = item.reasons.map((code) => (LEFT_OUT_LABELS[code] || { title: code }).title);
            lines.push(`   ${formatQuickPick(item.date)} ${shortShift(item.shift)}: ${why.join("; ")}`);
        }
        if (leftOut.length > shown) lines.push(`   …and ${leftOut.length - shown} more`);
    }
    return lines.join("\n");
}

function monthDay(iso) {
    const d = parseISODate(iso);
    return `${d.getMonth() + 1}/${d.getDate()}`;
}

function weekday(iso) {
    return parseISODate(iso).toLocaleDateString("en-US", { weekday: "short" });
}

/** A column is one day over 7 days, a week over 30. */
function isDayColumn(column) {
    return column.start === column.end;
}

/** "Mon, 9/28" for a day, "Tue, 9/22 – Mon, 9/28" for a week, for tooltips. */
function columnPhrase(column) {
    return isDayColumn(column)
        ? formatQuickPick(column.start)
        : `${formatQuickPick(column.start)} – ${formatQuickPick(column.end)}`;
}

/**
 * Over 7/30 days, swaps Top reasons (hidden by CSS under .is-range) for a
 * column per day or week, newest first. Removes them for one shift.
 */
function syncDayColumns(range) {
    oeeRoot.classList.toggle("is-range", isRange());
    document.querySelectorAll(".oee-grid .day-col").forEach((el) => el.remove());
    const columns = range && range.columns ? range.columns : [];
    if (!columns.length) return;

    document.querySelectorAll(".oee-grid").forEach((table) => {
        const head = table.querySelector("thead tr");
        for (const column of columns) {
            const th = document.createElement("th");
            th.className = "sum-head day-col";
            if (isDayColumn(column)) {
                th.textContent = weekday(column.start);
                const date = document.createElement("span");
                date.className = "day-col-date";
                date.textContent = monthDay(column.start);
                th.appendChild(date);
            } else {
                th.textContent = `${monthDay(column.start)}–${monthDay(column.end)}`;
            }
            head.appendChild(th);
        }
        table.querySelectorAll("tbody tr, tfoot tr").forEach((row) => {
            columns.forEach((_, index) => {
                const td = document.createElement("td");
                td.className = "sum day-col";
                td.dataset.column = index;
                td.textContent = "—";
                row.appendChild(td);
            });
        });
    });
}

/** Each day's (or week's) OEE in a machine or zone row. */
function fillDayCells(row, byKey, range) {
    const group = byKey[rangeShift];
    const unit = range.column_days === 1 ? "day's" : "week's";
    row.querySelectorAll("td.day-col").forEach((cell) => {
        const index = Number(cell.dataset.column);
        const value = group.columns[index];
        const when = columnPhrase(range.columns[index]);
        const total = value.counted + value.left_out;

        if (range.columns[index].before_tracking) {
            cell.setAttribute("data-empty", "");
            cell.title = `${when}: before OEE tracking began` +
                (range.tracking_start ? ` (${formatQuickPick(range.tracking_start)}).` : ".");
            return;
        }
        if (!total) {
            cell.setAttribute("data-empty", "");
            cell.title = `${when}: nothing entered.`;
            return;
        }
        if (value.oee === null) {
            cell.textContent = "!";
            cell.setAttribute("data-band", "poor");
            cell.title = `${when}: every shift left out (${value.left_out}). ` +
                "Hover the name on the left for why.";
            return;
        }

        cell.textContent = pct(value.oee, 0);
        setBand(cell, value.oee);
        const lines = [`${when}: ${pct(value.oee)} OEE over ${value.counted} of ${total} ` +
            `shift${total === 1 ? "" : "s"}.`];
        if (value.thin) {
            cell.setAttribute("data-warned", "");
            lines.push(`Fewer than half that ${unit} shifts counted.`);
        }
        if (rangeShift === ALL_SHIFTS) {
            for (const label of SHIFT_ORDER) {
                const one = byKey[label].columns[index];
                const text = one.oee !== null ? pct(one.oee)
                    : one.left_out ? "left out" : "—";
                lines.push(`   ${shortShift(label)}: ${text}`);
            }
        }
        cell.title = lines.join("\n");
    });
}

/** The floor's line of daily (or weekly) figures: "Mon 63 · Sun — · Sat 66". */
function renderFloorDays(range) {
    floorTotalDays.textContent = "";
    const group = range.floor[rangeShift];
    range.columns.forEach((column, index) => {
        const value = group.columns[index];
        const item = document.createElement("span");
        item.className = "floor-day";
        const label = isDayColumn(column)
            ? weekday(column.start)
            : `${monthDay(column.start)}–${monthDay(column.end)}`;
        const total = value.counted + value.left_out;
        const shown = value.oee !== null ? Math.round(value.oee * 100) : total ? "!" : "—";
        item.textContent = `${label} ${shown}`;
        setBand(item, value.oee);
        if (value.oee === null && total) item.setAttribute("data-band", "poor");
        item.title = column.before_tracking
            ? `${columnPhrase(column)}: before OEE tracking began`
            : value.oee !== null
                ? `${columnPhrase(column)}: ${pct(value.oee)} over ${value.counted} of ${total} machine-shifts`
                : total
                    ? `${columnPhrase(column)}: every machine-shift left out (${value.left_out})`
                    : `${columnPhrase(column)}: nothing entered`;
        floorTotalDays.appendChild(item);
    });
}

/** Performance and Quality need scrap, so they may cover fewer shifts. */
function splitTitle(current) {
    if (!current.split_shifts) return "Needs scrap to separate Performance from Quality";
    if (current.split_shifts === current.shifts) return "";
    return `From the ${current.split_shifts} of ${current.shifts} counted shifts with scrap entered`;
}

/** A machine's row over 7/30 days, or a zone's total row (byKey without the detail fields). */
function fillRangeRow(row, byKey, range) {
    fillDayCells(row, byKey, range);
    const group = byKey[rangeShift];
    const set = (field, text, title) => {
        const cell = row.querySelector(`[data-field="${field}"]`);
        if (!cell) return cell;
        cell.textContent = text;
        if (title) cell.title = title;
        return cell;
    };

    const total = group.counted + group.left_out;
    const operator = row.querySelector(".operator");
    if (!total) {
        // Nothing entered all period: didn't run, or wasn't scheduled.
        row.setAttribute("data-unscheduled", "");
        if (operator) operator.textContent = "no shifts";
        set("oee", "—", `Nothing entered, ${periodPhrase(range)}.`);
        return;
    }
    if (operator) operator.textContent = `${group.counted}/${total} shifts`;
    row.querySelector(".machine-col").title = countedTitle(group, range);

    const current = group.current;
    if (!current) {
        const cell = set("oee", "!", countedTitle(group, range));
        cell.setAttribute("data-band", "poor");
        return;
    }

    const cell = set("oee", pct(current.oee));
    setBand(cell, current.oee);
    const change = changeBadge(group);
    if (change) cell.appendChild(change);
    if (rangeShift === ALL_SHIFTS) cell.appendChild(byShiftLine(byKey));
    const title = [
        `${pct(current.oee)} OEE over ${group.counted} counted shift${group.counted === 1 ? "" : "s"}, ` +
        `${periodPhrase(range)}.`,
        changeTitle(group, range),
    ];
    if (group.thin) {
        cell.setAttribute("data-warned", "");
        title.push("Fewer than half this machine's shifts counted, so this number may not " +
            "represent the period. Hover the machine name for the ones left out.");
    }
    if (rangeShift === ALL_SHIFTS) title.push("By shift:", byShiftTitle(byKey));
    cell.title = title.join("\n");

    set("availability", pct(current.availability),
        `Ran ${count(Math.round(current.run_minutes))} of ${count(Math.round(current.ppt_minutes))} ` +
        "scheduled minutes");
    set("performance", pct(current.performance), splitTitle(current));
    set("quality", pct(current.quality), splitTitle(current));
    set("good", count(current.good), `Good units over ${current.shifts} counted shifts`);
    set("scrap", count(current.scrap), splitTitle(current));

    const planned = current.planned_minutes;
    const unplanned = current.unplanned_minutes;
    set("downtime", planned ? `${hoursText(unplanned)} + ${hoursText(planned)} planned` : hoursText(unplanned),
        `${count(unplanned)} min unplanned` + (planned ? `, ${count(planned)} min planned` : "") +
        ` over ${current.shifts} counted shifts`);
    const reasons = group.reasons || [];
    set("reasons", reasonSummary(reasons, 2, (minutes) => hoursText(minutes, "h")),
        reasons.map((r) => `${r.label}: ${count(r.minutes)} min on ${r.shifts} ` +
            `shift${r.shifts === 1 ? "" : "s"}`).join("\n"));
}

/** A zone's badge over 7/30 days, with its change and (under All) each shift. */
function fillRangeZone(section, range) {
    const target = section.querySelector("[data-rollup]");
    if (!target) return;
    const byKey = (range.zones || {})[section.dataset.zone];
    const group = byKey && byKey[rangeShift];
    if (!group || !group.current) {
        target.textContent = group && group.left_out
            ? "no OEE: every shift left out"
            : "no OEE: nothing counted in this period";
        return;
    }

    const current = group.current;
    target.textContent = [
        `OEE ${pct(current.oee)}`,
        `A ${pct(current.availability, 0)}`,
        `P ${pct(current.performance, 0)}`,
        `Q ${pct(current.quality, 1)}`,
    ].join(" · ");
    const change = changeBadge(group);
    if (change) target.appendChild(change);
    if (rangeShift === ALL_SHIFTS) target.appendChild(byShiftLine(byKey));
    target.setAttribute("data-band", bandFor(current.oee) || "unknown");

    const total = group.counted + group.left_out;
    const title = [`${group.counted} of ${total} machine-shifts counted, ${periodPhrase(range)}.`,
        changeTitle(group, range)];
    if (group.thin) {
        target.setAttribute("data-warned", "");
        title.push("Fewer than half this zone's shifts counted, so this number may not represent the period.");
    }
    if (current.split_shifts !== current.shifts) {
        title.push(`Performance and Quality cover the ${current.split_shifts} shifts with scrap entered.`);
    }
    if (rangeShift === ALL_SHIFTS) title.push("By shift:", byShiftTitle(byKey));
    target.title = title.join("\n");
}

/** The floor's headline over 7/30 days, in place of the one-shift completeness bar. */
function renderFloorTotal(range) {
    floorTotal.hidden = false;
    floorTotalOee.textContent = "";
    floorTotalDetail.textContent = "";
    floorTotalDays.textContent = "";
    floorTotalOee.removeAttribute("data-band");
    floorTotal.removeAttribute("data-warned");

    if (range === null || range === "error") {
        floorTotalDetail.textContent = range === null
            ? `Loading the last ${period} days…`
            : "Couldn't load that period. Check the connection and pick it again.";
        floorTotal.title = "";
        return;
    }

    const byKey = range.floor;
    const group = byKey[rangeShift];
    const total = group.counted + group.left_out;
    if (!group.current) {
        floorTotalDetail.textContent = `No OEE, ${periodPhrase(range)}.`;
        floorTotal.title = "";
        return;
    }

    const current = group.current;
    floorTotalOee.textContent = `OEE ${pct(current.oee)}`;
    setBand(floorTotalOee, current.oee);
    const change = changeBadge(group);
    if (change) floorTotalOee.appendChild(change);

    floorTotalDetail.textContent = [
        `A ${pct(current.availability, 0)}`,
        `P ${pct(current.performance, 0)}`,
        `Q ${pct(current.quality, 1)}`,
        `${count(group.counted)} of ${count(total)} machine-shifts counted`,
        periodPhrase(range),
    ].join(" · ");
    if (rangeShift === ALL_SHIFTS) floorTotalDetail.appendChild(byShiftLine(byKey));
    renderFloorDays(range);

    const title = [
        "Every counted machine-shift added together: total good units over the units " +
        "their scheduled time could have made. Not an average of percentages.",
        changeTitle(group, range),
    ];
    if (range.tracked_start && range.tracked_start !== range.start) {
        title.push(`OEE tracking began ${formatQuickPick(range.tracking_start)}, so this ` +
            "period's earlier days aren't in any figure.");
    }
    if (group.thin) {
        floorTotal.setAttribute("data-warned", "");
        title.push("Fewer than half the floor's shifts counted, so this number may not represent the period.");
    }
    if (rangeShift === ALL_SHIFTS) title.push("By shift:", byShiftTitle(byKey));
    floorTotal.title = title.join("\n");
}

// Left-out reasons that mean something wasn't entered. Every other reason
// means a number that was entered looks wrong.
const MISSING_CODES = new Set(["no_downtime", "no_production"]);

/**
 * Over 7/30 days: one line counting the left-out machine-shifts, opening to
 * two groups, numbers that look wrong and entries that are missing, each
 * listed by reason with the worst machines first. Dates are on each
 * machine's tooltip.
 */
function renderLeftOut(range) {
    leftOutDetails.textContent = "";
    const wrong = { groups: new Map(), shifts: 0 };
    const missing = { groups: new Map(), shifts: 0 };
    let total = 0;
    for (const machineId of ALL_MACHINE_IDS) {
        const group = range.machines[machineId] && range.machines[machineId][rangeShift];
        for (const item of (group && group.left_out_shifts) || []) {
            total += 1;
            const kinds = new Set();
            for (const code of item.reasons) {
                const kind = MISSING_CODES.has(code) ? missing : wrong;
                kinds.add(kind);
                if (!kind.groups.has(code)) kind.groups.set(code, new Map());
                const byMachine = kind.groups.get(code);
                byMachine.set(machineId, (byMachine.get(machineId) || 0) + 1);
            }
            kinds.forEach((kind) => { kind.shifts += 1; });
        }
    }
    if (!total) {
        leftOutEl.hidden = true;
        return;
    }
    leftOutEl.hidden = false;

    const parts = [];
    if (wrong.shifts) parts.push(`${count(wrong.shifts)} look wrong`);
    if (missing.shifts) parts.push(`${count(missing.shifts)} not entered`);
    leftOutCounts.textContent = `ⓘ ${count(total)} machine-shift${total === 1 ? "" : "s"} left out ` +
        `of these numbers: ${parts.join(", ")}.`;

    const plural = (n) => `${count(n)} machine-shift${n === 1 ? "" : "s"}`;
    appendLeftOutGroup(wrong, "flag-lead left-out-wrong",
        `Numbers that look wrong: ${plural(wrong.shifts)}. Fix at /console.`);
    appendLeftOutGroup(missing, "flag-lead flag-lead-warning",
        `Not entered: ${plural(missing.shifts)}. Enter on /console/oee.`);

    const hint = document.createElement("div");
    hint.className = "flag-hint";
    hint.textContent = "Hover a machine's shift count for the dates.";
    leftOutDetails.appendChild(hint);
}

function appendLeftOutGroup(kind, className, text) {
    if (!kind.shifts) return;
    const lead = document.createElement("div");
    lead.className = className;
    lead.textContent = text;
    leftOutDetails.appendChild(lead);

    const listed = new Map();
    for (const [code, byMachine] of kind.groups) {
        // Most first; ties keep page order, since the sort is stable.
        const worstFirst = Array.from(byMachine).sort((a, b) => b[1] - a[1]);
        listed.set(code, worstFirst.map(([id, n]) => (n > 1 ? `${id} ×${n}` : id)));
    }
    appendFlagGroup(leftOutDetails, listed, LEFT_OUT_LABELS);
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
        box.checked = paretoPicked.has(box.value);
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
    fetch(`/api/oee-range?end=${encodeURIComponent(date)}&days=${days}`)
        .then((res) => {
            if (!res.ok) throw new Error(`Request failed: ${res.status}`);
            return res.json();
        })
        .then((data) => rangeCache.set(key, data))
        .catch((err) => {
            rangeCache.set(key, "error");
            console.error("OEE range load failed:", err);
        })
        .finally(() => {
            // Only if the page still wants this range.
            if (payload && payload.date === date && period === days) render();
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
        records += machine[rangeShift].downtime_records;
        missing += machine[rangeShift].downtime_missing;
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
    if (!isRange()) {
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
        const range = rangeFor(payload.date, period);
        paretoScope.textContent = `${paretoScopeLabel()} · last ${period} days`;
        if (range === null || range === "error") {
            paretoTotal.textContent = "";
            paretoEmpty.textContent = range === null
                ? `Loading the last ${period} days…`
                : "Couldn't load that period. Check the connection and pick it again.";
            paretoEmpty.hidden = false;
            return;
        }
        items = rankReasons(
            Object.entries(range.machines)
                .filter(([machineId]) => paretoSelection.has(machineId))
                .map(([, machine]) => machine[rangeShift].pareto)
        );
        when = periodPhrase(range);
        unit = "machine-shift";
        renderCoverage(range);
    }
    paretoScope.textContent = `${paretoScopeLabel()} · ${when}`;

    if (!items.length) {
        paretoTotal.textContent = "";
        paretoEmpty.textContent = isRange()
            ? `No downtime recorded for ${paretoScopeLabel()} in these ${period} days.`
            : `No downtime recorded for ${paretoScopeLabel()} this shift.`;
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

/** The Period and Shift toggles. "All" only exists over 7/30 days. */
function syncToggles() {
    periodToggle.querySelectorAll(".shift-option").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.period === String(period));
    });
    shiftToggle.querySelectorAll(".shift-option").forEach((button) => {
        if (button.dataset.shift === ALL_SHIFTS) button.hidden = !isRange();
        button.classList.toggle("is-active", button.dataset.shift === shownShift());
    });
}

function render() {
    syncToggles();
    clearGrid();

    if (!payload) {
        syncUrl();
        return;
    }

    if (!payload.has_any_data) {
        showEmpty("No production entries have been recorded yet.");
    } else if (isRange()) {
        renderRange();
    } else {
        renderShift();
    }
    renderPareto();
    syncUrl();
}

function renderShift() {
    floorTotal.hidden = true;
    leftOutEl.hidden = true;
    syncDayColumns(null);
    document.querySelectorAll(".oee-grid tfoot").forEach((foot) => { foot.hidden = true; });
    const shiftData = payload.shifts[currentShift];
    if (!shiftData) return;

    const anyMachine = Object.values(payload.shifts).some((shift) =>
        Object.values(shift.machines).some((m) => m.shift !== null)
    );
    if (!anyMachine) {
        showEmpty(
            `No OEE can be computed for ${formatDate(payload.date)}. ` +
            "OEE needs both production units and a downtime entry for a shift, " +
            "and at least one of those is missing for every machine that day."
        );
    } else {
        hideEmpty();
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
}

function renderRange() {
    completenessEl.hidden = true;
    // The one-shift box; a period's left-out shifts go in the quiet line.
    flagsBanner.hidden = true;
    leftOutEl.hidden = true;
    const range = rangeFor(payload.date, period);
    const loaded = range !== null && range !== "error";
    syncDayColumns(loaded ? range : null);
    document.querySelectorAll(".oee-grid tfoot").forEach((foot) => { foot.hidden = !loaded; });
    renderFloorTotal(range);
    if (!loaded) {
        // The grid stays, blank, until the period arrives.
        hideEmpty();
        return;
    }

    const floor = range.floor[rangeShift];
    if (!floor.counted && !floor.left_out) {
        showEmpty(`Nothing has been entered for OEE, ${periodPhrase(range)}.`);
        floorTotal.hidden = true;
        return;
    }
    hideEmpty();

    document.querySelectorAll(".zone-section").forEach((section) => {
        section.querySelectorAll("tr[data-machine]").forEach((row) => {
            const byKey = range.machines[row.dataset.machine];
            if (byKey) fillRangeRow(row, byKey, range);
        });
        const zoneRow = section.querySelector("tr[data-zone-total]");
        const zone = (range.zones || {})[section.dataset.zone];
        if (zoneRow && zone) fillRangeRow(zoneRow, zone, range);
        fillRangeZone(section, range);
    });

    renderLeftOut(range);
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

// A period starts on All; going back to "This shift" shows the last single
// shift picked.
periodToggle.addEventListener("click", (event) => {
    const button = event.target.closest(".shift-option");
    if (!button) return;
    const days = parseInt(button.dataset.period, 10);
    const next = PERIOD_DAYS.includes(days) ? days : "shift";
    if (next === period) return;
    if (!isRange()) rangeShift = ALL_SHIFTS;
    period = next;
    render();
});

shiftToggle.addEventListener("click", (event) => {
    const button = event.target.closest(".shift-option");
    if (!button) return;
    const shift = button.dataset.shift;
    if (isRange()) rangeShift = shift;
    if (SHIFT_ORDER.includes(shift)) currentShift = shift;
    render(); // all three shifts, and each period's shifts, are already loaded
});

// A zone chip ticks exactly that zone's machines and All clears the ticks; a
// checkbox toggles one. Unticking the last one goes back to the whole floor.
paretoPicker.addEventListener("click", (event) => {
    const chip = event.target.closest(".pareto-chip");
    if (!chip) return;
    const zone = ZONES.find((z) => z.slug === chip.dataset.zone);
    setParetoPick(zone ? zone.machine_ids : []);
    renderPareto();
    syncUrl();
});

paretoPicker.addEventListener("change", (event) => {
    const box = event.target.closest('input[type="checkbox"]');
    if (!box) return;
    const picked = new Set(paretoPicked);
    if (box.checked) {
        picked.add(box.value);
    } else {
        picked.delete(box.value);
    }
    setParetoPick(ALL_MACHINE_IDS.filter((id) => picked.has(id)));
    renderPareto();
    syncUrl();
});

load(window.INITIAL_DATE || null);
