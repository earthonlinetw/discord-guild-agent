"""Human Override 系統。

讓管理員可以在必要時介入 AI Agent 的行為：
- !approve: 批准待執行的危險操作
- !deny: 拒絕待執行的操作
- !stop: 緊急停止所有待執行操作

每個 Override 動作都會記錄在 Action Log 中。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

import discord

import structlog

from src.config.settings import OverrideConfig
from src.database.repository import ActionLogRepository, ToolCallRepository
from src.discord.processor import MessageProcessor
from src.council.council import AICouncil

logger = structlog.get_logger(__name__)

# Type alias
NotificationCallback = Callable[[str, str], Coroutine[Any, Any, None]]


class OverrideStatus(Enum):
    """Override 操作狀態。"""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    STOPPED = "stopped"
    EXPIRED = "expired"


@dataclass
class OverrideAction:
    """待批准的操作。"""

    action_id: str
    guild_id: str
    channel_id: str
    agent_name: str
    tool_name: str
    arguments: dict[str, Any]
    reasoning: str
    expected_result: str
    safety_level: str
    requester_id: str  # 觸發此操作的訊息作者
    status: OverrideStatus = OverrideStatus.PENDING
    approver_id: str | None = None


class HumanOverride:
    """Human Override 系統。

    管理 DANGEROUS 操作的批准流程。
    當 Agent 要執行 DANGEROUS 工具時，系統會：
    1. 將操作暫停，建立 OverrideAction
    2. 在 Discord 頻道通知管理員
    3. 等待 !approve / !deny / !stop
    4. 根據管理員決定執行或取消

    也支援緊急停止（!stop）終止所有待執行操作。
    """

    def __init__(
        self,
        config: OverrideConfig,
        action_log_repo: ActionLogRepository,
        tool_call_repo: ToolCallRepository,
        processor: MessageProcessor | None = None,
        council: AICouncil | None = None,
    ) -> None:
        """初始化。

        Args:
            config: Override 設定。
            action_log_repo: 操作日誌 Repository。
            tool_call_repo: Tool 呼叫 Repository。
            processor: 訊息處理器。
            council: Council 系統（可選）。
        """
        self._config = config
        self._action_log = action_log_repo
        self._tool_call_repo = tool_call_repo
        self._processor = processor
        self._council = council

        # 待批准的操作（action_id → OverrideAction）
        self._pending: dict[str, OverrideAction] = {}

        # 超時任務
        self._timeout_tasks: dict[str, asyncio.Task[None]] = {}

        # 通知回呼
        self._notify_cb: NotificationCallback | None = None

        self._log = logger.bind(component="override")

    def set_notification_callback(self, cb: NotificationCallback) -> None:
        """設定通知回呼。

        Args:
            cb: (guild_id, message) → None 的回呼。
        """
        self._notify_cb = cb

    # ---- 提交操作 ----

    async def submit_action(
        self,
        guild_id: str,
        channel_id: str,
        agent_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        reasoning: str,
        expected_result: str,
        safety_level: str,
        requester_id: str = "",
    ) -> str:
        """提交需要批准的操作。

        Args:
            guild_id: 伺服器 ID。
            channel_id: 頻道 ID。
            agent_name: Agent 名稱。
            tool_name: 工具名稱。
            arguments: 工具參數。
            reasoning: AI 推理內容。
            expected_result: 預期結果。
            safety_level: 安全等級。
            requester_id: 請求者 ID。

        Returns:
            action_id: 操作 ID。
        """
        if not self._config.enabled:
            # Override 未啟用，自動批准
            return "auto-approved"

        import uuid
        action_id = str(uuid.uuid4())[:8]

        action = OverrideAction(
            action_id=action_id,
            guild_id=guild_id,
            channel_id=channel_id,
            agent_name=agent_name,
            tool_name=tool_name,
            arguments=arguments,
            reasoning=reasoning,
            expected_result=expected_result,
            safety_level=safety_level,
            requester_id=requester_id,
        )

        self._pending[action_id] = action

        # 通知管理員
        message = (
            f"🚨 **需要批准的操作** 🚨\n"
            f"**操作 ID**: `{action_id}`\n"
            f"**Agent**: {agent_name}\n"
            f"**工具**: {tool_name} ({safety_level})\n"
            f"**參數**: ```json\n{arguments}\n```\n"
            f"**推理**: {reasoning[:200]}\n"
            f"**預期結果**: {expected_result[:100]}\n"
            f"---\n"
            f"使用 `{self._config.approve_command} {action_id}` 批准\n"
            f"使用 `{self._config.deny_command} {action_id}` 拒絕\n"
        )

        if self._notify_cb:
            await self._notify_cb(guild_id, message)

        # 記錄 Action Log
        await self._action_log.insert(
            guild_id=guild_id,
            agent_name=agent_name,
            reason=reasoning,
            action=f"override_pending_{tool_name}",
            tool_name=tool_name,
            parameters=arguments,
            safety_level=safety_level,
        )

        # 設定超時
        timeout_seconds = getattr(self._config, "timeout", 300)
        self._timeout_tasks[action_id] = asyncio.create_task(
            self._expire_action(action_id, timeout_seconds)
        )

        self._log.info(
            "override.action_submitted",
            action_id=action_id,
            tool=tool_name,
            agent=agent_name,
        )

        return action_id

    # ---- 批准 ----

    async def approve(self, action_id: str, approver_id: str) -> OverrideAction | None:
        """批准操作。

        Args:
            action_id: 操作 ID。
            approver_id: 批准者 ID。

        Returns:
            操作詳情，或 None 如果找不到。
        """
        action = self._pending.pop(action_id, None)
        if not action:
            self._log.warning("override.approve_not_found", action_id=action_id)
            return None

        # 取消超時
        task = self._timeout_tasks.pop(action_id, None)
        if task:
            task.cancel()

        action.status = OverrideStatus.APPROVED
        action.approver_id = approver_id

        # 透過 Processor 執行
        if self._processor:
            success = await self._processor.approve_action(action_id)
            result_status = "success" if success else "failed"
        else:
            result_status = "no_processor"

        # 記錄 Action Log
        await self._action_log.insert(
            guild_id=action.guild_id,
            agent_name=action.agent_name,
            reason=f"管理員批准操作 {action_id}",
            action=f"override_approved_{action.tool_name}",
            tool_name=action.tool_name,
            parameters=action.arguments,
            safety_level=action.safety_level,
        )

        self._log.info(
            "override.approved",
            action_id=action_id,
            tool=action.tool_name,
            approver=approver_id,
            result=result_status,
        )

        return action

    # ---- 拒絕 ----

    async def deny(self, action_id: str, approver_id: str) -> OverrideAction | None:
        """拒絕操作。

        Args:
            action_id: 操作 ID。
            approver_id: 拒絕者 ID。

        Returns:
            操作詳情，或 None 如果找不到。
        """
        action = self._pending.pop(action_id, None)
        if not action:
            self._log.warning("override.deny_not_found", action_id=action_id)
            return None

        # 取消超時
        task = self._timeout_tasks.pop(action_id, None)
        if task:
            task.cancel()

        action.status = OverrideStatus.DENIED
        action.approver_id = approver_id

        # 透過 Processor 拒絕
        if self._processor:
            await self._processor.deny_action(action_id)

        # 記錄 Action Log
        await self._action_log.insert(
            guild_id=action.guild_id,
            agent_name=action.agent_name,
            reason=f"管理員拒絕操作 {action_id}",
            action=f"override_denied_{action.tool_name}",
            tool_name=action.tool_name,
            parameters=action.arguments,
            safety_level=action.safety_level,
        )

        self._log.info(
            "override.denied",
            action_id=action_id,
            tool=action.tool_name,
            approver=approver_id,
        )

        return action

    # ---- 緊急停止 ----

    async def stop_all(self, guild_id: str, operator_id: str) -> int:
        """緊急停止所有待執行操作。

        Args:
            guild_id: 伺服器 ID。
            operator_id: 操作者 ID。

        Returns:
            停止的數量。
        """
        # 收集該伺服器的待批准操作
        to_stop = [
            aid for aid, action in self._pending.items()
            if action.guild_id == guild_id
        ]

        for action_id in to_stop:
            action = self._pending.pop(action_id, None)
            if action:
                action.status = OverrideStatus.STOPPED
                action.approver_id = operator_id

                # 取消超時
                task = self._timeout_tasks.pop(action_id, None)
                if task:
                    task.cancel()

        # 停止 Processor
        if self._processor:
            stopped = await self._processor.stop_all_pending()
        else:
            stopped = 0

        # 停止 Council
        if self._council:
            await self._council.force_stop(guild_id)

        # 記錄 Action Log
        await self._action_log.insert(
            guild_id=guild_id,
            agent_name="override",
            reason=f"管理員緊急停止 ({operator_id})",
            action="override_stop_all",
            tool_name="override",
            parameters={"stopped_count": len(to_stop) + stopped},
            safety_level="SAFE",
        )

        total = len(to_stop) + stopped

        self._log.info(
            "override.stopped_all",
            guild=guild_id,
            count=total,
            operator=operator_id,
        )

        return total

    # ---- 超時處理 ----

    async def _expire_action(self, action_id: str, timeout: int) -> None:
        """操作超時後自動過期。"""
        await asyncio.sleep(timeout)

        action = self._pending.pop(action_id, None)
        if action:
            action.status = OverrideStatus.EXPIRED

            self._log.warning(
                "override.expired",
                action_id=action_id,
                tool=action.tool_name,
            )

            # 記錄 Action Log
            await self._action_log.insert(
                guild_id=action.guild_id,
                agent_name=action.agent_name,
                reason=f"操作 {action_id} 超時未批准，自動過期",
                action=f"override_expired_{action.tool_name}",
                tool_name=action.tool_name,
                parameters=action.arguments,
                safety_level=action.safety_level,
            )

        # 清理超時任務
        self._timeout_tasks.pop(action_id, None)

    # ---- 查詢 ----

    def get_pending_actions(self, guild_id: str | None = None) -> list[OverrideAction]:
        """取得待批准的操作。"""
        actions = list(self._pending.values())
        if guild_id:
            actions = [a for a in actions if a.guild_id == guild_id]
        return actions

    def get_action(self, action_id: str) -> OverrideAction | None:
        """取得指定操作。"""
        return self._pending.get(action_id)

    @property
    def pending_count(self) -> int:
        """待批准的操作數量。"""
        return len(self._pending)

    # ---- 指令解析 ----

    async def handle_command(
        self,
        content: str,
        guild_id: str,
        user_id: str,
    ) -> str:
        """處理 Override 指令。

        Args:
            content: 指令內容。
            guild_id: 伺服器 ID。
            user_id: 使用者 ID。

        Returns:
            回應訊息。
        """
        if not self._config.enabled:
            return "Override 系統未啟用"

        parts = content.strip().split()
        command = parts[0]
        action_id = parts[1] if len(parts) > 1 else None

        if command == self._config.approve_command:
            if not action_id:
                # 如果沒有指定 action_id，批准第一個
                pending = self.get_pending_actions(guild_id)
                if pending:
                    action_id = pending[0].action_id
                else:
                    return "沒有待批准的操作"

            action = await self.approve(action_id, user_id)
            if action:
                return f"✅ 操作 `{action_id}` 已批准並執行"
            else:
                return f"❌ 找不到操作 `{action_id}`"

        elif command == self._config.deny_command:
            if not action_id:
                pending = self.get_pending_actions(guild_id)
                if pending:
                    action_id = pending[0].action_id
                else:
                    return "沒有待拒絕的操作"

            action = await self.deny(action_id, user_id)
            if action:
                return f"❌ 操作 `{action_id}` 已拒絕"
            else:
                return f"❌ 找不到操作 `{action_id}`"

        elif command == self._config.stop_command:
            count = await self.stop_all(guild_id, user_id)
            return f"🛑 已停止 {count} 個待執行操作"

        else:
            return f"未知的 Override 指令: {command}"
