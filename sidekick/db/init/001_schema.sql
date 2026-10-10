-- Sidekick control-plane schema. Defaults (skills, policies, workflows, settings) are seeded by the API on startup.

CREATE TABLE settings (
  key    TEXT PRIMARY KEY,
  value  JSONB NOT NULL
);

-- One row per integration (aws, akamai, github, ...). Non-secret config is plain JSON;
-- secret fields are Fernet-encrypted JSON and never returned by the API.
CREATE TABLE integrations (
  key             TEXT PRIMARY KEY,
  config          JSONB       NOT NULL DEFAULT '{}',
  secrets         BYTEA,
  status          TEXT        NOT NULL DEFAULT 'not_configured',  -- not_configured | configured | connected | error
  status_detail   TEXT,
  last_tested_at  TIMESTAMPTZ,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workflows (
  id           TEXT PRIMARY KEY,
  name         TEXT        NOT NULL,
  description  TEXT        NOT NULL DEFAULT '',
  active       BOOLEAN     NOT NULL DEFAULT true,
  mode         TEXT        NOT NULL DEFAULT 'Live',
  version      INTEGER     NOT NULL DEFAULT 1,
  steps        JSONB       NOT NULL DEFAULT '[]',
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE skills (
  key          TEXT PRIMARY KEY,
  name         TEXT    NOT NULL,
  description  TEXT    NOT NULL,
  tools        JSONB   NOT NULL DEFAULT '[]',
  enabled      BOOLEAN NOT NULL DEFAULT true,
  autonomy     TEXT    NOT NULL DEFAULT 'approval'   -- suggest | approval | auto
);

CREATE TABLE users (
  id             SERIAL PRIMARY KEY,
  name           TEXT NOT NULL,
  email          TEXT NOT NULL UNIQUE,
  role           TEXT NOT NULL DEFAULT 'Developer', -- Owner | SRE | Developer | PM | Viewer
  slack_user_id  TEXT UNIQUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE policies (
  code         TEXT PRIMARY KEY,
  title        TEXT    NOT NULL,
  description  TEXT    NOT NULL,
  enabled      BOOLEAN NOT NULL DEFAULT true
);

-- A conversation = one Slack thread / DM / dashboard console session. `status` drives the agent's if/else brain.
CREATE TABLE conversations (
  id          SERIAL PRIMARY KEY,
  source      TEXT NOT NULL,              -- slack | dashboard
  channel     TEXT NOT NULL,
  thread_ts   TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'new',
  context     JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, channel, thread_ts)
);

CREATE TABLE runs (
  id               SERIAL PRIMARY KEY,
  conversation_id  INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  intent           TEXT NOT NULL,
  skill            TEXT NOT NULL,
  title            TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'running',   -- running | done | failed
  source           TEXT NOT NULL,
  requested_by     TEXT,
  steps            JSONB NOT NULL DEFAULT '[]',       -- [{tool, detail, status, secs, live}]
  result           JSONB,                             -- {text, actions}
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ
);
CREATE INDEX runs_created_idx ON runs (created_at DESC);

CREATE TABLE approvals (
  id               SERIAL PRIMARY KEY,
  conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  run_id           INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  title            TEXT NOT NULL,
  summary          TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
  requested_by     TEXT,
  decided_by       TEXT,
  decided_at       TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Messages the backend wants delivered to Slack (e.g. "approved from the dashboard"); the bot polls this.
CREATE TABLE outbox (
  id          SERIAL PRIMARY KEY,
  channel     TEXT NOT NULL,
  thread_ts   TEXT NOT NULL DEFAULT '',
  text        TEXT NOT NULL,
  run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  delivered   BOOLEAN NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
