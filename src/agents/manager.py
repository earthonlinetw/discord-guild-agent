"""Agent 管理器。

管理多個 Agent 實例的生命週期。
啟動時根據設定檔自動建立所有 Bot 實例。
所有 Agent 共享後端系統。
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord

import structlog

from src.config.settings import AppConfig, AgentConfig
from src.ai.provider import AIProvider
from src.ai.context import ContextManager
from src.memory.short_term import MessageCollector, MessageBatch, CollectedMessage
from src.memory.long_term import LongTermMemoryService
from src.memory.summary import SummaryService
from src.queue.task_queue import TaskQueue, Task, TaskPriority
from src.tools.registry import ToolRegistry
from src.tools.discord_tools import DiscordToolCollection
from src.agents.agent import Agent
from src.database.connection import DatabaseConnection, ConnectionProvider
from src.database.repository import (
    AgentRepository,
    MessageRepository,
    SummaryRepository,
    MemoryRepository,
    ActionLogRepository,
    TaskRepository,
    ToolCallRepository,
    ImageAnalysisRepository,
)

logger = structlog.get_logger(__name__)


class AgentManager:
    """Agent 管理器。

    負責：
    - 根據設定檔建立多個 Agent 實例
    - 管理共享服務（AI / Memory / Queue / Tools）
    - 處理 Discord 事件分派
    - 協調 Agent 間的互動
    """

    def __init__(self, config: AppConfig, db: DatabaseConnection) -> None:
        """初始化。

        Args:
            config: 完整應用設定。
            db: 資料庫連線管理器。
        """
        self._config = config
        self._db = db
        self._provider: ConnectionProvider | None = None

        # 共享服務（延遲初始化）
        self._ai_provider: AIProvider | None = None
        self._tool_registry: ToolRegistry | None = None
        self._task_queue: TaskQueue | None = None
        self._message_collector: MessageCollector | None = None
        self._long_term_memory: LongTermMemoryService | None = None
        self._summary_service: SummaryService | None = None

        # Repositories
        self._agent_repo: AgentRepository | None = None
        self._msg_repo: MessageRepository | None = None
        self._summary_repo: SummaryRepository | None = None
        self._memory_repo: MemoryRepository | None = None
        self._action_log_repo: ActionLogRepository | None = None
        self._task_repo: TaskRepository | None = None
        self._tool_call_repo: ToolCallRepository | None = None
        self._image_repo: ImageAnalysisRepository | None = None

        # Agent 實例
        self._agents: dict[str, Agent] = {}
        self._bots: dict[str, discord.Client] = {}
        self._council: Any = None
        # 主回應 bot（第一隻啟用的 Agent），負責一般聊天訊息
        self._primary_agent_name: str | None = None

    # ---- 初始化 ----

    async def initialize(self) -> None:
        """初始化所有共享服務。"""
        logger.info("agent_manager.initializing")

        # 1. 資料庫
        self._provider = await self._db.connect()

        self._init_repositories()

        # 2. Migration
        from src.database.migration import MigrationManager
        migration = MigrationManager(self._provider)
        await migration.run_pending()

        # 3. AI Provider
        self._ai_provider = AIProvider(self._config.ai)

        # 4. Tool Registry
        self._tool_registry = ToolRegistry()

        # 5. Task Queue
        self._task_queue = TaskQueue(self._config.queue, self._task_repo)

        # 6. Message Collector
        self._message_collector = MessageCollector(
            self._config.message, self._msg_repo
        )
        self._message_collector.on_trigger(self._on_batch_trigger)

        # 7. Long-Term Memory
        self._long_term_memory = LongTermMemoryService(self._memory_repo)

        # 8. Summary Service
        self._summary_service = SummaryService(
            self._ai_provider, self._msg_repo, self._summary_repo
        )

        # 9. 建立 Agent 實例（僅啟用的）
        for agent_config in self._config.agents:
            if not agent_config.enabled:
                logger.info("agent_manager.agent_disabled", name=agent_config.name)
                continue
            await self._create_agent(agent_config)
            # 第一隻啟用的 Agent 作為「主回應 bot」，負責一般聊天訊息
            if self._primary_agent_name is None:
                self._primary_agent_name = agent_config.name

        # 10. 註冊 Queue Handler
        self._task_queue.register_handler("mention", self._handle_mention_task)
        self._task_queue.register_handler("batch", self._handle_batch_task)
        self._task_queue.register_handler("admin", self._handle_admin_task)
        self._task_queue.register_handler("council", self._handle_council_task)
        self._task_queue.register_handler("guild_event", self._handle_guild_event_task)

        logger.info(
            "agent_manager.initialized",
            agents=len(self._agents),
        )

    def _init_repositories(self) -> None:
        """初始化所有 Repository。"""
        p = self._provider
        self._agent_repo = AgentRepository(p)
        self._msg_repo = MessageRepository(p)
        self._summary_repo = SummaryRepository(p)
        self._memory_repo = MemoryRepository(p)
        self._action_log_repo = ActionLogRepository(p)
        self._task_repo = TaskRepository(p)
        self._tool_call_repo = ToolCallRepository(p)
        self._image_repo = ImageAnalysisRepository(p)

    async def _create_agent(self, config: AgentConfig) -> None:
        """建立單一 Agent 實例。"""
        # 建立 Discord Bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        bot = discord.Client(intents=intents)

        # 建立該 Bot 的 Tool 集合（注入 AI provider 與圖片分析 repo，供 analyze_image 使用）
        tool_collection = DiscordToolCollection(
            bot,
            ai_provider=self._ai_provider,
            image_repo=self._image_repo,
            agent_name=config.name,
            memory_service=self._long_term_memory,
            council_provider=lambda: self._council,
            subscription_switcher=lambda guild_id, channel_id, channel_name="": self._set_agent_subscription(
                config.name, guild_id, channel_id, channel_name
            ),
        )
        agent_tool_registry = ToolRegistry()
        for tool in tool_collection.tools:
            agent_tool_registry.register(tool)
            if tool.name not in self._tool_registry.tool_names:
                self._tool_registry.register(tool)

        # 註冊 Tool 到 AI Provider（每個 Agent 都需要完整工具定義）
        from src.ai.provider import ToolDefinition
        for tool in tool_collection.tools:
            self._ai_provider.register_tool(ToolDefinition(
                name=tool.name,
                description=f"[{tool.safety_level.value}] {tool.description}",
                parameters=tool.parameters_schema,
                safety_level=tool.safety_level.value,
            ))

        # 建立 Context Manager
        context = ContextManager(self._config.context)

        # 建立 Agent
        agent = Agent(
            config=config,
            bot=bot,
            ai_provider=self._ai_provider,
            tool_registry=agent_tool_registry,
            task_queue=self._task_queue,
            context_manager=context,
            long_term_memory=self._long_term_memory,
            summary_service=self._summary_service,
            action_log_repo=self._action_log_repo,
            message_repo=self._msg_repo,
            summary_repo=self._summary_repo,
            memory_repo=self._memory_repo,
            tool_call_repo=self._tool_call_repo,
        )

        # 註冊 Discord 事件
        self._register_bot_events(bot, agent)

        # 註冊 Agent 到 DB
        await self._agent_repo.upsert(
            name=config.name,
            personality=config.personality,
            system_prompt=config.system_prompt,
        )

        self._agents[config.name] = agent
        self._bots[config.name] = bot

        logger.info(
            "agent_manager.agent_created",
            name=config.name,
        )

    def _set_agent_subscription(
        self,
        agent_name: str,
        guild_id: str,
        channel_id: str,
        channel_name: str = "",
    ) -> dict[str, str]:
        """切換 Agent 在指定 Guild 的訂閱頻道。"""
        return self._message_collector.set_subscription(
            guild_id=guild_id,
            agent_name=agent_name,
            channel_id=channel_id,
            channel_name=channel_name,
        )

    def _first_visible_channel(
        self,
        bot: discord.Client,
        guild: discord.Guild,
    ) -> discord.TextChannel | None:
        """選擇第一個可見的文字頻道作為預設訂閱。"""
        me = guild.me
        if me is None and bot.user is not None:
            me = guild.get_member(bot.user.id)
        if me is None:
            return None

        for channel in guild.text_channels:
            perms = channel.permissions_for(me)
            if perms.view_channel and perms.read_message_history:
                return channel
        return None

    def _ensure_default_subscription(
        self,
        bot: discord.Client,
        agent_name: str,
        guild: discord.Guild,
    ) -> None:
        """若尚未設定訂閱頻道，預設使用第一個可見頻道。"""
        current = self._message_collector.get_subscription(str(guild.id), agent_name)
        if current:
            return
        channel = self._first_visible_channel(bot, guild)
        if channel is None:
            return
        self._set_agent_subscription(
            agent_name,
            str(guild.id),
            str(channel.id),
            channel.name,
        )

    # ---- Discord 事件 ----

    def _register_bot_events(self, bot: discord.Client, agent: Agent) -> None:
        """為 Bot 實例註冊 Discord 事件處理。"""
        log = logger.bind(agent=agent.name)

        def _is_guild_event_bot() -> bool:
            return (
                self._config.guild_events.enabled
                and agent.name == self._config.guild_events.agent_name
            )

        def _event_enabled(event_type: str) -> bool:
            return event_type in self._config.guild_events.include_events

        def _guild_log_channel_id(guild: discord.Guild) -> str:
            channel = discord.utils.get(
                guild.text_channels,
                name=self._config.guild_events.log_channel_name,
            )
            if channel:
                return str(channel.id)
            return str(guild.system_channel.id) if guild.system_channel else ""

        async def _dispatch_guild_event(
            guild: discord.Guild,
            event_type: str,
            description: str,
        ) -> None:
            if not _is_guild_event_bot() or not _event_enabled(event_type):
                return
            log_channel_id = _guild_log_channel_id(guild)
            if not log_channel_id:
                log.warning(
                    "guild_event.no_log_channel",
                    guild=str(guild.id),
                    event_type=event_type,
                )
                return
            task = Task(
                guild_id=str(guild.id),
                agent_name=agent.name,
                task_type="guild_event",
                priority=TaskPriority.ADMIN,
                payload={
                    "event_type": event_type,
                    "description": description,
                    "log_channel_id": log_channel_id,
                },
            )
            await self._task_queue.enqueue(task)

        def _format_changes(before: Any, after: Any, fields: list[str]) -> list[str]:
            changes: list[str] = []
            for field in fields:
                old = getattr(before, field, None)
                new = getattr(after, field, None)
                if old != new:
                    changes.append(f"- {field}: {old!r} -> {new!r}")
            return changes

        def _role_summary(role: discord.Role) -> str:
            return (
                f"role={role.name!r} id={role.id} position={role.position} "
                f"color={role.color} hoist={role.hoist} mentionable={role.mentionable}"
            )

        def _channel_summary(channel: discord.abc.GuildChannel) -> str:
            return (
                f"channel={channel.name!r} id={channel.id} type={channel.type} "
                f"category={getattr(getattr(channel, 'category', None), 'name', None)!r}"
            )

        def _mentioned_other_bot(message: discord.Message) -> bool:
            if not bot.user:
                return False
            active_bot_ids = {
                client.user.id
                for name, client in self._bots.items()
                if name != agent.name and client.user is not None
            }
            return any(user.id in active_bot_ids for user in message.mentions)

        def _reply_to_other_bot(message: discord.Message) -> bool:
            if message.reference is None or message.reference.resolved is None:
                return False
            replied_author = getattr(message.reference.resolved, "author", None)
            if replied_author is None or not getattr(replied_author, "bot", False):
                return False
            return bot.user is not None and replied_author.id != bot.user.id

        def _bot_author_name(author: discord.abc.User | discord.Member) -> str:
            for bot_name, client in self._bots.items():
                if client.user is not None and client.user.id == author.id:
                    return bot_name
            return getattr(author, "display_name", None) or author.name

        @bot.event
        async def on_ready() -> None:
            log.info(
                "bot.ready",
                user=str(bot.user),
                guilds=len(bot.guilds),
            )
            # 更新伺服器記憶
            for guild in bot.guilds:
                self._ensure_default_subscription(bot, agent.name, guild)
                await agent._long_term_memory.store_server_info(
                    guild_id=str(guild.id),
                    server_name=guild.name,
                    member_count=guild.member_count or 0,
                )

        @bot.event
        async def on_message(message: discord.Message) -> None:
            # 忽略自己發的訊息
            if message.author == bot.user:
                return
            # 忽略 DM
            if message.guild is None:
                return

            guild_id = str(message.guild.id)
            channel_id = str(message.channel.id)
            self._ensure_default_subscription(bot, agent.name, message.guild)

            # 判斷是否為針對「這隻 bot」的 Mention / Reply
            is_mention = bot.user.mentioned_in(message) if bot.user else False
            is_reply = (
                message.reference is not None
                and message.reference.resolved is not None
                and getattr(message.reference.resolved, "author", None) == bot.user
            )
            is_directed_at_me = is_mention or is_reply
            is_directed_at_other_bot = (
                _mentioned_other_bot(message) or _reply_to_other_bot(message)
            )

            # 決定這則訊息是否由「我」(本 bot) 收集：
            #  - 被點名/回覆到我 → 我來處理
            #  - 否則：只有主回應 bot 收集一般聊天訊息，避免多隻 bot 搶答同一則
            if is_directed_at_me:
                pass  # 由我處理
            elif is_directed_at_other_bot:
                # 這則訊息是給其他 bot 的，主回應 bot 不搶答。
                if self._config.override.enabled:
                    await self._check_override(message, guild_id)
                return
            elif agent.name == self._primary_agent_name:
                pass  # 一般聊天交給主回應 bot
            else:
                # 非主回應 bot 且沒被點名 → 不收集，避免重複回應
                if self._config.override.enabled:
                    await self._check_override(message, guild_id)
                return

            # 將訊息完整內容序列化（含 embed / components / 轉發 / 系統事件），
            # 讓 Agent 不只看到純文字。若無豐富內容則沿用原始 content。
            from src.discord.message_parser import (
                message_to_readable_text,
                channel_lineage,
                reaction_counts,
            )
            rich_content = message_to_readable_text(message) or message.content
            lineage = channel_lineage(message.channel)

            # 收集訊息（綁定由本 Agent 處理）
            # 供 Agent 後續 reply 用的目標訊息 ID。
            # 這裡應該綁定「觸發本次回應的使用者訊息」，
            # 而不是使用者所回覆的舊訊息，否則 bot 會看起來像在回自己上一句。
            ref_msg_id = str(message.id) if (is_mention or is_reply) else ""

            collected = CollectedMessage(
                guild_id=guild_id,
                channel_id=channel_id,
                channel_name=lineage["channel_name"],
                channel_type=lineage["channel_type"],
                category_name=lineage["category_name"],
                parent_channel_id=lineage["parent_channel_id"],
                parent_channel_name=lineage["parent_channel_name"],
                message_id=str(message.id),
                author_id=str(message.author.id),
                author_name=(
                    _bot_author_name(message.author)
                    if message.author.bot
                    else message.author.display_name
                ),
                content=rich_content,
                is_bot=message.author.bot,
                is_mention=is_mention,
                is_reply=is_reply,
                timestamp=message.created_at.isoformat(),
                agent_name=agent.name,
                reference_message_id=ref_msg_id,
                jump_url=message.jump_url,
                reaction_counts=reaction_counts(message),
            )

            await self._message_collector.collect(collected)

            # Human Override 檢查
            if self._config.override.enabled:
                await self._check_override(message, guild_id)

        @bot.event
        async def on_member_join(member: discord.Member) -> None:
            log.info("bot.member_joined", member=member.display_name)
            # 可擴展：自動歡迎

        @bot.event
        async def on_member_remove(member: discord.Member) -> None:
            log.info("bot.member_removed", member=member.display_name)

        @bot.event
        async def on_guild_update(before: discord.Guild, after: discord.Guild) -> None:
            changes = _format_changes(
                before,
                after,
                ["name", "description", "verification_level", "explicit_content_filter", "preferred_locale"],
            )
            if changes:
                await _dispatch_guild_event(
                    after,
                    "guild_update",
                    "伺服器設定被更新：\n" + "\n".join(changes),
                )

        @bot.event
        async def on_guild_role_create(role: discord.Role) -> None:
            await _dispatch_guild_event(
                role.guild,
                "role_create",
                "身份組被建立：\n" + _role_summary(role),
            )

        @bot.event
        async def on_guild_role_update(before: discord.Role, after: discord.Role) -> None:
            changes = _format_changes(
                before,
                after,
                ["name", "color", "hoist", "mentionable", "position", "permissions"],
            )
            if changes:
                await _dispatch_guild_event(
                    after.guild,
                    "role_update",
                    f"身份組被更新：\n{_role_summary(after)}\n" + "\n".join(changes),
                )

        @bot.event
        async def on_guild_role_delete(role: discord.Role) -> None:
            await _dispatch_guild_event(
                role.guild,
                "role_delete",
                "身份組被刪除：\n" + _role_summary(role),
            )

        @bot.event
        async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
            await _dispatch_guild_event(
                channel.guild,
                "channel_create",
                "頻道被建立：\n" + _channel_summary(channel),
            )

        @bot.event
        async def on_guild_channel_update(
            before: discord.abc.GuildChannel,
            after: discord.abc.GuildChannel,
        ) -> None:
            changes = _format_changes(before, after, ["name", "position", "category", "overwrites"])
            topic_before = getattr(before, "topic", None)
            topic_after = getattr(after, "topic", None)
            if topic_before != topic_after:
                changes.append(f"- topic: {topic_before!r} -> {topic_after!r}")
            if changes:
                await _dispatch_guild_event(
                    after.guild,
                    "channel_update",
                    f"頻道被更新：\n{_channel_summary(after)}\n" + "\n".join(changes),
                )

        @bot.event
        async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
            await _dispatch_guild_event(
                channel.guild,
                "channel_delete",
                "頻道被刪除：\n" + _channel_summary(channel),
            )

    async def _check_override(self, message: discord.Message, guild_id: str) -> None:
        """檢查 Human Override 指令。"""
        content = message.content.strip()
        override = self._config.override

        if content == override.approve_command:
            logger.info("override.approve", guild=guild_id, user=message.author.display_name)
            # TODO: 批准待執行的危險操作

        elif content == override.deny_command:
            logger.info("override.deny", guild=guild_id, user=message.author.display_name)
            # TODO: 拒絕待執行的操作

        elif content == override.stop_command:
            logger.info("override.stop", guild=guild_id, user=message.author.display_name)
            # TODO: 停止所有待執行操作

    # ---- Batch 觸發回呼 ----

    async def _on_batch_trigger(self, batch: MessageBatch) -> None:
        """當 MessageCollector 觸發批次時的回呼。

        將批次轉換為 Task 並加入佇列。
        """
        task_type = "mention" if batch.is_priority else "batch"
        priority = TaskPriority.MENTION if batch.is_priority else TaskPriority.BATCH

        # 選擇 Agent（輪詢或指定）
        agent_name = self._select_agent(batch)

        task = Task(
            guild_id=batch.guild_id,
            agent_name=agent_name,
            task_type=task_type,
            priority=priority,
            payload={
                "batch_id": batch.batch_id,
                "channel_id": batch.channel_id,
                "reply_channel_id": batch.reply_channel_id or batch.channel_id,
                "message_count": len(batch.messages) + len(batch.attention_messages),
                "trigger_reason": batch.trigger_reason,
                "other_channels_new_count": batch.other_channels_new_count,
            },
        )

        # 將 batch 附加到 task（記憶體中，enqueue 時會自動移至 _batch_store）
        task.payload["_batch"] = batch  # type: ignore[assignment]

        await self._task_queue.enqueue(task)

        # 從 _batch_store 取回 batch 供後續 handler 使用
        # (batch 已由 TaskQueue.enqueue 自動存入 _batch_store)

    def _select_agent(self, batch: MessageBatch) -> str:
        """選擇處理任務的 Agent。

        優先使用 batch 在收集階段就綁定的 agent_name（被點名的 bot 或主回應 bot）。
        若沒有綁定，才退回到主回應 bot 或第一位可用 Agent。
        """
        if batch.agent_name and batch.agent_name in self._agents:
            return batch.agent_name
        if self._primary_agent_name and self._primary_agent_name in self._agents:
            return self._primary_agent_name
        agent_names = list(self._agents.keys())
        return agent_names[0] if agent_names else "Alice"

    # ---- Task Handlers ----

    async def _handle_mention_task(self, task: Task) -> None:
        """處理 Mention 任務。"""
        agent = self._agents.get(task.agent_name)
        if not agent:
            logger.error("task_handler.agent_not_found", name=task.agent_name)
            return

        batch: MessageBatch | None = self._task_queue.get_batch(task.id)
        if batch:
            await agent.process_batch(batch)
            self._task_queue.remove_batch(task.id)

    async def _handle_batch_task(self, task: Task) -> None:
        """處理 Batch 任務。"""
        await self._handle_mention_task(task)

    async def _handle_admin_task(self, task: Task) -> None:
        """處理 Admin 任務。"""
        await self._handle_mention_task(task)

    async def _handle_council_task(self, task: Task) -> None:
        """處理 Council 任務。"""
        agent = self._agents.get(task.agent_name)
        if not agent:
            return

        content = task.payload.get("content", "")
        guild_id = task.guild_id
        await agent.council_message(content, guild_id)

    async def _handle_guild_event_task(self, task: Task) -> None:
        """處理 Guild 事件任務。"""
        agent = self._agents.get(task.agent_name)
        if not agent:
            logger.error("guild_event.agent_not_found", name=task.agent_name)
            return

        await agent.process_guild_event(
            guild_id=task.guild_id,
            event_type=str(task.payload.get("event_type", "unknown")),
            event_description=str(task.payload.get("description", "")),
            log_channel_id=str(task.payload.get("log_channel_id", "")),
        )

    # ---- 生命週期 ----

    async def start(self) -> None:
        """啟動所有 Bot 實例。"""
        if not self._agents:
            await self.initialize()

        # 啟動 Task Queue
        await self._task_queue.start()

        # 處理 Retry 任務
        await self._task_queue.process_retries()

        # 啟動所有 Bot（並行）
        bot_tasks: list[asyncio.Task[None]] = []
        for name, bot in self._bots.items():
            token = self._config.agents[
                next(i for i, a in enumerate(self._config.agents) if a.name == name)
            ].token

            # 跳過無效的 token（placeholder）
            if not token or token.startswith("PLACEHOLDER"):
                logger.warning("agent_manager.bot_skipped", name=name, reason="placeholder_token")
                continue

            task = asyncio.create_task(
                bot.start(token),
                name=f"bot-{name}",
            )
            bot_tasks.append(task)
            logger.info("agent_manager.bot_starting", name=name)

        # 等待所有 Bot（通常不會結束）
        if bot_tasks:
            try:
                await asyncio.gather(*bot_tasks)
            except Exception as exc:
                logger.error("agent_manager.bot_error", error=str(exc))
        else:
            logger.warning("agent_manager.no_bots_with_valid_tokens")
            # 沒有有效 token 的 Bot，保持運行讓其他服務（如 Dashboard）可用
            import asyncio as _asyncio
            await _asyncio.Event().wait()

    async def stop(self) -> None:
        """停止所有 Bot 實例與服務。"""
        logger.info("agent_manager.stopping")

        # 停止 Task Queue
        if self._task_queue:
            await self._task_queue.stop()

        # 關閉所有 Bot
        for name, bot in self._bots.items():
            await bot.close()
            logger.info("agent_manager.bot_closed", name=name)

        # 排出暫存訊息
        if self._message_collector:
            await self._message_collector.flush_all()

        # 關閉資料庫
        await self._db.close()

        logger.info("agent_manager.stopped")

    # ---- 查詢 ----

    def get_agent(self, name: str) -> Agent | None:
        """取得指定 Agent。"""
        return self._agents.get(name)

    def get_all_agents(self) -> list[Agent]:
        """取得所有 Agent。"""
        return list(self._agents.values())

    def set_council(self, council: Any) -> None:
        """注入 Council 系統，供 start_council tool 使用。"""
        self._council = council

    async def get_system_status(self) -> dict[str, Any]:
        """取得系統整體狀態。"""
        agents_status = [a.get_status() for a in self._agents.values()]
        queue_status = await self._task_queue.get_status() if self._task_queue else {}

        return {
            "agents": agents_status,
            "queue": queue_status,
            "tools": self._tool_registry.tool_count if self._tool_registry else 0,
        }
