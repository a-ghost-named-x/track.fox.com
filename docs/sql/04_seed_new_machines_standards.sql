-- track.fox.com v2 — Standards seed data for the Poly and Leno machines
-- (P1-P4 and AS1-AS7, see DASHBOARD_ZONES / MACHINE_IDS in app/models.py).
--
-- Values transcribed from standards.xlsx (received 2026-08-20), which gives
-- one 8-hour shift's four cumulative checkpoints per machine. Per the floor,
-- that same set repeats for all three shifts — the identical pattern the
-- original machines follow in 03_seed_standards.sql (e.g. C1 runs
-- 11700/23400/35100/46800 and then starts over at 4PM and again at 12AM).
--
-- CORRECTION ON AS1-AS5 (2026-08-20): the source spreadsheet reads
-- 2,520 / 5,030 / 7,560 / 10,080, but its 2nd checkpoint is a typo. The 1st,
-- 3rd and 4th are exactly 1x/3x/4x of 2,520, so an even ramp puts the 2nd at
-- 5,040 — which every other machine in the sheet follows. Confirmed with the
-- floor and corrected to 5,040 here, in the 10AM / 6PM / 2AM slots.
--
-- This file is the corrected source of truth. If the spreadsheet is re-sent
-- with 5,030 still in it, fix the sheet — do not "restore" it here.
--
-- HOW TO APPLY:
--   psql/pgAdmin directly against POSTGRES_HOST in .env — not through CI/CD,
--   which never touches the database. Disable SSL mode in pgAdmin if that's
--   still needed for this server.
--
-- Idempotent: ON CONFLICT updates in place, so it's safe to re-run whenever
-- a standard changes. This supersedes the all-zero placeholder rows this
-- file used to carry; see 05_drop_legacy_leno_machine_ids.sql for removing
-- the orphaned A1-A7 rows those placeholders created.

INSERT INTO standards (machine_id, time_slot, standard_units) VALUES
    -- P1: 14,400 / 28,800 / 43,200 / 57,600 per 8-hour shift, repeated for all three shifts.
    ('P1', '8AM', 14400),
    ('P1', '10AM', 28800),
    ('P1', '12PM', 43200),
    ('P1', '2PM', 57600),
    ('P1', '4PM', 14400),
    ('P1', '6PM', 28800),
    ('P1', '8PM', 43200),
    ('P1', '10PM', 57600),
    ('P1', '12AM', 14400),
    ('P1', '2AM', 28800),
    ('P1', '4AM', 43200),
    ('P1', '6AM', 57600),

    -- P2: 16,200 / 32,400 / 48,600 / 64,800 per 8-hour shift, repeated for all three shifts.
    ('P2', '8AM', 16200),
    ('P2', '10AM', 32400),
    ('P2', '12PM', 48600),
    ('P2', '2PM', 64800),
    ('P2', '4PM', 16200),
    ('P2', '6PM', 32400),
    ('P2', '8PM', 48600),
    ('P2', '10PM', 64800),
    ('P2', '12AM', 16200),
    ('P2', '2AM', 32400),
    ('P2', '4AM', 48600),
    ('P2', '6AM', 64800),

    -- P3: 16,200 / 32,400 / 48,600 / 64,800 per 8-hour shift, repeated for all three shifts.
    ('P3', '8AM', 16200),
    ('P3', '10AM', 32400),
    ('P3', '12PM', 48600),
    ('P3', '2PM', 64800),
    ('P3', '4PM', 16200),
    ('P3', '6PM', 32400),
    ('P3', '8PM', 48600),
    ('P3', '10PM', 64800),
    ('P3', '12AM', 16200),
    ('P3', '2AM', 32400),
    ('P3', '4AM', 48600),
    ('P3', '6AM', 64800),

    -- P4: 16,200 / 32,400 / 48,600 / 64,800 per 8-hour shift, repeated for all three shifts.
    ('P4', '8AM', 16200),
    ('P4', '10AM', 32400),
    ('P4', '12PM', 48600),
    ('P4', '2PM', 64800),
    ('P4', '4PM', 16200),
    ('P4', '6PM', 32400),
    ('P4', '8PM', 48600),
    ('P4', '10PM', 64800),
    ('P4', '12AM', 16200),
    ('P4', '2AM', 32400),
    ('P4', '4AM', 48600),
    ('P4', '6AM', 64800),

    -- AS1: 2,520 / 5,040 / 7,560 / 10,080 per 8-hour shift, repeated for all three shifts.
    ('AS1', '8AM', 2520),
    ('AS1', '10AM', 5040),
    ('AS1', '12PM', 7560),
    ('AS1', '2PM', 10080),
    ('AS1', '4PM', 2520),
    ('AS1', '6PM', 5040),
    ('AS1', '8PM', 7560),
    ('AS1', '10PM', 10080),
    ('AS1', '12AM', 2520),
    ('AS1', '2AM', 5040),
    ('AS1', '4AM', 7560),
    ('AS1', '6AM', 10080),

    -- AS2: 2,520 / 5,040 / 7,560 / 10,080 per 8-hour shift, repeated for all three shifts.
    ('AS2', '8AM', 2520),
    ('AS2', '10AM', 5040),
    ('AS2', '12PM', 7560),
    ('AS2', '2PM', 10080),
    ('AS2', '4PM', 2520),
    ('AS2', '6PM', 5040),
    ('AS2', '8PM', 7560),
    ('AS2', '10PM', 10080),
    ('AS2', '12AM', 2520),
    ('AS2', '2AM', 5040),
    ('AS2', '4AM', 7560),
    ('AS2', '6AM', 10080),

    -- AS3: 2,520 / 5,040 / 7,560 / 10,080 per 8-hour shift, repeated for all three shifts.
    ('AS3', '8AM', 2520),
    ('AS3', '10AM', 5040),
    ('AS3', '12PM', 7560),
    ('AS3', '2PM', 10080),
    ('AS3', '4PM', 2520),
    ('AS3', '6PM', 5040),
    ('AS3', '8PM', 7560),
    ('AS3', '10PM', 10080),
    ('AS3', '12AM', 2520),
    ('AS3', '2AM', 5040),
    ('AS3', '4AM', 7560),
    ('AS3', '6AM', 10080),

    -- AS4: 2,520 / 5,040 / 7,560 / 10,080 per 8-hour shift, repeated for all three shifts.
    ('AS4', '8AM', 2520),
    ('AS4', '10AM', 5040),
    ('AS4', '12PM', 7560),
    ('AS4', '2PM', 10080),
    ('AS4', '4PM', 2520),
    ('AS4', '6PM', 5040),
    ('AS4', '8PM', 7560),
    ('AS4', '10PM', 10080),
    ('AS4', '12AM', 2520),
    ('AS4', '2AM', 5040),
    ('AS4', '4AM', 7560),
    ('AS4', '6AM', 10080),

    -- AS5: 2,520 / 5,040 / 7,560 / 10,080 per 8-hour shift, repeated for all three shifts.
    ('AS5', '8AM', 2520),
    ('AS5', '10AM', 5040),
    ('AS5', '12PM', 7560),
    ('AS5', '2PM', 10080),
    ('AS5', '4PM', 2520),
    ('AS5', '6PM', 5040),
    ('AS5', '8PM', 7560),
    ('AS5', '10PM', 10080),
    ('AS5', '12AM', 2520),
    ('AS5', '2AM', 5040),
    ('AS5', '4AM', 7560),
    ('AS5', '6AM', 10080),

    -- AS6: 2,250 / 4,500 / 6,750 / 9,000 per 8-hour shift, repeated for all three shifts.
    ('AS6', '8AM', 2250),
    ('AS6', '10AM', 4500),
    ('AS6', '12PM', 6750),
    ('AS6', '2PM', 9000),
    ('AS6', '4PM', 2250),
    ('AS6', '6PM', 4500),
    ('AS6', '8PM', 6750),
    ('AS6', '10PM', 9000),
    ('AS6', '12AM', 2250),
    ('AS6', '2AM', 4500),
    ('AS6', '4AM', 6750),
    ('AS6', '6AM', 9000),

    -- AS7: 2,250 / 4,500 / 6,750 / 9,000 per 8-hour shift, repeated for all three shifts.
    ('AS7', '8AM', 2250),
    ('AS7', '10AM', 4500),
    ('AS7', '12PM', 6750),
    ('AS7', '2PM', 9000),
    ('AS7', '4PM', 2250),
    ('AS7', '6PM', 4500),
    ('AS7', '8PM', 6750),
    ('AS7', '10PM', 9000),
    ('AS7', '12AM', 2250),
    ('AS7', '2AM', 4500),
    ('AS7', '4AM', 6750),
    ('AS7', '6AM', 9000)

ON CONFLICT (machine_id, time_slot) DO UPDATE
    SET standard_units = EXCLUDED.standard_units;
