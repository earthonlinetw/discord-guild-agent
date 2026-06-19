-- ============================================
-- Migration 002: 圖片分析結果
-- ============================================

-- 由視覺模型解析後的圖片內容，供 Agent 後續參考
CREATE TABLE IF NOT EXISTS image_analysis (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    image_url    TEXT NOT NULL,
    filename     TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',   -- 視覺模型產生的描述
    model        TEXT NOT NULL DEFAULT '',   -- 使用的視覺模型
    agent_name   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(message_id, image_url)
);

CREATE INDEX IF NOT EXISTS idx_image_analysis_guild_channel
    ON image_analysis (guild_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_image_analysis_message
    ON image_analysis (message_id);
