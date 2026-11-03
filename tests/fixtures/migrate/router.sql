-- The router database as main left it before numbered migrations (b035c86): the tables of protocol, outbox,
-- dumped from sqlite_master after every store opened it once. A fixture of tests/test_migrate.py: never edit it.
CREATE TABLE protocol (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                conversation_id TEXT NOT NULL,      -- gen_ai.conversation.id (A2A context id)
                source TEXT NOT NULL,               -- who sent: owner mxid or agent id
                target TEXT NOT NULL,               -- who received
                status TEXT NOT NULL,               -- done | failed | rejected | error
                input_chars INTEGER NOT NULL,
                output_chars INTEGER NOT NULL,
                num_turns INTEGER,
                cost_usd REAL,                      -- reported by the engine; on a subscription it is a usage gauge
                duration_ms INTEGER
            , channel TEXT);
CREATE TRIGGER protocol_append_only_u BEFORE UPDATE ON protocol
                BEGIN SELECT RAISE(ABORT, 'protocol is append-only'); END;
CREATE TRIGGER protocol_append_only_d BEFORE DELETE ON protocol
                BEGIN SELECT RAISE(ABORT, 'protocol is append-only'); END;
CREATE TABLE rooms (      -- Matrix adapter: agent room per agent
                agent_id TEXT PRIMARY KEY,          -- '_protocol' for the protocol room
                room_id TEXT NOT NULL,
                context_id TEXT NOT NULL            -- legacy: conversations moved to `conversations`
            );
CREATE TABLE places (     -- where an agent lives in a channel: Telegram topic id, ...
                channel TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                place_id TEXT NOT NULL,
                PRIMARY KEY (channel, agent_id)
            );
CREATE TABLE kv (         -- small adapter state, e.g. the Telegram update offset
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
CREATE TABLE conversations (  -- one current conversation per agent, for every channel
                agent_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL            -- A2A context id
            );
CREATE TABLE sent (       -- a message the router sent: messenger id -> archive event id
                channel TEXT NOT NULL,
                native_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                PRIMARY KEY (channel, native_id)
            );
CREATE TABLE buttons (    -- one row per button; the messenger carries only `id`
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,             -- archive id of the card the button belongs to
                label TEXT NOT NULL,
                action TEXT NOT NULL,               -- what the router does when it is pressed
                value TEXT NOT NULL,
                expires REAL NOT NULL,
                used REAL                           -- when the card was spent; NULL while it is live
            , alone INTEGER NOT NULL DEFAULT 0);
CREATE TABLE runs (       -- one row per model run: what the status page will count
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                agent_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- conversation | retry | summary | compact
                status TEXT NOT NULL,               -- done | failed | rejected | error | limit
                num_turns INTEGER,
                duration_ms INTEGER,
                cost_usd REAL,                      -- the CLI's estimate; on a subscription a gauge, not a bill
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                rate_status TEXT,                   -- the subscription limit as the CLI last reported it in this run
                rate_type TEXT,                     -- five_hour | seven_day | seven_day_opus | ...
                rate_utilization REAL,              -- share of the window used, 0..1
                rate_resets_at INTEGER              -- unix seconds
            , five_hour_utilization REAL, five_hour_resets_at INTEGER, seven_day_utilization REAL, seven_day_resets_at INTEGER, session_id TEXT);
CREATE INDEX runs_session ON runs (session_id, id);
CREATE INDEX runs_conversation ON runs (conversation_id, id);
CREATE TABLE drafts (
                id TEXT PRIMARY KEY,                -- 8 hex: the key a sender sends once
                created REAL NOT NULL,
                state TEXT NOT NULL,                -- shown | link | queued | sending | sent | failed | unknown
                                                    -- | cancelled | dropped | fixing | replaced | expired | stale
                channel TEXT NOT NULL,              -- mail | telegram
                envelope TEXT NOT NULL,             -- courier.Envelope as JSON
                digest TEXT NOT NULL,
                reply_to TEXT NOT NULL DEFAULT '',  -- the archive id answered
                thread TEXT NOT NULL DEFAULT '',    -- where a newer message makes the card stale: mail:<thread>, chat:<id>
                head TEXT NOT NULL,                 -- the card's header lines, JSON
                card TEXT NOT NULL DEFAULT '',      -- the archive id of the card shown
                expires REAL NOT NULL,
                send_after REAL,                    -- queued: the end of the hold
                result TEXT NOT NULL DEFAULT '{}',  -- what the sender answered
                at REAL NOT NULL
            );
CREATE INDEX drafts_state ON drafts (state);
