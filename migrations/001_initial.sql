-- ============================================
-- Migration 001: 初始資料表
-- ============================================

-- Agent 註冊表
CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    personality TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 訊息暫存
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    message_id  TEXT NOT NULL UNIQUE,
    author_id   TEXT NOT NULL,
    author_name TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    is_bot      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    batch_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_guild_channel
    ON messages (guild_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_messages_batch
    ON messages (batch_id);

-- 摘要
CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    summary     TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_guild_channel
    ON summaries (guild_id, channel_id);

-- 長期記憶
CREATE TABLE IF NOT EXISTS memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    category    TEXT NOT NULL,  -- server_info / channel_purpose / rules / user_preference / decision
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(guild_id, category, key)
);

CREATE INDEX IF NOT EXISTS idx_memory_guild_category
    ON memory (guild_id, category);

-- 操作日誌
CREATE TABLE IF NOT EXISTS action_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL DEFAULT '',
    tool_name       TEXT NOT NULL DEFAULT '',
    parameters      TEXT NOT NULL DEFAULT '{}',
    result          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / executed / failed
    safety_level    TEXT NOT NULL DEFAULT 'SAFE',      -- SAFE / MODERATE / DANGEROUS
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_action_logs_guild
    ON action_logs (guild_id);
CREATE INDEX IF NOT EXISTS idx_action_logs_status
    ON action_logs (status);

-- 任務佇列
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    task_type       TEXT NOT NULL,   -- mention / admin / batch / council / override
    priority        INTEGER NOT NULL DEFAULT 0,
    payload         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued / processing / completed / failed / retry
    retry_count     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
    ON tasks (status, priority DESC);

-- Tool 呼叫紀錄
CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER REFERENCES tasks(id),
    guild_id        TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    parameters      TEXT NOT NULL DEFAULT '{}',
    reasoning       TEXT NOT NULL DEFAULT '',
    expected_result TEXT NOT NULL DEFAULT '',
    actual_result   TEXT NOT NULL DEFAULT '',
    safety_level    TEXT NOT NULL DEFAULT 'SAFE',
    executed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_task
    ON tool_calls (task_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_guild
    ON tool_calls (guild_id);

-- Migration 紀錄
CREATE TABLE IF NOT EXISTS migrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT NOT NULL UNIQUE,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
