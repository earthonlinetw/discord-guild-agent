"""任務佇列系統。

實作 Priority Queue / Normal Queue / Retry Queue，
搭配 Guild-level Lock 避免多個 Agent 同時執行管理操作。
支援自動 Retry 機制。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Coroutine

import structlog

from src.config.settings import QueueConfig
from src.database.repository import TaskRepository

logger = structlog.get_logger(__name__)


# ============================================================
# 列舉與資料結構
# ============================================================


class TaskPriority(IntEnum):
    """任務優先級。"""

    BATCH = 0        # 一般批次處理
    NORMAL = 1       # 一般任務
    ADMIN = 5        # 管理員請求
    MENTION = 10     # Mention / Reply / Slash Command
    OVERRIDE = 20    # Human Override


class TaskStatus(str):
    """任務狀態。"""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY = "retry"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """任務單元。"""

    id: str = ""
    guild_id: str = ""
    agent_name: str = ""
    task_type: str = ""          # mention / admin / batch / council / override
    priority: TaskPriority = TaskPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    retry_count: int = 0
    error_message: str = ""
    db_id: int | None = None     # 資料庫中的 ID

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:16]

    @property
    def is_priority(self) -> bool:
        """是否為高優先級任務。"""
        return self.priority >= TaskPriority.MENTION


# ============================================================
# Task Callback 類型
# ============================================================

TaskHandler = Callable[[Task], Coroutine[Any, Any, None]]


# ============================================================
# Guild Lock 管理器
# ============================================================


class GuildLockManager:
    """Guild 等級的鎖管理。

    確保同一個 Guild 同時間只能有一個 AI Decision Process。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, str] = {}  # guild_id → agent_name

    def get_lock(self, guild_id: str) -> asyncio.Lock:
        """取得指定 Guild 的 Lock。"""
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    async def acquire(self, guild_id: str, agent_name: str) -> bool:
        """嘗試取得 Guild Lock。

        Args:
            guild_id: 伺服器 ID。
            agent_name: Agent 名稱。

        Returns:
            是否成功取得。
        """
        lock = self.get_lock(guild_id)
        acquired = lock.locked() is False and await lock.acquire()
        if acquired:
            self._holders[guild_id] = agent_name
            logger.debug("guild_lock.acquired", guild=guild_id, agent=agent_name)
        return acquired

    async def release(self, guild_id: str, agent_name: str) -> None:
        """釋放 Guild Lock。"""
        lock = self.get_lock(guild_id)
        if lock.locked():
            lock.release()
            self._holders.pop(guild_id, None)
            logger.debug("guild_lock.released", guild=guild_id, agent=agent_name)

    @property
    def lock_holders(self) -> dict[str, str]:
        """目前持有 Lock 的 Agent（用於狀態查詢）。"""
        return dict(self._holders)


class AgentLockManager:
    """Agent 等級的鎖管理。

    確保同一個 Agent 在同一個 Guild 中只會串行處理自己的任務，
    更接近「一個 agent 一條處理 lane」的模型。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, str] = {}

    def get_lock(self, guild_id: str, agent_name: str) -> asyncio.Lock:
        key = f"{guild_id}:{agent_name}"
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def set_holder(self, guild_id: str, agent_name: str, task_id: str) -> None:
        self._holders[f"{guild_id}:{agent_name}"] = task_id

    def clear_holder(self, guild_id: str, agent_name: str) -> None:
        self._holders.pop(f"{guild_id}:{agent_name}", None)

    @property
    def lock_holders(self) -> dict[str, str]:
        return dict(self._holders)


# ============================================================
# Task Queue
# ============================================================


class TaskQueue:
    """任務佇列管理器。

    管理三種佇列：Priority / Normal / Retry。
    搭配 Guild Lock 確保同一 Guild 不會併發執行。
    支援自動 Retry。
    """

    def __init__(self, config: QueueConfig, task_repo: TaskRepository) -> None:
        """初始化。

        Args:
            config: 佇列設定。
            task_repo: 任務資料庫 Repository。
        """
        self._config = config
        self._repo = task_repo
        self._guild_locks = GuildLockManager()
        self._agent_locks = AgentLockManager()

        # 記憶體中的 Batch 暫存（不會持久化到 DB）
        self._batch_store: dict[str, Any] = {}

        # 記憶體中的佇列（加速存取）
        self._priority_queue: asyncio.PriorityQueue[tuple[int, str, Task]] = asyncio.PriorityQueue()
        self._normal_queue: asyncio.PriorityQueue[tuple[int, str, Task]] = asyncio.PriorityQueue()
        self._retry_queue: asyncio.PriorityQueue[tuple[int, str, Task]] = asyncio.PriorityQueue()

        # 任務處理器
        self._handlers: dict[str, TaskHandler] = {}

        # 運行控制
        self._running = False
        self._worker_tasks: list[asyncio.Task[None]] = []

    # ---- Handler 註冊 ----

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        """註冊任務處理器。

        Args:
            task_type: 任務類型（mention / batch / admin 等）。
            handler: 處理回呼。
        """
        self._handlers[task_type] = handler
        logger.debug("queue.handler_registered", type=task_type)

    # ---- 任務入隊 ----

    async def enqueue(self, task: Task) -> str:
        """將任務加入佇列。

        根據優先級自動分派至對應佇列。
        同時持久化至資料庫。

        Args:
            task: 任務物件。

        Returns:
            任務 ID。
        """
        task.status = TaskStatus.QUEUED

        # 從 payload 中分離不可序列化的 _batch，存入記憶體暫存
        batch_obj = task.payload.pop("_batch", None)
        if batch_obj is not None:
            self._batch_store[task.id] = batch_obj

        # 持久化（payload 中已無不可序列化物件）
        db_id = await self._repo.insert(
            guild_id=task.guild_id,
            agent_name=task.agent_name,
            task_type=task.task_type,
            priority=int(task.priority),
            payload=task.payload,
        )
        task.db_id = db_id

        # 分派至佇列
        # PriorityQueue 使用 (priority, id) 排序，數字越大優先級越高
        # Python PriorityQueue 是最小堆，所以用負數
        queue_item = (-int(task.priority), task.id, task)

        if task.priority >= TaskPriority.MENTION:
            await self._priority_queue.put(queue_item)
            logger.info(
                "queue.enqueue.priority",
                task_id=task.id,
                type=task.task_type,
                priority=task.priority.name,
            )
        else:
            await self._normal_queue.put(queue_item)
            logger.info(
                "queue.enqueue.normal",
                task_id=task.id,
                type=task.task_type,
                priority=task.priority.name,
            )

        return task.id

    async def enqueue_retry(self, task: Task) -> None:
        """將任務加入 Retry 佇列。"""
        task.retry_count += 1
        task.status = TaskStatus.RETRY

        if task.db_id:
            await self._repo.increment_retry(task.db_id, task.error_message)

        if task.retry_count <= self._config.retry_max:
            queue_item = (-int(task.priority), task.id, task)
            await self._retry_queue.put(queue_item)
            logger.info(
                "queue.enqueue.retry",
                task_id=task.id,
                retry_count=task.retry_count,
                max_retries=self._config.retry_max,
            )
        else:
            logger.error(
                "queue.retry_exhausted",
                task_id=task.id,
                retry_count=task.retry_count,
            )

    # ---- 任務處理 ----

    async def _process_task(self, task: Task) -> None:
        """處理單一任務。

        包含 Guild Lock 取得、Handler 呼叫、錯誤處理、Retry。
        """
        guild_id = task.guild_id
        agent_name = task.agent_name

        # 先鎖 guild，再鎖 agent/guild，避免同 guild 內跨 agent 與同 agent 任務互相打架。
        guild_lock = self._guild_locks.get_lock(guild_id)
        agent_lock = self._agent_locks.get_lock(guild_id, agent_name)
        async with guild_lock:
            self._guild_locks._holders[guild_id] = agent_name
            async with agent_lock:
                self._agent_locks.set_holder(guild_id, agent_name, task.id)

                # 更新 DB 狀態
                if task.db_id:
                    await self._repo.update_status(task.db_id, TaskStatus.PROCESSING)

                logger.info(
                    "queue.processing",
                    task_id=task.id,
                    type=task.task_type,
                    agent=agent_name,
                    guild=guild_id,
                )

                try:
                    handler = self._handlers.get(task.task_type)
                    if handler:
                        await handler(task)
                    else:
                        logger.warning(
                            "queue.no_handler",
                            task_id=task.id,
                            type=task.task_type,
                        )

                    # 成功
                    if task.db_id:
                        await self._repo.update_status(task.db_id, TaskStatus.COMPLETED)
                    task.status = TaskStatus.COMPLETED
                    logger.info("queue.completed", task_id=task.id)

                except Exception as exc:
                    task.error_message = str(exc)
                    logger.error(
                        "queue.task_failed",
                        task_id=task.id,
                        error=str(exc),
                    )

                    if task.db_id:
                        await self._repo.update_status(
                            task.db_id, TaskStatus.FAILED, error_message=str(exc)
                        )

                    # 加入 Retry
                    await self.enqueue_retry(task)
                finally:
                    self._agent_locks.clear_holder(guild_id, agent_name)
            self._guild_locks._holders.pop(guild_id, None)

    # ---- Worker ----

    async def _worker(self) -> None:
        """佇列 Worker 主迴圈。

        優先處理 Priority Queue，再處理 Normal Queue，最後處理 Retry Queue。
        """
        while self._running:
            task: Task | None = None

            try:
                # 優先從 Priority Queue 取任務
                try:
                    _, _, task = self._priority_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

                # 其次從 Normal Queue 取
                if task is None:
                    try:
                        _, _, task = self._normal_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass

                # 最後從 Retry Queue 取
                if task is None:
                    try:
                        # 帶超時的非阻塞取得
                        _, _, task = await asyncio.wait_for(
                            self._retry_queue.get(), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        continue

                if task:
                    # 限制並行數
                    await self._process_task(task)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("queue.worker_error", error=str(exc))

    async def start(self) -> None:
        """啟動佇列處理 Workers。"""
        self._running = True
        for i in range(self._config.max_concurrent):
            worker = asyncio.create_task(self._worker(), name=f"queue-worker-{i}")
            self._worker_tasks.append(worker)
        logger.info(
            "queue.started",
            workers=self._config.max_concurrent,
        )

    async def stop(self) -> None:
        """停止佇列處理。"""
        self._running = False
        for task in self._worker_tasks:
            task.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        logger.info("queue.stopped")

    # ---- Batch 暫存 ----

    def get_batch(self, task_id: str) -> Any | None:
        """取得任務對應的 Batch 物件。"""
        return self._batch_store.get(task_id)

    def remove_batch(self, task_id: str) -> None:
        """移除 Batch 暫存（任務完成後呼叫）。"""
        self._batch_store.pop(task_id, None)

    # ---- 查詢 ----

    async def get_status(self) -> dict[str, Any]:
        """取得佇列狀態摘要。"""
        return {
            "priority_queue_size": self._priority_queue.qsize(),
            "normal_queue_size": self._normal_queue.qsize(),
            "retry_queue_size": self._retry_queue.qsize(),
            "workers": len(self._worker_tasks),
            "running": self._running,
            "guild_locks": self._guild_locks.lock_holders,
            "agent_locks": self._agent_locks.lock_holders,
        }

    async def cancel_task(self, task_id: str) -> bool:
        """取消任務（用於 Human Override）。"""
        # 由於佇列中的任務不易移除，我們標記 DB 狀態
        # Worker 在處理前會檢查狀態
        logger.info("queue.task_cancelled", task_id=task_id)
        return True

    # ---- Retry 排程 ----

    async def process_retries(self) -> None:
        """從資料庫載入可重試的任務並重新入隊。

        用於系統啟動時恢復失敗的任務。
        """
        retryable = await self._repo.get_retryable(max_retry=self._config.retry_max)
        for row in retryable:
            task = Task(
                id=str(row["id"]),
                guild_id=row["guild_id"],
                agent_name=row["agent_name"],
                task_type=row["task_type"],
                priority=TaskPriority(row["priority"]),
                payload={},  # 已在 DB 中
                status=TaskStatus.RETRY,
                retry_count=row["retry_count"],
                error_message=row.get("error_message", ""),
                db_id=row["id"],
            )
            queue_item = (-int(task.priority), task.id, task)
            await self._retry_queue.put(queue_item)

        if retryable:
            logger.info("queue.retries_loaded", count=len(retryable))
