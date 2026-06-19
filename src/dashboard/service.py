"""Dashboard 抽象服務。

為未來的 FastAPI / React 儀表板提供抽象層。
定義資料提供者介面，讓任何前端框架都能整合。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import structlog

from src.agents.manager import AgentManager
from src.council.council import AICouncil
from src.override.override import HumanOverride
from src.queue.task_queue import TaskQueue
from src.database.repository import (
    ActionLogRepository,
    TaskRepository,
    MessageRepository,
    MemoryRepository,
    ToolCallRepository,
)

logger = structlog.get_logger(__name__)


@dataclass
class AgentStatus:
    """Agent 狀態資料。"""

    name: str
    personality: str
    is_online: bool
    context_tokens: int
    token_budget: int
    pending_actions: int


@dataclass
class QueueStatus:
    """佇列狀態資料。"""

    priority_queue_size: int
    normal_queue_size: int
    retry_queue_size: int
    total_processed: int
    total_failed: int


@dataclass
class CouncilStatus:
    """Council 狀態資料。"""

    enabled: bool
    current_state: str
    active_guilds: list[str]
    total_discussions: int


@dataclass
class GuildStatus:
    """Discord Guild 狀態資料。"""

    id: str
    name: str
    member_count: int
    text_channels: int
    voice_channels: int
    connected_agents: list[str]


@dataclass
class OverrideStatusData:
    """Override 狀態資料。"""

    enabled: bool
    pending_count: int
    pending_actions: list[dict[str, Any]]


@dataclass
class SystemOverview:
    """系統總覽資料。"""

    agents: list[AgentStatus]
    queue: QueueStatus
    council: CouncilStatus
    override: OverrideStatusData
    tools_count: int
    uptime_seconds: float


class DashboardDataProvider(ABC):
    """Dashboard 資料提供者抽象介面。

    定義所有 Dashboard 需要的資料存取方法。
    未來可實作為 FastAPI router、WebSocket handler 等。
    """

    @abstractmethod
    async def get_system_overview(self) -> SystemOverview:
        """取得系統總覽。"""

    @abstractmethod
    async def get_agent_status(self, agent_name: str) -> AgentStatus | None:
        """取得指定 Agent 狀態。"""

    @abstractmethod
    async def get_all_agents(self) -> list[AgentStatus]:
        """取得所有 Agent 狀態。"""

    @abstractmethod
    async def get_queue_status(self) -> QueueStatus:
        """取得佇列狀態。"""

    @abstractmethod
    async def get_council_status(self) -> CouncilStatus:
        """取得 Council 狀態。"""

    @abstractmethod
    async def get_override_status(self) -> OverrideStatusData:
        """取得 Override 狀態。"""

    @abstractmethod
    async def get_guilds(self) -> list[GuildStatus]:
        """取得目前 Bot 可見的 Discord Guild。"""

    @abstractmethod
    async def get_tools(self) -> list[dict[str, Any]]:
        """取得工具清單。"""

    @abstractmethod
    async def get_action_logs(
        self, guild_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """取得操作日誌。"""

    @abstractmethod
    async def get_tool_calls(
        self, guild_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """取得工具呼叫紀錄。"""

    @abstractmethod
    async def get_memory(
        self, guild_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """取得長期記憶。"""

    @abstractmethod
    async def get_tasks(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """取得任務清單。"""


class DashboardService(DashboardDataProvider):
    """Dashboard 服務實作。

    整合 AgentManager、Council、Override 等系統，
    提供統一的資料存取介面。
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        council: AICouncil | None = None,
        override: HumanOverride | None = None,
        action_log_repo: ActionLogRepository | None = None,
        task_repo: TaskRepository | None = None,
        memory_repo: MemoryRepository | None = None,
        message_repo: MessageRepository | None = None,
        tool_call_repo: ToolCallRepository | None = None,
    ) -> None:
        self._manager = agent_manager
        self._council = council
        self._override = override
        self._action_log = action_log_repo
        self._task_repo = task_repo
        self._memory_repo = memory_repo
        self._message_repo = message_repo
        self._tool_call_repo = tool_call_repo

        self._start_time: float = 0.0
        self._log = logger.bind(component="dashboard")

    def set_start_time(self, timestamp: float) -> None:
        """設定系統啟動時間。"""
        self._start_time = timestamp

    async def get_system_overview(self) -> SystemOverview:
        """取得系統總覽。"""
        import time

        agents = await self.get_all_agents()
        queue = await self.get_queue_status()
        council = await self.get_council_status()
        override = await self.get_override_status()

        tools_count = 0
        system_status = await self._manager.get_system_status()
        tools_count = int(system_status.get("tools", 0))

        return SystemOverview(
            agents=agents,
            queue=queue,
            council=council,
            override=override,
            tools_count=tools_count,
            uptime_seconds=time.time() - self._start_time if self._start_time else 0,
        )

    async def get_agent_status(self, agent_name: str) -> AgentStatus | None:
        """取得指定 Agent 狀態。"""
        agent = self._manager.get_agent(agent_name)
        if not agent:
            return None

        status = agent.get_status()
        return AgentStatus(
            name=status["name"],
            personality=status["personality"],
            is_online=status["bot_ready"],
            context_tokens=status["context_tokens"],
            token_budget=status["token_budget"],
            pending_actions=0,  # TODO: 從 queue 取得
        )

    async def get_all_agents(self) -> list[AgentStatus]:
        """取得所有 Agent 狀態。"""
        result = []
        for agent in self._manager.get_all_agents():
            status = await self.get_agent_status(agent.name)
            if status:
                result.append(status)
        return result

    async def get_queue_status(self) -> QueueStatus:
        """取得佇列狀態。"""
        # 嘗試從 AgentManager 取得
        try:
            system_status = await self._manager.get_system_status()
            queue_data = system_status.get("queue", {})
            total_processed = 0
            total_failed = 0
            if self._task_repo:
                total_processed = await self._task_repo.count_by_status("completed")
                total_failed = await self._task_repo.count_by_status("failed")
            return QueueStatus(
                priority_queue_size=int(queue_data.get("priority_queue_size", 0)),
                normal_queue_size=int(queue_data.get("normal_queue_size", 0)),
                retry_queue_size=int(queue_data.get("retry_queue_size", 0)),
                total_processed=total_processed,
                total_failed=total_failed,
            )
        except Exception:
            return QueueStatus(
                priority_queue_size=0,
                normal_queue_size=0,
                retry_queue_size=0,
                total_processed=0,
                total_failed=0,
            )

    async def get_council_status(self) -> CouncilStatus:
        """取得 Council 狀態。"""
        if not self._council:
            return CouncilStatus(
                enabled=False,
                current_state="disabled",
                active_guilds=[],
                total_discussions=0,
            )

        states = getattr(self._council, "_states", {})
        return CouncilStatus(
            enabled=True,
            current_state="active" if states else "idle",
            active_guilds=list(states.keys()),
            total_discussions=len(getattr(self._council, "_discussions", {})),
        )

    async def get_override_status(self) -> OverrideStatusData:
        """取得 Override 狀態。"""
        if not self._override:
            return OverrideStatusData(
                enabled=False,
                pending_count=0,
                pending_actions=[],
            )

        actions = self._override.get_pending_actions()
        return OverrideStatusData(
            enabled=True,
            pending_count=len(actions),
            pending_actions=[
                {
                    "action_id": a.action_id,
                    "agent": a.agent_name,
                    "tool": a.tool_name,
                    "safety_level": a.safety_level,
                    "reasoning": a.reasoning[:100],
                }
                for a in actions
            ],
        )

    async def get_guilds(self) -> list[GuildStatus]:
        """取得目前 Bot 可見的 Discord Guild。"""
        guilds: dict[str, GuildStatus] = {}
        bots = getattr(self._manager, "_bots", {})
        for agent_name, bot in bots.items():
            for guild in bot.guilds:
                guild_id = str(guild.id)
                current = guilds.get(guild_id)
                if current:
                    current.connected_agents.append(agent_name)
                    continue
                guilds[guild_id] = GuildStatus(
                    id=guild_id,
                    name=guild.name,
                    member_count=guild.member_count or 0,
                    text_channels=len(getattr(guild, "text_channels", [])),
                    voice_channels=len(getattr(guild, "voice_channels", [])),
                    connected_agents=[agent_name],
                )
        return sorted(guilds.values(), key=lambda item: item.name.lower())

    async def get_tools(self) -> list[dict[str, Any]]:
        """取得工具清單。"""
        registry = getattr(self._manager, "_tool_registry", None)
        if not registry:
            return []
        safety_map = registry.get_safety_map()
        return [
            {"name": name, "safety_level": safety_map.get(name, "UNKNOWN")}
            for name in sorted(registry.tool_names)
        ]

    async def get_action_logs(
        self, guild_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """取得操作日誌。"""
        if not self._action_log:
            return []
        return await self._action_log.get_recent(guild_id, limit=limit)

    async def get_tool_calls(
        self, guild_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """取得工具呼叫紀錄。"""
        if not self._tool_call_repo:
            return []
        return await self._tool_call_repo.get_recent(guild_id, limit=limit)

    async def get_memory(
        self, guild_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """取得長期記憶。"""
        if not self._memory_repo:
            return []
        memories = await self._memory_repo.get_by_guild(guild_id)
        if category:
            memories = [m for m in memories if m.get("category") == category]
        return memories

    async def get_tasks(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """取得任務清單。"""
        if not self._task_repo:
            return []
        if status:
            return await self._task_repo.list_by_status(status, limit=limit)
        return await self._task_repo.get_recent(limit=limit)
