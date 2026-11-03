"""A project's description (docs/events.md#the-description): `project.yaml` in the project's folder, beside its
`AGENTS.md` — its collections with their forms and views, the events it declares, the reactions it has to its own
events and to other projects', and its own views in code. The platform reads it and the owner applies it: from the
server's terminal (`retinue-platform collections project <project> <folder>`, `collections_admin.py`), or by his
«Apply» of a proposal a session made of it (`colltools.CollTools.propose_project`). A session never applies one.

The file is checked strictly here — a closed set of keys, any other stops it, the forms and the views by the same
checks as a `define` (`collections.parse_form`, `parse_views`) — and kept, applied, in the collections' file:
each collection by its own `define` (in the project's Space), the rest in `projects` (`Collections.put_project`). What
the platform does with events and reactions is `events.py`.

Rights are not in it: they stay the owner's, set from the terminal for each collection (`collections rights`), and a
project's reactions write as the class `project:<name>` with what those rights give it.
"""

from __future__ import annotations

import html.parser
import re
from collections.abc import Callable
from typing import Any

import yaml

from .collections import (NAME, RIGHTS_ELSEWHERE, RIGHTS_KEYS, SPACE, Collections, Rejected, Writer, _plain,
                          parse_form, parse_views)

FILE = "project.yaml"
TEXT_MAX = 256 * 2**10      # bytes of the file
PAGE_MAX = 256 * 2**10      # bytes of one view's page
EVENTS_MAX = 40             # events one project declares
REACTIONS_MAX = 40          # reactions one project has
CODE_VIEWS_MAX = 10         # views in code of one project
WORDS_MAX = 2000            # characters of a brief or of what a reaction asks the owner
FIELDS_MAX = 32             # fields a reaction's record is given
INSIDE = ("add", "task", "session", "ask")   # what a reaction may do inside the install
OUTSIDE = ("send", "pay", "publish")         # what acts outside it: such a reaction only asks the owner
DO_KEYS = {
    "add": {"on", "do", "collection", "fields"},
    "task": {"on", "do", "fields"},
    "session": {"on", "do", "track", "brief"},
    "ask": {"on", "do", "text"},
    **{kind: {"on", "do", "text"} for kind in OUTSIDE},
}
PAGE_PATH = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*\.html")
TRACK = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
ON = re.compile(rf"(?:({SPACE.pattern})/)?({NAME.pattern})")  # `<project>/<event>`, or `<event>` of its own
OWNER = Writer("owner", "define")


class _Loader(yaml.SafeLoader):
    """YAML without aliases — a file of a few lines that expands to gigabytes is refused, not read — and with YAML
    1.2's booleans: `true` and `false` only, so a reaction's `on:` is the key it reads as, not `True`."""

    def compose_node(self, parent, index):  # noqa: ANN001 — PyYAML's own signature
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases (*name) are not taken: write each value out")
        return super().compose_node(parent, index)


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                              list("tTfF"))


def read(text: str | bytes, project: str, page: Callable[[str], bytes | None] | None = None) -> dict:
    """The description in `project.yaml`'s text, checked (`parse`); `page(path)` reads a view's page beside it, None
    when there is none. Raises Rejected with the reason."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise Rejected(f"{FILE} is not UTF-8 text") from None
    if len(text.encode()) > TEXT_MAX:
        raise Rejected(f"{FILE} is at most {TEXT_MAX // 2**10} KB")
    try:
        given = yaml.load(text, Loader=_Loader)  # noqa: S506 — a SafeLoader without aliases
    except yaml.YAMLError as exc:
        raise Rejected(f"{FILE} is not YAML: {exc}") from None
    return parse(given if given is not None else {}, project, page)


def parse(given: Any, project: str, page: Callable[[str], bytes | None] | None = None) -> dict:
    """A project's description checked strictly and made whole: `{collections: {name: {form, views}}, events: {name:
    {collection, when, by}}, reactions: [...], views: [{name, kind: code, collection, title, tab, html}]}`. A view in
    code names its page by `file` (read with `page`) or gives it as `html`; it is kept with the page in it. Raises
    Rejected with the reason."""
    if not isinstance(project, str) or not SPACE.fullmatch(project):
        raise Rejected(f"not a project's name: {project!r}")
    if not isinstance(given, dict):
        raise Rejected(f"{FILE} is a mapping: collections, events, reactions, views")
    if set(given) & RIGHTS_KEYS:
        raise Rejected(RIGHTS_ELSEWHERE)
    if unknown := set(given) - {"collections", "events", "reactions", "views"}:
        raise Rejected(f"{FILE} takes collections, events, reactions and views; not "
                       f"{', '.join(sorted(map(repr, unknown)))}")
    collections = _collections(given.get("collections", {}))
    events = _events(given.get("events", {}))
    reactions = [_reaction(r, n, project) for n, r in enumerate(_list(given.get("reactions", []), "reactions",
                                                                       REACTIONS_MAX))]
    views = [_code_view(v, n, page) for n, v in enumerate(_list(given.get("views", []), "views", CODE_VIEWS_MAX))]
    names = [(v["collection"], v["name"]) for v in views]
    if len(set(names)) != len(names):
        raise Rejected("views: each view in code has a name of its own in its collection")
    for collection, name in names:
        if name in {v["name"] for v in collections.get(collection, {}).get("views", [])}:
            raise Rejected(f"views: {collection} has a view {name!r} already")
    return {"collections": collections, "events": events, "reactions": reactions, "views": views}


def _list(given: Any, what: str, most: int) -> list:
    if not isinstance(given, list) or len(given) > most:
        raise Rejected(f"{what} is a list of up to {most}")
    return given


def _collections(given: Any) -> dict:
    if not isinstance(given, dict):
        raise Rejected("collections is a mapping: {<name>: {form: {...}, views: [...]}}")
    kept = {}
    for name, spec in given.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Rejected(f"collections: a collection's name is lowercase letters, digits and _: not {name!r}")
        if not isinstance(spec, dict) or "form" not in spec or set(spec) - {"form", "views"}:
            if isinstance(spec, dict) and set(spec) & RIGHTS_KEYS:
                raise Rejected(RIGHTS_ELSEWHERE)
            raise Rejected(f"collections.{name} takes form and views")
        try:
            kept[name] = {"form": parse_form(spec["form"]), "views": parse_views(spec.get("views", []))}
        except Rejected as exc:
            raise Rejected(f"collections.{name}: {exc.reason}") from None
    return kept


def _events(given: Any) -> dict:
    if not isinstance(given, dict) or len(given) > EVENTS_MAX:
        raise Rejected(f"events is a mapping of up to {EVENTS_MAX}: {{<name>: {{collection, when}}}}")
    kept = {}
    for name, spec in given.items():
        where = f"events.{name}"
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Rejected(f"events: an event's name is lowercase letters, digits and _: not {name!r}")
        if not isinstance(spec, dict) or set(spec) - {"collection", "when", "by"} or "collection" not in spec:
            raise Rejected(f"{where} takes collection, when and by")
        if not isinstance(spec["collection"], str) or not NAME.fullmatch(spec["collection"]):
            raise Rejected(f"{where}.collection names one of the project's collections")
        event: dict = {"collection": spec["collection"]}
        if "when" in spec:
            event["when"] = _when(spec["when"], where)
        by = spec.get("by", [])
        if (not isinstance(by, list) or len(by) > 8 or len(set(map(str, by))) != len(by)
                or not all(b in ("owner", "session", "mac", "assistant") or isinstance(b, str)
                           and re.fullmatch(r"code:[a-z][a-z0-9_-]{0,39}", b) for b in by)):
            raise Rejected(f"{where}.by lists who fires it by a call: owner, session, mac, assistant, code:<rule>")
        event["by"] = list(by)
        kept[name] = event
    return kept


def _when(when: Any, where: str) -> dict:
    """`{added: true}` — a record added — or `{field: state, to: [paid]}` — a change that gives the field a value, one
    of `to` when it names some."""
    if when == {"added": True}:
        return {"added": True}
    shape = f"{where}.when is {{added: true}}, or {{field: <a field>, to: [<values>]}} with `to` optional"
    if not isinstance(when, dict) or set(when) - {"field", "to"} or not isinstance(when.get("field"), str) \
            or not NAME.fullmatch(when["field"]):
        raise Rejected(shape)
    to = when.get("to", [])
    if not isinstance(to, list) or len(to) > 20 or not all(isinstance(v, (str, int, bool)) for v in to):
        raise Rejected(shape)
    return {"field": when["field"], "to": list(to)}


def _reaction(given: Any, n: int, project: str) -> dict:
    where = f"reactions[{n}]"
    kind = given.get("do") if isinstance(given, dict) else None
    if kind not in DO_KEYS:
        raise Rejected(f"{where}: do is one of {', '.join(DO_KEYS)}")
    if unknown := set(given) - DO_KEYS[kind]:
        raise Rejected(f"{where} ({kind}) takes {', '.join(sorted(DO_KEYS[kind]))}; not "
                       f"{', '.join(sorted(map(repr, unknown)))}")
    on = ON.fullmatch(given["on"]) if isinstance(given.get("on"), str) else None
    if on is None:
        raise Rejected(f"{where}: on names an event, <project>/<event>, or <event> of this project's")
    kept: dict = {"on": f"{on.group(1) or project}/{on.group(2)}", "do": kind}
    if kind == "add":
        if not isinstance(given.get("collection"), str) or not NAME.fullmatch(given["collection"]):
            raise Rejected(f"{where}: collection names the one it adds a record to")
        kept["collection"] = given["collection"]
    if kind in ("add", "task"):
        kept["fields"] = _fields(given.get("fields"), where)
        if kind == "task" and "title" not in kept["fields"]:
            raise Rejected(f"{where}: a task's fields give it a title")
    if kind == "session":
        if not isinstance(given.get("track"), str) or not TRACK.fullmatch(given["track"]):
            raise Rejected(f"{where}: track names the track its session runs on")
        kept["track"] = given["track"]
    words = "brief" if kind == "session" else "text"
    if kind not in ("add", "task"):
        said = given.get(words)
        if not isinstance(said, str) or not said.strip() or not _plain(said.replace("\n", " "), WORDS_MAX):
            raise Rejected(f"{where}: {words} is plain text of at most {WORDS_MAX} characters")
        kept[words] = said.strip()
    return kept


def _fields(given: Any, where: str) -> dict:
    """A reaction's record: each field a value as it is, or `{from: <field>}` — the event's record's own (`id` its
    id) — never anything a model wrote."""
    if not isinstance(given, dict) or not 0 < len(given) <= FIELDS_MAX:
        raise Rejected(f"{where}: fields is a mapping of 1 to {FIELDS_MAX}: {{<field>: <value> or {{from: <field>}}}}")
    kept = {}
    for name, value in given.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Rejected(f"{where}: not a field's name: {name!r}")
        if isinstance(value, dict):
            if set(value) != {"from"} or not isinstance(value["from"], str) or not NAME.fullmatch(value["from"]):
                raise Rejected(f"{where}.fields.{name}: {{from: <a field of the event's record, or id>}}")
        elif not (value is None or isinstance(value, (bool, int, float)) or isinstance(value, str)
                  and _plain(value.replace("\n", " "), WORDS_MAX)):
            raise Rejected(f"{where}.fields.{name}: a plain value, or {{from: <field>}}")
        kept[name] = value
    return kept


class _Page(html.parser.HTMLParser):
    """What a view's page would load from elsewhere: an address in an attribute that loads (src, href of a link,
    srcset, poster, a form's action, data). Links out (`<a href>`) are fine; the frame's policy blocks the rest too."""

    LOADS = {"src", "srcset", "poster", "action", "data", "formaction", "background"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001 — HTMLParser's own signature
        if tag in ("iframe", "frame", "object", "embed", "base"):
            self.out.append(f"<{tag}>")
        for name, value in attrs:
            loads = name in self.LOADS or name == "href" and tag == "link"
            if loads and value and not value.strip().lower().startswith(("data:", "#")):
                self.out.append(f"<{tag} {name}>")


def _code_view(given: Any, n: int, page: Callable[[str], bytes | None] | None) -> dict:
    where = f"views[{n}]"
    keys = {"name", "kind", "collection", "title", "tab", "file", "html"}
    if not isinstance(given, dict) or given.get("kind") != "code" or set(given) - keys:
        raise Rejected(f"{where}: a project's own view is {{name, kind: code, collection, title, tab, file}}")
    for key in ("name", "collection"):
        if not isinstance(given.get(key), str) or not NAME.fullmatch(given[key]):
            raise Rejected(f"{where}: {key} is lowercase letters, digits and _")
    for key, most in (("title", 80), ("tab", 24)):
        if key in given and not _plain(given[key], most):
            raise Rejected(f"{where}: {key} is plain text of at most {most} characters")
    if ("file" in given) == ("html" in given):
        raise Rejected(f"{where}: its page is a file beside {FILE} (file: views/<name>.html), or given as html")
    if "file" in given:
        if not isinstance(given["file"], str) or not PAGE_PATH.fullmatch(given["file"]):
            raise Rejected(f"{where}: file is a page's path in the project's folder: views/<name>.html")
        data = page(given["file"]) if page is not None else None
        if data is None:
            raise Rejected(f"{where}: there is no page {given['file']} in the project's folder")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Rejected(f"{where}: {given['file']} is not UTF-8 text") from None
    else:
        text = given["html"]
        if not isinstance(text, str):
            raise Rejected(f"{where}: html is the page's text")
    if not text.strip() or len(text.encode()) > PAGE_MAX:
        raise Rejected(f"{where}: its page is 1 byte to {PAGE_MAX // 2**10} KB")
    found = _Page()
    found.feed(text)
    found.close()
    if found.out:
        raise Rejected(f"{where}: its page loads nothing from elsewhere and frames nothing: "
                       f"{', '.join(dict.fromkeys(found.out))} — inline it, or draw it in a script")
    return {"name": given["name"], "kind": "code", "collection": given["collection"],
            **{k: given[k] for k in ("title", "tab") if k in given}, "html": text}


def _order(collections: dict) -> list[str]:
    """The file's collections in an order a `ref` never comes before the collection it names, when that one is in
    the file too; the file's order otherwise."""
    done: list[str] = []

    def visit(name: str, seen: tuple[str, ...]) -> None:
        if name in done or name in seen:
            return
        for spec in collections[name]["form"]["fields"].values():
            if spec["type"] == "ref" and spec["to"] in collections and spec["to"] != name:
                visit(spec["to"], (*seen, name))
        done.append(name)
    for name in collections:
        visit(name, ())
    return done


async def apply(store: Collections, project: str, description: dict, now: float, dry: bool = False,
                writer: Writer = OWNER) -> dict:
    """A description (`parse`'s) applied as the owner's: each collection defined in the project's Space — tried
    first, all of them, so one that does not fit stops the rest before anything is kept — then its events,
    reactions and views in code. `{project, collections: {name: define's answer}, project_answer}`. Each event must
    be of one of the project's collections. Raises Rejected, Refused."""
    for name, event in description["events"].items():
        if event["collection"] not in description["collections"]:
            schema = await store.schema(event["collection"])
            if schema is None or schema["space"] != project:
                raise Rejected(f"events.{name}: {event['collection']} is not a collection of {project}")
    for view in description["views"]:
        if view["collection"] not in description["collections"]:
            schema = await store.schema(view["collection"])
            if schema is None or schema["space"] != project:
                raise Rejected(f"views: {view['collection']} is not a collection of {project}")
    order = _order(description["collections"])
    tried = {}
    for name in order:  # a new collection has no records to try: only its form, which `parse` checked
        spec = description["collections"][name]
        if await store.schema(name) is None:
            tried[name] = {"name": name, "before": None, "after": spec["form"], "views_before": [],
                           "views_after": spec["views"], "unchanged": False, "records": 0, "failed": 0}
            continue
        trial = await store.define(writer, name, project, spec["form"], now, dry=True, views=spec["views"])
        if trial["failed"]:
            raise Rejected(f"collections.{name}: {trial['failed']} of {trial['records']} records do not fit its form")
        tried[name] = trial
    rest = {k: description[k] for k in ("events", "reactions", "views")}
    if dry:
        return {"project": project, "collections": tried,
                "project_answer": await store.put_project(writer, project, rest, now, dry=True)}
    done = {}
    for name in order:
        spec = description["collections"][name]
        done[name] = await store.define(writer, name, project, spec["form"], now, views=spec["views"])
    return {"project": project, "collections": done, "project_answer": await store.put_project(writer, project, rest,
                                                                                               now)}
