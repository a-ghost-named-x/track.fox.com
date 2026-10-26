"""Routes for /console/oee, the end-of-shift OEE entry form.

One person enters the whole floor's downtime, scrap, scheduling and shift
lengths once per shift. The 2-hour rounds on /console/<zone> only record good
units.

app/main.py must register this router before console.py's, or /console/oee is
matched as /console/{zone} with zone "oee" and returns 404.

Pre-fill
--------
Unlike /console/<zone>, which several people share, this form is owned by one
person, so every field is pre-filled from what's on record. Each write path
compares against the stored value and skips when nothing changed, so saving
an untouched page doesn't append an identical row for every machine.

Retired reason codes
--------------------
The picker only offers active codes. If a shift already has minutes against
a retired code, that code is shown on the machine's row (tagged "retired") so
a later correction carries it forward. Otherwise, because the newest
submission replaces the whole set, re-saving the shift would drop those
minutes.

Shift length and start
----------------------
Each shift's length is set on that shift's page: 8, 10 or 12 hours (just 8 on
the 3rd Shift), or a short shift of 2, 4 or 6. The 2nd and 3rd Shift pages
also take when the shift started, which defaults to when the shift before it
ends (rules in "Shift length and start" in app/models.py). Options that would
overlap another shift or run past 6AM are disabled on the page and rejected
on save by new_conflict(). A 3rd Shift after a 2nd that runs until 6AM shows
"no 3rd shift" and, if a later crew could still fit, only a start picker.

The short shift is a fourth radio with a number box inside it, so the radio
group always posts exactly one value. A number typed in the box while 8, 10
or 12 is picked is rejected as contradictory. The start is a <select>, which
also always posts its value; picking the default start stores "follows the
shift before" rather than a fixed hour.

The date is the production day the shift started on. The 3rd Shift page
defaults to yesterday when opened in the morning.
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.db.oee import (
    DowntimeExceedsShiftError,
    UnknownReasonCodeError,
    create_schedule_exception,
    create_shift_downtime,
    create_shift_length,
    create_shift_scrap,
    create_shift_start,
    get_downtime_reasons,
    get_latest_downtime_for_date,
    get_latest_scrap_for_date,
    get_schedule_for_date,
    get_shift_lengths,
    get_shift_starts,
)
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    DAY_START_HOUR,
    DEFAULT_SHIFT_HOURS,
    MAX_SHIFT_MINUTES,
    OEE_ZONE_ORDER,
    SHIFT_ORDER,
    SHORT_SHIFT_HOURS,
    DowntimeReasonInput,
    ScheduleCreate,
    ShiftDowntimeCreate,
    ShiftLengthCreate,
    ShiftScrapCreate,
    ShiftStartCreate,
    clock_hour,
    day_plan,
    default_production_day,
    default_scheduled,
    default_start,
    get_current_shift,
    long_length_options,
    new_conflict,
    offset_label,
    resolve_shift,
    shift_span,
    start_offset,
    start_options,
    window_span,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _parse_entry_date(raw: str | None) -> date | None:
    """Parses the date box, or None if missing/unparseable. The GET falls
    back to a default; the POST shows an error."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _zones() -> list[dict]:
    """Zone sections in the same order as /supervisor and /oee."""
    sections = []
    for slug in OEE_ZONE_ORDER:
        machine_ids = DASHBOARD_ZONES.get(slug)
        if machine_ids is None:
            continue
        sections.append(
            {
                "slug": slug,
                "label": DASHBOARD_ZONE_LABELS.get(slug, slug.upper()),
                "machine_ids": machine_ids,
            }
        )
    return sections


def _all_machines() -> list[str]:
    """Every machine this form renders, in the order the zones lay them out."""
    return [m for zone in _zones() for m in zone["machine_ids"]]


def _blank_row() -> dict:
    return {
        "shift_hours": DEFAULT_SHIFT_HOURS,
        "shift_exists": True,
        "span": "",
        "window_span": "",
        "shift_minutes": DEFAULT_SHIFT_HOURS * 60,
        # Length options that would overlap another shift on this machine's
        # day, keyed by radio value, with the reason (see _fill_geometry()).
        "blocked": {},
        # The 2nd/3rd Shift's start as a clock hour, the hours it can be set
        # to, and the one it takes when nothing is set. None on the 1st Shift.
        "start": None,
        "start_options": [],
        "default_start": None,
        "start_explicit": False,
        # Start options that don't fit with this row's current length, with
        # the reason.
        "blocked_starts": {},
        # For the JS: which starts each length can't take, keyed by hours.
        "conflicts": None,
        # Where a short shift on this row would sit, for its tooltip.
        "short_window_span": "",
        # Short-shift box value: the stored hours on a short shift, else empty.
        "short_value": "",
        # Why Scheduled starts unticked, when it does (see _unticked_hint()).
        "unticked_hint": None,
        # Whether this shift's length was set on this page or inherited.
        "hours_explicit": False,
        # Whether `scheduled` came from a stored row or the default. The POST
        # needs this because changing the length can change the default.
        "scheduled_explicit": False,
        # For a shift that doesn't exist: which shift covers the night.
        "covered_by": None,
        # Every explicitly set length and start on this machine's day, for
        # the overlap check.
        "explicit": {},
        "explicit_starts": {},
        "scheduled": True,
        "ran_clean": False,
        "scrap": "",
        "note": "",
        "minutes": {},
        "retired": [],
        "status": None,
        "error": None,
        "saved_parts": [],
    }


def _hour_text(hour: int) -> str:
    """A clock hour as the pages write it: 14 -> "2PM"."""
    return offset_label(start_offset(hour))


def _retired_on_row(codes, active: dict, all_reasons: dict) -> list[dict]:
    """Which of `codes` are retired, with labels, so the form can show them.
    `codes` comes from the record (GET) or the submitted form (POST)."""
    return [
        {"code": code, "label": all_reasons.get(code, {}).get("label", code)}
        for code in codes
        if code not in active
    ]


def _unticked_hint(shift: str, plan: dict[str, dict]) -> str | None:
    """Why `shift` starts unticked on a machine's day, or None when it starts
    ticked (or doesn't exist). Shown on the Scheduled box."""
    entry = plan[shift]
    if default_scheduled(shift, plan) or entry["slots"] is None:
        return None
    if entry["window"] > DEFAULT_SHIFT_HOURS:
        return (
            f"A {entry['hours']}h night crew ({shift_span(shift, plan)}) is the exception, "
            "so this starts unticked unless set. Tick it if one ran."
        )
    earlier_long = next(
        (label for label in SHIFT_ORDER[:SHIFT_ORDER.index(shift)]
         if plan[label]["hours"] > DEFAULT_SHIFT_HOURS),
        None,
    )
    if earlier_long:
        return (
            f"The {earlier_long} ran {plan[earlier_long]['hours']}h today, so a crew after "
            "it is the exception and this starts unticked. Tick it if one ran."
        )
    return (
        "This machine has a short shift today, so this starts unticked: on those "
        "days not every crew runs. Tick it if this machine ran."
    )


def _fill_geometry(row: dict, shift: str, lengths: dict[str, int], starts: dict[str, int]) -> None:
    """Fills a row's length, start and overlap fields for `shift` from the
    machine's day as set (`lengths` and `starts` hold only what was set)."""
    plan = day_plan(lengths, starts)
    entry = plan[shift]
    index = SHIFT_ORDER.index(shift)
    hours = entry["hours"]

    row["explicit"] = dict(lengths)
    row["explicit_starts"] = dict(starts)
    row["shift_hours"] = hours
    row["shift_exists"] = entry["slots"] is not None
    row["span"] = shift_span(shift, plan) or ""
    row["window_span"] = window_span(shift, plan) or ""
    row["shift_minutes"] = hours * 60
    row["short_value"] = str(hours) if hours < DEFAULT_SHIFT_HOURS else ""
    row["hours_explicit"] = entry["hours_set"]
    row["start_explicit"] = entry["start_set"]
    row["short_window_span"] = (
        window_span(shift, day_plan({**lengths, shift: SHORT_SHIFT_HOURS[0]}, starts)) or ""
    )
    row["unticked_hint"] = _unticked_hint(shift, plan)

    if not row["shift_exists"]:
        previous = SHIFT_ORDER[index - 1]
        row["covered_by"] = {
            "shift": previous,
            "hours": plan[previous]["hours"],
            "span": shift_span(previous, plan),
            "window_span": window_span(previous, plan),
        }
        # A later crew can still be given a start when the shift before ends
        # early inside a long night (a 4h crew after a 12h day).
        row["start_options"] = [
            {"hour": h, "label": _hour_text(h)} for h in start_options(shift, plan)
        ]
        row["start"] = clock_hour(entry["start"])
        row["default_start"] = row["start"]
        return
    row["covered_by"] = None

    # Every length the page offers, the short ones by their hours.
    candidates = {str(h): h for h in long_length_options(shift)}
    candidates.update({str(h): h for h in SHORT_SHIFT_HOURS})

    def conflict(hours_value: int | None, start_value: int | None) -> str | None:
        trial_lengths = lengths if hours_value is None else {**lengths, shift: hours_value}
        trial_starts = starts if start_value is None else {**starts, shift: start_value}
        return new_conflict(plan, day_plan(trial_lengths, trial_starts))

    # Lengths that won't fit at the current start. The short radio is only
    # blocked when no short length fits; the JS checks the typed hours.
    blocked = {
        str(h): message for h in long_length_options(shift) if (message := conflict(h, None))
    }
    short_messages = [conflict(h, None) for h in SHORT_SHIFT_HOURS]
    if all(short_messages):
        blocked["short"] = short_messages[0]
    row["blocked"] = blocked

    if index == 0:
        return

    row["start"] = clock_hour(entry["start"])
    row["default_start"] = default_start(shift, lengths, starts)
    options = start_options(shift, plan)
    if row["start"] not in options:
        options = [row["start"], *options]
    row["start_options"] = [{"hour": h, "label": _hour_text(h)} for h in options]
    # Checked against the length shown, even an inherited one: picking a
    # start keeps that length (see the POST), so 8h from 12AM doesn't fit.
    row["blocked_starts"] = {
        h: message for h in options if (message := conflict(hours, h))
    }
    row["conflicts"] = {
        value: [h for h in options if conflict(option, h)]
        for value, option in candidates.items()
    }


def _existing_rows(entry_date: date, shift: str) -> dict[str, dict]:
    """Current state of every machine for this date and shift, as form
    values."""
    scrap = get_latest_scrap_for_date(entry_date)
    downtime = get_latest_downtime_for_date(entry_date)
    schedule = get_schedule_for_date(entry_date)
    active = get_downtime_reasons()
    lengths = get_shift_lengths(entry_date)
    starts = get_shift_starts(entry_date)

    rows: dict[str, dict] = {}
    for machine_id in _all_machines():
        row = _blank_row()
        machine_lengths = {s: h for (m, s), h in lengths.items() if m == machine_id}
        machine_starts = {s: h for (m, s), h in starts.items() if m == machine_id}
        _fill_geometry(row, shift, machine_lengths, machine_starts)
        if not row["shift_exists"]:
            # Nothing but the start picker is rendered for this row.
            row["scheduled"] = False
            rows[machine_id] = row
            continue
        plan = day_plan(machine_lengths, machine_starts)
        row["scheduled_explicit"] = (machine_id, shift) in schedule
        row["scheduled"] = schedule.get(
            (machine_id, shift), default_scheduled(shift, plan)
        )

        scrap_value = scrap.get((machine_id, shift))
        if scrap_value is not None:
            row["scrap"] = str(scrap_value)

        record = downtime.get((machine_id, shift))
        if record is not None:
            row["note"] = record["note"] or ""
            row["minutes"] = {r["code"]: str(r["minutes"]) for r in record["reasons"]}
            # The record's reasons already carry labels, retired ones included.
            row["retired"] = _retired_on_row(
                row["minutes"], active, {r["code"]: r for r in record["reasons"]}
            )
            # A submission with no reasons means "ran clean", shown as the
            # ticked "no downtime" box.
            row["ran_clean"] = not record["reasons"]

        rows[machine_id] = row
    return rows


def _context(
    shift: str,
    entry_date: date,
    *,
    rows: dict[str, dict],
    entered_by: str = "",
    top_error: str | None = None,
):
    """Template context shared by the GET and the POST. `shift` and
    `entry_date` are the user's choice, never re-derived from the clock."""
    saved_count = sum(1 for row in rows.values() if row["status"] == "saved")
    failed_rows = [(m, r) for m, r in rows.items() if r["status"] == "failed"]
    next_day = entry_date + timedelta(days=1)

    return {
        "zones": _zones(),
        "reasons": list(get_downtime_reasons().values()),
        "shift": shift,
        "current_shift": get_current_shift(datetime.now()),
        "shift_options": SHIFT_ORDER,
        "entry_date": entry_date.isoformat(),
        "entered_by": entered_by,
        "rows": rows,
        "saved_count": saved_count,
        "failed_rows": failed_rows,
        "top_error": top_error,
        "max_shift_minutes": MAX_SHIFT_MINUTES,
        # 8/10/12 on the 1st and 2nd Shift pages, just 8 on the 3rd. The
        # short-shift option is on every page.
        "shift_length_options": long_length_options(shift),
        "short_shift_hours": SHORT_SHIFT_HOURS,
        "short_hours_text": SHORT_HOURS_TEXT,
        "default_shift_hours": DEFAULT_SHIFT_HOURS,
        # Page-level flags, used for the hint text.
        "is_last_shift": shift == SHIFT_ORDER[-1],
        "is_first_shift": shift == SHIFT_ORDER[0],
        "day_start_hour": DAY_START_HOUR,
        # "follows 1st" / "follows 2nd" on a row whose length is inherited.
        "previous_shift": SHIFT_ORDER[SHIFT_ORDER.index(shift) - 1].split(" ")[0]
        if shift != SHIFT_ORDER[0] else "",
        # "Thu 9/18" and "Fri 9/19". Built by hand because strftime has no
        # portable no-leading-zero format.
        "day_label": f"{entry_date:%a} {entry_date.month}/{entry_date.day}",
        "next_day_label": f"{next_day:%a} {next_day.month}/{next_day.day}",
        # The 8-hour span of the selected shift. Rows on other lengths show
        # their own.
        "default_span": shift_span(shift) or "",
        "crosses_midnight": shift == SHIFT_ORDER[-1],
    }


@router.get("/console/oee", response_class=HTMLResponse)
def console_oee_page(
    request: Request, shift: str | None = None, entry_date: str | None = None
):
    """The end-of-shift entry form, every machine on one page.

    `shift` and `entry_date` default to the current shift and its production
    day, which for 3rd Shift opened in the morning is yesterday (see
    default_production_day()).
    """
    now = datetime.now()
    selected_shift = resolve_shift(shift, now)
    selected_date = _parse_entry_date(entry_date) or default_production_day(
        selected_shift, now
    )

    return templates.TemplateResponse(
        request=request,
        name="console_oee.html",
        context=_context(
            selected_shift,
            selected_date,
            rows=_existing_rows(selected_date, selected_shift),
        ),
    )


def _collect_reasons(form, machine_id: str, reasons: dict) -> tuple[list, list[str]]:
    """Reads one machine's ticked reasons and their minutes off the form.

    Returns (reason inputs, errors). A ticked reason with no minutes is an
    error, and so are minutes typed against an unticked reason.

    `reasons` is the full code table, so a retired code shown on this row is
    read back like any other.
    """
    collected: list[DowntimeReasonInput] = []
    errors: list[str] = []

    for code in reasons:
        ticked = form.get(f"dt_on_{machine_id}_{code}") is not None
        raw_minutes = (form.get(f"dt_min_{machine_id}_{code}") or "").strip()

        if not ticked:
            if raw_minutes:
                errors.append(
                    f"{reasons[code]['label']} has minutes but isn't ticked."
                )
            continue

        if not raw_minutes:
            errors.append(f"{reasons[code]['label']} is ticked but has no minutes.")
            continue

        try:
            collected.append(
                DowntimeReasonInput(reason_code=code, minutes=int(raw_minutes))
            )
        # ValidationError subclasses ValueError, so it must be caught first.
        except ValidationError:
            errors.append(
                f"{reasons[code]['label']} minutes must be between 1 and {MAX_SHIFT_MINUTES}."
            )
        except ValueError:
            errors.append(f"{reasons[code]['label']} minutes must be a whole number.")

    return collected, errors


def _or_list(values) -> str:
    """[2, 4, 6] -> "2, 4 or 6"."""
    items = [str(v) for v in values]
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} or {items[-1]}"


# "2, 4 or 6", for the page's hint text and the error messages.
SHORT_HOURS_TEXT = _or_list(SHORT_SHIFT_HOURS)


def _length_menu(shift: str) -> str:
    """The lengths `shift` can take, as a sentence for error messages:
    "8, 10 or 12 hours, or a short shift of 2, 4 or 6" (just "8 hours, ..." on
    the 3rd Shift)."""
    return f"{_or_list(long_length_options(shift))} hours, or a short shift of {SHORT_HOURS_TEXT}"


def _parse_length(raw_choice: str | None, raw_short: str, shift: str) -> tuple[int | None, str | None]:
    """Reads one machine's length control: (hours, None), (None, error), or
    (None, None) when the control wasn't on the page at all.

    `raw_choice` is the radio value ("8", "10", "12" or "short") and
    `raw_short` the box inside the short option, which only counts when
    "short" is picked. The caller checks the result against the rest of the
    day with shift_length_conflict().
    """
    if raw_choice is None:
        return None, None

    if raw_choice == "short":
        if not raw_short:
            return None, f"Short shift is picked but no hours are typed - enter {SHORT_HOURS_TEXT}."
        try:
            hours = int(raw_short)
        except ValueError:
            hours = None
        if hours not in SHORT_SHIFT_HOURS:
            return None, (
                f"Short-shift hours must be {SHORT_HOURS_TEXT}, since production "
                "is only read every 2 hours."
            )
        return hours, None

    if raw_short:
        return None, (
            f"{raw_short} is typed in the short-shift box but {raw_choice}h is picked - "
            "pick the short shift, or clear the box."
        )
    try:
        hours = int(raw_choice)
    except ValueError:
        return None, f"Shift length must be {_length_menu(shift)}."
    if hours not in long_length_options(shift):
        return None, f"Shift length must be {_length_menu(shift)}."
    return hours, None


def _parse_start(raw: str | None, row: dict) -> tuple[int | None, str | None]:
    """Reads one machine's start control: (clock hour, None), (None, error),
    or (None, None) when it wasn't on the page or was left on "no 3rd crew".

    Any of the row's start options is accepted here, since the length may
    change on the same save; the caller checks the result with
    new_conflict().
    """
    if not raw:
        return None, None
    allowed = {str(option["hour"]): option["hour"] for option in row["start_options"]}
    if raw not in allowed:
        if not allowed:
            return None, "This shift's start can't be set: the shift before it runs until 6AM."
        labels = _or_list(option["label"] for option in row["start_options"])
        return None, f"Start must be {labels}."
    return allowed[raw], None


def _set_status(row: dict, errors: list[str]) -> None:
    """Marks a row failed, saved or unchanged after its writes."""
    if errors:
        row["status"] = "failed"
        # Show every failure for this machine, not just the first.
        row["error"] = " ".join(errors)
    elif row["saved_parts"]:
        row["status"] = "saved"
    else:
        row["status"] = "unchanged"


@router.post("/console/oee", response_class=HTMLResponse)
async def console_oee_submit(request: Request):
    """Saves shift length and start, scheduling, scrap and downtime for
    every machine that changed.

    Each is written separately and skipped when unchanged.
    Machines are also saved independently, so one bad value doesn't block the
    rest.
    """
    form = await request.form()
    entered_by = (form.get("entered_by") or "").strip()
    selected_shift = resolve_shift((form.get("shift") or "").strip(), datetime.now())
    selected_date = _parse_entry_date(form.get("entry_date"))
    active = get_downtime_reasons()
    # Full table, so retired codes on the form are read back too.
    reasons = get_downtime_reasons(active_only=False)

    # The day the boxes were pre-filled from. Missing on a page loaded before
    # the field existed.
    loaded_date = _parse_entry_date(form.get("loaded_date"))

    # Check the shared fields once rather than failing every row.
    if selected_date is None:
        top_error = "Enter a valid date before saving."
    elif loaded_date is not None and loaded_date != selected_date:
        # Saving would file the loaded day's pre-filled values under the new
        # date, so show the new date's record instead.
        top_error = (
            "Nothing was saved: the date was changed after the page loaded. This is "
            f"{selected_date:%a} {selected_date.month}/{selected_date.day}'s record now; "
            "enter its numbers and save again."
        )
    elif not entered_by:
        top_error = "Fill in your employee number before saving."
    else:
        top_error = None

    if top_error is not None:
        fallback_date = selected_date or date.today()
        return templates.TemplateResponse(
            request=request,
            name="console_oee.html",
            context=_context(
                selected_shift,
                fallback_date,
                rows=_existing_rows(fallback_date, selected_shift),
                entered_by=entered_by,
                top_error=top_error,
            ),
            status_code=400,
        )

    # Every write path below compares against the current state.
    stored = _existing_rows(selected_date, selected_shift)
    rows: dict[str, dict] = {}
    any_change = False

    for machine_id in _all_machines():
        previous = stored[machine_id]
        lengths_before = previous["explicit"]
        starts_before = previous["explicit_starts"]
        errors: list[str] = []

        # The length is a radio group with one option always checked and the
        # start a <select>, so each posts a value whenever it was on the page.
        # Absence means the row had no such control; leave it alone.
        parsed_hours, hours_error = None, None
        if previous["shift_exists"]:
            parsed_hours, hours_error = _parse_length(
                form.get(f"hours_{machine_id}"),
                (form.get(f"short_hours_{machine_id}") or "").strip(),
                selected_shift,
            )
        parsed_start, start_error = _parse_start(form.get(f"start_{machine_id}"), previous)

        # This machine's day as it would be after the save. Picking the
        # default start stores "follows the shift before" rather than the
        # hour. The length is stored once it differs from what the shift
        # would inherit from that start, so an inherited length stays
        # inherited and one shown on the page is never silently shortened.
        starts_after = dict(starts_before)
        if parsed_start is not None and parsed_start != previous["start"]:
            if parsed_start == previous["default_start"]:
                starts_after.pop(selected_shift, None)
            else:
                starts_after[selected_shift] = parsed_start
        lengths_after = dict(lengths_before)
        if parsed_hours is not None:
            unset = {s: h for s, h in lengths_before.items() if s != selected_shift}
            inherited = day_plan(unset, starts_after)[selected_shift]["hours"]
            if selected_shift in lengths_before or parsed_hours != inherited:
                lengths_after[selected_shift] = parsed_hours
        geometry_error = hours_error or start_error
        if geometry_error is None and (lengths_after, starts_after) != (lengths_before, starts_before):
            geometry_error = new_conflict(
                day_plan(lengths_before, starts_before), day_plan(lengths_after, starts_after)
            )
        if geometry_error:
            errors.append(geometry_error)
            # Keep the stored day for the downtime cap and the re-render.
            lengths_after, starts_after = lengths_before, starts_before
        plan_after = day_plan(lengths_after, starts_after)

        row = _blank_row()
        _fill_geometry(row, selected_shift, lengths_after, starts_after)
        rows[machine_id] = row

        # --- shift length and start -----------------------------------------
        # Saved even if the downtime fields have errors.
        hours_after = lengths_after.get(selected_shift)
        if hours_after != lengths_before.get(selected_shift):
            any_change = True
            try:
                create_shift_length(
                    ShiftLengthCreate(
                        machine_id=machine_id,
                        entry_date=selected_date,
                        shift=selected_shift,
                        shift_hours=hours_after,
                        entered_by=entered_by,
                    )
                )
                row["saved_parts"].append(
                    f"{hours_after}h short shift" if hours_after < DEFAULT_SHIFT_HOURS
                    else f"{hours_after}h shifts"
                )
            except ValidationError:
                errors.append(f"Shift length must be {_length_menu(selected_shift)}.")

        start_after = starts_after.get(selected_shift)
        if start_after != starts_before.get(selected_shift):
            any_change = True
            try:
                create_shift_start(
                    ShiftStartCreate(
                        machine_id=machine_id,
                        entry_date=selected_date,
                        shift=selected_shift,
                        start_hour=start_after,
                        entered_by=entered_by,
                    )
                )
                previous_shift = SHIFT_ORDER[SHIFT_ORDER.index(selected_shift) - 1]
                row["saved_parts"].append(
                    f"starts {_hour_text(start_after)}" if start_after is not None
                    else f"starts when the {previous_shift} ends"
                )
            except ValidationError:
                errors.append("Couldn't save the start time.")

        # A shift the day had no room for has nothing else on the page. One
        # that was just given a start comes back with its inputs, to fill in
        # on the next save.
        if not previous["shift_exists"]:
            row["scheduled"] = default_scheduled(selected_shift, plan_after)
            _set_status(row, errors)
            continue

        submitted_scheduled = form.get(f"scheduled_{machine_id}") is not None
        # An unticked checkbox and a missing one look the same in a POST.
        # Reading absence as "unticked" would mark machines not on the form as
        # not scheduled and inflate OEE, so a hidden companion field proves
        # the checkbox was on the page.
        sched_present = form.get(f"sched_present_{machine_id}") is not None
        ran_clean = form.get(f"dt_none_{machine_id}") is not None
        raw_scrap = (form.get(f"scrap_{machine_id}") or "").strip()
        note = (form.get(f"note_{machine_id}") or "").strip()

        collected, reason_errors = _collect_reasons(form, machine_id, reasons)
        errors.extend(reason_errors)

        row.update(
            {
                "scheduled": submitted_scheduled if sched_present else previous["scheduled"],
                "ran_clean": ran_clean,
                "scrap": raw_scrap,
                "note": note,
                "minutes": {
                    code: (form.get(f"dt_min_{machine_id}_{code}") or "").strip()
                    for code in reasons
                    if form.get(f"dt_on_{machine_id}_{code}") is not None
                },
            }
        )
        # A retired code was on this row if its minutes box was posted
        # (number inputs post even when empty), ticked or not.
        row["retired"] = _retired_on_row(
            [c for c in reasons if form.get(f"dt_min_{machine_id}_{c}") is not None],
            active,
            reasons,
        )

        # --- scheduling ---------------------------------------------------
        # Compare against the default for the day being saved, not the one
        # the page loaded with. Picking 12h or a short shift on the 2nd Shift
        # page flips the default to not scheduled, so a tick on the same save
        # must be written or the shift would show as not scheduled.
        baseline_scheduled = (
            previous["scheduled"] if previous["scheduled_explicit"]
            else default_scheduled(selected_shift, plan_after)
        )
        if sched_present and submitted_scheduled != baseline_scheduled:
            any_change = True
            try:
                create_schedule_exception(
                    ScheduleCreate(
                        machine_id=machine_id,
                        entry_date=selected_date,
                        shift=selected_shift,
                        scheduled=submitted_scheduled,
                        entered_by=entered_by,
                    )
                )
                row["saved_parts"].append(
                    "scheduled" if submitted_scheduled else "not scheduled"
                )
            except ValidationError:
                errors.append("Couldn't save the scheduled flag.")

        # --- scrap ----------------------------------------------------------
        if raw_scrap != previous["scrap"]:
            if raw_scrap:
                any_change = True
                try:
                    create_shift_scrap(
                        ShiftScrapCreate(
                            machine_id=machine_id,
                            entry_date=selected_date,
                            shift=selected_shift,
                            scrap_units=int(raw_scrap),
                            entered_by=entered_by,
                        )
                    )
                    row["saved_parts"].append("scrap")
                except ValidationError:
                    errors.append("Scrap can't be negative.")
                except ValueError:
                    errors.append("Scrap must be a whole number.")
            # Clearing the box doesn't write anything. To correct scrap, type
            # the right number.

        # --- downtime -------------------------------------------------------
        # "No downtime" plus a list of reasons is contradictory.
        if ran_clean and collected:
            errors.append(
                'Untick "no downtime" or clear the reasons - a shift cannot be both.'
            )
        elif not errors and (ran_clean or collected):
            submitted = {r.reason_code: r.minutes for r in collected}
            existing = {code: int(m) for code, m in previous["minutes"].items()}
            unchanged = (
                submitted == existing
                and note == previous["note"]
                and (ran_clean == previous["ran_clean"])
            )
            if not unchanged:
                any_change = True
                try:
                    create_shift_downtime(
                        ShiftDowntimeCreate(
                            machine_id=machine_id,
                            entry_date=selected_date,
                            shift=selected_shift,
                            reasons=collected,
                            note=note or None,
                            entered_by=entered_by,
                        ),
                        # Includes a length changed on this save.
                        shift_minutes=row["shift_minutes"],
                    )
                    row["saved_parts"].append(
                        "no downtime" if not collected else "downtime"
                    )
                except (UnknownReasonCodeError, DowntimeExceedsShiftError) as exc:
                    # Both carry readable messages.
                    errors.append(str(exc))
                except ValidationError:
                    errors.append("Couldn't save downtime - check the values.")

        _set_status(row, errors)

    top_error = (
        None if any_change
        else ("Nothing changed - enter downtime, scrap, a shift length or start, "
              "or a scheduling change first.")
    )
    return templates.TemplateResponse(
        request=request,
        name="console_oee.html",
        context=_context(
            selected_shift,
            selected_date,
            rows=rows,
            entered_by=entered_by,
            top_error=top_error,
        ),
        status_code=200 if any_change else 400,
    )
