-- track.fox.com v2 — OEE schema (downtime, scrap, scheduling, ideal rates)
--
-- OEE = Availability x Performance x Quality. This file adds the three things
-- the existing schema cannot express: how long a machine was down and why, how
-- many units it made that are NOT shippable, and whether it was supposed to be
-- running at all.
--
-- Nothing here touches `entries`. That was a deliberate choice, not an
-- oversight: production numbers, scrap and downtime are entered by DIFFERENT
-- PEOPLE at different times for the same machine+slot. get_latest_entries_for_date()
-- resolves the dashboard grid with DISTINCT ON (machine_id, time_slot) ORDER BY
-- created_at DESC — the newest ROW wins wholesale. Put scrap on `entries` and
-- the scrap person's submission becomes the newest row for that cell, blanking
-- the units the production person entered ten minutes earlier and taking the
-- floor screen down with it. Separate tables mean three people can write
-- the same cell concurrently and never collide.
--
-- HOW TO APPLY:
--   psql/pgAdmin directly against POSTGRES_HOST in .env, as the `postgres`
--   superuser — NOT as trackfox_app, and NOT through CI/CD, which never
--   touches the database. Disable SSL mode in pgAdmin if that is still needed
--   for this server.
--
-- Then: 07_seed_downtime_reasons.sql, then 08_seed_ideal_rates.sql.

-- ---------------------------------------------------------------------------
-- Reference data: downtime reason codes.
--
-- `is_planned` is what keeps OEE honest. Standard OEE EXCLUDES planned
-- downtime from Planned Production Time rather than counting it as an
-- availability loss — otherwise a machine with no orders scores 0% and drags
-- every rollup down with it. Unplanned downtime is a real loss and stays in
-- the denominator.
--
-- CHGOVR is deliberately is_planned = false. Setup and adjustment is one of
-- the Six Big Losses in textbook OEE; marking it planned would quietly raise
-- every score and hide changeover as an improvement target.
--
-- `active` retires a code without orphaning the history that references it —
-- drop it from the entry form dropdown, leave the row in place.
--
-- SELECT-only for trackfox_app, same posture as `standards`: maintained by
-- seed script, never written from an app endpoint.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS downtime_reasons (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    is_planned  BOOLEAN NOT NULL,
    sort_order  INTEGER NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------------
-- Scrap: units the machine produced that will not ship.
--
-- CUMULATIVE within the shift, matching entries.units_produced — the column
-- name says so on purpose. Two reasons it is cumulative rather than per-slot:
-- it makes shift-level Quality computable from the last checkpoint alone
-- (the same property units already has), and a missed checkpoint self-heals
-- at the next one instead of losing that slot's scrap forever.
--
-- entries.units_produced is the GOOD count (confirmed with the floor — they
-- only count shippable units), so:
--     total count = units_produced + scrap_cumulative
--     Quality     = units_produced / total count
--
-- Append-only like `entries`: a correction is a new row with a later
-- created_at, and the reader takes DISTINCT ON (machine_id, entry_date,
-- time_slot) ORDER BY created_at DESC.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS slot_scrap (
    id                BIGSERIAL PRIMARY KEY,
    machine_id        TEXT NOT NULL,
    entry_date        DATE NOT NULL,
    time_slot         TEXT NOT NULL,
    scrap_cumulative  INTEGER NOT NULL CHECK (scrap_cumulative >= 0),
    entered_by        TEXT NOT NULL,   -- free-text employee number, NOT verified identity
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Downtime, as a header + child pair. Two tables rather than one because a
-- slot routinely has MORE THAN ONE cause ("15 min material wait plus 10 min
-- mechanical"), and a single minutes+reason column pair cannot say that. The
-- split is also what makes the Pareto chart on /oee possible.
--
-- The header is one submission for one machine+date+slot. Latest header per
-- slot wins WHOLESALE — read the newest header, then its children, and ignore
-- older headers entirely. That is what lets a correction REMOVE a reason
-- somebody entered by mistake. (Resolving per-reason instead would hit the
-- same dead end as entries.issue, where a blank correction cannot retract an
-- earlier value — see get_shift_activity() in app/db/entries.py.)
--
-- A header with ZERO children is the load-bearing case: it means "this
-- machine ran clean, no downtime, 100% availability." No header at all means
-- "nobody has entered this slot yet." Those must never collapse into each
-- other — that is the NULL-is-not-zero rule one layer down, and the same trap
-- as a standards row of 0 (see MACHINE_IDS in app/models.py).
--
-- `note` is the downtime crew's own free text. entries.issue belongs to the
-- production person; these are different people and they have different
-- things to say.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS slot_downtime_entry (
    id          BIGSERIAL PRIMARY KEY,
    machine_id  TEXT NOT NULL,
    entry_date  DATE NOT NULL,
    time_slot   TEXT NOT NULL,
    note        TEXT,                -- optional, free text
    entered_by  TEXT NOT NULL,       -- free-text employee number, NOT verified identity
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per reason per submission. PER-SLOT minutes, not cumulative —
-- reason codes force that grain, and cumulative-to-date is just a SUM() over
-- the shift's slots, so nothing is lost by storing the finer number.
--
-- minutes > 0, not >= 0: a zero-minute reason row is meaningless. "Ran clean"
-- is a header with no children, not a child with a zero.
--
-- `was_planned` is SNAPSHOTTED from downtime_reasons.is_planned at write
-- time, deliberately duplicating it. Straight from the precedent in
-- app/db/entries.py, which snapshots `status` onto the row rather than
-- recomputing it on every read: if a reason gets reclassified next year, we
-- do NOT want every historical OEE number to silently shift underneath us.
CREATE TABLE IF NOT EXISTS slot_downtime_reason (
    id                 BIGSERIAL PRIMARY KEY,
    downtime_entry_id  BIGINT NOT NULL REFERENCES slot_downtime_entry(id) ON DELETE CASCADE,
    reason_code        TEXT NOT NULL REFERENCES downtime_reasons(code),
    minutes            INTEGER NOT NULL CHECK (minutes > 0),
    was_planned        BOOLEAN NOT NULL
);

-- ---------------------------------------------------------------------------
-- Scheduling exceptions. ABSENCE OF A ROW MEANS THE MACHINE WAS SCHEDULED —
-- so nobody fills anything in on a normal day, and only exceptions get typed.
--
-- Shift granularity, not per-slot: 34 machines x 4 slots would be 136
-- checkboxes a day, and scheduling is known at shift level by a planner
-- anyway. Partial-slot idle time is covered by the NOSCHED reason code
-- instead, so between the two there is full coverage.
--
-- The OEE rule this drives: a machine not scheduled for a shift is EXCLUDED
-- from that shift's rollup. Not 0%, not 100% — absent.
--
-- The shift CHECK exists to keep ALL_DAY_LABEL out of this table. It is a
-- /supervisor display option, not a real shift, and app/models.py warns that
-- it must never be saved onto a row.
--
-- Append-only, latest per (machine_id, entry_date, shift) wins.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS machine_schedule (
    id          BIGSERIAL PRIMARY KEY,
    machine_id  TEXT NOT NULL,
    entry_date  DATE NOT NULL,
    shift       TEXT NOT NULL CHECK (shift IN ('1st Shift', '2nd Shift', '3rd Shift')),
    scheduled   BOOLEAN NOT NULL,
    entered_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Per-machine theoretical maximum rate, for OEE's Performance factor.
--
-- Its own table rather than a column on `standards`, because this is a
-- MACHINE-level fact and `standards` is keyed per (machine, slot) — a column
-- there would be the same number copied across 12 rows with nothing stopping
-- them from drifting apart. 34 rows here, and they cannot disagree.
--
-- Kept SEPARATE from standards.standard_units on purpose, even though
-- 08_seed_ideal_rates.sql derives one from the other today. standard_units is
-- a management TARGET and it drives the :) / :( light; this is a physical
-- ceiling and it drives OEE. Deriving once via a seed script keeps them in
-- sync when we choose, without coupling them so that editing a target
-- silently moves every historical OEE number.
--
-- Note it is a TOTAL-throughput rate, not a good-units rate: the machine does
-- not know what is scrap. standard_units is in good units. Different
-- quantities — which is the other reason they do not share a column.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS machine_ideal_rates (
    machine_id           TEXT PRIMARY KEY,
    ideal_units_per_hour NUMERIC(10,2) NOT NULL CHECK (ideal_units_per_hour > 0),
    note                 TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Indexes. Same shape as idx_entries_machine_date_slot: the access pattern is
-- always "latest row for this machine+date+slot", so created_at DESC rides
-- along to make DISTINCT ON an index scan. The plain entry_date indexes back
-- /oee's date picker the way idx_entries_date backs /supervisor's.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_slot_scrap_machine_date_slot
    ON slot_scrap (machine_id, entry_date, time_slot, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_slot_scrap_date
    ON slot_scrap (entry_date);

CREATE INDEX IF NOT EXISTS idx_slot_downtime_machine_date_slot
    ON slot_downtime_entry (machine_id, entry_date, time_slot, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_slot_downtime_date
    ON slot_downtime_entry (entry_date);

CREATE INDEX IF NOT EXISTS idx_slot_downtime_reason_entry
    ON slot_downtime_reason (downtime_entry_id);
-- Backs the Pareto: "minutes by reason code across a date range."
CREATE INDEX IF NOT EXISTS idx_slot_downtime_reason_code
    ON slot_downtime_reason (reason_code);

CREATE INDEX IF NOT EXISTS idx_machine_schedule_lookup
    ON machine_schedule (machine_id, entry_date, shift, created_at DESC);

-- ---------------------------------------------------------------------------
-- Grants for trackfox_app, scoped exactly like 02_schema.sql did:
-- read/write on the tables the app writes, read-only on reference data,
-- usage on the sequences behind the BIGSERIALs.
--
-- downtime_reasons and machine_ideal_rates get SELECT only — both are
-- maintained by seed script, same as `standards`. That mirrors the gotcha
-- already learned the hard way: seed scripts run as `postgres`, not as
-- trackfox_app.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON slot_scrap            TO trackfox_app;
GRANT SELECT, INSERT ON slot_downtime_entry   TO trackfox_app;
GRANT SELECT, INSERT ON slot_downtime_reason  TO trackfox_app;
GRANT SELECT, INSERT ON machine_schedule      TO trackfox_app;

GRANT USAGE, SELECT ON SEQUENCE slot_scrap_id_seq            TO trackfox_app;
GRANT USAGE, SELECT ON SEQUENCE slot_downtime_entry_id_seq   TO trackfox_app;
GRANT USAGE, SELECT ON SEQUENCE slot_downtime_reason_id_seq  TO trackfox_app;
GRANT USAGE, SELECT ON SEQUENCE machine_schedule_id_seq      TO trackfox_app;

GRANT SELECT ON downtime_reasons     TO trackfox_app;
GRANT SELECT ON machine_ideal_rates  TO trackfox_app;
