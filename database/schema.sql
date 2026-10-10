-- Idempotent schema + migrations: safe to run on every start.

CREATE TABLE IF NOT EXISTS users (
    user_id     BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_blocked  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS videos (
    id                      BIGSERIAL PRIMARY KEY,
    video_number            INTEGER,
    category                TEXT CHECK (category IN ('new', 'old')),  -- NULL = pending admin choice
    telegram_file_id        TEXT,                                     -- NULL for videos found by the MTProto scanner
    telegram_file_unique_id TEXT NOT NULL UNIQUE,
    caption                 TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    last_error              TEXT,
    added_by                BIGINT
);

-- Added for the channel scanner (all idempotent).
ALTER TABLE videos ALTER COLUMN telegram_file_id DROP NOT NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS channel_id          BIGINT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS message_date       TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS duration           INTEGER;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS file_size          BIGINT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS mime_type          TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS width              INTEGER;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS height             INTEGER;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMPTZ NOT NULL DEFAULT now();
-- TRUE once an admin sets the category by hand: the age-based refresh then leaves it alone.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS category_locked    BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Video numbers used to be per category (New #1, Old #1). They are now one global sequence.
-- One-time migration: drop the per-category index, renumber, then enforce global uniqueness.
DROP INDEX IF EXISTS uq_videos_cat_number;
DROP INDEX IF EXISTS idx_videos_cat_number;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM meta WHERE key = 'numbering_v2') THEN
        UPDATE videos v SET video_number = r.rn
        FROM (SELECT id, row_number() OVER (
                        ORDER BY COALESCE(message_date, created_at), id) AS rn
              FROM videos WHERE category IS NOT NULL) r
        WHERE v.id = r.id;
        UPDATE videos SET video_number = NULL WHERE category IS NULL;
        INSERT INTO meta (key, value) VALUES ('numbering_v2', 'done');
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_videos_number
    ON videos (video_number) WHERE video_number IS NOT NULL;
-- Source-message identity. NULL channel/message (manually added videos) never collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_videos_source
    ON videos (channel_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_videos_category    ON videos (category);
CREATE INDEX IF NOT EXISTS idx_videos_message_date ON videos (message_date);
CREATE INDEX IF NOT EXISTS idx_videos_is_active   ON videos (is_active);
CREATE INDEX IF NOT EXISTS idx_videos_cat_active_created
    ON videos (category, is_active, created_at DESC);

-- Scanner progress. One row; survives restarts.
CREATE TABLE IF NOT EXISTS scan_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    status           TEXT NOT NULL DEFAULT 'idle',   -- idle | running | completed | stopped | failed | interrupted
    mode             TEXT,                           -- full | incremental
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    messages_scanned INTEGER NOT NULL DEFAULT 0,
    videos_found     INTEGER NOT NULL DEFAULT 0,
    videos_added     INTEGER NOT NULL DEFAULT 0,
    duplicates       INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    last_message_id  BIGINT NOT NULL DEFAULT 0,      -- everything up to here is already registered
    last_error       TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO scan_state (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Database-backed session state (replaces Redis)
CREATE TABLE IF NOT EXISTS user_states (
    user_id     BIGINT PRIMARY KEY,
    state       TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS requests (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    category   TEXT NOT NULL,
    requested  INTEGER NOT NULL,
    delivered  INTEGER NOT NULL,
    failed     INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests (created_at);

-- ======================================================================================
-- Upgrade: per-user delivery history, restart-safe auto-delete queue, rate limiting.
-- All statements are idempotent; none of them touches existing data.
-- ======================================================================================

-- Which video each user has already received. Never cleared by /scan, restarts or redeploys;
-- only an administrator (/resethistory) can wipe it. category = the collection it came from.
CREATE TABLE IF NOT EXISTS user_video_history (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    video_id     BIGINT NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    category     TEXT,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_video UNIQUE (user_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_uvh_user_cat ON user_video_history (user_id, category);
CREATE INDEX IF NOT EXISTS idx_uvh_video    ON user_video_history (video_id);

-- One row per video message the BOT sent to a user. A background worker deletes the Telegram
-- message once delete_at has passed. Only the user's copy is touched: the source channel
-- post and the videos table are never modified by this.
CREATE TABLE IF NOT EXISTS delivered_messages (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL,
    chat_id             BIGINT NOT NULL,
    video_id            BIGINT REFERENCES videos (id) ON DELETE SET NULL,
    telegram_message_id BIGINT NOT NULL,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    delete_at           TIMESTAMPTZ NOT NULL,
    deleted_at          TIMESTAMPTZ,
    attempts            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dm_due
    ON delivered_messages (delete_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_dm_user ON delivered_messages (user_id);

-- Lightweight fixed-window rate limiter (replaces Redis).
CREATE TABLE IF NOT EXISTS user_rate_limits (
    user_id      BIGINT PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits         INTEGER NOT NULL DEFAULT 0
);

-- The auto-delete explanation is shown once, with the user's first batch.
ALTER TABLE users ADD COLUMN IF NOT EXISTS delete_notice_shown BOOLEAN NOT NULL DEFAULT FALSE;

-- ======================================================================================
-- Admin panel upgrade (all additive and idempotent).
-- ======================================================================================

-- Persistent key/value settings edited from the Admin Panel (bot on/off, maintenance, texts, ...).
-- Survives Railway restarts; a missing key falls back to the environment default.
CREATE TABLE IF NOT EXISTS bot_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Admin-imposed block. Separate from users.is_blocked, which only records that the user
-- blocked the bot in Telegram (and is reset whenever they write again).
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_at TIMESTAMPTZ;

-- Safe, human-readable system events for the Logs panel. Never holds secrets or stack traces.
CREATE TABLE IF NOT EXISTS event_log (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind    TEXT NOT NULL,
    message TEXT NOT NULL,
    user_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_event_log_ts   ON event_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_event_log_kind ON event_log (kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users (last_seen DESC);

-- ======================================================================================
-- Upgrade: several source channels. Every channel keeps its own scan checkpoint.
-- (scan_state stays the single "last scan" status row; only the checkpoint moved.)
CREATE TABLE IF NOT EXISTS scan_checkpoints (
    channel_id      BIGINT PRIMARY KEY,
    last_message_id BIGINT NOT NULL DEFAULT 0,   -- everything up to here is already registered
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One-time migration: the old single checkpoint belonged to the one channel that existed.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM meta WHERE key = 'multi_channel_v1') THEN
        INSERT INTO scan_checkpoints (channel_id, last_message_id)
        SELECT channel_id,
               GREATEST(max(telegram_message_id),
                        COALESCE((SELECT last_message_id FROM scan_state WHERE id = 1), 0))
        FROM videos
        WHERE channel_id IS NOT NULL AND telegram_message_id IS NOT NULL
        GROUP BY channel_id
        ON CONFLICT (channel_id) DO NOTHING;
        INSERT INTO meta (key, value) VALUES ('multi_channel_v1', 'done');
    END IF;
END $$;

-- ======================================================================================
-- Fix: every channel post is its own row (same video re-posted / in two channels).
-- The old UNIQUE(telegram_file_unique_id) collapsed e.g. 5,001 channel videos into 3,986 rows.
-- Identity of a channel video is now (channel_id, telegram_message_id) only (uq_videos_source).
-- Videos added by hand (no channel) still stay unique by file_unique_id.
-- Existing rows are untouched; run one Full Scan afterwards to register the missing copies.
-- ======================================================================================
ALTER TABLE videos DROP CONSTRAINT IF EXISTS videos_telegram_file_unique_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_videos_manual_uid
    ON videos (telegram_file_unique_id) WHERE channel_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_videos_unique_id ON videos (telegram_file_unique_id);

-- ======================================================================================
-- Force Subscribe Manager (additive, idempotent): channels users must be members of.
-- Managed from the Admin Panel (📢 Force Subscribe Manager).
-- ======================================================================================
CREATE TABLE IF NOT EXISTS force_sub_channels (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    username   TEXT,
    url        TEXT NOT NULL,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
