"""Numbered migrations (`migrate`, docs/migrations.md): a database the last release left is upgraded in place and
keeps every row, a second start runs no step, and a database a newer Retinue wrote is never opened, let alone written.

The last release's schema is in `fixtures/migrate/`: what main's stores left on a disk before the steps were
numbered, dumped from `sqlite_master` (the columns its `ALTER TABLE`s added are in their tables' text)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from retinue import chat, endings, feed, migrate, outbox, protocol, sessions, subs, tobase

FIXTURES = Path(__file__).parent / "fixtures" / "migrate"
# The stores of each database file, in the order the services open them, with the module that holds their steps.
STORES = {"platform": (("sessions", sessions), ("feed", feed), ("subs", subs), ("endings", endings),
                       ("tobase", tobase), ("chat", chat)),
          "router": (("protocol", protocol), ("outbox", outbox))}
# A table of each store, gone in the newer database of the refusal test: an older store must not make it again.
OWN_TABLE = {"sessions": "kv", "feed": "assistant_digest", "subs": "sub_starts", "endings": "turn_endings",
             "tobase": "base_dirty", "chat": "chat",
             "protocol": "sent", "outbox": "drafts"}
# Main writes a room without its conversation (`Store.save_room`): the legacy copy into `conversations` has nothing to take.
AS_MAIN_WRITES = {("rooms", "context_id"): ""}


def _open(database: str, path: Path) -> sqlite3.Connection:
    """Every store of the file opened, as the platform (`platformd`) and the router (`router.main`) open them."""
    if database == "platform":
        store = sessions.PlatformStore(str(path))
        feed.Feed(store)
        subs.Subs(SimpleNamespace(store=store))  # and its turn endings (`endings`)
        tobase.ToBase(SimpleNamespace(store=store))
        chat.Chat(SimpleNamespace(store=store))
        return store.db
    store = protocol.Store(str(path))
    outbox.OutboxStore(store.db)
    return store.db


def _tables(db: sqlite3.Connection) -> list[str]:
    return [name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                           "'sqlite_%' ORDER BY name")]


def _value(table: str, column: str, kind: str):
    kind = kind.upper()
    if "INT" in kind:
        return 1
    if "REAL" in kind:
        return 1.5
    if "BLOB" in kind:
        return b"\x00"
    return f"{table}.{column}"


def _fill(db: sqlite3.Connection) -> None:
    """A row in every table, each column given a value of its type."""
    for table in _tables(db):
        columns = [(c[1], c[2]) for c in db.execute(f"PRAGMA table_info({table})")]
        values = [AS_MAIN_WRITES.get((table, name), _value(table, name, kind)) for name, kind in columns]
        db.execute(f"INSERT INTO {table} ({', '.join(n for n, _ in columns)}) VALUES "
                   f"({', '.join('?' * len(columns))})", values)
    db.commit()


def _rows(db: sqlite3.Connection, tables: dict | None = None) -> dict[str, tuple[list[str], list[tuple]]]:
    """Each table's rows, by the columns it has — or, given an earlier reading, by the columns it had then."""
    out = {}
    for table in tables or _tables(db):
        columns = tables[table][0] if tables else [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
        out[table] = (columns, db.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid").fetchall())
    return out


def _last_release(database: str, path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript((FIXTURES / f"{database}.sql").read_text(encoding="utf-8"))
    _fill(db)
    db.close()


@pytest.mark.parametrize("database", STORES)
def test_a_database_from_the_last_release_upgrades_without_losing_a_row(tmp_path, database):
    path = tmp_path / f"{database}.sqlite"
    _last_release(database, path)
    old = sqlite3.connect(path)
    before = _rows(old)
    assert all(rows for _, rows in before.values())
    assert "migrations" not in before, "the last release records no version"
    old.close()

    upgraded = _open(database, path)
    assert _rows(upgraded, before) == before
    for store, module in STORES[database]:
        assert migrate.version(upgraded, store) == len(module.STEPS) >= 1


@pytest.mark.parametrize("database", STORES)
@pytest.mark.parametrize("start", ["fresh", "last release"])
def test_a_second_start_runs_no_step(tmp_path, caplog, database, start):
    path = tmp_path / f"{database}.sqlite"
    if start == "last release":
        _last_release(database, path)
    caplog.set_level(logging.INFO, logger="retinue.migrate")
    _open(database, path)
    ran = [record.args[:2] for record in caplog.records if record.name == "retinue.migrate"]
    assert ran == [(store, n) for store, module in STORES[database] for n in range(1, len(module.STEPS) + 1)]

    caplog.clear()
    db = _open(database, path)
    assert [record for record in caplog.records if record.name == "retinue.migrate"] == []
    assert db.total_changes == 0, "a second start writes no row"


@pytest.mark.parametrize("database", STORES)
def test_a_database_newer_than_the_code_refuses_to_open(tmp_path, database):
    path = tmp_path / f"{database}.sqlite"
    _open(database, path).close()
    for store, module in STORES[database]:
        db = sqlite3.connect(path)
        db.execute("UPDATE migrations SET version = version + 1 WHERE store = ?", (store,))
        db.execute(f"DROP TABLE {OWN_TABLE[store]}")  # what the newer step did: the older CREATE must not undo it
        db.commit()
        db.close()
        written = path.read_bytes()

        with pytest.raises(migrate.NewerDatabase) as refused:
            _open(database, path)
        said = str(refused.value)
        assert store in said and str(path) in said
        assert f"version {len(module.STEPS) + 1}" in said and f"knows {len(module.STEPS)}" in said
        assert path.read_bytes() == written, "an older store writes nothing into a newer database"

        db = sqlite3.connect(path)  # back as this code left it, for the next store of the file
        db.execute("UPDATE migrations SET version = version - 1 WHERE store = ?", (store,))
        db.commit()
        db.close()
        _open(database, path).close()  # its table made again: the step that dropped it is undone with the version


def test_a_step_that_fails_leaves_the_database_as_it_was():
    """A step is one transaction with its version: one that raises leaves neither its half nor a version behind,
    and the next start runs it again."""
    db = sqlite3.connect(":memory:")

    def half(db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE notes ADD COLUMN at REAL")
        raise RuntimeError("the step's own bug")

    with pytest.raises(RuntimeError, match="own bug"):
        migrate.run(db, "notes", ("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT); "
                                  "INSERT INTO notes (text) VALUES ('kept; with a semicolon')", half))
    assert migrate.version(db, "notes") == 1
    assert [c[1] for c in db.execute("PRAGMA table_info(notes)")] == ["id", "text"]
    assert not db.in_transaction

    migrate.run(db, "notes", ("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT); "
                              "INSERT INTO notes (text) VALUES ('kept; with a semicolon')",
                              lambda db: db.execute("ALTER TABLE notes ADD COLUMN at REAL")))
    assert migrate.version(db, "notes") == 2
    assert db.execute("SELECT text, at FROM notes").fetchall() == [("kept; with a semicolon", None)]
