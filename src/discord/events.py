"""Discord 事件處理器。

負責接收 Discord 事件並分派到對應的處理邏輯。
包含訊息收集、Mention 偵測、Override 指令攔截。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Coroutine

import discord

import structlog

from src.config.settings import OverrideConfig, CouncilConfig
from src.memory.short_term import CollectedMessage, MessageCollector
from src.queue.task_queue import TaskQueue, Task, TaskPriority

logger = structlog.get_logger(__name__)

# Type alias for override callback
OverrideCallback = Callable[[str, str, str], Coroutine[Any, Any, None]]


class EventHandler:
    """Discord 事件處理器。

    負責：
    - on_message: 收集訊息、偵測 Mention/Reply、攔截 Override 指令
    - on_ready: 記錄啟動資訊、更新伺服器記憶
    - on_member_join/leave: 通知相關 Agent
    """

    def __init__(
        self,
        agent_name: str,
        bot: discord.Client,
        message_collector: MessageCollector,
        task_queue: TaskQueue,
        override_config: OverrideConfig,
        council_config: CouncilConfig,
        override_callback: OverrideCallback | None = None,
    ) -> None:
        """初始化。

        Args:
            agent_name: Agent 名稱。
            bot: Discord Bot 實例。
            message_collector: 訊息收集器。
            task_queue: 任務佇列。
            override_config: Override 設定。
            council_config: Council 設定。
            override_callback: Override 回呼（action, guild_id, user_id）。
        """
        self._agent_name = agent_name
        self._bot = bot
        self._collector = message_collector
        self._queue = task_queue
        self._override = override_config
        self._council = council_config
        self._override_cb = override_callback

        self._log = logger.bind(agent=agent_name)

    def register(self) -> None:
        """註冊所有事件處理器到 Bot。"""
        self._bot.event(self._on_ready)
        self._bot.event(self._on_message)
        self._bot.event(self._on_member_join)
        self._bot.event(self._on_member_remove)
        self._bot.event(self._on_guild_join)

    # ---- on_ready ----

    async def _on_ready(self) -> None:
        """Bot 連線完成。"""
        self._log.info(
            "discord.ready",
            user=str(self._bot.user),
            guilds=len(self._bot.guilds),
        )

    # ---- on_message ----

    async def _on_message(self, message: discord.Message) -> None:
        """處理訊息事件。

        流程：
        1. 過濾（自己、DM、其他 Bot 可選）
        2. 偵測 Mention / Reply
        3. 攔截 Override 指令
        4. 偵測 Council 頻道
        5. 收集訊息
        """
        # 過濾
        if self._should_ignore(message):
            return

        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        content = message.content.strip()

        # 偵測 Mention / Reply
        is_mention = self._is_mentioned(message)
        is_reply = self._is_reply_to_bot(message)

        # 攔截 Override 指令
        if self._override.enabled and self._is_override_command(content):
            await self._handle_override(content, guild_id, str(message.author.id))
            return

        # Council 頻道偵測
        if self._is_council_channel(message):
            await self._handle_council_message(message, guild_id)
            return

        # 收集訊息
        collected = CollectedMessage(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=str(message.id),
            author_id=str(message.author.id),
            author_name=message.author.display_name,
            content=content,
            is_bot=message.author.bot,
            is_mention=is_mention,
            is_reply=is_reply,
            is_slash_command=content.startswith("/"),
        )

        await self._collector.collect(collected)

    def _should_ignore(self, message: discord.Message) -> bool:
        """是否應忽略此訊息。"""
        # 自己的訊息
        if message.author == self._bot.user:
            return True
        # DM
        if message.guild is None:
            return True
        return False

    def _is_mentioned(self, message: discord.Message) -> bool:
        """是否被 Mention。"""
        if self._bot.user is None:
            return False
        return self._bot.user.mentioned_in(message)

    def _is_reply_to_bot(self, message: discord.Message) -> bool:
        """是否回覆 Bot 的訊息。"""
        if message.reference is None:
            return False
        # 只檢查 reference 是否存在，不嘗試解析 resolved（可能未快取）
        return True  # simplified: treat all replies as potential bot replies

    def _is_override_command(self, content: str) -> bool:
        """是否為 Override 指令。"""
        return content in (
            self._override.approve_command,
            self._override.deny_command,
            self._override.stop_command,
        )

    async def _handle_override(self, content: str, guild_id: str, user_id: str) -> None:
        """處理 Override 指令。"""
        action = "unknown"
        if content == self._override.approve_command:
            action = "approve"
        elif content == self._override.deny_command:
            action = "deny"
        elif content == self._override.stop_command:
            action = "stop"

        self._log.info(
            "discord.override",
            action=action,
            guild=guild_id,
            user=user_id,
        )

        if self._override_cb:
            await self._override_cb(action, guild_id, user_id)

    def _is_council_channel(self, message: discord.Message) -> bool:
        """是否為 Council 頻道。"""
        if not self._council.enabled:
            return False
        if isinstance(message.channel, discord.TextChannel):
            return message.channel.name == "ai-council"
        return False

    async def _handle_council_message(self, message: discord.Message, guild_id: str) -> None:
        """處理 Council 頻道訊息。"""
        self._log.info(
            "discord.council_message",
            author=message.author.display_name,
            content=message.content[:100],
        )

        # 將 Council 訊息加入佇列供所有 Agent 處理
        task = Task(
            guild_id=guild_id,
            agent_name=self._agent_name,
            task_type="council",
            priority=TaskPriority.ADMIN,
            payload={
                "content": message.content,
                "channel_id": str(message.channel.id),
                "author": message.author.display_name,
            },
        )

        await self._queue.enqueue(task)

    # ---- on_member_join ----

    async def _on_member_join(self, member: discord.Member) -> None:
        """成員加入伺服器。"""
        self._log.info(
            "discord.member_joined",
            member=member.display_name,
            guild=str(member.guild.id),
        )

    # ---- on_member_remove ----

    async def _on_member_remove(self, member: discord.Member) -> None:
        """成員離開伺服器。"""
        self._log.info(
            "discord.member_removed",
            member=member.display_name,
            guild=str(member.guild.id),
        )

    # ---- on_guild_join ----

    async def _on_guild_join(self, guild: discord.Guild) -> None:
        """Bot 加入新伺服器。"""
        self._log.info(
            "discord.guild_joined",
            guild=guild.name,
            members=guild.member_count,
        )
