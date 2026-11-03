-- The platform database as main left it before numbered migrations (b035c86): the tables of sessions, feed, subs, tobase,
-- dumped from sqlite_master after every store opened it once. A fixture of tests/test_migrate.py: never edit it.
CREATE TABLE sessions (
                id TEXT PRIMARY KEY,                -- 8 hex
                track TEXT NOT NULL,
                lane TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- code | private
                state TEXT NOT NULL,                -- active | waiting | idle | closed
                cli_session TEXT NOT NULL DEFAULT '',
                cli_version TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                at REAL NOT NULL,
                idle_since REAL,
                turn INTEGER NOT NULL DEFAULT 0,    -- the turn the next raw line goes into
                turn_run TEXT,                      -- the run whose lines the open turn holds; NULL: none yet
                pinned INTEGER NOT NULL DEFAULT 0,  -- the owner keeps it on top of the face's list
                parent TEXT,                        -- a side session: the session its question is about
                context_tokens INTEGER,             -- what the main conversation's last request carried
                sphere TEXT,                        -- the owner's sphere of life it belongs to (`config.Sphere`)
                main INTEGER NOT NULL DEFAULT 0,    -- the sphere's main session: at most one (`sessions_main`)
                fork_of TEXT,                       -- a fork: the session whose conversation it started from
                cwd TEXT,                           -- its track's directory when it started; NULL: from before
                seen_at REAL,                       -- when the owner last looked at it or wrote into it
                news_at REAL,                       -- when it last said something new: a summary, a card
                turn_started REAL,                  -- when its running turn began: idle or closed -> active
                sub_of TEXT,                        -- a sub-session: the session that started it (`subs`)
                title TEXT,                         -- a sub-session's given title, or the one it named itself;
                                                    -- NULL: its first input
                turn_by TEXT,                       -- who started its current turn: owner | parent | report (`subs`)
                sphere_by TEXT,                     -- who set its sphere last: owner | session; NULL: its start
                hops INTEGER NOT NULL DEFAULT 0,    -- a parent: child<->parent exchanges since the owner last wrote
                owner_hold TEXT,                    -- a sub-session the owner stopped or wrote into: stopped | wrote;
                                                    -- its parent may not write to it until he writes to the parent
                owner_joined INTEGER NOT NULL DEFAULT 0, -- the owner wrote into its current turn (its ping is his)
                stopped_run TEXT,                   -- the run of it the owner stopped: what it says is no report
                archived REAL                       -- when the owner archived it: out of the face's list until a turn
                                                    -- of it begins again
            );
CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                session TEXT NOT NULL,
                hook_token_hash TEXT NOT NULL UNIQUE,
                marks TEXT NOT NULL,                -- approvals.Mark of each marker hook n, JSON
                started REAL NOT NULL,
                ended REAL,
                mode TEXT                           -- the permission mode its init line named; NULL: not seen yet
            );
CREATE TABLE interrupted (
                run TEXT PRIMARY KEY,               -- a run the platform itself stopped while it shut down
                at REAL NOT NULL,
                resumed REAL                        -- when its session was resumed for it: once
            );
CREATE TABLE inputs (
                input_id TEXT PRIMARY KEY,          -- the router's: tg:<message id>
                session TEXT NOT NULL,
                text TEXT NOT NULL,
                at REAL NOT NULL
            , by TEXT, attachments TEXT NOT NULL DEFAULT '[]', turn INTEGER, n INTEGER);
CREATE TABLE input_thumbs (
                input_id TEXT NOT NULL,
                n INTEGER NOT NULL,                 -- the attachment's place in the input's `attachments`
                jpeg BLOB NOT NULL,                 -- inbox.thumbnail: what the face's transcript shows of a picture
                PRIMARY KEY (input_id, n)
            );
CREATE TABLE answer_inputs (
                input_id TEXT PRIMARY KEY,          -- the router's tg:<message id> of a reply taken as a question's answer
                approval TEXT NOT NULL,
                at REAL NOT NULL
            );
CREATE TABLE raw (
                id INTEGER PRIMARY KEY,
                lane TEXT NOT NULL,
                lane_seq INTEGER NOT NULL,
                session TEXT NOT NULL,
                run TEXT NOT NULL,
                turn INTEGER NOT NULL,
                line TEXT NOT NULL,
                at REAL NOT NULL,
                UNIQUE (lane, lane_seq)
            );
CREATE INDEX raw_session ON raw (session, turn);
CREATE TABLE items (
                session TEXT NOT NULL,
                turn INTEGER NOT NULL,
                n INTEGER NOT NULL,
                kind TEXT NOT NULL,
                data TEXT NOT NULL,
                first_raw INTEGER NOT NULL,
                last_raw INTEGER NOT NULL,
                PRIMARY KEY (session, turn, n)
            );
CREATE TABLE approvals (
                id TEXT PRIMARY KEY,                -- 8 hex
                session TEXT NOT NULL,
                run TEXT NOT NULL,
                tool_use_id TEXT NOT NULL,
                kind TEXT NOT NULL,                 -- approval | courier | question
                tool TEXT NOT NULL,
                input TEXT NOT NULL,
                cwd TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                digest TEXT NOT NULL,
                state TEXT NOT NULL,                -- pending | shown | granted | executing | executed | rejected
                                                    -- | stale | dropped | expired; a question: answered, not granted
                view TEXT NOT NULL DEFAULT '{}',    -- the card as shown: courier.CardView
                created REAL NOT NULL,
                at REAL NOT NULL,
                why TEXT NOT NULL DEFAULT ''        -- why it came to the owner: the classifier's reason, a card rule
            );
CREATE INDEX approvals_call ON approvals (tool_use_id);
CREATE TABLE buttons (
                id TEXT PRIMARY KEY,                -- BUTTON_PREFIX + token_urlsafe(12)
                approval TEXT NOT NULL,             -- a ping's reply: ping/<seq> (pings)
                action TEXT NOT NULL,               -- approve | reject | open; a question's q<n>:<option> | q<n>:done;
                                                    -- a ping's reply:<n>
                digest TEXT NOT NULL,               -- a ping's reply: its session
                expires REAL NOT NULL,
                used REAL
            );
CREATE TABLE notes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,                 -- card | update | summary | ping (pings) | file | file_skipped (outfiles)
                session TEXT NOT NULL,
                approval TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL,
                given INTEGER NOT NULL DEFAULT 0
            );
CREATE TABLE queue (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL,
                prompt TEXT,                        -- NULL: a resume after the owner's decision
                content TEXT,                       -- JSON, with attachments: inbox.Ready.lane — blocks, files
                at REAL NOT NULL,
                attempt TEXT,                       -- the run sent for it whose answer was lost: the lane's journal
                                                    -- tells whether it started
                by TEXT                             -- who it is from: owner (NULL) | parent | report (`subs`)
            );
CREATE INDEX inputs_session ON inputs (session, at);
CREATE INDEX approvals_session ON approvals (session, state);
CREATE INDEX notes_session ON notes (session, kind);
CREATE INDEX buttons_approval ON buttons (approval);
CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,                          -- session | item | items_reset | approval | summary | file | upload | collection…
                session TEXT,
                key TEXT,                           -- the object the event replaces whole
                data TEXT,
                at REAL
            );
CREATE TABLE login_codes (
                hash TEXT PRIMARY KEY,              -- SHA-256 of the code the bot gave the owner
                expires REAL NOT NULL,
                used REAL
            );
CREATE TABLE logins (
                hash TEXT PRIMARY KEY,              -- SHA-256 of the cookie
                via TEXT NOT NULL,                  -- code | telegram
                created REAL NOT NULL,
                expires REAL NOT NULL
            );
CREATE TABLE auth_failures (at REAL NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE base_files (
                session TEXT NOT NULL,              -- a session whose runs changed this Base file (`tobase`)
                path TEXT NOT NULL,                 -- relative to the clone's root; uncommitted while it is here
                via TEXT NOT NULL,                  -- snapshot | tool | both: what saw the change
                at REAL NOT NULL,
                PRIMARY KEY (session, path)
            );
CREATE TABLE push_touched (
                session TEXT NOT NULL,              -- a session whose runs worked in this repository (`topush`)
                repo TEXT NOT NULL,
                at REAL NOT NULL,
                PRIMARY KEY (session, repo)
            );
CREATE TABLE push_repos (
                repo TEXT PRIMARY KEY,              -- the room's clone of it as its last look found it (`topush`)
                branch TEXT NOT NULL,
                sha TEXT NOT NULL,
                n INTEGER NOT NULL,                 -- its commits no branch of the remote has: «Push · N»
                at REAL NOT NULL
            , why TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX sessions_main ON sessions (sphere) WHERE main = 1;
CREATE TABLE assistant_digest (id INTEGER PRIMARY KEY CHECK (id = 1), items TEXT NOT NULL, bot TEXT NOT NULL DEFAULT '', at REAL NOT NULL, chips TEXT NOT NULL DEFAULT '[]', counts TEXT NOT NULL DEFAULT '{}');
CREATE TABLE sub_starts (
                call TEXT PRIMARY KEY,              -- the parent's tool call that started it: one child per call
                parent TEXT NOT NULL,
                child TEXT NOT NULL,
                digest TEXT NOT NULL,               -- of title and brief: the same start again after a lost answer
                at REAL NOT NULL
            );
CREATE TABLE sub_reports (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                parent TEXT NOT NULL,
                child TEXT NOT NULL,
                text TEXT NOT NULL,
                at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'held', -- held | delivered | dropped (the owner stopped the family)
                run TEXT                            -- the child's run whose turn it reports: one report per run
            );
CREATE INDEX sub_reports_parent ON sub_reports (parent, state);
CREATE TABLE sub_proposals (
                id TEXT PRIMARY KEY,
                call TEXT NOT NULL UNIQUE,          -- the parent's tool call that proposed it: one proposal per call
                parent TEXT NOT NULL,
                title TEXT NOT NULL,
                brief TEXT NOT NULL,
                digest TEXT NOT NULL,               -- of title and brief: the same proposal again is the one made
                at REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'open', -- open | started | declined | expired (`expire`, past PROPOSAL_S)
                child TEXT,                         -- the sub-session started for it
                decided REAL,
                said TEXT,                          -- the owner's last message to the parent when it was proposed
                from_report INTEGER NOT NULL DEFAULT 0  -- proposed in a turn a sub-session's report started
            );
CREATE INDEX sub_proposals_parent ON sub_proposals (parent, state);
CREATE UNIQUE INDEX sub_reports_run ON sub_reports (child, run);
CREATE TABLE base_runs (
                run TEXT PRIMARY KEY,             -- a run of a session on the Base's lane, watched
                session TEXT NOT NULL,
                before TEXT,                      -- the tree's snapshot at its last mark: path -> digest, JSON;
                                                  -- NULL: it failed, the file tools alone count for that window
                last_raw INTEGER NOT NULL DEFAULT 0  -- the raw line (`raw.id`) its last mark read up to
            );
CREATE TABLE base_cards (
                id TEXT PRIMARY KEY,              -- 8 hex; its «Undo» is base/<id>
                session TEXT NOT NULL,
                state TEXT NOT NULL,              -- committing | committed | failed | undone (older rows: expired)
                files TEXT NOT NULL,              -- [{path, status, add, del, on, digest}], JSON
                subject TEXT NOT NULL DEFAULT '', -- unused
                fallback TEXT NOT NULL,           -- the commit's subject: `<Space>: <title>`
                ping INTEGER,                     -- the session's latest ping when it was opened
                result TEXT NOT NULL DEFAULT '{}', -- {sha, files, refused, said, undone}
                created REAL NOT NULL,
                at REAL NOT NULL,
                auto INTEGER NOT NULL DEFAULT 0   -- the platform's own commit after a turn: «Undo», not «Commit»
            );
CREATE INDEX base_cards_session ON base_cards (session, state);
CREATE TABLE base_dirty (
                path TEXT PRIMARY KEY,            -- the clone's uncommitted paths as it last said (`_dirty`)
                status TEXT NOT NULL
            );
CREATE TABLE base_asks (
                session TEXT NOT NULL,            -- told what kept its files out of the Base (`_ask`)
                digest TEXT NOT NULL,             -- of those problems: the same ones again are the owner's
                at REAL NOT NULL,
                PRIMARY KEY (session, digest)
            );
