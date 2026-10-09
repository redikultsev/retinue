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
