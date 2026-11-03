"""The router's bus tools, each stated once: the name, words and schema the model sees, the grant it needs, whether
this router has the part it works with, and the code that answers it. The engine's side (`engine.bus_tools`) and the
router's (`bus.BusServer`) are both made from `TOOLS`; which of them an agent gets is its `engine.bus_tools`.

Every call goes through `run`, which does, once and in this order, what no tool repeats: the caller's own open turn,
the grant, the part, the arguments as an object; then the tool's answer, its line in the protocol and its
announcement where the protocol is shown. The model never decides any of it.

On the wire: `POST /tools/<name>` with `{turn, arguments}`, signed with the agent's bus token, answers
`{ok, text, images?}`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import colltools, courier, pages

if TYPE_CHECKING:
    from .bus import Turn
    from .config import RouterAgent
    from .core import Core

QUERY_CHARS = 70_000  # a query's answer (at most 60 KB of records) kept inline, never put in a file
NOT_AN_OBJECT = "Not done: the tool's arguments are an object."


class Refused(Exception):
    """A refusal before anything was done: the model reads it as the tool's answer, the protocol does not record it."""


@dataclass
class Answer:
    ok: bool
    text: str
    chars: int | None = None  # the call's input as the protocol counts it; None — its arguments as JSON
    images: list[tuple[str, bytes]] = field(default_factory=list)  # (media type, data)


@dataclass
class Call:
    """One call, past every check of `run`: the caller's own turn and the arguments as an object."""
    core: Core
    caller: RouterAgent
    turn: Turn
    args: dict

    def text(self, key: str) -> str:
        return str(self.args.get(key) or "")

    def number(self, key: str) -> int:
        try:
            return int(self.args.get(key) or 0)
        except (TypeError, ValueError):
            return 0


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    answer: Callable[[Call], Awaitable[Answer]]
    during: str                 # «No active request: <during> only during an answer.»
    grant: str = ""             # the RouterAgent flag it needs; none — ask_agent's rules are `bus.check`'s
    denied: str = ""            # the refusal without the grant
    wired: Callable[[Core], object] | None = None  # whether this router has the part it works with
    off: str = ""               # the refusal when it has not
    log: str = ""               # its target in the protocol; empty — the answer records itself, or nothing does
    said: tuple[str, str] | None = None  # its line where the protocol is shown: what ({} the agent), the word for done
    timeout: float = 30         # how long the engine waits for the router
    annotations: dict | None = None


async def run(core: Core, caller: RouterAgent, turn_id: str, name: str, arguments) -> Answer:
    """An agent's call of the tool `name` in its turn `turn_id`, answered. Only `TOOLS`' names."""
    tool = TOOLS[name]
    turn = core.turns.of(caller.id, turn_id)
    if turn is None:
        return Answer(False, f"No active request: {tool.during} only during an answer.")
    if tool.grant and not getattr(caller, tool.grant):
        return Answer(False, tool.denied)
    if tool.wired is not None and not tool.wired(core):
        return Answer(False, tool.off)
    if not isinstance(arguments, dict):
        return Answer(False, NOT_AN_OBJECT)
    try:
        answer = await tool.answer(Call(core, caller, turn, arguments))
    except Refused as exc:
        return Answer(False, str(exc))
    if tool.log:
        chars = len(json.dumps(arguments, ensure_ascii=False)) if answer.chars is None else answer.chars
        core.store.log(conversation_id=f"tree-{turn.tree.id}", source=caller.id, target=tool.log,
                       status="done" if answer.ok else "rejected", input_chars=chars, output_chars=len(answer.text),
                       channel="bus")
    if tool.said:
        what, done = tool.said
        await core._each(core.channels, "protocol", f"{what.format(caller.name)}: {done if answer.ok else 'refused'}")
    return answer


# --- the answers -------------------------------------------------------------------------------------------------

async def _ask_agent(call: Call) -> Answer:
    return await call.core.bus_call(call.caller, call.turn, call.text("agent"), call.text("text"))


async def _list_agents(call: Call) -> Answer:
    caller = call.caller
    allowed = [a for a in call.core.agents.values()
               if a.id != caller.id and (a.id in caller.can_call or "*" in caller.can_call)]
    return Answer(True, json.dumps([{"id": a.id, "name": a.name, "description": a.description,
                                     "trust_class": a.trust_class} for a in allowed], ensure_ascii=False))


async def _search_archive(call: Call) -> Answer:
    return await call.core.archive_search(call.caller, call.turn, call.text("query"), call.text("scope"),
                                          call.text("since"), call.text("until"))


def _reminders(action: str) -> Callable[[Call], Awaitable[Answer]]:
    """The owner's reminders: every check of the time — the format, the weekday, the past, a repeat — is code's
    (`Scheduler`)."""
    async def answer(call: Call) -> Answer:
        jobs, now, when, weekday = call.core.jobs, time.time(), call.text("when"), call.text("weekday")
        if action == "add":
            ok, text = jobs.add(call.text("text"), when, weekday, now, call.text("repeat"))
        elif action == "list":
            ok, text = True, jobs.listing()
        elif action == "cancel":
            ok, text = jobs.cancel(call.number("id"))
        else:
            ok, text = jobs.move(call.number("id"), when, weekday, now)
        return Answer(ok, text, chars=len(call.text("text")))
    return answer


async def _set_timezone(call: Call) -> Answer:
    return await call.core.proactive.zone_tool(call.turn, call.text("zone"))


def _plan(action: str) -> Callable[[Call], Awaitable[Answer]]:
    async def answer(call: Call) -> Answer:
        return await call.core.plan_tool(call.turn, action, call.args)
    return answer


async def _get_attachment(call: Call) -> Answer:
    return await call.core.get_attachment(call.caller, call.turn, call.number("id"))


async def _publish_trip(call: Call) -> Answer:
    """What she sends is checked and looked up in travel-ops by code (`Pages.publish`); the answer has the address."""
    ok, text = await call.core.pages.publish(call.args, call.core.travel, time.time())
    return Answer(ok, text)


async def _draft_reply(call: Call) -> Answer:
    """Code builds the envelope from the archive and shows the owner the card (`Outbox.propose`); nothing leaves
    without his press. Whether someone else's words started the run, the router knows — not her: the turn's marks."""
    outbox, tree = call.core.outbox, call.turn.tree
    ok, text = await outbox.propose(call.args, set(tree.foreign), outbox.clock(), reads=list(tree.reads),
                                    others=bool(tree.exchanges))
    return Answer(ok, text)


def _collection(action: str) -> Callable[[Call], Awaitable[Answer]]:
    async def answer(call: Call) -> Answer:
        return await call.core.collection_tool(call.turn, action, call.args)
    return answer


# --- the tools ---------------------------------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["query"],
    "properties": {
        "query": {"type": "string", "maxLength": 200,
                  "description": "one to three keywords, nouns are best: word endings are matched by themselves"},
        "scope": {"type": "string", "enum": ["chat", "owner", "mail", "all"],
                  "description": "where to look: chat — this conversation (the default when judging somebody "
                                 "else's message), owner — conversations with the owner, mail — his letters and "
                                 "calendar, all — everything"},
        "since": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "description": "from this day, 2026-10-09"},
        "until": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "description": "up to and including this day"},
    },
}

_REMINDERS = {"during": "reminders can be set", "grant": "reminders",
              "denied": "Denied: this agent has not been granted reminders."}
_PLAN = {"during": "the plan is changed", "grant": "collections",
         "denied": "Denied: this agent has not been granted the owner's collections.",
         "wired": lambda core: getattr(core, "planner", None) is not None and core.platform is not None,
         "off": "The plan is not enabled in this router: say in your answer what to move."}
_COLLECTIONS = {"during": "the collections are read and written", "grant": "collections",
                "denied": "Denied: this agent has not been granted the owner's collections.",
                "wired": lambda core: core.platform is not None,
                "off": "The platform is not connected: there are no collections here. Say in your answer what should "
                       "be recorded.",
                "timeout": 60}


def _collection_tool(spec: dict) -> Tool:
    """One of the collections' tools: the very schema and words a session gets (`colltools.TOOLS`). The router names
    her and her turn's marks to the platform; the platform checks the rights (`Core.collection_tool`)."""
    action = spec["name"].removeprefix("collection_")
    writes = action in ("apply", "propose")
    return Tool(spec["name"], spec["description"], spec["inputSchema"], _collection(action), **_COLLECTIONS,
                log=f"collections/{action}", said=("collections: {} → " + action, "done") if writes else None,
                annotations={**spec["annotations"], **({"maxResultSizeChars": QUERY_CHARS} if action == "query" else {})})


TOOLS: dict[str, Tool] = {tool.name: tool for tool in (
    Tool("ask_agent",
         "Ask another Retinue agent through the router and wait for the answer. The router decides whether it "
         "is allowed; a refusal comes with the reason. agent — the agent's id, text — a self-contained question: "
         "the agent does not see this conversation. The list of agents is the list_agents tool.",
         {"agent": str, "text": str}, _ask_agent, during="agents can be asked", timeout=900),
    Tool("list_agents", "The Retinue agents you may ask: id, name, description.", {}, _list_agents,
         during="agents can be listed"),
    Tool("search_archive",
         "Find in the archive what was said before: the owner's messages, your past answers, letters, the "
         "calendar, Telegram chats and your captions of other people's photos. query — one to three keywords, "
         "nouns are best: word endings are matched by themselves. scope — where to look; since and until — days. "
         "Returns the finds with id, date, author and a note on where they come from. If nothing is found, it "
         "names where it looked and what the archive covers; try other words or a wider search.",
         SEARCH_SCHEMA, _search_archive, during="the archive can be searched", grant="archive",
         denied="Denied: this agent has not been granted archive search.", log="archive"),
    Tool("set_reminder",
         "Set a reminder for the owner. When it is due, the router wakes you and you write it in your own words; "
         "if you cannot, it sends text word for word. So write text the owner will understand in a few days: "
         "what to do, for whom, why. when — the owner's local time, in his zone from the «Now» note, like "
         "2026-10-09T18:00. weekday — the day of the week you mean («Friday»): the router checks it against the "
         "date and refuses if it does not match or the time has passed. The same text at the same time returns "
         "«Already set». repeat — empty (once), daily, weekdays, weekly or monthly; when is then the first time. "
         "Tell the owner the day and time from the router's answer, not the id.",
         {"text": str, "when": str, "weekday": str, "repeat": str}, _reminders("add"), **_REMINDERS,
         log="reminders/add"),
    Tool("list_reminders", "The owner's active reminders, nearest first: when, text, [id].", {}, _reminders("list"),
         **_REMINDERS, log="reminders/list"),
    Tool("cancel_reminder", "Cancel a reminder by its id from list_reminders.", {"id": int}, _reminders("cancel"),
         **_REMINDERS, log="reminders/cancel"),
    Tool("move_reminder",
         "Move a reminder by its id from list_reminders to another time. when and weekday — as in set_reminder.",
         {"id": int, "when": str, "weekday": str}, _reminders("move"), **_REMINDERS, log="reminders/move"),
    Tool("set_timezone",
         "Change the owner's time zone when he says he is in another city or asks to change the zone («I'm in "
         "Tokyo», «switch my zone to Manila», or in Russian «я в Токио»). zone — an IANA name (Asia/Tokyo) or a "
         "city as IANA writes it (Tokyo). Repeating reminders and the summary follow the new zone, one-off ones "
         "keep their moment. If the owner is only about to travel, do not change it: set a reminder for the day "
         "he arrives.",
         {"zone": str}, _set_timezone, during="the zone changes", grant="reminders",
         denied="Denied: this agent has no reminders granted, and the zone moves them.",
         wired=lambda core: core.proactive, off="Changing the time zone is not enabled in this router.", log="zone"),
    Tool("plan_move",
         "Move a task's time in the owner's plan when he asks in his own message («put the English homework on "
         "Wednesday evening», «move the report to 15:00 tomorrow»). task — its id from collection_query on tasks. "
         "day — the owner's local date, YYYY-MM-DD. part — morning (till 13:00), afternoon (13:00–18:00) or evening "
         "(from 18:00): the code takes the first free time there; or at — HH:MM, only a time the owner named "
         "himself. Its next piece moves, or a new one is laid when it has none. named — true only when the owner "
         "named that very piece of time he had put there himself. Meetings never move. Done at once; after your "
         "answer the code shows him one line of the turn's moves with «Undo» — do not repeat it.",
         {"type": "object", "properties": {
             "task": {"type": "string"}, "day": {"type": "string"},
             "part": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
             "at": {"type": "string"}, "named": {"type": "boolean"}},
          "required": ["task", "day"]},
         _plan("move"), **_PLAN, log="plan/move", said=("plan: {} → move", "done")),
    Tool("plan_estimate",
         "Set how long a task takes (minutes), the day it is due by (due, YYYY-MM-DD) or the moment it must be done "
         "by (by, his local time YYYY-MM-DDTHH:MM: «before the lesson at 19:00 on Thursday») when the owner says it "
         "in his own message («the homework takes two hours», «the report is due Friday»). task — its id from "
         "collection_query. No piece of it is then laid to end after by. The plan lays the time again by itself; "
         "it goes under the turn's «Undo» with the moves.",
         {"type": "object", "properties": {
             "task": {"type": "string"}, "minutes": {"type": "integer"}, "due": {"type": "string"},
             "by": {"type": "string"}},
          "required": ["task"]},
         _plan("estimate"), **_PLAN, log="plan/estimate", said=("plan: {} → estimate", "done")),
    Tool("plan_hold",
         "Hold the owner's time when he says in his own message that he is doing something that is not a task: "
         "eating, on the road, resting («having lunch», «lying down for an hour», «tomorrow at 19:00 to the "
         "airport, an hour and a half»). label — what it is, short, in English («Lunch», «Rest», «Travel»; one of "
         "his activities when it is one). minutes — how long, as he said or the activity's usual length. at — his "
         "local time YYYY-MM-DDTHH:MM when it is not now (at most 14 days ahead, at the earliest 15 minutes ago). "
         "The plan's tasks it crosses move after it; a hold of a meal's name frees that meal's place that day. "
         "day_off — true when he says he is not working today («I'm off today», «no work today»): the rest of "
         "today's window is held and its tasks move to the next days; label and minutes are then ignored. Done at "
         "once; the code says it in the app.",
         {"type": "object", "properties": {
             "label": {"type": "string"}, "minutes": {"type": "integer"}, "at": {"type": "string"},
             "day_off": {"type": "boolean"}}},
         _plan("hold"), **_PLAN, log="plan/hold", said=("plan: {} → hold", "done")),
    Tool("get_attachment",
         "Get again what the owner sent, by the number from the mark [attachment #N: …]. After compaction the "
         "pictures and PDFs are gone from the conversation — this tool returns the picture and the text: the "
         "transcript, the document's text; for a PDF, only its text layer.",
         {"id": int}, _get_attachment, during="attachments can be fetched", grant="attachments",
         denied="Denied: this agent has not been granted attachments.", log="attachments/get", timeout=60),
    Tool("publish_trip",
         "Publish a trip page among the owner's pages: all the options you found, with prices, times, reviews and "
         "photos. Name each option by its link (link.url) and search_id from travel-ops' answers: the router takes "
         "prices, seller and times from travel-ops itself. The answer is the page's address; give it to the owner "
         "at the end of your answer as a «details» link. A refusal names the field — fix it and try again.",
         pages.TRIP_SCHEMA, _publish_trip, during="a page can be published", grant="trips",
         denied="Denied: this agent has not been granted trip publishing.", wired=lambda core: core.pages,
         off="Pages are not connected: there will be no page. Answer the owner without a link.",
         log="trips/publish", said=("pages: {} → trip page", "published"), timeout=120),
    Tool("draft_reply",
         "Propose to the owner a reply to a person — to a letter or a Telegram message — when a reply is really "
         "expected. reply_to — the id of the incoming item from the archive (mail:… or tgb:…): the router takes "
         "the address, subject and thread from there. A new letter — to and subject, only to an address the owner "
         "has already written with. text — the whole text as it will go out. The router shows the owner a card; "
         "it goes out only if he presses «Send». A refusal names what to fix; an «edit» from the owner is a new "
         "call with replaces.",
         courier.DRAFT_SCHEMA, _draft_reply, during="a draft can be proposed", grant="drafts",
         denied="Denied: this agent has not been granted drafts.", wired=lambda core: core.outbox,
         off="Sending to people is not connected: there will be no draft. Give the owner the text in your answer.",
         log="courier/draft", said=("courier: {} → draft", "card"), timeout=120),
    *map(_collection_tool, colltools.TOOLS),
)}
