"""`retinue-platform collections …`: the owner's commands for his collections, run on the server's terminal inside
the platform's container (docs/collections.md#from-the-servers-terminal). They are the one way a form and the rights
are set: no route of the platform's reaches them, so neither the Router nor an agent can.

Each command opens the file itself, in this process, through the same store as the platform: its writes take the
file's lock like any other (`BEGIN IMMEDIATE`, refused past 2 s), and the platform's thread takes in what they wrote at
its next write (`Collections._fresh`). What changes a form, the rights or the records is tried first and shown — the
diff, the records it was tried on — and kept only on «y».
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import time
from collections.abc import Callable
from pathlib import Path

from .collections import RIGHTS_ELSEWHERE, RIGHTS_KEYS, Collections, Refused, Rejected, Writer

OWNER = Writer("owner", "define")


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("collections", help="the owner's collections: forms, rights, imports, export")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="every collection, with its Space and how many records it holds")
    show = actions.add_parser("show", help="one collection's form, rights and latest batches")
    show.add_argument("collection")
    define = actions.add_parser("define", help="make a collection, or grow its form, from a JSON file "
                                               '{"space": …, "form": {…}, "views": […]}')
    define.add_argument("collection")
    define.add_argument("file", type=Path)
    rights = actions.add_parser("rights", help="set a collection's rights from a JSON file "
                                               '{"writers": {…}, "set_by": {…}}')
    rights.add_argument("collection")
    rights.add_argument("file", type=Path)
    imports = actions.add_parser("import", help="apply a JSONL file of operations, one batch")
    imports.add_argument("collection")
    imports.add_argument("file", type=Path)
    revert = actions.add_parser("revert-batch", help="take back a batch, leaving the records changed after it")
    revert.add_argument("batch")
    export = actions.add_parser("export", help="write everything to an empty folder")
    export.add_argument("folder", type=Path)


def run(args: argparse.Namespace, path: str, ask: Callable[[str], str] | None = None,
        say: Callable[[str], None] = print, clock: Callable[[], float] = time.time) -> int:
    """The command `args` names, on the file at `path`: 0 when it did what was asked (or nothing was needed), 1 when
    it was refused or not confirmed. `ask` puts the question and reads the answer: the terminal's, by default."""
    store, ask = Collections(path), ask or input

    async def body() -> int:
        try:
            return await COMMANDS[args.action](store, args, ask, say, clock)
        except Rejected as exc:
            say(f"Refused: {exc.reason}")
            return 1
        except Refused as exc:
            say(str(exc))
            return 1
        finally:
            await store.close()

    return asyncio.run(body())


async def _list(store: Collections, args, ask, say, clock) -> int:
    shown = await store.catalog()
    if not shown:
        say("No collections yet: make one with `collections define <collection> <file>`.")
    for c in shown:
        say(f"{c['name']}  space {c['space']}  form revision {c['form_rev']}  {c['records']} records"
            + (f" ({c['deleted']} deleted)" if c["deleted"] else ""))
    return 0


async def _show(store: Collections, args, ask, say, clock) -> int:
    shown = await store.schema(args.collection)
    if shown is None:
        raise Rejected(f"there is no collection {args.collection!r}")
    say(f"{shown['name']}: space {shown['space']}, form revision {shown['form_rev']}, {shown['records']} records"
        + (f" ({shown['deleted']} deleted)" if shown["deleted"] else ""))
    say("form: " + _pretty(shown["form"]))
    say("rights: " + _pretty(shown["rights"]))
    say("views: " + _pretty(shown["views"]))
    for b in shown["batches"]:
        say(f"batch {b['id']}  {b['kind']}  {b['state']}  by {b['actor']}  {b['changes']} changes  "
            f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(b['created']))} UTC")
    return 0


async def _define(store: Collections, args, ask, say, clock) -> int:
    given = _read(args.file)
    if not isinstance(given, dict):
        raise Rejected('a define file is a JSON object: {"space": "<space>", "form": {"fields": {…}}}')
    if set(given) & RIGHTS_KEYS:
        raise Rejected(RIGHTS_ELSEWHERE)
    if unknown := set(given) - {"space", "form", "views"}:
        raise Rejected(f'a define file takes "space", "form" and "views", not {", ".join(sorted(map(repr, unknown)))}')
    if "form" not in given or "space" not in given:
        raise Rejected('a define file names the collection\'s "space" and its "form"')
    views = given.get("views")  # none: the views it has stay
    trial = await store.define(OWNER, args.collection, given["space"], given["form"], clock(), dry=True, views=views)
    if trial["unchanged"]:
        say(f"{args.collection} already has this form, in {trial['space']}: nothing to apply.")
        return 0
    say(f"{args.collection}: {'new collection, ' if trial['before'] is None else ''}space {trial['space']}, "
        f"form revision {trial['form_rev']}")
    if trial["before"] != trial["after"]:
        say(_diff(trial["before"], trial["after"], "form"))
    if trial["views_before"] != trial["views_after"]:
        say(_diff(trial["views_before"], trial["views_after"], "views"))
    if trial["failed"]:
        say(f"{trial['failed']} of {trial['records']} records do not fit it:")
        for failure in trial["failures"]:
            say(f"  {failure['id'] or '-'}: {failure['reason']}")
        return 1
    say(f"All {trial['records']} records fit it." if trial["records"] else "No records yet.")
    if not _yes(ask, say):
        return 1
    writer = Writer(f"define:{trial['form_rev']}", "define")
    done = await store.define(writer, args.collection, given["space"], given["form"], clock(), views=views)
    say(f"Applied: {args.collection} is at form revision {done['form_rev']}.")
    return 0


async def _rights(store: Collections, args, ask, say, clock) -> int:
    given = _read(args.file)
    trial = await store.rights(OWNER, args.collection, given, clock(), dry=True)
    if trial["unchanged"]:
        say(f"{args.collection} already has these rights: nothing to apply.")
        return 0
    say(_diff(trial["before"], trial["after"], "rights"))
    if not _yes(ask, say):
        return 1
    await store.rights(OWNER, args.collection, given, clock())
    say(f"Applied: the rights of {args.collection}.")
    return 0


async def _import(store: Collections, args, ask, say, clock) -> int:
    ops = []
    try:
        text = args.file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Rejected(f"cannot read {args.file}: {exc}") from None
    for n, line in enumerate(text.rstrip("\n").split("\n"), 1):
        try:
            ops.append(json.loads(line))
        except ValueError:
            raise Rejected(f"line {n}: not a JSON operation") from None
    trial = await store.import_ops(args.collection, ops, clock(), dry=True)
    say(f"{trial['lines']} lines into {args.collection}: {trial['added']} to add, {trial['changed']} to change, "
        f"{trial['same']} already so.")
    if not _yes(ask, say):
        return 1
    done = await store.import_ops(args.collection, ops, clock())
    say(f"Imported as batch {done['batch']}: {done['added']} added, {done['changed']} changed, {done['same']} "
        f"already so. Take it back with `collections revert-batch {done['batch']}`.")
    return 0


async def _revert_batch(store: Collections, args, ask, say, clock) -> int:
    trial = await store.revert_batch(OWNER, args.batch, clock(), dry=True)
    _touched(trial, say)
    if not trial["reverted"]:
        say(f"Nothing of batch {args.batch} to take back.")
        if trial["state"] == "reverted":
            await store.revert_batch(OWNER, args.batch, clock())  # it is all taken back already: say so in its state
        return 0 if not trial["touched"] else 1
    say(f"Takes back {trial['reverted']} changes of {trial['records']} records.")
    if not _yes(ask, say):
        return 1
    done = await store.revert_batch(OWNER, args.batch, clock())
    say(f"Taken back as batch {done['undo']}: {done['reverted']} changes of {done['records']} records"
        + (f"; {len(done['touched'])} left as they are." if done["touched"] else "."))
    return 0


def _touched(answer: dict, say: Callable[[str], None]) -> None:
    if answer["touched"]:
        say(f"{len(answer['touched'])} records were changed after the batch and are left as they are:")
        for t in answer["touched"]:
            say(f"  {t['collection']}/{t['id']}: {t['op']} by {t['by']} ({t['change_id']})")


async def _export(store: Collections, args, ask, say, clock) -> int:
    done = await store.export(args.folder)
    say(f"Exported to {args.folder}: {done['collections']} collections, {done['records']} records, "
        f"{done['changes']} changes, {done['files']} files.")
    return 0


COMMANDS = {"list": _list, "show": _show, "define": _define, "rights": _rights, "import": _import,
            "revert-batch": _revert_batch, "export": _export}


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise Rejected(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise Rejected(f"{path} is not JSON: {exc}") from None


def _pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _diff(before, after, what: str) -> str:
    lines = difflib.unified_diff(_pretty(before).splitlines() if before is not None else [],
                                 _pretty(after).splitlines(), f"{what} now", f"{what} applied", lineterm="")
    return "\n".join(lines)


def _yes(ask: Callable[[str], str], say: Callable[[str], None]) -> bool:
    try:
        yes = ask("Apply? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        yes = False
    if not yes:
        say("Nothing applied.")
    return yes
