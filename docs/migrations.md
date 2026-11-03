# Migrations

A store's SQLite tables change by numbered steps (`retinue/migrate.py`). A store declares `STEPS` and opens through
`migrate.run(db, store, STEPS, schema)`: each step runs once per database, in order, in one transaction with the
version it brings. A start after the first runs none.

The version is a row per store in the file's `migrations` table, not `PRAGMA user_version`: `platform.sqlite`
holds six stores (`sessions`, `feed`, `subs`, `endings`, `tobase`, `chat`) and `router.sqlite` two (`protocol`, `outbox`), each opened
on its own.

**Upgrade from a release before the steps.** No version is recorded. Step 1 of every store is the upgrade its open
did before (`PRAGMA table_info`, then `ALTER TABLE`): it adds the columns that are missing, so the file converges
without a row rewritten.

**A database newer than the code.** A store at a version above its steps refuses to open (`migrate.NewerDatabase`)
before anything is written to the file: run the newer release, or put back the copy taken before the update
([backup.md](backup.md)).

**Adding a step.** Append a function of the connection (it never commits) or SQL to the store's `STEPS`; a new
column, table or index goes there, not into `schema`. Never change a step a release has shipped.
