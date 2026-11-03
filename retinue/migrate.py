"""Numbered migrations: how a store's tables change from one release to the next (docs/migrations.md).

A store declares its steps — each a function of the connection, or SQL — and opens through `run`. A step runs once,
in order, in one transaction with the version it brings. The version is the store's own row in `migrations`: one
file holds several stores, so SQLite's one `user_version` per file could not say which is where. A store at a
version above the code's steps is refused before anything is written to the file.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence

# A step: a function of the connection (it never commits), or SQL — one or more statements, `;` between them.
Step = Callable[[sqlite3.Connection], object] | str

log = logging.getLogger("retinue.migrate")


class NewerDatabase(RuntimeError):
    """The file holds a store at a version this code has no step for: a newer Retinue wrote it."""


def version(db: sqlite3.Connection, store: str) -> int:
    """The steps of `store` this file has been through; 0: none recorded — a fresh file, or one from before the
    steps were numbered."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migrations'").fetchone():
        return 0
    row = db.execute("SELECT version FROM migrations WHERE store = ?", (store,)).fetchone()
    return row[0] if row else 0


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    """The columns `table` has: what a step 1 recognises a database from before the steps by."""
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def run(db: sqlite3.Connection, store: str, steps: Sequence[Step], schema: str = "") -> list[int]:
    """The store opened: refused when the file is newer than `steps`, its `schema` (`CREATE … IF NOT EXISTS`) made,
    then each step it has not been through. What the connection had not committed is committed first, as
    `executescript` does. Returns the numbers of the steps run: none on every start after the first."""
    if db.in_transaction:
        db.commit()
    _refuse_newer(db, store, steps, version(db, store))
    if schema:
        db.executescript(schema)
    if version(db, store) == len(steps):
        return []
    ran = []
    while True:
        db.execute("BEGIN IMMEDIATE")  # another process opening the same file waits here, then finds the step done
        try:
            db.execute("CREATE TABLE IF NOT EXISTS migrations (store TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                       "at REAL NOT NULL)")
            at = version(db, store)
            _refuse_newer(db, store, steps, at)
            if at == len(steps):
                db.rollback()
                return ran
            _apply(db, steps[at])
            db.execute("INSERT INTO migrations (store, version, at) VALUES (?, ?, ?) ON CONFLICT (store) DO UPDATE "
                       "SET version = excluded.version, at = excluded.at", (store, at + 1, time.time()))
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        log.info("%s: step %d of %d done in %s", store, at + 1, len(steps), _file(db))
        ran.append(at + 1)


def _refuse_newer(db: sqlite3.Connection, store: str, steps: Sequence[Step], at: int) -> None:
    if at > len(steps):
        raise NewerDatabase(
            f"{_file(db)}: the {store} tables are at version {at}, this Retinue knows {len(steps)} — a newer Retinue "
            "wrote this database, and an older one must not write it. Run the newer version again, or put back the "
            "snapshot taken before the update (docs/migrations.md).")


def _apply(db: sqlite3.Connection, step: Step) -> None:
    if callable(step):
        step(db)
        return
    statement = ""
    for piece in step.split(";"):  # a `;` inside a quoted string is not the statement's end: complete_statement says
        statement += piece + ";"
        if sqlite3.complete_statement(statement):
            if statement.strip(" \t\r\n;"):
                db.execute(statement)
            statement = ""
    if statement.strip(" \t\r\n;"):
        raise ValueError(f"a step's SQL ends inside a statement: {statement.strip()[:80]}")


def _file(db: sqlite3.Connection) -> str:
    return db.execute("PRAGMA database_list").fetchone()[2] or ":memory:"
