"""短期記憶 — Message Collector。

以 Agent/Guild 為單位維護一份訂閱狀態。
AI 的主要上下文來自「目前訂閱的頻道」，
其他頻道只保留新訊息數量與提及資訊，避免 token 被多頻道上下文放大。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import structlog

from src.config.settings import MessageConfig
from src.database.repository import MessageRepository

logger = structlog.get_logger(__name__)


# ============================================================
# 訊息資料結構
# ============================================================


@dataclass
class CollectedMessage:
    """收集到的 Discord 訊息。"""

    guild_id: str
    channel_id: str
    message_id: str
    author_id: str
    author_name: str
    content: str
    channel_name: str = ""
    channel_type: str = ""
    category_name: str = ""
    parent_channel_id: str = ""
    parent_channel_name: str = ""
    is_bot: bool = False
    is_mention: bool = False
    is_reply: bool = False
    is_slash_command: bool = False
    timestamp: str = ""
    agent_name: str = ""  # 指定由哪位 Agent 處理（空字串表示交給預設派發）
    reference_message_id: str = ""  # 回覆對象的訊息 ID（空字串表示非回覆）
    jump_url: str = ""
    reaction_counts: dict[str, int] = field(default_factory=dict)

    @property
    def lineage_text(self) -> str:
        """回傳頻道層級描述。"""
        parts: list[str] = []
        if self.category_name:
            parts.append(self.category_name)
        if self.parent_channel_name:
            parts.append(self.parent_channel_name)
        if self.channel_name:
            parts.append(self.channel_name)
        return " / ".join(parts) if parts else self.channel_id


@dataclass
class ChannelActivity:
    """其他頻道的未讀活動摘要。"""

    channel_id: str
    channel_name: str = ""
    channel_type: str = ""
    category_name: str = ""
    parent_channel_id: str = ""
    parent_channel_name: str = ""
    unread_count: int = 0
    mentioned: bool = False
    message_ids: list[str] = field(default_factory=list)

    @property
    def lineage_text(self) -> str:
        parts: list[str] = []
        if self.category_name:
            parts.append(self.category_name)
        if self.parent_channel_name:
            parts.append(self.parent_channel_name)
        if self.channel_name:
            parts.append(self.channel_name)
        return " / ".join(parts) if parts else self.channel_id


@dataclass
class MessageBatch:
    """一批待處理的訊息。"""

    batch_id: str = ""
    guild_id: str = ""
    channel_id: str = ""  # 目前訂閱的頻道（上下文來源）
    messages: list[CollectedMessage] = field(default_factory=list)
    attention_messages: list[CollectedMessage] = field(default_factory=list)
    other_channels: list[ChannelActivity] = field(default_factory=list)
    other_channels_new_count: int = 0
    is_priority: bool = False  # Mention 等高優先級
    agent_name: str = ""  # 指定由哪位 Agent 處理（空字串表示交給預設派發）
    reference_message_id: str = ""  # 回覆對象的訊息 ID（取自最後一則 mention/reply 訊息）
    reply_channel_id: str = ""
    reply_channel_name: str = ""
    subscribed_channel_name: str = ""
    trigger_reason: str = ""

    def __post_init__(self) -> None:
        if not self.batch_id:
            self.batch_id = uuid.uuid4().hex[:12]
        if not self.reply_channel_id:
            self.reply_channel_id = self.channel_id
        if not self.reply_channel_name:
            self.reply_channel_name = self.subscribed_channel_name


# ============================================================
# Trigger Callback 類型
# ============================================================

# 當批次觸發時呼叫的回呼函數
TriggerCallback = Callable[[MessageBatch], Coroutine[Any, Any, None]]


@dataclass
class _SubscriptionState:
    """單一 Agent 在單一 Guild 的訂閱狀態。"""

    guild_id: str
    agent_name: str
    subscribed_channel_id: str = ""
    subscribed_channel_name: str = ""
    subscribed_messages: list[CollectedMessage] = field(default_factory=list)
    other_channels: dict[str, ChannelActivity] = field(default_factory=dict)
    last_other_channel_trigger_at: float = 0.0


# ============================================================
# Message Collector
# ============================================================


class MessageCollector:
    """訊息收集器。

    收集 Discord 頻道中的訊息，達到 batch_size 時自動觸發 AI 處理。
    Mention / Reply / Slash Command 等事件則立即建立高優先級批次。
    """

    def __init__(
        self,
        config: MessageConfig,
        message_repo: MessageRepository,
    ) -> None:
        """初始化。

        Args:
            config: 訊息設定（batch_size / mention_priority）。
            message_repo: 訊息資料庫 Repository。
        """
        self._config = config
        self._repo = message_repo
        # 每個 Agent/Guild 一份訂閱狀態：{guild:agent -> state}
        self._states: dict[str, _SubscriptionState] = {}
        # 鎖，避免併發問題
        self._lock = asyncio.Lock()
        # 觸發回呼
        self._callbacks: list[TriggerCallback] = []

    def _state_key(self, guild_id: str, agent_name: str) -> str:
        """生成 Agent/Guild 的唯一鍵。"""
        return f"{guild_id}:{agent_name}"

    def _get_state(self, guild_id: str, agent_name: str) -> _SubscriptionState:
        key = self._state_key(guild_id, agent_name)
        state = self._states.get(key)
        if state is None:
            state = _SubscriptionState(guild_id=guild_id, agent_name=agent_name)
            self._states[key] = state
        return state

    def set_subscription(
        self,
        guild_id: str,
        agent_name: str,
        channel_id: str,
        channel_name: str = "",
    ) -> dict[str, str]:
        """設定 Agent 在指定 Guild 的訂閱頻道。"""
        state = self._get_state(guild_id, agent_name)
        previous_channel_id = state.subscribed_channel_id
        previous_channel_name = state.subscribed_channel_name

        if previous_channel_id == channel_id:
            if channel_name and not state.subscribed_channel_name:
                state.subscribed_channel_name = channel_name
            return {
                "previous_channel_id": previous_channel_id,
                "previous_channel_name": previous_channel_name,
                "channel_id": state.subscribed_channel_id,
                "channel_name": state.subscribed_channel_name,
            }

        if previous_channel_id and state.subscribed_messages:
            activity = state.other_channels.get(previous_channel_id)
            if activity is None:
                activity = ChannelActivity(
                    channel_id=previous_channel_id,
                    channel_name=previous_channel_name,
                )
                state.other_channels[previous_channel_id] = activity
            activity.unread_count += len(state.subscribed_messages)
            activity.message_ids.extend(msg.message_id for msg in state.subscribed_messages)
            state.subscribed_messages.clear()

        state.other_channels.pop(channel_id, None)
        state.subscribed_channel_id = channel_id
        state.subscribed_channel_name = channel_name
        return {
            "previous_channel_id": previous_channel_id,
            "previous_channel_name": previous_channel_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
        }

    def get_subscription(self, guild_id: str, agent_name: str) -> str:
        """取得 Agent 目前的訂閱頻道 ID。"""
        return self._get_state(guild_id, agent_name).subscribed_channel_id

    def _build_activity(self, message: CollectedMessage) -> ChannelActivity:
        return ChannelActivity(
            channel_id=message.channel_id,
            channel_name=message.channel_name,
            channel_type=message.channel_type,
            category_name=message.category_name,
            parent_channel_id=message.parent_channel_id,
            parent_channel_name=message.parent_channel_name,
        )

    def _other_channel_trigger_available(self, state: _SubscriptionState) -> bool:
        """判斷其他頻道門檻喚醒是否已過 cooldown。"""
        cooldown = max(0, self._config.other_channel_trigger_cooldown_seconds)
        if cooldown == 0:
            return True
        return (time.monotonic() - state.last_other_channel_trigger_at) >= cooldown

    def _snapshot_other_channels(self, state: _SubscriptionState) -> list[ChannelActivity]:
        activities = sorted(
            state.other_channels.values(),
            key=lambda item: (not item.mentioned, -item.unread_count, item.channel_name),
        )
        limit = max(1, self._config.max_other_channel_summaries)
        return [
            ChannelActivity(
                channel_id=item.channel_id,
                channel_name=item.channel_name,
                channel_type=item.channel_type,
                category_name=item.category_name,
                parent_channel_id=item.parent_channel_id,
                parent_channel_name=item.parent_channel_name,
                unread_count=item.unread_count,
                mentioned=item.mentioned,
                message_ids=list(item.message_ids),
            )
            for item in activities[:limit]
        ]

    def _collect_message_ids(self, batch: MessageBatch) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for msg in [*batch.messages, *batch.attention_messages]:
            if msg.message_id and msg.message_id not in seen:
                seen.add(msg.message_id)
                ids.append(msg.message_id)
        for activity in batch.other_channels:
            for message_id in activity.message_ids:
                if message_id and message_id not in seen:
                    seen.add(message_id)
                    ids.append(message_id)
        return ids

    async def _emit_batch(
        self,
        state: _SubscriptionState,
        *,
        messages: list[CollectedMessage],
        attention_messages: list[CollectedMessage],
        is_priority: bool,
        trigger_reason: str,
        reply_channel_id: str,
        reply_channel_name: str,
        reference_message_id: str,
    ) -> MessageBatch:
        other_channels = self._snapshot_other_channels(state)
        batch = MessageBatch(
            guild_id=state.guild_id,
            channel_id=state.subscribed_channel_id or reply_channel_id,
            messages=list(messages),
            attention_messages=list(attention_messages),
            other_channels=other_channels,
            other_channels_new_count=sum(item.unread_count for item in other_channels),
            is_priority=is_priority,
            agent_name=state.agent_name,
            reference_message_id=reference_message_id,
            reply_channel_id=reply_channel_id or state.subscribed_channel_id,
            reply_channel_name=reply_channel_name,
            subscribed_channel_name=state.subscribed_channel_name,
            trigger_reason=trigger_reason,
        )
        if trigger_reason == "other_channels_threshold":
            state.last_other_channel_trigger_at = time.monotonic()
        await self._repo.mark_batched(self._collect_message_ids(batch), batch.batch_id)
        state.subscribed_messages.clear()
        state.other_channels.clear()
        await self._notify_callbacks(batch)
        return batch

    def on_trigger(self, callback: TriggerCallback) -> None:
        """註冊觸發回呼。"""
        self._callbacks.append(callback)

    async def _notify_callbacks(self, batch: MessageBatch) -> None:
        """通知所有回呼。"""
        for cb in self._callbacks:
            try:
                await cb(batch)
            except Exception as exc:
                logger.error("collector.callback_error", error=str(exc))

    # ---- 訊息收集 ----

    async def collect(self, message: CollectedMessage) -> MessageBatch | None:
        """收集一則訊息。

        訂閱頻道的訊息會累積到 batch_size 後送出。
        非訂閱頻道只累積未讀數量；若被提及則立即觸發，
        或在訂閱頻道安靜但其他頻道新訊息超過閾值時觸發。

        Args:
            message: 收集到的訊息。

        Returns:
            若觸發了批次則回傳 MessageBatch，否則 None。
        """
        # 寫入資料庫
        inserted = await self._repo.insert(
            guild_id=message.guild_id,
            channel_id=message.channel_id,
            message_id=message.message_id,
            author_id=message.author_id,
            author_name=message.author_name,
            content=message.content,
            is_bot=message.is_bot,
        )
        if inserted == 0:
            logger.info(
                "collector.duplicate_message",
                guild=message.guild_id,
                channel=message.channel_id,
                message_id=message.message_id,
            )
            return None

        state = self._get_state(message.guild_id, message.agent_name)

        if not state.subscribed_channel_id:
            state.subscribed_channel_id = message.channel_id
            state.subscribed_channel_name = message.channel_name

        # 高優先級：立即觸發
        is_priority = message.is_mention or message.is_reply or message.is_slash_command
        async with self._lock:
            if message.channel_id == state.subscribed_channel_id:
                state.subscribed_messages.append(message)

                if is_priority and self._config.mention_priority:
                    logger.info(
                        "collector.priority_trigger",
                        guild=message.guild_id,
                        channel=message.channel_id,
                        trigger="mention/reply/slash",
                    )
                    return await self._emit_batch(
                        state,
                        messages=list(state.subscribed_messages),
                        attention_messages=[],
                        is_priority=True,
                        trigger_reason="subscribed_channel_priority",
                        reply_channel_id=message.channel_id,
                        reply_channel_name=message.channel_name,
                        reference_message_id=message.reference_message_id,
                    )

                if len(state.subscribed_messages) >= self._config.batch_size:
                    pending = list(state.subscribed_messages)
                    logger.info(
                        "collector.batch_trigger",
                        guild=message.guild_id,
                        channel=message.channel_id,
                        count=len(pending),
                    )
                    return await self._emit_batch(
                        state,
                        messages=pending,
                        attention_messages=[],
                        is_priority=False,
                        trigger_reason="subscribed_channel_batch",
                        reply_channel_id=state.subscribed_channel_id,
                        reply_channel_name=state.subscribed_channel_name,
                        reference_message_id="",
                    )

                return None

            activity = state.other_channels.get(message.channel_id)
            if activity is None:
                activity = self._build_activity(message)
                state.other_channels[message.channel_id] = activity
            activity.unread_count += 1
            activity.mentioned = activity.mentioned or is_priority
            activity.message_ids.append(message.message_id)

            if is_priority and self._config.mention_priority:
                logger.info(
                    "collector.priority_trigger",
                    guild=message.guild_id,
                    channel=message.channel_id,
                    trigger="cross_channel_mention",
                )
                return await self._emit_batch(
                    state,
                    messages=list(state.subscribed_messages),
                    attention_messages=[message],
                    is_priority=True,
                    trigger_reason="cross_channel_priority",
                    reply_channel_id=message.channel_id,
                    reply_channel_name=message.channel_name,
                    reference_message_id=message.reference_message_id,
                )

            other_count = sum(item.unread_count for item in state.other_channels.values())
            if not state.subscribed_messages and other_count >= self._config.other_channel_trigger_count:
                if not self._other_channel_trigger_available(state):
                    logger.info(
                        "collector.cross_channel_threshold_suppressed",
                        guild=message.guild_id,
                        subscribed_channel=state.subscribed_channel_id,
                        unread_count=other_count,
                        cooldown_seconds=self._config.other_channel_trigger_cooldown_seconds,
                    )
                    return None
                logger.info(
                    "collector.cross_channel_threshold_trigger",
                    guild=message.guild_id,
                    subscribed_channel=state.subscribed_channel_id,
                    unread_count=other_count,
                )
                return await self._emit_batch(
                    state,
                    messages=[],
                    attention_messages=[],
                    is_priority=False,
                    trigger_reason="other_channels_threshold",
                    reply_channel_id=state.subscribed_channel_id,
                    reply_channel_name=state.subscribed_channel_name,
                    reference_message_id="",
                )

        return None

    async def force_flush(self, guild_id: str, channel_id: str) -> MessageBatch | None:
        """強制將某個訂閱頻道的暫存狀態排出。

        用於系統關閉或特殊情況。

        Args:
            guild_id: 伺服器 ID。
            channel_id: 頻道 ID。

        Returns:
            若有暫存訊息則回傳批次，否則 None。
        """
        async with self._lock:
            for state in self._states.values():
                if state.guild_id != guild_id or state.subscribed_channel_id != channel_id:
                    continue
                if not state.subscribed_messages and not state.other_channels:
                    continue
                logger.info(
                    "collector.force_flush",
                    guild=guild_id,
                    channel=channel_id,
                    count=len(state.subscribed_messages),
                )
                return await self._emit_batch(
                    state,
                    messages=list(state.subscribed_messages),
                    attention_messages=[],
                    is_priority=False,
                    trigger_reason="force_flush",
                    reply_channel_id=state.subscribed_channel_id,
                    reply_channel_name=state.subscribed_channel_name,
                    reference_message_id="",
                )
        return None

    async def flush_all(self) -> list[MessageBatch]:
        """強制排出所有暫存訊息。"""
        batches: list[MessageBatch] = []
        async with self._lock:
            states = list(self._states.values())
            for state in states:
                if not state.subscribed_messages and not state.other_channels:
                    continue
                batch = await self._emit_batch(
                    state,
                    messages=list(state.subscribed_messages),
                    attention_messages=[],
                    is_priority=False,
                    trigger_reason="flush_all",
                    reply_channel_id=state.subscribed_channel_id,
                    reply_channel_name=state.subscribed_channel_name,
                    reference_message_id="",
                )
                batches.append(batch)
        return batches

    @property
    def pending_counts(self) -> dict[str, int]:
        """各 Agent/Guild 暫存訊息數量（用於狀態查詢）。"""
        return {
            key: len(state.subscribed_messages) + sum(
                activity.unread_count for activity in state.other_channels.values()
            )
            for key, state in self._states.items()
        }
