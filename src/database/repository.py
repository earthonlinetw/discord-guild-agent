"""資料庫 Repository 層。

封裝所有資料庫操作，上層模組不直接撰寫 SQL。
每個 Repository 對應一張資料表，提供 CRUD 操作。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from src.database.connection import ConnectionProvider

logger = structlog.get_logger(__name__)


def _now_iso() -> str:
    """取得目前 UTC 時間的 ISO 格式字串。"""
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# Agent Repository
# ============================================================


class AgentRepository:
    """Agent 註冊表操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def upsert(
        self, name: str, personality: str, system_prompt: str
    ) -> int:
        """新增或更新 Agent。"""
        row = await self._db.fetchone(
            "SELECT id FROM agents WHERE name = ?", (name,)
        )
        if row:
            await self._db.execute(
                "UPDATE agents SET personality=?, system_prompt=?, updated_at=? WHERE name=?",
                (personality, system_prompt, _now_iso(), name),
            )
            return row["id"]
        cursor = await self._db.execute(
            "INSERT INTO agents (name, personality, system_prompt) VALUES (?, ?, ?)",
            (name, personality, system_prompt),
        )
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        """依名稱查詢 Agent。"""
        return await self._db.fetchone("SELECT * FROM agents WHERE name = ?", (name,))

    async def list_active(self) -> list[dict[str, Any]]:
        """列出所有啟用中的 Agent。"""
        return await self._db.fetchall("SELECT * FROM agents WHERE is_active = 1")


# ============================================================
# Message Repository
# ============================================================


class MessageRepository:
    """訊息暫存操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def insert(
        self,
        guild_id: str,
        channel_id: str,
        message_id: str,
        author_id: str,
        author_name: str,
        content: str,
        is_bot: bool = False,
        batch_id: str | None = None,
    ) -> int:
        """新增一筆訊息。

        Returns:
            實際新增的列數。0 表示 message_id 已存在，被去重忽略。
        """
        cursor = await self._db.execute(
            """INSERT OR IGNORE INTO messages
               (guild_id, channel_id, message_id, author_id, author_name, content, is_bot, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, message_id, author_id, author_name, content, int(is_bot), batch_id),
        )
        return cursor.rowcount  # type: ignore[return-value]

    async def get_recent(
        self, guild_id: str, channel_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """取得頻道最近 N 筆訊息。"""
        return await self._db.fetchall(
            """SELECT * FROM messages
               WHERE guild_id = ? AND channel_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, channel_id, limit),
        )

    async def get_unbatched(
        self, guild_id: str, channel_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """取得尚未批次處理的訊息。"""
        return await self._db.fetchall(
            """SELECT * FROM messages
               WHERE guild_id = ? AND channel_id = ? AND batch_id IS NULL
               ORDER BY created_at ASC LIMIT ?""",
            (guild_id, channel_id, limit),
        )

    async def mark_batched(self, message_ids: list[str], batch_id: str) -> None:
        """將訊息標記為已批次處理。"""
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        await self._db.execute(
            f"UPDATE messages SET batch_id = ? WHERE message_id IN ({placeholders})",
            (batch_id, *message_ids),
        )

    async def delete_old(self, guild_id: str, channel_id: str, before_date: str) -> int:
        """刪除舊訊息（已被摘要者）。"""
        cursor = await self._db.execute(
            "DELETE FROM messages WHERE guild_id=? AND channel_id=? AND created_at<?",
            (guild_id, channel_id, before_date),
        )
        return cursor.rowcount  # type: ignore[return-value]


# ============================================================
# Summary Repository
# ============================================================


class SummaryRepository:
    """摘要操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def insert(
        self,
        guild_id: str,
        channel_id: str,
        summary: str,
        message_count: int,
        start_time: str,
        end_time: str,
    ) -> int:
        """新增摘要。"""
        cursor = await self._db.execute(
            """INSERT INTO summaries
               (guild_id, channel_id, summary, message_count, start_time, end_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, summary, message_count, start_time, end_time),
        )
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_latest(
        self, guild_id: str, channel_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """取得頻道最近 N 筆摘要。"""
        return await self._db.fetchall(
            """SELECT * FROM summaries
               WHERE guild_id = ? AND channel_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, channel_id, limit),
        )


# ============================================================
# Memory Repository
# ============================================================


class MemoryRepository:
    """長期記憶操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def upsert(
        self,
        guild_id: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 1.0,
    ) -> None:
        """新增或更新記憶。"""
        await self._db.execute(
            """INSERT INTO memory (guild_id, category, key, value, confidence, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, category, key)
               DO UPDATE SET value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at""",
            (guild_id, category, key, value, confidence, _now_iso()),
        )

    async def get(
        self, guild_id: str, category: str, key: str
    ) -> dict[str, Any] | None:
        """取得單筆記憶。"""
        return await self._db.fetchone(
            "SELECT * FROM memory WHERE guild_id=? AND category=? AND key=?",
            (guild_id, category, key),
        )

    async def get_by_guild(
        self, guild_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """取得伺服器所有記憶（可依分類篩選）。"""
        if category:
            return await self._db.fetchall(
                "SELECT * FROM memory WHERE guild_id=? AND category=?",
                (guild_id, category),
            )
        return await self._db.fetchall(
            "SELECT * FROM memory WHERE guild_id=?", (guild_id,)
        )

    async def delete(self, guild_id: str, category: str, key: str) -> bool:
        """刪除一筆記憶。

        Returns:
            是否成功刪除（True = 有刪到東西）。
        """
        cursor = await self._db.execute(
            "DELETE FROM memory WHERE guild_id=? AND category=? AND key=?",
            (guild_id, category, key),
        )
        return cursor.rowcount > 0  # type: ignore[return-value]

    async def search(
        self, guild_id: str, keyword: str, category: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """關鍵字搜尋記憶（模糊比對 key 或 value）。"""
        pattern = f"%{keyword}%"
        if category:
            return await self._db.fetchall(
                """SELECT * FROM memory
                   WHERE guild_id=? AND category=?
                     AND (key LIKE ? OR value LIKE ?)
                   ORDER BY confidence DESC LIMIT ?""",
                (guild_id, category, pattern, pattern, limit),
            )
        return await self._db.fetchall(
            """SELECT * FROM memory
               WHERE guild_id=?
                 AND (key LIKE ? OR value LIKE ?)
               ORDER BY confidence DESC LIMIT ?""",
            (guild_id, pattern, pattern, limit),
        )


# ============================================================
# Action Log Repository
# ============================================================


class ActionLogRepository:
    """操作日誌操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def insert(
        self,
        guild_id: str,
        agent_name: str,
        reason: str,
        action: str,
        tool_name: str,
        parameters: dict[str, Any],
        safety_level: str = "SAFE",
        status: str = "pending",
    ) -> int:
        """新增操作日誌。"""
        cursor = await self._db.execute(
            """INSERT INTO action_logs
               (guild_id, agent_name, reason, action, tool_name, parameters, safety_level, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, agent_name, reason, action, tool_name, json.dumps(parameters), safety_level, status),
        )
        return cursor.lastrowid  # type: ignore[return-value]

    async def update_status(self, log_id: int, status: str, result: str = "") -> None:
        """更新日誌狀態。"""
        await self._db.execute(
            "UPDATE action_logs SET status=?, result=? WHERE id=?",
            (status, result, log_id),
        )

    async def get_recent(
        self, guild_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """取得最近的操作日誌。"""
        return await self._db.fetchall(
            "SELECT * FROM action_logs WHERE guild_id=? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        )

    async def get_pending(self, guild_id: str) -> list[dict[str, Any]]:
        """取得待審核的操作。"""
        return await self._db.fetchall(
            "SELECT * FROM action_logs WHERE guild_id=? AND status='pending' ORDER BY created_at ASC",
            (guild_id,),
        )


# ============================================================
# Task Repository
# ============================================================


class TaskRepository:
    """任務佇列操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def insert(
        self,
        guild_id: str,
        agent_name: str,
        task_type: str,
        priority: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """新增任務。"""
        cursor = await self._db.execute(
            """INSERT INTO tasks (guild_id, agent_name, task_type, priority, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, agent_name, task_type, priority, json.dumps(payload or {})),
        )
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_next(self, status: str = "queued") -> dict[str, Any] | None:
        """取得下一個待處理任務（依優先級排序）。"""
        return await self._db.fetchone(
            """SELECT * FROM tasks
               WHERE status = ?
               ORDER BY priority DESC, created_at ASC
               LIMIT 1""",
            (status,),
        )

    async def update_status(
        self,
        task_id: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """更新任務狀態。"""
        await self._db.execute(
            """UPDATE tasks SET status=?, error_message=?, updated_at=?
               WHERE id=?""",
            (status, error_message, _now_iso(), task_id),
        )

    async def increment_retry(self, task_id: int, error_message: str) -> None:
        """增加重試次數並標記為 retry。"""
        await self._db.execute(
            """UPDATE tasks SET retry_count = retry_count + 1,
               status = 'retry', error_message = ?, updated_at = ?
               WHERE id = ?""",
            (error_message, _now_iso(), task_id),
        )

    async def get_retryable(self, max_retry: int = 3) -> list[dict[str, Any]]:
        """取得可重試的任務。"""
        return await self._db.fetchall(
            """SELECT * FROM tasks
               WHERE status = 'retry' AND retry_count < ?
               ORDER BY priority DESC, created_at ASC""",
            (max_retry,),
        )

    async def list_by_status(
        self, status: str, guild_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """依狀態列出任務。"""
        if guild_id:
            return await self._db.fetchall(
                """SELECT * FROM tasks
                   WHERE status=? AND guild_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (status, guild_id, limit),
            )
        return await self._db.fetchall(
            "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )

    async def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """取得最近的任務。"""
        return await self._db.fetchall(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def count_by_status(self, status: str) -> int:
        """計算指定狀態的任務數。"""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS count FROM tasks WHERE status=?",
            (status,),
        )
        return int(row["count"] if row else 0)


# ============================================================
# Tool Call Repository
# ============================================================


class ToolCallRepository:
    """Tool 呼叫紀錄操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def insert(
        self,
        guild_id: str,
        agent_name: str,
        tool_name: str,
        parameters: dict[str, Any],
        reasoning: str = "",
        expected_result: str = "",
        safety_level: str = "SAFE",
        task_id: int | None = None,
    ) -> int:
        """新增 Tool 呼叫紀錄。"""
        cursor = await self._db.execute(
            """INSERT INTO tool_calls
               (task_id, guild_id, agent_name, tool_name, parameters,
                reasoning, expected_result, safety_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, guild_id, agent_name, tool_name, json.dumps(parameters),
             reasoning, expected_result, safety_level),
        )
        return cursor.lastrowid  # type: ignore[return-value]

    async def update_result(self, call_id: int, actual_result: str) -> None:
        """更新 Tool 執行結果。"""
        await self._db.execute(
            "UPDATE tool_calls SET actual_result=?, executed_at=? WHERE id=?",
            (actual_result, _now_iso(), call_id),
        )

    async def get_recent(
        self, guild_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """取得最近的 Tool 呼叫紀錄。"""
        return await self._db.fetchall(
            """SELECT * FROM tool_calls
               WHERE guild_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, limit),
        )


# ============================================================
# Image Analysis Repository
# ============================================================


class ImageAnalysisRepository:
    """圖片分析結果操作。"""

    def __init__(self, provider: ConnectionProvider) -> None:
        self._db = provider

    async def insert(
        self,
        guild_id: str,
        channel_id: str,
        message_id: str,
        image_url: str,
        description: str,
        model: str = "",
        filename: str = "",
        agent_name: str = "",
    ) -> int:
        """新增（或忽略重複的）圖片分析結果。"""
        cursor = await self._db.execute(
            """INSERT OR IGNORE INTO image_analysis
               (guild_id, channel_id, message_id, image_url, filename, description, model, agent_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, message_id, image_url, filename, description, model, agent_name),
        )
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_by_message(self, message_id: str) -> list[dict[str, Any]]:
        """取得某訊息的所有圖片分析結果。"""
        return await self._db.fetchall(
            "SELECT * FROM image_analysis WHERE message_id = ? ORDER BY created_at",
            (message_id,),
        )

    async def get_cached(self, message_id: str, image_url: str) -> dict[str, Any] | None:
        """查詢是否已分析過某圖片（避免重複呼叫 vision 模型）。"""
        return await self._db.fetchone(
            "SELECT * FROM image_analysis WHERE message_id = ? AND image_url = ?",
            (message_id, image_url),
        )

    async def get_recent(
        self, guild_id: str, channel_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """取得頻道最近的圖片分析結果。"""
        return await self._db.fetchall(
            """SELECT * FROM image_analysis
               WHERE guild_id = ? AND channel_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, channel_id, limit),
        )
