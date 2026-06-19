"""摘要服務。

將舊訊息壓縮為摘要，儲存至資料庫，未來對話時載入。
"""

from __future__ import annotations

from typing import Any

import structlog

from src.ai.provider import AIProvider
from src.database.repository import MessageRepository, SummaryRepository

logger = structlog.get_logger(__name__)


class SummaryService:
    """摘要服務。

    當 Context 接近 Token 上限時，將舊訊息壓縮為摘要。
    使用 AI 產生摘要，並持久化至資料庫。
    """

    def __init__(
        self,
        ai_provider: AIProvider,
        message_repo: MessageRepository,
        summary_repo: SummaryRepository,
    ) -> None:
        """初始化。

        Args:
            ai_provider: AI Provider（用於產生摘要）。
            message_repo: 訊息 Repository。
            summary_repo: 摘要 Repository。
        """
        self._ai = ai_provider
        self._msg_repo = message_repo
        self._summary_repo = summary_repo

    async def summarize_channel(
        self,
        guild_id: str,
        channel_id: str,
        agent_name: str = "",
    ) -> str | None:
        """對頻道舊訊息產生摘要。

        流程：
        1. 從 DB 取得最近訊息。
        2. 將訊息格式化為文字。
        3. 呼叫 AI 產生摘要。
        4. 儲存摘要至 DB。
        5. 刪除已摘要的舊訊息。

        Args:
            guild_id: 伺服器 ID。
            channel_id: 頻道 ID。
            agent_name: 執行的 Agent 名稱。

        Returns:
            摘要文字，若無訊息可摘要則 None。
        """
        # 取得舊訊息
        messages = await self._msg_repo.get_recent(guild_id, channel_id, limit=50)
        if not messages:
            logger.debug("summary.no_messages", guild=guild_id, channel=channel_id)
            return None

        # 格式化訊息
        formatted = self._format_messages(messages)

        # 呼叫 AI 摘要
        logger.info(
            "summary.generating",
            guild=guild_id,
            channel=channel_id,
            message_count=len(messages),
        )
        try:
            summary_text = await self._ai.summarize(formatted, agent_name=agent_name)
        except Exception as exc:
            logger.error("summary.ai_failed", error=str(exc))
            return None

        if not summary_text:
            logger.warning("summary.empty_result")
            return None

        # 儲存摘要
        start_time = messages[-1].get("created_at", "")  # 最舊
        end_time = messages[0].get("created_at", "")      # 最新（DESC）
        await self._summary_repo.insert(
            guild_id=guild_id,
            channel_id=channel_id,
            summary=summary_text,
            message_count=len(messages),
            start_time=start_time,
            end_time=end_time,
        )

        # 刪除已摘要的舊訊息
        if start_time:
            deleted = await self._msg_repo.delete_old(guild_id, channel_id, start_time)
            logger.info(
                "summary.completed",
                guild=guild_id,
                channel=channel_id,
                deleted_messages=deleted,
            )

        return summary_text

    async def get_summaries(
        self, guild_id: str, channel_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """取得頻道的歷史摘要。"""
        return await self._summary_repo.get_latest(guild_id, channel_id, limit)

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        """將訊息格式化為文字供 AI 摘要。"""
        lines: list[str] = []
        for msg in reversed(messages):  # 按時間正序
            author = msg.get("author_name", "Unknown")
            content = msg.get("content", "")
            is_bot = msg.get("is_bot", 0)
            prefix = "[Bot] " if is_bot else ""
            lines.append(f"{prefix}{author}: {content}")
        return "\n".join(lines)
