-- ============================================
-- Migration 003: 去重與查詢索引
-- ============================================

-- 加速同一伺服器近期 tool call 查詢，供重複操作防護與稽核使用。
CREATE INDEX IF NOT EXISTS idx_tool_calls_guild_tool_created
    ON tool_calls (guild_id, tool_name, created_at DESC);

-- 加速依訊息批次查詢，避免同訊息重複處理時掃描過多資料。
CREATE INDEX IF NOT EXISTS idx_messages_message_batch
    ON messages (message_id, batch_id);
