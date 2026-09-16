"""Routes for /console/oee — end-of-shift OEE data entry.

One person, once per shift, enters the whole floor's downtime and scrap in a
single sitting. Deliberately separate from /console and /console/<zone>, which
went back to good units only: the 2-hour rounds are a walking job with a clock
running, and folding downtime capture into them was adding work to every lap.
This is sit-down work at the end of the shift.

That split is also better for the metric. A per-slot downtime figure has to be
attributed to a 2-hour window whose boundaries nobody reads exactly, and the
resulting noise produced false "impossible value" alarms on WS1 and C8 in the
first week. A shift is 480 minutes however the readings fell.

ROUTE ORDER MATTERS
-------------------
/console/oee would otherwise be captured by /console/{zone} in
app/routers/console.py and 404 as a zone named "oee". FastAPI matches routes in
registration order, so app/main.py includes THIS router before that one. Keep
it that way — the failure is silent and looks like a missing page.

PREFILL POLICY, AND WHY IT DIFFERS FROM THE PRODUCTION FORM
-----------------------------------------------------------
This form pre-fills every field from what is already on record, and the batch
production form deliberately does not. The difference is how many people touch
each one.

/console/<zone> is shared: several people submit it during a shift, so a blank
box has to mean "I'm not touching this" or one person's save wipes another's
numbers. Here a single person owns the whole form, so pre-filling makes
reviewing and correcting a shift natural — open it, see what's recorded, adjust,
save.

To keep that from appending an identical row for all 34 machines every time
somebody opens and saves the page, each write path compares against what is
already stored and skips when nothing changed. The tables stay append-only; the
"saved" badges then mean something, because only genuinely changed machines get
one.

RETIRED REASON CODES STAY ON THE SHIFTS THAT HAVE THEM
------------------------------------------------------
The reason picker lists active codes only — that is what retiring a code
(`downtime_reasons.active = false`) means. But pre-fill plus newest-header-
wins is a trap for shifts entered before a retirement: if the form silently
left a retired code off a machine's row, re-saving that shift for any reason
(fixing the note, correcting another reason's minutes) would post only the
codes it could see, and the old minutes would drop out of that shift's record.

So a retired code is rendered on a machine's row when, and only when, that
shift already has it on record — tagged "retired", with the same checkbox and
minutes box, so it carries forward untouched by default or can be deliberately
unticked and reclassified. An untouched machine never shows it, which is what
keeps new use impossible. First needed by docs/sql/13_split_operator_adjustments.sql.

SHIFT LENGTH IS SET ON THE 1ST SHIFT PAGE, AND ONLY THERE
--------------------------------------------------------
A machine's shift length (8, 10 or 12 hours) is a fact about its PRODUCTION
DAY, not about one shift: the day starts at 6AM and the pattern never mixes.
So the control is offered on the 1st Shift page — the date box there IS the
production day — and the 2nd and 3rd Shift pages show it read-only. That is
also the moment the decision is made; per the floor, "at 6AM tomorrow they
could decide to run an 8-hour shift".

Two things follow from a 10 or 12-hour day, both handled by app/models.py:
the 2nd Shift defaults to NOT scheduled (a night crew is the exception and
gets ticked on), and the 3rd Shift does not exist — which, because the
overnight shift is filed under the morning it lands on, means the NEXT date's
3rd Shift row for that machine is rendered as "no 3rd shift" with no inputs.

Access model matches the rest of the app: no auth, URL obscurity only.
"""
from datetime import date, datetime

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
)
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    DEFAULT_SHIFT_HOURS,
    MAX_SHIFT_MINUTES,
    OEE_ZONE_ORDER,
    SHIFT_LENGTH_HOURS,
    SHIFT_ORDER,
    DowntimeReasonInput,
    ScheduleCreate,
    ShiftDowntimeCreate,
    ShiftLengthCreate,
    ShiftScrapCreate,
    default_scheduled,
    get_current_shift,
    production_day_for,
    resolve_shift,
    shift_plan,
    shift_span,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _parse_entry_date(raw: str | None) -> date | None:
    """Parses the date box, or None if missing/unparseable.

    Same forgiving-on-GET, strict-on-POST split the production console uses: a
    junk date in a URL shouldn't 500 the page, but it must not silently file a
    whole shift under the wrong day either.
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _zones() -> list[dict]:
    """Zone sections in /supervisor's order, so the three review-side pages
    agree about where Poly sits.

    A slug missing from DASHBOARD_ZONES is skipped rather than raising, so
    retiring a zone can't 500 this page.
    """
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
        "shift_minutes": DEFAULT_SHIFT_HOURS * 60,
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
    """Which of `codes` the picker no longer offers, with labels.

    These get their own rows on the form so they round-trip — see the module
    docstring. `codes` is whatever is on the machine's record (GET) or whatever
    the form carried for it (POST); there is rarely more than one.
    """
    return [
        {"code": code, "label": all_reasons.get(code, {}).get("label", code)}
        for code in codes
        if code not in active
    ]


def _existing_rows(entry_date: date, shift: str) -> dict[str, dict]:
    """Current state of every machine for this date+shift, as form values.

    Pre-fills the form so the page shows what is already recorded rather than
    an empty grid over the top of real data — see the module docstring for why
    that is safe here and not on the shared production form.
    """
    scrap = get_latest_scrap_for_date(entry_date)
    downtime = get_latest_downtime_for_date(entry_date)
    schedule = get_schedule_for_date(entry_date)
    active = get_downtime_reasons()
    # Shift length is per production DAY, and for 3rd Shift that is the day
    # before the one this page is filed under — see the module docstring.
    production_day = production_day_for(shift, entry_date)
    lengths = get_shift_lengths([production_day])

    rows: dict[str, dict] = {}
    for machine_id in _all_machines():
        row = _blank_row()
        hours = lengths.get((machine_id, production_day), DEFAULT_SHIFT_HOURS)
        row["shift_hours"] = hours
        row["shift_exists"] = shift in shift_plan(hours)
        row["span"] = shift_span(shift, hours) or ""
        row["shift_minutes"] = hours * 60
        if not row["shift_exists"]:
            # No inputs are rendered for this row and nothing is read back
            # for it on POST, so its record (if any) is neither shown nor
            # touched.
            row["scheduled"] = False
            rows[machine_id] = row
            continue
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
            # The record already carries labels from the full code table, so a
            # retired code resolves to its name without another lookup.
            row["retired"] = _retired_on_row(
                row["minutes"], active, {r["code"]: r for r in record["reasons"]}
            )
            # A submission with no reasons is the "ran clean" statement, which
            # has to round-trip as a ticked box rather than as an empty form
            # indistinguishable from never-entered.
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
    """Shared context builder for the GET and POST responses.

    `shift` and `entry_date` are resolved by the caller and passed in rather
    than re-derived from the clock, because both are user-controlled and this
    runs on the POST path too — quietly recomputing either would mean a page
    that saved 1st Shift's numbers at 4PM re-rendered itself as 2nd Shift.
    """
    saved_count = sum(1 for row in rows.values() if row["status"] == "saved")
    failed_rows = [(m, r) for m, r in rows.items() if r["status"] == "failed"]
    production_day = production_day_for(shift, entry_date)

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
        "shift_length_options": SHIFT_LENGTH_HOURS,
        "default_shift_hours": DEFAULT_SHIFT_HOURS,
        # The length control is only offered where the date box is the
        # production day, which is the 1st Shift page.
        "length_editable": shift == SHIFT_ORDER[0],
        "production_day": production_day.isoformat(),
        # "Tue 9/15", for the "ran 12h shifts on ..." message. Built by hand
        # because strftime has no portable no-leading-zero day.
        "production_day_label": (
            f"{production_day:%a} {production_day.month}/{production_day.day}"
        ),
        "is_first_shift": shift == SHIFT_ORDER[0],
    }


@router.get("/console/oee", response_class=HTMLResponse)
def console_oee_page(
    request: Request, shift: str | None = None, entry_date: str | None = None
):
    """The end-of-shift entry form, all 34 machines on one page.

    `shift` and `entry_date` come from the page's own toggle and date box and
    default to the shift in progress and today. The date box matters most for
    3rd Shift, whose slots land on the calendar day AFTER the shift starts, and
    for anyone filling in yesterday the next morning.
    """
    selected_shift = resolve_shift(shift, datetime.now())
    selected_date = _parse_entry_date(entry_date) or date.today()

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
    error rather than a silent zero — the whole point of ticking it is that
    time was lost, and a zero-minute reason says nothing. Minutes typed against
    an unticked reason are also an error, because it means the two controls
    disagree about what the person meant.

    `reasons` is the FULL code table, retired codes included, so a retired
    code the form rendered for this machine (because the shift already had it)
    is read back like any other. A retired code that wasn't rendered has no
    fields in the POST body at all, so it falls through as unticked-and-empty.
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
        # ORDER MATTERS: pydantic's ValidationError subclasses ValueError, so a
        # bare `except ValueError` first would swallow it and mislabel a
        # range failure as bad number formatting.
        except ValidationError:
            errors.append(
                f"{reasons[code]['label']} minutes must be between 1 and {MAX_SHIFT_MINUTES}."
            )
        except ValueError:
            errors.append(f"{reasons[code]['label']} minutes must be a whole number.")

    return collected, errors


@router.post("/console/oee", response_class=HTMLResponse)
async def console_oee_submit(request: Request):
    """Saves downtime, scrap and scheduling for every machine that changed.

    Four INDEPENDENT write paths per machine — shift length, scheduling,
    scrap, downtime — each skipped when its submitted state matches what's
    already stored. That keeps the append-only tables from growing an
    identical row per machine every time the page is opened and saved, and
    makes the per-row "saved" badge mean something.

    Machines are processed independently rather than as one transaction: a
    single bad minutes value shouldn't block the other 33 from saving.
    """
    form = await request.form()
    entered_by = (form.get("entered_by") or "").strip()
    selected_shift = resolve_shift((form.get("shift") or "").strip(), datetime.now())
    selected_date = _parse_entry_date(form.get("entry_date"))
    active = get_downtime_reasons()
    # Reading the form against the full table is what lets a retired code
    # carry forward — see _collect_reasons.
    reasons = get_downtime_reasons(active_only=False)

    # Validate the shared fields once. If these are wrong every row would fail
    # with the same message, which is noise rather than information.
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

    # Read before writing: every path below compares against current state.
    stored = _existing_rows(selected_date, selected_shift)
    rows: dict[str, dict] = {}
    any_change = False

    length_editable = selected_shift == SHIFT_ORDER[0]

    for machine_id in _all_machines():
        previous = stored[machine_id]

        # A shift this machine's day doesn't have (the 3rd Shift after a 10
        # or 12-hour day). The form rendered no inputs for it, so there is
        # nothing to read and nothing to write.
        if not previous["shift_exists"]:
            row = _blank_row()
            row.update({k: previous[k] for k in
                        ("shift_hours", "shift_exists", "span", "shift_minutes", "scheduled")})
            row["status"] = "unchanged"
            rows[machine_id] = row
            continue

        submitted_scheduled = form.get(f"scheduled_{machine_id}") is not None
        # An unticked checkbox and an absent one are indistinguishable in a form
        # POST. For scheduling that ambiguity is dangerous — reading absence as
        # "unticked" would mark every machine not on the submitted form as
        # not-scheduled, removing them from the OEE denominator and inflating
        # every number. The hidden companion field is always posted, so its
        # presence proves the control was really on the form.
        sched_present = form.get(f"sched_present_{machine_id}") is not None
        ran_clean = form.get(f"dt_none_{machine_id}") is not None
        raw_scrap = (form.get(f"scrap_{machine_id}") or "").strip()
        note = (form.get(f"note_{machine_id}") or "").strip()

        collected, errors = _collect_reasons(form, machine_id, reasons)

        # Shift length: a radio group, so the browser always posts the
        # checked value when the control was on the form at all, and nothing
        # when it wasn't (the 2nd and 3rd Shift pages, where it's read-only).
        # Absence therefore needs no companion field — it simply means "leave
        # it alone". Accepted only from the 1st Shift page, whose date box is
        # the production day the length belongs to.
        raw_hours = form.get(f"hours_{machine_id}") if length_editable else None
        submitted_hours = previous["shift_hours"]
        hours_error = None
        if raw_hours is not None:
            try:
                submitted_hours = int(raw_hours)
            except ValueError:
                hours_error = "Shift length must be 8, 10 or 12."
            if submitted_hours not in SHIFT_LENGTH_HOURS:
                hours_error = "Shift length must be 8, 10 or 12."
                # Fall back to what's on record, so the downtime cap and the
                # re-rendered row below are built from a real length.
                submitted_hours = previous["shift_hours"]
        if hours_error:
            errors.append(hours_error)

        row = _blank_row()
        row.update(
            {
                "shift_hours": submitted_hours,
                "shift_exists": True,
                "span": shift_span(selected_shift, submitted_hours),
                "shift_minutes": submitted_hours * 60,
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
        # Re-rendered from the form, not the record. A retired code was on this
        # machine's row if its minutes box came back (number inputs post even
        # when empty), and that is the test — not whether it is still ticked,
        # so an untick that failed validation still has a row to fix.
        row["retired"] = _retired_on_row(
            [c for c in reasons if form.get(f"dt_min_{machine_id}_{c}") is not None],
            active,
            reasons,
        )
        rows[machine_id] = row

        # --- shift length ---------------------------------------------------
        # Independent of the downtime path's errors, like scrap: a typo in a
        # minutes box shouldn't stop the day's length from saving.
        if hours_error is None and submitted_hours != previous["shift_hours"]:
            any_change = True
            try:
                create_shift_length(
                    ShiftLengthCreate(
                        machine_id=machine_id,
                        entry_date=selected_date,
                        shift_hours=submitted_hours,
                        entered_by=entered_by,
                    )
                )
                row["saved_parts"].append(f"{submitted_hours}h shifts")
            except ValidationError:
                errors.append("Shift length must be 8, 10 or 12.")

        # --- scheduling ---------------------------------------------------
        if sched_present and submitted_scheduled != previous["scheduled"]:
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
            # Clearing the box is NOT a write. Scrap has no "unknown" value to
            # write back to, and blanking it would have to mean either zero or
            # retracted — neither of which the person can express. Correcting a
            # wrong scrap number means typing the right one.

        # --- downtime -------------------------------------------------------
        # "Ran clean" and a list of reasons are contradictory statements about
        # the same shift, so submitting both is an error rather than something
        # to silently resolve one way.
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
                        # The cap is this machine's shift length on this
                        # day, including a length changed on this very save.
                        shift_minutes=row["shift_minutes"],
                    )
                    row["saved_parts"].append(
                        "no downtime" if not collected else "downtime"
                    )
                except (UnknownReasonCodeError, DowntimeExceedsShiftError) as exc:
                    # Both carry human-readable messages by design.
                    errors.append(str(exc))
                except ValidationError:
                    errors.append("Couldn't save downtime - check the values.")

        if errors:
            row["status"] = "failed"
            # Several paths can fail independently for one machine, so the
            # messages are joined rather than only the first being shown.
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
