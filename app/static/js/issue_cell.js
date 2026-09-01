/**
 * Shared renderer for the Issue column cell, used by both dashboard.js (the
 * BrightSign boards) and supervisor.js (the review page).
 *
 * One cell per machine row still, but one LINE per reported issue inside it,
 * each prefixed with the time slot it was logged against:
 *
 *     10AM      belt slip
 *     2PM       waiting on forklift
 *
 * The slot prefix is the whole mechanism for telling the lines apart, which
 * is deliberate: the dashboards run on signage with no pointer and no
 * keyboard, so hover tooltips and click-to-expand — the usual answers to
 * "several things in one cell" — aren't available there.
 *
 * Lives in its own file because both pages must format these identically;
 * the run-collapsing rule that produces the ranges is server-side in
 * get_shift_activity() for the same reason.
 */

/**
 * Fills `cell` with one line per item in `issues` (the shape returned by
 * get_shift_activity: {time_slot, through, issue}).
 *
 * `maxLines` caps how many lines render, keeping the NEWEST ones and
 * summarising the rest as "+N earlier" above them — a shift is only four
 * slots, so this can't fire until a machine reports four genuinely different
 * issues, and it exists so a fixed-height board can't be pushed off-screen
 * by one bad night. Pass 0 (or omit) for no cap.
 */
function renderIssueCell(cell, issues, maxLines) {
    cell.textContent = "";
    cell.classList.remove("has-issue");

    if (!issues || !issues.length) {
        cell.textContent = "—";
        return;
    }

    let shown = issues;
    let hidden = 0;
    if (maxLines && issues.length > maxLines) {
        hidden = issues.length - maxLines;
        shown = issues.slice(hidden);
    }

    if (hidden) {
        const more = document.createElement("span");
        more.className = "issue-more";
        more.textContent = `+${hidden} earlier`;
        cell.appendChild(more);
    }

    for (const item of shown) {
        const line = document.createElement("span");
        line.className = "issue-line";

        const slot = document.createElement("span");
        slot.className = "issue-slot";
        // `through` is set when consecutive slots reported the same issue,
        // so the range stands in for what would otherwise be repeat lines.
        slot.textContent = item.through
            ? `${item.time_slot}–${item.through}`
            : item.time_slot;

        const text = document.createElement("span");
        text.className = "issue-text";
        // textContent, not innerHTML — `issue` is free text typed at
        // /console and goes straight to the screen.
        text.textContent = item.issue;

        line.appendChild(slot);
        line.appendChild(text);
        cell.appendChild(line);
    }

    cell.classList.add("has-issue");
}
