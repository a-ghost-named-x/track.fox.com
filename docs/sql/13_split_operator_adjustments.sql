-- track.fox.com v2 — Break "Operator Adjustments" into four sub-reasons
--
-- Requested by the floor (2026-09-11) as the next step in their continuous
-- improvement process: Operator Adjustments had shown up as a big enough
-- bucket on the /oee Pareto that they now want to know WHICH adjustment is
-- costing the time. The four are:
--
--     Temperature adjustment
--     Timing adjustment
--     Pressure adjustment
--     Air jet adjustment
--
-- All four apply to all 34 machines (confirmed 2026-09-11), so they join the
-- floor-wide list like every other code — there is no per-zone reason list,
-- and this doesn't introduce one.
--
-- HOW TO APPLY
--   psql/pgAdmin as the `postgres` superuser — trackfox_app has SELECT only on
--   this table by design. Not through CI/CD.
--   Run 12_seed_shift_downtime_reasons.sql first (the guard below checks).
--
--   Either order works between this file and the matching app deploy, but
--   PUSH THE CODE FIRST if you can. The code change teaches /console/oee to
--   keep showing a retired code on shifts that already have it (see below);
--   running this file before that code is live opens a short window where a
--   correction to a pre-cutover shift could drop its Operator Adjustments
--   minutes. Nothing breaks in the other order — the new code just sees no
--   retired codes on any row until this file runs.
--
-- Idempotent: ON CONFLICT updates in place.
--
-- ---------------------------------------------------------------------------
-- THE OLD CODE IS RETIRED, NOT DELETED — same mechanism as file 12
-- ---------------------------------------------------------------------------
-- Every shift entered between 2026-09-09 and the day this runs has its
-- operator-adjustment minutes filed under OPER_ADJUST, and there is no way to
-- know after the fact which of the four kinds each one was. Those rows stay
-- exactly as they are: `active = false` takes the code off the form's list
-- of things to tick, while /oee keeps labelling history with it.
--
-- So on /oee, dates before the cutover show one "Operator Adjustments" bar and
-- dates after show up to four narrower ones. That is the honest picture, and
-- the floor should expect it — the breakdown starts the day this is applied.
--
-- ---------------------------------------------------------------------------
-- A RETIRED CODE MUST STILL ROUND-TRIP THROUGH THE ENTRY FORM
-- ---------------------------------------------------------------------------
-- /console/oee pre-fills every machine from what is on record, and the newest
-- downtime header for a shift wins WHOLESALE (that is what lets a correction
-- remove a reason). Put those two facts together with a form that only shows
-- active codes and you get a trap: re-saving a pre-cutover shift for any
-- reason — fixing the note, correcting another reason's minutes — would post
-- only the codes the form could see, and the old Operator Adjustments minutes
-- would silently disappear from that shift.
--
-- app/routers/console_oee.py therefore renders a retired code on a machine's
-- row when, and only when, that shift already has it on record — tagged as
-- retired, with the same checkbox and minutes box, so it either carries
-- forward untouched or is deliberately unticked and reclassified. New use is
-- still impossible: an untouched machine never shows it.
--
-- ---------------------------------------------------------------------------
-- WHY THE LABELS KEEP THE FAMILY NAME
-- ---------------------------------------------------------------------------
-- "Operator Adjustments - Temperature" rather than just "Temperature", so the
-- four read as a group wherever they appear standalone: the Pareto bars, the
-- Top Reasons column, a per-machine tooltip. The floor's own wording for the
-- sub-reasons is kept; the doubled word ("... - Temperature adjustment") is
-- dropped because the reason picker is a narrow column with a minutes box
-- beside it. A plain ASCII hyphen, not a typographic dash, so the label can't
-- be mangled by a client encoding somewhere between pgAdmin and the browser.
--
-- Codes share the OPER_ADJ_ prefix so any raw query sorts them together.
-- sort_order 51–54 slots them exactly where Operator Adjustments sat on the
-- paper sheet (50), which is what the 10-spacing in file 12 was left for.
-- ---------------------------------------------------------------------------

-- Guards. Same two-stage check as file 12: "wrong database" and "file 12 not
-- applied yet" would otherwise look identical, and the first is the easier
-- mistake to make in pgAdmin (a Query Tool opened from the SERVER node
-- connects to the `postgres` maintenance database, not trackfox).
DO $$
BEGIN
    IF to_regclass('public.entries') IS NULL
       OR to_regclass('public.standards') IS NULL THEN
        RAISE EXCEPTION
            'This is not the track.fox.com database. Connected to "%" as "%", which has no `entries`/`standards` tables. Reconnect to the trackfox database — in pgAdmin, open the Query Tool from the trackfox database node, not the server node.',
            current_database(), current_user;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM downtime_reasons WHERE code = 'OPER_ADJUST') THEN
        RAISE EXCEPTION
            'OPER_ADJUST is not in downtime_reasons in database "%". Apply docs/sql/12_seed_shift_downtime_reasons.sql first, then re-run this file.',
            current_database();
    END IF;
END
$$;


-- ===========================================================================
-- 1. Retire the umbrella code. History keeps resolving; the form stops
--    offering it to machines that don't already have it.
-- ===========================================================================
UPDATE downtime_reasons
SET active = false
WHERE code = 'OPER_ADJUST';


-- ===========================================================================
-- 2. The four sub-reasons, in the order the floor listed them.
-- ===========================================================================
INSERT INTO downtime_reasons (code, label, is_planned, sort_order, active) VALUES
    ('OPER_ADJ_TEMP',     'Operator Adjustments - Temperature',  false, 51, true),
    ('OPER_ADJ_TIMING',   'Operator Adjustments - Timing',       false, 52, true),
    ('OPER_ADJ_PRESSURE', 'Operator Adjustments - Pressure',     false, 53, true),
    ('OPER_ADJ_AIRJET',   'Operator Adjustments - Air jet',      false, 54, true)
ON CONFLICT (code) DO UPDATE
    SET label      = EXCLUDED.label,
        is_planned = EXCLUDED.is_planned,
        sort_order = EXCLUDED.sort_order,
        active     = EXCLUDED.active;


-- ===========================================================================
-- VERIFICATION — run these one at a time. pgAdmin shows only the last result
-- set when a file is executed as a whole.
-- ===========================================================================

-- The dropdown, exactly as /console/oee will render it. Expect 14 rows, with
-- the four OPER_ADJ_ codes between Equipment Failure (40) and Defective
-- Material (60), every one is_planned = false, and NO plain OPER_ADJUST.
SELECT sort_order, code, label, is_planned
FROM downtime_reasons
WHERE active
ORDER BY sort_order;

-- Expect 14 active, 11 retired.
SELECT active, count(*) FROM downtime_reasons GROUP BY active ORDER BY active DESC;

-- The shifts that still carry the umbrella code on their CURRENT record
-- (newest header per machine+date+shift — superseded corrections don't count).
-- These are the rows that will show a "retired" line on /console/oee if anyone
-- opens them, and the ones that keep a single "Operator Adjustments" bar on
-- /oee for their date. Not a problem — just worth knowing how many there are
-- before telling the floor where the breakdown starts.
WITH latest AS (
    SELECT DISTINCT ON (machine_id, entry_date, shift)
        id, machine_id, entry_date, shift
    FROM shift_downtime_entry
    ORDER BY machine_id, entry_date, shift, created_at DESC
)
SELECT l.entry_date, l.shift, count(*) AS machines, sum(r.minutes) AS minutes
FROM latest l
JOIN shift_downtime_reason r ON r.downtime_entry_id = l.id
WHERE r.reason_code = 'OPER_ADJUST'
GROUP BY l.entry_date, l.shift
ORDER BY l.entry_date, l.shift;
