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
an untouched page doesn't append 34 identical rows.

Retired reason codes
--------------------
The picker only offers active codes. If a shift already has minutes against
a retired code, that code is shown on the machine's row (tagged "retired") so
a later correction carries it forward. Otherwise, because the newest
submission replaces the whole set, re-saving the shift would drop those
minutes.

Shift length
------------
Each shift's length is set on that shift's page: 8, 10 or 12 hours, or 2, 4 or 6
on a short day (rules in "Shift length" in app/models.py). Options that would
overlap an earlier shift are disabled on the page and rejected on save by
shift_length_conflict(). A 10 or 12-hour 2nd Shift defaults to not scheduled
and leaves no 3rd Shift that night; that row shows "no 3rd shift" with no
inputs.

The short day is a fourth radio with a number box inside it, so the radio
group always posts exactly one value. A number typed in the box while 8, 10
or 12 is picked is rejected as contradictory. The 3rd Shift page only shows
the length control on rows whose 2nd Shift is a short day, the only case
where a 3rd Shift has a choice.

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
    get_downtime_reasons,
    get_latest_downtime_for_date,
    get_latest_scrap_for_date,
    get_schedule_for_date,
    get_shift_lengths,
    machine_day_lengths,
)
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    DAY_START_HOUR,
    DEFAULT_SHIFT_HOURS,
    MAX_SHIFT_MINUTES,
    OEE_ZONE_ORDER,
    SHIFT_LENGTH_HOURS,
    SHIFT_ORDER,
    SHORT_PATTERN_HOURS,
    SHORT_SHIFT_HOURS,
    DowntimeReasonInput,
    ScheduleCreate,
    ShiftDowntimeCreate,
    ShiftLengthCreate,
    ShiftScrapCreate,
    default_production_day,
    default_scheduled,
    get_current_shift,
    long_length_options,
    pattern_hours,
    resolve_day_lengths,
    resolve_shift,
    shift_length_conflict,
    shift_slots,
    shift_span,
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
        # Shortest and longest pattern this shift may be, so the page can
        # disable options that would overlap. A short day's 2, 4 or 6 are all
        # pattern 6.
        "min_pattern": SHORT_PATTERN_HOURS,
        "max_pattern": max(SHIFT_LENGTH_HOURS),
        # Short-day box value: the stored hours on a short day, else empty.
        "short_value": "",
        # Whether this row gets a length control. On the 3rd Shift page,
        # only when the 2nd Shift is a short day.
        "length_editable": True,
        # Whether this shift's length was set on this page or inherited.
        "hours_explicit": False,
        # Whether `scheduled` came from a stored row or the default. The POST
        # needs this because changing the length can change the default.
        "scheduled_explicit": False,
        # For a shift that doesn't exist: which shift covers the night.
        "covered_by": None,
        # Every explicitly set length on this machine's day, for the overlap
        # check.
        "explicit": {},
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


def _retired_on_row(codes, active: dict, all_reasons: dict) -> list[dict]:
    """Which of `codes` are retired, with labels, so the form can show them.
    `codes` comes from the record (GET) or the submitted form (POST)."""
    return [
        {"code": code, "label": all_reasons.get(code, {}).get("label", code)}
        for code in codes
        if code not in active
    ]


def _existing_rows(entry_date: date, shift: str) -> dict[str, dict]:
    """Current state of every machine for this date and shift, as form
    values."""
    scrap = get_latest_scrap_for_date(entry_date)
    downtime = get_latest_downtime_for_date(entry_date)
    schedule = get_schedule_for_date(entry_date)
    active = get_downtime_reasons()
    lengths = get_shift_lengths(entry_date)
    index = SHIFT_ORDER.index(shift)

    rows: dict[str, dict] = {}
    for machine_id in _all_machines():
        row = _blank_row()
        day = machine_day_lengths(lengths, machine_id)
        hours = day[shift]
        row["shift_hours"] = hours
        row["shift_exists"] = shift_slots(shift, hours) is not None
        row["span"] = shift_span(shift, hours) or ""
        row["window_span"] = window_span(shift, hours) or ""
        row["shift_minutes"] = hours * 60
        row["short_value"] = str(hours) if hours <= SHORT_PATTERN_HOURS else ""
        row["hours_explicit"] = (machine_id, shift) in lengths
        row["explicit"] = {
            s_label: h for (m, s_label), h in lengths.items() if m == machine_id
        }
        if index > 0:
            row["min_pattern"] = pattern_hours(day[SHIFT_ORDER[index - 1]])
        later_explicit = [
            pattern_hours(row["explicit"][s_label]) for s_label in SHIFT_ORDER[index + 1:]
            if s_label in row["explicit"]
        ]
        if later_explicit:
            row["max_pattern"] = min(later_explicit)
        # After an 8-hour 2nd Shift, the 3rd can only be 8 hours.
        row["length_editable"] = (
            shift != SHIFT_ORDER[-1] or row["min_pattern"] == SHORT_PATTERN_HOURS
        )
        if not row["shift_exists"]:
            # No inputs are rendered for this row and the POST ignores it.
            previous = SHIFT_ORDER[index - 1]
            row["covered_by"] = {
                "shift": previous,
                "hours": day[previous],
                "span": shift_span(previous, day[previous]),
            }
            row["scheduled"] = False
            rows[machine_id] = row
            continue
        row["scheduled_explicit"] = (machine_id, shift) in schedule
        row["scheduled"] = schedule.get(
            (machine_id, shift), default_scheduled(shift, hours)
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
        # short-day option is on every page.
        "shift_length_options": long_length_options(shift),
        "short_shift_hours": SHORT_SHIFT_HOURS,
        "short_hours_text": SHORT_HOURS_TEXT,
        "short_pattern_hours": SHORT_PATTERN_HOURS,
        "default_shift_hours": DEFAULT_SHIFT_HOURS,
        # Page-level flags, used for the hint text.
        "is_last_shift": shift == SHIFT_ORDER[-1],
        "is_first_shift": shift == SHIFT_ORDER[0],
        "shift_index": SHIFT_ORDER.index(shift),
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
    "8, 10 or 12 hours, or a short day of 2, 4 or 6" (just "8 hours, ..." on
    the 3rd Shift)."""
    return f"{_or_list(long_length_options(shift))} hours, or a short day of {SHORT_HOURS_TEXT}"


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
            return None, f"Short day is picked but no hours are typed - enter {SHORT_HOURS_TEXT}."
        try:
            hours = int(raw_short)
        except ValueError:
            hours = None
        if hours not in SHORT_SHIFT_HOURS:
            return None, (
                f"Short-day hours must be {SHORT_HOURS_TEXT}, since production "
                "is only read every 2 hours."
            )
        return hours, None

    if raw_short:
        return None, (
            f"{raw_short} is typed in the short-day box but {raw_choice}h is picked - "
            "pick the short day, or clear the box."
        )
    try:
        hours = int(raw_choice)
    except ValueError:
        return None, f"Shift length must be {_length_menu(shift)}."
    if hours not in long_length_options(shift):
        return None, f"Shift length must be {_length_menu(shift)}."
    return hours, None


@router.post("/console/oee", response_class=HTMLResponse)
async def console_oee_submit(request: Request):
    """Saves shift length, scheduling, scrap and downtime for every machine
    that changed.

    Each of the four is written separately and skipped when unchanged.
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

    # Check the shared fields once rather than failing every row.
    if selected_date is None:
        top_error = "Enter a valid date before saving."
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

        # A shift that doesn't exist for this machine today. No inputs were
        # rendered, so there's nothing to save.
        if not previous["shift_exists"]:
            row = _blank_row()
            row.update({k: previous[k] for k in
                        ("shift_hours", "shift_exists", "span", "window_span",
                         "shift_minutes", "scheduled", "covered_by")})
            row["status"] = "unchanged"
            rows[machine_id] = row
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

        collected, errors = _collect_reasons(form, machine_id, reasons)

        # Shift length is a radio group with one option always checked, so it
        # posts a value whenever it was on the page. Absence means the row
        # had no length control; leave it alone.
        raw_hours = form.get(f"hours_{machine_id}")
        parsed_hours, hours_error = _parse_length(
            raw_hours, (form.get(f"short_hours_{machine_id}") or "").strip(), selected_shift
        )
        if parsed_hours is not None:
            # Check the whole day as it would be after this save.
            hours_error = shift_length_conflict(resolve_day_lengths(
                {**previous["explicit"], selected_shift: parsed_hours}
            ))
        # On error, keep the stored length for the downtime cap and re-render.
        submitted_hours = (
            parsed_hours if parsed_hours is not None and hours_error is None
            else previous["shift_hours"]
        )
        if hours_error:
            errors.append(hours_error)

        row = _blank_row()
        row.update(
            {
                "shift_hours": submitted_hours,
                "shift_exists": True,
                "span": shift_span(selected_shift, submitted_hours),
                "window_span": window_span(selected_shift, submitted_hours),
                "shift_minutes": submitted_hours * 60,
                "short_value": (
                    str(submitted_hours) if submitted_hours <= SHORT_PATTERN_HOURS else ""
                ),
                "min_pattern": previous["min_pattern"],
                "max_pattern": previous["max_pattern"],
                "length_editable": previous["length_editable"],
                "hours_explicit": previous["hours_explicit"] or raw_hours is not None,
                "explicit": previous["explicit"],
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
        rows[machine_id] = row

        # --- shift length ---------------------------------------------------
        # Saved even if the downtime fields have errors.
        if hours_error is None and submitted_hours != previous["shift_hours"]:
            any_change = True
            try:
                create_shift_length(
                    ShiftLengthCreate(
                        machine_id=machine_id,
                        entry_date=selected_date,
                        shift=selected_shift,
                        shift_hours=submitted_hours,
                        entered_by=entered_by,
                    )
                )
                row["saved_parts"].append(
                    f"{submitted_hours}h short day" if submitted_hours <= SHORT_PATTERN_HOURS
                    else f"{submitted_hours}h shifts"
                )
            except ValidationError:
                errors.append(f"Shift length must be {_length_menu(selected_shift)}.")

        # --- scheduling ---------------------------------------------------
        # Compare against the default for the length being saved, not the one
        # the page loaded with. Picking 12h on the 2nd Shift page flips the
        # default to not scheduled, so a tick on the same save must be
        # written or the night would show as not scheduled.
        baseline_scheduled = (
            previous["scheduled"] if previous["scheduled_explicit"]
            else default_scheduled(selected_shift, submitted_hours)
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

        if errors:
            row["status"] = "failed"
            # Show every failure for this machine, not just the first.
            row["error"] = " ".join(errors)
        elif row["saved_parts"]:
            row["status"] = "saved"
        else:
            row["status"] = "unchanged"

    top_error = (
        None if any_change
        else "Nothing changed - enter downtime, scrap, a shift length or a scheduling change first."
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
