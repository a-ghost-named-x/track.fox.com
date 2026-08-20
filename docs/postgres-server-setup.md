# Setting up a new PostgreSQL server for track.fox.com

## When to use this

Follow this when you've stood up a brand-new, standalone PostgreSQL server
(a separate Linux VM with Postgres installed, not the `db` container in
`docker-compose.yml`) and need to get it ready to serve as track.fox.com's
manual-entry database. This stops right before editing the app's `.env` to
actually point at it — that's a separate, later step once every check below
passes.

Nothing here touches lost-woods or the app code. Until you change `.env`,
this new server has zero production traffic pointed at it, so there's no
risk in taking your time and re-running steps if something doesn't look right.

## Prerequisites and access needed

- SSH access to the new VM with `sudo` rights.
- PostgreSQL already installed and running on it (`systemctl status postgresql`).
- This repo's `docs/sql/02_schema.sql`, `docs/sql/03_seed_standards.sql` and
  `docs/sql/04_seed_new_machines_standards.sql` files, copied onto the new
  server. (`docs/sql/01_roles.sh` is reference
  only here — its logic is run by hand in Step 3, since its actual script
  only ever ran automatically inside a bundled Docker container's init
  process, which this project no longer has.)
- The IP address of whatever will connect to this database — for now, that's
  just your workstation (or wherever you're testing from); the real app
  server's IP gets added later when you're ready to cut over.
- A strong password picked out for the `trackfox_app` role. Don't reuse the
  dev password from your local `.env`.

## Step-by-step procedure

### 1. Confirm Postgres is installed and check its version

```bash
sudo systemctl status postgresql
psql --version
sudo -u postgres psql -c "SELECT version();"
```

Nothing in the schema needs a specific version — no extensions, no exotic
types — so whatever's already installed is almost certainly fine.

### 2. Find the config file paths

Ubuntu/Debian installs put these under `/etc/postgresql/<version>/main/`, but
asking Postgres directly avoids hardcoding a version number:

```bash
sudo -u postgres psql -c "SHOW config_file;"
sudo -u postgres psql -c "SHOW hba_file;"
```

Keep both paths handy — you'll edit them in Steps 8 and 9.

### 3. Copy the SQL files onto the server

From your workstation, run this from the repo root (where the `docs/sql/` folder lives):

```bash
scp docs/sql/02_schema.sql docs/sql/03_seed_standards.sql docs/sql/04_seed_new_machines_standards.sql youruser@<new-server-ip>:/tmp/
```

### 4. Create the database and the app's role

SSH in, then:

```bash
sudo -u postgres psql
```

```sql
CREATE DATABASE trackfox;
CREATE ROLE trackfox_app WITH LOGIN PASSWORD 'REPLACE_WITH_A_STRONG_PASSWORD';
\q
```

This is the manual equivalent of what `docs/sql/01_roles.sh` did automatically
inside the old bundled container's first-boot init — a standalone server
never runs that script, so the role has to be created by hand, and it must
exist **before** Step 5, since that script's `GRANT` statements reference it
by name.

### 5. Apply the schema

```bash
sudo -u postgres psql -d trackfox -f /tmp/02_schema.sql
```

This creates the `entries` and `standards` tables, their indexes, and grants
`trackfox_app` exactly `SELECT`/`INSERT` on `entries`, `SELECT` on
`standards`, and `USAGE`/`SELECT` on the `entries_id_seq` sequence — nothing
broader.

### 6. Load the seed data

Both seed files, in order. `03` covers the original 23 machines; `04` covers
Poly (`P1`-`P4`) and Leno (`AS1`-`AS7`).

```bash
sudo -u postgres psql -d trackfox -f /tmp/03_seed_standards.sql
```

```bash
sudo -u postgres psql -d trackfox -f /tmp/04_seed_new_machines_standards.sql
```

This matters functionally, not just for realism: the app refuses to save a
`/console` entry for any machine+slot combo that has no standard row. Running
only `03` leaves every Poly and Leno machine unusable.

`05_drop_legacy_leno_machine_ids.sql` is **not** needed on a fresh server —
it only cleans up legacy `A1`-`A7` rows on a database that predates the
`AS1`-`AS7` rename. See `docs/applying-poly-leno-standards.md`.

### 7. Verify schema and data landed correctly

```bash
sudo -u postgres psql -d trackfox -c "\dt"
sudo -u postgres psql -d trackfox -c "SELECT count(*) FROM standards;"   # expect 408 (34 machines x 12 slots)
sudo -u postgres psql -d trackfox -c "SELECT count(*) FROM standards WHERE standard_units = 0;"   # expect 0
sudo -u postgres psql -d trackfox -c "\dp entries"
sudo -u postgres psql -d trackfox -c "\dp standards"
```

### 8. Let Postgres listen on the network

Edit the config file found in Step 2:

```bash
sudo nano /etc/postgresql/<version>/main/postgresql.conf
```

Set:

```
listen_addresses = '*'
```

(Or a specific interface IP instead of `*`, if you'd rather be more
restrictive about which network interface it listens on.)

### 9. Allow `trackfox_app` to connect remotely

Edit the hba file found in Step 2:

```bash
sudo nano /etc/postgresql/<version>/main/pg_hba.conf
```

Add a line scoped to just the machine that needs access right now (your
workstation's IP, for testing) — not `0.0.0.0/0`:

```
host    trackfox    trackfox_app    <your-ip>/32    scram-sha-256
```

### 10. Restart Postgres to apply both config changes

```bash
sudo systemctl restart postgresql
```

### 11. Open the firewall for just that IP

```bash
sudo ufw allow from <your-ip> to any port 5432 proto tcp
sudo ufw status
```

If this VM also sits behind a cloud provider's security group or a network
firewall/VLAN (the way lost-woods does), that layer needs the same narrow
rule added separately — `ufw` alone won't cover it.

### 12. Test the connection end-to-end

From the machine whose IP you just allowed:

```bash
psql -h <new-server-ip> -U trackfox_app -d trackfox
```

Once connected, confirm both grants actually work, not just that they exist:

```sql
SELECT count(*) FROM standards;

INSERT INTO entries (machine_id, operator, time_slot, units_produced, status, entered_by, entry_date)
VALUES ('C1', 'Test Op', '8AM', 100, ':(', '99999', CURRENT_DATE);

SELECT * FROM entries ORDER BY created_at DESC LIMIT 1;

DELETE FROM entries WHERE entered_by = '99999';  -- clean up the test row
```

### 13. (Optional, recommended) Enable SSL

Since credentials and production-adjacent data will cross the network to
this server, SSL is worth turning on even for an internal box — a
self-signed cert is enough here. Set `ssl = on` plus `ssl_cert_file`/
`ssl_key_file` in `postgresql.conf`, then restart.

One flag for later: `get_pg_connection()` in `app/db/postgres.py` doesn't
currently set an `sslmode` when connecting — that's fine against the
existing container (no SSL configured there today), but if this new server
enforces SSL, that's a small app-side code change to make when you get to
that step, not something this runbook covers.

## Rollback

Since this server has no real data on it yet, rollback is cheap: if
something looks wrong at any point, just undo and re-run from Step 4:

```sql
DROP DATABASE trackfox;
DROP ROLE trackfox_app;
```

If you'd rather not redo the SQL each time, take a VM disk snapshot before
Step 4 if your hypervisor supports it — restoring it is faster than
re-running steps.

## Escalation

None needed — this is a solo-admin setup with no production traffic pointed
at it until the `.env` change happens later. If you get stuck on a specific
step, that's a good point to come back with the exact error message.

## Explicitly out of scope here

Pointing track.fox.com at this server (the `.env` changes on the app side)
is a separate step, deliberately not covered in this runbook — do that only
after every check in Step 7 and Step 12 passes.
