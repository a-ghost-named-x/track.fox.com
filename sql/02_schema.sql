-- track.fox.com v2 — Postgres schema (manual-entry data only)
-- This database owns ONLY manual-entry / app data. Production data lives in
-- MSSQL and is never written to from this app.

-- ---------------------------------------------------------------------------
-- Reference data: per-machine, per-time-slot cumulative production standards.
-- Maintained manually (see sql/03_seed_standards.sql) — not editable via /console.
-- Example: machine 'C1', slot '8AM' -> standard_units 11700 means "by 8AM,
-- C1 should have produced 11,700 units cumulative for the day."
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS standards (
    machine_id      TEXT NOT NULL,
    time_slot       TEXT NOT NULL,
    standard_units  INTEGER NOT NULL CHECK (standard_units >= 0),
    PRIMARY KEY (machine_id, time_slot)
);

-- ---------------------------------------------------------------------------
-- Append-only log of manual entries. One row per /console submission.
-- Never UPDATEd or DELETEd by the app — corrections are new rows with a
-- later created_at. The /dashboard view picks the latest row per
-- (machine_id, date, time_slot) when building the display grid.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entries (
    id              BIGSERIAL PRIMARY KEY,
    machine_id      TEXT NOT NULL,
    operator        TEXT NOT NULL,
    time_slot       TEXT NOT NULL,
    units_produced  INTEGER NOT NULL CHECK (units_produced >= 0),
    status          TEXT NOT NULL CHECK (status IN (':)', ':(')),
    issue           TEXT,                       -- optional, free text
    entered_by      TEXT NOT NULL,              -- free-text employee number, NOT verified identity
    entry_date      DATE NOT NULL,              -- the production date this entry is for
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fast lookups for "today's entries" and "latest per machine/slot/date"
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries (entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_machine_date_slot
    ON entries (machine_id, entry_date, time_slot, created_at DESC);

-- ---------------------------------------------------------------------------
-- Grants for the app's low-privilege role (created by 01_roles.sh).
-- Scoped to exactly what the app needs: read/write on entries, read-only on
-- standards (standards are maintained manually via seed scripts, not via
-- any app endpoint), and usage on the sequence backing entries.id.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON entries TO trackfox_app;
GRANT USAGE, SELECT ON SEQUENCE entries_id_seq TO trackfox_app;
GRANT SELECT ON standards TO trackfox_app;

