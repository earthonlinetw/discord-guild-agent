"""訊息處理管線。

將 Discord 事件 → MessageCollector → TaskQueue → Agent 串聯起來。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from src.agents.agent import Agent
from src.agents.manager import AgentManager
from src.memory.short_term import MessageBatch
from src.queue.task_queue import Task, TaskPriority, TaskQueue
from src.tools.base import SafetyLevel, ToolResult
from src.tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)


class MessageProcessor:
    """訊息處理管線。

    負責從 TaskQueue 取得任務並分派給 Agent 處理。
    包含 Tool Call 安全檢查與 Human Override 整合。
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        tool_registry: ToolRegistry,
        task_queue: TaskQueue,
    ) -> None:
        """初始化。

        Args:
            agent_manager: Agent 管理器。
            tool_registry: Tool 註冊中心。
            task_queue: 任務佇列。
        """
        self._manager = agent_manager
        self._tools = tool_registry
        self._queue = task_queue

        # 待批准的危險操作（action_id → (task, tool_name, arguments)）
        self._pending_approval: dict[str, tuple[Task, str, dict[str, Any]]] = {}

    async def process_task(self, task: Task) -> None:
        """處理單一 Task。

        Args:
            task: 要處理的任務。
        """
        agent = self._manager.get_agent(task.agent_name)
        if not agent:
            logger.error("processor.agent_not_found", name=task.agent_name)
            return

        task_type = task.task_type

        if task_type in ("mention", "batch"):
            batch: MessageBatch | None = task.payload.get("_batch")
            if batch:
                await agent.process_batch(batch)
            else:
                # 從 DB 重建 batch
                logger.warning(
                    "processor.no_batch_in_payload",
                    task_id=task.id,
                    task_type=task_type,
                )

        elif task_type == "council":
            content = task.payload.get("content", "")
            guild_id = task.guild_id
            await agent.council_message(content, guild_id)

        elif task_type == "admin":
            # Admin 任務直接處理
            batch = task.payload.get("_batch")
            if batch:
                await agent.process_batch(batch)

        else:
            logger.warning("processor.unknown_task_type", type=task_type)

    async def request_approval(
        self,
        task: Task,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """請求 Human 批准危險操作。

        Args:
            task: 相關任務。
            tool_name: 工具名稱。
            arguments: 工具參數。

        Returns:
            action_id: 操作 ID，用於後續 approve/deny。
        """
        import uuid
        action_id = str(uuid.uuid4())[:8]

        self._pending_approval[action_id] = (task, tool_name, arguments)

        logger.info(
            "processor.approval_requested",
            action_id=action_id,
            tool=tool_name,
            agent=task.agent_name,
        )

        return action_id

    async def approve_action(self, action_id: str) -> bool:
        """批准操作。

        Args:
            action_id: 操作 ID。

        Returns:
            是否成功執行。
        """
        entry = self._pending_approval.pop(action_id, None)
        if not entry:
            logger.warning("processor.approval_not_found", action_id=action_id)
            return False

        task, tool_name, arguments = entry

        # 執行工具
        result = await self._tools.execute_tool(tool_name, **arguments)

        logger.info(
            "processor.action_approved",
            action_id=action_id,
            tool=tool_name,
            success=result.success,
        )

        return result.success

    async def deny_action(self, action_id: str) -> bool:
        """拒絕操作。

        Args:
            action_id: 操作 ID。

        Returns:
            是否成功拒絕。
        """
        entry = self._pending_approval.pop(action_id, None)
        if not entry:
            logger.warning("processor.approval_not_found", action_id=action_id)
            return False

        task, tool_name, arguments = entry

        logger.info(
            "processor.action_denied",
            action_id=action_id,
            tool=tool_name,
        )

        return True

    async def stop_all_pending(self) -> int:
        """停止所有待批准的操作。

        Returns:
            停止的數量。
        """
        count = len(self._pending_approval)
        self._pending_approval.clear()

        logger.info("processor.all_stopped", count=count)
        return count

    @property
    def pending_count(self) -> int:
        """待批准的操作數量。"""
        return len(self._pending_approval)

    def get_pending_actions(self) -> list[dict[str, Any]]:
        """取得所有待批准的操作。"""
        result = []
        for action_id, (task, tool_name, arguments) in self._pending_approval.items():
            result.append({
                "action_id": action_id,
                "tool": tool_name,
                "arguments": arguments,
                "agent": task.agent_name,
                "guild_id": task.guild_id,
            })
        return result
