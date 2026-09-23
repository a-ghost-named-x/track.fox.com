/**
 * Renders the Issue cell for dashboard.js and supervisor.js: one line per
 * reported issue, prefixed with the slot it was logged against.
 *
 *     10AM      belt slip
 *     2PM       waiting on forklift
 *
 * The floor screens have no mouse or keyboard, so everything has to be
 * readable without tooltips.
 */

/**
 * Fills `cell` with one line per item in `issues` (the shape returned by
 * get_shift_activity: {time_slot, through, issue}).
 *
 * `maxLines` keeps only the newest lines and summarises the rest as
 * "+N earlier", so a fixed-height board can't overflow. Pass 0 (or omit)
 * for no cap.
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
        // `through` is set when consecutive slots reported the same issue.
        slot.textContent = item.through
            ? `${item.time_slot}–${item.through}`
            : item.time_slot;

        const text = document.createElement("span");
        text.className = "issue-text";
        // textContent, not innerHTML: `issue` is free text from /console.
        text.textContent = item.issue;

        line.appendChild(slot);
        line.appendChild(text);
        cell.appendChild(line);
    }

    cell.classList.add("has-issue");
}
