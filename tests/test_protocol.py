"""Protocol store: one row per model run, for the status page."""

from retinue.protocol import Store


def test_runs_keep_tokens_and_the_last_limit_state(tmp_path):
    store = Store(str(tmp_path / "r.sqlite"))
    assert store.last_rate_limit() is None and store.runs_since(0) == {}
    store.run(agent_id="assistant", conversation_id="c1", kind="conversation", status="done", ts=100.0,
              meta={"num_turns": 2.0, "duration_ms": 3100.0, "cost_usd": 0.12,
                    "usage": {"input_tokens": 1200.0, "output_tokens": 300.0, "cache_read_tokens": 56000.0},
                    "rate_limit": {"status": "allowed_warning", "rate_limit_type": "five_hour", "utilization": 0.8,
                                   "resets_at": 1760003600.0}})
    store.run(agent_id="assistant", conversation_id="summary", kind="summary", status="limit", ts=200.0,
              meta={"limit": True})
    store.run(agent_id="assistant", conversation_id="c1", kind="compact", status="done", ts=300.0, meta={})
    row = store.db.execute("SELECT kind, status, num_turns, duration_ms, input_tokens, output_tokens,"
                           " cache_read_tokens, cache_write_tokens FROM runs ORDER BY id").fetchone()
    assert row == ("conversation", "done", 2, 3100, 1200, 300, 56000, None), "counts are whole numbers"
    assert store.last_rate_limit() == {"status": "allowed_warning", "type": "five_hour", "utilization": 0.8,
                                       "resets_at": 1760003600, "seen": 100.0}, "a run that said nothing keeps the last"
    assert store.runs_since(150.0) == {"limit": 1, "done": 1}


def test_each_limit_window_keeps_its_last_known_share(tmp_path):
    """Windows come and go from one event to the next: each keeps its own last value, and an old database gets the
    columns."""
    import sqlite3

    path = str(tmp_path / "r.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, agent_id TEXT NOT NULL,"
                " conversation_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, num_turns INTEGER,"
                " duration_ms INTEGER, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER,"
                " cache_read_tokens INTEGER, cache_write_tokens INTEGER, rate_status TEXT, rate_type TEXT,"
                " rate_utilization REAL, rate_resets_at INTEGER)")
    old.commit()
    store = Store(path)
    assert store.windows() == {}
    both = {"five_hour": {"utilization": 0.23, "resets_at": 1760010000.0},
            "seven_day": {"utilization": 0.63, "resets_at": 1760500000.0}}
    store.run(agent_id="assistant", conversation_id="c", kind="conversation", status="done", ts=100.0,
              meta={"rate_limit": {"status": "allowed", "windows": both}})
    store.run(agent_id="assistant", conversation_id="c", kind="triage", status="done", ts=200.0,
              meta={"rate_limit": {"status": "allowed", "windows": {"five_hour": {"utilization": 0.31,
                                                                                  "resets_at": 1760010000.0}}}})
    store.run(agent_id="assistant", conversation_id="c", kind="mail", status="done", ts=300.0, meta={})
    assert store.windows() == {"five_hour": {"utilization": 0.31, "resets_at": 1760010000, "seen": 200.0},
                               "seven_day": {"utilization": 0.63, "resets_at": 1760500000, "seen": 100.0}}


def test_a_run_costs_what_it_added_to_its_session(tmp_path):
    """The CLI's `total_cost_usd` is its session's, carried on when the session is resumed: summed as it was, 23
    conversation runs came to $2687 on the status page. A run costs what it added; a new session counts whole; a
    fresh session per run (the summary, the mail) is whole each time. Rows from before `session_id` was kept go by
    their conversation, for the kinds that resume one."""
    store = Store(str(tmp_path / "r.sqlite"))

    def run(kind, cost, session=None, conversation="c", ts=100.0):
        store.run(agent_id="assistant", conversation_id=conversation, kind=kind, status="done", ts=ts,
                  meta={"cost_usd": cost, **({"session_id": session} if session else {})})

    run("conversation", 1.0, "s1", ts=10.0)    # before the week: still what the next run is counted from
    run("conversation", 1.5, "s1")
    run("reminder", 2.25, "s1")
    run("conversation", 0.25, "s2")            # a new session: whole
    run("summary", 0.5, "x1", conversation="summary")
    run("summary", 0.75, "x2", conversation="summary")
    run("conversation", 3.0, conversation="old")   # written before session_id: by the conversation
    run("conversation", 3.5, conversation="old")
    run("conversation", 2.0, conversation="old")   # smaller than the one before: started over, whole
    run("triage", 0.25, conversation="mail")
    run("triage", 0.5, conversation="mail")    # a kind that never resumes: whole
    costs = {row["kind"]: row["cost_usd"] for row in store.tokens(50.0)}
    assert costs == {"conversation": 0.5 + 0.25 + 3.0 + 0.5 + 2.0, "reminder": 0.75, "summary": 1.25,
                     "triage": 0.75}
    run("conversation", 9.0, "s1", ts=100.0 + 61 * 86400)  # s1 idle two months: looked for 30 days back at most
    month = {row["kind"]: row["cost_usd"] for row in store.tokens(100.0 + 60 * 86400)}
    assert month == {"conversation": 9.0}, "an overestimate, never a sum of totals"
    plans = " ".join(row[3] for row in store.db.execute(
        "EXPLAIN QUERY PLAN SELECT p.cost_usd FROM runs p WHERE p.session_id = 's1' AND p.id < 9 ORDER BY p.id DESC"))
    assert "runs_session" in plans, plans
    assert store.db.execute("SELECT session_id FROM runs WHERE id = 1").fetchone() == ("s1",)
