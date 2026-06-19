"""資料庫連線抽象層。

支援 SQLite（預設）與 PostgreSQL，透過 connection string 自動切換。
使用 aiosqlite / asyncpg 提供非同步連線池。
"""

from __future__ import annotations

import aiosqlite
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


# ============================================================
# 抽象介面
# ============================================================


@runtime_checkable
class ConnectionProvider(Protocol):
    """資料庫連線提供者協定。"""

    async def execute(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> aiosqlite.Cursor | Any:
        ...

    async def executemany(
        self, query: str, parameters_seq: list[tuple[Any, ...]]
    ) -> aiosqlite.Cursor | Any:
        ...

    async def executescript(self, script: str) -> None:
        ...

    async def fetchone(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        ...

    async def fetchall(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        ...


# ============================================================
# SQLite 實作
# ============================================================


class SQLiteConnection:
    """SQLite 非同步連線管理。

    使用 aiosqlite 提供輕量級的單檔案資料庫。
    自動將 ROW 轉為 dict 以便上層使用。
    """

    def __init__(self, db_path: str) -> None:
        """初始化。

        Args:
            db_path: SQLite 檔案路徑，例如 "data/agent.db"。
        """
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """建立資料庫連線。"""
        self._db = await aiosqlite.connect(self._db_path)
        # 啟用 WAL 模式以提升並行讀寫效能
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # 使用 Row factory 將結果轉為 dict
        self._db.row_factory = aiosqlite.Row
        logger.info("database.connected", backend="sqlite", path=self._db_path)

    async def close(self) -> None:
        """關閉資料庫連線。"""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("database.closed", backend="sqlite")

    async def execute(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> aiosqlite.Cursor:
        """執行單條 SQL 語句。"""
        if not self._db:
            raise RuntimeError("資料庫未連線")
        cursor = await self._db.execute(query, parameters or ())
        await self._db.commit()
        return cursor

    async def executemany(
        self, query: str, parameters_seq: list[tuple[Any, ...]]
    ) -> aiosqlite.Cursor:
        """批次執行 SQL 語句。"""
        if not self._db:
            raise RuntimeError("資料庫未連線")
        cursor = await self._db.executemany(query, parameters_seq)
        await self._db.commit()
        return cursor

    async def executescript(self, script: str) -> None:
        """執行 SQL 腳本（多用於 migration）。"""
        if not self._db:
            raise RuntimeError("資料庫未連線")
        await self._db.executescript(script)
        await self._db.commit()

    async def fetchone(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        """查詢單筆紀錄，回傳 dict 或 None。"""
        if not self._db:
            raise RuntimeError("資料庫未連線")
        cursor = await self._db.execute(query, parameters or ())
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)  # type: ignore[arg-type]

    async def fetchall(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        """查詢多筆紀錄，回傳 list[dict]。"""
        if not self._db:
            raise RuntimeError("資料庫未連線")
        cursor = await self._db.execute(query, parameters or ())
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]  # type: ignore[arg-type]


# ============================================================
# 連線工廠
# ============================================================


class DatabaseConnection:
    """資料庫連線工廠與管理器。

    根據 connection string 自動選擇後端：
    - sqlite+aiosqlite:// → SQLiteConnection
    - postgresql+asyncpg:// → PostgreSQLConnection（需安裝 asyncpg）
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._provider: ConnectionProvider | None = None

    def _create_provider(self) -> ConnectionProvider:
        """根據 URL 建立對應的連線提供者。"""
        if self._url.startswith("sqlite"):
            # 提取路徑：sqlite+aiosqlite:///data/agent.db → data/agent.db
            path = self._url.split("///", 1)[-1]
            return SQLiteConnection(path)
        if self._url.startswith("postgresql"):
            # PostgreSQL 實作（需要 asyncpg）
            try:
                from src.database.pg_connection import PostgreSQLConnection

                return PostgreSQLConnection(self._url)
            except ImportError:
                raise ImportError(
                    "PostgreSQL 支援需要安裝 asyncpg：pip install asyncpg"
                )
        raise ValueError(f"不支援的資料庫 URL: {self._url}")

    async def connect(self) -> ConnectionProvider:
        """建立連線並回傳 provider。"""
        self._provider = self._create_provider()
        # 僅 SQLiteConnection 有 connect 方法
        if hasattr(self._provider, "connect"):
            await self._provider.connect()  # type: ignore[union-attr]
        return self._provider

    async def close(self) -> None:
        """關閉連線。"""
        if self._provider and hasattr(self._provider, "close"):
            await self._provider.close()  # type: ignore[union-attr]
        self._provider = None

    @property
    def provider(self) -> ConnectionProvider:
        """取得目前連線 provider。"""
        if self._provider is None:
            raise RuntimeError("資料庫未連線，請先呼叫 connect()")
        return self._provider
