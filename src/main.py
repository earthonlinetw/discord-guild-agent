"""Discord AI Agent 管理系統 - 主入口點。

初始化所有系統、載入設定、執行遷移、啟動 Bot 實例。
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time

import structlog

from src.config.settings import ConfigLoader
from src.logging_.logger import setup_logging
from src.database.connection import DatabaseConnection
from src.agents.manager import AgentManager
from src.council.council import AICouncil
from src.override.override import HumanOverride
from src.discord.processor import MessageProcessor
from src.dashboard.service import DashboardService
from src.dashboard.web import DashboardServer
from src.slash.commands import register_all_commands
from src.database.repository import (
    ActionLogRepository,
    TaskRepository,
    MemoryRepository,
    SummaryRepository,
    ToolCallRepository,
)

logger = structlog.get_logger(__name__)


class Application:
    """應用程式主類別。

    管理整個系統的生命週期：
    1. 載入設定
    2. 初始化日誌
    3. 連接資料庫
    4. 執行遷移
    5. 建立共享服務
    6. 建立 Agent 實例
    7. 註冊 Slash Commands
    8. 啟動所有 Bot
    9. 處理優雅關閉
    """

    def __init__(self) -> None:
        self._config = None
        self._db: DatabaseConnection | None = None
        self._agent_manager: AgentManager | None = None
        self._council: AICouncil | None = None
        self._override: HumanOverride | None = None
        self._processor: MessageProcessor | None = None
        self._dashboard: DashboardService | None = None
        self._dashboard_server: DashboardServer | None = None
        self._start_time: float = 0.0

    async def run(self) -> None:
        """啟動應用程式。"""
        self._start_time = time.time()

        # 1. 載入設定
        try:
            self._config = ConfigLoader.load("config.yaml")
        except Exception as exc:
            print(f"❌ 設定載入失敗: {exc}")
            sys.exit(1)

        # 2. 初始化日誌
        setup_logging(self._config.logging.level, self._config.logging.format)

        logger.info(
            "app.starting",
            agents=[a.name for a in self._config.agents],
            database=self._config.database.url[:30] + "...",
        )

        # 3. 連接資料庫
        self._db = DatabaseConnection(self._config.database.url)

        try:
            # 4. 初始化 Agent Manager（包含 DB、Migration、AI、Tools 等）
            self._agent_manager = AgentManager(self._config, self._db)
            await self._agent_manager.initialize()

            # 5. 建立 Processor
            from src.tools.registry import ToolRegistry
            from src.queue.task_queue import TaskQueue

            # 取得共享服務
            tool_registry = self._agent_manager._tool_registry
            task_queue = self._agent_manager._task_queue

            self._processor = MessageProcessor(
                self._agent_manager, tool_registry, task_queue
            )

            # 6. 建立 Override 系統
            action_log_repo = self._agent_manager._action_log_repo
            tool_call_repo = self._agent_manager._tool_call_repo

            self._override = HumanOverride(
                self._config.override,
                action_log_repo,
                tool_call_repo,
                self._processor,
            )

            # 7. 建立 Council 系統
            if self._config.council.enabled:
                from src.ai.provider import AIProvider
                ai_provider = self._agent_manager._ai_provider

                self._council = AICouncil(
                    self._config.council,
                    self._agent_manager.get_all_agents(),
                    ai_provider,
                    action_log_repo,
                )
                self._agent_manager.set_council(self._council)

            # 8. 建立 Dashboard
            task_repo = self._agent_manager._task_repo
            memory_repo = self._agent_manager._memory_repo
            summary_repo = self._agent_manager._summary_repo
            message_repo = self._agent_manager._msg_repo

            self._dashboard = DashboardService(
                self._agent_manager,
                self._council,
                self._override,
                action_log_repo,
                task_repo,
                memory_repo,
                message_repo,
                tool_call_repo,
            )
            self._dashboard.set_start_time(self._start_time)
            self._dashboard_server = DashboardServer(
                self._dashboard,
                self._config.dashboard,
            )
            await self._dashboard_server.start()

            # 9. 註冊 Slash Commands 到所有 Bot
            if self._agent_manager._ai_provider:
                from src.memory.long_term import LongTermMemoryService
                from src.memory.summary import SummaryService

                ltm = self._agent_manager._long_term_memory
                summary = self._agent_manager._summary_service

                for name, bot in self._agent_manager._bots.items():
                    tree = register_all_commands(
                        bot=bot,
                        agent_manager=self._agent_manager,
                        long_term_memory=ltm,
                        summary_service=summary,
                        action_log_repo=action_log_repo,
                        task_repo=task_repo,
                        memory_repo=memory_repo,
                        summary_repo=summary_repo,
                    )
                    # 儲存 tree 供後續 sync
                    if not hasattr(bot, '_command_tree'):
                        bot._command_tree = tree  # type: ignore[attr-defined]

            # 10. 設定優雅關閉
            self._setup_shutdown()

            logger.info("app.initialized", agents=len(self._agent_manager._agents))

            # 11. 啟動所有 Bot
            await self._agent_manager.start()

        except Exception as exc:
            logger.error("app.startup_error", error=str(exc), exc_info=True)
            await self.shutdown()
            sys.exit(1)

    def _setup_shutdown(self) -> None:
        """設定優雅關閉信號處理。"""
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
            except NotImplementedError:
                # Windows 不支援 add_signal_handler
                pass

    async def shutdown(self) -> None:
        """優雅關閉所有系統。"""
        logger.info("app.shutting_down")

        if self._dashboard_server:
            await self._dashboard_server.stop()

        if self._agent_manager:
            await self._agent_manager.stop()

        logger.info("app.stopped")


async def main() -> None:
    """主入口。"""
    app = Application()
    await app.run()


def run() -> None:
    """同步入口點，供 pyproject.toml script 使用。"""
    asyncio.run(main())


if __name__ == "__main__":
    run()
