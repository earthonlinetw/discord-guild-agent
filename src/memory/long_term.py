"""長期記憶服務。

記錄並提供伺服器的持久化知識：名稱、頻道用途、規則、使用者偏好、歷史決策。
AI 每次推理前皆可讀取相關長期記憶。
"""

from __future__ import annotations

from typing import Any

import structlog

from src.database.repository import MemoryRepository

logger = structlog.get_logger(__name__)


# ============================================================
# 記憶分類常數
# ============================================================


class MemoryCategory:
    """長期記憶的分類常數。"""

    SERVER_INFO = "server_info"
    CHANNEL_PURPOSE = "channel_purpose"
    RULES = "rules"
    USER_PREFERENCE = "user_preference"
    DECISION = "decision"
    AGENT_KNOWLEDGE = "agent_knowledge"


# ============================================================
# 長期記憶服務
# ============================================================


class LongTermMemoryService:
    """長期記憶管理服務。

    提供記憶的 CRUD 以及查詢功能。
    所有資料持久化至資料庫，跨 Agent 共享。
    """

    def __init__(self, repo: MemoryRepository) -> None:
        """初始化。

        Args:
            repo: Memory Repository。
        """
        self._repo = repo

    async def store(
        self,
        guild_id: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 1.0,
    ) -> None:
        """儲存一筆長期記憶。

        Args:
            guild_id: 伺服器 ID。
            category: 記憶分類。
            key: 記憶鍵。
            value: 記憶值。
            confidence: 信心水準 (0.0 ~ 1.0)。
        """
        await self._repo.upsert(
            guild_id=guild_id,
            category=category,
            key=key,
            value=value,
            confidence=confidence,
        )
        logger.debug(
            "memory.stored",
            guild=guild_id,
            category=category,
            key=key,
        )

    async def retrieve(
        self, guild_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """查詢長期記憶。

        Args:
            guild_id: 伺服器 ID。
            category: 可選的分類篩選。

        Returns:
            記憶清單。
        """
        return await self._repo.get_by_guild(guild_id, category)

    async def get_server_info(self, guild_id: str) -> dict[str, Any] | None:
        """取得伺服器資訊。"""
        return await self._repo.get(guild_id, MemoryCategory.SERVER_INFO, "main")

    async def store_server_info(
        self, guild_id: str, server_name: str, member_count: int, **extra: Any
    ) -> None:
        """儲存伺服器資訊。"""
        import json
        data = {"name": server_name, "member_count": member_count, **extra}
        await self.store(
            guild_id=guild_id,
            category=MemoryCategory.SERVER_INFO,
            key="main",
            value=json.dumps(data, ensure_ascii=False),
        )

    async def store_channel_purpose(
        self, guild_id: str, channel_id: str, purpose: str
    ) -> None:
        """儲存頻道用途。"""
        await self.store(
            guild_id=guild_id,
            category=MemoryCategory.CHANNEL_PURPOSE,
            key=channel_id,
            value=purpose,
        )

    async def store_rule(self, guild_id: str, rule: str, index: int = 0) -> None:
        """儲存規則。"""
        await self.store(
            guild_id=guild_id,
            category=MemoryCategory.RULES,
            key=f"rule_{index}",
            value=rule,
        )

    async def store_user_preference(
        self, guild_id: str, user_id: str, preference: str
    ) -> None:
        """儲存使用者偏好。"""
        await self.store(
            guild_id=guild_id,
            category=MemoryCategory.USER_PREFERENCE,
            key=user_id,
            value=preference,
        )

    async def store_decision(
        self, guild_id: str, decision: str, agent_name: str = ""
    ) -> None:
        """儲存歷史決策。"""
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat()
        await self.store(
            guild_id=guild_id,
            category=MemoryCategory.DECISION,
            key=f"{agent_name}_{timestamp}",
            value=decision,
        )

    async def get_context_for_ai(self, guild_id: str) -> list[dict[str, Any]]:
        """取得 AI 推理所需的長期記憶。

        優先回傳高 confidence 的記憶。

        Args:
            guild_id: 伺服器 ID。

        Returns:
            記憶清單，適合直接注入 Context。
        """
        all_memory = await self.retrieve(guild_id)
        # 依 confidence 排序
        all_memory.sort(key=lambda m: m.get("confidence", 1.0), reverse=True)
        return all_memory

    async def delete_memory(self, guild_id: str, category: str, key: str) -> bool:
        """刪除一筆長期記憶。

        Args:
            guild_id: 伺服器 ID。
            category: 記憶分類。
            key: 記憶鍵。

        Returns:
            是否成功刪除。
        """
        result = await self._repo.delete(guild_id, category, key)
        if result:
            logger.info("memory.deleted", guild=guild_id, category=category, key=key)
        else:
            logger.warning("memory.delete_not_found", guild=guild_id, category=category, key=key)
        return result

    async def search_memory(
        self, guild_id: str, keyword: str, category: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """關鍵字搜尋長期記憶。

        Args:
            guild_id: 伺服器 ID。
            keyword: 搜尋關鍵字（模糊比對 key 或 value）。
            category: 可選的分類篩選。
            limit: 最多回傳筆數。

        Returns:
            符合的記憶清單。
        """
        results = await self._repo.search(guild_id, keyword, category, limit)
        logger.debug(
            "memory.searched",
            guild=guild_id,
            keyword=keyword,
            results=len(results),
        )
        return results
